# Cron Jobs and App Landscape

This document lists each cron job used by the FantraxAPI app, what it does, and how it fits into the overall system.

## App landscape (where cron fits)

- **Streamlit UI** (`apps/auth_login/overview.py`): Runs under systemd (or manually). Users view rosters, conditional swap rules, trade opportunities, and lineup status. The UI reads data produced by cron jobs and by Fantrax/API calls.
- **SofaScore data**: Schedules and lineups are fetched by cron (listener + kickoff watcher), written to `data/sofascore/schedules/` and `data/sofascore/lineups/`. The UI and the conditional runner both depend on this data. All current lineup/schedule scraping is **SofaScore only**; FotMob is not implemented (see `IDEAS.md` for possible future sources).
- **Conditional swaps**: Rules are defined in the UI and stored in `data/conditional_rules.json` (or per-user under `data/conditional_rules/<user_id>.json`). The **conditional runner** cron job evaluates those rules and executes swaps on Fantrax when conditions (e.g. confirmed lineup) are met.
- **Logs**: Cron jobs write to `logs/` and `data/logs/`. A **log trim** cron job can run weekly to keep log files from growing without bound.

Cron jobs do not run the Streamlit app; they only refresh data and run the headless conditional runner.

### Where the SofaScore scraping lives

The **schedule and lineup scraping** is done by:

| What                | Where it lives | In crontab? |
|---------------------|----------------|-------------|
| Predictions + schedule | `bin/run_sofascore_listener.sh` → `scripts/sofascore_lineup_listener.py` (uses `services/sofascore_lineup_service.py`) | Yes – `deploy/sofascore_crontab` (daily 02:02 UTC) |
| Confirmed lineups (pre-kickoff) | `bin/run_sofascore_watcher.sh` → `scripts/sofascore_kickoff_watcher.py` (same service) | Yes – `deploy/sofascore_crontab` (every 5 min) |
| Alternative: schedule + lineups in one script | `esd_export_schedule_and_lineups_v2.py` (repo root) | No – optional cron; see section 5 and `docs/ESD_LINEUP_FETCHER.md` |

Both paths use **EasySoccerData (ESD)** and raw SofaScore HTTP as fallback. There is no FotMob scraper in this repo; FotMob is mentioned only as a future option in `IDEAS.md`.

---

## Crontab reference

A sample crontab lives in `deploy/sofascore_crontab`. It sets `ROOT_DIR`, `LOG_DIR`, and `PYTHON_BIN` and runs the SofaScore jobs. The conditional runner and log trim are typically added on the same machine (same crontab or a separate one) if you use them.

| Schedule      | Job                  | Purpose |
|---------------|----------------------|--------|
| `2 2 * * *`   | SofaScore predictions | Daily refresh of schedule + predicted lineups (and mappings). At **02:02 UTC** (not :00) to avoid racing the `*/5` kickoff watcher for `data/sofascore/.sofascore_lineup.lock`. |
| `*/5 * * * *` | SofaScore kickoff watcher | Every 5 minutes: poll fixtures 70–80 min before kickoff; capture confirmed lineups. |
| `*/5 * * * *` | Conditional runner   | Every 5 minutes: evaluate swap rules for all users and execute on Fantrax. |
| `0 3 * * 0`   | Log trim (optional)   | Weekly: trim oversized logs in `logs/` and `data/logs/`. |
| `0 */2 * * 6,0` | ESD export (optional) | Every 2 hours on Sat/Sun: schedule + lineups via root script (alternative to listener). |

---

## 1. SofaScore predictions (daily)

| Field   | Value |
|--------|--------|
| **Schedule** | `2 2 * * *` (daily at **02:02 UTC**) |
| **Script**   | `bin/run_sofascore_listener.sh` |
| **Args**     | `--mode predictions --horizon-days 7 --with-mappings --browser-path "$CHROME_BIN"` (see `deploy/sofascore_crontab`) |
| **Log**      | `logs/sofascore_predictions.log` |

