# Fantrax API Documentation Index

Complete documentation for the Fantrax API application and lineup intelligence system.

---

## Quick Navigation

### 🚀 Getting Started
- **[Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md)** - Step-by-step user instructions
- **[Quick Start](../apps/auth_login/QUICK_START.md)** - Fast setup guide
- **[Navigation Guide](../apps/auth_login/README_NAVIGATION.md)** - Understanding the app structure

### 🏗️ Architecture & Technical
- **[Application Architecture](APPLICATION_ARCHITECTURE.md)** - Complete file structure, data flows, and technical design (THIS IS THE MASTER REFERENCE)
- **[Design Decisions](../apps/auth_login/DESIGN_DECISION.md)** - Rationale for self-contained page design
- **[Automatic Roster Loading](../apps/auth_login/AUTOMATIC_ROSTER_LOADING.md)** - Roster loading behavior

### ⚽ Lineup Intelligence
- **[Lineup Intelligence Integration](LINEUP_INTELLIGENCE_INTEGRATION.md)** - Detailed lineup intelligence architecture
- **[Lineup Prediction Selection](LINEUP_PREDICTION_SELECTION.md)** - How predictions are filtered (next match only)
- **[Lineup Intelligence Summary](LINEUP_INTELLIGENCE_SUMMARY.md)** - High-level overview
- **[ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md)** - How to fetch lineup data from SofaScore

### 🤖 Automation
- **[Lineup Workflow Summary](LINEUP_WORKFLOW_SUMMARY.md)** - Automated lineup processing workflow

---

## Documentation by Audience

### For End Users
If you just want to use the app:
1. Start with **[Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md)**
2. Reference **[Quick Start](../apps/auth_login/QUICK_START.md)** for fast setup
3. Use **[ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md)** to understand how to fetch lineup data

### For Developers
If you're modifying or extending the code:
1. Read **[Application Architecture](APPLICATION_ARCHITECTURE.md)** - MASTER REFERENCE
2. Review **[Lineup Intelligence Integration](LINEUP_INTELLIGENCE_INTEGRATION.md)** for lineup feature details
3. Check **[Design Decisions](../apps/auth_login/DESIGN_DECISION.md)** to understand architectural choices

### For Data Scientists
If you're working with the data:
1. **[ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md)** - Data fetching process
2. **[Lineup Prediction Selection](LINEUP_PREDICTION_SELECTION.md)** - Data filtering logic
3. **[Application Architecture](APPLICATION_ARCHITECTURE.md)** → "Data Flow Diagrams" section

---

## Key File Locations

### Application Code
```
/Users/hogan/FantraxAPI/
├── apps/auth_login/
│   ├── app.py                              # Main application
│   ├── pages/lineup_intelligence_page.py   # Lineup intelligence UI
│   └── login.py                            # Selenium authentication
├── fantraxapi/
│   ├── fantrax.py                          # Fantrax API client
│   ├── player_mapping.py                   # Player ID mapping
│   └── providers/sofascore/client.py       # SofaScore API client
└── esd_export_schedule_and_lineups_v2.py  # Lineup data fetcher
```

### Configuration Files
```
/Users/hogan/FantraxAPI/config/
├── player_mappings.yaml    # Player ID mappings (5,800+ entries)
├── team_mappings.yaml      # Team ID mappings
├── leagues.yaml            # League configurations
└── fantrax_leagues.yaml    # User league settings
```

### Data Files
```
/Users/hogan/FantraxAPI/data/
├── sofascore/
│   ├── schedules/          # Match schedules (CSV)
│   ├── lineups/            # Lineup data (JSON)
│   └── lineups_index.csv   # Index of all lineup files
└── logs/                   # Application logs
```

---

## Feature Documentation Matrix

| Feature | User Guide | Technical Doc | Code Files | Config Files |
|---------|-----------|---------------|------------|--------------|
| **Authentication** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Step 2) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Authentication Flow" | `app.py`, `utils/auth_helpers.py`, `login.py` | None |
| **Roster Management** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Steps 3-5) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Roster Management" | `app.py`, `fantraxapi/fantrax.py` | None |
| **Lineup Intelligence** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Steps 6-8) | [Lineup Integration](LINEUP_INTELLIGENCE_INTEGRATION.md) | `pages/lineup_intelligence_page.py` | `config/player_mappings.yaml` |
| **Lineup Data Fetching** | [ESD Fetcher](ESD_LINEUP_FETCHER.md) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Data Fetching" | `esd_export_schedule_and_lineups_v2.py` | `config/leagues.yaml` |
| **Player Swaps** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Step 9) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Player Swaps" | `app.py` (lines 520-650) | None |
| **Player Drops** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Step 9) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Player Swaps" | `app.py` (lines 652-750) | None |
| **FAAB/Waivers** | [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) (Step 9) | [Architecture](APPLICATION_ARCHITECTURE.md) → "Player Swaps" | `app.py` (lines 752-900) | None |
| **Player Mapping** | N/A | [Architecture](APPLICATION_ARCHITECTURE.md) → "Mapping & Configuration" | `fantraxapi/player_mapping.py`, `scripts/update_player_mappings.py` | `config/player_mappings.yaml` |

