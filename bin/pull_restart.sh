#!/usr/bin/env bash
set -euo pipefail

# systemd often runs services without HOME set; git then ignores /root/.gitconfig (e.g. safe.directory for dubious ownership).
export HOME="${HOME:-/root}"

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

# Git 2.35+ "dubious ownership" when root runs git on a tree owned by another user. Passing safe.directory here
# avoids relying on ~/.gitconfig (systemd often has no HOME) and lets this script bootstrap: a broken deploy
# could not git-pull a fix; this works on the first run after copying only this file.
git() {
  command git -c "safe.directory=${REPO_ROOT}" "$@"
}

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "error: git check failed in ${REPO_ROOT}: $(git rev-parse --is-inside-work-tree 2>&1 | tr '\n' ' ')"
  log "hint: dubious ownership is handled in-script; if this persists, see docs/SSH_COMMANDS_AND_TROUBLESHOOTING.md (VPS deploy: git / pull_restart fails)."
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
# EasySoccerData (git) runs setup that imports esd → playwright. Pip's default PEP 517 build isolation
# uses a clean env without playwright, so the wheel build fails with ModuleNotFoundError: playwright.
# --no-build-isolation uses the venv so packages listed earlier in requirements.txt are importable.
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -r requirements.txt --no-build-isolation

log "Restarting Streamlit"
pkill -f "streamlit run" >/dev/null 2>&1 || true
log "Launching Streamlit"
exec env PYTHONPATH="$REPO_ROOT" \
  .venv/bin/python -m streamlit run "$REPO_ROOT/apps/auth_login/overview.py" \
  --server.address 127.0.0.1 --server.port 8501 \
  >> "$STREAMLIT_LOG" 2>&1
