# SSH Commands & Troubleshooting

Purpose: a concise, repeatable guide for maintaining the repo, the VPS app, and the background jobs.

## Terminal Count (How Many Windows You Need)

- `1 terminal`: Any one-off VPS maintenance command (pull/restart/check logs).
- `2 terminals`: VPS Streamlit access from Mac via SSH tunnel.
  - Terminal A: SSH tunnel command, keep it running.
  - Terminal B: Optional extra SSH session for VPS commands while tunnel stays open.
- `3 terminals` (optional): If you also want local app/dev commands running at the same time.

## SSH Key Setup (Mac) for Passwordless Root Login

Do this once on your Mac. Do **not** paste these lines directly into the shell prompt as separate commands.

1. Ensure your key exists:
```bash
ls -la ~/.ssh/id_ed25519 ~/.ssh/id_ed25519.pub
```

2. Add SSH host aliases in `~/.ssh/config`:
```sshconfig
Host fantrax-vps
  HostName 5.78.118.108
  User hogan
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes

Host fantrax-vps-root
  HostName 5.78.118.108
  User root
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

3. Lock down permissions:
```bash
chmod 600 ~/.ssh/config
chmod 700 ~/.ssh
```

4. Verify resolved SSH config:
```bash
ssh -G fantrax-vps-root | grep -E 'hostname|user|identityfile'
```

5. Connect:
```bash
ssh fantrax-vps-root
```

If prompted unexpectedly, it is usually key passphrase prompt (not root password). Cache key in macOS keychain:
```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

## Daily Workflow (Mac → VPS)

1. Confirm local repo state.
```
cd /Users/hogan/FantraxAPI
git status -sb
```

2. Push changes to `testing`.
```
git add -A
git commit -m "describe your change"
git push origin testing
```

3. Pull + restart on VPS.
```
ssh fantrax-vps
BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh
sudo systemctl restart fantrax-pull-restart.service
```

4. Verify Streamlit on VPS.
```
sudo systemctl status fantrax-pull-restart.service
tail -n 40 /opt/FantraxAPI/logs/streamlit.out
```

5. (Optional) Tunnel VPS Streamlit to your Mac.
```
ssh -L 8501:127.0.0.1:8501 fantrax-vps
```

## VPS Quick Start (After Shutdown / Reconnect)

Use this exact sequence when your machine/processes were interrupted and you need everything back up quickly.
Terminal requirement: `2 terminals` (1 for admin commands, 1 for tunnel).

1. From your Mac, connect to VPS as root (admin shell).
```bash
ssh fantrax-vps-root
```

2. On VPS, pull latest code and restart app service.
```bash
systemctl restart fantrax-pull-restart.service
systemctl status fantrax-pull-restart.service --no-pager
```

3. Confirm Streamlit is listening on `127.0.0.1:8501`.
```bash
ss -ltnp | grep -E ':8501\\b' || true
tail -n 80 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true
```

4. If service did not bring Streamlit up, launch manually on VPS (foreground/debug mode).
```bash
cd /opt/FantraxAPI
PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501
```

5. Open a new terminal on your Mac and create the tunnel (leave it running).
```bash
ssh -L 8501:127.0.0.1:8501 hogan@5.78.118.108
```

6. Open the app in your Mac browser.
```text
http://127.0.0.1:8501
```

### When your shell is busy ("Restarting Streamlit / Launching Streamlit")

If your current SSH shell is busy and not accepting commands, **leave it alone** and open another terminal.

1. Open **Terminal A (new)** on Mac:
```bash
ssh fantrax-vps-root
```

2. In Terminal A, run checks:
```bash
ss -ltnp 'sport = :8501' || true
systemctl status fantrax-pull-restart.service --no-pager -l
tail -n 80 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true
```

3. Open **Terminal B (new)** on Mac for tunnel:
```bash
ssh -L 8501:127.0.0.1:8501 fantrax-vps-root
```

4. Open browser:
```text
http://127.0.0.1:8501
```

Rule:
- Busy shell = leave it.
- Use a second shell for checks.
- Use a dedicated shell for the tunnel.

### VPS Streamlit Tunnel (Mac)

Use the same user you normally SSH with (`hogan`) and run the tunnel from your **Mac**, not from the VPS.
Terminal requirement: `2 terminals`.

### 1) From your Mac: tunnel as `hogan`
```bash
ssh -L 8501:127.0.0.1:8501 hogan@5.78.118.108
```
Then open: `http://127.0.0.1:8501`

### 1b) From your Mac: tunnel as `root` (if root SSH key auth is configured)
```bash
ssh -L 8501:127.0.0.1:8501 fantrax-vps-root
```
Then open: `http://127.0.0.1:8501`

### 2) If you want it even simpler (no typing IP/user each time): add an SSH alias on your Mac
Add to `~/.ssh/config`:
```sshconfig
Host fantrax-vps
  HostName 5.78.118.108
  User hogan
```

Then you can run:
```bash
ssh -L 8501:127.0.0.1:8501 fantrax-vps
```

### 3) If you’re unsure whether you have SSH keys set up (Mac)
```bash
ls -la ~/.ssh
```

## Local Streamlit (Mac)

1. Run Streamlit locally.
```
cd /Users/hogan/FantraxAPI
PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501
```

2. If the port is in use, pick a new one and update the tunnel.
```
PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 12345
```

## Background Jobs (Cron)

### VPS cron (expected)
VPS should run SofaScore jobs and the conditional runner every 5 minutes.

Check current crontab:
```
ssh fantrax-vps
crontab -l
```

