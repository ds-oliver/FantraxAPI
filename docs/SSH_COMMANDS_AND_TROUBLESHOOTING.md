# SSH Commands & Troubleshooting

Purpose: a concise, repeatable guide for maintaining the repo, the VPS app, and the background jobs.

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
