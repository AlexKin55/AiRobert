"""AiService JSON protocol (camera <-> service, TEXT JSON, base64 embedded).

The robot is controlled BY THE CAMERA over a local WS (M5StackUnitV2-M12);
AiService may run in the cloud and only talks to the camera.

Every message carries "timestamp": Unix time in MICROSECONDS (added by
make_message; clients add it too).

Camera -> service:
    hello / audio (b64 PCM) / image (b64 JPEG) / face / emotion (face)
    touch (robot touch relay) / decay (phrase request) / hb (with robot flag)

Service -> camera:
    ok / error
    {"type":"emotion","name":...}                  # emotion for the robot
    {"type":"play","audio":"<b64 full PCM>"}       # one full audio answer
    {"type":"play","audio":"...","decay":"<em>"}   # decay phrase (stale dropped)
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
MSG_TOUCH = "touch"
MSG_PLAY = "play"
MSG_DECAY = "decay"

# Robot emotion names (validated before sending).
ROBOT_EMOTIONS = (
    "happy", "angry", "sad", "doubt", "sleepy", "neutral",
)

# Head-touch actions sent by the robot ({"type":"touch","action":...}).
TOUCH_ACTIONS = ("press", "release", "swipe_forward", "swipe_backward")


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


def play_message(pcm: bytes, decay: str = "") -> str:
    """Full audio answer for the camera: {"type":"play","audio":"<b64>"}.

    The cloud sends ONE message per answer — the CAMERA splits the PCM into
    chunks and plays them on the robot with real-time pacing (no chunk relay
    through the cloud). An optional ``decay=<emotion>`` marks a decay phrase;
    the camera drops stale answers.
    """
    fields: Dict[str, Any] = {"audio": enc_b64(pcm)}
    if decay:
        fields["decay"] = decay
    return make_message(MSG_PLAY, **fields)


def robot_emotion_message(name: str) -> str:
    """Emotion command for the robot via the camera:
    {"type":"emotion","name":...} (the camera relays it locally)."""
    return make_message(MSG_EMOTION, name=name)