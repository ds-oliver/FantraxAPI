# FantraxAPI File Index

**Last Updated:** November 15, 2025

Complete listing of all files in the FantraxAPI project with brief descriptions. Files are organized by tier (see `ARCHITECTURE.md` for details).

---

## Core Library (`fantraxapi/`)

### Main API Client
- **`__init__.py`** - Package initialization and public API exports
- **`fantrax.py`** ⭐ - Main FantraxAPI class; HTTP request wrapper; session management (696 lines)
- **`objs.py`** - Data models: Team, Player, Roster, ScoringPeriod, Trade, Transaction

### Feature Services
- **`subs.py`** ⭐ - Substitution engine; complex lineup logic; position validation (1,626 lines)
- **`waivers.py`** - Waiver and free agent claim submission
- **`drops.py`** - Drop player operations with validation
- **`league.py`** - League-wide operations: FAAB budgets, claim info, roster lists
- **`trades.py`** - Trade operations: pending trades, trade block

### Support
- **`exceptions.py`** - Custom exceptions: FantraxException, Unauthorized
- **`utils.py`** - Utility functions
- **`player_mapping.py`** - Player ID mapping between SofaScore and Fantrax

### Lineup Automation (`fantraxapi/lineups/`) ⚠️
- **`__init__.py`** - Lineups module initialization
- **`status.py`** ✅ - Lineup status determination logic (PRELIMINARY → CONFIRMED → FINAL)
- **`automation.py`** ⚠️ - Main lineup automation controller (incomplete)
- **`fantrax_sync.py`** ⚠️ - Synchronize confirmed lineups to Fantrax (incomplete)
- **`models.py`** - Data models: LineupRecord, TeamLineup, PlayerRecord, LineupStatus
- **`normalize.py`** - Normalize lineup data from various sources
- **`testing.py`** - Test framework for lineup prediction accuracy
- **`player_status_manager.py`** - Track player status changes
- **`lineup_status_monitor.py`** - Monitor schedule and trigger lineup checks

### Integrations (`fantraxapi/integrations/`)
Various integration modules for external services

### Providers (`fantraxapi/providers/`)
- **`sofascore/`** - SofaScore API integration for lineups and schedules
  - `__init__.py` - Module initialization
  - `client.py` - HTTP client for SofaScore API
  - `discover.py` - Match discovery and watchlist
  - `poll.py` - Lineup polling logic
  - `normalize.py` - Data normalization
  - `upsert.py` - Save lineup data
  - `schedule.py` - Schedule fetching
  - `models.py` - Data models
- **`ffscout/`** - Fantasy Football Scout integration
  - `client.py` - FFS API client
  - `models.py` - Data models

### Lineup Rules (`fantraxapi/lineup_rules/`)
Rule-based lineup decision making (incomplete)

---

## User Applications

### Primary GUI (`apps/auth_login/`)
- **`app.py`** ⭐ - Main Streamlit application; auth + subs + drops + FAAB (1,188 lines)
- **`requirements.txt`** - GUI-specific dependencies

### Lineup Watcher (`apps/lineup_watcher/`)
- **`__init__.py`** - Module initialization
- **`watch_lineups.py`** ⚠️ - CLI watcher for SofaScore lineups (TODO: Fantrax integration)

### Roster Viewer (`apps/roster_viewer/`)
- **`__init__.py`** - Module initialization
- **`app.py`** - Streamlit roster viewing application
- **`requirements.txt`** - Viewer-specific dependencies

---

## Utilities (`utils/`)

- **`auth_helpers.py`** ⭐ - Authentication core; FantraxAuth class; session builders; validation
- **`roster_ops.py`** ⭐ - Service wrappers: DropService, LineupService (546 lines)
- **`cookie_import.py`** - Cookie format conversion; Selenium pickle and Cookie-Editor JSON
- **`log_helpers.py`** - Logging utilities; fieldMap diff/digest functions

---

## CLI Scripts & Examples

### Root Directory (Active)
- **`substitutions_v2.py`** ⭐ - CLI reference for substitutions (302 lines)
- **`bootstrap_cookie.py`** - Bootstrap authentication cookies via Selenium
- **`setup_cookies.py`** - Cookie setup utility
- **`setup.py`** - Package installation configuration
- **`watch_lineups.py`** - Symlink to lineup watcher