---

## Common Tasks

### I want to...

#### ...use the app for the first time
→ Read [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md)

#### ...fetch lineup data from SofaScore
→ Read [ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md)

#### ...understand how lineup predictions are filtered
→ Read [Lineup Prediction Selection](LINEUP_PREDICTION_SELECTION.md)

#### ...add a new player mapping
→ Read [Application Architecture](APPLICATION_ARCHITECTURE.md) → "Player Mappings"

#### ...understand the code structure
→ Read [Application Architecture](APPLICATION_ARCHITECTURE.md) → "File Dependencies Graph"

#### ...troubleshoot authentication issues
→ Read [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) → "Troubleshooting"

#### ...understand why lineup intelligence is a separate page
→ Read [Design Decisions](../apps/auth_login/DESIGN_DECISION.md)

#### ...understand the data flow
→ Read [Application Architecture](APPLICATION_ARCHITECTURE.md) → "Data Flow Diagrams"

#### ...modify the lineup display
→ Read [Lineup Intelligence Integration](LINEUP_INTELLIGENCE_INTEGRATION.md)

---

## Documentation Standards

### File Naming Convention
- User guides: `[FEATURE]_GUIDE.md` or `WORKFLOW_GUIDE.md`
- Technical docs: `[FEATURE]_[ASPECT].md` (e.g., `LINEUP_INTELLIGENCE_INTEGRATION.md`)
- Design docs: `DESIGN_DECISION.md`, `ARCHITECTURE.md`
- Summaries: `[FEATURE]_SUMMARY.md`

### Location Convention
- User-facing guides: `/Users/hogan/FantraxAPI/apps/auth_login/*.md`
- Technical docs: `/Users/hogan/FantraxAPI/docs/*.md`
- Feature-specific: Co-located with feature code when appropriate

### Content Structure
All documentation should include:
1. **Purpose** - What this doc covers
2. **Audience** - Who should read this
3. **Quick Reference** - TL;DR or quick answers
4. **Detailed Content** - In-depth explanation
5. **Related Docs** - Links to other relevant documentation

---

## Updating Documentation

### When to Update

**Always update documentation when you:**
- Add a new feature
- Change user workflow
- Modify file structure
- Add/remove configuration
- Change data formats
- Fix a common issue

### What to Update

**User Guides** - When user-facing behavior changes
- [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md)
- [Quick Start](../apps/auth_login/QUICK_START.md)

**Technical Docs** - When code structure changes
- [Application Architecture](APPLICATION_ARCHITECTURE.md) - ALWAYS UPDATE THIS FIRST
- Feature-specific integration docs

**This Index** - When adding new documentation files

### Update Process

1. **Update primary docs** (Application Architecture, Integration docs)
2. **Update user guides** if user-facing changes
3. **Update this index** if new files added
4. **Review related docs** for consistency
5. **Update "Last Updated" dates** in modified files

---

## Documentation Hierarchy

```
📚 Documentation Root
│
├── 🎯 THIS FILE (README.md) - Start here for navigation
│
├── 📖 User Documentation
│   ├── WORKFLOW_GUIDE.md - Primary user guide
│   ├── QUICK_START.md - Fast setup
│   └── README_NAVIGATION.md - App structure
│
├── 🏗️ Technical Documentation
│   ├── APPLICATION_ARCHITECTURE.md ⭐ MASTER REFERENCE
│   ├── LINEUP_INTELLIGENCE_INTEGRATION.md
│   ├── LINEUP_PREDICTION_SELECTION.md
│   ├── ESD_LINEUP_FETCHER.md
│   └── LINEUP_WORKFLOW_SUMMARY.md
│
└── 🎨 Design Documentation
    ├── DESIGN_DECISION.md
    └── AUTOMATIC_ROSTER_LOADING.md
```

---

## Getting Help

### In-App Help
- Each page has contextual help
- Error messages include suggestions
- Logs available for debugging

### Documentation Questions
- Start with [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) for user issues
- Check [Application Architecture](APPLICATION_ARCHITECTURE.md) for technical questions
- Search this index for specific topics

### Common Issues
See the "Troubleshooting" sections in:
- [Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) → User issues
- [ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md) → Data fetching issues
- [Application Architecture](APPLICATION_ARCHITECTURE.md) → Code issues

---

## Version History

### v1.0 (November 18, 2025)
- Initial comprehensive documentation
- Complete application architecture documented
- User guides created
- Lineup intelligence fully documented
- Navigation structure established

---

**Quick Links:**
- [📋 Workflow Guide](../apps/auth_login/WORKFLOW_GUIDE.md) - How to use the app
- [🏗️ Application Architecture](APPLICATION_ARCHITECTURE.md) - How the app works
- [⚽ ESD Lineup Fetcher](ESD_LINEUP_FETCHER.md) - How to get lineup data

**Last Updated**: November 18, 2025

