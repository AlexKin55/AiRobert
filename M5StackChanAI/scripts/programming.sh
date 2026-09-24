#!/usr/bin/env bash
# Flash the robot: build + upload the firmware to the device.
#
# The port is taken from platformio.ini (upload_port). It can be overridden
# with the PORT variable, e.g.: PORT=/dev/ttyUSB0 ./scripts/programming.sh
set -euo pipefail

# Look for PlatformIO: first the PIO variable, then the common install
# locations (~/.platformio/penv — classic CLI venv; ~/.local/bin — pip
# --user), then the binary in $PATH.
PIO_DEFAULT_PENV="$HOME/.platformio/penv/bin/pio"
PIO_DEFAULT_LOCAL="$HOME/.local/bin/pio"
PIO="${PIO:-}"
if [[ -z "$PIO" && -x "$PIO_DEFAULT_PENV" ]]; then
    PIO="$PIO_DEFAULT_PENV"
fi
if [[ -z "$PIO" && -x "$PIO_DEFAULT_LOCAL" ]]; then
    PIO="$PIO_DEFAULT_LOCAL"
fi
if [[ -z "$PIO" ]]; then
    PIO="$(command -v pio 2>/dev/null || true)"
fi

if [[ -z "$PIO" || ! -x "$PIO" ]]; then
    echo "[programming] PlatformIO not found (PIO variable, $PIO_DEFAULT_PENV, $PIO_DEFAULT_LOCAL, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${ENV_NAME:-m5stack-cores3}"

cd "$ROOT_DIR"
if [[ ! -f "platformio.ini" ]]; then
    echo "[programming] platformio.ini not found in $ROOT_DIR" >&2
    exit 1
fi

ARGS=(
    "run"
    "--environment" "$ENV_NAME"
    "--target" "upload"
)

# Allows an explicit port when needed.
if [[ -n "${PORT:-}" ]]; then
    ARGS+=("--upload-port" "$PORT")
fi

echo "[programming] Upload port: ${PORT:-<from platformio.ini>}"
echo "[programming] Building and flashing ($ENV_NAME) ..."
exec "$PIO" "${ARGS[@]}"