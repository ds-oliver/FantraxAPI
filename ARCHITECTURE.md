# FantraxAPI Architecture

**Last Updated:** November 15, 2025

This document describes the architecture of the FantraxAPI project, identifying core components, their relationships, and maintenance priorities.

---

## Quick Start

### For End Users

**GUI Application (Recommended):**
```bash
streamlit run apps/auth_login/app.py
```
Features: Authentication, roster management, substitutions, drops, FAAB monitoring

**CLI Example:**
```bash
python substitutions_v2.py --league-id YOUR_LEAGUE_ID --team-id YOUR_TEAM_ID
```

### For Developers

**Core library usage:**
```python
from fantraxapi import FantraxAPI
from requests import Session

# Load authenticated session (see AUTH.md for details)
session = Session()
# ... load cookies ...

# Initialize API
api = FantraxAPI(league_id="your_league_id", session=session)

# Make a substitution
api.swap_players(team_id, starter_id, bench_id)

# Submit a waiver claim
api.waivers.submit_claim(
    team_id=team_id,
    claim_scorer_id=player_id,
    bid_amount=10.0
)
```

---

## Architecture Tiers

### **Tier 1: Core Library** (`fantraxapi/` package)

The foundation that powers all functionality. These files implement the Fantrax API client and core operations.

#### Main API Client
- **`fantrax.py`** (696 lines) - Central API wrapper
	- Handles HTTP requests to Fantrax endpoints
	- Manages authentication session
	- Provides high-level methods: `roster_info()`, `swap_players()`, `scoring_periods()`
	- Integrates all feature services

#### Data Models
- **`objs.py`** - Data classes and models
	- `Team`, `Player`, `Roster`, `RosterRow`
	- `ScoringPeriod`, `Standings`, `Trade`, `Transaction`
	- Parsing and normalization of API responses

#### Feature Services
- **`subs.py`** (1,626 lines) - Substitution engine
	- Complex lineup change logic with position validation
	- Two-phase swap: confirm → execute
	- Handles locked players, deadline-passed scenarios
	- Field map construction for full roster changes
	- Position eligibility caching

- **`waivers.py`** - Waiver and free agent claims
	- `submit_claim()` - Submit FAAB bids or free agent pickups
	- `_fetch_player_stats_page()` - Browse available players
	- Three-step flow: getScorerDetails → getClaimDropConfirmInfo → createClaimDrop

- **`drops.py`** - Drop player operations
	- `drop_player()` - Release players from roster
	- Validation and preflight checks
	- Handles immediate vs. scheduled drops

- **`league.py`** - League-wide operations
	- `faab_budgets()` - Get FAAB budgets for all teams
	- `get_claim_info()` - Pending claims by team
	- `list_rosters()` - Fetch all team rosters

- **`trades.py`** - Trade operations
	- `list_pending()` - Active trade proposals
	- `get_trade_block()` - Players on trade block

#### Support Files
- **`exceptions.py`** - Custom exception classes (`FantraxException`, `Unauthorized`)
- **`utils.py`** - Utility functions
- **`__init__.py`** - Package initialization and exports

---

### **Tier 2: User Applications**

Entry points for end users.

#### Primary GUI
- **`apps/auth_login/app.py`** (1,188 lines) - **MAIN APPLICATION**
	- Streamlit web interface
	- Cookie-based authentication (upload or Selenium capture)
	- Roster viewer with substitutions
	- Drop player interface
	- League-wide FAAB monitoring
	- Division support

**Run with:** `streamlit run apps/auth_login/app.py`

#### CLI Reference
- **`substitutions_v2.py`** (302 lines) - Command-line example
	- Demonstrates `api.swap_players()` usage
	- Interactive roster management
	- Good reference for scripting

**Run with:** `python substitutions_v2.py --league-id XXX`

---

### **Tier 3: Utilities & Infrastructure**

Supporting infrastructure used by both applications and library.

#### Authentication
- **`utils/auth_helpers.py`** - Core auth functionality
	- `FantraxAuth` class - Selenium-based login
	- `load_requests_session_from_artifacts()` - Build authenticated session
	- `validate_logged_in()` - Session validation
	- `fetch_user_leagues()` - List user's leagues
	- Cookie and local storage management

See `AUTH.md` for detailed authentication documentation.

#### Service Wrappers
- **`utils/roster_ops.py`** (546 lines)
	- `DropService` - High-level drop operations
	- `LineupService` - Wraps SubsService for lineup changes
	- User-friendly abstractions over core library

#### Cookie Management
- **`utils/cookie_import.py`** - Cookie format conversions
	- Read Selenium pickle format
	- Read Cookie-Editor JSON exports
	- Normalize to standard format

---

### **Tier 4: Lineup Automation** ⚠️ INCOMPLETE

Infrastructure for automated lineup changes based on confirmed starting lineups from SofaScore.

#### Status: Partially Implemented

