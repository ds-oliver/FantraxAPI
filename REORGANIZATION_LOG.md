# Codebase Reorganization Log

**Date:** November 15, 2025  
**Purpose:** Archive old versions and organize codebase structure

This document tracks all file moves during the codebase reorganization. **No files were deleted** - everything is preserved for historical reference.

---

## Overview

- **Total files archived:** 12 old versions
- **Total files reorganized:** TBD (scripts)
- **Rationale:** Reduce clutter, clarify core vs. auxiliary files, improve maintainability

---

## Archived Files (Old Versions)

All superseded version files moved to `archive/old_versions/` with date stamp.

### 1. Streamlit GUI Versions

| Original Path | New Path | Reason | Superseded By |
|--------------|----------|--------|---------------|
| `apps/auth_login/app_v1.py` | `archive/old_versions/app_v1.py` | Early iteration of auth/subs GUI | `apps/auth_login/app.py` |
| `apps/auth_login/app_v2.py` | `archive/old_versions/app_v2.py` | Second iteration with improved auth | `apps/auth_login/app.py` |
| `apps/auth_login/app_v3.py` | `archive/old_versions/app_v3.py` | Third iteration before final | `apps/auth_login/app.py` |

**Date Archived:** 2025-11-15  
**Notes:** The v1-v3 files contain valuable development history showing the evolution of the authentication flow and UI design. The current `app.py` incorporates lessons learned from all three versions.

### 2. Substitution CLI Versions

| Original Path | New Path | Reason | Superseded By |
|--------------|----------|--------|---------------|
| `substitutions_v1.py` | `archive/old_versions/substitutions_v1.py` | Early CLI implementation | `substitutions_v2.py` |
| `subs_v1.py` | `archive/old_versions/subs_v1.py` | Alternative early implementation | `substitutions_v2.py` |

**Date Archived:** 2025-11-15  
**Notes:** v1 implementations used simpler swap logic. v2 incorporates robust period detection and deadline handling based on insights documented in `SUBS SUMMARY.md`.

### 3. Schedule/Lineup Export Scripts

| Original Path | New Path | Reason | Superseded By |
|--------------|----------|--------|---------------|
| `esd_export_schedule_and_lineups_v1.py` | `archive/old_versions/esd_export_schedule_and_lineups_v1.py` | Initial implementation | Current version |
| `esd_export_schedule_and_lineups_v2.py` | `archive/old_versions/esd_export_schedule_and_lineups_v2.py` | Improved parsing | Current version |
| `esd_export_schedule_and_lineups_v3.py` | `archive/old_versions/esd_export_schedule_and_lineups_v3.py` | Latest before cleanup | Current version |
| `scripts/esd_export_schedule_and_lineups_v3.py` | `archive/old_versions/scripts_esd_export_schedule_and_lineups_v3.py` | Duplicate of root v3 | Same as root v3 |

**Date Archived:** 2025-11-15  
**Notes:** These scripts evolved to handle SofaScore API changes and improved lineup data normalization. The progression shows refinement of the player mapping system.

### 4. Monitoring Scripts

| Original Path | New Path | Reason | Superseded By |
|--------------|----------|--------|---------------|
| `monitor_faab_v2.py` | `archive/old_versions/monitor_faab_v2.py` | FAAB monitoring standalone script | `apps/auth_login/app.py` (integrated) |

**Date Archived:** 2025-11-15  
**Notes:** FAAB monitoring functionality was integrated into the main Streamlit app with division support and improved UI. The standalone script is preserved as a CLI reference.

### 5. Utility Scripts

| Original Path | New Path | Reason | Superseded By |
|--------------|----------|--------|---------------|
| `scripts/update_player_mappings_v2.py` | `archive/old_versions/update_player_mappings_v2.py` | One-off mapping update script | Manual mapping updates |

**Date Archived:** 2025-11-15  
**Notes:** Player mapping updates are now done manually via YAML config files. This script shows the automated approach that was used during initial setup.

---

## Scripts Reorganized

Scripts moved from root to organized directories.

### Example Scripts → `scripts/examples/`

| Original Path | New Path | Purpose |
|--------------|----------|---------|
| `drop_player.py` | `scripts/examples/drop_player.py` | Example of drop API usage |
| `list_rosters.py` | `scripts/examples/list_rosters.py` | Example of roster fetching |
| `list_all_rosters.py` | `scripts/examples/list_all_rosters.py` | Batch roster operations |
| `submit_claim.py` | `scripts/examples/submit_claim.py` | Waiver claim example |
| `lineup_optimizer.py` | `scripts/examples/lineup_optimizer.py` | Lineup optimization utility |

**Date Moved:** 2025-11-15  
**Notes:** These scripts demonstrate API usage patterns and serve as templates for building custom tools.

### Monitoring Scripts → `scripts/monitoring/`