**What it does:** Runs the SofaScore lineup listener in “predictions” mode. Fetches the upcoming schedule (default: Premier League, tournament 17) and predicted lineups for matches within the next 7 days. `--with-mappings` updates player-mapping data. Writes:

- `data/sofascore/schedules/<tournament>_<season>_upcoming.csv` (and related schedule CSVs)
- `data/sofascore/lineups/<event_id>.json` for predicted lineups

**Role in the app:** Keeps schedule and predicted lineups up to date so the UI and the conditional runner know which fixtures exist and what lineups to expect. The kickoff watcher uses the same schedule cache.

**Defined in:** `deploy/sofascore_crontab`

---

## 2. SofaScore kickoff watcher (every 5 minutes)

| Field   | Value |
|--------|--------|
| **Schedule** | `*/5 * * * *` (every 5 minutes) |
| **Script**   | `bin/run_sofascore_watcher.sh` |
| **Args**     | `--window-minutes 80 --min-window-minutes 70 --poll-interval-seconds 10 --max-watch-minutes 15` |
| **Log**      | `logs/sofascore_kickoff_watcher.log` |

**What it does:** For each run, reads the cached upcoming schedule. If any fixtures are in the “pre-kickoff window” (70–80 minutes before kickoff), the script enters a tight loop (10s polling) for up to 15 minutes to capture **confirmed** lineups from SofaScore. Writes updated lineup JSON with `confirmed=True` and archives previous predicted snapshots under `data/sofascore/lineups/archive/`.

**Role in the app:** Confirmed lineups drive the conditional runner’s “confirmed_lineup” trigger: once a lineup is confirmed, the runner can apply swap rules. The UI also uses these lineups for status and explanations.

**Defined in:** `deploy/sofascore_crontab`

---

## 3. Conditional runner (every 5 minutes)

| Field   | Value |
|--------|--------|
| **Schedule** | `*/5 * * * *` (every 5 minutes) |
| **Script**   | `scripts/conditional_runner.py` (run with repo as cwd; use `PYTHONPATH` or `bin/run_all_cron_scripts.sh` for env) |
| **Args**     | `--mode coordinator --all-users --max-workers 4` |
| **Log**      | `data/logs/conditional_runner.log` (rotating, 2 MB × 5 backups) |

**What it does:** Loads conditional swap rules from `data/conditional_rules.json` and per-user rules under `data/conditional_rules/<user_id>.json`. For each user with stored auth (cookies in `data/auth/` or shared auth artifacts), it:

- Resolves lineup info (Fantrax roster + SofaScore schedule/lineups)
- Evaluates rules (e.g. “when lineup is confirmed and active player is not starting, swap in bench player”)
- Calls Fantrax SubsService to execute eligible swaps and marks rules as fired

The runner also writes persistent JSONL diagnostics to `data/logs/conditional_runner_runs.jsonl` and `data/logs/conditional_runner_actions.jsonl`.

**Role in the app:** This is the automation that actually performs subs on Fantrax. The UI is where users create and edit rules; cron runs the runner so those rules are applied on a schedule without the app being open.

**Defined in:** `deploy/sofascore_crontab` (recommended on VPS). If you are installing your own crontab manually, the line looks like:

```cron
*/5 * * * * cd "$ROOT_DIR" && PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" scripts/conditional_runner.py --mode coordinator --all-users --max-workers 4 >> "$LOG_DIR/conditional_runner.cron.out" 2>&1
```

(Or use `bin/run_all_cron_scripts.sh` for a single combined run; the runner logs to `data/logs/conditional_runner.log` itself.)

---

## 4. Log trim (weekly, optional)

| Field   | Value |
|--------|--------|
| **Schedule** | `0 3 * * 0` (Sunday 03:00) |
| **Script**   | `scripts/trim_logs.py` |
| **Args**     | (defaults: trim files &gt; 5 MB to last 50,000 lines) |
| **Log**      | None (script only trims other logs) |

