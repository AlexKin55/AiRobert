#!/usr/bin/env bash
# Flash the TESTS into the robot: build the test firmware + upload to the device.
#
# This project has no tests/ directory yet — the script exits with a clear
# message unless a tests/tests.cpp entry point appears (like in AiBot).
# If it does, the m5stack-cores3-tests environment must exist in
# platformio.ini; the environment can be overridden with ENV_NAME.
#
# The port is taken from platformio.ini (upload_port). It can be overridden
# with the PORT variable, e.g.: PORT=/dev/ttyUSB0 ./scripts/test_programming.sh
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
    echo "[test-programming] PlatformIO not found (PIO variable, $PIO_DEFAULT_PENV, $PIO_DEFAULT_LOCAL, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Default test environment (used only when tests/ exists).
ENV_NAME="${ENV_NAME:-m5stack-cores3-tests}"

cd "$ROOT_DIR"
if [[ ! -f "platformio.ini" ]]; then
    echo "[test-programming] platformio.ini not found in $ROOT_DIR" >&2
    exit 1
fi
if [[ ! -f "tests/tests.cpp" ]]; then
    echo "[test-programming] No tests/tests.cpp in this project — nothing to flash." >&2
    echo "[test-programming] (Test scenarios live in tests/*.cpp, as in AiBot.)" >&2
    exit 1
fi

ARGS=(
    "run"
    "--environment" "$ENV_NAME"
    "--target" "upload"
)

if [[ -n "${PORT:-}" ]]; then
    ARGS+=("--upload-port" "$PORT")
fi

echo "[test-programming] Upload port: ${PORT:-<from platformio.ini>}"
echo "[test-programming] Building and flashing the TESTS ($ENV_NAME) ..."
exec "$PIO" "${ARGS[@]}"