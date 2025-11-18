# Lineup Intelligence - Quick Summary

**Purpose:** Analyze lineup predictions alongside your roster, waivers, and league data for informed decision-making.

**Key Point:** This is **separate from automatic sync** - it's for planning, not automatic execution.

---

## What Was Built

### 1. Core Intelligence Module
**File:** `fantraxapi/lineups/intelligence.py`

**Components:**
- `LineupIntelligence`: Data model for player intelligence
- `ActionRecommendation`: Recommended actions with priority
- `LineupIntelligenceAnalyzer`: Main analysis engine

**Features:**
- Combines lineup predictions with roster data
- Calculates confidence scores
- Generates prioritized recommendations
- Exports reports to CSV

### 2. Streamlit App Integration
**File:** `apps/roster_viewer/app.py`

**New Page:** "🧠 Lineup Intelligence" (added to existing roster viewer)

**Features:**
- Visual dashboard with summary metrics
- Three tabs:
  - 📋 All Predictions
  - ⚡ Starters Only  
  - 🎯 Your Roster (compares predictions vs actual)
- Inline recommendations with warnings
- Clean, modern UI matching your existing aesthetic

**Access:**
```bash
cd apps/roster_viewer
streamlit run app.py
# Then select "🧠 Lineup Intelligence" in sidebar
```

### 3. CLI Tool
**File:** `scripts/lineup_intelligence.py`

**Usage:**
```bash
python scripts/lineup_intelligence.py \
	--league-id YOUR_LEAGUE_ID \
	--team-id YOUR_TEAM_ID \
	--days-ahead 7 \
	--min-confidence 0.7
```

**Output:**
- Rich terminal tables with colored recommendations
- Exported CSV reports
- Grouped by action type with priorities

---

## Decision Types Supported

### 1. Active/Reserve Swaps
- Bench players predicted to start → move to active
- Starters predicted benched/out → move to reserves
- Smart pairing by position and confidence

### 2. Waiver Claims
- Available players predicted to start
- Sorted by confidence and FPPG
- Identifies high-value targets

### 3. Free Agent Pickups
- Unclaimed players predicted to start
- Immediate pickup opportunities

### 4. Drop Candidates  
- Rostered players consistently not starting
- Long-term injured players
- Low-value holds

---

## How It Works

### Data Flow

```
1. Collect Predictions
   └─ python watch_lineups.py --window 90
      └─ Saves to: data/lineups/*.parquet

2. Analyze
   └─ Streamlit app OR CLI tool
      └─ Loads predictions + your roster
      └─ Maps players via config/player_mappings.yaml
      └─ Generates intelligence & recommendations

3. Review
   └─ Visual app: See dashboard and tabs
   └─ CLI: See terminal tables
   └─ CSV: Export for spreadsheet analysis

4. Execute (Manual)
   └─ Use subs module for swaps
   └─ Use waivers module for claims
   └─ NOT automatic - you decide!
```

### Key Metrics

**Player Intelligence:**
- Status: confirmed_starter, predicted_starter, etc.
- Confidence: 0.6-1.0 (higher = more reliable)
- Roster context: on_roster, is_starting, is_on_bench
- Availability: on waivers, free agent, owned

**Recommendations:**
- Priority: P1 (highest) to P10 (lowest)
- Action type: swap, claim, drop
- Reasoning: Why action is recommended
- Swap pairing: Who to swap with (when applicable)

---

## Usage Examples

### Example 1: Check Your Lineup Before Matchday

**Streamlit App:**
1. Open app: `streamlit run apps/roster_viewer/app.py`
2. Navigate to "🧠 Lineup Intelligence"
3. Select your league
4. Click "🔍 Analyze Lineups"
5. Check "🎯 Your Roster" tab
6. Review warnings: ⬆️ = move to active, ⚠️ = mismatch

### Example 2: Find Waiver Targets

**CLI:**
```bash
python scripts/lineup_intelligence.py \
	--league-id YOUR_LEAGUE \
	--team-id YOUR_TEAM \
	--min-confidence 0.75
```

Look for "Waiver Claim" recommendations (P4 priority).

### Example 3: Get CSV for Analysis