**What it does:** Scans `logs/` and `data/logs/` for `.log` and `.out` files. Any file larger than the threshold (default 5 MB) is replaced with its last N lines (default 50,000). Uses `tail` so large files are not fully loaded into memory.

**Role in the app:** Prevents log directories from growing without bound. The conditional runner already uses rotating file handlers; this job trims logs that are written by other scripts (e.g. kickoff watcher, listener redirects) or by cron stdout/stderr redirects.

**Example cron line:**

```cron
0 3 * * 0 cd "$ROOT_DIR" && "$PYTHON_BIN" scripts/trim_logs.py
```

---

## 5. ESD schedule + lineup export (optional alternative)

| Field   | Value |
|--------|--------|
| **Schedule** | `0 */2 * * 6,0` (every 2 hours on Saturday and Sunday) – or any schedule you prefer |
| **Script**   | `esd_export_schedule_and_lineups_v2.py` (repo root) |
| **Args**     | `--tournament-id 17 --upcoming --with-lineups` |
| **Log**      | `logs/lineup_fetch.log` (if you redirect stdout/stderr) |

**What it does:** Fetches SofaScore schedule and lineup data using the same ESD + raw-HTTP approach as the listener. Writes the same layout: `data/sofascore/schedules/` and `data/sofascore/lineups/`. This script is a **standalone alternative** to the listener; it does not run the “confirmed” or kickoff-watcher flows.

**Role in the app:** If you prefer one script for “schedule + lineups” on a custom schedule (e.g. every 2 hours on match days), you can use this instead of or in addition to the listener. Documented in `docs/ESD_LINEUP_FETCHER.md` and `docs/LINEUP_INTELLIGENCE_INTEGRATION.md`.

**Not in `deploy/sofascore_crontab`.** Example cron line (from LINEUP_INTELLIGENCE_INTEGRATION.md):

```cron
0 */2 * * 6,0 cd "$ROOT_DIR" && "$PYTHON_BIN" esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups >> "$LOG_DIR/lineup_fetch.log" 2>&1
```

**FotMob:** There is no FotMob scraping job in this repo. FotMob is listed in `IDEAS.md` as a possible future data source; all current scraping is SofaScore-only.

---

## Installing the crontab

1. Copy the contents of `deploy/sofascore_crontab`.
2. Set `ROOT_DIR` and `LOG_DIR` (and optionally `PYTHON_BIN`) for your environment (e.g. `ROOT_DIR=/opt/FantraxAPI`, `LOG_DIR=/opt/FantraxAPI/logs`).
3. If you use conditional swaps, add a line for `conditional_runner.py --mode coordinator --all-users --max-workers 4` as in section 3.
4. Optionally add the log-trim line from section 4.
5. Install: `crontab -e` and paste the full crontab.

To run all cron-style scripts once by hand (listener, watcher, conditional runner), use:

```bash
cd /path/to/FantraxAPI
bin/run_all_cron_scripts.sh
```

See `docs/APP_INFRASTRUCTURE.md` for more on deployment and log locations.

---

## Other jobs in the same crontab (not part of this project)

You may have other cron jobs on the same machine (e.g. **WhoScored + FotMob scraper scripts**) that are **not** part of FantraxAPI. They can live in the **same crontab** as the jobs above; cron runs all lines in the installed crontab.

If those scrapers should run **every Monday at 2pm** (local time), use:

```cron
0 14 * * 1 /path/to/your/whoscored_fotmob_script_or_wrapper
```

- `0 14` = 14:00 (2pm)
- `* * 1` = every month, every day of month, day-of-week 1 = Monday

The file `deploy/sofascore_crontab` in this repo only contains FantraxAPI jobs; any WhoScored/FotMob entries would be added by you when you edit the crontab (e.g. `crontab -e`).
