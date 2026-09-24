"""AI processing: streaming Yandex STT for camera audio segments.

Camera audio is recognized streamingly (SpeechKit STT v3, StreamingRecognizer):
- ``begin_segment()`` — start of an utterance (first chunk after a pause): a
  gRPC recognition stream is opened;
- ``process_audio()`` — every PCM chunk goes to the stream immediately;
- ``end_segment()`` — finalization (pause > AUDIO_SEGMENT_GAP or camera
  disconnect): the stream is closed, the utterance text is taken and logged.

Pictures/faces/emotions are still stubs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("uvicorn")


class Processor:
    """AI pipeline: streaming STT + stubs for pictures/faces."""

    def __init__(self, stt_enabled: bool = True,
                 stt_language: str = "ru-RU") -> None:
        from . import yandex as yandex_mod
        self.stt_language = stt_language
        self.stt_enabled = bool(stt_enabled) and yandex_mod.credentials_ok()
        self._stt: Optional[Any] = None
        self._seg_started_at = 0.0
        self.segments = 0       # finished speech segments
        self.recognized = 0     # of those with recognized text
        if not self.stt_enabled:
            logger.warning("[ai] STT disabled: set YANDEX_API_KEY and "
                           "YANDEX_FOLDER_ID (env or config/settings.json)")

    # ------------------------------------------------------------------
    # Streaming STT (audio segments from the camera).
    # ------------------------------------------------------------------
    async def begin_segment(self) -> None:
        """Utterance start: opens a Yandex STT recognition stream."""
        if not self.stt_enabled or self._stt is not None:
            return
        from . import yandex as yandex_mod
        try:
            stt = yandex_mod.StreamingRecognizer(
                language_code=self.stt_language)
            stt.start()
            self._stt = stt
            self._seg_started_at = time.monotonic()
            logger.info("[ai] STT: segment %d started (stream open)",
                        self.segments + 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] STT stream failed to start: %s", exc)
            self._stt = None

    async def process_audio(self, pcm: bytes) -> None:
        """Camera PCM chunk: goes straight into the open recognition stream."""
        if self._stt is not None:
            try:
                self._stt.feed(pcm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ai] STT feed error: %s", exc)

    async def end_segment(self) -> str:
        """Utterance finalization: closes the stream and returns the text.

        Does not block the asyncio loop: finish() runs in a separate thread.
        Returns "" if there was no stream or no text was recognized.
        """
        stt = self._stt
        self._stt = None
        if stt is None:
            return ""
        self.segments += 1
        seconds = time.monotonic() - self._seg_started_at
        try:
            text = await asyncio.to_thread(stt.finish)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ai] STT finish error: %s", exc)
            return ""
        if text:
            self.recognized += 1
            logger.info("[ai] STT segment %d (%.1f s): %r",
                        self.segments, seconds, text)
        else:
            logger.info("[ai] STT segment %d (%.1f s): empty text",
                        self.segments, seconds)
        return text or ""

    def health(self) -> Dict[str, Any]:
        """STT statistics for /health."""
        return {
            "stt_enabled": self.stt_enabled,
            "stt_segments": self.segments,
            "stt_recognized": self.recognized,
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