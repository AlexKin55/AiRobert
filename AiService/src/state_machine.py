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
from typing import Any, Dict, List, Optional

from . import config as app_config
from . import protocol as proto
from . import recorder as recorder_mod
from .camera import CameraSession, EV_AUDIO, EV_IMAGE, EV_FACE, EV_EMOTION
from .processor import Processor
from .robot import RobotSession

logger = logging.getLogger("uvicorn")

# After an audio pause of this length _in.wav is closed (finalized). The
# camera with VAD sends silence up to silence_seconds (2 s) and stops — the
# 3 s gap guarantees the file closes at the end of a speech segment.
AUDIO_SEGMENT_GAP = 3.0


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
        # True while the camera streams audio chunks (no segment markers in
        # the JSON protocol — the stream is continuous).
        self._streaming = False
        # Monotonic time of the last camera audio chunk (segment closing).
        self._last_audio = 0.0
        # True while a speech segment is active (STT stream is open).
        self._seg_active = False
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
                # The first chunk after a pause = start of a new utterance:
                # open the Yandex STT recognition stream.
                if not self._seg_active or \
                        now - self._last_audio >= AUDIO_SEGMENT_GAP:
                    await self.ai.begin_segment()
                    self._seg_active = True
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
        # stream, save the recognized text next to the WAV (<stamp>_in.txt).
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
        self._update_state()
        await self.camera.send_text(proto.ok_message())

    # ------------------------------------------------------------------
    # JSON message from the robot.
    # ------------------------------------------------------------------
    async def on_robot_message(self, text: str) -> None:
        self.robot.note_message(text)
        await self.ai.on_robot_text(text)

    # ------------------------------------------------------------------
    # Service -> robot actions.
    # ------------------------------------------------------------------
    async def send_robot_move(self, axis: str, degrees: int = 0) -> bool:
        return await self.robot.send_movement(axis, degrees)

    async def send_robot_emotion(self, name: str) -> bool:
        return await self.robot.send_emotion(name)

    async def send_robot_audio(self, pcm: bytes) -> bool:
        """Sends playback PCM to the robot in JSON chunks.

        Chunk length — config robot.play_chunk_seconds (default 0.15 s,
        4800 B -> 6400 chars base64): keeps each JSON message small for the
        ESP32 WS client. Delivery speed — config robot.play_speed (1.0 =
        real time). Ends with {"type":"audio","audio":""} (EOF marker).
        """
        rate = app_config.CONFIG["audio"]["sample_rate"]
        chunk_size = round(rate * self._play_secs) * 2  # N s of PCM
        chunk_dur = chunk_size / (2 * rate)
        n_chunks = (len(pcm) + chunk_size - 1) // chunk_size
        sent_any = False
        logger.info("PLAY: playback %d B (%d chunks of %.2f s, speed x%.2f)",
                    len(pcm), n_chunks, chunk_dur, self._play_speed)
        if self.rec is not None:
            self.rec.start_out()
        for idx in range(n_chunks):
            part = pcm[idx * chunk_size:(idx + 1) * chunk_size]
            ok = await self.robot.send_audio_chunk(part)
            if ok and self.rec is not None:
                self.rec.feed_out_audio(part)
            sent_any = sent_any or ok
            if not ok:
                logger.warning("PLAY: dropped at chunk %d/%d", idx + 1, n_chunks)
                break
            await asyncio.sleep(chunk_dur / self._play_speed)
        if sent_any:
            eof_ok = await self.robot.send_audio_chunk(b"")
            logger.info("PLAY: EOF marker -> %s",
                        "ok" if eof_ok else "NO CONNECTION")
            if self.rec is not None:
                self.rec.close_out()
        elif self.rec is not None:
            self.rec.close_out()
        logger.info("PLAY: finished (%d of %d chunks)",
                    n_chunks if sent_any else 0, n_chunks)
        return sent_any

    # ------------------------------------------------------------------
    # /health summary.
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "camera_connected": self.camera.connected,
            "robot_connected": self.robot.connected,
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