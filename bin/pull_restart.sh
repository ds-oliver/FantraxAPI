#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/logs"
LOG_FILE="$LOG_DIR/pull_restart.log"
STREAMLIT_LOG="$LOG_DIR/streamlit.out"
BRANCH="${BRANCH:-testing}"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

log "Starting pull_restart (${BRANCH})"
cd "$REPO_ROOT"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "error: not a git repository (REPO_ROOT=${REPO_ROOT})"
  log "hint: /opt/FantraxAPI must be a clone with a .git directory, or Streamlit never starts. See docs/SSH_COMMANDS_AND_TROUBLESHOOTING.md (VPS deploy: not a git repository)."
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
log "Launching Streamlit"
exec env PYTHONPATH="$REPO_ROOT" \
  .venv/bin/python -m streamlit run "$REPO_ROOT/apps/auth_login/overview.py" \
  --server.address 127.0.0.1 --server.port 8501 \
  >> "$STREAMLIT_LOG" 2>&1
