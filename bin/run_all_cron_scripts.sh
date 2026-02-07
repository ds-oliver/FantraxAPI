#!/usr/bin/env bash
# Run all scripts that are normally scheduled via cron (for testing or one-off sync).
# Order: SofaScore listener (schedules + lineups), then kickoff watcher (confirmed), then conditional runner (swaps).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Use PYTHON_BIN if set and present; else .venv/bin/python if present; else python3
if [[ -n "${PYTHON_BIN:-}" ]]; then
  _PY="${PYTHON_BIN}"
  [[ "$_PY" == .venv/* || "$_PY" == .venv/bin/python ]] && _PY="$ROOT_DIR/.venv/bin/python"
else
  _PY="$ROOT_DIR/.venv/bin/python"
fi
if [[ ! -x "$_PY" ]]; then
  _PY=python3
fi
PYTHON_BIN="$_PY"
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

echo "[1/3] SofaScore lineup listener (predictions + confirmed)..."
"$PYTHON_BIN" scripts/sofascore_lineup_listener.py --mode both --horizon-days 7

echo "[2/3] SofaScore kickoff watcher..."
"$PYTHON_BIN" scripts/sofascore_kickoff_watcher.py

echo "[3/3] Conditional runner (all users)..."
"$PYTHON_BIN" scripts/conditional_runner.py --all-users

echo "Done."
