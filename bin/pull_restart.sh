#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/pull_restart.log"
STREAMLIT_LOG="$LOG_DIR/streamlit.out"
BRANCH="${BRANCH:-testing}"

mkdir -p "$LOG_DIR"
exec >> "$LOG_FILE" 2>&1

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

log "Starting pull_restart (${BRANCH})"
cd "$REPO_ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "error: not a git repository"
  exit 1
fi

log "Fetching origin/${BRANCH}"
git fetch origin "$BRANCH"

log "Resetting to origin/${BRANCH}"
git checkout "$BRANCH" >/dev/null 2>&1 || true
git reset --hard origin/"$BRANCH"

log "Ensuring virtual environment"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

log "Installing requirements"
.venv/bin/pip install -r requirements.txt

log "Restarting Streamlit"
pkill -f "streamlit run" >/dev/null 2>&1 || true
mkdir -p "$LOG_DIR"
PYTHONPATH="$REPO_ROOT" \
  .venv/bin/python -m streamlit run "$REPO_ROOT/apps/auth_login/overview.py" \
  --server.address 127.0.0.1 --server.port 8501 \
  >> "$STREAMLIT_LOG" 2>&1 &

log "pull_restart finished"
