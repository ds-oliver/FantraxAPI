# Lineup Intelligence Workflow

**Last Updated:** November 17, 2025

This document describes the Lineup Intelligence workflow for analyzing predicted and confirmed lineups alongside your Fantrax roster, waiver wire, and league data to make informed decisions.

**Important:** This is separate from the automatic lineup sync workflow. It's designed for planning and decision-making, not automatic execution.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Components](#components)
4. [Usage](#usage)
5. [Decision Types](#decision-types)
6. [Integration Points](#integration-points)

---

## Overview

The Lineup Intelligence system helps you make better fantasy football decisions by:

1. **Analyzing Predictions**: Process lineup predictions from SofaScore/FFScout
2. **Comparing with Roster**: See how predictions match your current lineup
3. **Identifying Opportunities**: Find available players predicted to start
4. **Generating Recommendations**: Get actionable insights for swaps, claims, and drops

### Key Difference from Automatic Sync

| Feature | Lineup Intelligence | Automatic Sync |
|---------|-------------------|----------------|
| **Purpose** | Planning & analysis | Automatic execution |
| **Action** | Show recommendations | Make changes |
| **Confidence** | Works with predictions | Requires confirmation |
| **Timing** | Anytime | 60-75min before kickoff |
| **Interface** | Visual app + CLI | Background service |
| **Control** | User decides | Automatic (when enabled) |

---

## Architecture

### Data Flow

```
Lineup Predictions (SofaScore/FFScout)
         ↓
[LineupIntelligenceAnalyzer]
         ↓
Combined with:
  • Your Roster (Fantrax API)
  • Waiver Wire (Fantrax API)
  • Player Mappings (YAML)
         ↓
[Intelligence Map]
  • PlayerStatus per player
  • Confidence scores
  • Roster context
         ↓
[Recommendation Engine]
         ↓
Actionable Insights:
  • Swap recommendations
  • Waiver targets
  • Drop candidates
  • Free agent pickups
```

### Component Overview

```
fantraxapi/lineups/intelligence.py
  ├── LineupIntelligence (dataclass)
  │     • Player identification
  │     • Prediction status & confidence
  │     • Roster context
  │     • Performance metrics
  │
  ├── ActionRecommendation (dataclass)
  │     • Recommended action type
  │     • Priority level
  │     • Reasoning
  │
  └── LineupIntelligenceAnalyzer (class)
        • Load predictions
        • Build intelligence map
        • Generate recommendations
        • Export reports
```

---

## Components

### 1. LineupIntelligence (Data Model)

Represents intelligence about a single player.

**Key Fields:**

```python
@dataclass
class LineupIntelligence:
	# Identification
	player_name: str
	fantrax_id: Optional[str]
	sofascore_id: Optional[int]
	
	# Prediction
	status: PlayerStatus  # CONFIRMED_STARTER, PREDICTED_STARTER, etc.
	confidence: float  # 0-1 confidence score
	lineup_status: LineupStatus  # PRELIMINARY, CONFIRMED, etc.
	
	# Roster context
	is_on_roster: bool
	is_starting: bool
	is_on_bench: bool
	is_available: bool  # On waivers/free agency
	
	# Performance
	fppg: Optional[float]
```

**Player Status Values:**

- `CONFIRMED_STARTER`: Official lineup, starting XI
- `PREDICTED_STARTER`: Predicted to start (not yet confirmed)
- `CONFIRMED_BENCH`: Official lineup, substitute
- `PREDICTED_BENCH`: Predicted to be on bench
- `OUT`: Injured/suspended
- `UNKNOWN`: No prediction available

**Confidence Scores:**

| Status | Confidence |
|--------|-----------|
| Confirmed (from official lineup) | 1.0 |
| Confirmed starter | 0.95 |
| Predicted starter | 0.7 |
| Confirmed bench | 0.9 |
| Predicted bench | 0.6 |
| Out | 0.95 |

### 2. ActionRecommendation (Data Model)

Represents a recommended action.

**Action Types:**

```python
class ActionType(str, Enum):
	SWAP_TO_ACTIVE = "swap_to_active"     # Move bench → starters
	SWAP_TO_RESERVE = "swap_to_reserve"   # Move starters → bench
	WAIVER_CLAIM = "waiver_claim"         # Claim from waivers
	FREE_AGENT_CLAIM = "free_agent_claim" # Pick up free agent
	DROP_PLAYER = "drop_player"           # Consider dropping
	NO_ACTION = "no_action"               # All good
```

**Priority Levels:**

- **P1 (Highest)**: Player out/injured but in your starting lineup
- **P2**: Confirmed starter on your bench, or starter not playing
- **P3**: Predicted lineup mismatches
- **P4**: Available players predicted to start
- **P5-P7**: Lower priority optimizations
- **P8-P10 (Lowest)**: Nice-to-have improvements

**Example:**

```python
ActionRecommendation(
	action_type=ActionType.SWAP_TO_ACTIVE,
	player=LineupIntelligence(...),  # Bench player predicted to start
	swap_with=LineupIntelligence(...),  # Starter predicted on bench
	priority=2,
	reason="Swap bench player (starting) with starter (benched/out)"
)
```

### 3. LineupIntelligenceAnalyzer (Core Engine)

Main analysis engine that combines all data sources.

**Key Methods:**

#### `load_lineup_predictions()`

Loads lineup data from saved files.

```python
analyzer = LineupIntelligenceAnalyzer(
	fantrax_api=api,
	player_mapping=player_mapping,
	lineup_data_dir=Path("data/lineups")
)

records = analyzer.load_lineup_predictions(
	source="sofascore",
	from_date=datetime.now(),
	to_date=datetime.now() + timedelta(days=7)
)
```

#### `build_intelligence()`

Creates intelligence map combining predictions with roster data.

```python
intelligence_map = analyzer.build_intelligence(
	lineup_records=records,
	team_id="your_team_id"
)

# Returns: Dict[fantrax_id, LineupIntelligence]
```

#### `generate_recommendations()`

Generates prioritized action recommendations.

```python
recommendations = analyzer.generate_recommendations(
	intelligence_map=intelligence_map,
	min_confidence=0.7  # Only show high-confidence recommendations
)

# Returns: List[ActionRecommendation]
```

**Recommendation Logic:**

1. **Bench → Active**: Player on bench but predicted to start
2. **Active → Bench**: Starter predicted on bench/out
3. **Waiver Claims**: Available players predicted to start
4. **Drop Candidates**: Rostered players consistently not starting

#### `generate_swap_recommendations()`

Specific swap recommendations with pairing logic.

```python
swaps = analyzer.generate_swap_recommendations(intelligence_map)

# Matches bench players who should start with
# starters who should be benched (by position/confidence)
```

#### `get_matchday_summary()`

Summary DataFrame for a specific matchday.

```python
df = analyzer.get_matchday_summary(
	intelligence_map=intelligence_map,
	matchday=datetime(2025, 11, 23)
)

# Returns DataFrame with columns:
# Player, Team, Position, Status, Confidence, On Roster, etc.
```

#### `export_recommendations()`

Export recommendations to CSV for review.

```python
analyzer.export_recommendations(
	recommendations=recommendations,
	output_path=Path("data/lineup_intelligence/recommendations.csv")
)
```

---

## Usage

### Option 1: Streamlit App (Recommended)

**Access:** Integrated into the roster viewer app

**Start:**

```bash
cd apps/roster_viewer
streamlit run app.py
```

**Navigate:**
1. In the sidebar, select **"🧠 Lineup Intelligence"**
2. Choose your league
3. Adjust settings (days ahead, confidence threshold)
4. Click **"🔍 Analyze Lineups"**

**Features:**

- **Dashboard**: Summary metrics (total players, mapped, mismatches, available starters)
- **All Predictions Tab**: View all lineup predictions
- **Starters Only Tab**: Filter to predicted starters
- **Your Roster Tab**: Compare predictions with your current lineup
- **Recommendations**: Actionable insights with warnings for mismatches

**Visual Indicators:**

- ⬆️ Player on bench but predicted to start
- ⬇️ Starter predicted to be benched/out
- 🟢 Available predicted starter
- ⚠️ Lineup mismatch
- ✅ Lineup matches prediction

### Option 2: CLI Tool

**Location:** `scripts/lineup_intelligence.py`

**Basic Usage:**

```bash
python scripts/lineup_intelligence.py \
	--league-id YOUR_LEAGUE_ID \
	--team-id YOUR_TEAM_ID \
	--days-ahead 7 \
	--min-confidence 0.7
```

**Arguments:**

- `--league-id`: Your Fantrax league ID (required)
- `--team-id`: Your Fantrax team ID (required)
- `--days-ahead`: Days to look ahead for matches (default: 7)
- `--min-confidence`: Minimum confidence threshold (default: 0.7)
- `--output-dir`: Output directory for reports (default: data/lineup_intelligence)
- `-v, --verbose`: Verbose logging

**Output:**

1. **Terminal Display:**
   - Colored tables with recommendations grouped by action type
   - Priority indicators (P1, P2, etc.)
   - Confidence percentages
   - Matchday summary

2. **CSV Exports:**
   - `recommendations_YYYYMMDD_HHMMSS.csv`
   - `matchday_summary_YYYYMMDD_HHMMSS.csv`

**Example Output:**

```
╭─ Swap To Active Recommendations ─╮
│ P  Player         Team  Status     │
│ 2  John Doe      ARS   Predicted  │
│                        Starter     │
│    Confidence: 85%                 │
│    Reason: Predicted to start     │
│    Swap with: Jane Smith          │
╰───────────────────────────────────╯
```

### Option 3: Python API

**Import:**

```python
from fantraxapi import FantraxAPI
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.intelligence import LineupIntelligenceAnalyzer
```

**Usage:**

```python
# Initialize
api = FantraxAPI(league_id="YOUR_LEAGUE_ID")
player_mapping = PlayerMappingManager("config/player_mappings.yaml")

analyzer = LineupIntelligenceAnalyzer(
	fantrax_api=api,
	player_mapping=player_mapping
)

# Load predictions (from watch_lineups.py output)
records = analyzer.load_lineup_predictions(source="sofascore")

# Build intelligence
intelligence = analyzer.build_intelligence(records, team_id="YOUR_TEAM_ID")

# Generate recommendations
recommendations = analyzer.generate_recommendations(
	intelligence,
	min_confidence=0.7
)

# Process recommendations
for rec in recommendations:
	if rec.priority <= 3:  # High priority only
		print(f"{rec.action_type}: {rec.player.player_name}")
		print(f"  Reason: {rec.reason}")
		print(f"  Confidence: {rec.player.confidence:.0%}")
```

---

## Decision Types

### 1. Active/Reserve Swaps

**Scenario:** Bench player predicted to start, or starter predicted to be benched.

**Analysis:**

```python
# Players on bench who should start
bench_should_start = [
	intel for intel in intelligence_map.values()
	if intel.is_on_bench and intel.is_likely_starter
]

# Starters who should be benched
starters_should_bench = [
	intel for intel in intelligence_map.values()
	if intel.is_starting and (intel.is_likely_bench or intel.status == PlayerStatus.OUT)
]
```

**Example Recommendations:**

- **P2**: Move Saka (bench, confirmed starter) to active lineup
- **P1**: Bench Martinelli (active, out injured) ❌
- **P3**: Swap Havertz (bench, predicted starter) with Jesus (active, predicted bench)

**When to Act:**

- **P1 (Injured/Out)**: Act immediately
- **P2 (Confirmed)**: Act when lineup confirmed (60-75min before kickoff)
- **P3 (Predicted)**: Consider for planning, wait for confirmation

### 2. Waiver Claims

**Scenario:** Player on waivers predicted to start.

**Analysis:**

```python
available_starters = [
	intel for intel in intelligence_map.values()
	if intel.is_available and 
	   intel.is_likely_starter and 
	   intel.availability_type == "waiver"
]

# Sort by confidence and FPPG
available_starters.sort(key=lambda x: (x.confidence, x.fppg or 0), reverse=True)
```

**Example Recommendations:**

- **P4**: Claim Nketiah (waiver, predicted starter, 8.5 FPPG)
- **P5**: Claim Pereira (waiver, predicted starter, 6.2 FPPG)

**Considerations:**

- Confidence level (>70% recommended)
- Player's recent form (FPPG)
- Your FAAB budget
- Waiver priority
- Drop candidate availability

### 3. Free Agent Pickups

**Scenario:** Unclaimed player predicted to start.

**Analysis:**

```python
free_agent_starters = [
	intel for intel in intelligence_map.values()
	if intel.is_available and 
	   intel.is_likely_starter and 
	   intel.availability_type == "free_agent"
]
```

**Example Recommendations:**

- **P4**: Pick up Broja (free agent, predicted starter)

**When to Act:**

- Immediately if high confidence
- Before other managers notice
- Consider short-term streaming vs long-term hold

### 4. Drop Candidates

**Scenario:** Rostered player consistently not starting.

**Analysis:**

```python
drop_candidates = [
	intel for intel in intelligence_map.values()
	if intel.is_on_roster and 
	   intel.status in (PlayerStatus.PREDICTED_BENCH, PlayerStatus.OUT) and
	   intel.confidence >= 0.7
]
```

**Example Recommendations:**

- **P7**: Consider dropping Smith (on roster, consistently benched)
- **P6**: Consider dropping Jones (on roster, out injured long-term)

**Considerations:**

- Injury status (short-term vs long-term)
- Team schedule (upcoming fixtures)
- Position scarcity
- Potential return value

---

## Integration Points

### With Fantrax Methods

The intelligence system integrates with existing Fantrax methods:

#### 1. Roster Data

```python
# Get current roster
roster = api.roster_info(team_id)

# Compare with predictions
for row in roster.get_starters():
	intel = intelligence_map.get(row.player.id)
	if intel and not intel.is_likely_starter:
		print(f"⚠️ {row.player.name} starting but predicted on bench")
```

#### 2. Waiver Wire

```python
# Search for predicted starters
available = api.waivers.list_players_by_name(
	limit=100,
	status="ALL_AVAILABLE"
)

# Cross-reference with predictions
for player in available:
	intel = intelligence_map.get(player["id"])
	if intel and intel.is_likely_starter:
		print(f"🟢 {player['name']} available and predicted to start")
```

#### 3. Substitutions

**Important:** Intelligence is for planning only. Actual swaps use the subs workflow.

```python
# After reviewing intelligence recommendations,
# use the subs module to execute:

from fantraxapi.subs import SubsService

subs = SubsService(api._request, api)

# Execute recommended swap
result = subs.swap_players(
	league_id=league_id,
	team_id=team_id,
	out_id=bench_player_id,  # From recommendation.swap_with
	in_id=starter_player_id  # From recommendation.player
)
```

### With Lineup Watcher

The intelligence system reads data generated by the lineup watcher:

**Workflow:**

1. **Collect Data** (lineup watcher):
   ```bash
   python watch_lineups.py --window 90 --interval 60
   ```
   
   This creates: `data/lineups/lineups_*.parquet`

2. **Analyze Data** (intelligence):
   ```bash
   python scripts/lineup_intelligence.py --league-id XXX --team-id YYY
   ```
   
   Or use Streamlit app

3. **Take Action** (manual or subs module):
   - Review recommendations
   - Execute swaps via subs module
   - Submit claims via waivers module

### Data Storage

**Lineup Predictions:**
- Location: `data/lineups/`
- Format: Parquet files
- Naming: `lineups_[league]_[event_id]_[status].parquet`
- Columns: player_name, team_name, position, is_sub, fantrax_id, fantrax_name, etc.

**Intelligence Reports:**
- Location: `data/lineup_intelligence/`
- Format: CSV files
- Types:
  - `recommendations_YYYYMMDD_HHMMSS.csv`
  - `matchday_summary_YYYYMMDD_HHMMSS.csv`

**Player Mappings:**
- Location: `config/player_mappings.yaml`
- Format: YAML
- Structure:
  ```yaml
  mappings:
    - fantrax_id: "abc123"
      fantrax_name: "Player Name"
      sofascore_id: 12345
      sofascore_name: "Player Name"
  ```

---

## Best Practices

### 1. Timing

**When to Check:**

- **Daily**: Quick check for upcoming matches
- **Matchday Morning**: Detailed analysis
- **60-90min Before Kickoff**: Final confirmation check
- **After Official Lineups**: Execute high-priority swaps

### 2. Confidence Thresholds

**Recommended Settings:**

- **High Stakes**: 0.9+ (only act on confirmed lineups)
- **Standard**: 0.7+ (balance between early action and accuracy)
- **Aggressive**: 0.6+ (act on predictions, accept some errors)

### 3. Priority Levels

**Action Guidelines:**

- **P1-P2**: Act immediately when confident
- **P3-P4**: Plan ahead, wait for confirmation
- **P5-P7**: Consider for optimization
- **P8-P10**: Nice-to-have, low impact

### 4. Validation

Always cross-check recommendations:

1. Check player's recent form
2. Verify team news (injuries, rotation)
3. Consider fixture difficulty
4. Check deadlines (waivers, lineups)

---

## Troubleshooting

### No Lineup Data Available

**Problem:** App shows "No lineup data found"

**Solution:**

```bash
# Run lineup watcher to collect data
python watch_lineups.py --window 90 --interval 60

# Or for test mode (future games):
python watch_lineups.py --test-mode --days-ahead 7
```

### Player Not Mapped

**Problem:** Player shows "No Fantrax ID"

**Solution:**

```bash
# Update player mappings
python scripts/update_player_mappings.py \
	--league-id YOUR_LEAGUE_ID \
	--interactive
```

### Low Confidence Scores

**Problem:** All recommendations have low confidence

**Causes:**

- Too far from kickoff (lineups not released yet)
- Using predicted lineups instead of confirmed
- Poor data quality

**Solution:**

- Wait until closer to kickoff
- Lower confidence threshold temporarily
- Check lineup data quality

### No Recommendations

**Problem:** No actions recommended

**Possible Reasons:**

1. Your lineup already optimal ✅
2. Confidence threshold too high
3. No lineup data for your players' teams
4. All your players' matches haven't been analyzed yet

**Check:**

- Lower min_confidence setting
- Verify lineup data exists for upcoming matches
- Check that player mappings are up to date

---

## Summary

**Lineup Intelligence** provides decision-making support by:

✅ Combining lineup predictions with your roster data
✅ Identifying mismatches and opportunities
✅ Generating prioritized action recommendations
✅ Separating planning from execution (unlike automatic sync)
✅ Supporting waiver, free agent, and drop decisions

**Key Benefits:**

- **Informed Decisions**: Data-driven insights, not guesswork
- **Early Planning**: Act on predictions before confirmation
- **Safety**: Review before executing (no automatic changes)
- **Flexibility**: CLI and visual interfaces
- **Integration**: Works with existing Fantrax methods

**Next Steps:**

1. Run lineup watcher to collect data
2. Open Streamlit app and analyze
3. Review recommendations
4. Execute approved actions via subs/waivers modules

For automatic lineup sync (different workflow), see `LINEUP_WORKFLOW.md`.

---

**Last Updated:** November 17, 2025

