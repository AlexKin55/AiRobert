"""AiService — FastAPI entry point with two WebSocket endpoints.

  /camera (config camera_ws.path) — the camera connects as a WS client and
      sends JSON: audio (base64 PCM), pictures (base64 JPEG), face/emotion
      events; the service may reply mute/unmute/capture;
  /robot  (config robot_ws.path)  — the robot (AiBot firmware) connects as a
      WS client and receives JSON playback chunks, movements and emotions.

All messages are TEXT JSON (protocol.py); binary data is base64-embedded.

Run:
  python3 -m src.server                        # ALWAYS listens on 0.0.0.0
  uvicorn src.server:app --host 0.0.0.0 --port 9002
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, WebSocket

from . import config as app_config
from . import recorder as recorder_mod
from .camera import CameraSession
from .processor import Processor
from .robot import RobotSession
from .state_machine import AiStateMachine

logger = logging.getLogger("uvicorn")

# Debug recorder (incoming/outgoing audio + pictures), see recording section.
_REC_CFG = app_config.CONFIG["recording"]
rec = recorder_mod.Recorder(
    record_dir=os.environ.get("RECORD_DIR", _REC_CFG["record_dir"]),
    sample_rate=app_config.CONFIG["audio"]["sample_rate"],
    save_audio=_REC_CFG.get("save_audio", True),
    save_images=_REC_CFG.get("save_images", True),
    rotate_seconds=_REC_CFG.get("audio_rotate_seconds", 0),
)

# Global sessions and the state machine.
camera = CameraSession()
robot = RobotSession()
_Y_CFG = app_config.CONFIG.get("yandex", {})
ai = Processor(
    stt_enabled=_Y_CFG.get("stt_enabled", True),
    stt_language=_Y_CFG.get("stt_language", "ru-RU"),
)
sm = AiStateMachine(camera, robot, ai, rec=rec)

app = FastAPI(
    title="AiService: camera <-> robot gateway (JSON protocol)",
    version="0.3.0",
)


# ---------------------------------------------------------------------------
# WebSocket: camera (/camera)
# ---------------------------------------------------------------------------
@app.websocket(app_config.CONFIG["camera_ws"]["path"])
async def camera_endpoint(ws: WebSocket):
    """Accepts the camera connection and feeds JSON messages to the SM."""
    await ws.accept()
    await camera.attach(ws)
    await sm.on_camera_connected()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                logger.info("[camera] binary message (%d B) — "
                            "text protocol only, ignored", len(data))
                continue
            text = msg.get("text")
            if text is not None:
                await sm.on_camera_message(text)
    finally:
        await sm.on_camera_disconnected()
        await camera.detach()


# ---------------------------------------------------------------------------
# WebSocket: robot (/robot)
# ---------------------------------------------------------------------------
@app.websocket(app_config.CONFIG["robot_ws"]["path"])
async def robot_endpoint(ws: WebSocket):
    """Accepts the robot connection (JSON protocol)."""
    await ws.accept()
    await robot.attach(ws)
    await sm.on_robot_connected()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                logger.info("[robot] binary message (%d B) — "
                            "text protocol only, ignored", len(data))
                continue
            text = msg.get("text")
            if text is not None:
                await sm.on_robot_message(text)
    finally:
        await sm.on_robot_disconnected()
        await robot.detach()


# ---------------------------------------------------------------------------
# HTTP: diagnostics.
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Service status: state, connection flags and statistics."""
    return sm.health()


def main() -> None:
    """Server entry point: python3 -m src.server.

    Always listens on 0.0.0.0 so the camera and the robot can connect via
    the machine's LAN address. Override: AISERVICE_HOST / AISERVICE_PORT.
    """
    import uvicorn

    host = os.environ.get("AISERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get(
        "AISERVICE_PORT", app_config.CONFIG["server"]["port"]))
    log_cfg = os.environ.get(
        "AISERVICE_LOG_CONFIG",
        str(Path(__file__).resolve().parent.parent / "config" /
            "logging.json"))
    cam_path = app_config.CONFIG["camera_ws"]["path"]
    rob_path = app_config.CONFIG["robot_ws"]["path"]
    logger.info("Server listening on ws://%s:%d%s (camera) and "
                "ws://%s:%d%s (robot)", host, port, cam_path,
                host, port, rob_path)
    uvicorn.run("src.server:app", host=host, port=port,
                log_config=log_cfg)


if __name__ == "__main__":
    main()