| Original Path | New Path | Purpose |
|--------------|----------|---------|
| `monitor_faab.py` | `scripts/monitoring/monitor_faab.py` | FAAB budget monitoring |
| `monitor_trades.py` | `scripts/monitoring/monitor_trades.py` | Trade activity monitoring |

**Date Moved:** 2025-11-15  
**Notes:** Standalone monitoring scripts. Most functionality now integrated into main Streamlit app.

### Demo Scripts → `scripts/demos/`

| Original Path | New Path | Purpose |
|--------------|----------|---------|
| `esd_lineups_demo.py` | `scripts/demos/esd_lineups_demo.py` | Lineup export demo |
| `esd_lineups_demo_best.py` | `scripts/demos/esd_lineups_demo_best.py` | Enhanced lineup demo |
| `esd_lineups_full_demo_best.py` | `scripts/demos/esd_lineups_full_demo_best.py` | Full lineup demo |

**Date Moved:** 2025-11-15  
**Notes:** Demonstration scripts showing SofaScore lineup integration evolution.

### Test Scripts → `archive/old_versions/`

| Original Path | New Path | Purpose |
|--------------|----------|---------|
| `test.py` | `archive/old_versions/test.py` | Ad-hoc testing script |
| `test_matching_fixes.py` | `archive/old_versions/test_matching_fixes.py` | Player matching tests |

**Date Moved:** 2025-11-15  
**Notes:** One-off test scripts. Formal tests are in `tests/` directory.

---

## What Was NOT Moved

Files that remain in their current locations:

### Active Development Files
- **`fantraxapi/`** - Core library (all files)
- **`apps/auth_login/app.py`** - Primary GUI
- **`substitutions_v2.py`** - Current CLI reference
- **`utils/`** - All utility modules
- **`requirements.txt`**, **`setup.py`** - Package configuration

### Documentation
- **`README.rst`** - Project overview
- **`AUTH.md`** - Authentication guide
- **`SUBS SUMMARY.md`** - Substitution insights
- **`IDEAS.md`** - Feature roadmap
- **`ARCHITECTURE.md`** - This architecture guide (new)
- **`FILE_INDEX.md`** - File listing (new)

### Configuration
- **`config.ini`** - User config
- **`config/`** - All YAML configs

### Data & Logs
- **`data/`** - All data files (lineups, schedules, logs)
- **`archive/`** - Historical code (preserve indefinitely)

---

## Rationale for Each Move

### Why Archive Old Versions?
1. **Reduce cognitive load** - Developers see only current, maintained code
2. **Preserve history** - Old implementations remain accessible for reference
3. **Clarify intent** - File names without version numbers indicate "this is current"
4. **Enable search** - Searching code finds only active implementations

### Why Keep in Archive vs. Delete?
1. **Development insights** - Shows evolution of solutions
2. **Rollback capability** - Can reference old approaches if needed
3. **Documentation** - Code is self-documenting; old versions show "why we changed"
4. **Disk space** - Negligible cost to keep text files

### Why NOT Archive Core Files?
Files like `fantrax.py`, `subs.py`, `app.py` are actively maintained and have no "superseded" versions. They represent the current, working state of the system.

---

## How to Find Archived Files

1. **Browse archive directory:**
   ```bash
   ls archive/old_versions/
   ```

2. **Search archived code:**
   ```bash
   grep -r "search_term" archive/old_versions/
   ```

3. **View specific archived file:**
   ```bash
   cat archive/old_versions/app_v2.py
   ```

4. **Compare versions:**
   ```bash
   diff archive/old_versions/substitutions_v1.py substitutions_v2.py
   ```

---

## Archive Policy

### When to Archive
- **Version files** (`*_v1.py`, `*_v2.py`) - When superseded by newer version
- **Duplicate files** - When identical functionality exists elsewhere
- **Experimental code** - When experiment concluded and not adopted
- **One-off scripts** - When task complete and script no longer needed

### When NOT to Archive
- **Active development** - Code still being modified
- **Unique functionality** - No replacement exists
- **Referenced by docs** - Documentation points to it
- **Part of API** - External users depend on it

### Naming Convention in Archive
- Preserve original filename if from root: `substitutions_v1.py`
- Add path prefix if from subdirectory: `scripts_update_player_mappings_v2.py`
- Add date if multiple archives same name: `backup_20251115_filename.py`

---

## Next Steps

After this reorganization:

1. ✅ **ARCHITECTURE.md** created - Documents 4-tier structure
2. ✅ **Old versions archived** - 12 files preserved in archive/
3. ⏳ **Scripts organized** - Move examples and monitoring scripts
4. ⏳ **README updated** - Add quick start and architecture link
5. ⏳ **FILE_INDEX.md** created - Complete file listing

---

## Questions or Concerns?

If you need to access archived code or have questions about why something was moved:
1. Check this log for rationale
2. View the archived file directly
3. Compare with current implementation
4. Reach out to maintainers if unclear

**Remember:** Nothing is deleted. Everything is preserved.

---

**Last Updated:** November 15, 2025  
**Next Review:** When next major reorganization needed

