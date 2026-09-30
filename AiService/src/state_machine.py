"""AiService state machine: ONE WebSocket (/camera) + local robot control.

The camera sends audio (b64 PCM), pictures, face events, emotions, robot
touch relay and decay phrase requests; it receives emotion commands for the
robot and FULL audio answers ({"type":"play","audio":"<b64>"} — the camera
splits them into chunks itself). The robot is controlled by the CAMERA over a
local WS; the cloud only processes audio (Yandex Realtime, audio in -> audio
out) and synthesizes decay phrases on request.
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
from .camera import (CameraSession, EV_AUDIO, EV_IMAGE, EV_FACE, EV_EMOTION,
                     EV_TOUCH, EV_DECAY)
from .processor import Processor

logger = logging.getLogger("uvicorn")

# After an audio pause of this length _in.wav is closed (finalized). The
# camera with VAD sends silence up to silence_seconds (2 s) and stops — the
# 3 s gap guarantees the file closes at the end of a speech segment.
AUDIO_SEGMENT_GAP = 3.0

# Watchdog: after this silence (no audio and no HB at all) from the camera a
# warning is logged — the camera may have died/disconnected while the WS still
# looks alive to the peer.
CAMERA_SILENCE_WARN_S = 30.0


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    STREAMING = "streaming"


class AiStateMachine:
    def __init__(self, camera: CameraSession, processor: Processor,
                 rec: Optional[recorder_mod.Recorder] = None) -> None:
        self.camera = camera
        self.ai = processor
        self.rec = rec
        self.state = State.DISCONNECTED
        # Realtime answer callback (pcm, text) — the voice of the main dialog.
        self.ai.on_answer = self._on_realtime_answer
        # Question transcript callback (logging).
        self.ai.on_user_text = self._on_user_text
        # Speech-start callback: log only — the decay timers live on the
        # camera, which restarts them when its VAD detects user speech.
        self.ai.on_speech_started = self._on_speech_started
        # Function-call emotion callback: the model CALLS emotion(name)
        # instead of speaking it (see callbacks.emotion_tool / Realtime tools).
        self.ai.on_emotion = self._on_realtime_emotion
        # Function-call weather callback: returns a summary the model voices.
        self.ai.on_weather = self._on_realtime_weather
        # True while the camera streams audio chunks (no segment markers in
        # the JSON protocol — the stream is continuous).
        self._streaming = False
        # Monotonic time of the last camera audio chunk (segment closing).
        self._last_audio = 0.0
        # True while an _in.wav recording segment is active (closed after a
        # pause longer than AUDIO_SEGMENT_GAP).
        self._audio_seg_active = False

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
        self._update_state()

    async def on_camera_disconnected(self) -> None:
        logger.info("Camera disconnected")
        self._streaming = False
        self._audio_seg_active = False
        if self.rec is not None:
            self.rec.close_audio()
        self._update_state()

    def _update_state(self) -> None:
        if not self.camera.connected:
            self.state = State.DISCONNECTED
        elif self._streaming:
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
                else:
                    logger.warning("[camera] not connected")
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
                # (The camera mutes its mic locally while the robot plays —
                # the echo-loop guard lives on the camera now.)
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
                # The CAMERA tracks the face and steers the robot locally —
                # the cloud only logs the event (stub for future AI use).
                await self.ai.on_face(event[1], event[2], event[3], event[4])
            elif kind == EV_EMOTION:
                await self.ai.on_camera_emotion(event[1])
            elif kind == EV_TOUCH:
                # The camera reacts to the touch locally (happy emotion +
                # sound); here the event is only logged for statistics.
                logger.info("[camera] robot touch (relayed): %r", event[1])
            elif kind == EV_DECAY:
                # Decay phrase request from the camera's decay scheduler:
                # synthesize the phrase and send it back as a play message
                # marked with the stage emotion (stale answers are dropped).
                await self._on_camera_decay_request(str(event[1]))
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
    # Decay phrase requests from the camera's decay scheduler.
    # ------------------------------------------------------------------
    async def _on_camera_decay_request(self, emotion: str) -> None:
        """Synthesizes a decay phrase for the camera's decay scheduler.

        The camera runs the decay TIMERS and already switched the robot
        emotion locally; this endpoint only provides the audio. On any
        error/empty answer the phrase is skipped — the camera continues
        without it (the emotion stays switched).
        """
        if emotion not in proto.ROBOT_EMOTIONS:
            logger.warning("[ai] decay request: unknown emotion %r", emotion)
            return
        logger.info("[ai] decay phrase request: %s", emotion)
        try:
            pcm = await self.ai.say_emotion(emotion)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] decay phrase error: %s", exc)
            return
        if not pcm:
            logger.info("[ai] decay phrase empty — skipped")
            return
        ok = await self.camera.send_play(pcm, decay=emotion)
        logger.info("[ai] decay phrase %s sent (%d B -> %s)", emotion,
                    len(pcm), "ok" if ok else "NO CONNECTION")

    # ------------------------------------------------------------------
    # Realtime answer pipeline (audio question -> audio answer).
    # ------------------------------------------------------------------
    def _on_speech_started(self) -> None:
        """The user started speaking — log only.

        The emotion-decay timers live on the camera; the camera restarts
        them itself when its VAD detects user speech.
        """
        logger.info("[ai] speech started (camera restarts its decay timers)")

    async def _on_user_text(self, transcript: str) -> None:
        """Question transcript from the Realtime session — log only (the
        debug files are saved by the Processor)."""
        logger.info("[ai] user said: %r", transcript)

    async def _on_realtime_emotion(self, name: str) -> None:
        """Emotion from the model's function call (tools / function calling).

        The model calls ``emotion(name)`` instead of speaking the command, so
        the answer audio never contains "Emotion: ...". The emotion goes to
        the robot VIA THE CAMERA ({"type":"emotion","name":...}), which
        relays it over the local WS.
        """
        if name not in proto.ROBOT_EMOTIONS:
            logger.warning("[ai] emotion function call: unknown %r", name)
            return
        logger.info("[ai] answer emotion (function call) -> %s", name)
        await self.camera.send_emotion(name)

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

        The primary emotion path is the function call
        (``_on_realtime_emotion``); the trailing "Emotion: <name>" line in
        the text is a fallback for models that ignore the tool. The playback
        is sent to the CAMERA as ONE play message — the camera splits the PCM
        into chunks and plays it on the robot with real-time pacing.
        """
        _, emotion = yandex_mod.split_emotion(text)
        if emotion:
            logger.info("[ai] answer emotion (text fallback) -> %s", emotion)
            await self.camera.send_emotion(emotion)
        if pcm:
            await self.send_playback(pcm)

    # ------------------------------------------------------------------
    # Service -> camera actions (the camera controls the robot locally).
    # ------------------------------------------------------------------
    async def send_playback(self, pcm: bytes, decay: str = "") -> bool:
        """Sends a full audio answer to the camera (relayed to the robot).

        The camera splits the PCM into chunks and plays them with real-time
        pacing; ``decay`` marks a decay phrase for the camera's scheduler.
        Returns False immediately when the camera is offline or pcm is
        empty — the server never waits for a missing peer (no crash/hang).
        """
        if not self.camera.connected:
            logger.info("PLAY: camera offline, skipping %d B", len(pcm))
            return False
        if not pcm:
            return False
        ok = await self.camera.send_play(pcm, decay=decay)
        logger.info("PLAY: %d B to camera (%s)%s", len(pcm),
                    "ok" if ok else "NO CONNECTION",
                    ", decay=%s" % decay if decay else "")
        return ok

    # ------------------------------------------------------------------
    # /health summary.
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "state": self.state.value,
            "camera_connected": self.camera.connected,
            "robot_connected": self.camera.robot_connected,
            "camera_activity_age_s": round(
                now - self.camera.last_activity, 1)
            if self.camera.last_activity else None,
            "last_audio_age_s": round(now - self._last_audio, 1)
            if self._last_audio else None,
            "audio_seg_active": self._audio_seg_active,
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
                # The robot is controlled by the CAMERA; these stats are
                # relayed by the camera through the /camera channel.
                "connected": self.camera.robot_connected,
                "playback_messages": self.camera.playback_messages,
                "playback_bytes": self.camera.playback_bytes,
                "emotion_commands": self.camera.emotion_commands,
                "touch_events": self.camera.touch_events,
                "last_touch": self.camera.last_touch,
                "decay_requests": self.camera.decay_requests,
            },
        }