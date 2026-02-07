# Lineup Data Maintenance & Flow (Weekly/Ongoing Reference)

## Weekly / Ongoing Tasks
- Run SofaScore lineup fetch:  
  `python scripts/sofascore_lineup_listener.py --mode both --output-dir data/sofascore`  
  Add `--with-mappings --fantrax-players-csv players.csv --mapping-file config/player_mappings.yaml` when you want auto-sync of new players.
- Verify fresh data: check `data/sofascore/schedules/*_upcoming.csv` and `data/sofascore/lineups/*.json` are present for the coming gameweek.
- Keep mappings current: regenerate `config/player_mappings.yaml` when new players appear (listener with `--with-mappings` or `scripts/update_player_mappings.py`). Extend `TEAM_NAME_ALIASES` in `fantraxapi/lineups/sofascore_bridge.py` if new club name variants appear.
- Check Fantrax session health: ensure cookies/session headers are valid so `roster_info` and FXPA calls succeed; re-auth if calls start failing.
- Monitor logs: `data/logs/conditional_swaps.log` for `[unknown-status]`, missing kickoff, or alias warnings. If many `fx_status=UNKNOWN`, revisit `_derive_fx_statuses_and_kickoff` in `fantraxapi/lineups/fantrax_lineup_bridge.py`.
- Optional cleanup: prune stale lineups/schedules from `data/sofascore` to avoid collisions with old events.

## Component / Data Flow
- Data ingress:
  - SofaScore listener → writes schedules (`data/sofascore/schedules/*`) and lineups (`data/sofascore/lineups/{event_id}.json`).
  - Fantrax API (`FantraxAPI.roster_info`, FXPA) → roster rows (`_raw`) and nextOpponent/lock flags.
- Bridges & resolver:
  - `sofascore_bridge.build_lineup_info_by_player` → `ss_status` / `ss_kickoff` + fixture metadata (event_id, team/opponent, is_home).
  - `fantrax_lineup_bridge.build_lineup_info_by_player_fantrax` → `fx_status` / `fx_kickoff`; fills team/opponent if missing.
  - `lineup_resolver.resolve_lineup_info` → merges SS + FX into effective `PlayerLineupInfo` (status, kickoff, status_source).
- UI / pages:
  - `apps/auth_login/pages/conditional_swaps.py`:
    - Loads roster via FantraxAPI/session.
    - Builds `lineup_info_by_player` via resolver.
    - Active/Reserves snapshot uses `team_name` / `opponent_name` / `is_home` from `PlayerLineupInfo` (fallback to Fantrax scorer).
    - Lock styling uses `RosterView.lock_flags` (fx_locked, kickoff_passed, finished_marker, visually_locked).
    - Swap rules/eligibility rely on effective status/kickoff and strict Fantrax lock.
- Debug helpers:
  - `debug_player_lineup_context` (SofaScore) → schedule/snapshot/status/kickoff + alias info.
  - `debug_fx_lineup_context` (Fantrax) → icons, disableLineupChange, upcomingEventStatusId, derived `fx_status`.

## Handy Commands
- Fetch lineups + mappings:  
  `python scripts/sofascore_lineup_listener.py --mode both --output-dir data/sofascore --with-mappings --fantrax-players-csv players.csv --mapping-file config/player_mappings.yaml`
- Update mappings manually:  
  `python scripts/update_player_mappings.py --data-dir data --mapping-file config/player_mappings.yaml`
