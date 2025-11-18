# Lineup Scraping & Integration Workflow

**Last Updated:** November 17, 2025

This document describes the complete workflow for obtaining confirmed or predicted lineups from external sources (primarily SofaScore, with FFScout as a supplementary data source for player identification) and integrating them with Fantrax methods.

---

## Table of Contents

1. [Overview](#overview)
2. [Data Sources](#data-sources)
3. [Workflow Architecture](#workflow-architecture)
4. [Component Details](#component-details)
5. [Lineup Status States](#lineup-status-states)
6. [Integration with Fantrax](#integration-with-fantrax)
7. [Usage Examples](#usage-examples)
8. [File Reference](#file-reference)

---

## Overview

The lineup automation system provides end-to-end functionality for:

1. **Discovering** upcoming Premier League matches
2. **Polling** for lineup releases from SofaScore
3. **Tracking** lineup status through confirmation windows
4. **Mapping** players from SofaScore IDs to Fantrax IDs
5. **Synchronizing** confirmed lineups to Fantrax rosters

### Key Features

- Async/await architecture for concurrent polling
- Multi-stage status tracking (preliminary → confirmed → final)
- Automatic retry logic with exponential backoff
- Player mapping with fuzzy matching fallback
- Dry-run mode for testing without making changes
- Comprehensive logging and error handling

---

## Data Sources

### Primary: SofaScore API

**Purpose:** Real-time lineup data scraping

**Endpoints Used:**
- `GET /api/v1/sport/football/scheduled-events/{date}` - Discover matches
- `GET /api/v1/event/{event_id}/lineups` - Fetch lineup data
- `GET /api/v1/event/{event_id}` - Match metadata

**Data Captured:**
- Starting XI (11 players per team)
- Substitutes (typically 7-9 players per team)
- Formation (e.g., "4-3-3", "4-4-2")
- Player details: name, shirt number, position, captain status
- Missing players with injury/suspension reasons
- Coach information
- Match metadata: teams, kickoff time, tournament info

**Reliability:**
- Lineups typically released 60-75 minutes before kickoff
- Confirmed flag indicates official team sheet release
- API returns 404 when lineups not yet available
- Occasional truncated responses (handled with retry logic)

**Implementation Files:**
- `fantraxapi/providers/sofascore/client.py` - API client
- `fantraxapi/providers/sofascore/discover.py` - Match discovery
- `fantraxapi/providers/sofascore/poll.py` - Polling logic
- `fantraxapi/providers/sofascore/normalize.py` - Data normalization

### Supplementary: FFScout

**Purpose:** Player identification and mapping validation

**Source:** FFScout predicted/scout lineups web scraping

**Use Cases:**
1. **Player Mapping:** Helps identify players by matching against FFScout roster data
2. **Validation:** Cross-reference SofaScore lineups with FFScout predictions
3. **Future Backup:** Infrastructure exists to use FFScout as fallback (not currently implemented)

**Data Captured:**
- Team rosters by position (GK, DEF, MID, FWD)
- Player display names and full names
- Premier League photo IDs
- Team codes and next match info

**Limitations:**
- Only covers likely starters/regular squad players
- Updated weekly, not real-time
- Requires web scraping (more fragile than API)
- Manual trigger required (not automated)

**Implementation Files:**
- `fantraxapi/providers/ffscout/scout_picks_rosters.py` - Web scraper
- `scripts/update_player_mappings.py` - Uses FFScout data for player mapping

**Current Status:**
- FFScout is **NOT** currently used as a live backup for lineup scraping
- Only used for building/updating player ID mappings
- Infrastructure exists to expand its role in the future

---

## Workflow Architecture

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    1. DISCOVERY PHASE                           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ get_season_events() or get_watchlist()                    │  │
│  │ → Queries SofaScore API for upcoming matches             │  │
│  │ → Filters for Premier League (tournament_id=17)          │  │
│  │ → Returns Event objects with match metadata              │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    2. POLLING PHASE                             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ poll_events()                                              │  │
│  │ → Starts polling 90 minutes before kickoff               │  │
│  │ → Checks every 60 seconds for lineup availability        │  │
│  │ → Validates 11 starters per team (confirmed check)       │  │
│  │ → Handles 404/timeout/rate-limit errors gracefully       │  │
│  │ → Returns raw lineup data when confirmed                 │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                  3. NORMALIZATION PHASE                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ normalize_lineup_data()                                    │  │
│  │ → Converts SofaScore format to LineupRecord models       │  │
│  │ → Maps SofaScore player IDs → Fantrax IDs               │  │
│  │ → Enriches with metadata (tournament, kickoff, etc.)     │  │
│  │ → Returns List[LineupRecord]                             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                  4. STATUS TRACKING PHASE                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ determine_lineup_status()                                  │  │
│  │ → Assigns status based on timing and confirmation        │  │
│  │ → PRELIMINARY: >75min before kickoff                     │  │
│  │ → PENDING_CONFIRMATION: <75min, not confirmed            │  │
│  │ → CONFIRMED: <75min, confirmed by source                 │  │
│  │ → FINAL: >5min after kickoff                             │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                  5. SYNCHRONIZATION PHASE                       │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ LineupSynchronizer.sync_lineup()                           │  │
│  │ → Only syncs CONFIRMED or FINAL lineups                  │  │
│  │ → Determines changes needed vs current Fantrax lineup    │  │
│  │ → Validates changes (formation, position rules)          │  │
│  │ → Executes via SubsService.set_lineup_by_ids()           │  │
│  │ → Logs results and handles errors                        │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Detailed Component Flow

```
SofaScore API
     ↓
[discover.py] ────→ Event objects (match schedule)
     ↓
[poll.py] ────────→ Raw lineup JSON (when available)
     ↓
[normalize.py] ───→ LineupRecord objects
     ↓
[PlayerMappingManager] ─→ Adds Fantrax IDs
     ↓
[status.py] ──────→ Assigns LineupStatus
     ↓
[automation.py] ──→ Orchestrates processing
     ↓
[fantrax_sync.py] → Syncs to Fantrax
     ↓
[subs.py] ────────→ Executes roster changes
     ↓
Fantrax Platform (updated rosters)
```

---

## Component Details

### 1. Discovery (`fantraxapi/providers/sofascore/discover.py`)

**Key Functions:**

#### `get_season_events(season: str, verbose: bool) -> List[Event]`
Fetches all Premier League matches for an entire season.

**Process:**
1. Calculates season date range (Aug 1 - May 31)
2. Iterates through each date
3. Fetches scheduled events from SofaScore API
4. Filters for Premier League (tournament_id=17)
5. Deduplicates matches that appear on multiple days
6. Returns sorted list of Event objects

**Usage:**
```python
events = await get_season_events(season="2025-26", verbose=True)
# Returns all PL matches for the season
```

#### `get_watchlist(window_minutes: int, date_from: datetime, date_to: datetime) -> List[Event]`
Gets matches within the polling window (upcoming in next few days).

**Process:**
1. Fetches events for date range (default: today + 3 days)
2. Filters for matches within polling window (default: 90min before kickoff)
3. Also includes matches starting in next 6 hours
4. Returns events to actively monitor

**Usage:**
```python
events = await get_watchlist(window_minutes=90)
# Returns matches to poll for lineups
```

#### `Event` Class
Data model for match metadata:
- `event_id`: SofaScore match ID
- `tournament_id`: Tournament ID (17 for Premier League)
- `tournament_name`: "Premier League"
- `kickoff_utc`: Match start time (UTC)
- `home_team`: Dict with team info (id, name, etc.)
- `away_team`: Dict with team info

---

### 2. Polling (`fantraxapi/providers/sofascore/poll.py`)

**Key Functions:**

#### `xi_is_confirmed(lineup_data: dict) -> bool`
Validates that lineup data contains 11 confirmed starters per team.

**Logic:**
```python
for side in ["home", "away"]:
	players = lineup_data.get(side, {}).get("players", [])
	starters = sum(1 for p in players if not p.get("substitute", False))
	if starters != 11:
		return False
return True
```

#### `poll_event(client: httpx.AsyncClient, event: Event, poll_interval: int, max_minutes: int) -> Optional[Dict]`
Polls a single event until lineups are confirmed or timeout.

**Process:**
1. Constructs lineup URL: `/api/v1/event/{event_id}/lineups`
2. Polls every `poll_interval` seconds (default: 60)
3. Checks `xi_is_confirmed()` on each response
4. Returns lineup data when confirmed
5. Handles HTTP errors (404=not ready, 429=rate limit, 500/503=server issues)
6. Times out after `max_minutes` (default: 120)
7. Enriches data with event metadata before returning

**Return Format:**
```python
{
	"event_id": 12345,
	"tournament_id": 17,
	"tournament_name": "Premier League",
	"kickoff_utc": datetime(...),
	"home_team": {"id": 35, "name": "Arsenal", ...},
	"away_team": {"id": 33, "name": "Chelsea", ...},
	"captured_at_utc": datetime(...),
	"home": {
		"formation": "4-3-3",
		"coach": {"name": "Mikel Arteta"},
		"players": [
			{
				"player": {"id": 123, "name": "Player Name", "position": "M"},
				"substitute": False,
				"captain": False,
				"shirtNumber": 8
			},
			...
		]
	},
	"away": { ... }
}
```

#### `poll_events(events: List[Event], poll_interval: int, max_minutes: int) -> List[Dict]`
Polls multiple events concurrently using `asyncio.gather()`.

**Benefits:**
- Monitors multiple matches simultaneously
- More efficient than sequential polling
- Each event has independent timeout

---

### 3. Normalization (`fantraxapi/providers/sofascore/normalize.py`)

#### `normalize_lineup_data(data: Dict, player_mapping: Optional[PlayerMappingManager]) -> List[LineupRecord]`

Converts raw SofaScore API response into structured LineupRecord objects.

**Process:**

1. **Extract Event Metadata:**
   ```python
   event_id = data["event_id"]
   kickoff_utc = data["kickoff_utc"]
   tournament_info = {...}
   ```

2. **Process Each Team (home/away):**
   ```python
   for side in ["home", "away"]:
	   team_data = data[f"{side}_team"]
	   lineup_data = data.get(side, {})
   ```

3. **Parse Players:**
   ```python
   for player_data in lineup_data.get("players", []):
	   player = Player(
		   id=player_data["player"]["id"],
		   name=player_data["player"]["name"],
		   position=player_data["player"]["position"],
		   substitute=player_data.get("substitute", False),
		   captain=player_data.get("captain", False),
		   shirtNumber=player_data.get("shirtNumber")
	   )
   ```

4. **Map to Fantrax IDs:**
   ```python
   # Try by SofaScore ID first
   mapping = player_mapping.get_by_sofascore_id(player.id)
   
   # Fallback to name matching
   if not mapping:
	   mapping = player_mapping.get_by_name(player.name)
   
   fantrax_id = mapping.fantrax_id if mapping else None
   fantrax_name = mapping.fantrax_name if mapping else None
   ```

5. **Create LineupRecord:**
   ```python
   record = LineupRecord(
	   event_id=event_id,
	   kickoff_utc=kickoff_utc,
	   team_id=team.team_id,
	   team_name=team.team_name,
	   player_id=player.id,
	   player_name=player.name,
	   fantrax_id=fantrax_id,
	   fantrax_name=fantrax_name,
	   is_sub=player.substitute,
	   position=player.position,
	   ...
   )
   ```

**Output:** `List[LineupRecord]` - One record per player across both teams

---

### 4. Player Mapping (`fantraxapi/player_mapping.py`)

#### `PlayerMappingManager`

Manages bidirectional mapping between SofaScore and Fantrax player IDs.

**Data Structure (YAML):**
```yaml
mappings:
  - fantrax_id: "abc123"
	fantrax_name: "Martin Ødegaard"
	sofascore_id: 851057
	sofascore_name: "Martin Ødegaard"
	aliases:
	  - "Odegaard"
	  - "M. Ødegaard"
```

**Key Methods:**

```python
# Lookup by SofaScore ID
mapping = manager.get_by_sofascore_id(851057)

# Lookup by Fantrax ID
mapping = manager.get_by_fantrax_id("abc123")

# Lookup by name (with fuzzy matching)
mapping = manager.get_by_name("Odegaard")

# Add new mapping
manager.add_mapping(
	fantrax_id="xyz789",
	fantrax_name="Bukayo Saka",
	sofascore_id=845368,
	sofascore_name="Bukayo Saka"
)

# Save to file
manager.save()
```

**Mapping Sources:**

1. **Existing mappings** (loaded from YAML)
2. **FFScout data** (name + team matching)
3. **SofaScore ratings** (player ID matching)
4. **Manual additions** (via interactive prompts)

**Update Process:**

Run `scripts/update_player_mappings.py` to refresh mappings:

```bash
python scripts/update_player_mappings.py \
	--league-id YOUR_LEAGUE_ID \
	--data-dir data \
	--output-file config/player_mappings.yaml \
	--interactive
```

**Process:**
1. Loads existing mappings from YAML
2. Fetches current Fantrax roster
3. Loads FFScout scout picks data
4. Loads SofaScore player ratings
5. For each Fantrax player:
   - Check existing mapping
   - Try exact name match with FFScout
   - Try fuzzy name match (threshold: 75)
   - Try exact name match with SofaScore
   - Try fuzzy name match with SofaScore
   - Prompt for manual match if uncertain (interactive mode)
6. Saves updated mappings to YAML

---

### 5. Status Tracking (`fantraxapi/lineups/status.py`)

#### Lineup Status States

```python
class LineupStatus(str, Enum):
	PRELIMINARY = "preliminary"           # >75min before kickoff
	PENDING_CONFIRMATION = "pending_confirmation"  # <75min, not confirmed
	CONFIRMED = "confirmed"               # <75min, confirmed
	FINAL = "final"                       # >5min after kickoff
	INVALID = "invalid"                   # Invalidated/error
```

#### `determine_lineup_status(lineup: LineupRecord, current_time: datetime) -> LineupStatus`

Assigns appropriate status based on timing and confirmation.

**Logic:**

```python
time_to_kickoff = lineup.kickoff_utc - current_time
confirmation_window = timedelta(minutes=75)
final_window = timedelta(minutes=5)

# Match has started
if current_time > lineup.kickoff_utc + final_window:
	return LineupStatus.FINAL

# Within confirmation window
if time_to_kickoff <= confirmation_window:
	if lineup.is_confirmed:
		return LineupStatus.CONFIRMED
	return LineupStatus.PENDING_CONFIRMATION

# Outside confirmation window
return LineupStatus.PRELIMINARY
```

**Status Transitions:**

```
PRELIMINARY
    ↓ (75min before kickoff)
PENDING_CONFIRMATION
    ↓ (lineup confirmed by source)
CONFIRMED
    ↓ (5min after kickoff)
FINAL
```

**Usage in Automation:**

- **PRELIMINARY:** Store for testing/comparison, don't sync to Fantrax
- **CONFIRMED:** Sync to Fantrax (primary trigger)
- **FINAL:** Lineup locked, no further changes allowed
- **INVALID:** Skip processing

---

### 6. Automation Controller (`fantraxapi/lineups/automation.py`)

#### `LineupAutomation`

Orchestrates the entire lineup processing workflow.

**Initialization:**

```python
automation = LineupAutomation(
	fantrax=fantrax_api,              # FantraxAPI instance
	player_mapping=player_mapping,     # PlayerMappingManager
	output_dir=Path("data/lineups"),
	dry_run=False,                     # Set True to preview changes
	test_mode=False                    # Set True for testing preliminary lineups
)
```

**Key Method:** `process_lineup(data: Dict, current_time: datetime) -> bool`

**Process:**

1. **Normalize Data:**
   ```python
   lineup = normalize_lineup_data(data, self.player_mapping)
   ```

2. **Check Duplicates:**
   ```python
   if event_id in self.processed_lineups:
	   prev_lineup = self.processed_lineups[event_id]
	   if prev_lineup.status == lineup.status:
		   return True  # No change, skip
   ```

3. **Store Lineup:**
   ```python
   self.processed_lineups[event_id] = lineup
   ```

4. **Handle Based on Mode:**

   **Test Mode:**
   ```python
   if self.test_mode:
	   result = self.tester.process_lineup(lineup)
	   # Compares preliminary vs confirmed lineups
	   # Tracks prediction accuracy
   ```

   **Live Mode:**
   ```python
   elif lineup.is_confirmed or (self.dry_run and lineup.is_preliminary):
	   success = self.synchronizer.sync_lineup(lineup)
	   # Syncs to Fantrax
   ```

   **Preliminary:**
   ```python
   else:
	   # Just store, don't sync yet
   ```

**Run Method:** `run(poll_interval: int, max_runtime: int)`

Continuous polling loop (not yet implemented - TODO).

---

### 7. Fantrax Synchronization (`fantraxapi/lineups/fantrax_sync.py`)

#### `LineupSynchronizer`

Handles synchronization between confirmed SofaScore lineups and Fantrax rosters.

**Initialization:**

```python
syncer = LineupSynchronizer(
	fantrax=fantrax_api,
	dry_run=False,   # Set True to preview changes without executing
	logger=logger
)
```

**Key Methods:**

#### `sync_lineup(lineup: LineupRecord, team_id: str) -> bool`

Main entry point for synchronizing a lineup.

**Process:**

1. **Validate Confirmation:**
   ```python
   if not lineup.is_confirmed:
	   self.logger.warning("Cannot sync unconfirmed lineup")
	   return False
   ```

2. **Determine Changes:**
   ```python
   changes = self.determine_changes(lineup)
   if not changes:
	   self.logger.info("No changes needed")
	   return True
   ```

3. **Validate Changes:**
   ```python
   if not self.validate_changes(changes):
	   self.logger.error("Invalid changes detected")
	   return False
   ```

4. **Execute Changes:**
   ```python
   success = self.execute_changes(changes, team_id)
   self.changes.extend(changes)  # Store for reference
   return success
   ```

#### `determine_changes(lineup: LineupRecord) -> List[LineupChange]`

Compares confirmed lineup with current Fantrax roster.

**Process:**

1. **Get Current Fantrax Lineup:**
   ```python
   current = self.fantrax.get_team_lineup(team_id)
   current_starters = {p["id"] for p in current["starters"]}
   current_subs = {p["id"] for p in current["subs"]}
   ```

2. **Check Each Player:**
   ```python
   for player in team.players:
	   if not player.fantrax_id:
		   continue  # Can't sync without Fantrax ID
	   
	   current_starting = player.fantrax_id in current_starters
	   should_start = not player.is_sub
	   
	   if current_starting != should_start:
		   # Need to make a change
   ```

3. **Create Change Objects:**
   ```python
   if should_start:
	   # Find sub to swap out
	   changes.append(LineupChange(
		   player_in=player,
		   player_out=sub_to_replace,
		   reason="Move to starting lineup"
	   ))
   ```

#### `execute_changes(changes: List[LineupChange], team_id: str) -> bool`

Executes lineup changes via Fantrax API.

**Methods:**

**Option 1: Bulk Update (Preferred)**
```python
# Use SubsService to set entire XI at once
starter_ids = [player.fantrax_id for player in starters]

result = self.fantrax.subs.set_lineup_by_ids(
	league_id=self.fantrax.league_id,
	team_id=team_id,
	desired_starter_ids=starter_ids,
	best_effort=True,
	verify_each=True
)
```

**Option 2: Sequential Swaps**
```python
# Swap players one at a time
for change in changes:
	result = self.fantrax.swap_players(
		team_id=team_id,
		starter_id=change.player_in.fantrax_id,
		bench_id=change.player_out.fantrax_id
	)
```

**Error Handling:**
- Locked players (deadline passed)
- Position validation failures
- Formation rule violations
- Network errors
- Rate limiting

---

## Lineup Status States

### State Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      PRELIMINARY                            │
│  • Lineup data exists but >75min before kickoff             │
│  • May be prediction/rumor, not official                    │
│  • Action: Store for testing, don't sync to Fantrax        │
└─────────────────────────────────────────────────────────────┘
                         ↓
              (75 minutes before kickoff)
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                 PENDING_CONFIRMATION                        │
│  • Within 75min window but not officially confirmed         │
│  • Waiting for team sheet release                           │
│  • Action: Continue polling, don't sync yet                 │
└─────────────────────────────────────────────────────────────┘
                         ↓
              (lineup confirmed by source)
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                      CONFIRMED                              │
│  • Official team sheet released                             │
│  • Both teams have 11 confirmed starters                    │
│  • Action: SYNC TO FANTRAX                                  │
└─────────────────────────────────────────────────────────────┘
                         ↓
              (5 minutes after kickoff)
                         ↓
┌─────────────────────────────────────────────────────────────┐
│                        FINAL                                │
│  • Match has started                                        │
│  • Lineup is locked                                         │
│  • Action: Archive, no further changes                      │
└─────────────────────────────────────────────────────────────┘
```

### Timing Windows

| Status | Time Window | Confirmation | Action |
|--------|------------|--------------|--------|
| PRELIMINARY | >75min before KO | Any | Store only |
| PENDING_CONFIRMATION | <75min before KO | Not confirmed | Poll & wait |
| CONFIRMED | <75min before KO | Confirmed | **Sync to Fantrax** |
| FINAL | >5min after KO | Confirmed | Archive |

### Confirmation Criteria

A lineup is considered "confirmed" when:

1. **Both teams have exactly 11 starters** (non-substitute players)
2. **SofaScore API returns complete data** (no 404/truncated responses)
3. **Within confirmation window** (<75min before kickoff)

**Validation Logic:**

```python
def is_confirmed(lineup_data: dict) -> bool:
	for side in ["home", "away"]:
		players = lineup_data.get(side, {}).get("players", [])
		starters = [p for p in players if not p.get("substitute", False)]
		if len(starters) != 11:
			return False
	return True
```

---

## Integration with Fantrax

### Architecture

```
LineupSynchronizer
        ↓
  [Determine Changes]
        ↓
   [Validate Changes]
        ↓
  [Execute via SubsService]
        ↓
    FantraxAPI
        ↓
  /fantasyLineupChange
  /checkLineupChange
        ↓
   Fantrax Platform
```

### Fantrax Methods Used

#### 1. `SubsService.set_lineup_by_ids()`

**Primary method for bulk lineup updates.**

Located in: `fantraxapi/subs.py`

**Signature:**
```python
def set_lineup_by_ids(
	self,
	*,
	league_id: str,
	team_id: str,
	desired_starter_ids: List[str],  # Fantrax IDs of 11 starters
	best_effort: bool = True,        # Continue on errors
	verify_each: bool = True,        # Validate each swap
	pos_overrides: Optional[Dict[str, str]] = None,  # Force positions
	apply_to_future: bool = False,   # Apply to future periods
	roster_limit_period: Optional[int] = None
) -> Dict[str, Any]
```

**Process:**

1. **Load Current Roster:**
   ```python
   roster = self._get_team_roster(league_id, team_id, roster_limit_period)
   ```

2. **Build Field Map:**
   ```python
   # Maps position slots to player IDs
   field_map = {
	   "0": starter_1_fantrax_id,   # GK
	   "1": starter_2_fantrax_id,   # DEF
	   "2": starter_3_fantrax_id,   # DEF
	   ...
	   "10": starter_11_fantrax_id, # FWD
	   "11": sub_1_fantrax_id,
	   ...
   }
   ```

3. **Validate Formation:**
   ```python
   # Ensures position requirements met (e.g., 1 GK, 3+ DEF, etc.)
   ```

4. **Confirm & Execute:**
   ```python
   # Two-phase commit:
   # 1. confirm_or_execute_lineup(..., do_finalize=False)
   # 2. confirm_or_execute_lineup(..., do_finalize=True)
   ```

5. **Handle Warnings:**
   ```python
   # If response has msgType="WARNING", acknowledge and retry
   ```

**Advantages:**
- Atomic operation (all or nothing)
- Automatic position validation
- Handles formation rules
- Confirms before executing

**Disadvantages:**
- Complex implementation
- Requires full lineup (can't swap single player)

#### 2. `FantraxAPI.swap_players()`

**Alternative method for individual swaps.**

Located in: `fantraxapi/fantrax.py`

**Signature:**
```python
def swap_players(
	self,
	team_id: str,
	starter_id: str,  # Player to move to starting XI
	bench_id: str     # Player to move to bench
) -> dict
```

**Process:**

1. **Validate Players:**
   ```python
   # Ensure both players exist on roster
   # Check positions are compatible
   ```

2. **Execute Swap:**
   ```python
   result = self.subs.swap_players(
	   league_id=self.league_id,
	   team_id=team_id,
	   out_id=bench_id,
	   in_id=starter_id
   )
   ```

3. **Verify Result:**
   ```python
   if result.get("fantasyResponse", {}).get("msgType") == "SUCCESS":
	   return True
   ```

**Advantages:**
- Simple API
- Good for single changes
- Fast execution

**Disadvantages:**
- No bulk operation
- Must execute multiple swaps sequentially
- No automatic formation validation

### Synchronization Strategies

#### Strategy 1: Full Lineup Replacement (Recommended)

**Use when:** Significant lineup changes (3+ swaps needed)

**Implementation:**
```python
# Collect all starter IDs from confirmed lineup
starter_ids = [
	player.fantrax_id 
	for player in lineup.starters
	if player.fantrax_id
]

# Set entire lineup at once
result = fantrax.subs.set_lineup_by_ids(
	league_id=league_id,
	team_id=team_id,
	desired_starter_ids=starter_ids,
	best_effort=True,
	verify_each=True
)
```

**Pros:**
- Handles complex changes atomically
- Automatic validation
- Single API call

**Cons:**
- Requires all 11 starters mapped
- More complex error handling

#### Strategy 2: Sequential Swaps

**Use when:** Minor lineup changes (1-2 swaps)

**Implementation:**
```python
changes = determine_changes(confirmed_lineup, current_lineup)

for change in changes:
	result = fantrax.swap_players(
		team_id=team_id,
		starter_id=change.player_in.fantrax_id,
		bench_id=change.player_out.fantrax_id
	)
	
	if not result["success"]:
		handle_error(result)
```

**Pros:**
- Simple implementation
- Clear error attribution
- Works with partial mappings

**Cons:**
- Multiple API calls
- Race conditions possible
- Manual validation needed

### Error Handling

Common errors and resolutions:

| Error | Cause | Resolution |
|-------|-------|------------|
| `LINEUP_LOCKED` | Deadline passed | Apply to future period |
| `INVALID_POSITION` | Position rules violated | Use pos_overrides |
| `PLAYER_NOT_FOUND` | Bad Fantrax ID | Check player mapping |
| `FORMATION_INVALID` | Not enough players at position | Review starter selection |
| `WARNING` with 12 starters | Needs confirmation | Acknowledge and retry |

**Example Error Handling:**

```python
try:
	result = subs.set_lineup_by_ids(...)
	
	fr = result.get("fantasyResponse", {})
	
	if fr.get("msgType") == "WARNING":
		# Acknowledge warning and retry
		result = subs.set_lineup_by_ids(..., acknowledge=True)
	
	if fr.get("msgType") == "ERROR":
		# Check for specific errors
		if "deadline" in fr.get("msgBody", "").lower():
			# Try applying to future period
			result = subs.set_lineup_by_ids(..., apply_to_future=True)
	
except Exception as e:
	logger.error(f"Sync failed: {e}")
	# Store failed lineup for manual review
```

---

## Usage Examples

### Example 1: Watch for Today's Lineups

```python
import asyncio
from fantraxapi.providers.sofascore.discover import get_watchlist
from fantraxapi.providers.sofascore.poll import poll_events
from fantraxapi.lineups.sofascore_normalize import normalize_lineup_data
from fantraxapi.player_mapping import PlayerMappingManager

async def watch_todays_lineups():
	# Get matches to watch
	events = await get_watchlist(window_minutes=90)
	
	if not events:
		print("No matches to watch today")
		return
	
	print(f"Watching {len(events)} matches:")
	for e in events:
		print(f"  - {e.home_team['name']} vs {e.away_team['name']}")
	
	# Poll for lineups
	results = await poll_events(
		events,
		poll_interval=60,
		max_minutes=120
	)
	
	if not results:
		print("No lineups confirmed")
		return
	
	# Process results
	player_mapping = PlayerMappingManager("config/player_mappings.yaml")
	
	for data in results:
		records = normalize_lineup_data(data, player_mapping)
		
		print(f"\nLineup for {data['home_team']['name']} vs {data['away_team']['name']}:")
		
		for record in records:
			if not record.is_sub:
				fantrax_info = f" → {record.fantrax_name}" if record.fantrax_id else " [UNMAPPED]"
				print(f"  {record.player_name} ({record.position}){fantrax_info}")

asyncio.run(watch_todays_lineups())
```

### Example 2: Sync Confirmed Lineup to Fantrax

```python
import asyncio
from fantraxapi import FantraxAPI
from fantraxapi.lineups.fantrax_sync import LineupSynchronizer
from fantraxapi.lineups.status import determine_lineup_status, LineupStatus
from fantraxapi.player_mapping import PlayerMappingManager

async def sync_lineup_to_fantrax(lineup_data: dict, team_id: str):
	# Initialize components
	api = FantraxAPI(league_id="YOUR_LEAGUE_ID")
	player_mapping = PlayerMappingManager("config/player_mappings.yaml")
	
	# Normalize lineup
	from fantraxapi.lineups.sofascore_normalize import normalize_lineup_data
	records = normalize_lineup_data(lineup_data, player_mapping)
	
	# Check status
	status = determine_lineup_status(records[0])
	
	if status not in [LineupStatus.CONFIRMED, LineupStatus.FINAL]:
		print(f"Lineup not confirmed yet (status: {status})")
		return False
	
	# Initialize synchronizer
	syncer = LineupSynchronizer(
		fantrax=api,
		dry_run=False  # Set True to preview without executing
	)
	
	# Sync lineup
	success = syncer.sync_lineup(records[0], team_id)
	
	if success:
		print("✓ Lineup synced successfully")
	else:
		print("✗ Lineup sync failed")
		print(f"Errors: {syncer.errors}")
	
	return success

# Usage
lineup_data = { ... }  # From SofaScore polling
asyncio.run(sync_lineup_to_fantrax(lineup_data, "your_team_id"))
```

### Example 3: Full Automation Pipeline

```python
#!/usr/bin/env python
"""
Complete lineup automation pipeline.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from fantraxapi import FantraxAPI
from fantraxapi.lineups.automation import LineupAutomation
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.providers.sofascore.discover import get_watchlist
from fantraxapi.providers.sofascore.poll import poll_events

async def run_lineup_automation(
	league_id: str,
	team_id: str,
	dry_run: bool = False
):
	"""
	Complete automation pipeline:
	1. Discover matches
	2. Poll for lineups
	3. Normalize data
	4. Sync to Fantrax
	"""
	# Setup
	logger = logging.getLogger(__name__)
	output_dir = Path("data/lineups")
	output_dir.mkdir(parents=True, exist_ok=True)
	
	# Initialize components
	fantrax = FantraxAPI(league_id=league_id)
	player_mapping = PlayerMappingManager("config/player_mappings.yaml")
	
	automation = LineupAutomation(
		fantrax=fantrax,
		player_mapping=player_mapping,
		output_dir=output_dir,
		dry_run=dry_run,
		test_mode=False
	)
	
	logger.info("Starting lineup automation...")
	
	while True:
		try:
			# Get matches to watch
			events = await get_watchlist(window_minutes=90)
			
			if not events:
				logger.info("No matches in polling window")
				await asyncio.sleep(300)  # Check again in 5 minutes
				continue
			
			logger.info(f"Watching {len(events)} matches")
			
			# Poll for lineups
			results = await poll_events(
				events,
				poll_interval=60,
				max_minutes=120
			)
			
			if not results:
				logger.info("No confirmed lineups yet")
				await asyncio.sleep(300)
				continue
			
			# Process each lineup
			for data in results:
				success = await automation.process_lineup(
					data,
					current_time=datetime.now(timezone.utc)
				)
				
				if success:
					logger.info(f"✓ Processed lineup for event {data['event_id']}")
				else:
					logger.error(f"✗ Failed to process lineup for event {data['event_id']}")
			
			# Wait before next check
			await asyncio.sleep(300)
			
		except KeyboardInterrupt:
			logger.info("Stopping automation...")
			break
			
		except Exception as e:
			logger.exception(f"Error in automation loop: {e}")
			await asyncio.sleep(60)

if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	
	asyncio.run(run_lineup_automation(
		league_id="YOUR_LEAGUE_ID",
		team_id="YOUR_TEAM_ID",
		dry_run=True  # Set False for live mode
	))
```

### Example 4: CLI Lineup Watcher

The repository includes a ready-to-use CLI tool:

```bash
# Watch for lineups (dry run)
python watch_lineups.py \
	--window 90 \
	--interval 60 \
	--output-dir data/lineups \
	--test-mode

# Live mode (syncs to Fantrax)
python watch_lineups.py \
	--window 90 \
	--interval 60 \
	--output-dir data/lineups

# Look ahead mode (scan next 7 days)
python watch_lineups.py \
	--test-mode \
	--days-ahead 7 \
	--output-dir data/lineups
```

**Features:**
- Automatic discovery of matches
- Concurrent polling
- Player mapping with unmapped player reporting
- Parquet export for analysis
- Comprehensive logging

---

## File Reference

### Core Library (`fantraxapi/`)

#### Lineup Modules

| File | Purpose | Key Components |
|------|---------|----------------|
| `lineups/models.py` | Data models | `LineupRecord`, `TeamLineup`, `PlayerRecord`, `LineupStatus` |
| `lineups/status.py` | Status tracking | `determine_lineup_status()`, status constants |
| `lineups/automation.py` | Main controller | `LineupAutomation` class |
| `lineups/fantrax_sync.py` | Fantrax sync | `LineupSynchronizer`, `LineupChange` |
| `lineups/normalize.py` | Data normalization | `normalize_lineup_data()` |
| `lineups/testing.py` | Testing framework | `LineupTester`, accuracy tracking |
| `lineups/player_status_manager.py` | Player tracking | `PlayerStatusManager` |

#### Provider Modules

| File | Purpose | Key Components |
|------|---------|----------------|
| `providers/sofascore/client.py` | API client | `get_matches()`, `get_match_lineups()` |
| `providers/sofascore/discover.py` | Match discovery | `get_season_events()`, `get_watchlist()`, `Event` |
| `providers/sofascore/poll.py` | Lineup polling | `poll_event()`, `poll_events()`, `xi_is_confirmed()` |
| `providers/sofascore/normalize.py` | Normalization | `normalize_lineup_data()` |
| `providers/sofascore/models.py` | Data models | `Event`, `Player`, etc. |
| `providers/ffscout/scout_picks_rosters.py` | FFScout scraper | `scrape()`, `extract_team_block()` |

#### Core Modules

| File | Purpose | Key Components |
|------|---------|----------------|
| `fantrax.py` | Main API client | `FantraxAPI` class |
| `subs.py` | Substitution engine | `SubsService`, `set_lineup_by_ids()` |
| `player_mapping.py` | Player ID mapping | `PlayerMappingManager` |

### Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `watch_lineups.py` | CLI lineup watcher | `python watch_lineups.py --window 90` |
| `scripts/update_player_mappings.py` | Update player mappings | `python scripts/update_player_mappings.py --league-id XXX` |
| `scripts/map_lineups_to_fantrax.py` | Map lineup to Fantrax | Analysis/debugging tool |
| `scripts/test_lineup_retrieval.py` | Test SofaScore API | Debugging/development |

### Apps

| App | Purpose | Status |
|-----|---------|--------|
| `apps/lineup_watcher/watch_lineups.py` | Multi-user lineup watcher | In development |

### Configuration

| File | Purpose | Format |
|------|---------|--------|
| `config/player_mappings.yaml` | Player ID mappings | YAML |
| `config/team_code_mappings.yaml` | Team code mappings | YAML |
| `config/club_name_mappings.yaml` | Club name variations | YAML |

### Data Storage

| Directory | Purpose | Format |
|-----------|---------|--------|
| `data/lineups/` | Saved lineup data | Parquet |
| `data/sofascore/` | SofaScore raw data | JSON |
| `data/silver/scout_picks/` | FFScout data | CSV/Parquet |
| `data/logs/` | Application logs | Text |

---

## Current Limitations & Future Enhancements

### Current Limitations

1. **No FFScout Backup:**
   - FFScout scraper exists but not integrated as live backup
   - If SofaScore API fails, no fallback option
   - **Future:** Add FFScout as fallback when SofaScore unavailable

2. **Single League Support:**
   - Currently focused on Premier League (tournament_id=17)
   - **Future:** Extend to other leagues (La Liga, Serie A, etc.)

3. **Manual Player Mapping:**
   - Requires periodic manual mapping updates
   - New players need manual addition
   - **Future:** Automatic mapping via ML/NLP

4. **No Injury Intelligence:**
   - Doesn't check injury/suspension databases
   - Only trusts lineup data
   - **Future:** Integrate with injury databases (Physioroom, etc.)

5. **Single User:**
   - Current design is single-user focused
   - **Future:** Multi-user with user management (partially implemented in `apps/`)

### Potential Enhancements

#### 1. FFScout Backup Integration

**Implementation Path:**

```python
# In poll.py
async def get_lineup_with_fallback(event_id: int) -> Optional[Dict]:
	"""Try SofaScore first, fallback to FFScout."""
	
	# Try SofaScore
	try:
		lineup = await get_sofascore_lineup(event_id)
		if lineup:
			return {"source": "sofascore", "data": lineup}
	except Exception as e:
		logger.warning(f"SofaScore failed: {e}")
	
	# Fallback to FFScout
	try:
		lineup = await get_ffscout_lineup(event_id)
		if lineup:
			return {"source": "ffscout", "data": lineup}
	except Exception as e:
		logger.warning(f"FFScout failed: {e}")
	
	return None
```

**Required Files:**
- `fantraxapi/providers/ffscout/client.py` - API/scraper client
- `fantraxapi/providers/ffscout/normalize.py` - Data normalization
- Unified interface between SofaScore and FFScout data

#### 2. Multi-League Support

**Changes Needed:**

```python
# In discover.py
SUPPORTED_LEAGUES = {
	17: "Premier League",
	87: "La Liga",
	135: "Serie A",
	78: "Bundesliga"
}

async def get_watchlist(
	tournament_ids: List[int] = [17],
	window_minutes: int = 90
) -> List[Event]:
	"""Support multiple leagues."""
	all_events = []
	for tournament_id in tournament_ids:
		events = await get_league_events(tournament_id, ...)
		all_events.extend(events)
	return all_events
```

#### 3. Intelligent Prediction Mode

**Feature:** Track prediction accuracy and use historical data.

```python
class PredictionEngine:
	def predict_lineup(self, team_id: int, match_id: int) -> List[PlayerRecord]:
		"""Predict lineup based on:
		- Recent starting XIs
		- Injury reports
		- Fixture congestion
		- Historical rotation patterns
		"""
		...
```

#### 4. Push Notifications

**Feature:** Alert users when lineups are confirmed.

```python
# Integrations needed:
- Telegram bot
- Discord webhook
- SMS via Twilio
- Email notifications
```

#### 5. Web Dashboard

**Feature:** Monitor lineup status in real-time.

```
Technologies:
- FastAPI backend
- React/Vue frontend
- WebSocket for real-time updates
- Charts showing lineup tracking
```

---

## Troubleshooting

### Common Issues

#### 1. No Lineups Found

**Symptoms:**
- `poll_events()` returns empty list
- "No lineups confirmed" messages

**Causes & Solutions:**

| Cause | Solution |
|-------|----------|
| Polling too early | Start 90min before kickoff |
| Wrong tournament ID | Verify `tournament_id=17` for PL |
| SofaScore API down | Check SofaScore website, wait and retry |
| Rate limiting | Increase `poll_interval`, add delays |

**Debug Commands:**

```bash
# Test API connectivity
python scripts/test_lineup_retrieval.py --event-id EVENT_ID

# Check match schedule
python -c "
import asyncio
from fantraxapi.providers.sofascore.discover import get_season_events
asyncio.run(get_season_events(verbose=True))
"
```

#### 2. Player Mapping Failures

**Symptoms:**
- Many "UNMAPPED" players in output
- `fantrax_id` is None for players

**Solutions:**

1. **Update Mappings:**
   ```bash
   python scripts/update_player_mappings.py \
	   --league-id YOUR_LEAGUE_ID \
	   --force-refresh \
	   --interactive
   ```

2. **Check FFScout Data:**
   ```bash
   # Scrape latest FFScout data
   python fantraxapi/providers/ffscout/scout_picks_rosters.py \
	   --url "https://www.fantasyfootballscout.co.uk/..." \
	   --out-dir data/silver/scout_picks
   ```

3. **Manual Mapping:**
   Edit `config/player_mappings.yaml`:
   ```yaml
   mappings:
	 - fantrax_id: "abc123"
	   fantrax_name: "Player Name"
	   sofascore_id: 12345
	   sofascore_name: "Player Name"
	   aliases:
		 - "Alt Name"
   ```

#### 3. Sync Failures

**Symptoms:**
- "Failed to sync lineup" errors
- Changes not reflecting in Fantrax

**Debug Steps:**

1. **Check Lineup Lock:**
   - Is the scoring period deadline passed?
   - Solution: Use `apply_to_future=True`

2. **Verify Player IDs:**
   ```python
   # Check if Fantrax IDs are valid
   fantrax = FantraxAPI(league_id="...")
   roster = fantrax.get_roster(team_id="...")
   print([p.id for p in roster.starters])
   ```

3. **Test Dry Run:**
   ```python
   syncer = LineupSynchronizer(fantrax=api, dry_run=True)
   syncer.sync_lineup(lineup, team_id)
   print(syncer.changes)  # Preview changes
   ```

4. **Check Logs:**
   ```bash
   tail -f data/logs/lineup_watcher_*.log
   ```

#### 4. Formation Validation Errors

**Symptoms:**
- "Invalid formation" errors
- Can't set lineup due to position requirements

**Solutions:**

1. **Check League Settings:**
   - How many players per position required?
   - Is formation flexible or fixed?

2. **Use Position Overrides:**
   ```python
   result = subs.set_lineup_by_ids(
	   ...,
	   pos_overrides={
		   "player_id_1": "M",  # Force as midfielder
		   "player_id_2": "D"   # Force as defender
	   }
   )
   ```

3. **Review Player Eligibility:**
   - Does player have correct position in Fantrax?
   - Check on Fantrax website: Players > Search > View player

---

## Summary

This lineup workflow provides a comprehensive solution for:

✅ **Discovering** upcoming Premier League matches from SofaScore
✅ **Polling** for lineup releases with automatic retries
✅ **Normalizing** lineup data into consistent format
✅ **Mapping** SofaScore players to Fantrax IDs
✅ **Tracking** lineup status through confirmation windows
✅ **Synchronizing** confirmed lineups to Fantrax rosters

### Key Strengths

- **Reliable:** Exponential backoff, error handling, validation
- **Efficient:** Async/concurrent polling, minimal API calls
- **Flexible:** Dry-run mode, test mode, configurable timing
- **Maintainable:** Clear separation of concerns, comprehensive logging
- **Extensible:** Easy to add new data sources (FFScout, FotMob, etc.)

### Integration Points

The system integrates with Fantrax via:
- `SubsService.set_lineup_by_ids()` for bulk updates
- `FantraxAPI.swap_players()` for individual swaps
- Player mapping system for ID translation
- Status tracking for automation logic

### Next Steps

1. **Test the workflow:**
   ```bash
   python watch_lineups.py --test-mode --days-ahead 7
   ```

2. **Update player mappings:**
   ```bash
   python scripts/update_player_mappings.py --interactive
   ```

3. **Run live automation:**
   ```bash
   python watch_lineups.py --window 90 --interval 60
   ```

For questions or issues, refer to:
- `ARCHITECTURE.md` - System architecture
- `FILE_INDEX.md` - File organization
- `KEY_FUNCTIONS.md` - Core function reference
- GitHub issues - Bug reports and feature requests

---

**Last Updated:** November 17, 2025

