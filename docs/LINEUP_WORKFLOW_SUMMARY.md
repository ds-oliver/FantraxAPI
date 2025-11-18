# Lineup Workflow - Quick Reference

## Overview

Your system scrapes Premier League lineups from **SofaScore** (primary) and uses **FFScout** data for player identification only (not currently used as a backup lineup source).

## Data Flow (5 Phases)

```
1. DISCOVERY → Find upcoming matches (SofaScore API)
      ↓
2. POLLING → Wait for lineup release (check every 60s)
      ↓
3. NORMALIZATION → Convert to internal format + map player IDs
      ↓
4. STATUS TRACKING → PRELIMINARY → CONFIRMED → FINAL
      ↓
5. SYNC TO FANTRAX → Update rosters via SubsService
```

## Key Components

### SofaScore (Primary Source)
- **Purpose:** Real-time lineup scraping
- **When:** 60-75 minutes before kickoff
- **What:** Starting XI, subs, formation, player details
- **Files:**
  - `fantraxapi/providers/sofascore/client.py` - API client
  - `fantraxapi/providers/sofascore/poll.py` - Polling logic
  - `fantraxapi/providers/sofascore/discover.py` - Match discovery

### FFScout (Player Mapping Only)
- **Purpose:** Help identify players for mapping
- **Usage:** Build/validate player ID mappings
- **NOT used:** As backup lineup source (infrastructure exists but not implemented)
- **Files:**
  - `fantraxapi/providers/ffscout/scout_picks_rosters.py` - Web scraper
  - `scripts/update_player_mappings.py` - Uses FFScout for mappings

### Player Mapping
- **Purpose:** Translate SofaScore IDs ↔ Fantrax IDs
- **File:** `config/player_mappings.yaml`
- **Manager:** `fantraxapi/player_mapping.py`
- **Update:** Run `python scripts/update_player_mappings.py`

### Fantrax Integration
- **Primary Method:** `SubsService.set_lineup_by_ids()` (bulk update)
- **Alternative:** `FantraxAPI.swap_players()` (single swap)
- **File:** `fantraxapi/subs.py`
- **Workflow:** Confirmed lineups → Determine changes → Execute swaps

## Lineup Status States

| Status | Timing | Action |
|--------|--------|--------|
| PRELIMINARY | >75min before KO | Store only, don't sync |
| PENDING_CONFIRMATION | <75min, not confirmed | Wait |
| **CONFIRMED** | <75min, confirmed | **SYNC TO FANTRAX** |
| FINAL | >5min after KO | Archive |

## Quick Start

### Watch for Lineups (Test Mode)
```bash
python watch_lineups.py --test-mode --days-ahead 7
```

### Update Player Mappings
```bash
python scripts/update_player_mappings.py \
	--league-id YOUR_LEAGUE_ID \
	--interactive
```

### Run Live Automation
```bash
python watch_lineups.py --window 90 --interval 60
```

## Key Files

| Component | File |
|-----------|------|
| Main CLI | `watch_lineups.py` |
| Automation | `fantraxapi/lineups/automation.py` |
| Sync Logic | `fantraxapi/lineups/fantrax_sync.py` |
| Player Mapping | `fantraxapi/player_mapping.py` |
| SofaScore Client | `fantraxapi/providers/sofascore/client.py` |
| Subs Engine | `fantraxapi/subs.py` |

## Important Notes

1. **FFScout is NOT a live backup** - Only used for player ID mapping
2. **SofaScore is primary** - All lineup data comes from SofaScore API
3. **Confirmation required** - Only syncs lineups with status=CONFIRMED
4. **Player mapping critical** - Unmapped players can't be synced
5. **Timing windows matter** - Starts polling 90min before kickoff

## Future Enhancements

- [ ] Implement FFScout as backup lineup source
- [ ] Multi-league support (La Liga, Serie A, etc.)
- [ ] Automatic player mapping via ML
- [ ] Push notifications when lineups confirmed
- [ ] Web dashboard for monitoring

---

See `LINEUP_WORKFLOW.md` for comprehensive documentation.

