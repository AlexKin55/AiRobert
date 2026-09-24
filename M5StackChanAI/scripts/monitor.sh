#!/usr/bin/env bash
# Connect to the UART to view the robot log via the PlatformIO Serial Monitor.
#
# Port and baud rate are taken from platformio.ini (monitor_port, monitor_speed).
# They can be overridden with: PORT=/dev/ttyUSB0 BAUDRATE=115200
# DTR/RTS settings are also read from platformio.ini (monitor_dtr, monitor_rts).
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
    echo "[monitor] PlatformIO not found (PIO variable, $PIO_DEFAULT_PENV, $PIO_DEFAULT_LOCAL, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${ENV_NAME:-m5stack-cores3}"

cd "$ROOT_DIR"
if [[ ! -f "platformio.ini" ]]; then
    echo "[monitor] platformio.ini not found in $ROOT_DIR" >&2
    exit 1
fi

ARGS=(
    "device" "monitor"
    "--environment" "$ENV_NAME"
)

if [[ -n "${PORT:-}" ]]; then
    ARGS+=("--port" "$PORT")
fi
if [[ -n "${BAUDRATE:-}" ]]; then
    ARGS+=("--baud" "$BAUDRATE")
fi

echo "[monitor] Port: ${PORT:-<from platformio.ini>}, baud rate: ${BAUDRATE:-<from platformio.ini>}"
echo "[monitor] Connecting to UART, press Ctrl+C to exit ..."
exec "$PIO" "${ARGS[@]}"