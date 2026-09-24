#!/usr/bin/env bash
# Launch the AiService (two WebSockets: /camera and /robot).
#
# The address/port can be overridden with the HOST/PORT variables:
#   HOST=127.0.0.1 PORT=9002 ./scripts/run_server.sh
# The interpreter — with the PY variable (default python3).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9002}"
PY="${PY:-python3}"

cd "$ROOT_DIR"
# The entry point python3 -m src.server always listens on 0.0.0.0 (all
# addresses): running uvicorn manually without --host binds to 127.0.0.1, and
# the camera/robot get "Connection reset" on the LAN address. HOST/PORT here
# override the defaults via the AISERVICE_* env vars (0.0.0.0 / config port).
echo "[server] starting: ws://$HOST:$PORT/camera (camera) and ws://$HOST:$PORT/robot (robot)"
export AISERVICE_HOST="${HOST:-0.0.0.0}"
export AISERVICE_PORT="${PORT}"
export AISERVICE_LOG_CONFIG="$ROOT_DIR/config/logging.json"
exec "$PY" -m src.server