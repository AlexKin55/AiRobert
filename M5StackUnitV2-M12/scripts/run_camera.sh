#!/bin/sh
# Camera "firmware" launcher (M5StackUnitV2-M12) without Jupyter.
#
# The code lives in the M5StackUnitV2-M12/src package (plain Python modules:
# settings, wsclient, audio, camera, state_machine, main). The script runs
# the src.main entry point: connection to AiService /camera, UnitV2
# microphone capture and audio sending (with auto-reconnect when the server
# is unavailable).
#
# Dependencies: pip install -r requirements.txt
#
# Settings: settings.json on the camera SD card or settings module defaults.
# Recording duration (s): ./scripts/run_camera.sh 60
# Show configuration only: ./scripts/run_camera.sh --check
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PY="${PY:-python3}"

cd "$ROOT_DIR"
exec "$PY" "$ROOT_DIR/scripts/run_camera.py" "$@"