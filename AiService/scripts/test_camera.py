#!/usr/bin/env python3
"""Test client for the camera WebSocket (/camera) of the AiService.

Connects to ws://HOST:PORT/camera and sends JSON messages (protocol.py):
  hello, audio (base64 PCM), image (base64 JPEG), face, hb.
Prints service replies (ok/error).

Usage:
  python3 scripts/test_camera.py [--host HOST] [--port PORT] [--image FILE]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src import protocol as proto  # noqa: E402

# Tiny valid JPEG (1x1 pixel) used when --image is not given.
_TINY_JPEG = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

SAMPLE_RATE = 16000


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--image", default="",
                        help="path to the JPEG file to send")
    parser.add_argument("--audio-chunks", type=int, default=3,
                        help="how many audio chunks to send (0.5 s each)")
    args = parser.parse_args()

    import websockets

    url = f"ws://{args.host}:{args.port}/camera"
    print(f"[test_camera] connecting to {url}")

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        # 1. Handshake.
        await ws.send(proto.hello_message("test-camera", SAMPLE_RATE))
        print("[test_camera] hello ->", await asyncio.wait_for(ws.recv(), 5))

        # 2. Audio chunks: 0.5 s of silence each (int16 LE, base64 in JSON).
        chunk = b"\x00\x00" * (SAMPLE_RATE // 2)
        for i in range(args.audio_chunks):
            await ws.send(proto.audio_message(chunk))
            print(f"[test_camera] audio chunk {i + 1}/{args.audio_chunks} "
                  f"({len(chunk)} bytes) ->",
                  await asyncio.wait_for(ws.recv(), 5))
            await asyncio.sleep(0.1)

        # 3. Image (JPEG).
        if args.image:
            with open(args.image, "rb") as f:
                jpeg = f.read()
        else:
            jpeg = base64.b64decode(_TINY_JPEG)
        await ws.send(proto.image_message(jpeg))
        print(f"[test_camera] image ({len(jpeg)} bytes) ->",
              await asyncio.wait_for(ws.recv(), 5))

        # 4. Face event.
        await ws.send(proto.face_message("user-alex", 0.97))
        print("[test_camera] face ->", await asyncio.wait_for(ws.recv(), 5))

        # 5. Heartbeat.
        await ws.send(proto.hb_message())
        print("[test_camera] hb ->", await asyncio.wait_for(ws.recv(), 5))

    print("[test_camera] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))