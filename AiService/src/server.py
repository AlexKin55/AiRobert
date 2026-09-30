"""AiService — FastAPI entry point with ONE WebSocket endpoint.

  /camera (config camera_ws.path) — the camera connects as a WS client and
      sends JSON: audio (base64 PCM), pictures (base64 JPEG), face/emotion
      events, robot-touch relay, decay phrase requests; the service replies
      mute/unmute/capture, emotion commands for the robot and FULL audio
      answers ({"type":"play","audio":"<b64>"}).

The ROBOT is controlled by the CAMERA over a local WS
(ws://<camera-ip>:8765/robot) — AiService may run in the cloud and never
talks to the robot directly (see M5StackUnitV2-M12/src/robot_server.py).

All messages are TEXT JSON (protocol.py); binary data is base64-embedded.

Run:
  python3 -m src.server                        # ALWAYS listens on 0.0.0.0
  uvicorn src.server:app --host 0.0.0.0 --port 9002
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket

from . import config as app_config
from . import recorder as recorder_mod
from .camera import CameraSession
from .processor import Processor
from .state_machine import AiStateMachine

logger = logging.getLogger("uvicorn")

# Debug recorder (incoming/outgoing audio + pictures), see recording section.
_REC_CFG = app_config.CONFIG["recording"]
rec = recorder_mod.Recorder(
    record_dir=os.environ.get("RECORD_DIR", _REC_CFG["record_dir"]),
    sample_rate=app_config.CONFIG["audio"]["sample_rate"],
    save_audio=_REC_CFG.get("save_audio", True),
    save_images=_REC_CFG.get("save_images", True),
    save_tts_audio=_REC_CFG.get("save_tts_audio", True),
    save_prompts=_REC_CFG.get("save_prompts", True),
    rotate_seconds=_REC_CFG.get("audio_rotate_seconds", 0),
)

# Global session and the state machine (the robot is controlled via camera).
camera = CameraSession()
ai = Processor(rec=rec)
sm = AiStateMachine(camera, ai, rec=rec)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup/shutdown: opens the persistent Yandex Realtime session (audio
    question -> audio answer) and runs the background watchdog (silent-camera
    diagnostics + Realtime-session recovery) while the server is up."""
    await ai.start()
    task = asyncio.create_task(sm.run_watchdog())
    logger.info("[ai] watchdog started")
    try:
        yield
    finally:
        task.cancel()
        await ai.stop()
        logger.info("[ai] watchdog stopped")


app = FastAPI(
    title="AiService: camera <-> robot gateway (JSON protocol, Realtime)",
    version="0.4.0",
    lifespan=lifespan,
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
# HTTP: diagnostics.
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Service status: state, connection flags and statistics."""
    return sm.health()


def main() -> None:
    """Server entry point: python3 -m src.server.

    Always listens on 0.0.0.0 so the camera can connect via the machine's
    LAN address. Override: AISERVICE_HOST / AISERVICE_PORT.
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
    logger.info("Server listening on ws://%s:%d%s (camera)",
                host, port, cam_path)
    uvicorn.run("src.server:app", host=host, port=port,
                log_config=log_cfg)


if __name__ == "__main__":
    main()