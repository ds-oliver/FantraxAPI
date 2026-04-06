# SofaScore fetch: VPS vs local Mac

Use this to see why the **same repo** can work on your **Mac** but fail on a **Hetzner/VPS** (or vice versa).

## What this means for fixing the VPS

The doc is not “change one flag and SofaScore works on Hetzner.” It means:

1. **Make the VPS Python env complete** (same as local): `pip install -r requirements.txt` in `/opt/FantraxAPI/.venv`, then on Linux run **`playwright install chromium`**, and use a real Chrome/Chromium path (`CHROME_BIN=/usr/bin/chromium-browser` in `deploy/sofascore_crontab`). That fixes missing `esd` / `curl_cffi` / install-order failures — **not** IP blocks.

2. **If `api.sofascore.com` still returns 403 from the VPS**, that is **network reputation (datacenter IP)**. The reliable fixes are: **run the listener on your Mac** (or another residential IP) and **sync** `data/sofascore/schedules/` + `lineups/` to the server (Syncthing/rsync), **or** supply **`data/sofascore/cookies.json`** from a browser session that passed Cloudflare, **or** use a residential proxy (not built into this repo).

3. **Keep cron from stepping on itself:** `02:02` predictions + remove **stale** `.sofascore_lineup.lock` if the PID is dead.

So “fixing the VPS” = **(A)** parity of packages + browsers, **plus (B)** either accept **Mac-as-fetcher** for SofaScore JSON or invest in **cookies/proxy**.

### Manual mapping: “Rayan” (SofaScore 1464966) = Rayan Vitor Simplício Rocha (Bournemouth)

SofaScore lists this player as **“Rayan”**; that is **not** Rayan Ait-Nouri (Man City). **Rayan Ait-Nouri** stays on `sofascore_id: 931278` in `config/player_mappings.yaml`.

**Rayan Vitor** was not present in the repo’s `players.csv` export at the time of the fix — add him after you refresh the export from Fantrax (find his row, copy **`id`** from column `id`), then append to `config/player_mappings.yaml`:

```yaml
- fantrax_id: <paste Fantrax id from players.csv>
  fantrax_name: Rayan Vitor Simplício Rocha
  sofascore_id: 1464966
  sofascore_name: Rayan
  ffscout_name: null
  other_names:
  - Rayan Vitor Simplicio Rocha
  display_name: Rayan Vitor Simplício Rocha
```

Re-run mapping sync or `run_sofascore_listener.sh` with `--with-mappings`.

## What must match (Python)

| Dependency | Role |
|------------|------|
| `curl_cffi` | Raw `api.sofascore.com` HTTP with browser-like TLS (`services/sofascore_lineup_service.py`). |
| `playwright` + `playwright install chromium` | Required **before** pip can install EasySoccerData (`esd`) — its `setup.py` imports the package at install time. |
| `lxml` | Same — import chain during ESD install. |
| `esd` (EasySoccerData from Git) | `import esd` — browser-driven SofaScore client. |

Install order on a **fresh** venv:

```bash
pip install -r requirements.txt
playwright install chromium   # Linux/Mac: pulls browser binaries
```

**VPS:** `deploy/sofascore_crontab` sets `CHROME_BIN=/usr/bin/chromium-browser` (Ubuntu snap wrapper). Google Chrome at `/usr/bin/google-chrome` is often **missing** on servers.

**Mac:** use `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` or rely on Playwright’s Chromium after `playwright install chromium`.

## Why VPS returns 403 / “Access denied”

SofaScore (and Cloudflare in front) often **block or challenge datacenter IPs**. Your Mac on a **residential** IP can get **JSON 200** while the VPS gets **HTTP 403** on the same URL.

Mitigations (in order of practicality):

1. **Fetch on Mac**, sync `data/sofascore/schedules/` and `data/sofascore/lineups/` to the server (Syncthing/rsync/git — watch for `.gitignore` on large JSON).
2. Optional **`data/sofascore/cookies.json`** — see `SOFASCORE_COOKIES_PATH` / `_load_sofascore_cookies()` in `services/sofascore_lineup_service.py` (treat as secret).
3. Residential proxy / different egress (not implemented in-repo).

## Cron / lock issues (VPS and Mac)

- `data/sofascore/.sofascore_lineup.lock` — only one of **listener** or **watcher** at a time.
- **Stale lock:** if the PID in the file is dead, `rm` the file (see `docs/SSH_COMMANDS_AND_TROUBLESHOOTING.md`).
- **Schedule:** daily predictions at **02:02 UTC** in `deploy/sofascore_crontab` avoids racing `*/5` jobs at **:00**.

## `players.csv` and mapping sync

`--with-mappings` needs a current **`players.csv`** (Fantrax export). If many EPL players are missing from that file, auto-mapping cannot resolve them.

**Manual mapping:** add entries to `config/player_mappings.yaml` (`PlayerMapping` fields: `fantrax_id`, `fantrax_name`, `sofascore_id`, `sofascore_name`, …). Re-run mapping sync or the full listener with `--with-mappings`.

## Quick parity check commands

**Local:**

```bash
cd /path/to/FantraxAPI && source .venv/bin/activate
python -c "import esd, curl_cffi, lxml; print('ok')"
playwright --version
```

**VPS:**

```bash
cd /opt/FantraxAPI && . .venv/bin/activate
python -c "import esd, curl_cffi, lxml; print('ok')"
command -v chromium-browser
curl -s -o /dev/null -w '%{http_code}' "https://api.sofascore.com/api/v1/unique-tournament/17/seasons"
# 403 from server IP is expected for bare requests; still useful as a smoke test
```
