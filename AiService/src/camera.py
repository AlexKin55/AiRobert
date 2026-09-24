"""Camera WebSocket session (/camera): JSON messages from the camera.

The camera connects as a WebSocket client to ws://host:port/camera and sends
TEXT JSON messages (see protocol.py):
    hello, audio (base64 PCM), image (base64 JPEG), face, emotion, hb

The session parses/decode messages, keeps statistics and returns a list of
media events for the state machine:
    [("audio", pcm_bytes)], [("image", jpeg_bytes)],
    [("face", face_id, confidence)], [("emotion", name)]

The service may reply with mute/unmute/capture (send_mute/send_unmute/
send_capture).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import config as app_config
from . import protocol as proto
from .websocket import WsSession

logger = logging.getLogger("uvicorn")

_AUDIO_CFG = app_config.CONFIG["audio"]
_IMAGE_CFG = app_config.CONFIG["image"]

# Media event tags returned by on_message().
EV_AUDIO = "audio"
EV_IMAGE = "image"
EV_FACE = "face"
EV_EMOTION = "emotion"

DEFAULT_FORMAT: Dict[str, Any] = {
    "audio": {
        "rate": _AUDIO_CFG["sample_rate"],
        "channels": _AUDIO_CFG["channels"],
        "bits": _AUDIO_CFG["bits_per_sample"],
    },
    "image": {
        "codec": "jpeg",
        "width": 0,
        "height": 0,
    },
}


class CameraSession(WsSession):
    """WebSocket session of the camera (JSON protocol)."""

    def __init__(self) -> None:
        super().__init__("camera")
        self.format: Dict[str, Any] = dict(DEFAULT_FORMAT)
        self.device = ""
        self.last_face: Optional[Dict[str, Any]] = None
        self.last_emotion: str = ""
        self.frames_total = 0
        self.audio_frames = 0
        self.image_frames = 0
        self.audio_bytes = 0
        self.face_events = 0
        self.emotion_events = 0

    # ------------------------------------------------------------------
    # Incoming JSON message -> media events.
    # ------------------------------------------------------------------
    async def on_message(self, text: str) -> Optional[List[Tuple[str, Any]]]:
        """Parses a camera JSON message into media events.

        Returns None on malformed input (the caller sends "error"); an empty
        list for pure-control messages (hello/hb/unknown).
        """
        msg = proto.parse_message(text)
        if msg is None:
            return None
        mtype = msg.get("type")
        if mtype == proto.MSG_HELLO:
            self._on_hello(msg)
            return []
        if mtype == proto.MSG_HB:
            logger.info("[camera] HB from %s", self.peer)
            return []
        if mtype == proto.MSG_AUDIO:
            try:
                pcm = proto.dec_b64(str(msg.get("audio", "")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[camera] audio: invalid base64: %s", exc)
                return None
            self.note_audio_frame(pcm)
            return [(EV_AUDIO, pcm)]
        if mtype == proto.MSG_IMAGE:
            try:
                jpeg = proto.dec_b64(str(msg.get("image", "")))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[camera] image: invalid base64: %s", exc)
                return None
            self.note_image_frame(jpeg)
            return [(EV_IMAGE, jpeg)]
        if mtype == proto.MSG_FACE:
            face_id = str(msg.get("face_id", ""))
            confidence = msg.get("confidence")
            self.last_face = {"id": face_id, "confidence": confidence}
            self.face_events += 1
            logger.info("[camera] face: id=%r confidence=%s",
                        face_id, confidence)
            return [(EV_FACE, face_id, confidence)]
        if mtype == proto.MSG_EMOTION:
            self.last_emotion = str(msg.get("emotion", ""))
            self.emotion_events += 1
            logger.info("[camera] face emotion: %r", self.last_emotion)
            return [(EV_EMOTION, self.last_emotion)]
        logger.info("[camera] unknown message type: %s", mtype)
        return []

    def _on_hello(self, msg: Dict[str, Any]) -> None:
        self.device = str(msg.get("device", "camera"))
        fmt = msg.get("format")
        if isinstance(fmt, dict):
            if isinstance(fmt.get("audio"), dict):
                self.format["audio"] = {**DEFAULT_FORMAT["audio"], **fmt["audio"]}
            if isinstance(fmt.get("image"), dict):
                self.format["image"] = {**DEFAULT_FORMAT["image"], **fmt["image"]}
        logger.info("[camera] hello from %r, format: %s", self.device, self.format)

    # ------------------------------------------------------------------
    # Commands service -> camera.
    # ------------------------------------------------------------------
    async def send_mute(self) -> bool:
        """Asks the camera to turn its microphone off (robot is playing)."""
        ok = await self.send_text(proto.mute_message())
        logger.info("[camera] mute -> %s", "ok" if ok else "no connection")
        return ok

    async def send_unmute(self) -> bool:
        """Asks the camera to turn its microphone back on."""
        ok = await self.send_text(proto.unmute_message())
        logger.info("[camera] unmute -> %s", "ok" if ok else "no connection")
        return ok

    async def send_capture(self) -> bool:
        """Asks the camera to take a picture and send it as an image message."""
        ok = await self.send_text(proto.capture_message())
        logger.info("[camera] capture -> %s", "ok" if ok else "no connection")
        return ok

    # ------------------------------------------------------------------
    # Statistics.
    # ------------------------------------------------------------------
    def note_audio_frame(self, payload: bytes) -> None:
        self.frames_total += 1
        self.audio_frames += 1
        self.audio_bytes += len(payload)
        rate = int(self.format["audio"].get("rate", 16000))
        logger.info("[camera] audio chunk #%d received: %d bytes "
                    "(total %d B = %.2f s)",
                    self.audio_frames, len(payload), self.audio_bytes,
                    self.audio_bytes / (2 * rate))

    def note_image_frame(self, payload: bytes) -> None:
        self.frames_total += 1
        self.image_frames += 1