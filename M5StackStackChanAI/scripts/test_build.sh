#!/usr/bin/env bash
# Build ONLY the test firmware (no flashing to the device).
#
# Builds the test project environment (default m5stack-cores3).
# Override the environment with the ENV_NAME variable.
# Quick compile check of the tests: ./scripts/test_build.sh
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
    echo "[test-build] PlatformIO not found (PIO variable, $PIO_DEFAULT, \$PATH)." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Default test environment.
ENV_NAME="${ENV_NAME:-m5stack-cores3-tests}"

cd "$ROOT_DIR"
echo "[test-build] Building the test firmware ($ENV_NAME) without uploading ..."
exec "$PIO" run --environment "$ENV_NAME"