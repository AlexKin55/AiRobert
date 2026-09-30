"""AiService state machine: routes events between camera and robot.

Architecture (two WebSockets, JSON protocol):
  * the camera connects to /camera and sends JSON: audio (base64 PCM from its
    mic), pictures (base64 JPEG), face events and face emotions; the service
    may ask it for a picture (capture) or mute/unmute the mic;
  * the robot connects to /robot and receives binary playback frames
    ([type][codec][raw PCM]) plus movement/emotion commands; it replies
    with hb/ack.

AI processing — the Yandex Realtime API (audio question -> audio answer):
  * camera PCM goes straight into one persistent Realtime session
    (Processor.process_audio); the server-side VAD detects the end of the
    utterance and the Speech Realtime model answers in a single pass;
  * the finished answer arrives via Processor callbacks:
      on_answer(pcm, text)      -> emotion command + robot playback;
      on_user_text(transcript)  -> log/debug files;
      on_speech_started()       -> the emotion-decay countdown is postponed.

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
from . import yandex as yandex_mod
from .camera import CameraSession, EV_AUDIO, EV_IMAGE, EV_FACE, EV_EMOTION
from .emotion_decay import EmotionDecay, _load_pcm
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
        # Serializes robot playback: the Realtime answer and the emotion-decay
        # phrases share the speaker, so only one stream plays at a time.
        self._play_lock = asyncio.Lock()
        # Realtime answer callback (pcm, text) — the voice of the main dialog.
        self.ai.on_answer = self._on_realtime_answer
        # Question transcript callback (logging).
        self.ai.on_user_text = self._on_user_text
        # Speech-start callback: the first words restart the decay countdown.
        self.ai.on_speech_started = self._on_speech_started
        # Function-call emotion callback: the model CALLS emotion(name)
        # instead of speaking it (see callbacks.emotion_tool / Realtime tools).
        self.ai.on_emotion = self._on_realtime_emotion
        # Function-call weather callback: returns a summary the model voices.
        self.ai.on_weather = self._on_realtime_weather
        # Post-dialogue emotion decay (Neutral -> Sad -> Sleepy), see
        # emotion_decay.py: one-shot Realtime phrase + playback per stage.
        self._decay = EmotionDecay(self.ai, self.robot,
                                   send_audio=self._play_audio)
        # Head-touch response (touch section): when the robot reports a touch
        # event, play sound_file on it (see _play_touch_sound).
        try:
            _t = app_config.CONFIG.get("touch", {})
            self._touch_enabled = bool(_t.get("enabled", True))
            self._touch_sound_file = str(_t.get("sound_file",
                                                "sounds/touch.wav"))
        except Exception:  # noqa: BLE001
            self._touch_enabled = True
            self._touch_sound_file = "sounds/touch.wav"
        self._touch_pcm: Optional[bytes] = None
        # True while the camera streams audio chunks (no segment markers in
        # the JSON protocol — the stream is continuous).
        self._streaming = False
        # Monotonic time of the last camera audio chunk (segment closing).
        self._last_audio = 0.0
        # True while an _in.wav recording segment is active (closed after a
        # pause longer than AUDIO_SEGMENT_GAP).
        self._audio_seg_active = False
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
        # connection (stale timers) so the new session starts clean and
        # /health shows the fresh values.
        self._streaming = False
        self._last_audio = 0.0
        self._audio_seg_active = False
        if self.rec is not None:
            self.rec.close_audio()
        self.send_robot_move(0,0)
        self._update_state()

    async def on_camera_disconnected(self) -> None:
        logger.info("Camera disconnected")
        self._streaming = False
        self._audio_seg_active = False
        if self.rec is not None:
            self.rec.close_audio()
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
    # and Realtime-session recovery.
    # ------------------------------------------------------------------
    async def run_watchdog(self) -> None:
        """Periodic diagnostics loop (every 10 s).

        * If the camera is connected but sends NOTHING (no audio, no HB) for
          a long time — a warning is logged (the camera may be dead/hung
          while its WS peer still looks alive).
        * If the persistent Yandex Realtime session went down — it is
          reconnected so the next utterance is answered as usual.
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
                if self.ai.enabled and not self.ai.connected:
                    await self.ai.ensure_running()
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
                # The first chunk after a pause = start of a new recording
                # segment (a new _in.wav is created by the recorder). The
                # audio itself goes into the persistent Realtime session —
                # the server-side VAD closes the utterance there.
                if not self._audio_seg_active or \
                        now - self._last_audio >= AUDIO_SEGMENT_GAP:
                    self._audio_seg_active = True
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
                movement = await self.ai.on_face(event[1], event[2], event[3], event[4])
                if movement is not None:
                    pan, tilte = movement
                    await self.send_robot_move(pan, tilte)

            elif kind == EV_EMOTION:
                await self.ai.on_camera_emotion(event[1])
        # An audio pause = end of the recording segment: finalize _in.wav.
        # (The Realtime VAD ends the utterance on its own, faster.)
        if (self._last_audio
                and time.monotonic() - self._last_audio >= AUDIO_SEGMENT_GAP):
            if self.rec is not None:
                if self.rec.close_audio():
                    logger.info("[camera] audio pause: _in.wav segment closed")
            self._audio_seg_active = False
        self._update_state()
        await self.camera.send_text(proto.ok_message())

    # ------------------------------------------------------------------
    # JSON message from the robot.
    # ------------------------------------------------------------------
    async def on_robot_message(self, text: str) -> None:
        mtype = self.robot.note_message(text)
        if mtype == proto.MSG_TOUCH and self._touch_enabled:
            # The robot shows a happy face and plays the touch sound.
            await self.send_robot_emotion("happy")
            await self._play_touch_sound()
        await self.ai.on_robot_text(text)

    # ------------------------------------------------------------------
    # Realtime answer pipeline (audio question -> audio answer).
    # ------------------------------------------------------------------
    def _on_speech_started(self) -> None:
        """The user started speaking — postpone the emotion decay.

        Called by Processor as soon as the Realtime VAD reports speech, so
        the decay countdown restarts immediately when the user starts
        speaking (not after the whole answer). start() cancels the previous
        run internally.
        """
        logger.info("[ai] speech started — restarting decay")
        self._decay.start()

    async def _on_user_text(self, transcript: str) -> None:
        """Question transcript from the Realtime session — log only (the
        debug files are saved by the Processor)."""
        logger.info("[ai] user said: %r", transcript)

    async def _on_realtime_emotion(self, name: str) -> None:
        """Emotion from the model's function call (tools / function calling).

        The model calls ``emotion(name)`` instead of speaking the command, so
        the answer audio never contains "Emotion: ...". The robot gets the
        EMOTION:<name> text command.
        """
        if name not in proto.ROBOT_EMOTIONS:
            logger.warning("[ai] emotion function call: unknown %r", name)
            return
        logger.info("[ai] answer emotion (function call) -> %s", name)
        await self.send_robot_emotion(name)

    async def _on_realtime_weather(self, city: str) -> str:
        """Weather from the model's function call (tools / function calling).

        Uses the Yandex Weather REST API v2/forecast (X-Yandex-Weather-Key,
        key from ``yandex.weather_api_key`` config or the
        YANDEX_WEATHER_API_KEY env); the city is resolved to coordinates via
        the Open-Meteo geocoder (no key required). Returns a short Russian
        summary which goes back to the model as function_call_output and is
        voiced to the user.
        """
        import os

        import requests
        from urllib.parse import quote

        key = os.environ.get("YANDEX_WEATHER_API_KEY", "").strip() or \
            str(app_config.CONFIG.get("yandex", {}).get(
                "weather_api_key", "")).strip()
        if not key:
            logger.warning("[ai] weather(%r): no Yandex Weather API key "
                           "(yandex.weather_api_key / YANDEX_WEATHER_API_KEY)",
                           city)
            return (f"Не могу получить погоду: не задан ключ Яндекс Погоды. "
                    f"Добавьте weather_api_key в настройки сервиса.")
        try:
            # City -> coordinates (Open-Meteo geocoder, free, works without key).
            geo = await asyncio.to_thread(
                requests.get,
                f"https://geocoding-api.open-meteo.com/v1/search"
                f"?name={quote(city)}&count=1&language=ru&format=json",
                timeout=8.0)
            results = (geo.json() or {}).get("results") or []
            if not results:
                return f"Не удалось найти город {city}."
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] weather(%r) geocoding error: %s", city, exc)
            return f"Не удалось определить координаты города {city}."
        try:
            resp = await asyncio.to_thread(
                requests.get,
                f"https://api.weather.yandex.ru/v2/forecast"
                f"?lat={lat}&lon={lon}&lang=ru_RU",
                headers={"X-Yandex-Weather-Key": key},
                timeout=10.0)
            payload = resp.json()
            fact = payload.get("fact") or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] weather(%r) request error: %s", city, exc)
            return f"Не удалось получить погоду для города {city}."
        try:
            temp = fact.get("temp")
            desc = self._weather_desc(fact.get("condition"))
            wind = fact.get("wind_speed")
            parts = [f"В городе {city} сейчас {desc}"]
            if temp is not None:
                parts.append(f"температура {temp} градусов")
            if wind is not None:
                parts.append(f"ветер {wind} метров в секунду")
            return ", ".join(parts) + "."
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] weather(%r) parse error: %s", city, exc)
            return f"Не удалось получить погоду для города {city}."

    @staticmethod
    def _weather_desc(condition: Any) -> str:
        """Maps the Yandex Weather v2 ``fact.condition`` code to Russian."""
        cond_map = {
            "clear": "ясно",
            "partly-cloudy": "переменная облачность",
            "cloudy": "облачно с прояснениями",
            "overcast": "пасмурно",
            "drizzle": "морось",
            "light-rain": "небольшой дождь",
            "rain": "дождь",
            "moderate-rain": "умеренный дождь",
            "heavy-rain": "сильный дождь",
            "continuous-heavy-rain": "длительный сильный дождь",
            "showers": "ливень",
            "wet-snow": "дождь со снегом",
            "light-snow": "небольшой снег",
            "snow": "снег",
            "snow-showers": "снегопад",
            "hail": "град",
            "thunderstorm": "гроза",
            "thunderstorm-with-rain": "гроза с дождём",
            "thunderstorm-with-hail": "гроза с градом",
        }
        return cond_map.get(str(condition).lower(),
                            str(condition).lower().replace("-", " "))

    async def _on_realtime_answer(self, pcm: bytes, text: str) -> None:
        """Full model answer from the Realtime session: emotion + playback.

        Runs in the background (scheduled by the Processor); the playback
        lock serializes it against emotion-decay phrases so the robot never
        mixes two streams. The primary emotion path is the function call
        (``_on_realtime_emotion``); the trailing "Emotion: <name>" line in
        the text is a fallback for models that ignore the tool.
        """
        self._decay.cancel()
        _, emotion = yandex_mod.split_emotion(text)
        if emotion:
            logger.info("[ai] answer emotion (text fallback) -> %s", emotion)
            await self.send_robot_emotion(emotion)
        if pcm:
            await self._play_audio(pcm)
        # Dialogue finished — schedule the emotion decay countdown.
        self._decay.start()

    # ------------------------------------------------------------------
    # Head-touch response (robot -> server -> playback).
    # ------------------------------------------------------------------
    def _load_touch_pcm(self) -> bytes:
        """Loads/caches the touch response WAV as PCM (16k mono)."""
        if self._touch_pcm is None:
            self._touch_pcm = _load_pcm(self._touch_sound_file)
        return self._touch_pcm

    async def _play_touch_sound(self) -> None:
        """Plays the touch response sound on the robot (touch section).

        The sound shares the playback lock with speech, so it never overlaps
        an answer; a missing/broken file is skipped silently.
        """
        pcm = self._load_touch_pcm()
        if not pcm:
            return
        logger.info("PLAY: touch sound %d B", len(pcm))
        await self._play_audio(pcm)

    # ------------------------------------------------------------------
    # Service -> robot actions.
    # ------------------------------------------------------------------
    async def send_robot_move(self, pan, tilte) -> bool:
        return await self.robot.send_movement(pan, tilte)

    async def send_robot_emotion(self, name: str) -> bool:
        return await self.robot.send_emotion(name)

    async def _play_audio(self, pcm: bytes) -> bool:
        """Robot playback serialized by _play_lock (one stream at a time)."""
        async with self._play_lock:
            return await self.send_robot_audio(pcm)

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
            "audio_seg_active": self._audio_seg_active,
            "decay_running": self._decay.running,
            "ai": self.ai.health(),
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
                "touch_events": self.robot.touch_events,
                "last_touch": self.robot.last_touch,
            },
        }