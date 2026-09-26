"""AiService settings loading from config/*.json.

Main config:  config/settings.json  (env AISERVICE_CONFIG)

CONFIG — the main config (used by the server). Missing keys are filled with
defaults. Any string value in the JSON supports environment variable
substitution of the form ${VAR} (e.g. "${RECORD_DIR}"); an unset variable is
replaced with an empty string.
"""
import json
import os
import re
from pathlib import Path
from typing import Any, Dict

# Defaults of the main config (no test-only parameters).
_DEFAULTS: Dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 9002},
    "camera_ws": {"path": "/camera"},
    "robot_ws": {"path": "/robot"},
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
        "bits_per_sample": 16,
        "frame_type": 1,
        "codec_pcm": 1,
        "chunk_seconds": 1.0,
    },
    "image": {
        "codec_jpeg": 1,
        "codec_png": 2,
        "max_frame_bytes": 3_000_000,
        "save_frames": False,
    },
    "recording": {
        "record_dir": "records",
        "save_audio": True,
        "save_images": True,
        "save_tts_audio": True,
        "save_prompts": True,
        "audio_rotate_seconds": 0,
    },
    "robot": {
        "play_chunk_seconds": 0.15,
        "play_speed": 1.0,
        "drop_audio_during_playback": True,
    },
    "yandex": {
        "api_key": "",
        "folder_id": "",
        "enabled": True,
        "realtime_model": "speech-realtime-260528",
        "realtime_input_rate": 16000,
        "realtime_output_rate": 16000,
        "realtime_language": "ru-RU",
        "realtime_timeout_s": 90,
        "system_prompt": "",
        "voice": "alena",
        "role": "",
        "weather_api_key": "",
        "emotion_decay_prompt": "",
        "emotion_decay_neutral_ms": 10000,
        "emotion_decay_sad_ms": 30000,
        "emotion_decay_sleepy_ms": 45000,
        "emotion_decay_yawn_file": "sounds/yawn.wav",
        "emotion_decay_snore_file": "sounds/snore.wav",
        "emotion_decay_snore_after_ms": 5000,
    },
}


def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _default_path(name: str) -> str:
    return str(_config_dir() / name)


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


# ${VAR} substitution in all string values of the JSON.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Recursively replaces ${VAR} in strings with environment values."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            return os.environ.get(match.group(1), "")
        return _ENV_RE.sub(repl, value)
    return value


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return _expand_env(json.load(f))
    except FileNotFoundError:
        return {}


def load() -> Dict[str, Any]:
    """Main config (config/settings.json)."""
    path = os.environ.get("AISERVICE_CONFIG", _default_path("settings.json"))
    return _merge(_DEFAULTS, _read(path))


CONFIG: Dict[str, Any] = load()