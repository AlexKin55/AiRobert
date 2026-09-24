#!/usr/bin/env bash
# Build the PlatformIO firmware.
# The PlatformIO path is given explicitly because penv may not be in $PATH.
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
    echo "[build] PlatformIO not found (PIO variable, $PIO_DEFAULT, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Default build environment — the robot firmware.
ENV_NAME="${ENV_NAME:-m5stack-cores3}"

cd "$ROOT_DIR"
echo "[build] Building the robot firmware ($ENV_NAME) ..."
exec "$PIO" run --environment "$ENV_NAME" "$@"