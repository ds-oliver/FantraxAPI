# Complete Workflow Guide

## First Time Using the App

### Step-by-Step Instructions

#### 1. Start the App
```bash
cd /Users/hogan/FantraxAPI
streamlit run apps/auth_login/app.py
```

The app opens in your browser at `http://localhost:8501`

---

#### 2. Authenticate (Main Page)

You'll see the main app with sections in this order:

**Option A: Upload Cookies File**
- Look for "📤 Upload Fantrax Auth" section at the top
- Click "Browse files"
- Upload your Fantrax cookies file
- Click "Load Auth Data"

**Option B: Selenium Login** (if you don't have cookies)
- Expand "Selenium Capture (visible browser)" or "Headless Selenium"
- Click "Start Selenium Capture"
- A browser window opens
- Log into Fantrax
- Close the browser
- Cookies are automatically captured

---

#### 3. Select Your League (Main Page)

After authentication:
- A dropdown appears: "Select League"
- Click it and choose your league from the list
- Example: "League Name (ID: xxx)"

---

#### 4. Select Your Team (Main Page)

After selecting league:
- Another dropdown appears: "Select Your Team"
- Click it and choose your team
- Example: "Your Team Name (ID: xxx)"

---

#### 5. Load Your Roster (Main Page)

After selecting team:
- Click the button: **"📋 Load Roster for Selected Team"**
- Wait a moment
- Your roster appears in a table showing:
  - Position
  - Player Name
  - Team
  - Status
  - Opponent
  - Score

**✅ Success!** You now have your roster loaded.

---

#### 6. Navigate to Lineup Intelligence

Now you can access lineup predictions:

**In the Sidebar** (left side of screen):
- Scroll down if needed
- Look for: **"⚽ Fantrax x SofaScore Link"**
- Click it

You're now on the Lineup Intelligence page!

---

#### 7. Load Lineup Predictions (Lineup Intelligence Page)

On the new page:
- Click the big button: **"🔄 Load Latest Lineup Predictions"**
- Wait for it to load (shows "Loading lineup predictions...")
- You'll see a success message: "✅ Loaded X predictions from next matches for Y teams"

---

#### 8. View Recommendations (Lineup Intelligence Page)

The page shows two sections:

**🚨 Action Required** (if any)
- Players who need action (mismatches between your roster and predictions)
- Example: "Currently starting but predicted to be benched"
- Example: "Currently benched but predicted to start"

**✅ All Good** (collapsible)
- Players where your roster matches predictions
- Click to expand and see them

Each player shows:
- Status emoji (✅ confirmed, 🔮 predicted)
- Player name
- Current roster status vs prediction
- Match details (opponent, kickoff time)
- Recommendation

---

#### 9. Go Back to Main App

When done:
- Click **"← Back to Main App"** at the top
- Returns to main page
- Your roster is still loaded
- You can make substitutions, drops, etc.

---

## Quick Reference

### Visual Flow

```
START HERE
    ↓
Main App (app.py)
    ├─ 1. Upload Auth (cookies or Selenium)
    ├─ 2. Select League (dropdown)
    ├─ 3. Select Team (dropdown)
    └─ 4. Load Roster (button) ✅ ROSTER LOADED
           ↓
    Click sidebar link: "⚽ Fantrax x SofaScore Link"
           ↓
Lineup Intelligence Page
    ├─ 5. Load Predictions (button)
    └─ 6. View Recommendations
           ↓
    Click "← Back" to return to main app
```

### Where Things Are Located

**Main App Sections** (top to bottom):
1. Upload Fantrax Auth
2. Selenium Capture
3. Cookie Debug (expander)
4. League Selection (dropdown)
5. Team Selection (dropdown)
6. Load Roster (button)
7. Roster Table (after loading)
8. Make a Substitution
9. Drop a Player
10. League FAAB & Claims
11. **Sidebar**: "⚽ Fantrax x SofaScore Link"

**Lineup Intelligence Page**:
- Back link at top
- Load Predictions button
- Your Players' Lineup Status
- Action Required section
- All Good section (collapsible)
- Help/Info sidebar (right)

---

## Troubleshooting

### "You need to load your roster first!"
**Problem**: You went directly to Lineup Intelligence without loading roster

**Solution**: 
1. Click "← Go to Main App Now"
2. Follow steps 2-5 above to load your roster
3. Return to Lineup Intelligence page

---

### "No league found" or dropdown is empty
**Problem**: Authentication failed or no leagues available

**Solution**:
1. Re-upload your cookies file
2. Or use Selenium login again
3. Make sure you're logged into Fantrax

---

### "No lineup data found"
**Problem**: Haven't fetched SofaScore lineups yet

**Solution**: Run the ESD script first:
```bash
python esd_export_schedule_and_lineups_v2.py \
    --tournament-id 17 \
    --upcoming \
    --with-lineups
```

---

### Roster not showing after clicking "Load Roster"
**Problem**: API call might have failed

**Solution**:
1. Check the logs at bottom of page
2. Try re-selecting league and team
3. Click "Load Roster" again

---

### Can't find "⚽ Fantrax x SofaScore Link"
**Problem**: Sidebar is hidden or scrolled

**Solution**:
1. Look for `>` arrow to expand sidebar
2. Scroll down in sidebar to find the link
3. It's after other sidebar content

---

## Common Workflows

### Daily Pre-Match Routine

1. **Morning** (8-10 AM):
   ```bash
   # Fetch latest lineups
   python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups
   ```

2. **Open App**:
   ```bash
   streamlit run apps/auth_login/app.py
   ```

3. **Load Roster** (if needed):
   - Upload auth → Select league → Select team → Load roster

4. **Check Lineups**:
   - Click "⚽ Fantrax x SofaScore Link"
   - Click "🔄 Load Latest Lineup Predictions"
   - Review recommendations

5. **Make Changes**:
   - Go back to main app
   - Use "Make a Substitution" to swap players
   - Or use "Drop a Player" to remove

6. **Pre-Match** (2 hours before kickoff):
   - Re-run ESD script for confirmed lineups
   - Reload predictions in app
   - Make final adjustments

---

## Tips

### First Time Setup
- Use Selenium login if you don't have cookies
- Save your cookies file for future sessions
- Bookmark `http://localhost:8501` in your browser

### Using Lineup Intelligence
- Morning predictions are usually preliminary (🔮)
- Pre-match predictions are often confirmed (✅)
- Focus on "Action Required" section first
- Match context shows opponent and kickoff time

### Making Roster Changes
- Do lineup intelligence first (plan changes)
- Then go back to main app to execute
- Use "Make a Substitution" for swaps
- Use "Drop a Player" to drop

### Session State
- Once roster is loaded, it stays loaded
- Navigate between pages without reloading
- Only need to load roster once per session
- Restart app if session expires

---

## Summary

The key concept: **Load your roster on the main page FIRST, then use lineup intelligence.**

The app has two distinct workflows:
1. **Main App**: Authentication, roster management, swaps, drops
2. **Lineup Intelligence**: View predictions, plan changes

You navigate between them using the sidebar link and back button, but your roster data is shared between both pages via session state.

Once you understand this flow, it becomes very intuitive!

