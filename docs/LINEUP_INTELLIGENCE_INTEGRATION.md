# Lineup Intelligence Integration

## Overview

This document explains how **ESD Lineup Fetcher** integrates with the **Lineup Intelligence** feature in the Streamlit app, enabling intelligent roster decisions based on predicted and confirmed lineups.

## Architecture

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  1. DATA COLLECTION (ESD Script)                                │
├─────────────────────────────────────────────────────────────────┤
│  esd_export_schedule_and_lineups_v2.py                          │
│  ├─ Fetches Premier League schedule via EasySoccerData          │
│  ├─ Gets lineup data (predicted/confirmed) from SofaScore       │
│  └─ Saves JSON files: data/sofascore/lineups/{event_id}.json    │
└─────────────────────────────────────────────────────────────────┘
                            ⬇
┌─────────────────────────────────────────────────────────────────┐
│  2. PLAYER MAPPING                                               │
├─────────────────────────────────────────────────────────────────┤
│  fantraxapi.lineups.mapping.PlayerMappingManager                 │
│  ├─ Maps SofaScore player IDs → Fantrax player IDs              │
│  ├─ Uses pre-built mapping database                             │
│  └─ Updated via: scripts/update_player_mappings.py              │
└─────────────────────────────────────────────────────────────────┘
                            ⬇
┌─────────────────────────────────────────────────────────────────┐
│  3. LINEUP INTELLIGENCE (Streamlit App)                          │
├─────────────────────────────────────────────────────────────────┤
│  apps/auth_login/app.py - Lineup Intelligence Sidebar            │
│  ├─ Loads JSON lineup files                                     │
│  ├─ Extracts starters/subs for each match                       │
│  ├─ Maps to Fantrax IDs                                         │
│  ├─ Compares with current roster positions                      │
│  └─ Displays recommendations                                    │
└─────────────────────────────────────────────────────────────────┘
                            ⬇
┌─────────────────────────────────────────────────────────────────┐
│  4. USER DECISION                                                │
├─────────────────────────────────────────────────────────────────┤
│  Based on lineup intelligence, user can:                         │
│  ├─ Make substitutions (active ↔ reserve swaps)                │
│  ├─ Submit waiver claims for predicted starters                 │
│  ├─ Pick up free agents who are starting                        │
│  └─ Drop players predicted to be benched                        │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. ESD Lineup Fetcher

**File**: `esd_export_schedule_and_lineups_v2.py`

**Purpose**: Fetches lineup data from SofaScore using EasySoccerData library

**Key Features**:
- Browser automation (resistant to rate limiting)
- Dual strategy: ESD first, raw HTTP fallback
- Saves normalized JSON format
- Handles empty lineups gracefully
- Tracks lineup status (predicted vs confirmed)

**Usage**:
```bash
# Fetch upcoming Premier League matches with lineups
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--output-dir data/sofascore \
	--upcoming \
	--with-lineups
```

**Output**: Creates JSON files in `data/sofascore/lineups/`

**Empty Lineup Files**:
The script saves all lineup requests, even when SofaScore returns empty data (H:0 A:0):

**Why save empty files?**
1. **Event tracking**: Maintains a complete record of all matches
2. **Re-fetch intelligence**: Script knows which matches need re-checking
3. **Delta updates**: Can selectively update only empty files closer to kickoff
4. **Minimal cost**: Empty JSON files are ~300 bytes each
5. **Future updates**: Can implement cron job to re-fetch empty files

**When do empty files occur?**
- Matches >1 week away (no predicted lineups yet)
- Matches without released lineups
- Postponed/canceled matches
- API data not yet available

**Filtering strategy**:
The app filters empty files automatically:
```python
# Only process if we have actual player data
if not starters and not subs:
	continue
```

This means empty files are stored but not displayed to users.

### 2. Player Mapping

**File**: `fantraxapi/lineups/mapping.py`

**Class**: `PlayerMappingManager`

**Purpose**: Translates between SofaScore and Fantrax player identifiers

**Key Methods**:
- `get_fantrax_from_sofascore(sofascore_id)` → returns `(fantrax_id, fantrax_name)`
- `get_sofascore_from_fantrax(fantrax_id)` → returns `sofascore_id`

