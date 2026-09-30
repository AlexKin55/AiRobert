"""Camera WebSocket session (/camera): JSON messages from the camera.

The camera connects as a WebSocket client to ws://host:port/camera and sends
TEXT JSON messages (see protocol.py):
    hello, audio (base64 PCM), image (base64 JPEG), face, emotion,
    touch (robot touch relay), decay (decay phrase request), hb

The session parses/decodes messages, keeps statistics and returns a list of
media events for the state machine:
    [("audio", pcm_bytes)], [("image", jpeg_bytes)],
    [("face", face_id, confidence, pan, tilt, visible)], [("emotion", name)],
    [("touch", action)], [("decay", emotion)]

The service may reply with mute/unmute/capture (send_mute/send_unmute/
send_capture), emotion-for-the-robot (send_emotion) and full audio answers
(send_play) which the camera plays on the robot locally.
"""
from __future__ import annotations

import logging
import time
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
EV_TOUCH = "touch"
EV_DECAY = "decay"

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
        # Whether the robot is connected to the CAMERA's local WS server
        # (reported by the camera in every hb; the cloud controls the robot
        # only through the camera).
        self.robot_connected = False
        # Monotonic time of the last message received from the camera
        # (any type: audio/HB/image); the watchdog detects a hung camera.
        self.last_activity = 0.0
        self.frames_total = 0
        self.audio_frames = 0
        self.image_frames = 0
        self.audio_bytes = 0
        self.face_events = 0
        self.emotion_events = 0
        self.touch_events = 0     # robot touches relayed via the camera
        self.last_touch = ""
        self.decay_requests = 0   # decay phrase requests from the camera
        # Playback sent to the camera (which relays it to the robot):
        self.playback_bytes = 0
        self.playback_messages = 0
        self.emotion_commands = 0

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
        self.last_activity = time.monotonic()
        mtype = msg.get("type")
        if mtype == proto.MSG_HELLO:
            self._on_hello(msg)
            return []
        if mtype == proto.MSG_HB:
            # The hb carries the robot status (connected to the camera's
            # local WS) — used for /health and decay scheduling.
            self.robot_connected = bool(msg.get("robot", self.robot_connected))
            logger.info("[camera] HB from %s (robot=%s)", self.peer,
                        self.robot_connected)
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
            visible = bool(msg.get("visible", False))
            pan_angle = msg.get("pan_angle")
            tilt_angle = msg.get("tilt_angle")
            confidence = msg.get("confidence")

            self.last_face = {"id": face_id, "visible": visible, "pan_angle": pan_angle,
                "tilt_angle": tilt_angle, "confidence": confidence}

            self.face_events += 1

            logger.info("[camera] face: id=%r visible=%s pan=%s tilt=%s confidence=%s",
                face_id, visible, pan_angle, tilt_angle, confidence,)

            return [(EV_FACE, face_id, confidence, pan_angle, tilt_angle, visible,)]
        if mtype == proto.MSG_EMOTION:
            self.last_emotion = str(msg.get("emotion", ""))
            self.emotion_events += 1
            logger.info("[camera] face emotion: %r", self.last_emotion)
            return [(EV_EMOTION, self.last_emotion)]
        if mtype == proto.MSG_TOUCH:
            # Robot touch relayed via the camera (the camera reacts locally;
            # this event is for statistics/logs only).
            action = str(msg.get("action", ""))
            self.touch_events += 1
            self.last_touch = action
            logger.info("[camera] robot touch: %r (total %d)", action,
                        self.touch_events)
            return [(EV_TOUCH, action)]
        if mtype == proto.MSG_DECAY:
            # The camera asks to synthesize a decay phrase.
            emotion = str(msg.get("emotion", ""))
            self.decay_requests += 1
            logger.info("[camera] decay phrase request: %r", emotion)
            return [(EV_DECAY, emotion)]
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
    async def send_play(self, pcm: bytes, decay: str = "") -> bool:
        """Sends a FULL audio answer to the camera: {"type":"play",...}.

        The camera splits the PCM into chunks and plays it on the robot with
        real-time pacing (the cloud sends one message per answer, no chunk
        relay). ``decay`` marks a decay phrase for the emotion-decay scheduler.
        """
        ok = await self.send_text(proto.play_message(pcm, decay=decay))
        if ok:
            self.playback_messages += 1
            self.playback_bytes += len(pcm)
        logger.info("[camera] play -> %s (%d B%s)", "ok" if ok else "no",
                    len(pcm), ", decay=%s" % decay if decay else "")
        return ok

    async def send_emotion(self, name: str) -> bool:
        """Emotion command for the robot via the camera
        ({"type":"emotion","name":...} — the camera relays it locally)."""
        ok = await self.send_text(proto.robot_emotion_message(name))
        if ok:
            self.emotion_commands += 1
        logger.info("[camera] emotion %r -> %s", name,
                    "ok" if ok else "no connection")
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