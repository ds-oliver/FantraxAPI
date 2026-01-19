#!/usr/bin/env bash
set -euo pipefail

# Helper wrapper for sofascore_lineup_listener.py
# Honors PYTHON_BIN if you want to point at a venv, otherwise uses python3.
PYTHON_BIN="${PYTHON_BIN:-python3}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

cd "$ROOT_DIR"
exec "$PYTHON_BIN" scripts/sofascore_lineup_listener.py "$@"
