"""UnitV2 camera settings: defaults + settings.json from the SD card, logging.

Resolution order:
1. values from the ``DEFAULTS`` dict;
2. if ``settings.json`` exists on the camera SD card, its keys override the
   defaults (missing keys stay from defaults);
3. result — global ``SETTINGS`` and ``SETTINGS_PATH`` (None if no file).

``settings.json`` search paths: ``/sdcard/settings.json``,
``/media/sdcard/settings.json``, ``./settings.json`` (cwd). The path can be
set explicitly via the ``CAMERA_SETTINGS_PATH`` environment variable.

The module also configures file logging with rotation and installs global
uncaught-exception handlers (sys.excepthook + threading.excepthook) so that
tracebacks land in the log file.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

# Defaults — applied when settings.json on the SD card is missing.
# A file from the SD overrides only the keys it defines.
DEFAULTS = {
    'ws_url': 'ws://192.168.1.8:9002/camera',  # AiService URL
    # Audio capture via system arecord (ALSA): device (plugin) name,
    # e.g. 'default', 'plughw:1,0'; None = 'default'.
    'audio_device': None,
    'audio_channels': 1,     # 1 = mono; 2 = stereo (UnitV2 mics, downmixed)
    # arecord needs root on UnitV2: None = auto (sudo -n true);
    # true/false — force use/do not use sudo.
    'audio_sudo': None,
    'sample_rate': 16000,    # Hz — protocol format (PCM int16 LE, mono)
    'chunk_seconds': 0.5,    # one PCM chunk duration, s
    'record_seconds': 0,     # recording duration, s; 0 = until interrupted
    'reconnect_delay': 3.0,  # s, pause between server reconnect attempts
    'record_restart_delay': 3.0,  # s, pause before restarting arecord on error
    # File logging with rotation:
    #   max_bytes — one file size (2 MB); when reached the next file is
    #   created, backup_count = 1 -> 2 files total, the oldest is removed.
    'logging': {
        'enabled': True,
        'level': 'DEBUG',          # DEBUG/INFO/WARNING/ERROR
        'dir': '/sdcard/logs',   # log dir (SD card); fallback ./logs
        'max_bytes': 2 * 1024 * 1024,
        'backup_count': 1,
    },
    # Adaptive VAD: audio is sent only while the user is talking.
    #   alpha            — noise adaptation speed (0..1);
    #   min_threshold    — absolute loudness floor (int16 RMS);
    #   multiplier       — how much louder than the background speech must be;
    #   silence_seconds  — pause after which the speech segment ends;
    #   pre_record_seconds — pre-record before speech starts (so the first
    #                      word is not cut off).
    'vad': {
        'enabled': True,
        'alpha': 0.05,
        'min_threshold': 20,
        'multiplier': 1.1,
        # Threshold WHILE a segment is active: lowered near the noise floor so
        # a quiet continuation of the dialogue after a loud trigger ("Robert")
        # is recorded; after silence the normal threshold returns.
        'active_multiplier': 1.05,
        'active_min_threshold': 25,
        # Startup calibration: for the first seconds the noise floor quickly
        # catches up with the real background so room noise does not keep the
        # segment open forever (background above threshold -> "stuck").
        'calibration_seconds': 3.0,
        'silence_seconds': 1.0,
        'pre_record_seconds': 0.4,
    },
    # settings.json search paths (camera SD card).
    'settings_paths': [
        '/sdcard/settings.json',
        '/media/sdcard/settings.json',
        'settings.json',
    ],

    'face_detect': {
        'resolution': (640, 480),
        'max_radius': 60,
        'lost_timeout': 1.5,
    },

    # Local WebSocket SERVER for the robot (M5StackChanAI connects here).
    # The camera controls the robot locally: ws://<camera-ip>:port/path.
    'robot': {
        'enabled': True,
        'host': '0.0.0.0',
        'port': 8765,
        'path': '/robot',
    },

    # Local touch reaction: the camera itself shows the emotion and plays the
    # sound on the robot (no cloud round-trip; works offline).
    'touch': {
        'enabled': True,
        'emotion': 'happy',
        'sound_file': 'sounds/touch.wav',   # WAV PCM int16 LE 16 kHz mono
    },

    # How the camera plays audio on the robot (it splits the full audio
    # answer from the cloud into chunks itself):
    #   chunk_seconds — one binary PCM frame duration (0.15 s = 4800 B,
    #                   stays below the robot WS receive limit ~8 KB);
    #   speed        — delivery pace: pause between frames = duration / speed
    #                  (1.0 = real time);
    #   drop_tail_seconds — mic remains muted this long after EOF (echo tail).
    'playback': {
        'chunk_seconds': 0.15,
        'speed': 1.0,
        'drop_tail_seconds': 2.0,
    },

    # Post-dialogue emotion decay timers ON THE CAMERA (Neutral -> Sad ->
    # Sleepy + yawn + snore). Phrases for each stage are requested from
    # AiService; if the cloud is offline the emotion still switches locally.
    'decay': {
        'enabled': True,
        'neutral_min': 5.0,      # minutes after the dialogue end
        'sad_min': 30.0,
        'sleepy_min': 60.0,
        'snore_after_min': 60.1,  # minutes after the Sleepy phrase
        'timeout_s': 25.0,       # wait for the cloud phrase answer
        'yawn_file': 'sounds/yawn.wav',
        'snore_file': 'sounds/snore.wav',
    },
}


def _candidates():
    env = os.environ.get('CAMERA_SETTINGS_PATH')
    if env:
        return [env]
    return DEFAULTS['settings_paths']


def _find_settings_file():
    for path in _candidates():
        if os.path.isfile(path):
            return path
    return None


def _deep_merge(base, extra):
    """Recursively merges extra into base (nested dicts are merged by key).

    Needed for dict sections (logging, vad): settings.json may set only part
    of a section (e.g. only level) — the other keys stay from defaults.
    """
    result = dict(base)
    for key, value in extra.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings():
    """Reads settings.json from the SD card; no file -> defaults.

    Returns (settings, path): settings — settings dict (defaults + file
    overrides), path — loaded file path or None.
    """
    settings = dict(DEFAULTS)
    path = _find_settings_file()
    if path is None:
        print('settings: settings.json not found, using defaults')
        return settings, None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print('settings: file', path, 'is not a JSON object, defaults')
            return settings, None
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            print('settings: unknown keys ignored:', unknown)
        overrides = {k: v for k, v in data.items() if k in DEFAULTS}
        settings = _deep_merge(settings, overrides)
        print('settings: loaded', path)
        return settings, path
    except Exception as exc:
        print('settings: error reading', path, ':', exc)
        return settings, None


SETTINGS, SETTINGS_PATH = load_settings()


# --- file logging setup (rotation) ------------------------------------
def setup_logging(settings):
    """Configures file logging with rotation (see the 'logging' section)."""
    cfg = settings.get('logging') or {}
    if not cfg.get('enabled', False):
        return None
    level = getattr(logging, str(cfg.get('level', 'INFO')).upper(), logging.INFO)
    max_bytes = int(cfg.get('max_bytes', 2 * 1024 * 1024))
    backup_count = int(cfg.get('backup_count', 1))
    log_dir = str(cfg.get('dir') or 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, 'camera.log')
    except OSError as exc:
        # The SD card may be unavailable — write to the local directory.
        print('settings: log dir unavailable, fallback ./logs:', exc)
        log_dir = 'logs'
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, 'camera.log')
    fmt = logging.Formatter(
        '%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S')
    handler = RotatingFileHandler(path, maxBytes=max_bytes,
                                  backupCount=backup_count, encoding='utf-8')
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    logging.getLogger('camera').info(
        'logging to %s (level %s, %d files of %d bytes)',
        path, cfg.get('level', 'INFO'), backup_count + 1, max_bytes)
    return path


LOG_FILE = setup_logging(SETTINGS)


# --- global uncaught-exception handlers -------------------------------
# Tracebacks (including background threads) go to the log file.
_CAMERA_LOG = logging.getLogger('camera')


def _excepthook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _CAMERA_LOG.critical(
        'uncaught exception:\n%s',
        ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    # Continue with the standard handling (the error is also visible in console).
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _thread_excepthook(args):
    tb = ''.join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback))
    _CAMERA_LOG.critical(
        'uncaught exception in thread %r:\n%s',
        getattr(args.thread, 'name', '?'), tb)


sys.excepthook = _excepthook
threading.excepthook = _thread_excepthook