**Mapping Data Source**:
- Stored in: `data/player_mappings.csv` or similar
- Updated by: `scripts/update_player_mappings.py`
- Sources: FFScout scraping, manual mappings, automated fuzzy matching

**Mapping Process**:
```python
from fantraxapi.lineups.mapping import PlayerMappingManager

mapping_mgr = PlayerMappingManager()

# Example: Map SofaScore ID 803031 (Matthijs de Ligt)
fantrax_id, fantrax_name = mapping_mgr.get_fantrax_from_sofascore(803031)
# Returns: ("abc123xyz", "Matthijs de Ligt")
```

### 3. Lineup Intelligence Sidebar

**File**: `apps/auth_login/app.py` (lines 1144-1370)

**Location**: Sidebar section, appears after roster display

**Features**:

#### A. Load Lineup Predictions Button

**Intelligent Match Selection**:
The system uses a smart algorithm to show only relevant predictions:

1. **Load Schedule**: Reads `data/sofascore/schedules/*_upcoming.csv`
2. **Filter Upcoming**: Removes matches that have already kicked off
3. **Sort by Time**: Orders matches chronologically
4. **Next Match Only**: For each team, selects only their next upcoming match
5. **Extract Players**: Gets lineup data only from these next matches
6. **Map IDs**: Converts SofaScore player IDs to Fantrax IDs
7. **Store Context**: Saves match details (opponent, kickoff time) with each prediction

This means instead of loading 1420+ predictions from all matches, it shows only ~440 predictions (22 players × 20 teams with next matches).

**What Gets Loaded**:
- ✅ Next match for each team (closest kickoff time)
- ✅ Only upcoming matches (kickoff > now)
- ✅ Only matches with actual lineup data (not empty files)
- ❌ Past matches are excluded
- ❌ Future matches beyond next match are excluded
- ❌ Empty lineup files are automatically skipped

#### B. View Your Players Expander
Shows lineup intelligence for **only your rostered players**:

**Display Logic**:
```
For each rostered player with lineup data:
	✅ = Confirmed lineup
	🔮 = Predicted lineup
	
	⬇️ WARNING: Currently starting but predicted benched
	⬆️ INFO: Currently benched but predicted to start
	✓ CAPTION: No mismatch (status matches prediction)
	
	Match context shown in parentheses: (Home vs Away, Kickoff Time)
```

**Example Output**:
```
✓ Found 16 of your players in predictions from next matches for 16 teams

🔮 ⬆️ Ryan Sessegnon: Currently benched but predicted to start (Fulham vs Sunderland, Fri Nov 22, 15:00 UTC)
🔮 ⬆️ Harry Wilson: Currently benched but predicted to start (Bournemouth vs West Ham United, Fri Nov 22, 15:00 UTC)
✅ Gabriel Magalhaes: starting, predicted to start (Arsenal vs Tottenham Hotspur, Sat Nov 23, 16:30 UTC)
✅ Antonee Robinson: benched, predicted on bench (Fulham vs Sunderland, Fri Nov 22, 15:00 UTC)
🔮 Bruno Fernandes: starting, predicted to start (Manchester United vs Everton, Sun Nov 24, 20:00 UTC)
🔮 Casemiro: starting, predicted to start (Manchester United vs Everton, Sun Nov 24, 20:00 UTC)
```

#### C. Intelligent Filtering
- Only shows players from your roster
- Groups multiple predictions per player (uses most recent)
- Provides helpful troubleshooting messages:
	- "Player mappings need updating" → run `update_player_mappings.py`
	- "Lineup predictions are for different teams" → your players aren't in fetched matches
	- "Teams haven't released lineups yet" → too early, try closer to kickoff

## Integration Workflow

### Daily Lineup Check (Recommended)

**1. Morning Check (8-10 AM)**
```bash
# Fetch upcoming week's lineups
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--upcoming \
	--with-lineups \
	--output-dir data/sofascore
```

**2. Open Streamlit App**
```bash
streamlit run apps/auth_login/app.py
```

**3. Load Predictions**
- Click "Load Lineup Predictions" in sidebar
- Review "View Your Players" section

**4. Make Decisions**
Based on lineup intelligence:
- **Predicted to start** → Move from bench to active lineup
- **Predicted benched** → Move from active to bench (or consider dropping)
- **Confirmed lineup** → Higher confidence, prioritize these changes