### Example Scripts (`scripts/examples/`)
- **`drop_player.py`** - Example: Drop a player from roster
- **`list_rosters.py`** - Example: List all rosters in a league
- **`list_all_rosters.py`** - Example: Batch roster operations
- **`submit_claim.py`** - Example: Submit waiver/FA claim
- **`lineup_optimizer.py`** - Lineup optimization utility

### Monitoring Scripts (`scripts/monitoring/`)
- **`monitor_faab.py`** - Monitor FAAB budgets across league
- **`monitor_trades.py`** - Monitor trade activity

### Demo Scripts (`scripts/demos/`)
- **`esd_lineups_demo.py`** - SofaScore lineup export demo
- **`esd_lineups_demo_best.py`** - Enhanced lineup demo
- **`esd_lineups_full_demo_best.py`** - Full lineup demo with mapping

### Utility Scripts (`scripts/`)
- **`build_player_mappings_from_fantrax.py`** - Build mapping DB from Fantrax
- **`build_player_mappings.py`** - General player mapping builder
- **`demo_sofascore_schedule.py`** - SofaScore schedule API demo
- **`example_usage.py`** - General API usage examples
- **`fetch_season_games.py`** - Fetch all games for a season
- **`list_players.py`** - List all players in Fantrax
- **`map_lineups_to_fantrax.py`** - Map SofaScore lineups to Fantrax IDs
- **`test_lineup_*.py`** - Various lineup integration tests
- **`test_mapping_*.py`** - Player mapping tests
- **`test_scraperfc.py`** - ScraperFC integration test
- **`esd_export_schedule_and_lineups.py`** - Current schedule/lineup export script

---

## Tests (`tests/`)

- **`__init__.py`** - Test module initialization
- **`test_api.py`** - Core API tests
- **`test_lineup_workflow.py`** - Lineup workflow integration tests
- **`test_sofascore_normalize.py`** - SofaScore normalization tests

### Test Subdirectories
- **`apps/`** - Application-specific tests
  - `test_watch_lineups_cli.py` - CLI watcher tests
- **`lineups/`** - Lineup module tests
  - `test_automation.py` - Automation tests
  - `test_status.py` - Status determination tests
- **`mocks/`** - Test mocks and fixtures
  - `fantrax.py` - Fantrax API mocks
  - `sofascore.py` - SofaScore API mocks
- **`providers/`** - Provider integration tests

---

## Configuration

### Root Configuration
- **`config.ini`** - User configuration (league IDs, team IDs, paths)
- **`requirements.txt`** ⭐ - Core dependencies
- **`requirements-dev.txt`** - Development dependencies

### Configuration Files (`config/`)
- **`club_team_mappings.yaml`** - Club team name mappings
- **`fantrax_leagues.yaml`** - Fantrax league configuration
- **`leagues.yaml`** - General league configuration
- **`player_mappings.yaml`** - SofaScore ↔ Fantrax player ID mappings
- **`team_mappings.yaml`** - Team name mappings

---

## Documentation

- **`README.rst`** ⭐ - Project overview; quick start; installation
- **`ARCHITECTURE.md`** ⭐ - Detailed architecture documentation (this is primary reference)
- **`FILE_INDEX.md`** - This file
- **`REORGANIZATION_LOG.md`** - History of file moves and archiving
- **`AUTH.md`** - Authentication guide; cookie management
- **`SUBS SUMMARY.md`** - Substitution workflow insights and server behavior
- **`IDEAS.md`** - Feature roadmap and enhancement ideas
- **`INVENTORY.md`** - Original inventory (superseded by this file and ARCHITECTURE.md)
- **`KEY_FUNCTIONS.md`** - Key scripts listing
- **`LICENSE`** - MIT License
- **`VERSION`** - Package version number

### Sphinx Documentation (`docs/`)
- **`conf.py`** - Sphinx configuration
- **`index.rst`** - Documentation index
- **`intro.rst`** - Introduction
- **`fantrax.rst`** - Fantrax API docs
- **`objs.rst`** - Objects documentation
- **`toc.rst`** - Table of contents
- **`Makefile`** - Build documentation (Unix)
- **`make.bat`** - Build documentation (Windows)
- **`requirements.txt`** - Documentation build dependencies

---

## Data Storage (`data/`)

### Lineups (`data/lineups/`)
- *.parquet files - Saved lineup data from SofaScore

