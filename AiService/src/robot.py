"""Robot WebSocket session (/robot): JSON commands and audio playback.

The robot (AiBot firmware) connects as a WebSocket client to
ws://host:port/robot. The robot's microphone is DISABLED — audio comes only
from the camera, the robot only plays it back.

JSON protocol (see protocol.py):
    service -> robot: {"type":"audio","audio":"<b64 pcm>"}  ("" = EOF marker)
                      {"type":"movement","axis":"left","degrees":60}
                      {"type":"emotion","name":"happy"}
    robot -> service: {"type":"hb"} / {"type":"ack","command":"..."}
"""
from __future__ import annotations

import logging
from typing import Optional

from . import protocol as proto
from .websocket import WsSession

logger = logging.getLogger("uvicorn")


class RobotSession(WsSession):
    """WebSocket session of the robot (JSON protocol)."""

    def __init__(self) -> None:
        super().__init__("robot")
        self.move_commands = 0
        self.emotion_commands = 0
        self.playback_bytes = 0
        self.last_ack: Optional[str] = None
        # Head-touch events (robot -> server): press/release/swipe_*.
        self.touch_events = 0
        self.last_touch: Optional[str] = None

    # ------------------------------------------------------------------
    # Actions service -> robot.
    # ------------------------------------------------------------------
    async def send_movement(self, axis: str, degrees: int = 0) -> bool:
        """Movement command {"type":"movement",...}."""
        if axis not in proto.ROBOT_AXES:
            logger.warning("[robot] movement: unknown axis %r", axis)
            return False
        text = proto.robot_movement_message(axis, degrees)
        self.move_commands += 1
        logger.info("[robot] movement %s -> %s", text, self.peer)
        return await self.send_text(text)

    async def send_emotion(self, name: str) -> bool:
        """Emotion command {"type":"emotion","name":...}."""
        if name not in proto.ROBOT_EMOTIONS:
            logger.warning("[robot] emotion: unknown emotion %r", name)
            return False
        text = proto.robot_emotion_message(name)
        self.emotion_commands += 1
        logger.info("[robot] emotion %s -> %s", text, self.peer)
        return await self.send_text(text)

    async def send_audio_frame(self, frame_type: int, codec: int,
                               payload: bytes) -> bool:
        """Sends a binary audio frame [type][codec][payload].

        Binary playback keeps the ESP32 free of base64/JSON decoding — the
        PCM goes straight from the WS frame into the speaker queue (no
        stutter). Empty payload = end-of-playback marker.
        """
        ok = await self.send_bytes(bytes((frame_type, codec)) + payload)
        if ok:
            self.playback_bytes += len(payload)
        return ok

    # ------------------------------------------------------------------
    # Text from the robot (JSON: hb, ack).
    # ------------------------------------------------------------------
    def note_message(self, text: str) -> Optional[str]:
        """Parses and logs a robot text message; returns its type or None."""
        msg = proto.parse_message(text)
        if msg is None:
            logger.info("[robot] non-JSON text: %s", text[:64])
            return None
        mtype = msg.get("type")
        if mtype == proto.MSG_HB:
            logger.info("[robot] HB from %s", self.peer)
        elif mtype == proto.MSG_ACK:
            self.last_ack = str(msg.get("command", ""))
            logger.info("[robot] ack: %s", self.last_ack)
        elif mtype == proto.MSG_TOUCH:
            action = str(msg.get("action", ""))
            self.touch_events += 1
            self.last_touch = action
            logger.info("[robot] touch: %r (total %d)",
                        action, self.touch_events)
        else:
            logger.info("[robot] message: %s", text[:128])
        return mtype