# Fantrax API Application Architecture

**Complete File Structure & User Flow Documentation**

This document provides a comprehensive map of all key files organized by feature and user flow, with explicit file paths.

---

## Table of Contents

1. [Application Entry Points](#application-entry-points)
2. [Authentication Flow](#authentication-flow)
3. [Roster Management](#roster-management)
4. [Lineup Intelligence](#lineup-intelligence)
5. [Player Swaps & Transactions](#player-swaps--transactions)
6. [Data Fetching & Processing](#data-fetching--processing)
7. [Mapping & Configuration](#mapping--configuration)
8. [Supporting Utilities](#supporting-utilities)

---

## Application Entry Points

### Main Streamlit Application
**File**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py`
- **Purpose**: Primary application interface for Fantrax roster management
- **Features**:
  - User authentication (cookies & Selenium)
  - League and team selection
  - Roster display and management
  - Player substitutions
  - Player drops
  - FAAB/waiver claims
  - Navigation to Lineup Intelligence
- **Entry Command**: `streamlit run apps/auth_login/app.py`
- **Dependencies**: 
  - `/Users/hogan/FantraxAPI/apps/auth_login/login.py`
  - `/Users/hogan/FantraxAPI/utils/auth_helpers.py`
  - `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`

### Lineup Intelligence Page
**File**: `/Users/hogan/FantraxAPI/apps/auth_login/pages/lineup_intelligence_page.py`
- **Purpose**: Display SofaScore lineup predictions against user roster
- **Features**:
  - League and team selection (self-contained)
  - Load lineup predictions from JSON files
  - Map SofaScore players to Fantrax roster
  - Display actionable recommendations
  - Show injured/unavailable players
  - Filter to next match only per team
- **Access**: Via sidebar link "Fantrax x SofaScore Link" in main app
- **Dependencies**:
  - `/Users/hogan/FantraxAPI/data/sofascore/lineups/*.json`
  - `/Users/hogan/FantraxAPI/data/sofascore/schedules/*_upcoming.csv`
  - `/Users/hogan/FantraxAPI/fantraxapi/player_mapping.py`

---

## Authentication Flow

### 1. Cookie-Based Authentication (Primary Method)

**Status**: ✅ **Primary, recommended authentication method**

**Primary File**: `/Users/hogan/FantraxAPI/utils/auth_helpers.py`
- **Functions**:
  - `load_requests_session_from_artifacts(artifacts)` - Build session from cookies + storage
  - `validate_logged_in(session)` - Validate authentication
  - `fetch_user_leagues(session)` - Get user's leagues from Fantrax
  - `fetch_user_profile(session)` - Get user profile info

**Cookie Import**: `/Users/hogan/FantraxAPI/utils/cookie_import.py`
- **Function**: `read_auth_file(file_handle)` - Parse cookie JSON from Cookie-Editor browser extension
- **Returns**: `{"cookies": [...], "storage": {"local": {...}, "session": {...}}}`

**UI Component**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 555-615)
- Two input methods:
  1. **Paste cookies**: Text area for pasting JSON directly
  2. **Upload file**: File uploader for `.json` files
- Cookie validation
- Session creation
- User cookie storage

**Cookie Storage Format**: JSON exported from Cookie-Editor browser extension:
```json
{
  "cookies": [
    {"name": "FX_RM", "value": "...", "domain": ".fantrax.com", ...},
    {"name": "FX_SESS", "value": "...", "domain": ".fantrax.com", ...}
  ],
  "storage": {
    "local": {"key": "value"},
    "session": {"key": "value"}
  }
}
```

**Workflow**:
1. User installs Cookie-Editor browser extension
2. User logs into Fantrax in browser
3. User exports cookies using Cookie-Editor
4. User pastes or uploads cookies to app
5. App validates cookies and creates authenticated session

### 2. Selenium Authentication (Experimental Fallback)

**Status**: ⚠️ **Experimental - may break due to anti-bot detection**

**Primary File**: `/Users/hogan/FantraxAPI/utils/auth_helpers.py`
- **Class**: `FantraxAuth`
- **Methods**:
  - `login_and_get_cookies(username, password, headless=False)` - Automate browser login
  - `headless_login_build_session(username, password, headless=True, validate=True)` - Headless login

**UI Component**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 617-683)
- Located in collapsed expander: "⚙️ Advanced / Experimental Login Methods"
- Two tabs:
  1. **Visible Browser**: Opens Chrome/Firefox window for login
  2. **Headless**: Automated login without browser window
- Warning about reliability issues
- Browser automation
- Automatic cookie extraction

**Note**: Selenium authentication is kept as a fallback but is less reliable than cookie upload and may fail due to Fantrax's anti-bot protections. Cookie-based authentication is strongly preferred.

### 3. Session Management

**Core Library**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`
- **Class**: `FantraxAPI`
- **Constructor**: `__init__(league_id, session)`
- **Session State**: Stored in `st.session_state["auth_artifacts"]`
- **Persistence**: Cookies stored in session state, survives page navigation

---

## Roster Management

### Roster Display

**Main File**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 195-380)

**API Call**: 
- **Method**: `FantraxAPI.roster_info(team_id)`
- **Source**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py` (lines 180-220)
- **Returns**: `RosterInfo` object

**Data Models**: `/Users/hogan/FantraxAPI/fantraxapi/models.py`
- `RosterInfo` - Full roster data
- `RosterRow` - Individual player row
- `Player` - Player details
- `Position` - Position info

**Display Component**: Streamlit dataframe with columns:
- Position
- Player Name
- Team
- Status
- Opponent
- Score

### Roster Loading Flow

1. **User Action**: Select league → Select team → Click "Load Roster"
2. **API Call**: `api.roster_info(team_id)`
3. **Data Storage**: 
   - `st.session_state["roster"]` - Roster data
   - `st.session_state["api"]` - API instance
   - `st.session_state["league_id"]` - Current league
   - `st.session_state["team_id"]` - Current team
4. **UI Update**: Display roster table

**Session State Keys**:
- `auth_artifacts` - Authentication cookies
- `roster` - Current roster data
- `api` - FantraxAPI instance
- `session` - Requests session
- `league_id` - Selected league ID
- `team_id` - Selected team ID

---

## Lineup Intelligence

### Complete Flow: User → Data Display

#### Step 1: Data Fetching (External Script)

**Script**: `/Users/hogan/FantraxAPI/esd_export_schedule_and_lineups_v2.py`
- **Purpose**: Fetch schedules and lineups from SofaScore
- **Command**: 
  ```bash
  python esd_export_schedule_and_lineups_v2.py \
    --tournament-id 17 \
    --upcoming \
    --with-lineups
  ```
- **Output Files**:
  - `/Users/hogan/FantraxAPI/data/sofascore/schedules/17_*_upcoming.csv`
  - `/Users/hogan/FantraxAPI/data/sofascore/lineups/{event_id}.json`
  - `/Users/hogan/FantraxAPI/data/sofascore/lineups_index.csv`

**Documentation**: `/Users/hogan/FantraxAPI/docs/ESD_LINEUP_FETCHER.md`

#### Step 2: Lineup JSON Structure

**Location**: `/Users/hogan/FantraxAPI/data/sofascore/lineups/{event_id}.json`

**Structure**:
```json
{
  "event_id": 14025066,
  "confirmed": false,
  "home": {
    "formation": "4-2-3-1",
    "starters": [
      {
        "id": 869792,
        "name": "Gabriel Magalhães",
        "position": "D",
        "team_id": 42,
        "substitute": false,
        "captain": false
      }
    ],
    "subs": [],
    "missing": [
      {
        "id": 869792,
        "name": "Gabriel Magalhães",
        "position": "D",
        "reason": 1  // 1=Doubtful, 2=Out
      }
    ]
  },
  "away": { ... }
}
```

**Key Arrays**:
- `starters[]` - Predicted/confirmed starting XI (has `team_id`)
- `subs[]` - Substitutes (only in confirmed lineups, has `team_id`)
- `missing[]` - Injured/unavailable (no `team_id` - must infer from side)

#### Step 3: Schedule CSV Structure

**Location**: `/Users/hogan/FantraxAPI/data/sofascore/schedules/17_*_upcoming.csv`

**Columns**:
- `event_id` - Match ID (links to lineup JSON)
- `kickoff_utc` - Match kickoff time
- `home_team` - Home team name
- `away_team` - Away team name
- `home_team_id` - SofaScore home team ID
- `away_team_id` - SofaScore away team ID
- `tournament_id` - League ID (17 = Premier League)
- `season_id` - Season identifier
- `round` - Round/gameweek number
- `status_code` - Match status

#### Step 4: Player Mapping

**File**: `/Users/hogan/FantraxAPI/fantraxapi/player_mapping.py`
- **Class**: `PlayerMappingManager`
- **Config**: `/Users/hogan/FantraxAPI/config/player_mappings.yaml`

**Mapping Structure** (YAML):
```yaml
- fantrax_id: "05nzu"
  fantrax_name: "Gabriel Magalhaes"
  sofascore_id: 869792
  sofascore_name: "Gabriel Magalhães"
  ffscout_name: "Gabriel"
  other_names: []
  display_name: "Gabriel Magalhães"
```

**Key Methods**:
- `get_by_sofascore_id(sofascore_id)` - Get Fantrax mapping for SofaScore player
- `get_by_fantrax_id(fantrax_id)` - Get SofaScore mapping for Fantrax player
- `add_mapping(mapping)` - Add new player mapping
- `save_mappings()` - Persist mappings to YAML

**Update Script**: `/Users/hogan/FantraxAPI/scripts/update_player_mappings.py`

#### Step 5: Lineup Loading in UI

**File**: `/Users/hogan/FantraxAPI/apps/auth_login/pages/lineup_intelligence_page.py`

**Button Handler** (lines 96-255):
1. Load schedule CSV (most recent `*_upcoming.csv`)
2. Filter to future matches only (`kickoff_dt > now`)
3. Sort by kickoff time (earliest first)
4. For each match in schedule:
   - Load lineup JSON (`{event_id}.json`)
   - Extract `home_team_id` and `away_team_id` from schedule
   - Track if this is team's next match (`team_next_match` dict)
   - Only process if it's the team's first upcoming match
5. Extract players from relevant lineups:
   - **Starters**: `is_sub=False`, `is_missing=False`, has `team_id`
   - **Subs**: `is_sub=True`, `is_missing=False`, has `team_id`
   - **Missing**: `is_sub=False`, `is_missing=True`, `team_id` inferred from side
6. Map SofaScore IDs to Fantrax IDs using `PlayerMappingManager`
7. Store in `st.session_state["lineup_predictions"]`

**Intelligent Filtering**:
- **Purpose**: Only show next match per team (not all future matches)
- **Algorithm**: 
  - Sort schedule by kickoff time
  - First time a team appears = their next match
  - Subsequent matches for same team = ignored
- **Documentation**: `/Users/hogan/FantraxAPI/docs/LINEUP_PREDICTION_SELECTION.md`

#### Step 6: Display Logic

**File**: `/Users/hogan/FantraxAPI/apps/auth_login/pages/lineup_intelligence_page.py` (lines 260-440)

**Data Flow**:
1. Get all roster players from `roster.rows`
2. Create lookup dict of lineup predictions by `fantrax_id`
3. For each roster player:
   - Check if prediction exists in lookup
   - **If prediction exists**:
     - **If `is_missing=True`**: Show "Doubtful" or "Out" (reason 1 or 2)
     - **If `is_missing=False`**: Show "Starter" or "Bench" (based on `is_sub`)
   - **If no prediction**: Show "Unknown" / "No Data"
4. Build rows for dataframe with:
   - Player name
   - Position (actual position, not "Res")
   - Roster Status ("Starting" or "Benched")
   - Predicted ("Starter", "Bench", "Doubtful", "Out", "Unknown")
   - Status ("Confirmed", "Predicted", "No Data")
   - Action ("Start", "Bench", "OK", "-")
   - Match (opponent and kickoff time)
5. Sort by: Roster Status (starting first) → Position (G,D,M,F) → Name
6. Display as Streamlit dataframe

**Column Widths** (lines 408-414):
- Player: medium
- Pos: small
- Roster Status: small
- Predicted (SofaScore Pred): small
- Status (Confirmation): small
- Action: small
- Match (Next Match): large

**Summary Metrics** (lines 392-401):
- Total Players
- With Lineup Data
- Need Attention

---

## Player Swaps & Transactions

### Player Substitutions

**File**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 520-650)

**API Method**: 
- **File**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`
- **Method**: `swap_players(team_id, player_in_id, player_out_id, pos_in, pos_out)`
- **Purpose**: Swap two players on roster (active ↔ reserve)

**UI Flow**:
1. User selects "Player to Remove" (from starting lineup)
2. User selects "Position for New Player"
3. User selects "Player to Add" (from bench/reserves)
4. Click "Execute Swap"
5. API call to Fantrax
6. Roster reloads automatically

**Position Mapping**: 
- Uses `Position` objects from roster
- `pos_id` for API calls
- `short_name` for display

### Player Drops

**File**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 652-750)

**API Method**:
- **File**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`
- **Method**: `drop_player(team_id, player_id)`
- **Purpose**: Drop player from roster

**UI Flow**:
1. User selects player from dropdown (all roster players)
2. Click "Drop Player"
3. Confirmation required
4. API call to Fantrax
5. Roster reloads automatically

### FAAB & Waiver Claims

**File**: `/Users/hogan/FantraxAPI/apps/auth_login/app.py` (lines 752-900)

**API Methods**:
- **File**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`
- **Methods**:
  - `get_league_faab()` - Get all teams' FAAB amounts
  - `get_available_players(position_filter)` - Get free agents
  - `submit_waiver_claim(team_id, add_player_id, drop_player_id, bid_amount)` - Submit claim

**UI Components**:
1. **FAAB Display**: Show all teams' remaining budgets
2. **Available Players**: Filter by position, search by name
3. **Submit Claim**: Select add/drop, enter bid amount
4. **Current Claims**: View pending claims

---

## Data Fetching & Processing

### SofaScore Data Fetching

**Primary Script**: `/Users/hogan/FantraxAPI/esd_export_schedule_and_lineups_v2.py`

**Functionality**:
- Fetch match schedules for tournaments
- Fetch lineups (predicted & confirmed)
- Handle both browser automation (EasySoccerData) and HTTP fallbacks
- Export to CSV (schedules) and JSON (lineups)
- Create index of all lineup files

**Command Line Options**:
```bash
--tournament-id 17          # Premier League
--season-id 61627           # Optional, defaults to current
--upcoming                  # Fetch upcoming matches
--last                      # Fetch past matches
--with-lineups              # Fetch lineups for matches
--rounds 1,2,3              # Specific rounds only
```

**Output Structure**:
```
/Users/hogan/FantraxAPI/data/sofascore/
├── schedules/
│   ├── 17_61627_upcoming.csv
│   ├── 17_61627_last.csv
│   └── ...
├── lineups/
│   ├── 14025066.json
│   ├── 14025063.json
│   └── ...
└── lineups_index.csv
```

**Documentation**: `/Users/hogan/FantraxAPI/docs/ESD_LINEUP_FETCHER.md`

### Alternative Lineup Watcher (Deprecated)

**File**: `/Users/hogan/FantraxAPI/fantraxapi/lineups/sofascore_watch.py`
- **Status**: Deprecated in favor of ESD script
- **Reason**: HTTP 403 errors from SofaScore API
- **Method**: `watch_lineups(tournament_id, poll_interval, output_dir)`

### Lineup Normalization

**File**: `/Users/hogan/FantraxAPI/fantraxapi/lineups/sofascore_normalize.py`
- **Purpose**: Convert raw SofaScore data to internal models
- **Function**: `normalize_lineup_data(raw_data, mapping_manager)`
- **Output**: `LineupRecord` objects

**Models**: `/Users/hogan/FantraxAPI/fantraxapi/lineups/models.py`
- `LineupRecord` - Complete lineup for a match
- `TeamLineup` - Single team's lineup
- `PlayerRecord` - Individual player in lineup
- `LineupStatus` - Enum (PRELIMINARY, PENDING_CONFIRMATION, CONFIRMED, FINAL, INVALID)

### Lineup Status Determination

**File**: `/Users/hogan/FantraxAPI/fantraxapi/lineups/status.py`
- **Function**: `determine_lineup_status(lineup_data, kickoff_time)`
- **Logic**:
  - Check if confirmed flag is set
  - Check time until kickoff
  - Check completeness of data
  - Return appropriate `LineupStatus`

---

## Mapping & Configuration

### Player Mappings

**Config File**: `/Users/hogan/FantraxAPI/config/player_mappings.yaml`
- **Format**: YAML list of player mappings
- **Fields**: fantrax_id, fantrax_name, sofascore_id, sofascore_name, ffscout_name, other_names, display_name
- **Size**: 5,800+ entries

**Manager Class**: `/Users/hogan/FantraxAPI/fantraxapi/player_mapping.py`
- **Purpose**: Load, query, update player mappings
- **Methods**:
  - `get_by_sofascore_id(id)` - Lookup by SofaScore ID
  - `get_by_fantrax_id(id)` - Lookup by Fantrax ID
  - `get_by_name(name)` - Fuzzy name matching
  - `add_mapping(mapping)` - Add new mapping
  - `save_mappings()` - Write to YAML file

**Update Script**: `/Users/hogan/FantraxAPI/scripts/update_player_mappings.py`
- **Purpose**: Refresh mappings from various sources
- **Sources**: FFScout, Fantrax roster data, SofaScore lineups
- **Run**: `python scripts/update_player_mappings.py`

### Team Mappings

**Config File**: `/Users/hogan/FantraxAPI/config/team_mappings.yaml`
- **Purpose**: Map team names between Fantrax, SofaScore, FFScout
- **Structure**:
  ```yaml
  - fantrax_name: "Arsenal"
    sofascore_id: 42
    sofascore_name: "Arsenal"
    other_names: ["AFC", "The Gunners"]
  ```

### League Configuration

**Config File**: `/Users/hogan/FantraxAPI/config/leagues.yaml`
- **Purpose**: Define monitored leagues and tournaments
- **Structure**:
  ```yaml
  - id: 17
    name: "Premier League"
    country: "England"
    season: 61627
    enabled: true
  ```

### Fantrax League Settings

**Config File**: `/Users/hogan/FantraxAPI/config/fantrax_leagues.yaml`
- **Purpose**: Store user's Fantrax league IDs and settings
- **Structure**:
  ```yaml
  leagues:
    - league_id: "abc123"
      league_name: "My Fantasy League"
      team_id: "xyz789"
      team_name: "My Team"
  ```

---

## Supporting Utilities

### Authentication Utilities

**File**: `/Users/hogan/FantraxAPI/utils/auth_helpers.py`
- `load_cookies_from_json(path)` - Parse cookie JSON file
- `build_requests_session(cookies)` - Create authenticated session
- `load_requests_session_from_artifacts(artifacts)` - Rebuild session
- `fetch_user_leagues(session)` - Get user's leagues
- `list_rosters(league_id, session)` - List all rosters in league

### Fantrax API Client

**File**: `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py`
- **Class**: `FantraxAPI`
- **Key Methods**:
  - `roster_info(team_id)` - Get roster
  - `swap_players(...)` - Swap players
  - `drop_player(...)` - Drop player
  - `get_league_faab()` - Get FAAB info
  - `get_available_players(...)` - Get free agents
  - `submit_waiver_claim(...)` - Submit claim
  - `get_league_standings()` - Get standings
  - `get_player_stats(...)` - Get player stats

### SofaScore API Client

**File**: `/Users/hogan/FantraxAPI/fantraxapi/providers/sofascore/client.py`
- **Class**: `SofaScoreClient`
- **Methods**:
  - `get_matches(tournament_id, season_id, round_num)` - Get matches
  - `get_match_lineups(event_id)` - Get lineup for match
  - `get_tournament_seasons(tournament_id)` - Get seasons
  - `get_player_details(player_id)` - Get player info

### FFScout Scraper

**File**: `/Users/hogan/FantraxAPI/fantraxapi/providers/ffscout/scout_picks_rosters.py`
- **Purpose**: Scrape player data from FFScout for mappings
- **Method**: `scrape_ffscout_rosters()`
- **Output**: Player names and IDs for mapping

### Logging Configuration

**File**: `/Users/hogan/FantraxAPI/fantraxapi/logging_config.py`
- **Purpose**: Configure application logging
- **Log Files**:
  - `/Users/hogan/FantraxAPI/data/logs/auth_workflow.log`
  - `/Users/hogan/FantraxAPI/data/logs/lineup_status_app.log`
- **Levels**: DEBUG, INFO, WARNING, ERROR

---

## Key Design Patterns

### Session State Management

**Pattern**: Store authentication and data in `st.session_state`

**Keys**:
```python
st.session_state["auth_artifacts"]      # Authentication cookies
st.session_state["session"]             # Requests session
st.session_state["api"]                 # FantraxAPI instance
st.session_state["league_id"]           # Current league
st.session_state["team_id"]             # Current team
st.session_state["roster"]              # Roster data
st.session_state["lineup_predictions"]  # Lineup predictions
```

**Benefits**:
- Persist data across page navigation
- Share data between main app and lineup intelligence
- No need to reload roster when switching pages

### Player Mapping Strategy

**Two-Way Lookup**:
1. SofaScore ID → Fantrax ID (for displaying lineups)
2. Fantrax ID → SofaScore ID (for finding lineup data)

**Fallback Chain**:
1. Try exact ID match
2. Try exact name match
3. Try fuzzy name match (normalized, unidecode)
4. Try other_names list
5. Return None (show as "Unknown")

### Lineup Filtering Algorithm

**Next Match Selection**:
```python
team_next_match = {}  # team_id -> event_id
for match in sorted_by_kickoff:
    if home_team_id not in team_next_match:
        team_next_match[home_team_id] = event_id
    if away_team_id not in team_next_match:
        team_next_match[away_team_id] = event_id
```

**Rationale**: Only show next match per team, not all future matches
**Documentation**: `/Users/hogan/FantraxAPI/docs/LINEUP_PREDICTION_SELECTION.md`

### Error Handling

**Pattern**: Try-except with user-friendly messages

```python
try:
    # API call or data processing
except Exception as e:
    logger.exception("Detailed error")
    st.error(f"User-friendly message: {e}")
```

**Logging**: All errors logged to file for debugging

---

## File Dependencies Graph

### Main App Flow
```
app.py
├── login.py (Selenium auth)
├── utils/auth_helpers.py (Cookie auth)
├── fantraxapi/fantrax.py (API client)
│   └── fantraxapi/models.py (Data models)
└── pages/lineup_intelligence_page.py
    ├── data/sofascore/schedules/*.csv
    ├── data/sofascore/lineups/*.json
    └── fantraxapi/player_mapping.py
        └── config/player_mappings.yaml
```

### Lineup Intelligence Flow
```
esd_export_schedule_and_lineups_v2.py
├── fetches from SofaScore API
└── outputs to:
    ├── data/sofascore/schedules/*.csv
    └── data/sofascore/lineups/*.json

lineup_intelligence_page.py
├── reads from:
│   ├── data/sofascore/schedules/*.csv
│   └── data/sofascore/lineups/*.json
├── maps players via:
│   └── fantraxapi/player_mapping.py
│       └── config/player_mappings.yaml
└── displays against:
    └── roster from st.session_state
```

### Player Mapping Update Flow
```
scripts/update_player_mappings.py
├── scrapes from:
│   ├── FFScout (fantraxapi/providers/ffscout/scout_picks_rosters.py)
│   ├── Fantrax rosters (fantraxapi/fantrax.py)
│   └── SofaScore lineups (data/sofascore/lineups/*.json)
└── updates:
    └── config/player_mappings.yaml
```

---

## Data Flow Diagrams

### Authentication & Roster Loading
```
User exports cookies from browser (Cookie-Editor extension)
  ↓ pastes or uploads JSON
app.py (auth section, lines 555-615)
  ↓ reads cookies
utils/cookie_import.py (read_auth_file)
  ↓ parses cookies + storage
utils/auth_helpers.py (load_requests_session_from_artifacts)
  ↓ validates and builds session
requests.Session
  ↓ stores in st.session_state["auth_artifacts"]
User selects league and team
  ↓
fantraxapi/fantrax.py (FantraxAPI)
  ↓ api.roster_info(team_id)
Fantrax API
  ↓ returns roster data
app.py (display section)
  ↓ shows dataframe
User sees roster
```

### Lineup Intelligence
```
User runs esd_export_schedule_and_lineups_v2.py
  ↓
SofaScore API
  ↓ fetches schedules & lineups
data/sofascore/*.csv, *.json
  ↓
User clicks "Load Lineup Predictions"
  ↓
lineup_intelligence_page.py
  ↓ reads files
  ↓ filters to next match per team
  ↓ maps SofaScore IDs to Fantrax IDs
PlayerMappingManager
  ↓ returns mappings
lineup_intelligence_page.py
  ↓ builds display rows
  ↓ compares with roster
st.dataframe
  ↓
User sees recommendations
```

### Player Swap
```
User selects players in app.py
  ↓
Click "Execute Swap"
  ↓
app.py (swap handler)
  ↓
fantraxapi.fantrax.FantraxAPI.swap_players()
  ↓ POST request
Fantrax API
  ↓ success/error
app.py
  ↓ reloads roster
User sees updated roster
```

---

## Critical Path Files

### Must-Have for Basic Functionality
1. `/Users/hogan/FantraxAPI/apps/auth_login/app.py` - Main UI
2. `/Users/hogan/FantraxAPI/fantraxapi/fantrax.py` - API client
3. `/Users/hogan/FantraxAPI/utils/auth_helpers.py` - Authentication
4. `/Users/hogan/FantraxAPI/utils/cookie_import.py` - Cookie parsing
5. `/Users/hogan/FantraxAPI/fantraxapi/models.py` - Data structures

### Must-Have for Lineup Intelligence
5. `/Users/hogan/FantraxAPI/apps/auth_login/pages/lineup_intelligence_page.py` - Lineup UI
6. `/Users/hogan/FantraxAPI/esd_export_schedule_and_lineups_v2.py` - Data fetcher
7. `/Users/hogan/FantraxAPI/fantraxapi/player_mapping.py` - Player mappings
8. `/Users/hogan/FantraxAPI/config/player_mappings.yaml` - Mapping data

### Supporting But Important
9. `/Users/hogan/FantraxAPI/apps/auth_login/login.py` - User account management (not Fantrax auth)
10. `/Users/hogan/FantraxAPI/fantraxapi/providers/sofascore/client.py` - SofaScore API
11. `/Users/hogan/FantraxAPI/scripts/update_player_mappings.py` - Mapping updates
12. `/Users/hogan/FantraxAPI/fantraxapi/logging_config.py` - Logging setup

**Note**: `/Users/hogan/FantraxAPI/apps/auth_login/login.py` handles user account login for the app itself (user manager), NOT Fantrax authentication. Fantrax authentication is handled via cookies in `app.py` and `utils/auth_helpers.py`.

---

## Environment & Configuration

### Required Environment
- **Python**: 3.10+
- **Dependencies**: See `/Users/hogan/FantraxAPI/apps/auth_login/requirements.txt`
- **Browser**: Chrome/Firefox (for Selenium)

### Key Environment Variables
- None required (uses file-based authentication)

### Configuration Files
1. `/Users/hogan/FantraxAPI/config/player_mappings.yaml` - Player mappings
2. `/Users/hogan/FantraxAPI/config/team_mappings.yaml` - Team mappings
3. `/Users/hogan/FantraxAPI/config/leagues.yaml` - League settings
4. `/Users/hogan/FantraxAPI/config/fantrax_leagues.yaml` - User leagues

### Data Directories
1. `/Users/hogan/FantraxAPI/data/sofascore/schedules/` - Match schedules
2. `/Users/hogan/FantraxAPI/data/sofascore/lineups/` - Lineup data
3. `/Users/hogan/FantraxAPI/data/logs/` - Application logs

---

## Deployment & Running

### Local Development
```bash
# Navigate to project root
cd /Users/hogan/FantraxAPI

# Run main app
streamlit run apps/auth_login/app.py

# Fetch lineup data
python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups

# Update player mappings
python scripts/update_player_mappings.py
```

### Daily Workflow
1. **Morning**: Fetch predicted lineups
   ```bash
   python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups
   ```

2. **Open App**: 
   ```bash
   streamlit run apps/auth_login/app.py
   ```

3. **Use App**:
   - Authenticate (cookies or Selenium)
   - Load roster
   - View lineup intelligence
   - Make roster changes

4. **Pre-Match**: Re-fetch for confirmed lineups (1-2 hours before kickoff)

---

## Documentation Files

### User-Facing Guides
1. `/Users/hogan/FantraxAPI/apps/auth_login/WORKFLOW_GUIDE.md` - Step-by-step user guide
2. `/Users/hogan/FantraxAPI/apps/auth_login/QUICK_START.md` - Quick start guide
3. `/Users/hogan/FantraxAPI/apps/auth_login/README_NAVIGATION.md` - Navigation structure

### Technical Documentation
4. `/Users/hogan/FantraxAPI/docs/APPLICATION_ARCHITECTURE.md` - This file
5. `/Users/hogan/FantraxAPI/docs/ESD_LINEUP_FETCHER.md` - Lineup fetching script
6. `/Users/hogan/FantraxAPI/docs/LINEUP_INTELLIGENCE_INTEGRATION.md` - Lineup intelligence details
7. `/Users/hogan/FantraxAPI/docs/LINEUP_PREDICTION_SELECTION.md` - Prediction filtering logic
8. `/Users/hogan/FantraxAPI/docs/LINEUP_WORKFLOW_SUMMARY.md` - Lineup automation summary

### Design Decisions
9. `/Users/hogan/FantraxAPI/apps/auth_login/DESIGN_DECISION.md` - Self-contained page rationale
10. `/Users/hogan/FantraxAPI/apps/auth_login/AUTOMATIC_ROSTER_LOADING.md` - Roster loading behavior

---

## Summary

This architecture document provides a complete map of the Fantrax API application, organized by user flow and feature. Each section includes explicit file paths, dependencies, and data flows.

### Quick Reference Matrix

| Feature | Entry Point | Key Files | Data Sources | Configuration |
|---------|------------|-----------|--------------|---------------|
| **Authentication** | `app.py` (lines 555-683) | `utils/auth_helpers.py`<br>`utils/cookie_import.py` | Cookie JSON from Cookie-Editor extension | None |
| **Roster Display** | `app.py` (lines 195-380) | `fantraxapi/fantrax.py`<br>`fantraxapi/models.py` | Fantrax API | None |
| **Lineup Intelligence** | `pages/lineup_intelligence_page.py` | `fantraxapi/player_mapping.py`<br>`esd_export_schedule_and_lineups_v2.py` | `data/sofascore/*.csv`<br>`data/sofascore/*.json` | `config/player_mappings.yaml` |
| **Player Swaps** | `app.py` (lines 520-650) | `fantraxapi/fantrax.py` | Fantrax API | None |
| **Player Drops** | `app.py` (lines 652-750) | `fantraxapi/fantrax.py` | Fantrax API | None |
| **FAAB/Waivers** | `app.py` (lines 752-900) | `fantraxapi/fantrax.py` | Fantrax API | None |
| **Player Mapping** | `scripts/update_player_mappings.py` | `fantraxapi/player_mapping.py` | FFScout, SofaScore | `config/player_mappings.yaml` |

### Future Enhancements

Potential areas for expansion (not yet implemented):
- Automated roster optimization based on lineup predictions
- Multi-league support with quick switching
- Historical lineup accuracy tracking
- Push notifications for confirmed lineups
- Trade analysis and recommendations
- Projected points based on lineup status

---

**Last Updated**: November 18, 2025
**Version**: 1.0
**Maintainer**: Application team

