"""AiService state machine: routes events between camera and robot.

Architecture (two WebSockets, JSON protocol):
  * the camera connects to /camera and sends JSON: audio (base64 PCM from its
    mic), pictures (base64 JPEG), face events and face emotions; the service
    may ask it for a picture (capture) or mute/unmute the mic;
  * the robot connects to /robot and receives JSON playback chunks
    ({"type":"audio","audio":"<b64>"}), movements and emotions; it replies
    with hb/ack.

States:
  DISCONNECTED — nothing connected;
  IDLE         — at least one peer connected, no active audio segment;
  STREAMING    — camera is streaming an audio segment.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any, Dict, Optional

from . import config as app_config
from . import protocol as proto
from . import recorder as recorder_mod
from .camera import CameraSession, EV_AUDIO, EV_IMAGE, EV_FACE, EV_EMOTION
from .emotion_decay import EmotionDecay
from .processor import Processor
from .robot import RobotSession

logger = logging.getLogger("uvicorn")

# After an audio pause of this length _in.wav is closed (finalized). The
# camera with VAD sends silence up to silence_seconds (2 s) and stops — the
# 3 s gap guarantees the file closes at the end of a speech segment.
AUDIO_SEGMENT_GAP = 3.0

# Extra time after the last playback frame during which camera audio is still
# dropped: the robot drains its playback queue/DMA tail after the EOF marker.
PLAYBACK_DROP_TAIL_S = 2.0

# Watchdog: after this silence (no audio and no HB at all) from the camera a
# warning is logged — the camera may have died/disconnected while the WS still
# looks alive to the peer.
CAMERA_SILENCE_WARN_S = 30.0

# Watchdog: an STT segment open for longer than this is force-closed (the
# gRPC stream may hang after an idle period; without the close the next
# utterance would reuse the dead stream and the robot would stay silent).
SEGMENT_MAX_AGE_S = 120.0


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    STREAMING = "streaming"


class AiStateMachine:
    def __init__(self, camera: CameraSession, robot: RobotSession,
                 processor: Processor,
                 rec: Optional[recorder_mod.Recorder] = None) -> None:
        self.camera = camera
        self.robot = robot
        self.ai = processor
        self.rec = rec
        self.state = State.DISCONNECTED
        # Echo-loop protection: while the robot plays, camera audio is dropped
        # (config robot.drop_audio_during_playback). _drop_until — monotonic
        # time when the playback (incl. tail) finishes.
        try:
            self._drop_audio = bool(app_config.CONFIG["robot"].get(
                "drop_audio_during_playback", True))
        except Exception:  # noqa: BLE001
            self._drop_audio = True
        self._drop_until = 0.0
        # Background GPT+TTS answer task for the last recognized utterance.
        self._answer_task: Optional[asyncio.Task] = None
        self._answer_lock = asyncio.Lock()
        # Post-dialogue emotion decay (Neutral -> Sad -> Sleepy), see
        # emotion_decay.py: GPT phrase + TTS for each stage.
        self._decay = EmotionDecay(self.ai, self.robot,
                                   send_audio=self.send_robot_audio)
        # Any recognized speech restarts the decay countdown (the emotion
        # change is postponed): the first STT word restarts it via
        # _on_first_word, the final segment text — in _schedule_answer.
        self.ai.on_partial = self._on_first_word
        # True while the camera streams audio chunks (no segment markers in
        # the JSON protocol — the stream is continuous).
        self._streaming = False
        # Monotonic time of the last camera audio chunk (segment closing).
        self._last_audio = 0.0
        # True while a speech segment is active (STT stream is open).
        self._seg_active = False
        # Monotonic time when the current STT segment was opened (watchdog).
        self._seg_started_at = 0.0
        # Playback pacing (chunk size / delivery speed) from the config.
        try:
            self._play_secs = float(
                app_config.CONFIG["robot"]["play_chunk_seconds"])
        except Exception:  # noqa: BLE001
            self._play_secs = 0.15
        if self._play_secs <= 0 or self._play_secs > 10:
            self._play_secs = 0.15
        try:
            self._play_speed = float(
                app_config.CONFIG["robot"]["play_speed"])
        except Exception:  # noqa: BLE001
            self._play_speed = 1.0
        if self._play_speed <= 0 or self._play_speed > 10:
            self._play_speed = 1.0

    # ------------------------------------------------------------------
    # Connection events.
    # ------------------------------------------------------------------
    async def on_camera_connected(self) -> None:
        logger.info("Camera connected: %s", self.camera.peer)
        # Fresh camera session: clear any state left from a previous
        # connection (stuck STT segment, stale timers) so the new session
        # starts clean and /health shows the fresh values.
        self._streaming = False
        self._last_audio = 0.0
        self._seg_started_at = 0.0
        if self._seg_active:
            self._seg_active = False
            self.ai.reset_segment()
        if self.rec is not None:
            self.rec.close_audio()
        self._update_state()

    async def on_camera_disconnected(self) -> None:
        logger.info("Camera disconnected")
        self._streaming = False
        if self.rec is not None:
            self.rec.close_audio()
        # Disconnect in the middle of an utterance: finalize STT and save
        # whatever was recognized.
        if self._seg_active:
            self._seg_active = False
            text = await self.ai.end_segment()
            if text and self.rec is not None:
                self.rec.save_stt_text(text)
        self._update_state()

    async def on_robot_connected(self) -> None:
        logger.info("Robot connected: %s", self.robot.peer)
        self._update_state()
        # Start the emotion decay countdown right away: the robot may keep an
        # emotion from a previous session (e.g. Sad after a mid-decay
        # disconnect), so Neutral -> Sad -> Sleepy restarts from Neutral and
        # the face cannot stay stuck. A dialogue restarts/cancels it.
        self._decay.start()

    async def on_robot_disconnected(self) -> None:
        logger.info("Robot disconnected")
        self._update_state()

    def _update_state(self) -> None:
        if not self.camera.connected and not self.robot.connected:
            self.state = State.DISCONNECTED
        elif self.camera.connected and self._streaming:
            self.state = State.STREAMING
        else:
            self.state = State.IDLE

    # ------------------------------------------------------------------
    # Watchdog (started by the server lifespan): silent-camera diagnostics
    # and stuck-STT-segment recovery.
    # ------------------------------------------------------------------
    async def run_watchdog(self) -> None:
        """Periodic diagnostics loop (every 10 s).

        * If the camera is connected but sends NOTHING (no audio, no HB) for
          a long time — a warning is logged (the camera may be dead/hung
          while its WS peer still looks alive).
        * If an STT segment stays open for too long — it is force-closed so
          the next utterance starts a fresh recognition stream (a hung gRPC
          stream would otherwise keep the robot silent forever).
        """
        while True:
            await asyncio.sleep(10.0)
            now = time.monotonic()
            try:
                if self.camera.connected:
                    age = (now - self.camera.last_activity
                           if self.camera.last_activity else -1.0)
                    if age < 0 or age > CAMERA_SILENCE_WARN_S:
                        logger.warning(
                            "[camera] silent for %.0f s (no messages; "
                            "peer=%s)", max(age, 0.0), self.camera.peer)
                elif self.robot.connected:
                    logger.warning("[camera] not connected (robot is online)")
                if (self._seg_active and self._seg_started_at
                        and now - self._seg_started_at > SEGMENT_MAX_AGE_S):
                    logger.warning(
                        "[ai] STT segment stuck for %.0f s — resetting it",
                        now - self._seg_started_at)
                    self._seg_active = False
                    self._seg_started_at = 0.0
                    self.ai.reset_segment()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] watchdog error: %s", exc)

    # ------------------------------------------------------------------
    # JSON message from the camera.
    # ------------------------------------------------------------------
    async def on_camera_message(self, text: str) -> None:
        """Handles a camera JSON message: media events + ok/error reply."""
        events = await self.camera.on_message(text)
        if events is None:
            logger.warning("[camera] malformed message: %s", text[:96])
            await self.camera.send_text(proto.error_message("malformed message"))
            return
        for event in events:
            kind = event[0]
            if kind == EV_AUDIO:
                now = time.monotonic()
                # The robot is playing — drop the camera audio so the speaker
                # echo does not trigger a new utterance (loop).
                if self._drop_audio and now < self._drop_until:
                    logger.debug("[camera] audio dropped during playback")
                    continue
                # The first chunk after a pause = start of a new utterance:
                # open the Yandex STT recognition stream. The emotion decay
                # is NOT restarted here (the camera often produces false
                # segments from background noise, "empty text") — it is
                # postponed by non-empty STT results: on the first recognized
                # word (on_partial -> _on_first_word) and once more when the
                # final segment text arrives (_schedule_answer).
                if not self._seg_active or \
                        now - self._last_audio >= AUDIO_SEGMENT_GAP:
                    await self.ai.begin_segment()
                    self._seg_active = True
                    self._seg_started_at = time.monotonic()
                self._last_audio = now
                self._streaming = True
                self._update_state()
                if self.rec is not None:
                    self.rec.feed_audio(event[1])
                await self.ai.process_audio(event[1])
            elif kind == EV_IMAGE:
                if self.rec is not None:
                    self.rec.save_image(event[1])
                detections = await self.ai.detect_objects(event[1])
                if detections:
                    logger.info("Objects detected: %d", len(detections))
            elif kind == EV_FACE:
                await self.ai.on_face(event[1], event[2])
            elif kind == EV_EMOTION:
                await self.ai.on_camera_emotion(event[1])
        # An audio pause = end of the utterance: finalize _in.wav and the STT
        # stream, save the recognized text next to the WAV (<stamp>_in.txt),
        # then generate the robot answer (GPT + TTS) in the background.
        if (self._last_audio
                and time.monotonic() - self._last_audio >= AUDIO_SEGMENT_GAP):
            if self.rec is not None:
                if self.rec.close_audio():
                    logger.info("[camera] audio pause: _in.wav segment closed")
            if self._seg_active:
                self._seg_active = False
                text = await self.ai.end_segment()
                if text and self.rec is not None:
                    self.rec.save_stt_text(text)
                if text:
                    self._schedule_answer(text)
        self._update_state()
        await self.camera.send_text(proto.ok_message())

    # ------------------------------------------------------------------
    # JSON message from the robot.
    # ------------------------------------------------------------------
    async def on_robot_message(self, text: str) -> None:
        self.robot.note_message(text)
        await self.ai.on_robot_text(text)

    # ------------------------------------------------------------------
    # Answer pipeline: GPT + TTS -> robot playback (background).
    # ------------------------------------------------------------------
    def _schedule_answer(self, text: str) -> None:
        """Starts the answer pipeline (GPT + TTS -> robot playback).

        A background task keeps the camera message loop unblocked while the
        answer is generated; the lock serializes overlapping answers (a new
        utterance while the previous one is still being processed is skipped).
        """
        # Recognized speech — restart the decay countdown: the emotion change
        # is postponed by the whole delay again (already restarted by the
        # first word; this is the safety net for segments without partials).
        # An empty-text segment never reaches this point, so false camera
        # triggers cannot kill the decay.
        self._decay.start()
        if self._answer_task is not None and not self._answer_task.done():
            logger.warning("[ai] answer task still busy — skipping utterance")
            return
        self._answer_task = asyncio.create_task(self._answer_worker(text))

    def _on_first_word(self, part: str) -> None:
        """First STT-recognized word of a new utterance — postpone the decay.

        Called by Processor.process_audio() from the asyncio loop as soon as
        SpeechKit reports a non-empty partial, so the decay countdown is
        restarted immediately when the user starts speaking (not after the
        whole segment ends). start() internally cancels the previous run.
        """
        logger.info("[ai] first word heard (%r) — restarting decay", part)
        self._decay.start()

    async def _answer_worker(self, text: str) -> None:
        """Background worker: recognizes -> GPT answer -> TTS -> playback."""
        try:
            async with self._answer_lock:
                result = await self.ai.ask(text)
                if not result:
                    return
                pcm, emotion = result
                if emotion:
                    logger.info("[ai] answer emotion -> %s", emotion)
                    await self.send_robot_emotion(emotion)
                if pcm:
                    await self.send_robot_audio(pcm)
                # Dialogue finished — schedule the emotion decay countdown.
                self._decay.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] answer pipeline error: %s", exc)

    # ------------------------------------------------------------------
    # Service -> robot actions.
    # ------------------------------------------------------------------
    async def send_robot_move(self, axis: str, degrees: int = 0) -> bool:
        return await self.robot.send_movement(axis, degrees)

    async def send_robot_emotion(self, name: str) -> bool:
        return await self.robot.send_emotion(name)

    async def send_robot_audio(self, pcm: bytes) -> bool:
        """Sends playback PCM to the robot as binary frames.

        Frame = [type=1][codec=1][raw PCM int16 LE 16 kHz mono] (see
        protocol.robot_audio_frame). Chunk length — config
        robot.play_chunk_seconds (default 0.15 s = 4800 B): keeps each frame
        below the robot WS client receive limit (~8 KB). Delivery speed —
        config robot.play_speed (1.0 = real time): the pause between frames
        equals the chunk duration / play_speed, so the robot's playback
        queue never overflows (no stutter) and the stream ends with an empty
        [type][codec] frame (EOF marker).

        Returns False immediately when the robot is offline or pcm is empty —
        the server never waits for a missing peer (no crash/hang).
        """
        if not self.robot.connected:
            logger.info("PLAY: robot offline, skipping %d B", len(pcm))
            return False
        if not pcm:
            return False
        rate = app_config.CONFIG["audio"]["sample_rate"]
        chunk_size = round(rate * self._play_secs) * 2  # N s of PCM
        chunk_dur = chunk_size / (2 * rate)
        n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
        sent_any = False
        # Drop camera audio from the very first frame: the robot starts
        # playing as soon as the first frames arrive, so the echo window must
        # cover the whole delivery time (len/(2*rate)/play_speed) + tail —
        # setting it only after the send loop would leave the first seconds
        # unprotected (the camera hears the beginning of the phrase).
        if self._drop_audio:
            playback_secs = len(pcm) / (2.0 * rate) / self._play_speed
            self._drop_until = (time.monotonic() + playback_secs
                                + PLAYBACK_DROP_TAIL_S)
            logger.info("PLAY: camera audio dropped for %.1f s "
                        "(playback %.1f s + tail %.1f s)",
                        playback_secs + PLAYBACK_DROP_TAIL_S,
                        playback_secs, PLAYBACK_DROP_TAIL_S)
        logger.info("PLAY: playback %d B (%d frames of %.2f s, speed x%.2f)",
                    len(pcm), n_chunks, chunk_dur, self._play_speed)
        if self.rec is not None:
            self.rec.start_out()
        for idx in range(n_chunks):
            part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
            ok = await self.robot.send_audio_frame(
                proto.ROBOT_AUDIO_FRAME_TYPE, proto.ROBOT_AUDIO_CODEC_PCM,
                part)
            if ok and self.rec is not None:
                self.rec.feed_out_audio(part)
            sent_any = sent_any or ok
            if not ok:
                logger.warning("PLAY: dropped at frame %d/%d",
                               idx + 1, n_chunks)
                break
            # Pause = chunk duration / delivery speed (real-time pacing).
            await asyncio.sleep(chunk_dur / self._play_speed)
        if sent_any:
            eof_ok = await self.robot.send_audio_frame(
                proto.ROBOT_AUDIO_FRAME_TYPE, proto.ROBOT_AUDIO_CODEC_PCM,
                b"")
            logger.info("PLAY: EOF marker -> %s",
                        "ok" if eof_ok else "NO CONNECTION")
            if self.rec is not None:
                self.rec.close_out()
        elif self.rec is not None:
            self.rec.close_out()
        logger.info("PLAY: finished (%d of %d frames)",
                    n_chunks if sent_any else 0, n_chunks)
        return sent_any

    # ------------------------------------------------------------------
    # /health summary.
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "state": self.state.value,
            "camera_connected": self.camera.connected,
            "robot_connected": self.robot.connected,
            "camera_activity_age_s": round(
                now - self.camera.last_activity, 1)
            if self.camera.last_activity else None,
            "last_audio_age_s": round(now - self._last_audio, 1)
            if self._last_audio else None,
            "seg_active": self._seg_active,
            "decay_running": self._decay.running,
            "camera": {
                "device": self.camera.device,
                "frames": self.camera.frames_total,
                "audio_frames": self.camera.audio_frames,
                "image_frames": self.camera.image_frames,
                "audio_bytes": self.camera.audio_bytes,
                "face_events": self.camera.face_events,
                "emotion_events": self.camera.emotion_events,
                "last_face": self.camera.last_face,
            },
            "robot": {
                "move_commands": self.robot.move_commands,
                "emotion_commands": self.robot.emotion_commands,
                "playback_bytes": self.robot.playback_bytes,
                "last_ack": self.robot.last_ack,
            },
        }