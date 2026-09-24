#!/usr/bin/env bash
# Flash the robot: build + upload the firmware to the device.
#
# The port is taken from platformio.ini (upload_port). It can be overridden
# with the PORT variable, e.g.: PORT=/dev/ttyUSB0 ./scripts/programming.sh
set -euo pipefail

# Look for PlatformIO: first the PIO variable, then the standard penv path,
# then the binary in $PATH (e.g. installed via pip --user).
PIO_DEFAULT="$HOME/.platformio/penv/bin/pio"
PIO="${PIO:-}"
if [[ -z "$PIO" && -x "$PIO_DEFAULT" ]]; then
    PIO="$PIO_DEFAULT"
fi
if [[ -z "$PIO" ]]; then
    PIO="$(command -v pio 2>/dev/null || true)"
fi

if [[ -z "$PIO" || ! -x "$PIO" ]]; then
    echo "[programming] PlatformIO not found (PIO variable, $PIO_DEFAULT, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${ENV_NAME:-m5stack-cores3}"

cd "$ROOT_DIR"

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