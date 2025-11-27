#!/usr/bin/env bash
set -euo pipefail

# Helper wrapper for sofascore_kickoff_watcher.py
# Honors PYTHON_BIN if you want to point at a venv, otherwise uses python.
PYTHON_BIN="${PYTHON_BIN:-python}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"
exec "$PYTHON_BIN" scripts/sofascore_kickoff_watcher.py "$@"
