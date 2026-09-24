"""AiService JSON protocol (camera <-> service, service <-> robot).

All messages are TEXT JSON. Binary data (PCM audio, JPEG pictures) is
embedded as base64 strings in dedicated fields.

EVERY message carries a "timestamp" field: Unix time in MICROSECONDS
(int(time.time() * 1_000_000)). It is added automatically by make_message()
on the service side; clients must add it to their outgoing messages too.

Camera -> service (/camera):
    {"type":"hello","device":"unitv2","timestamp":1727000000000000,
     "format":{"audio":{"rate":16000,"channels":1,"bits":16},
               "image":{"codec":"jpeg"}}}
    {"type":"audio","audio":"<base64 pcm int16 LE 16kHz mono>","timestamp":...}
    {"type":"image","image":"<base64 jpeg>","timestamp":...}
    {"type":"face","face_id":"alex","confidence":0.97,"timestamp":...}
    {"type":"emotion","emotion":"happy","timestamp":...}  # face emotion
    {"type":"hb","timestamp":...}

Service -> camera:
    {"type":"ok","detail":"ok","timestamp":...}
    {"type":"mute"} / {"type":"unmute"} / {"type":"capture"} (+timestamp)

Service -> robot (/robot):
  Text (commands):
    {"type":"movement","axis":"left","degrees":60,"timestamp":...}
    {"type":"emotion","name":"happy","timestamp":...}
  Binary (playback audio): [byte0=type][byte1=codec][raw PCM int16 LE
  16 kHz mono]; an empty payload [type][codec] = end-of-stream marker.
  Audio is binary so the ESP32 plays the PCM directly from the frame
  buffer without base64/JSON decoding (no stutter) — commands stay text.

Robot -> service (text):
    {"type":"hb","timestamp":...}
    {"type":"ack","command":"MOVE:left:60","timestamp":...}
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Message types.
# ---------------------------------------------------------------------------
MSG_HELLO = "hello"
MSG_AUDIO = "audio"
MSG_IMAGE = "image"
MSG_FACE = "face"
MSG_EMOTION = "emotion"
MSG_HB = "hb"
MSG_OK = "ok"
MSG_ERROR = "error"
MSG_MUTE = "mute"
MSG_UNMUTE = "unmute"
MSG_CAPTURE = "capture"
MSG_MOVEMENT = "movement"
MSG_ACK = "ack"

# Robot emotion names (validated before sending).
ROBOT_EMOTIONS = (
    "happy", "angry", "sad", "doubt", "sleepy", "neutral",
)

# Robot movement axes (validated before sending).
ROBOT_AXES = ("left", "right", "up", "down", "center")

# Binary audio frame layout (server -> robot): byte[0] = type,
# byte[1] = codec, then raw PCM payload. The only codec is 1:
# raw PCM int16 LE, 16 kHz mono. An empty payload = EOF marker.
ROBOT_AUDIO_FRAME_TYPE = 1
ROBOT_AUDIO_CODEC_PCM = 1


# ---------------------------------------------------------------------------
# Encode helpers.
# ---------------------------------------------------------------------------
def enc_b64(data: bytes) -> str:
    """Base64 (ASCII) for embedding binary data into JSON."""
    return base64.b64encode(data).decode("ascii")


def dec_b64(value: str) -> bytes:
    """Decodes a base64 field back to bytes."""
    return base64.b64decode(value)


def now_us() -> int:
    """Current Unix time in microseconds (int)."""
    return int(time.time() * 1_000_000)


def make_message(mtype: str, **fields: Any) -> str:
    """Builds a JSON text message: {"type": mtype, **fields, "timestamp": us}.

    The timestamp field is added automatically (Unix time, microseconds).
    """
    return json.dumps({"type": mtype, "timestamp": now_us(), **fields})


def parse_message(text: str) -> Optional[Dict[str, Any]]:
    """Parses a JSON text message. Returns a dict or None on malformed input."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Camera messages (camera -> service).
# ---------------------------------------------------------------------------
def hello_message(device: str, sample_rate: int,
                  image_codec: str = "jpeg") -> str:
    return make_message(
        MSG_HELLO, device=device,
        format={"audio": {"rate": sample_rate, "channels": 1, "bits": 16},
                "image": {"codec": image_codec}})


def audio_message(pcm: bytes) -> str:
    """Audio chunk from the camera mic: {"type":"audio","audio":"<b64>"}."""
    return make_message(MSG_AUDIO, audio=enc_b64(pcm))


def image_message(jpeg: bytes) -> str:
    """Picture from the camera: {"type":"image","image":"<b64>"}."""
    return make_message(MSG_IMAGE, image=enc_b64(jpeg))


def face_message(face_id: str, confidence: Any = None) -> str:
    fields: Dict[str, Any] = {"face_id": face_id}
    if confidence is not None:
        fields["confidence"] = confidence
    return make_message(MSG_FACE, **fields)


def emotion_message(emotion: str) -> str:
    """Face emotion recognized by the camera."""
    return make_message(MSG_EMOTION, emotion=emotion)


def hb_message() -> str:
    return make_message(MSG_HB)


# ---------------------------------------------------------------------------
# Service -> camera messages.
# ---------------------------------------------------------------------------
def ok_message(detail: str = "ok") -> str:
    return make_message(MSG_OK, detail=detail)


def error_message(detail: str) -> str:
    return make_message(MSG_ERROR, detail=detail)


def mute_message() -> str:
    return make_message(MSG_MUTE)


def unmute_message() -> str:
    return make_message(MSG_UNMUTE)


def capture_message() -> str:
    return make_message(MSG_CAPTURE)


# ---------------------------------------------------------------------------
# Service -> robot messages.
# ---------------------------------------------------------------------------
def robot_audio_message(pcm: bytes) -> str:
    """Playback chunk for the robot; empty pcm = end-of-stream marker."""
    return make_message(MSG_AUDIO, audio=enc_b64(pcm))


def robot_movement_message(axis: str, degrees: int = 0) -> str:
    """Movement command: left/right/up/down with degrees, or center."""
    if axis == "center":
        return make_message(MSG_MOVEMENT, axis="center")
    return make_message(MSG_MOVEMENT, axis=axis, degrees=int(degrees))


def robot_emotion_message(name: str) -> str:
    return make_message(MSG_EMOTION, name=name)


def robot_ack_message(command: str) -> str:
    """Robot -> service: movement finished (ACK:MOVE...)."""
    return make_message(MSG_ACK, command=command)


def robot_audio_frame(pcm: bytes) -> bytes:
    """Binary playback frame [type][codec][pcm]; empty pcm = EOF marker."""
    return bytes((ROBOT_AUDIO_FRAME_TYPE, ROBOT_AUDIO_CODEC_PCM)) + pcm