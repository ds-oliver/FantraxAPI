#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="${SSH_HOST:-fantrax-vps-root}"
SERVICE_NAME="${SERVICE_NAME:-fantrax-pull-restart.service}"
COMMIT_MESSAGE=""
AUTO_COMMIT=1
SKIP_STATUS=0

usage() {
  cat <<'EOF'
Usage:
  bin/push_and_deploy_vps.sh [options]

Options:
  -m, --message <msg>      Commit message for auto-commit.
  -H, --host <ssh-host>    SSH host alias (default: fantrax-vps-root or $SSH_HOST).
  -s, --service <name>     systemd service on VPS (default: fantrax-pull-restart.service or $SERVICE_NAME).
  --no-commit              Fail if local changes are not committed (do not auto-commit).
  --skip-status            Skip remote service status/log tail output.
  -h, --help               Show this help.

Behavior:
  1) Auto-commits local changes on the current branch (unless --no-commit)
  2) Pushes current branch to origin
  3) SSHes to VPS and restarts the configured service
EOF
}

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: required command not found: $1" >&2
    exit 1
  }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--message)
      [[ $# -ge 2 ]] || { echo "error: missing value for $1" >&2; exit 1; }
      COMMIT_MESSAGE="$2"
      shift 2
      ;;
    -H|--host)
      [[ $# -ge 2 ]] || { echo "error: missing value for $1" >&2; exit 1; }
      SSH_HOST="$2"
      shift 2
      ;;
    -s|--service)
      [[ $# -ge 2 ]] || { echo "error: missing value for $1" >&2; exit 1; }
      SERVICE_NAME="$2"
      shift 2
      ;;
    --no-commit)
      AUTO_COMMIT=0
      shift
      ;;
    --skip-status)
      SKIP_STATUS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd git
require_cmd ssh

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: this script must be run inside a git repository" >&2
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" == "HEAD" ]]; then
  echo "error: detached HEAD; checkout a branch first" >&2
  exit 1
fi

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "error: git remote 'origin' not configured" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  if [[ "$AUTO_COMMIT" -eq 0 ]]; then
    echo "error: local changes detected, commit them first or remove --no-commit" >&2
    exit 1
  fi

  if [[ -z "$COMMIT_MESSAGE" ]]; then
    COMMIT_MESSAGE="chore: deploy $(date +'%Y-%m-%d %H:%M:%S %Z')"
  fi

  log "Committing local changes on branch '$BRANCH'"
  git add -A
  git commit -m "$COMMIT_MESSAGE"
else
  log "No local changes to commit"
fi

log "Pushing '$BRANCH' to origin"
git push origin "$BRANCH"

log "Restarting VPS service '$SERVICE_NAME' on host '$SSH_HOST'"
if [[ "$SKIP_STATUS" -eq 1 ]]; then
  ssh "$SSH_HOST" "sudo systemctl restart '$SERVICE_NAME'"
else
  ssh "$SSH_HOST" \
    "set -e; \
     sudo systemctl restart '$SERVICE_NAME'; \
     sleep 2; \
     if sudo systemctl is-failed --quiet '$SERVICE_NAME' 2>/dev/null; then \
       echo 'error: service entered failed state after restart (Streamlit did not stay up).' >&2; \
       sudo systemctl status '$SERVICE_NAME' --no-pager -l || true; \
       sudo journalctl -u '$SERVICE_NAME' -n 60 --no-pager; \
       tail -n 80 /opt/FantraxAPI/logs/pull_restart.log 2>/dev/null || true; \
       exit 1; \
     fi; \
     sudo systemctl status '$SERVICE_NAME' --no-pager -l; \
     tail -n 40 /opt/FantraxAPI/logs/pull_restart.log 2>/dev/null || true"
fi

log "Deploy complete"