Expected lines (paths may vary):
```
*/5 * * * * cd "$ROOT_DIR" && mkdir -p "$LOG_DIR" && PYTHON_BIN="$PYTHON_BIN" bash bin/run_sofascore_watcher.sh --window-minutes 80 --min-window-minutes 70 --poll-interval-seconds 10 --max-watch-minutes 15 --browser-path "$CHROME_BIN" >> "$LOG_DIR/sofascore_kickoff_watcher.log" 2>&1
*/5 * * * * cd "$ROOT_DIR" && mkdir -p "$LOG_DIR" && PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" scripts/conditional_runner.py --all-users >> "$LOG_DIR/conditional_runner.cron.out" 2>&1
```

Install/update the repo cron file:
```
ssh fantrax-vps
cd /opt/FantraxAPI
crontab deploy/sofascore_crontab
crontab -l
```

### Local cron (optional)
To auto-run the conditional runner while your Mac is awake, add the same line to your local `crontab`.

## Conditional Runner (VPS)

Logs:
```
tail -n 80 /opt/FantraxAPI/data/logs/conditional_runner.log
tail -n 80 /opt/FantraxAPI/logs/conditional_runner.cron.out
```

If you run the runner manually as `hogan` and see `PermissionError` for `/opt/FantraxAPI/data/logs/*.log`,
fix directory ownership/permissions on the VPS:
```
sudo mkdir -p /opt/FantraxAPI/data/logs
sudo chown -R hogan:hogan /opt/FantraxAPI/data/logs
sudo chmod -R u+rwX /opt/FantraxAPI/data/logs
```

If you see `PermissionError` for `/opt/FantraxAPI/data/auth/.encryption_key` when running as `hogan`,
the file is owned by `root` (or too restrictive). Fix it from a root shell:
```
sudo chown hogan:hogan /opt/FantraxAPI/data/auth/.encryption_key
sudo chmod 600 /opt/FantraxAPI/data/auth/.encryption_key
```

Rules storage (where conditional swaps live):
- Global rules file: `/opt/FantraxAPI/data/conditional_rules.json`
- Per-user rules dir: `/opt/FantraxAPI/data/conditional_rules/<user_id>.json`

Quick checks:
```
ls -la /opt/FantraxAPI/data/conditional_rules.json
ls -la /opt/FantraxAPI/data/conditional_rules 2>/dev/null || true
```

If "auto lineup swaps" is enabled but no rules appear:
- Auto rules are generated by `scripts/conditional_runner.py` only when projections exist.
```
ls -la /opt/FantraxAPI/data/derived/projections.parquet 2>/dev/null || true
```

Compute `user_id` from email (matches the app’s UserManager logic):
```
python3 - <<'PY'
import hashlib
email = input("email: ").strip().lower()
print(hashlib.sha256(email.encode()).hexdigest()[:16])
PY
```

Forced dry-run (debug a single user):
```
PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python scripts/conditional_runner.py --user-id <USER_ID> --dry-run --force-trigger
```

## Syncthing

### Mac
Start Syncthing:
```
brew services start syncthing
```

Check local UI:
```
http://127.0.0.1:8384/
```

Open VPS Syncthing tunnel:
```
ssh -L 8385:127.0.0.1:8385 fantrax-vps
```

Check VPS UI via tunnel:
```
http://127.0.0.1:8385/
```

### VPS
Ensure service is running:
```
sudo systemctl start syncthing@hogan
sudo systemctl status syncthing@hogan
journalctl -u syncthing@hogan -n 40
```

## Cleanup Before Commit

Clean temporary SofaScore artifacts:
```
cd /Users/hogan/FantraxAPI
git clean -fd data/sofascore/lineups data/sofascore/schedules
git clean -fd data/sofascore/*.lock
```

## Troubleshooting

### SSH alias fails
1. Confirm alias in `~/.ssh/config`.
2. Inspect resolved host:
```
ssh -G fantrax-vps
```
3. Use IP directly if needed:
```
ssh 5.78.118.108
```

### Streamlit Tunnel Fails (Connection Refused / Browser Reset)

Symptoms:
- SSH prints: `channel X: open failed: connect failed: Connection refused`
- Browser shows: `This site can’t be reached` / `The connection was reset`

Meaning:
- The SSH tunnel is fine, but **nothing is listening on the VPS** at `127.0.0.1:8501` (Streamlit is stopped, crashed, or running on a different port).

Step-by-step on the VPS:

1. Verify something is listening on port 8501:
```
# If ripgrep (rg) is installed:
# sudo ss -ltnp | rg ':8501' || true
#
# Without rg (default on many VPS images):
sudo ss -ltnp | grep -E ':8501\\b' || true
sudo ss -ltnp 'sport = :8501' || true
```

2. Check the Streamlit/systemd unit:
```
sudo systemctl status fantrax-pull-restart.service --no-pager
sudo journalctl -u fantrax-pull-restart.service -n 200 --no-pager
```

3. Check the app log file (if present):
```
tail -n 200 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true
```

4. Restart the service, then re-check the port:
```
sudo systemctl restart fantrax-pull-restart.service
sudo ss -ltnp | grep -E ':8501\\b' || true
sudo ss -ltnp 'sport = :8501' || true
```

5. Re-open the tunnel from your Mac (keep it running in a terminal):
```
ssh -L 8501:127.0.0.1:8501 fantrax-vps
```
Then open: `http://127.0.0.1:8501`

If port 8501 is still not listening:
- Streamlit likely failed to start; the reason will be in `journalctl` or `streamlit.out`.
- Confirm the service is configured to bind to `127.0.0.1`/port `8501`, or adjust the tunnel to match the actual port.

### VPS cannot reach GitHub
```
ssh fantrax-vps
ping -c 3 github.com
curl -I https://github.com/
```

If DNS fails:
```
sudo resolvectl dns eth0 1.1.1.1 1.0.0.1
sudo resolvectl flush-caches
```
