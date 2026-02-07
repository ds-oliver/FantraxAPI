# Fantrax Data Flow in the App

This document summarizes which data elements we pull from the Fantrax APIs and how they are used in the app (primarily the Conditional Swaps page and lineup bridges).

## Sources & Key Fields

### 1) Roster API (`FantraxAPI.roster_info(team_id)`)
Returns `Roster` and `RosterRow` objects with:
- `player.id`, `player.name`, `player.team_name`, `player.team_short_name`, `player.next_kickoff` (ms epoch or ISO), plus the raw `_raw` payload.
- `_raw["scorer"]` block with:
  - `scorerId`, `name`, `teamName`, `teamShortName`
  - `icons` (list of dicts: `typeId`, `tooltip`) used to derive `fx_status`
  - `disableLineupChange` (bool) → strict Fantrax lock
  - `nextOpponent*` fields: `nextOpponentShortName`, `nextOpponent`, `nextOpponentName`, `nextOpponentIsAway`
  - `upcomingEventStatusId` (e.g., `"2"` = expected to play)
  - Positional/meta fields (`posIds`, `posShortNames`, etc.)
- `_raw["cells"][0]["content"]` often contains the matchup/score string; “ F” indicates finished.

Usage:
- Parsed in `fantraxapi/lineups/sofascore_bridge._kickoff_from_fantrax_row` to get `fantrax_kickoff`.
- `fantraxapi/lineups/fantrax_lineup_bridge._derive_fx_statuses_and_kickoff`:
  - `disableLineupChange` + finished marker → `fx_status = OUT`
  - `icons` typeId 12/32 → `STARTING`
  - `upcomingEventStatusId=="2"` → `STARTING`
  - `fx_kickoff` from `_kickoff_from_fantrax_row` if future
- Team/opponent fallback:
  - `teamShortName`/`teamName` → `PlayerLineupInfo.team_name` (if SS didn’t set)
  - `nextOpponent*` + `nextOpponentIsAway` → `PlayerLineupInfo.opponent_name` / `is_home` (fallback)
- Locking:
  - `is_row_locked` (strict) uses `disableLineupChange`
  - `get_row_lock_flags` adds `finished_marker` (cells content) and `kickoff_passed` (from lineup info)

### 2) FXPA Players Table (`/fxpa/req` via `fantrax_lineup_bridge.fetch_fantrax_player_status_snapshot`)
Payload contains `data.statsTable` rows with `scorer` and `icons`; parsed by:
- `parse_fantrax_player_statuses` → `FantraxPlayerStatus` (fantrax_player_id, status, event_id from cells[2].eventId)
- This is not the primary path for lineup info in the swaps page; we prefer per-roster-row status. The FXPA call can be used for diagnostics or alternative flows.

### 3) Next Opponent / Team Info for Display
- Extracted in UI:
  - Preferred: `PlayerLineupInfo.team_name`, `opponent_name`, `is_home` (set by SofaScore schedule or Fantrax fallback).
  - Fallback: `_fantrax_team_and_opponent(row)` from `row._raw["scorer"]` fields as above.

## Processing Pipeline

1) **SofaScore bridge** (`fantraxapi/lineups/sofascore_bridge.build_lineup_info_by_player`):
   - Resolves schedule (SofaScore CSV) → `event_id`, `kickoff_utc`, home/away teams.
   - Builds `ss_status` from snapshots (confirmed/predicted roles).
   - Sets `event_id`, `team_name`, `opponent_name`, `is_home` from schedule when possible.
   - Fallbacks to Fantrax scorer for team/opponent if no schedule match.

2) **Fantrax bridge** (`fantraxapi/lineups/fantrax_lineup_bridge.build_lineup_info_by_player_fantrax`):
   - Derives `fx_status`, `fx_kickoff` from roster row `_raw` (icons, disableLineupChange, upcomingEventStatusId, next_kickoff).
   - Populates team/opponent/is_home only if SofaScore hasn’t already done so.

3) **Resolver** (`fantraxapi/lineups/lineup_resolver.py`):
   - Merges SS + FX into effective `PlayerLineupInfo.status/kickoff/status_source`, preserving source-specific fields (`ss_*`, `fx_*`) and fixture metadata.

4) **UI (Conditional Swaps page)** (`apps/auth_login/pages/conditional_swaps.py`):
   - Loads roster via FantraxAPI/session.
   - Calls resolver to get `lineup_info_by_player`.
   - Displays Active/Reserves snapshot with Team/Opponent (preferring `PlayerLineupInfo`, falling back to Fantrax scorer).
   - Lock styling uses `RosterView.lock_flags` (`fx_locked`, `kickoff_passed`, `finished_marker`, `visually_locked`).
   - Swap legality uses strict `is_row_locked` (Fantrax disableLineupChange).

## Outputs / Storage
- Lineup info is computed in-memory per request; not persisted.
- Logs:
  - `data/logs/conditional_swaps.log` for SofaScore bridge status, missing kickoff, lock-debug, unknown-status.
  - FX debug via `debug_fx_lineup_context` (icons, disableLineupChange, upcomingEventStatusId, derived `fx_status`).

## Summary of Field Usage
- `disableLineupChange`: strict lock (Fantrax), used in swap validation and lock flags.
- `icons` (typeId 12/32): map to `fx_status` STARTING (expected/confirmed).
- `upcomingEventStatusId`: fallback to `fx_status` STARTING when == "2".
- `next_kickoff`: primary kickoff fallback when future; ingested by `_kickoff_from_fantrax_row`.
- `nextOpponent*`: opponent display fallback and home/away (`nextOpponentIsAway`).
- `cells[0].content`: finished marker " F" used in lock flags.
- Schedule CSV (SofaScore): source of `event_id`, `kickoff`, canonical team/opponent, home/away side, driving `ss_status` and fixture metadata.
