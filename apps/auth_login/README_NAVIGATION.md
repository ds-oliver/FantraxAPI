# Multi-Page Navigation Setup

## Overview

The Fantrax auth app now has a multi-page structure using Streamlit's `st.page_link` for navigation between the main roster management page and the lineup intelligence page.

## Structure

```
apps/auth_login/
├── app.py                          # Main app (roster, swaps, drops, FAAB)
├── pages/
│   └── lineup_intelligence_page.py # Lineup intelligence (SofaScore integration)
└── README_NAVIGATION.md            # This file
```

**Note**: Streamlit requires additional pages to be in a `pages/` directory relative to the entrypoint file.

## Pages

### 1. Main App (`app.py`)

**Route**: Main entry point
**Features**:
- Authentication and session management
- Roster loading and display
- Player substitutions (swap_players)
- Drop player functionality
- League FAAB & Claims display
- **Navigation**: Sidebar link to Lineup Intelligence

**Sidebar Navigation**:
```python
st.page_link("pages/lineup_intelligence_page.py", 
             label="⚽ Fantrax x SofaScore Link", 
             icon="⚽")
```

### 2. Lineup Intelligence Page (`pages/lineup_intelligence_page.py`)

**Route**: `pages/lineup_intelligence_page.py` (relative to entrypoint)
**Features**:
- Load predicted/confirmed lineups from SofaScore
- Intelligent next-match selection per team
- Player mapping (SofaScore ID → Fantrax ID)
- Actionable recommendations (who to bench/start)
- Match context display (opponent, kickoff time)
- **Navigation**: Back link to main app

**Top Navigation**:
```python
st.page_link("app.py", 
             label="← Back to Main App", 
             icon="🏠")
```

## How It Works

### Navigation Flow

```
Main App (app.py)
    │
    │ Click "⚽ Fantrax x SofaScore Link" in sidebar
    ↓
Lineup Intelligence Page (pages/lineup_intelligence_page.py)
    │
    │ Click "← Back to Main App" at top
    ↓
Main App (app.py)
```

### Session State Sharing

Both pages share `st.session_state`, which allows:

**Main App sets**:
- `st.session_state.roster` - User's current roster
- `st.session_state.api` - FantraxAPI instance
- `st.session_state.session` - Requests session (authentication)
- `st.session_state.league_id` - Selected league
- `st.session_state.team_id` - Selected team

**Lineup Intelligence Page accesses**:
- `st.session_state.roster` - To compare predictions with current lineup
- Stores `st.session_state.lineup_predictions` - Loaded lineup data

### Page Requirements

**Lineup Intelligence Page**:
1. Requires `roster` in session state
2. If not present, shows error and stops
3. User must load roster on main app first

**Flow**:
```
User → Main App → Load Roster → Navigate to Lineup Intelligence → View Predictions
```

## Running the App

### Start the Main App

```bash
cd /Users/hogan/FantraxAPI
streamlit run apps/auth_login/app.py
```

### Navigate Between Pages

1. **From Main App to Lineup Intelligence**:
   - Look in sidebar
   - Click "⚽ Fantrax x SofaScore Link"
   - Streamlit automatically loads `lineup_intelligence_page.py`

2. **From Lineup Intelligence to Main App**:
   - Click "← Back to Main App" at the top
   - Returns to main roster management page

## Technical Details

### st.page_link vs st.navigation

We use `st.page_link` instead of `st.navigation` because:

1. **Simpler structure**: No need to restructure entire app with `.run()` pattern
2. **Backward compatible**: Existing app.py continues to work as-is
3. **Explicit navigation**: Clear links where you want them
4. **Session state preservation**: Automatically maintained across page transitions

**st.page_link** creates a hyperlink that:
- Navigates to another Python file in your app
- Maintains session state
- Allows parameters: `label`, `icon`
- Works in sidebar or main content

### File Paths

**Relative paths** (from entrypoint file):
```python
# From app.py to pages
st.page_link("pages/lineup_intelligence_page.py", ...)

# From pages back to app.py
st.page_link("app.py", ...)
```

**Important**: When using `st.page_link`, paths are relative to the entrypoint file (the file you run with `streamlit run`), not the workspace root. Additional pages must be in a `pages/` directory relative to the entrypoint.

## Adding More Pages

