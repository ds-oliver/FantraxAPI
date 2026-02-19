# App Infrastructure & Operations

This document captures how the Streamlit front end, runner scripts, data feeds, and synchronization actors are wired together so the local working tree stays aligned with the production VPS deployment. It also includes exact terminal commands (local macOS zsh vs VPS shell) and what to expect in output.

## 1. Repo & Deployment Layout

- **Local repo** (`/Users/hogan/FantraxAPI`): working branch `testing` (tracked on GitHub `ds-oliver/FantraxAPI`). Contains:
  - `apps/auth_login/...`: Streamlit UI pages.
  - `scripts/`: cron/CLI helpers (`conditional_runner.py`, `sofascore_lineup_listener.py`, etc.).
  - `services/`: wrappers around Fantrax/ESD.
  - `data/`: synced SofaScore/conditional rule assets and derived catalogs.
  - `bin/pull_restart.sh` & `deploy/systemd/fantrax-pull-restart.service`: helper + systemd unit for auto-pulling and restarting the app on the VPS.

- **VPS clone** (`/opt/FantraxAPI`): a git clone of the same branch. Runs Streamlit via the `pull_restart.sh` workflow and hosts Syncthing + cron jobs for background data collection.

## 2. Streamlit App Updates (Cheatsheet)

1. **Local changes** are developed/committed in `apps/` + supporting code (macOS zsh).
2. Push to remote (macOS zsh):
   ```bash
   cd /Users/hogan/FantraxAPI
   git status -sb
   git add -A
   git commit -m "your message"
   git push origin testing
   ```
   Expected: `testing -> testing` pushed without errors.
3. On the VPS, the `fantrax-pull-restart.service` (systemd) runs `/opt/FantraxAPI/bin/pull_restart.sh`, which:
   - fetches `origin/testing`, resets the clone, and reinstalls `requirements.txt`.
   - restarts Streamlit in the foreground (`.venv/bin/python -m streamlit run apps/auth_login/overview.py`).
   - logs to `logs/pull_restart.log` and `logs/streamlit.out`.
4. Manual/automated trigger (macOS zsh):
   ```bash
   ssh fantrax-vps sudo systemctl restart fantrax-pull-restart.service
   ```
   Expected: no output or brief systemd restart output. Check logs on VPS if needed.
   - Automated webhooks or CI can call that same `systemctl restart` command over SSH after every push.

## 3. Background Laminate: Runners & Sync

- **Conditional runner** (`scripts/conditional_runner.py`):
  - Orchestrates polling every `*/5 * * * *` (Cron) with `--all-users`.
  - Uses `user_manager` + `substitutions/lineup_resolver` to decide swaps.
  - Pulls Fantrax roster/lineup data via `fantraxapi` (FXPA) and SofaScore snapshots (ESD fallback).
  - Logs to `data/logs/conditional_runner.log`.

- **SofaScore data collection**:
  1. `sofascore_lineup_listener.py` (ESD) fetches schedules + lineups – saves JSON in `data/sofascore/lineups`.
  2. `sofascore_kickoff_watcher.py` (poll interval / 5s) keeps confirmed statuses.
  3. Confirmed snapshots flagged `confirmed=True` and archived as `predicted_*`.

- **Syncthing**:
  - Local device watches `/Users/hogan/FantraxAPI/data/sofascore`.
  - VPS device receives via Syncthing; folder named `sofascore`. Configuration uses Send-only (local) / Receive-only (VPS).
  - UI for troubleshooting lives behind `ssh -L 8385:127.0.0.1:8385 fantrax-vps`.

## 4. Data Flow Summary

1. **SofaScore/ESD data**: generated locally, synced via Syncthing. Updates land in VPS under `data/sofascore` (schedules, lineups, indexes).
2. **Fantrax data**: `auth_artifacts.json` and credentials stored locally; `scripts/conditional_runner.py` consumes this to build swaps; output rules written to `data/conditional_rules/<user>.json`.
3. **Streamlit UI**: reads `data/sofascore/*`, `data/conditional_rules/*.json`, `data/silver/sofascore*.csv` to display statuses, projections, and explanations.
4. **Logs**:
   - `logs/conditional_runner.log`: runner activity.
   - `logs/streamlit.out`: Streamlit runtime output (monitored by tail or service).
   - `logs/pull_restart.log`: git/venv steps from auto-restart script.
   - `data/logs/`: conditional_runner, conditional_swaps, auth, lineup bridge, etc.
5. **Conditional execution journal**:
   - Runner appends execution records to `data/conditional_rules_journal/<user_id>.jsonl`.
   - Journal is append-only and is the authoritative source for executed conditional swaps.

## Conditional State Authority

- Canonical writer environment is VPS.
- Controlled via:
  - `CONDITIONAL_STATE_ROLE=writer|reader`
  - `CONDITIONAL_WRITER_ENV=vps|local`
- Default behavior:
  - VPS defaults to `writer`.
  - Non-VPS environments default to `reader`.
- In `reader` mode:
  - Streamlit save/toggle/delete actions are disabled.
  - `scripts/conditional_runner.py` auto-forces dry-run behavior.
- Rule files now include metadata:
  - `meta.revision`, `meta.writer_env`, `meta.updated_at`.

**Log trimming:** To prevent logs from consuming too much space, run `scripts/trim_logs.py` on a schedule (e.g. weekly). It trims any `.log` or `.out` in `logs/` and `data/logs/` that exceed a size threshold to the last N lines. Example cron (Sunday 3am): `0 3 * * 0 cd /opt/FantraxAPI && .venv/bin/python scripts/trim_logs.py`. Options: `--max-size-mb 5` (default), `--keep-lines 50000`, `--dry-run`. The conditional runner also uses rotating file handlers so its log is capped at 2 MB per file with 5 backups.

