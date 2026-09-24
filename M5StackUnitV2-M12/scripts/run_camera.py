#!/usr/bin/env python3
"""Camera "firmware" launcher (M5StackUnitV2-M12) without Jupyter.

All the logic lives in the ``M5StackUnitV2-M12/src`` package (``src.main.main``):
dependency checks, ``--check``, the ``StateMachine`` session. This script only
adds the camera directory to ``sys.path`` and calls the entry point.

Usage:
  python3 scripts/run_camera.py [--check] [SECONDS]
    SECONDS  — recording duration (overrides record_seconds)
    --check  — load modules and show the configuration without recording
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())