### Pre-Match Check (2 Hours Before Kickoff)

**1. Re-fetch Lineups**
Closer to kickoff, lineups become confirmed:
```bash
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--upcoming \
	--with-lineups
```

**2. Reload in App**
- Click "Load Lineup Predictions" again
- Look for ✅ (confirmed) indicators
- Make final roster adjustments

### Automation Options

**Cron Job Example** (runs every 2 hours on match days):
```bash
# Add to crontab
0 */2 * * 6,0 cd /Users/hogan/FantraxAPI && python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups >> logs/lineup_fetch.log 2>&1
```

## How Prediction Selection Works

### The Challenge

When you run the ESD script with `--upcoming`, it fetches lineups for **all** upcoming matches in the schedule. For a league with 20 teams, this could mean:
- 10 matches in the current gameweek = 220 players
- 10 matches in the next gameweek = 220 players
- 10 matches in the following gameweek = 220 players
- **Total: 660+ player predictions across 30 matches**

The old approach loaded **all** of these and showed whichever file was most recently modified, resulting in:
- 📊 1420 total predictions loaded
- Predictions from matches weeks away
- Multiple predictions per player from different matches
- No context about which match the prediction was for

### The Solution: Intelligent Next-Match Selection

The new algorithm solves this by selecting only the **next upcoming match** for each team:

```python
# Algorithm pseudocode
upcoming_matches = load_schedule().filter(kickoff > now).sort_by(kickoff)
team_next_match = {}

for match in upcoming_matches:
    if match.home_team not in team_next_match:
        team_next_match[match.home_team] = match  # First match = next match
    
    if match.away_team not in team_next_match:
        team_next_match[match.away_team] = match  # First match = next match

# Only load lineups for these next matches
relevant_lineups = [load_lineup(match) for match in team_next_match.values()]
```

### Example Scenario

**Current time**: Friday Nov 17, 10:00 AM

**Manchester United's upcoming matches**:
1. Sun Nov 24, 20:00 - Man United vs Everton (NEXT MATCH)
2. Sun Nov 30, 12:00 - Crystal Palace vs Man United
3. Mon Dec 2, 19:30 - Man United vs Newcastle

**What gets loaded**:
- ✅ Only Match #1 (Nov 24) - the next match
- ❌ Match #2 and #3 are ignored (future matches)

**Result**:
- User sees predictions for: Casemiro, Bruno Fernandes, etc. **for the Nov 24 match**
- Match context shown: "(Manchester United vs Everton, Sun Nov 24, 20:00 UTC)"
- Clear understanding of which match the prediction applies to

### Benefits

1. **Relevance**: Only shows predictions for imminent matches
2. **Clarity**: Each player has exactly one prediction (their next match)
3. **Context**: Shows opponent and kickoff time
4. **Performance**: Loads ~440 predictions instead of 1420+
5. **Accuracy**: Predictions for upcoming matches are more reliable than distant ones

### Data Flow Comparison

**Old Approach**:
```
Load ALL lineup files → Extract ALL players → Show most recent for each player
Result: 1420 predictions, no context, mixed timeframes
```

**New Approach**:
```
Load schedule → Filter upcoming → Sort by time → Select next match per team
→ Load only those lineups → Extract players → Show with match context
Result: ~440 predictions, full context, next matches only
```

## Data Comparison: Empty vs. Populated Lineups

### Empty Lineup File (14025286.json)
```json
{
  "event_id": 14025286,
  "confirmed": false,
  "home": {
    "formation": "",
    "starters": [],
    "subs": [],
    "missing": []
  },
  "away": {
    "formation": null,
    "starters": [],
    "subs": [],
    "missing": []
  }
}
```

**Characteristics**:
- File size: ~300 bytes
- No player data
- Saved for future reference
- **Not displayed in app** (filtered out)

### Populated Lineup File (14723574.json)
```json
{
  "event_id": 14723574,
  "confirmed": false,
  "home": {
    "formation": "3-4-2-1",
    "starters": [
      {
        "id": 964753,
        "name": "Senne Lammens",
        "position": "G",
        "team_id": 35,
        "substitute": false,
        "captain": false
      },
      // ... 10 more starters
    ],
    "subs": [],
    "missing": [
      {
        "id": 859999,
        "name": "Lisandro Martínez",
        "position": "D",
        "reason": 1
      }
    ]
  },
  "away": { /* similar structure */ }
}
```

