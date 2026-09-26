"""AI processing via the Yandex Realtime API: audio question -> audio answer.

Single voice channel instead of the STT -> GPT -> TTS chain:

- ``start()`` opens one persistent ``RealtimeDialog`` session (the server
  keeps the dialog context in it);
- ``process_audio()`` sends every camera PCM chunk into that session
  (input_audio_buffer.append); the server-side VAD detects the end of the
  utterance and the Speech Realtime model recognizes, answers and synthesizes
  the speech in one pass;
- the finished answer (PCM) and its text are delivered via callbacks:
    on_answer(pcm, text)     — playback to the robot (+ Emotion command);
    on_user_text(text)       — recognized question transcript (logs/files);
    on_speech_started()      — the user started speaking (decay postponed);
- ``ask_audio()`` / ``say_emotion()`` run one-shot TEXT -> audio requests in
  their own short Realtime sessions (post-dialogue emotion-decay phrases).

Debug saving happens here (recorder): the question transcript goes next to
the _in.wav file, the answer PCM as _tts.wav, and the prompt exchange (system
prompt + question + answer) as _prompt.txt.

Pictures/faces/emotions from the camera are still stubs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as app_config

logger = logging.getLogger("uvicorn")


class Processor:
    """AI pipeline: Yandex Realtime audio question -> audio answer."""

    def __init__(self, rec: Optional[Any] = None,
                 on_answer: Optional[Callable[[bytes, str], Any]] = None,
                 on_user_text: Optional[Callable[[str], Any]] = None,
                 on_speech_started: Optional[Callable[[], Any]] = None,
                 on_emotion: Optional[Callable[[str], Any]] = None,
                 on_weather: Optional[Callable[[str], Any]] = None) -> None:
        from . import yandex as yandex_mod
        # Called when a full model answer is ready: (pcm, answer_text).
        self.on_answer = on_answer
        # Called with the recognized question transcript.
        self.on_user_text = on_user_text
        # Called when the user starts speaking (server VAD).
        self.on_speech_started = on_speech_started
        # Called when the model calls the emotion(name) function — the robot
        # emotion command (the model does NOT speak it).
        self.on_emotion = on_emotion
        # Called when the model calls the weather(city) function — returns a
        # short weather summary string that the model voices to the user.
        self.on_weather = on_weather
        self.rec = rec  # optional Recorder for debug saving
        y = app_config.CONFIG.get("yandex", {})
        self.enabled = bool(y.get("enabled", True)) and \
            yandex_mod.credentials_ok()
        self.model = str(y.get("realtime_model", "speech-realtime-260528"))
        self.voice = str(y.get("voice", "alena"))
        self.role = str(y.get("role", ""))
        self.input_rate = int(y.get("realtime_input_rate", 16000))
        self.output_rate = int(y.get("realtime_output_rate", 16000))
        self.language = str(y.get("realtime_language", "ru-RU"))
        self.timeout_s = float(y.get("realtime_timeout_s", 90.0))
        # The last recognized question transcript (paired with the next
        # answer in the prompt log).
        self.last_user_text = ""
        self._rt: Optional[Any] = None
        # Statistics.
        self.answers = 0            # full audio answers generated
        self.user_utterances = 0    # recognized question transcripts
        self.audio_answers = 0      # non-empty answer PCM segments
        self.decay_phrases = 0      # emotion-decay phrases synthesized
        if not self.enabled:
            logger.warning("[ai] Realtime disabled: set YANDEX_API_KEY and "
                           "YANDEX_FOLDER_ID (env or config/settings.json)")

    # ------------------------------------------------------------------
    # Persistent Realtime session lifecycle.
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._rt is not None and self._rt.connected

    async def start(self) -> None:
        """Opens the persistent Realtime dialog session (once)."""
        if not self.enabled or self._rt is not None:
            return
        from . import callbacks as cb
        from . import yandex as yandex_mod
        try:
            rt = yandex_mod.RealtimeDialog(
                model=self.model,
                voice=self.voice,
                role=self.role,
                instructions=yandex_mod.default_system_prompt(),
                input_rate=self.input_rate,
                output_rate=self.output_rate,
                language=self.language,
                tools=list(cb.TOOLS),
                on_answer=self._on_rt_answer,
                on_user_text=self._on_rt_user_text,
                on_speech_started=self._on_rt_speech_started,
                on_function_call=self._on_rt_function_call,
                on_error=self._on_rt_error,
            )
            await rt.start()
            self._rt = rt
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] Realtime session failed to start: %s", exc)
            self._rt = None

    async def stop(self) -> None:
        """Closes the Realtime session (server shutdown / reconnect)."""
        if self._rt is not None:
            try:
                await self._rt.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] Realtime close error: %s", exc)
            self._rt = None

    async def ensure_running(self) -> None:
        """Reconnects the Realtime session if it went down (watchdog)."""
        if self.enabled and not self.connected:
            logger.info("[ai] Realtime session down — reconnecting")
            await self.stop()
            await self.start()

    # ------------------------------------------------------------------
    # Camera audio -> Realtime (audio question).
    # ------------------------------------------------------------------
    async def process_audio(self, pcm: bytes) -> None:
        """Camera PCM chunk: goes straight into the Realtime session."""
        if self._rt is not None:
            try:
                await self._rt.feed(pcm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] Realtime feed error: %s", exc)

    # ------------------------------------------------------------------
    # Realtime callbacks (called from the Realtime reader task).
    # ------------------------------------------------------------------
    def _on_rt_user_text(self, transcript: str) -> None:
        """Question transcript: save next to the _in.wav and log."""
        self.user_utterances += 1
        self.last_user_text = transcript
        if self.rec is not None:
            try:
                self.rec.save_transcript(transcript)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] transcript save error: %s", exc)
        if self.on_user_text is not None:
            try:
                result = self.on_user_text(transcript)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(self._run_coro(result))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] on_user_text callback error: %s", exc)

    def _on_rt_speech_started(self) -> None:
        """The user started speaking — notify the caller (decay postponed)."""
        if self.on_speech_started is not None:
            try:
                result = self.on_speech_started()
                if asyncio.iscoroutine(result):
                    asyncio.create_task(self._run_coro(result))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] on_speech_started callback error: %s",
                               exc)

    async def _on_rt_function_call(self, name: str,
                                   args: dict) -> Optional[str]:
        """Model tool call — executed on the server via callbacks.dispatch().

        The tool schemas (TOOLS) and their handlers live in callbacks.py: the
        model CALLS ``emotion(name)`` instead of speaking it (the answer audio
        never contains the command), and ``weather(city)`` returns a summary
        which the model voices. Returns the result for the model or None.
        """
        from . import callbacks as cb
        return await cb.dispatch(name, args, cb.CallbackContext(
            send_emotion=self.on_emotion,
            get_weather=self.on_weather))

    def _on_rt_answer(self, pcm: bytes, text: str) -> None:
        """Full model answer: save debug files and notify the caller."""
        self.answers += 1
        if pcm:
            self.audio_answers += 1
        if self.rec is not None:
            try:
                if pcm:
                    self.rec.save_tts_audio(pcm)
                from . import yandex as yandex_mod
                self.rec.save_prompt_log(
                    yandex_mod.default_system_prompt(),
                    self.last_user_text or "(no transcript)", text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] answer save error: %s", exc)
        if self.on_answer is not None:
            try:
                result = self.on_answer(pcm, text)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(self._run_coro(result))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] on_answer callback error: %s", exc)

    def _on_rt_error(self, exc: Exception) -> None:
        logger.error("[ai] Realtime error: %s", exc)

    async def _run_coro(self, coro: Any) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] async callback error: %s", exc)

    # ------------------------------------------------------------------
    # One-shot text -> audio requests (emotion-decay phrases).
    # ------------------------------------------------------------------
    async def ask_audio(self, text: str,
                        instructions: str = "") -> Optional[bytes]:
        """One-shot: TEXT -> synthesized answer PCM (own Realtime session).

        Used for emotion-decay phrases: the model both generates the phrase
        and speaks it in one call. Returns PCM (16k mono) or None on error /
        empty input / empty answer.
        """
        if not self.enabled or not text or not text.strip():
            return None
        from . import yandex as yandex_mod
        try:
            pcm = await yandex_mod.ask_audio(
                text,
                instructions=instructions or yandex_mod.default_system_prompt(),
                model=self.model,
                voice=self.voice,
                role=self.role,
                input_rate=self.input_rate,
                output_rate=self.output_rate,
                timeout_s=self.timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] ask_audio error: %s", exc)
            return None
        if not pcm:
            return None
        self.decay_phrases += 1
        self.audio_answers += 1
        if self.rec is not None:
            try:
                self.rec.save_tts_audio(pcm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] decay audio save error: %s", exc)
        return pcm

    async def say_emotion(self, emotion: str) -> Optional[bytes]:
        """Emotion-decay phrase: TEXT -> audio for the post-dialogue decay.

        Uses the yandex.emotion_decay_prompt system prompt (from settings) so
        the robot says a short phrase matching the decay emotion. Returns the
        synthesized PCM (or None on error/empty).
        """
        if not self.enabled or not emotion:
            return None
        from . import yandex as yandex_mod
        prompt = yandex_mod.default_emotion_decay_prompt()
        pcm = await self.ask_audio(emotion, instructions=prompt)
        if pcm and self.rec is not None:
            try:
                self.rec.save_prompt_log(
                    prompt, f"[decay:{emotion}]", "(audio)")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] decay prompt save error: %s", exc)
        return pcm

    # ------------------------------------------------------------------
    # /health statistics.
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return {
            "realtime_enabled": self.enabled,
            "realtime_connected": self.connected,
            "answers": self.answers,
            "user_utterances": self.user_utterances,
            "audio_answers": self.audio_answers,
            "decay_phrases": self.decay_phrases,
        }

    # ------------------------------------------------------------------
    # Picture pipeline (discrete frames, no video stream).
    # ------------------------------------------------------------------
    async def detect_objects(self, jpeg: bytes) -> List[Dict[str, Any]]:
        """Object recognition on a JPEG picture. Stub: empty detections."""
        logger.info("[ai] detect_objects: JPEG %d bytes (stub — 0 objects)",
                    len(jpeg))
        return []

    async def on_face(self, face_id: str,
                      confidence: Any = None) -> None:
        """Recognized-face event from the camera. Stub: log only."""
        logger.info("[ai] face: id=%r confidence=%s (stub)",
                    face_id, confidence)

    async def on_camera_emotion(self, emotion: str) -> None:
        """Face emotion recognized by the camera. Stub: log only."""
        logger.info("[ai] face emotion: %r (stub)", emotion)

    async def on_robot_text(self, text: str) -> None:
        """Text from the robot. Stub: log only."""
        logger.info("[ai] robot text: %r (stub)", text)