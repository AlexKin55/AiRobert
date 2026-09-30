"""Base WebSocket session wrapper (common send helpers).

Subclass: CameraSession (/camera). The robot is now controlled by the CAMERA
over a local WS (see M5StackUnitV2-M12/src/robot_server.py) — AiService may
run in the cloud and has only one WebSocket endpoint.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("uvicorn")


class WsSession:
    """Wrapper over an active WebSocket connection with a remote peer."""

    def __init__(self, name: str = "ws") -> None:
        self.name = name
        self.ws: Optional[WebSocket] = None
        self.peer = ""

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def attach(self, ws: WebSocket) -> None:
        self.ws = ws
        self.peer = ws.client.host if ws.client else "?"

    async def detach(self) -> None:
        self.ws = None
        self.peer = ""

    async def send_text(self, text: str) -> bool:
        """Sends a text message to the peer. False if there is no connection."""
        if not self.connected:
            return False
        try:
            await self.ws.send_text(text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] failed to send %r: %s",
                           self.name, text, exc)
            return False

    async def send_bytes(self, data: bytes) -> bool:
        """Sends a binary message to the peer. False if there is no connection."""
        if not self.connected:
            return False
        try:
            await self.ws.send_bytes(data)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] failed to send %d bytes: %s",
                           self.name, len(data), exc)
            return False

    def log_connect(self) -> None:
        logger.info("[%s] connected: %s", self.name, self.peer)

    def log_disconnect(self) -> None:
        logger.info("[%s] disconnected", self.name)