#!/usr/bin/env python3
"""M5Stack UnitV2 -> AiService: main launcher (no Jupyter).

Starts the camera session: audio capture from the UnitV2 microphones (two
mics, downmixed to mono) and PCM sending to AiService over the JSON protocol
(``{"type":"audio","audio":"<base64>"}``). Pictures are sent on the server
``capture`` command. When the server is unavailable — auto-reconnect.

Settings live in one place — the ``settings`` module: defaults are there;
if ``settings.json`` exists on the camera SD card (see
``M5StackUnitV2-M12/settings.example.json``) it overrides the defaults.

Usage:
  python3 -m src.main [--check] [SECONDS]
    SECONDS  — recording duration (overrides record_seconds)
    --check  — load modules and show the configuration without recording

Startup order:
1. Start the server: ``cd AiService && ./scripts/run_server.sh``.
2. Install dependencies: ``pip install -r requirements.txt``.
3. Optionally put ``settings.json`` on the camera SD card.
4. Run: ``python3 -m src.main`` (from the M5StackUnitV2-M12 directory).
"""
from __future__ import annotations

import importlib
import shutil
import sys
import threading
from typing import Optional

from .settings import SETTINGS
from .state_machine import StateMachine

# Python dependencies (see requirements.txt). Nothing is installed
# automatically. Audio capture — system arecord (alsa-utils), not PortAudio.
REQUIRED = (
    ("websocket-client", "websocket"),  # sync WebSocket client
    ("opencv-python", "cv2"),           # camera snapshots (Camera class)
)

PREFIX = "[camera]"


def check_deps() -> bool:
    """Checks dependencies; installs nothing."""
    missing = []
    for pkg, mod in REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if shutil.which("arecord") is None:
        missing.append("arecord (alsa-utils)")
    if missing:
        print(f"{PREFIX} missing dependencies: {', '.join(missing)}")
        print(f"{PREFIX} python packages: pip install -r requirements.txt")
        print(f"{PREFIX} arecord: sudo apt install alsa-utils")
        return False
    return True


def _print_settings() -> None:
    print(f"{PREFIX} settings:")
    for key, value in SETTINGS.items():
        if key != "settings_paths":
            print(f"    {key} = {value!r}")


def _tune_thread_stack() -> None:
    """Shrinks the stack of new threads (default ~8 MB) to 512 KB.

    On the low-power UnitV2 a large stack wastes virtual memory and can cause
    ``RuntimeError: can't start new thread``.
    """
    try:
        current = threading.stack_size()
        target = 512 * 1024
        if not current or current > target:
            threading.stack_size(target)
    except (ValueError, RuntimeError):
        pass  # the platform does not support stack sizing


def _print_thread_hint() -> None:
    """Prints diagnostics on a thread/memory shortage."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        limits = f"{soft}/{hard}"
    except Exception:
        limits = "?"
    print(f"{PREFIX} threads in process: {threading.active_count()}, "
          f"process/thread limit: {limits}")
    print(f"{PREFIX} hint: kill stale camera processes "
          "(ps aux | grep -E 'run_camera|arecord') and retry, "
          "or raise the limit: ulimit -u")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    rest = [a for a in args if a != "--check"]
    seconds: Optional[float] = None
    if rest:
        try:
            seconds = float(rest[0])
        except ValueError:
            print(f"{PREFIX} invalid seconds: {rest[0]!r}")
            return 2

    if not check_deps():
        return 1

    if check_only:
        print(f"{PREFIX} modules loaded: settings, wsclient, audio, "
              "camera, state_machine")
        _print_settings()
        return 0

    s = SETTINGS
    if seconds is not None:
        s["record_seconds"] = seconds
    print(f"{PREFIX} camera session: ws={s['ws_url']}, "
          f"record={s['record_seconds']} s (0 = until interrupted)")

    sm = StateMachine(
        s["ws_url"],
        sample_rate=s["sample_rate"],
        chunk_seconds=s["chunk_seconds"],
        audio_device=s["audio_device"],
        audio_channels=s["audio_channels"],
        audio_sudo=s["audio_sudo"],
        record_seconds=s["record_seconds"],
        reconnect_delay=s["reconnect_delay"],
        record_restart_delay=s["record_restart_delay"],
        vad_config=s["vad"],
        face_detect=s["face_detect"],
        robot_cfg=s["robot"],
        touch_cfg=s["touch"],
        playback_cfg=s["playback"],
        decay_cfg=s["decay"],
    )
    _tune_thread_stack()
    try:
        sm.start()
    except RuntimeError as exc:
        print(f"{PREFIX} thread start error: {exc}")
        _print_thread_hint()
        return 1
    if s["record_seconds"] > 0:
        print(f"{PREFIX} recording {s['record_seconds']} s ...")
    else:
        print(f"{PREFIX} continuous run (record_seconds=0) — stop with "
              "Ctrl+C; auto-reconnect if the server is unavailable")
    try:
        sm.run()
    except KeyboardInterrupt:
        print(f"\n{PREFIX} stopped by user (Ctrl+C)")
    finally:
        sm.summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())