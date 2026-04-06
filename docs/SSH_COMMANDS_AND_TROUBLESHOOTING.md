# SSH Commands & Troubleshooting

Purpose: a concise, repeatable guide for maintaining the repo, the VPS app, and the background jobs.

## Quick resume: open the VPS app at http://127.0.0.1:8511

Port **8511** on your Mac is **not** where you run Streamlit locally. It is the **local end of an SSH tunnel** to the VPS, where Streamlit binds **127.0.0.1:8501**. Nothing listens on 8511 until the tunnel is up.

**Every time you come back to development:**

1. Open a terminal on your Mac and start the tunnel; **leave this window open** (it will look idle; that is normal):
   ```bash
   ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
   ```
2. In the browser, go to [http://127.0.0.1:8511/](http://127.0.0.1:8511/).

### Tunnel shows `channel X: open failed: connect failed: Connection refused`

That message is **normal to see once** if you opened the URL before Streamlit was up; it is **the main symptom** when the tunnel is working but **nothing on the VPS is listening on `127.0.0.1:8501`**. SSH is forwarding your browser’s traffic to the VPS; the refusal comes from the VPS because Streamlit is stopped, crashed, or bound to another port.

**Fix (from your Mac, does not require a separate VPS login):**

```bash
ssh fantrax-vps-root "systemctl restart fantrax-pull-restart.service && sleep 2 && ss -ltnp | grep -E ':8501\\b' || true; tail -n 60 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true"
```

You want `ss` to show something listening on `8501`. Then reload [http://127.0.0.1:8511/](http://127.0.0.1:8511/) (the tunnel terminal can stay open).

If `8501` still has no listener, use the full checklist: [Streamlit tunnel fails (connection refused / browser reset)](#streamlit-tunnel-refused).

If the tunnel command itself fails to start (not the repeating `channel` lines), or the browser says the site cannot be reached before you ever get `Connection refused`, see [VPS Quick Start (After Shutdown / Reconnect)](#vps-quick-start-after-shutdown--reconnect).

**Running the app from your local clone** (different URL): use port **8502** on localhost, not 8511. See [Port Policy](#port-policy-use-this-everywhere) and [Three-Terminal Workflow](#three-terminal-workflow-local--vps-side-by-side).

## Conditional Swaps Deprecations

- Sidebar controls `Late KOS coverage` and `Do not move unless confirmed out` are deprecated.
- Stored `users.json` values (`late_kos_policy`, `do_not_move`) may still exist but are ignored at runtime.

## Port Policy (Use This Everywhere)

- Local Streamlit on Mac: `127.0.0.1:8502`
- VPS Streamlit on VPS host: `127.0.0.1:8501`
- VPS Streamlit on Mac via SSH tunnel: `127.0.0.1:8511` (`-L 8511:127.0.0.1:8501`)

Do not run local Streamlit on `8501` when using the VPS tunnel workflow.

## Terminal Count (How Many Windows You Need)

- `1 terminal`: Any one-off VPS maintenance command (pull/restart/check logs).
- `2 terminals`: VPS Streamlit access from Mac via SSH tunnel.
  - Terminal A: SSH tunnel command, keep it running.
  - Terminal B: Optional extra SSH session for VPS commands while tunnel stays open.
- `3 terminals` (optional): If you also want local app/dev commands running at the same time.

Important shell context rule:
- `ssh fantrax-vps-root` is a **Mac SSH alias**. Run it from your Mac terminal only.
- Do **not** run `ssh fantrax-vps-root` from inside `root@fantrax-sofascore`; it will fail with hostname resolution errors.

## Three-Terminal Workflow (Local + VPS Side-by-Side)

Use this when you want to test both apps at the same time.

- Mac local app: `http://127.0.0.1:8502`
- VPS app via tunnel: `http://127.0.0.1:8511`
- Why these ports: local and tunneled VPS access are always separate on your Mac.

### Terminal 1 (Mac local app)
```bash
cd /Users/hogan/FantraxAPI
PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8502
```
Keep this running.

### Terminal 2 (VPS admin shell)
```bash
ssh fantrax-vps-root
systemctl restart fantrax-pull-restart.service
systemctl status fantrax-pull-restart.service --no-pager
ss -ltnp | grep -E ':8501\\b' || true
tail -n 80 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true
```
Use this terminal for VPS checks/restarts.

### Terminal 3 (Mac SSH tunnel to VPS app)
```bash
ssh -N -L 8511:127.0.0.1:8501 fantrax-vps-root
```
Keep this running.

### Browser URLs
- Local app (Terminal 1): `http://127.0.0.1:8502`
- VPS app (Terminal 3 tunnel): `http://127.0.0.1:8511`

Rule:
- If Terminal 3 closes, VPS URL stops working until tunnel is restarted.
- If Terminal 1 closes, local URL stops working until local Streamlit is restarted.

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

2. Preferred: push + deploy in one command (auto-commit, push, VPS restart/pull).
```
bin/push_and_deploy_vps.sh -m "describe your change"
```

3. Manual fallback: push changes to `testing`.
```
git add -A
git commit -m "describe your change"
git push origin testing
```

4. Manual fallback: pull + restart on VPS.
```
ssh fantrax-vps-root "systemctl restart fantrax-pull-restart.service && systemctl status fantrax-pull-restart.service --no-pager -l"
```

5. Verify Streamlit on VPS.
```
ssh fantrax-vps-root "ss -ltnp | grep -E ':8501\\b' || true; tail -n 40 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true"
```

6. (Optional) Tunnel VPS Streamlit to your Mac.
```
ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
```

## Restart Streamlit on VPS (Load Latest Changes)

Use this when you already pushed commits and want the VPS app to pick them up immediately.

1. Pull + restart in one command (recommended):
```bash
ssh fantrax-vps-root "cd /opt/FantraxAPI && git pull origin testing && systemctl restart fantrax-pull-restart.service && sleep 2 && systemctl status fantrax-pull-restart.service --no-pager -l"
```

2. Verify the app is listening:
```bash
ssh fantrax-vps-root "ss -ltnp | grep -E ':8501\\b' || true"
```

3. Verify logs show new run (and no startup errors):
```bash
ssh fantrax-vps-root "tail -n 120 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true; journalctl -u fantrax-pull-restart.service -n 120 --no-pager"
```

### Deploy Helper Script Notes

- Script path: `bin/push_and_deploy_vps.sh`
- Defaults:
  - SSH host: `fantrax-vps-root`
  - Service: `fantrax-pull-restart.service`
- Useful options:
```
bin/push_and_deploy_vps.sh --no-commit
bin/push_and_deploy_vps.sh -H fantrax-vps-root -s fantrax-pull-restart.service
bin/push_and_deploy_vps.sh --skip-status
```

### VPS deploy: `pull_restart` / git check fails (was “not a git repository”)

Symptoms:

- `logs/pull_restart.log` or `journalctl` shows a git-related error right after `Starting pull_restart`.
- `bin/push_and_deploy_vps.sh` may exit non-zero and print journal output (the service fails before Streamlit starts).
- Browser: tunnel to `8511` shows connection refused or “site can’t be reached,” because **nothing listens on `127.0.0.1:8501`** on the VPS.

#### What caused this, and is it because the tree is owned by `hogan` instead of `root`?

**Most likely:** nothing is “corrupt” in your app. **Git 2.35+** refuses to run commands in a repo when the **process user** (here **root**, via `fantrax-pull-restart.service`) does not match the **directory owner** (e.g. **`hogan:hogan`**). That is a **security rule**, not a mistake you made by using `hogan`.

**Why it feels brand new:** the rule has been in Git for a while; you only see it when **root** runs `git` in that tree **and** ownership differs. If you used to run deploys or Streamlit as `hogan`, or had not restarted this service path, you would not have hit it. A **Ubuntu / `git` package update** can also make behavior stricter. Your logs also show a moment when full `git fetch` / `pip` runs stopped and only the first-line check failed — that pattern matches **dubious ownership** (or missing `HOME` for systemd), not a missing `.git` folder.

**Do you need to “undo” `hogan` ownership?** Usually **no**. Pick one approach:

| Approach | Idea |
|----------|------|
| **Recommended** | Keep **`hogan`** (or whatever user owns `/opt/FantraxAPI`) and make **root’s Git** trust the path: `git config --global --add safe.directory /opt/FantraxAPI` **and** ensure the service sees **`HOME=/root`** (see `deploy/systemd/fantrax-pull-restart.service` and `bin/pull_restart.sh` in this repo). |
| **Alternative** | `chown -R root:root /opt/FantraxAPI` so root owns the tree — dubious ownership goes away, but **you may not want** root-owned files if `hogan` routinely edits there without `sudo`. |
| **Heavier** | Run the systemd unit as **`User=hogan`** so Git runs as the same user as the files (requires path/venv permissions to be consistent). |

#### Next steps on the VPS (do in order)

1. **SSH as root** (`ssh fantrax-vps-root`).
2. **One-time Git trust** (safe if the path is your real repo):
   ```bash
   git config --global --add safe.directory /opt/FantraxAPI
   ```
3. **Ship the repo fixes** so systemd sets `HOME` and the script exports it: deploy **`bin/pull_restart.sh`** and **`deploy/systemd/fantrax-pull-restart.service`** to the server (e.g. `git pull` in `/opt/FantraxAPI` after pushing from your Mac, or copy the two files by hand).
4. **Install the updated unit file** (if you edited it):
   ```bash
   cp /opt/FantraxAPI/deploy/systemd/fantrax-pull-restart.service /etc/systemd/system/
   systemctl daemon-reload
   ```
5. **Clear the failed state** (needed after many restart loops):
   ```bash
   systemctl reset-failed fantrax-pull-restart.service
   ```
6. **Start the app** and verify:
   ```bash
   systemctl restart fantrax-pull-restart.service
   sleep 3
   systemctl status fantrax-pull-restart.service --no-pager -l
   ss -ltnp | grep -E ':8501\b' || true
   tail -n 40 /opt/FantraxAPI/logs/pull_restart.log
   ```
   You want log lines past **`Starting pull_restart`** such as **`Fetching origin`**, **`Installing requirements`**, **`Launching Streamlit`**, and **`ss`** showing a listener on **8501**.
7. **On your Mac:** start the tunnel, then open `http://127.0.0.1:8511/`:
   ```bash
   ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
   ```

If step 6 still fails, read the **full** message from `git` (updated `pull_restart.sh` logs it) and `journalctl -u fantrax-pull-restart.service -n 80 --no-pager`.

#### A) `dubious ownership` (common when `.git` exists but is owned by another user)

`fantrax-pull-restart.service` runs as **root**, but **`/opt/FantraxAPI` is often owned by a normal user** (e.g. `hogan:hogan`). Git 2.35+ treats that as unsafe and refuses to run; the old script text only said “not a git repository” because stderr was hidden.

**Check as root:**

```bash
cd /opt/FantraxAPI && git rev-parse --is-inside-work-tree
```

If you see `fatal: detected dubious ownership in repository at '/opt/FantraxAPI'`, fix with:

```bash
git config --global --add safe.directory /opt/FantraxAPI
```

Then reload the unit if you updated it from the repo (`Environment=HOME=/root`), run **`systemctl reset-failed fantrax-pull-restart.service`** (required after bursts of failures — see **“Start request repeated too quickly”** in `journalctl`), then `systemctl restart fantrax-pull-restart.service` and confirm `ss -ltnp | grep 8501`.

**Why interactive `git rev-parse` worked but the service still logged “not a git repository”:** an interactive root login has `HOME=/root`, so Git reads `/root/.gitconfig`. **systemd often starts services with `HOME` unset**, so Git ignored your `safe.directory` until `HOME` is set (now done in `deploy/systemd/fantrax-pull-restart.service` and `bin/pull_restart.sh`).

#### B) Missing or broken `.git`

Meaning: **`/opt/FantraxAPI` is not a git working tree** (missing or broken `.git`). `bin/pull_restart.sh` runs `git fetch` / `git reset` first; if that check fails, it exits and **never launches Streamlit**.

**Confirm:**

```bash
ssh fantrax-vps-root "test -d /opt/FantraxAPI/.git && echo OK || echo MISSING_DOT_GIT"
```

**Restore a proper clone** (use your real `origin` URL if different, e.g. same as `git remote get-url origin` on your Mac):
   ```bash
   ssh fantrax-vps-root
   cd /opt
   mv FantraxAPI "FantraxAPI.bak.$(date +%s)"
   git clone https://github.com/ds-oliver/FantraxAPI.git FantraxAPI
   cd FantraxAPI && git checkout testing
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
   Copy back any **secrets or local-only files** from the backup (e.g. under `data/`, env files) if you rely on them, then:
   ```bash
   systemctl restart fantrax-pull-restart.service
   ss -ltnp | grep -E ':8501\b' || true
   ```

#### C) In-place repair (only if you must keep the current tree and only restore git metadata)

From `/opt/FantraxAPI`, `git init`, add `origin`, `git fetch`, and check out `testing` to match GitHub (resolve conflicts carefully; `git checkout -f` overwrites tracked files).

## VPS Quick Start (After Shutdown / Reconnect)

Use this exact sequence when your machine/processes were interrupted and you need everything back up quickly.
Terminal requirement: `2 terminals` (1 for admin commands, 1 for tunnel).

1. From your Mac, connect to VPS as root (admin shell).
```bash
ssh fantrax-vps-root
```

2. On your Mac, run this exact one-liner (recommended: avoids missing steps in interactive shells).
```bash
ssh fantrax-vps-root "systemctl restart fantrax-pull-restart.service && sleep 2 && systemctl status fantrax-pull-restart.service --no-pager -l && ss -ltnp | grep -E ':8501\\b' || true"
```

3. Alternative interactive sequence (if you prefer staying in a VPS shell).
```bash
systemctl restart fantrax-pull-restart.service
sleep 2
systemctl status fantrax-pull-restart.service --no-pager
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
ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
```

6. Open the app in your Mac browser.
```text
http://127.0.0.1:8511
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
ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
```

4. Open browser:
```text
http://127.0.0.1:8511
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
ssh -L 8511:127.0.0.1:8501 hogan@5.78.118.108
```
Then open: `http://127.0.0.1:8511`

### 1b) From your Mac: tunnel as `root` (if root SSH key auth is configured)
```bash
ssh -L 8511:127.0.0.1:8501 fantrax-vps-root
```
Then open: `http://127.0.0.1:8511`

### 2) If you want it even simpler (no typing IP/user each time): add an SSH alias on your Mac
Add to `~/.ssh/config`:
```sshconfig
Host fantrax-vps
  HostName 5.78.118.108
  User hogan
```

Then you can run:
```bash
ssh -L 8511:127.0.0.1:8501 fantrax-vps
```

### 3) If you’re unsure whether you have SSH keys set up (Mac)
```bash
ls -la ~/.ssh
```

## Local Streamlit (Mac)

1. Run Streamlit locally on `8502`.
```
cd /Users/hogan/FantraxAPI
PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8502
```

2. If the port is in use, pick a new local port.
```
PYTHONPATH=/Users/hogan/FantraxAPI streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 12345
```

## Background Jobs (Cron)

### VPS cron (expected)
VPS should run the **daily** SofaScore predictions job (schedule + predicted lineups) and the **every-5-minute** kickoff watcher and conditional runner. The canonical definitions are in `deploy/sofascore_crontab` (predictions at **02:02 UTC** so it does not run in the same minute as the `*/5` jobs at :00 and lose the shared lineup lock).

Check current crontab:
```
ssh fantrax-vps
crontab -l
```

Expected lines (paths may vary; prefer installing from the repo file):
```
2 2 * * * cd "$ROOT_DIR" && mkdir -p "$LOG_DIR" && PYTHON_BIN="$PYTHON_BIN" bash bin/run_sofascore_listener.sh --mode predictions --horizon-days 7 --with-mappings --browser-path "$CHROME_BIN" >> "$LOG_DIR/sofascore_predictions.log" 2>&1
*/5 * * * * cd "$ROOT_DIR" && mkdir -p "$LOG_DIR" && PYTHON_BIN="$PYTHON_BIN" bash bin/run_sofascore_watcher.sh --window-minutes 80 --min-window-minutes 70 --poll-interval-seconds 10 --max-watch-minutes 15 --browser-path "$CHROME_BIN" >> "$LOG_DIR/sofascore_kickoff_watcher.log" 2>&1
*/5 * * * * cd "$ROOT_DIR" && mkdir -p "$LOG_DIR" && PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" scripts/conditional_runner.py --mode coordinator --all-users --max-workers 4 >> "$LOG_DIR/conditional_runner.cron.out" 2>&1
```

Daily predictions log (last run / errors):
```
tail -n 80 /opt/FantraxAPI/logs/sofascore_predictions.log
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
tail -n 80 /opt/FantraxAPI/data/logs/conditional_runner_runs.jsonl
tail -n 120 /opt/FantraxAPI/data/logs/conditional_runner_actions.jsonl
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
- Execution journal dir: `/opt/FantraxAPI/data/conditional_rules_journal/<user_id>.jsonl`
- Coordinator run journal: `/opt/FantraxAPI/data/logs/conditional_runner_runs.jsonl`
- Action journal: `/opt/FantraxAPI/data/logs/conditional_runner_actions.jsonl`

Canonical state controls:
- `CONDITIONAL_STATE_ROLE=writer|reader`
- `CONDITIONAL_WRITER_ENV=vps|local`
- VPS should run as writer; local should run as reader unless explicitly doing maintenance.

Auto lineup fallback behavior:
- For auto-generated lineup rules (`source=auto_lineup_swaps`), if the active player is confirmed non-starter and no reserve is confirmed starter yet, runner falls back to unconfirmed reserves by ranking:
  1. KOS ordering
  2. `ProjGS`
  3. `ProjFPts`
- Manual rules remain strict to user-authored semantics and do not implicitly broaden to this fallback path.

Quick checks:
```
ls -la /opt/FantraxAPI/data/conditional_rules.json
ls -la /opt/FantraxAPI/data/conditional_rules 2>/dev/null || true
ls -la /opt/FantraxAPI/data/conditional_rules_journal 2>/dev/null || true
```

Backfill journal from existing fired rules:
```
PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python scripts/backfill_conditional_execution_journal.py
```

Explicit conditional-state sync (run from local repo):
```
python scripts/sync_conditional_state.py --pull-from-vps
```
Emergency reverse sync:
```
python scripts/sync_conditional_state.py --push-to-vps
```

If "auto lineup swaps" is enabled but no rules appear:
- Auto rules are generated by `scripts/conditional_runner.py` only when projections exist.
```
ls -la /opt/FantraxAPI/data/derived/projections.parquet 2>/dev/null || true
```

If projections keep showing `(Parquet cache)` and stale warnings:
- Confirm Streamlit runtime has Google Sheet credentials at the VPS path (this file is gitignored and must be copied manually):
```
ssh fantrax-vps-root "ls -la /opt/FantraxAPI/config/service_account.json"
```
- If missing, copy your local key to VPS:
```
scp /Users/hogan/FantraxAPI/config/service_account.json fantrax-vps-root:/opt/FantraxAPI/config/service_account.json
ssh fantrax-vps-root "chmod 600 /opt/FantraxAPI/config/service_account.json"
```
- Restart service after copying credentials:
```
ssh fantrax-vps-root "systemctl restart fantrax-pull-restart.service && sleep 2 && systemctl status fantrax-pull-restart.service --no-pager -l"
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

## Repo ownership on the VPS (`Permission denied` / `chown: Operation not permitted`)

If `/opt/FantraxAPI` was cloned or updated **as root**, files are `root:root` and your deploy user (e.g. `hogan`) **cannot** overwrite `players.csv`, run exports to the repo root, or `chown` anything away from root.

**Symptoms**

- `PermissionError` writing `players.csv`
- As non-root: `chown: ... Operation not permitted` on almost every path

**Fix (run as root, once)**

From your Mac:

```bash
ssh fantrax-vps-root
```

On the server as **root**:

```bash
chown -R hogan:hogan /opt/FantraxAPI
```

Verify as `hogan`:

```bash
ssh hogan@5.78.118.108   # or: ssh fantrax-vps
ls -la /opt/FantraxAPI/players.csv
```

**Rare:** if `chown` still fails even as root, check immutable flags: `lsattr -R /opt/FantraxAPI | head -20` and clear with `chattr -R -i -a /opt/FantraxAPI` (only if you see `i`/`a` bits).

**Note:** `systemd` / `pull_restart` may still create **new** files as root depending on unit configuration; if root-owned files reappear, set `User=` in the service unit to `hogan` or run deploy scripts as `hogan`.

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

<a id="streamlit-tunnel-refused"></a>

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
# ss -ltnp | rg ':8501' || true
#
# Without rg (default on many VPS images):
ss -ltnp | grep -E ':8501\\b' || true
ss -ltnp 'sport = :8501' || true
```

2. Check the Streamlit/systemd unit:
```
systemctl status fantrax-pull-restart.service --no-pager
journalctl -u fantrax-pull-restart.service -n 200 --no-pager
```

3. Check the app log file (if present):
```
tail -n 200 /opt/FantraxAPI/logs/streamlit.out 2>/dev/null || true
```

4. Restart the service, then re-check the port:
```
systemctl restart fantrax-pull-restart.service
sleep 2
ss -ltnp | grep -E ':8501\\b' || true
ss -ltnp 'sport = :8501' || true
```

5. Re-open the tunnel from your Mac (keep it running in a terminal):
```
ssh -N -o ExitOnForwardFailure=yes -L 8511:127.0.0.1:8501 fantrax-vps-root
```
Then open: `http://127.0.0.1:8511`

If tunnel startup says `Address already in use`:
- A local process already owns Mac port `8511` (often an existing SSH tunnel).
- Check owner: `lsof -nP -iTCP:8511 -sTCP:LISTEN`
- Either stop the existing tunnel/process, or tunnel to another local port:
```
ssh -N -o ExitOnForwardFailure=yes -L 8521:127.0.0.1:8501 fantrax-vps-root
```
Then open: `http://127.0.0.1:8521`

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