**Characteristics**:
- File size: ~6-8 KB
- 11 home starters + 11 away starters = 22 players minimum
- Formation data included
- Missing/injured players tracked
- **Displayed in app** (processed and shown)

## Player Mapping Example

### SofaScore Player Data
```json
{
  "id": 803031,
  "name": "Matthijs de Ligt",
  "position": "D",
  "team_id": 35,
  "substitute": false,
  "captain": false
}
```

### After Mapping
```python
{
  "sofascore_id": 803031,
  "player_name": "Matthijs de Ligt",
  "position": "D",
  "team_id": 35,
  "is_sub": False,
  "captain": False,
  "fantrax_id": "jdoe1234",  # ← Mapped via PlayerMappingManager
  "fantrax_name": "Matthijs de Ligt",
  "event_id": 14723574,
  "confirmed": False
}
```

### Displayed in App
If player is on your roster:
```
🔮 ✓ Matthijs de Ligt: starting, predicted to start
```

If not on your roster:
- Not displayed (filtered out)

## Troubleshooting

### "No lineup data" warning
**Solution**: Run the ESD script first:
```bash
python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups
```

### "No rostered players found in predictions"
**Possible causes**:
1. **Mappings outdated**: Run `python scripts/update_player_mappings.py`
2. **Different teams**: Your players' teams aren't in the fetched lineups (e.g., you have La Liga players, but only fetched Premier League)
3. **No lineups yet**: Lineups typically appear 1-2 hours before kickoff

### Empty lineup files being created
**This is expected behavior**:
- Empty files track events without lineup data
- App automatically filters them out
- Can be re-fetched later when lineups become available
- Minimal storage impact (<1KB per file)

**To skip saving empty files**, modify the script (optional):
```python
# In esd_export_schedule_and_lineups_v2.py, around line 418
hs = len((data.get("home") or {}).get("starters") or [])
as_ = len((data.get("away") or {}).get("starters") or [])

# Add this check before saving:
if hs == 0 and as_ == 0:
	print(f"[lineups] {event_id}: skipping empty lineup")
	continue
```

**However**, keeping empty files is recommended for tracking and delta updates.

### Player mappings not working
**Solution**:
1. Check mapping file exists: `data/player_mappings.csv` (or configured path)
2. Verify mapping contains SofaScore IDs: `grep "803031" data/player_mappings.csv`
3. Update mappings: `python scripts/update_player_mappings.py`
4. Check logs for mapping warnings: `grep "mapping" data/logs/auth_workflow.log`

## Future Enhancements

### Planned Features

1. **Automatic Re-fetch**
	- Cron job to update lineups every 2 hours
	- Only re-fetch empty files or recently modified matches
	- Alert when confirmed lineups replace predictions

2. **Lineup Confidence Scores**
	- Weight predictions based on source reliability
	- Historical accuracy tracking
	- Injury/suspension data integration

3. **Multi-League Support**
	- Fetch lineups for multiple tournaments (La Liga, Serie A, etc.)
	- Tournament selection in app UI
	- Cross-league player tracking

4. **Automated Swap Suggestions**
	- AI-powered swap recommendations
	- One-click apply lineup intelligence
	- Undo/rollback functionality

5. **Lineup Change Notifications**
	- Email/SMS alerts when rostered players' status changes
	- Push notifications when confirmed lineups differ from predictions
	- Webhook integration for Discord/Slack

6. **Historical Lineup Analytics**
	- Track lineup prediction accuracy over time
	- Player starting frequency analysis
	- Manager rotation patterns

## Summary

The ESD Lineup Fetcher + Lineup Intelligence integration provides:

**Benefits**:
- ✅ Reliable lineup data via browser automation
- ✅ Automatic player ID mapping
- ✅ Real-time roster intelligence
- ✅ Intelligent filtering (only your players)
- ✅ Confirmed vs. predicted lineup distinction
- ✅ Graceful handling of missing data

**Workflow**:
1. Run ESD script to fetch lineups
2. Open Streamlit app
3. Load lineup predictions
4. Review your players' status
5. Make informed roster decisions

**Key Advantage**: Separates data collection (ESD script) from intelligence display (Streamlit app), allowing flexible scheduling and testing without disrupting the user interface.