**What Works:**
- ✅ SofaScore lineup polling (`apps/lineup_watcher/watch_lineups.py`)
- ✅ Lineup data normalization (`fantraxapi/lineups/normalize.py`)
- ✅ Status determination logic (`fantraxapi/lineups/status.py`)
- ✅ Player mapping system

**What's Missing:**
- ❌ Integration between watcher and Fantrax API (TODO at line 65-67 in `watch_lineups.py`)
- ❌ Complete `LineupSynchronizer.sync_lineup()` implementation
- ❌ Automated execution loop
- ❌ Error handling and retry logic

#### Files

**Complete:**
- **`fantraxapi/lineups/status.py`** (88 lines)
	- `determine_lineup_status()` - Calculate lineup status based on timing
	- Status transitions: PRELIMINARY → PENDING_CONFIRMATION → CONFIRMED → FINAL
	- 75-minute confirmation window before kickoff

**Incomplete:**
- **`fantraxapi/lineups/automation.py`** - Main controller
	- `LineupAutomation` class orchestrates the workflow
	- **TODO:** Line 160 - needs polling integration
	- **TODO:** Connect to actual lineup sources

- **`fantraxapi/lineups/fantrax_sync.py`** - Sync confirmed lineups to Fantrax
	- `LineupSynchronizer` class
	- `determine_changes()` - Compare confirmed vs. current lineup
	- **TODO:** Complete `sync_lineup()` method
	- **TODO:** Error handling for locked players

- **`apps/lineup_watcher/watch_lineups.py`** - CLI watcher
	- Polls SofaScore for upcoming matches
	- Saves lineup data to parquet
	- **TODO:** Lines 65-67 - integrate with Fantrax API

#### Supporting Files
- **`fantraxapi/lineups/models.py`** - Data models (`LineupRecord`, `TeamLineup`, `PlayerRecord`)
- **`fantraxapi/lineups/normalize.py`** - Data normalization
- **`fantraxapi/lineups/testing.py`** - Test mode for validation
- **`fantraxapi/lineups/player_status_manager.py`** - Player status tracking

#### Integration Path (To Complete)

To finish the automation:

1. **Connect the watcher** - In `watch_lineups.py` line 65-67:
```python
from fantraxapi.lineup_rules.fantrax_actions import update_lineups
await update_lineups(records, fantrax_session, league_id, team_id)
```

2. **Implement sync_lineup()** - In `fantraxapi/lineups/fantrax_sync.py`:
	- Use `SubsService.set_lineup_by_ids()` for bulk changes
	- Or use `api.swap_players()` for sequential swaps
	- Handle errors and retries

3. **Add configuration** - Create config for:
	- Which leagues/teams to monitor
	- Timing windows (how early to check)
	- Dry-run vs. live mode

---

## Data Flow

### Authentication Flow
```
User Credentials
    ↓
[FantraxAuth] → Selenium Login → Capture Cookies + Storage
    ↓
[load_requests_session_from_artifacts] → Build requests.Session
    ↓
[FantraxAPI] → Authenticated API Client
```

### Substitution Flow
```
User Selection (starter_id, bench_id)
    ↓
[api.swap_players] OR [SubsService.swap_players]
    ↓
Build field_map (all roster positions)
    ↓
[SubsService.confirm_or_execute_lineup] (confirm=True)
    ↓
Server validates → Returns period/warnings
    ↓
[SubsService.confirm_or_execute_lineup] (do_finalize=True)
    ↓
Change applied (or scheduled for next period)
    ↓
Verify roster reflects change
```

### Lineup Automation Flow (When Complete)
```
[SofaScore API] → Match Schedule
    ↓
[watch_lineups.py] → Poll 75 min before kickoff
    ↓
Lineup Confirmed?
    ↓
[normalize_lineup_data] → LineupRecord
    ↓
[LineupSynchronizer.determine_changes] → List of swaps
    ↓
[SubsService.set_lineup_by_ids] → Execute changes
    ↓
Updated Fantrax Roster
```

---

## Dependencies

### Core Library
- `requests` - HTTP client
- `python-dateutil` - Date/time handling
- `pydantic` - Data validation (lineups module)
- `pandas` - Data processing (optional, for lineup storage)
- `PyYAML` - Configuration files

### Authentication
- `selenium` - Browser automation for login
- `webdriver-manager` - ChromeDriver management

### GUI Application
- `streamlit` - Web interface framework
- `pandas` - Data display

See `requirements.txt` for complete list.

---

## File Organization

