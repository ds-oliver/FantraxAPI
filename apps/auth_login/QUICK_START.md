# Quick Start Guide - Multi-Page App

## Fixed: StreamlitPageNotFoundError

The error `Could not find page: apps/auth_login/lineup_intelligence_page.py` has been fixed by:

1. ✅ Moving page file to `pages/` directory
2. ✅ Updating `st.page_link` paths to be relative to entrypoint

## Current Structure

```
apps/auth_login/
├── app.py                              # Run this file ✓
└── pages/
    └── lineup_intelligence_page.py     # Accessed via navigation
```

## How to Run

```bash
cd /Users/hogan/FantraxAPI
streamlit run apps/auth_login/app.py
```

Then:
1. Upload your Fantrax cookies (or use Selenium login)
2. Click **"⚽ Fantrax x SofaScore Link"** in sidebar
3. Select your league from dropdown on Lineup page
4. Click **"🔄 Load Latest Lineup Predictions"**
5. View recommendations

**Note**: Lineup Intelligence page is self-contained! You can go directly there after authenticating.

## Key Points

### Streamlit Multi-Page Requirements

1. **Entrypoint file**: The file you run with `streamlit run` (in this case `app.py`)
2. **Additional pages**: Must be in a `pages/` directory relative to the entrypoint
3. **Path references**: Use paths relative to the entrypoint file, not workspace root

### Correct Path Usage

```python
# ✅ Correct - from app.py to pages
st.page_link("pages/lineup_intelligence_page.py", ...)

# ✅ Correct - from pages back to app.py  
st.page_link("app.py", ...)

# ❌ Incorrect - workspace-relative path
st.page_link("apps/auth_login/pages/lineup_intelligence_page.py", ...)
```

## What Changed

### Before (Broken)
```
apps/auth_login/
├── app.py
└── lineup_intelligence_page.py  # ❌ Not in pages/ directory
```

```python
# ❌ This path didn't work
st.page_link("apps/auth_login/lineup_intelligence_page.py", ...)
```

### After (Fixed)
```
apps/auth_login/
├── app.py
└── pages/
    └── lineup_intelligence_page.py  # ✅ In pages/ directory
```

```python
# ✅ This path works
st.page_link("pages/lineup_intelligence_page.py", ...)
```

## Features

### Main App (`app.py`)
- Authentication & roster loading
- Player substitutions
- Drop players
- League FAAB & Claims
- Navigation to Lineup Intelligence

### Lineup Intelligence Page
- Load SofaScore predictions
- See next match for each team
- Actionable recommendations
- Match context (opponent, kickoff)
- Statistics and help

## Troubleshooting

### App won't load lineup page
- ✅ Check file is in `pages/` directory: `ls apps/auth_login/pages/`
- ✅ Check path in `st.page_link` is relative: `pages/lineup_intelligence_page.py`
- ✅ Restart Streamlit if you just moved files

### "Please load your roster first"
- Go back to main app
- Load your roster
- Then navigate to Lineup Intelligence

## Documentation

- **Full navigation guide**: `README_NAVIGATION.md`
- **Lineup intelligence details**: `/docs/LINEUP_INTELLIGENCE_INTEGRATION.md`
- **ESD script docs**: `/docs/ESD_LINEUP_FETCHER.md`

## Next Steps

After loading the app:

1. **Authenticate** - Upload cookies or use Selenium login
2. **Load Roster** - Select league and team
3. **Navigate** - Click "⚽ Fantrax x SofaScore Link"
4. **Load Predictions** - Click "🔄 Load Latest Lineup Predictions"
5. **Act** - Review recommendations and make roster changes

That's it! The app is now working with proper multi-page navigation.