## 5. Keeping App + Data Aligned (Cheatsheet)

- **Code push ➜ Streamlit**:
  1. Push branch (macOS zsh):
     ```bash
     cd /Users/hogan/FantraxAPI
     git push origin testing
     ```
     Expected: push completes without errors.
  2. Trigger systemd restart (macOS zsh):
     ```bash
     ssh fantrax-vps sudo systemctl restart fantrax-pull-restart.service
     ```
     Expected: silent success; failures show in `systemctl status`.
  3. Check logs (VPS):
     ```bash
     sudo systemctl status fantrax-pull-restart.service
     tail -n 40 /opt/FantraxAPI/logs/pull_restart.log
     tail -n 40 /opt/FantraxAPI/logs/streamlit.out
     ```
     Expected:
     - `pull_restart.log` shows fetch/reset + “Restarting Streamlit”
     - `streamlit.out` shows “You can now view your Streamlit app…” and stays running.

- **Data sync**:
  - Syncthing ensures `data/sofascore` is mirrored between Mac and VPS. Resync conflicts produce `.sync-conflict-*` files.
  - Conditional state is synced explicitly (not implicitly) with:
    ```bash
    python scripts/sync_conditional_state.py --pull-from-vps
    ```
    Emergency reverse sync:
    ```bash
    python scripts/sync_conditional_state.py --push-to-vps
    ```
    Paths synced by this script:
    - `data/conditional_rules/`
    - `data/conditional_rules_journal/`
  - UI tunnel (macOS zsh):
    ```bash
    ssh -L 8385:127.0.0.1:8385 fantrax-vps
    ```
    Expected: open `http://127.0.0.1:8385/` in browser while tunnel is open.
  - Clean conflict files before committing (macOS zsh):
    ```bash
    cd /Users/hogan/FantraxAPI
    git clean -fd data/sofascore/lineups data/sofascore/schedules
    ```
    Expected: conflict files removed; no tracked files deleted.

- **Service lifecycle**:
  - Systemd unit is `Type=simple`, restarts on failure, and always runs the latest Streamlit process in the foreground.
  - Logs (`journalctl -u fantrax-pull-restart.service`, `logs/streamlit.out`) are used for debugging.

## 6. Additional Helpers (Cheatsheet)

- **Run all cron-style scripts** (SofaScore listener, kickoff watcher, conditional runner) in one go:
  ```bash
  cd /path/to/FantraxAPI
  PYTHON_BIN=.venv/bin/python bin/run_all_cron_scripts.sh
  ```
  Or run each manually:
  - `bin/run_sofascore_listener.sh --mode both` (or `--mode predictions` / `--mode confirmed`)
  - `bin/run_sofascore_watcher.sh`
  - `python scripts/conditional_runner.py --all-users` (or `--user-id <id>` / `--league-id X --team-id Y`)
  - `python scripts/trim_logs.py` (optional; trim oversized logs)

- `.gitignore` excludes runtime artifacts (`data/sofascore/*.lock`, log files).
- `bin/pull_restart.sh` can take `BRANCH` env var for ad-hoc deployments (VPS):
  ```bash
  BRANCH=testing /opt/FantraxAPI/bin/pull_restart.sh
  ```
  Expected: pulls branch, installs deps, starts Streamlit.
- `ssh` config on Mac (`~/.ssh/config`) for convenience:
  ```
  Host fantrax-vps
    HostName 5.78.118.108
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
  ```

With this stack, code updates flow via git, data arrives via Syncthing/ESD, and `pull_restart.sh` + systemd keep the VPS Streamlit app in sync with the repository. Backups are preserved in `data/sofascore/lineups/archive` for historical analysis. Review the logs when failures occur and re-trigger the systemd service to reload the latest codebase after pushing changes.

## 7. Quick Troubleshooting (Cheatsheet)

- **Connect to VPS (macOS zsh)**:
  ```bash
  ssh fantrax-vps
  ```
  Expected: VPS login banner and shell prompt.

- **Fix unresolved `fantrax-vps` host**:
  - If `ssh fantrax-vps` fails with `Could not resolve hostname`, check that the alias exists in `~/.ssh/config`, run `ssh -G fantrax-vps` to inspect the resolved HostName, and fall back to the numeric IP (`5.78.118.108`) or a temporary `/etc/hosts` entry until DNS or the alias is restored.
  - To add the alias locally (macOS) so SSH always works even when DNS hiccups occur:
    ```bash
    echo "5.78.118.108 fantrax-vps" | sudo tee -a /etc/hosts
    ```
    Afterwards `ssh fantrax-vps` should resolve immediately, and you can remove the line once DNS is stable by editing `/etc/hosts`.

- **Start Streamlit manually (VPS)**:
  ```bash
  cd /opt/FantraxAPI
  PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python -m streamlit run apps/auth_login/overview.py --server.address 127.0.0.1 --server.port 8501
  ```
  Expected: “You can now view your Streamlit app…” and process stays running.

- **Tunnel Streamlit UI (macOS zsh)**:
  ```bash
  ssh -L 8501:127.0.0.1:8501 fantrax-vps
  ```
  Expected: open `http://127.0.0.1:8501` in browser.

- **Confirm conditional runner activity (VPS)**:
  ```bash
  tail -n 80 /opt/FantraxAPI/data/logs/conditional_runner.log
  ```
  Expected: “Generated … auto lineup swap rules” and “Rule … executed/skipped” lines.

- **Force a dry run (VPS)**:
  ```bash
  PYTHONPATH=/opt/FantraxAPI /opt/FantraxAPI/.venv/bin/python scripts/conditional_runner.py --user-id <USER_ID> --dry-run --force-trigger
  ```
  Expected: “DRY RUN ok: would swap …” lines.