```
FantraxAPI/
├── fantraxapi/              # Tier 1: Core Library
│   ├── fantrax.py          # Main API client ⭐
│   ├── subs.py             # Substitution engine ⭐
│   ├── objs.py             # Data models
│   ├── waivers.py          # Claims/waivers
│   ├── drops.py            # Drop players
│   ├── league.py           # League operations
│   ├── trades.py           # Trade operations
│   ├── exceptions.py       # Custom exceptions
│   ├── utils.py            # Utilities
│   └── lineups/            # Tier 4: Automation (incomplete)
│       ├── status.py       # ✅ Status logic
│       ├── automation.py   # ⚠️ Main controller
│       ├── fantrax_sync.py # ⚠️ Sync logic
│       ├── models.py       # Data models
│       ├── normalize.py    # Data normalization
│       └── testing.py      # Test framework
│
├── apps/                    # Tier 2: Applications
│   ├── auth_login/
│   │   └── app.py          # Primary GUI ⭐
│   ├── lineup_watcher/
│   │   └── watch_lineups.py # CLI watcher ⚠️
│   └── roster_viewer/
│       └── app.py          # Roster viewer
│
├── utils/                   # Tier 3: Utilities
│   ├── auth_helpers.py     # Auth core ⭐
│   ├── roster_ops.py       # Service wrappers ⭐
│   ├── cookie_import.py    # Cookie utils
│   └── log_helpers.py      # Logging
│
├── substitutions_v2.py      # CLI reference ⭐
├── requirements.txt         # Dependencies
├── setup.py                # Package setup
│
├── docs/                    # Documentation
│   ├── ARCHITECTURE.md     # This file
│   ├── AUTH.md             # Authentication guide
│   ├── README.rst          # Project overview
│   └── SUBS SUMMARY.md     # Substitution insights
│
└── archive/                 # Old versions (preserved)
    └── old_versions/        # Deprecated files
```

⭐ = Critical core file
⚠️ = Incomplete/has TODOs

---

## Maintenance Priorities

### High Priority (Active Maintenance)
1. **`fantraxapi/fantrax.py`** - Core API changes
2. **`fantraxapi/subs.py`** - Substitution logic refinements
3. **`apps/auth_login/app.py`** - Main user interface
4. **`utils/auth_helpers.py`** - Auth reliability

### Medium Priority (Stable, Occasional Updates)
5. `fantraxapi/waivers.py`, `drops.py`, `league.py` - Feature services
6. `utils/roster_ops.py` - Service wrappers
7. `substitutions_v2.py` - CLI reference

### Low Priority (Stable, Rarely Changed)
8. `fantraxapi/objs.py` - Data models
9. `fantraxapi/exceptions.py` - Exceptions
10. `utils/cookie_import.py` - Cookie handling

### Future Work (Incomplete Features)
11. **`fantraxapi/lineups/`** - Complete automation integration
12. **`apps/lineup_watcher/`** - Finish watcher integration

---

## Testing

### Manual Testing
Use the Streamlit GUI (`apps/auth_login/app.py`) to test:
- Authentication (cookie upload and Selenium)
- Roster viewing
- Substitutions
- Player drops
- FAAB monitoring

### Automated Tests
Located in `tests/` directory:
- `tests/test_api.py` - Core API tests
- `tests/lineups/` - Lineup automation tests

Run tests:
```bash
pytest tests/
```

---

## Known Issues & Limitations

### Authentication
- **Issue:** Headless login fails with Cloudflare challenges
- **Workaround:** Use visible browser once, then reuse cookies
- **Status:** Documented in logs (see `data/logs/auth_workflow.log`)

### Substitutions
- **Issue:** Complex swaps require sequential execution
- **Solution:** `SubsService` handles this automatically
- **Note:** See `SUBS SUMMARY.md` for detailed explanation

### Lineup Automation
- **Status:** Infrastructure complete, integration incomplete
- **Missing:** Connection from `watch_lineups.py` → Fantrax API
- **Impact:** Manual substitutions work; auto-lineup doesn't

---

## Configuration Files

- **`config.ini`** - User configuration (league IDs, team IDs)
- **`config/leagues.yaml`** - League-specific settings
- **`config/player_mappings.yaml`** - SofaScore ↔ Fantrax player mapping
- **`config/team_mappings.yaml`** - Team name mappings

---

## Related Documentation

- **`README.rst`** - Project overview and quick start
- **`AUTH.md`** - Detailed authentication guide
- **`SUBS SUMMARY.md`** - Substitution workflow insights
- **`IDEAS.md`** - Feature roadmap
- **`FILE_INDEX.md`** - Complete file listing with descriptions
- **`REORGANIZATION_LOG.md`** - History of file moves and archiving

---

## Contributing

When modifying the codebase:

1. **Core Library Changes** - Update tests and increment version
2. **GUI Changes** - Test authentication flow and all features
3. **New Features** - Update this document and `FILE_INDEX.md`
4. **Bug Fixes** - Document in commit message and update relevant .md files

### Version Control
- **Never delete old code** - Archive to `archive/old_versions/` with date
- **Document moves** - Update `REORGANIZATION_LOG.md`
- **Keep deprecation notes** - Leave comments in archived files explaining why

---

**Questions?** Check `README.rst` for contact information and community links.

