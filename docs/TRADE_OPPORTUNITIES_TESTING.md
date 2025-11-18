# Trade Opportunities Feature - Testing & Verification

## Overview

This document provides a comprehensive testing checklist for the Trade Opportunities feature, which analyzes roster composition across division teams and suggests ideal trade partners.

## Unit Tests

Unit tests have been created and verified in `tests/test_trade_analysis.py`:

✓ Position extraction from roster rows (GK/D/M/F)
✓ Surplus/need computation based on division averages
✓ Trade partner matching algorithm

**Run unit tests:**
```bash
cd /Users/hogan/FantraxAPI
python tests/test_trade_analysis.py
```

## Integration Testing Checklist

### 1. Authentication & Session Setup

- [ ] Navigate to Overview page
- [ ] Upload Fantrax cookies or login via Selenium
- [ ] Verify session is authenticated
- [ ] Select a league from the sidebar dropdown
- [ ] Select your team from the sidebar

### 2. Page Access

- [ ] Navigate to "Trade Opportunities" page (should appear in sidebar as page 3)
- [ ] Page loads without errors
- [ ] Page title displays: "Trade Opportunities"
- [ ] League/team selector appears in sidebar

### 3. Division Detection

- [ ] Verify correct division is detected for your team
- [ ] Check division name displays correctly
- [ ] Verify number of teams in division is accurate
- [ ] Test fallback behavior if divisions can't be detected (should use "All Teams")

### 4. Your Roster Analysis

**Position Counts:**
- [ ] GK count is accurate
- [ ] D (Defender) count is accurate
- [ ] M (Midfielder) count is accurate
- [ ] F (Forward) count is accurate
- [ ] Total player count matches actual roster

**Surplus/Needs:**
- [ ] Surplus positions are correctly identified (positions where you have more than division average)
- [ ] Need positions are correctly identified (positions where you have fewer than division average)
- [ ] "Well-balanced" message appears if no surplus/needs

### 5. Division Overview Table

- [ ] Table displays all teams in your division
- [ ] Position counts (GK/D/M/F) are accurate for each team
- [ ] Total counts are correct
- [ ] Surplus and needs are computed correctly for each team
- [ ] Your team is highlighted in the table

### 6. Trade Partner Suggestions

**Basic Functionality:**
- [ ] Trade partners are listed (if any complementary matches exist)
- [ ] Partners are ranked by match score (best matches first)
- [ ] Top 3 suggestions are expanded by default

**Match Quality:**
- [ ] "You Give" section shows positions where you have surplus AND partner has need
- [ ] "You Get" section shows positions where partner has surplus AND you have need
- [ ] Match score accurately reflects number of complementary positions
- [ ] Partners with no complementary matches are not shown

**No Matches Scenario:**
- [ ] If no matches, helpful message explains why
- [ ] Suggestions for creating stronger surpluses appear

### 7. Edge Cases

- [ ] Empty division (single team): Should show "All Teams" fallback
- [ ] Team with empty roster: Should handle gracefully
- [ ] League without divisions: Should create "All Teams" group
- [ ] Division with only 2 teams: Should still compute averages
- [ ] API errors: Should show informative error messages

### 8. UI/UX Verification

- [ ] Page is responsive and uses full width
- [ ] Tables are readable and well-formatted
- [ ] Expanders work correctly
- [ ] Colors and styling match other pages
- [ ] No emoji usage (per user preference)
- [ ] Tabs are used for indentation (per user preference)

### 9. Performance

- [ ] Page loads within reasonable time (<5 seconds for typical division)
- [ ] Roster loading shows spinner/progress indicator
- [ ] No unnecessary API calls
- [ ] Data is cached appropriately in session state

## Manual Test Scenarios

### Scenario 1: Balanced Roster
**Setup:** Team with position counts near division average
**Expected:** No surplus/needs shown, no trade suggestions

### Scenario 2: Strong Surplus
**Setup:** Team with 8+ defenders when division average is 5
**Expected:** "D" appears in surplus, teams needing defenders are suggested

### Scenario 3: Multiple Complementary Matches
**Setup:** You have surplus D/M, opponent has surplus F and needs D/M
**Expected:** High match score (3-4), clear "You Give" and "You Get" sections

### Scenario 4: No Complementary Matches
**Setup:** All teams in division have similar roster compositions
**Expected:** "No strong trade matches" message with explanation

## Verification of Key Files

### Created Files:
✓ `/Users/hogan/FantraxAPI/fantraxapi/trade_analysis.py` - Core analysis logic
✓ `/Users/hogan/FantraxAPI/apps/auth_login/pages/3_trade_opportunities.py` - Streamlit UI
✓ `/Users/hogan/FantraxAPI/tests/test_trade_analysis.py` - Unit tests

### Modified Files:
✓ `/Users/hogan/FantraxAPI/fantraxapi/utils.py` - Added `group_teams_by_division_api()` function

## Known Limitations (Phase 1)

1. **No player-level suggestions**: Shows position-level matches only
2. **No trade builder UI**: Phase 2 feature
3. **No direct trade submission**: Phase 2 feature
4. **Fixed min_delta**: Currently hardcoded to 1, could be configurable
5. **Bench players**: Currently included in counts (configurable in future)

## Troubleshooting

### "No active Fantrax session" error
- Go back to Overview page and re-authenticate
- Upload cookies or login via Selenium

### "Could not detect your division" warning
- Normal for leagues without divisions
- Feature will use "All Teams" as single group

### Position counts seem wrong
- Verify roster includes active players only
- Check if bench players should be included/excluded
- Review position mapping logic in `_extract_primary_position()`

### No trade suggestions
- Normal if roster is well-balanced
- Try creating stronger positional surpluses
- Check if other teams have similar compositions

## Next Steps (Phase 2)

- [ ] Add player-level recommendations within complementary positions
- [ ] Build interactive trade builder UI
- [ ] Integrate with Fantrax trade submission API
- [ ] Add trade value calculations (beyond just position fit)
- [ ] Historical trade tracking and analysis
- [ ] Configurable min_delta for surplus/need detection

## Success Criteria

The feature is working correctly if:

1. ✓ Unit tests pass
2. ✓ Page loads without errors
3. ✓ Division detection works (or falls back gracefully)
4. ✓ Position counts are accurate
5. ✓ Surplus/needs match manual calculation
6. ✓ Trade suggestions are logically sound
7. ✓ UI is clean and informative
8. ✓ No linter errors
9. ✓ Follows user preferences (tabs, no emojis)
10. ✓ Integrates with existing auth/session flow

