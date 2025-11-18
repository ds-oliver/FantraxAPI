# Lineup Prediction Selection Algorithm

## Quick Summary

The lineup intelligence feature now **intelligently selects only the next upcoming match** for each team, instead of loading all 1420+ predictions from all upcoming matches.

## How It Works

### 1. Load Schedule
Reads the most recent `*_upcoming.csv` schedule file from `data/sofascore/schedules/`

### 2. Filter to Upcoming Matches Only
```python
schedule_df['kickoff_dt'] = pd.to_datetime(schedule_df['kickoff_utc'])
now = datetime.now(timezone.utc)
schedule_df = schedule_df[schedule_df['kickoff_dt'] > now]
```

### 3. Sort Chronologically
Orders matches by kickoff time (earliest first)

### 4. Select Next Match Per Team
```python
team_next_match = {}  # Tracks which match is next for each team

for match in schedule_df.sort_values('kickoff_dt'):
    home_team_id = match['home_team_id']
    away_team_id = match['away_team_id']
    
    # First match encountered = next match for that team
    if home_team_id not in team_next_match:
        team_next_match[home_team_id] = match
    
    if away_team_id not in team_next_match:
        team_next_match[away_team_id] = match
```

### 5. Load Only Relevant Lineups
- For each team's next match, load the lineup JSON file
- Skip files with no player data (empty lineups)
- Extract starters and subs with match context

### 6. Map to Fantrax IDs
Uses `PlayerMappingManager` to convert SofaScore IDs to Fantrax IDs

### 7. Display with Context
Shows each player's prediction with match details:
```
🔮 Bruno Fernandes: starting, predicted to start (Manchester United vs Everton, Sun Nov 24, 20:00 UTC)
```

## Example

### Scenario
**Current time**: Friday Nov 17, 10:00 AM

**Manchester United's schedule**:
- Nov 24, 20:00 - Man United vs Everton ← **NEXT MATCH**
- Nov 30, 12:00 - Crystal Palace vs Man United
- Dec 2, 19:30 - Man United vs Newcastle

**What gets loaded**:
- ✅ Nov 24 match only
- ✅ Casemiro, Bruno Fernandes, etc. from that match
- ✅ Match context: opponent, kickoff time
- ❌ Nov 30 and Dec 2 matches ignored

## Before vs. After

| Metric | Old Approach | New Approach |
|--------|-------------|--------------|
| Predictions loaded | 1420+ | ~440 |
| Match context | None | Full (opponent, kickoff) |
| Relevance | Mixed (all upcoming) | High (next match only) |
| Clarity | Multiple predictions per player | One prediction per player |
| Performance | Slower | Faster |

## Benefits

1. **Relevance**: Only shows predictions for imminent matches (next match for each team)
2. **Clarity**: Each player appears once with their next match prediction
3. **Context**: Users see which match the prediction is for
4. **Performance**: Loads ~70% fewer predictions
5. **Accuracy**: Predictions for next matches are more reliable and actionable

## Why This Matters

### Old Problem
User sees: *"📊 1420 total predictions loaded"*
- Which match are these predictions for?
- Is this for today's match or next week's match?
- Why does Bruno Fernandes appear multiple times?

### New Solution
User sees: *"Loaded 440 predictions from next matches for 20 teams"*
- Predictions are for each team's next upcoming match
- Clear match context shown: opponent and kickoff time
- Each player appears once (for their next match)

## Technical Details

### Files Involved
- **Schedule**: `data/sofascore/schedules/17_76986_upcoming.csv`
- **Lineups**: `data/sofascore/lineups/{event_id}.json`
- **Code**: `apps/auth_login/app.py` (lines 1149-1294)

### Key Data Structures

**`schedule_df`**: DataFrame with columns:
- `event_id`: Unique match ID
- `kickoff_utc`: Match start time
- `home_team`, `away_team`: Team names
- `home_team_id`, `away_team_id`: SofaScore team IDs

**`team_next_match`**: Dict tracking next match per team:
```python
{
    35: 14723574,  # Man United's next match
    48: 14723574,  # Everton's next match
    # ...
}
```

**`relevant_lineups`**: Dict of lineup data for next matches only:
```python
{
    14723574: {
        "lineup_data": {...},  # Raw lineup JSON
        "match_context": {
            "opponent": "Everton",
            "kickoff": "2025-11-24 20:00:00+0000",
            "confirmed": False,
            "home_team": "Manchester United",
            "away_team": "Everton"
        }
    },
    # ...
}
```

### Algorithm Complexity
- **Time**: O(M + N) where M = matches in schedule, N = lineup files to load
  - Old: O(all lineup files) = O(1420+)
  - New: O(upcoming matches) = O(440)
- **Space**: O(N) for storing relevant lineups

## Edge Cases Handled

1. **No schedule file**: Falls back gracefully, shows warning
2. **Match already kicked off**: Filtered out (only future matches)
3. **Empty lineup file**: Skipped automatically
4. **Missing lineup file**: Skipped, continues processing
5. **Player mapping fails**: Continues without Fantrax ID, shows SofaScore name
6. **Invalid kickoff date**: Uses fallback match string without time

## Future Enhancements

1. **Configurable lookahead**: Allow users to see predictions for next 2-3 matches
2. **Gameweek filtering**: Group by gameweek instead of just next match
3. **Team filtering**: Show predictions for specific teams only
4. **Match prioritization**: Weight matches by proximity (next 24 hours = higher priority)
5. **Historical comparison**: Compare predicted vs. actual lineups post-match