**Output:** `data/lineup_intelligence/recommendations_*.csv`

Columns:
- Priority, Action, Player, Team, Position
- Status, Confidence, Reason
- Swap With, On Roster, Currently Starting, FPPG

Import into Excel/Google Sheets for further analysis.

---

## Integration with Existing Workflows

### Separate from Automatic Sync

| Feature | Intelligence | Automatic Sync |
|---------|-------------|----------------|
| **Timing** | Anytime | 60-75min before KO |
| **Data** | Predictions + Confirmed | Confirmed only |
| **Action** | Manual review & decision | Automatic execution |
| **Risk** | Zero (view only) | Executes changes |
| **Use Case** | Planning, analysis | Last-minute accuracy |

### Works With

**Lineup Watcher** (`watch_lineups.py`):
- Collects prediction data
- Intelligence reads this data

**Subs Module** (`fantraxapi/subs.py`):
- After reviewing recommendations
- Use subs to execute swaps

**Waivers Module** (`fantraxapi/waivers.py`):
- After identifying targets
- Use waivers to submit claims

**Player Mapping** (`config/player_mappings.yaml`):
- Maps SofaScore IDs → Fantrax IDs
- Update with: `python scripts/update_player_mappings.py`

---

## Configuration

### Required Files

1. **League Config:** `config/fantrax_leagues.yaml`
   ```yaml
   leagues:
     My League:
       league_id: "abc123"
       team_id: "xyz789"
   ```

2. **Player Mappings:** `config/player_mappings.yaml`
   - Updated via `scripts/update_player_mappings.py`
   - Maps SofaScore players to Fantrax

3. **Lineup Data:** `data/lineups/*.parquet`
   - Generated by `watch_lineups.py`
   - Loaded by intelligence analyzer

### Settings

**Streamlit App:**
- Days Ahead: 1-14 (how far to look)
- Min Confidence: 0.5-1.0 (threshold for recommendations)

**CLI Tool:**
- Same settings via command-line arguments
- Plus: output directory, verbose logging

---

## Quick Start

### 1. Collect Data
```bash
# Run lineup watcher
python watch_lineups.py --window 90 --interval 60
```

### 2. Analyze
```bash
# Option A: Streamlit App
cd apps/roster_viewer
streamlit run app.py

# Option B: CLI Tool
python scripts/lineup_intelligence.py \
	--league-id YOUR_LEAGUE \
	--team-id YOUR_TEAM
```

### 3. Review & Act
- Check recommendations
- Prioritize P1-P3 actions
- Execute via subs/waivers modules (manually)

---

## Files Created/Modified

### New Files

1. **`fantraxapi/lineups/intelligence.py`** - Core module (600 lines)
2. **`scripts/lineup_intelligence.py`** - CLI tool (300 lines)
3. **`LINEUP_INTELLIGENCE.md`** - Full documentation (1,000 lines)
4. **`docs/LINEUP_INTELLIGENCE_SUMMARY.md`** - This file

### Modified Files

1. **`apps/roster_viewer/app.py`**
   - Added `lineup_intelligence_view()` function
   - Added page navigation in sidebar
   - Integrated with existing roster viewer

2. **`apps/roster_viewer/requirements.txt`**
   - Added: httpx, tenacity, pydantic, pyarrow

---

## Documentation

- **Full Guide:** `LINEUP_INTELLIGENCE.md` (comprehensive)
- **Quick Summary:** `docs/LINEUP_INTELLIGENCE_SUMMARY.md` (this file)
- **Lineup Workflow:** `LINEUP_WORKFLOW.md` (automatic sync - different!)

---

## What's Next?

**Future Enhancements:**

1. **Real-time Updates:** Auto-refresh in Streamlit app
2. **Historical Tracking:** Track recommendation accuracy over time
3. **Team Analysis:** Compare with other managers' rosters
4. **Fixture Difficulty:** Factor in opponent strength
5. **Bulk Actions:** Queue multiple swaps/claims

**Ready to Use:**
- Core functionality complete ✅
- Integrated into existing app ✅
- CLI tool ready ✅
- Documentation complete ✅

---

**Last Updated:** November 17, 2025