To add additional pages to the navigation:

### 1. Create the New Page File

```python
# apps/auth_login/pages/new_feature_page.py

import streamlit as st

st.title("New Feature")
st.page_link("app.py", label="← Back", icon="🏠")

# Your page content here
```

### 2. Add Navigation Link in Main App

```python
# In app.py sidebar
with st.sidebar:
    st.page_link("pages/lineup_intelligence_page.py", 
                 label="⚽ Fantrax x SofaScore Link", 
                 icon="⚽")
    
    st.page_link("pages/new_feature_page.py", 
                 label="🆕 New Feature", 
                 icon="🆕")
```

### 3. Add Back Links in New Page

```python
st.page_link("app.py", label="← Main", icon="🏠")
```

## Advanced: Section Grouping (Future Enhancement)

For more complex navigation with sections, you can use `st.navigation`:

```python
# streamlit_app.py (future refactor)

import streamlit as st

# Define pages
main_page = st.Page("apps/auth_login/app.py", title="Roster Manager", icon="🔁")
lineup_page = st.Page("apps/auth_login/lineup_intelligence_page.py", 
                      title="Lineup Intelligence", icon="⚽")
claims_page = st.Page("apps/auth_login/claims_page.py", 
                      title="Waiver Claims", icon="📋")

# Group pages
pg = st.navigation({
    "Main": [main_page],
    "Intelligence": [lineup_page, claims_page],
})

st.set_page_config(page_title="Fantrax Manager", page_icon="⚽")
pg.run()
```

But for now, `st.page_link` provides a simple, effective solution.

## Benefits of Current Structure

### Pros

✅ **Modular**: Each page is self-contained  
✅ **Maintainable**: Easy to update individual pages  
✅ **Session sharing**: Automatic state preservation  
✅ **Simple**: No complex navigation setup  
✅ **Flexible**: Easy to add/remove pages  
✅ **Backward compatible**: Main app unchanged  

### Cons

❌ No automatic sidebar navigation menu  
❌ Manual link management  
❌ No built-in page grouping  

## Troubleshooting

### "Page not found" Error

**Cause**: Incorrect file path in `st.page_link` or page not in `pages/` directory

**Solution**: 
1. Ensure page file is in `pages/` directory relative to entrypoint
2. Use correct relative path:

```python
# Correct (from app.py to pages)
st.page_link("pages/lineup_intelligence_page.py", ...)

# Correct (from pages back to app.py)
st.page_link("app.py", ...)

# Incorrect - not in pages/ directory
st.page_link("lineup_intelligence_page.py", ...)

# Incorrect - absolute path
st.page_link("/apps/auth_login/pages/lineup_intelligence_page.py", ...)

# Incorrect - workspace-relative path
st.page_link("apps/auth_login/pages/lineup_intelligence_page.py", ...)
```

### Session State Not Preserved

**Cause**: Running pages as separate Streamlit apps

**Solution**: Always navigate via `st.page_link`, not by running multiple `streamlit run` commands

### Roster Not Found Error

**Symptom**: Lineup Intelligence shows "Please load your roster first"

**Solution**: 
1. Go back to main app
2. Load your roster
3. Then navigate to Lineup Intelligence

## File Size Comparison

**Before (single file)**:
- `app.py`: ~1,700 lines (everything in one file)

**After (multi-page)**:
- `app.py`: ~1,480 lines (main features)
- `pages/lineup_intelligence_page.py`: ~280 lines (lineup intelligence)
- **Total**: Same functionality, better organization

## Directory Structure

```
apps/auth_login/
├── app.py                          # Main entrypoint (run this with streamlit)
├── pages/                          # Additional pages directory (required by Streamlit)
│   └── lineup_intelligence_page.py # Lineup intelligence page
├── README_NAVIGATION.md            # This documentation
└── utils/                          # Helper modules (unchanged)
    ├── auth_helpers.py
    ├── cookie_import.py
    ├── log_helpers.py
    └── roster_ops.py
```

## Summary

The new navigation structure:
- Keeps main roster management on the primary page
- Moves lineup intelligence to a dedicated page
- Uses simple `st.page_link` for navigation
- Maintains all functionality
- Improves code organization and maintainability

Users can now access lineup intelligence via a clear sidebar link, keeping the main app focused on core roster management tasks.

