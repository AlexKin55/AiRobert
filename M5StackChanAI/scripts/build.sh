#!/usr/bin/env bash
# Build the PlatformIO firmware.
# The PlatformIO path is given explicitly because penv may not be in $PATH.
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
    echo "[build] PlatformIO not found (PIO variable, $PIO_DEFAULT_PENV, $PIO_DEFAULT_LOCAL, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Default build environment — the robot firmware (see platformio.ini).
ENV_NAME="${ENV_NAME:-m5stack-cores3}"

cd "$ROOT_DIR"
if [[ ! -f "platformio.ini" ]]; then
    echo "[build] platformio.ini not found in $ROOT_DIR" >&2
    exit 1
fi
echo "[build] Building the robot firmware ($ENV_NAME) ..."
exec "$PIO" run --environment "$ENV_NAME" "$@"