# ESD Lineup Fetcher Documentation

## Overview

`esd_export_schedule_and_lineups_v2.py` fetches SofaScore schedule and lineup data using the **EasySoccerData (ESD)** library with automatic fallback to raw HTTP requests when needed.

## Key Features

### 1. Dual Data Source Strategy
- **Primary**: Uses EasySoccerData library (browser automation via Selenium)
- **Fallback**: Raw HTTP requests to SofaScore API when ESD fails
- Configurable with `--disable-raw` flag to use ESD only

### 2. Data Outputs

The script generates three types of files:

#### Schedule CSV (`schedules/{tournament_id}_{season_id}_{mode}.csv`)
Contains match schedule with columns:
- `event_id`: Unique match identifier
- `kickoff_utc`: Match start time
- `home_team`, `away_team`: Team names
- `home_team_id`, `away_team_id`: SofaScore team IDs
- `tournament_id`, `season_id`: Competition identifiers
- `round`: Match round/gameweek
- `status_code`: Match status (100 = finished)

#### Lineup JSON (`lineups/{event_id}.json`)
Per-match lineup files with structure:
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
			}
			// ... more players
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
	"away": { /* same structure */ }
}
```

**Empty Lineup Files**: The script saves files even when no lineup data exists (H:0 A:0) for games that are too far in the future. This creates placeholder files with empty arrays but preserves the event tracking.

#### Lineup Index (`lineups_index.csv`)
Tracks all saved lineups:
- `event_id`: Match ID
- `confirmed`: Whether lineup is confirmed (bool)
- `home_starters`, `away_starters`: Count of players
- `saved_at_utc`: Timestamp of when lineup was fetched

### 3. Season Resolution

Smart season selection:
1. Explicit `--season-id` takes priority
2. Text match via `--season` (e.g., "2024/25")
3. Falls back to "current" season
4. Uses highest season ID if no current season

### 4. Event Filtering

- `--upcoming`: Fetch future matches instead of completed
- `--finished-only`: Filter to completed matches (status=100)
- `--limit N`: Stop after N events (for testing)

## Usage Examples

### Fetch Current Season Schedule
```bash
python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --output-dir data/sofascore
```

### Fetch Upcoming Matches with Lineups
```bash
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--output-dir data/sofascore \
	--upcoming \
	--with-lineups
```

### Fetch Specific Season with Raw HTTP Disabled
```bash
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--season "2024/25" \
	--with-lineups \
	--disable-raw
```

### Quick Test (5 matches only)
```bash
python esd_export_schedule_and_lineups_v2.py \
	--tournament-id 17 \
	--upcoming \
	--with-lineups \
	--limit 5
```

## Command-Line Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--tournament-id` | int | 17 | SofaScore tournament ID (17 = Premier League) |
| `--season` | str | None | Season label to match (e.g., '2023/2024') |
| `--season-id` | int | None | Explicit season ID (overrides --season) |
| `--upcoming` | flag | False | Fetch upcoming fixtures instead of completed |
| `--finished-only` | flag | False | Keep only finished matches (status=100) |
| `--limit` | int | None | Stop after N events |
| `--with-lineups` | flag | False | Fetch and save lineup JSON files |
| `--output-dir` | Path | `data/sofascore` | Base output directory |
| `--browser-path` | str | Chrome path | Chrome/Chromium path for ESD |
| `--disable-raw` | flag | False | Disable HTTP fallback (ESD only) |

## Common Tournament IDs

- **17**: Premier League (England)
- **34**: La Liga (Spain)
- **35**: Serie A (Italy)
- **8**: Bundesliga (Germany)
- **23**: Ligue 1 (France)

## Advantages Over `watch_lineups.py`

### 1. Reliability
- Browser automation is more robust against API rate limiting
- Fallback mechanisms ensure data collection continues
- Less susceptible to 403 Forbidden errors

### 2. Flexibility
- Can fetch historical data (`--finished-only`)
- Can fetch future schedules (`--upcoming`)
- Configurable season selection
- Works across multiple tournaments

### 3. Data Completeness
- Captures missing players and injury reasons
- Includes formation data
- Tracks captain and substitute status
- Provides complete schedule metadata

### 4. Output Format
- Normalized JSON structure compatible with lineup intelligence
- CSV index for quick lookup
- Separate schedule files for easy querying

## Integration with Fantrax Workflow

### Data Flow

1. **ESD Script** → Fetches lineup JSON files
2. **Lineup Intelligence** → Reads JSON, maps to Fantrax IDs
3. **Streamlit App** → Displays predictions alongside roster
4. **User Decision** → Makes swaps/claims/drops based on intelligence
5. **Fantrax Sync** → (Future) Automates roster changes

### Player Mapping

To use lineup data with Fantrax:
1. SofaScore player IDs must be mapped to Fantrax IDs
2. Use `scripts/update_player_mappings.py` to maintain mapping
3. Lineup intelligence uses `PlayerMappingManager` for ID translation

### Empty Lineup Files

**Why save them?**
- Maintains event tracking even when no lineup data exists
- Allows script to skip re-fetching on subsequent runs
- Provides complete audit trail of all events
- Can be updated later when lineups become available

**When do they occur?**
- Games >1 week out (no predicted lineups yet)
- Postponed matches
- Events without lineup data in SofaScore

**Should we filter them?**
The script could skip empty lineups, but keeping them:
- Preserves event awareness
- Enables delta updates (re-fetch only empty ones)
- Minimal storage cost (<1KB per file)

## Error Handling

### ESD Failures
- Automatically falls back to raw HTTP
- Continues processing remaining events
- Logs specific errors per event

### Rate Limiting
- Includes random jitter (0.3-0.6s) between requests
- Uses proper headers to mimic browser behavior
- Respects SofaScore's API structure

### Missing Data
- Gracefully handles missing player info
- Uses safe getters with defaults
- Continues processing when individual lineups fail

## Output Structure

```
data/sofascore/
├── schedules/
│   └── 17_76986_upcoming.csv
├── lineups/
│   ├── 14025072.json (11 vs 11 - has data)
│   ├── 14723574.json (11 vs 11 - has data)
│   ├── 14025286.json (0 vs 0 - empty)
│   └── ...
└── lineups_index.csv
```

## Troubleshooting

### "ESD get_tournament_seasons failed"
- Ensure Chrome/Chromium is at specified path
- Check if browser path needs updating (`--browser-path`)
- Script will automatically try raw HTTP fallback

### Empty Lineups for Recent Matches
- Some matches don't have predicted lineups
- Lineups typically appear 1-2 hours before kickoff
- Re-run script closer to match time

### "403 Forbidden" Errors
- ESD browser automation avoids this
- If using `--disable-raw`, remove flag to enable fallback
- Raw HTTP includes proper headers to minimize 403s

## Performance

- **Schedule fetching**: ~5-10 seconds for full season
- **Lineup fetching**: ~1-2 seconds per event (with jitter)
- **Full Premier League**: ~5-10 minutes for all upcoming lineups
- **Rate limiting**: Built-in delays prevent API blocks

