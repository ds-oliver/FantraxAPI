# Conditional Swaps Design & Endpoints

This captures how the conditional swaps feature is stitched together, working backwards from the Streamlit page through the lineup/roster plumbing and into the Fantrax/FxPA calls it relies on.

- **UI surface**: `apps/auth_login/pages/conditional_swaps.py`
  - Rebuilds authenticated Fantrax session (`X-XSRF-TOKEN` from cookies, discovers `X-Fantrax-UI-Version`, sets `X-TZ`).
  - Forces league/team selection, then lets users pick lineup source (SofaScore primary vs Fantrax fallback) and target Fantrax period (GW inferred via `_match_period_from_round` + `get_available_periods`).
  - Offers immediate active↔reserve swap (uses `SubsService.swap_players`) to set the baseline XI before rule creation.
  - Rule builder supports lineup swap rules (ordered bench backups) and FA claim/drop rules (single FA target). Eligibility uses engine helpers (`is_row_locked`, `would_break_mandatory_slots`, `can_swap_in_period`) so only legal, future-kickoff starters appear.
  - Persists rules to `data/lineups/conditional_swap_rules.json` via `RuleStorage`; lists/toggles/deletes in-page. Logs to `data/logs/conditional_swaps.log`.

- **Lineup data pipeline**
  - SofaScore bridge (`fantraxapi/lineups/sofascore_bridge.py`): reads cached lineup JSON (`data/sofascore/lineups`) and schedule CSVs; builds `PlayerLineupInfo` with kickoff, team/opponent, home/away, predicted/confirmed lineup status.
  - Fantrax bridge (`fantraxapi/lineups/fantrax_lineup_bridge.py`): derives `fx_status` from roster row icons (`typeId` 12/32 or `upcomingEventStatusId==2`), strict locks from `disableLineupChange`, kickoff from `next_kickoff`/`startTime`.
  - Resolver (`fantraxapi/lineups/lineup_resolver.py`): merges SofaScore + Fantrax into effective `PlayerLineupInfo.status/kickoff/status_source`, retaining source-specific fields for debug/display.
  - Lock semantics (`fantraxapi/lineups/conditional_swaps.py`): `is_row_locked` only respects Fantrax `disableLineupChange`; UI “visually locked” also accounts for finished markers and passed kickoffs for styling.

- **Rule model and engine** (`fantraxapi/lineups/conditional_swaps.py`)
  - Models: `ConditionalSwapRule`, `BackupOption`, `RuleState`, `RuleActionType`, `SwapCondition`. Lineup swap rules require unique backups; FA rules require add-scorer/position ids.
  - Evaluation (lineup swap): fires only if active is an unlocked starter with future kickoff, status != STARTING, period matches, and fire count < max. Backup must be unlocked reserve, STARTING with future kickoff (>= active if `enforce_kickoff_order`), keep mandatory slots intact, and pass `can_swap_in_period`.
  - Evaluation (FA claim/drop): drop player must be non-starting with future kickoff; FA target must be STARTING with future kickoff not earlier than drop; roster legality checked via `_drop_would_keep_roster_legal`.
  - Execution: `execute_rules` dispatches lineup swaps via `SubsService.swap_players`; FA claims via `WaiversService.submit_claim`; fires tracked by `FireTracker`. (No scheduler in-repo; caller must invoke `execute_rules`.)

- **Fantrax / FxPA touchpoints**
  - Roster and periods: `FantraxAPI.roster_info(team_id)`, `FantraxAPI.scoring_periods()` (used by UI to warm caches and populate period selector).
  - FXPA Players table parsing:
    - API call: `fetch_fantrax_player_status_snapshot` posts to `https://www.fantrax.com/fxpa/req` with `getPlayerStats` (defaults: `miscDisplayType=10`, `status_filter=ALL` or `ALL_AVAILABLE` for FA). Headers set to include `X-Fantrax-UI-Version` and `X-TZ` when available.
    - Parsing: `parse_fantrax_player_statuses` walks `responses[0].data.statsTable` rows, extracts `scorer.icons` (`typeId` 12/32 → STARTING; strict lock if `disableLineupChange`), `scorerId`, `startTime`/`startTimestamp` → `kickoff`, `teamShortName`/`teamName`, `nextOpponent*`, and `cells[2].eventId`.
    - Storage for app use: converted into `FantraxPlayerStatus` objects, then into an in-memory map for the caller. For FA pool, `fetch_fa_status_map` returns `scorer_id -> FALineupSnapshot` and is fed directly into FA rule evaluation; nothing from FXPA snapshots is persisted to disk.
    - Roster-row parsing (no FXPA call): `build_lineup_info_by_player_fantrax` derives `fx_status` and `fx_kickoff` straight from `RosterRow._raw.scorer` (icons, `disableLineupChange`, `upcomingEventStatusId`, `startTime`), and only falls back to FXPA snapshots for FA targets as above.
  - Lineup legality: `test_swap_in_period` / `can_swap_in_period` call `SubsService.confirm_or_execute_lineup` (FXPA `confirmOrExecuteTeamRosterChanges`) for confirm-phase legality; immediate and executed swaps reuse `SubsService.swap_players`.
  - Waivers: `WaiversService.submit_claim` mirrors UI flow (`getScorerDetails` → `getClaimDropConfirmInfo` → `createClaimDrop`) for FA claim/drop rules; `WaiversService.list_players_by_name` plus FXPA status snapshots drive FA target eligibility.
  - Session headers: `_ensure_xsrf_header` injects `X-XSRF-TOKEN`; `_apply_fxpa_client_hints` seeds `X-Fantrax-UI-Version` and `X-TZ` via a lightweight `getAllLeagues` FXPA call.

- **Persistence, logging, debug**
  - Rules stored at `data/lineups/conditional_swap_rules.json` (JSON array of `ConditionalSwapRule`).
  - Logs at `data/logs/conditional_swaps.log` capture lock-debug, lineup bridge warnings, and rule execution outcomes.
  - UI debug affordances: per-player SofaScore/Fantrax context dumps (`debug_player_lineup_context`, `debug_fx_lineup_context`), backup eligibility table, and team/opponent source traces for snapshot tables.