### Logs (`data/logs/`)
- **`auth_workflow.log`** - Authentication workflow logging
- **`auth_api.log`** - API request/response logging
- *.html, *.png files - Debug captures from Selenium

### Reports (`data/reports/unmatched/`)
- *.csv, *.json files - Unmatched player reports from mapping

### Schedule (`data/schedule/`)
- *.parquet files - Saved schedule data

### Silver Layer (`data/silver/`)
- *.csv, *.parquet, *.json files - Processed/cleaned data

### SofaScore (`data/sofascore/`)
- **`lineups/`** - Raw lineup JSON from SofaScore API (382 files)
- **`lineups_index.csv`** - Index of lineup files
- **`schedules/`** - Schedule CSV files

### Test Data (`data/test_lineups/`)
- Test lineup files for validation

---

## Archived Code (`archive/`)

### Old Versions (`archive/old_versions/`)
All superseded files preserved for historical reference. **Nothing deleted.**

- `app_v1.py`, `app_v2.py`, `app_v3.py` - Early Streamlit app versions
- `substitutions_v1.py`, `subs_v1.py` - Early CLI implementations
- `esd_export_schedule_and_lineups_v*.py` - Schedule export script versions
- `monitor_faab_v2.py` - Standalone FAAB monitoring
- `scripts_esd_export_schedule_and_lineups_v3.py` - Duplicate script
- `update_player_mappings_v2.py` - One-off mapping updater
- `test.py`, `test_matching_fixes.py` - Ad-hoc test scripts

See `REORGANIZATION_LOG.md` for details on why each was archived and what superseded it.

---

## Miscellaneous

- **`.gitignore`** - Git ignore patterns
- **`.cursorignore`** - Cursor AI ignore patterns
- **`fantraxloggedin.cookie`** - Saved authentication cookies (gitignored)
- **`cokkie.cook`** - Cookie configuration
- **`players.csv`** - Player data cache
- **`scout_picks_cache.sqlite`** - FFScout data cache
- **`player_mappings.yaml`** - Root player mappings (also in config/)

---

## Package Metadata (`fantraxapi.egg-info/`)
- `dependency_links.txt` - Package dependencies
- `PKG-INFO` - Package metadata
- `requires.txt` - Requirements
- `SOURCES.txt` - Source file list
- `top_level.txt` - Top-level packages

---

## Legend

- ⭐ - Core file, critical to maintain
- ⚠️ - Incomplete or has TODOs
- ✅ - Complete and stable

---

## File Count Summary

- **Core Library:** ~25 Python files
- **Applications:** 3 main apps
- **Utilities:** 4 key utility modules
- **Scripts:** ~30 examples, demos, and utilities
- **Tests:** ~15 test files
- **Configuration:** ~10 config files
- **Documentation:** 10 markdown/rst files
- **Archived:** 13 old version files (preserved)

**Total Active Python Files:** ~80  
**Total Archived Files:** 13  
**Total Documentation Files:** 10

---

## Finding Files

### By Function

**Need to make substitutions?**
- Library: `fantraxapi/subs.py`, `fantraxapi/fantrax.py`
- GUI: `apps/auth_login/app.py`
- CLI: `substitutions_v2.py`

**Need authentication?**
- Core: `utils/auth_helpers.py`
- Setup: `bootstrap_cookie.py`, `setup_cookies.py`
- Docs: `AUTH.md`

**Need lineup automation?**
- Watcher: `apps/lineup_watcher/watch_lineups.py`
- Automation: `fantraxapi/lineups/automation.py` (incomplete)
- Status: `fantraxapi/lineups/status.py`

**Need player mapping?**
- Config: `config/player_mappings.yaml`
- Code: `fantraxapi/player_mapping.py`
- Scripts: `scripts/map_lineups_to_fantrax.py`

### By Tier (see ARCHITECTURE.md)

**Tier 1 (Core Library):** `fantraxapi/*.py`  
**Tier 2 (Applications):** `apps/*/app.py`  
**Tier 3 (Utilities):** `utils/*.py`  
**Tier 4 (Automation):** `fantraxapi/lineups/*.py`

---

## Questions?

- **Architecture details:** See `ARCHITECTURE.md`
- **File history:** See `REORGANIZATION_LOG.md`
- **Getting started:** See `README.rst`
- **Authentication:** See `AUTH.md`

---

**Maintained By:** FantraxAPI Development Team  
**Last Review:** November 15, 2025

