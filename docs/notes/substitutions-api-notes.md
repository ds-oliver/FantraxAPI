Here's the key information for making successful substitutions:

1. Request Structure:
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "data": {
    "rosterLimitPeriod": 3,
    "fantasyTeamId": "ekzha63dmcm8fxhp",
    "confirm": true,
    "applyToFuturePeriods": true,
    "fieldMap": {
      "PLAYER_ID": {
        "posId": "701-704",  // Numeric position IDs
        "stId": "1 or 2"     // 1=starter, 2=bench
      }
    }
  }
}
```

2. Position ID Mapping:
- 701 = Forward (F)
- 702 = Midfielder (M)
- 703 = Defender (D)
- 704 = Goalkeeper (G)

3. Status ID Mapping:
- stId: "1" = Active/Starter
- stId: "2" = Reserve/Bench

4. Critical Fields:
- Must include ALL players in fieldMap (both starters and bench)
- Must use numeric posIds (not string codes)
- Must set applyToFuturePeriods=true when playerPickDeadlinePassed=true

5. Two-Step Process:
```
1. Confirm (confirm=true) -> Returns changes to review
2. Execute (confirm=false) -> Actually applies changes
```

Here's how the two-step substitution process works:

STEP 1 (Previous) - Confirm Request:
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "data": {
    "confirm": true,  // First step is confirm
    "applyToFuturePeriods": true,
    "fieldMap": {...}  // Full roster positions
  }
}
```

STEP 2 (This) - Execute Request:
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "data": {
    "confirm": false,  // Second step removes confirm flag
    "applyToFuturePeriods": true,  // Keep this true when deadline passed
    "fieldMap": {...}  // Same exact fieldMap as step 1
  }
}
```

Key Differences in Step 2:
1. `confirm` flag is removed/set to false
2. Everything else stays identical
3. Server responds with:
   ```json
   "fantasyResponse": {
     "msgType": "CONFIRM",
     "lineupChanges": [],  // Empty means success
     "changeAllowed": true
   }
   ```

Implementation Strategy:
1. First call with confirm=true to validate changes
2. If validation succeeds (changeAllowed=true), make identical call without confirm flag
3. Keep applyToFuturePeriods=true when playerPickDeadlinePassed=true
4. Use exact same fieldMap in both calls

Looking at the sequence of API responses:

STEP 1 (Previous) - Initial Confirm:
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "data": {
    "confirm": true,
    "fieldMap": {...}
  }
}
```

STEP 2 (This) - Execute:
```json
{
  "method": "getTeamRosterInfo",  // Server requests fresh roster info
  "data": {
    "leagueId": "2n2fdfaxmcm64shc",
    "period": "3"
  }
}
```

STEP 3 (Response) - Server Returns:
1. Full roster details including:
   ```json
   "statusTotals": [
     {"total": 11, "max": 11, "name": "Active", "id": "1"},
     {"total": 6, "max": 7, "name": "Reserve", "id": "2"}
   ]
   ```

2. Player details with:
   - posIdsNoFlex: Base position (e.g. ["702"])
   - defaultPosId: Default position (e.g. "701")
   - posShortNames: Display position (e.g. "M")
   - statusId: Current status ("1"=active, "2"=reserve)

Implementation Strategy:
1. After confirm succeeds, server wants fresh roster state
2. Need to verify our changes took effect by comparing:
   - Player positions match what we requested
   - Active/Reserve counts match expectations
   - No unexpected position/status changes

Here's what's happening in this multi-step substitution flow:

STEP 1 - Moving D to Active (Current Response):
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "msgType": "WARNING",  // Different from previous "CONFIRM"
  "lineupChanges": [
    {
      "scorerId": "04tm0",  // Digne
      "text": "Reserve to Active"
    }
  ],
  "illegalRosterMsgs": [
    "The maximum number of <b>11</b> active player(s) will be exceeded."
  ]
}
```

Key Differences from Previous Approach:
1. Previous: Tried to do all changes at once
   ```json
   fieldMap: {
     "player1": {"posId": "702", "stId": "1"},
     "player2": {"posId": "702", "stId": "2"}
   }
   ```

2. New: Must do changes sequentially
   - First: Move defender to active spot
   - Then: Move midfielder to reserve
   - Server enforces this order by warning about 11+ active players

3. Server Behavior:
   - Previous: Rejected bulk change silently
   - Now: Gives explicit warning about roster size
   - Allows proceeding despite warning ("Click OK to execute anyway")

Implementation Strategy:
1. Break changes into ordered steps:
   ```python
   # Step 1: Move D to active (triggers warning)
   # Step 2: Move M to reserve (fixes roster size)
   # Step 3: Verify final positions
   ```

STEP 2 - Confirming Illegal State (After Warning):

Request:
```json
{
  "method": "confirmOrExecuteTeamRosterChanges",
  "data": {
    // Same fieldMap as before, showing:
    // - Digne (04tm0) now active ("stId": "1")
    // - Still have all other actives
    // - Results in 12 active players (illegal)
  }
}
```

Response:
```json
{
  "fantasyResponse": {
    "msgType": "CONFIRM",  // Changed from WARNING to CONFIRM
    "lineupChanges": [],   // Empty because we accepted warning
    "illegalRosterMsgs": [
      "The maximum number of <b>11</b> active player(s) has been exceeded."
    ],
    "changeAllowed": true  // Key: Allows proceeding despite illegal state
  }
}
```

Sequential Flow Now Clear:
1. First Request: Move D to active → Server warns about 12 players
2. Second Request: Confirm anyway → Server accepts illegal state
3. Next Step Needed: Move M to reserve to fix roster size

Implementation Strategy:
```python
def execute_position_swap(out_player, in_player):
    # 1. Move in_player to active (triggers warning)
    response = submit_change(in_player, status="1")
    if "WARNING" in response:
        # 2. Confirm illegal state
        response = confirm_change()
    # 3. Move out_player to reserve
    response = submit_change(out_player, status="2")
```

Looking at this roster response, here are the key insights for our substitution workflow:

1. Player Status Structure:
```json
"statusId": "1",  // Active/Starting XI
"statusId": "2",  // Reserve/Bench
"eligibleStatusIds": ["1", "2"]  // Valid positions for player
```

2. Position Mapping:
```json
"posIdsNoFlex": ["702"],  // Base position
"defaultPosId": "701",    // Default position
"posShortNames": "M",     // Display name
"posIds": ["702"]         // All eligible positions

// Position ID mapping:
"alwaysShowPositionIds": [
  "701",  // Forward (F)
  "702",  // Midfielder (M)
  "703",  // Defender (D)
  "704"   // Goalkeeper (G)
]
```

3. Roster Validation:
```json
"statusTotals": [
  {"total": 12, "max": 11, "name": "Active", "id": "1"},  // Currently illegal state
  {"total": 5, "max": 7, "name": "Reserve", "id": "2"}
]
```

4. Key Workflow Flags:
```json
"showConfirmWindow": true,
"changeAllowed": true,  // Can proceed despite illegal state
"applyToFuturePeriods": true
```

This shows we need to:
1. First get roster into legal state (11 active players)
2. Then make position changes
3. Handle future periods flag when deadline passed
4. Track both posId and statusId for each player

This shows the second step in the sequential substitution workflow:

1. Moving Player to Reserve:
```json
"lineupChanges": [
  {
    "scorerId": "0524k",  // Rodri
    "text": "Active to Reserve"
  }
]
```

2. Full fieldMap Required:
```json
"fieldMap": {
  // Must include ALL players with their positions
  "06fuw": {"posId": "704", "stId": "1"},  // GK stays active
  "06p4k": {"posId": "703", "stId": "1"},  // D stays active
  // ...
  "0524k": {"stId": "2"}  // Key change: Moving Rodri to bench
}
```

3. Server Response Flags:
```json
"model": {
  "playerPickDeadlinePassed": true,
  "changeAllowed": true,
  "illegalBefore": false,
  "numIllegalRosterMsgs": 0  // Now legal with 11 players
}
```

Workflow Pattern:
1. First request: Move player to active (caused illegal 12-player state)
2. This request: Move Rodri to bench (restores legal 11-player state)
3. Server confirms change is allowed despite deadline passing
4. Changes will apply to future periods (applyToFuturePeriods: true)

This validates our need to:
1. Make changes one at a time
2. Keep full fieldMap state
3. Handle both position (posId) and status (stId) changes
4. Track roster legality between steps

Looking at the roster reload response, here are the key insights:

1. Roster Status After Changes:
```json
"statusTotals": [
  {"total": 10, "name": "Active", "id": "1"},  // Now legal (was 12)
  {"total": 6, "name": "Reserve", "id": "2"}   // Rodri moved here
]
```

2. Player States:
```json
// Example player structure
{
  "scorerId": "0524k",  // Rodri
  "posIdsNoFlex": ["702"],  // Base position (M)
  "statusId": "2",  // Now on bench
  "eligibleStatusIds": ["1", "2"],  // Can be starter or bench
  "posId": "702"  // Current position
}
```

3. Display Settings:
```json
"displayedSelections": {
  "displayedPeriod": 3,
  "lineupChangeSystem": "EASY_CLICK",
  "displayedStartDate": 1755244800000
}
```

This confirms:
1. Roster is now legal (10 starters + GK)
2. Rodri successfully moved to bench
3. Changes will apply to period 3 (Aug 29 - Sep 11)
4. All position IDs and eligibility preserved

The sequential workflow successfully:
1. First moved player to active (caused illegal state)
2. Then moved Rodri to bench (restored legal state)
3. Maintained all position assignments
4. Applied to future period as needed

`#!/usr/bin/env python3
"""
Example script showing how to make lineup substitutions using FantraxAPI.
This demonstrates the actual substitution functionality.
"""

# substitutions_v2.py

import os
import sys
import pickle
import argparse
import configparser
from pathlib import Path
from typing import Optional
from fantraxapi import FantraxAPI
from requests import Session

def _load_authenticated_session(cookie_path: str) -> Optional[Session]:
	"""Load an authenticated session using a cookie file.

	Attempts to load from the provided cookie_path, and if not found,
	falls back to a cookie in the repository root named 'fantraxloggedin.cookie'.
	"""
	session = Session()
	primary_path = Path(cookie_path) if cookie_path else Path("deploy/fantraxloggedin.cookie")
	fallback_path = Path("fantraxloggedin.cookie")

	try:
		cookie_file_to_use = None
		if primary_path.exists():
			cookie_file_to_use = primary_path
		elif fallback_path.exists():
			cookie_file_to_use = fallback_path

		if cookie_file_to_use is None:
			print("❌ Cookie file not found! Please run the bootstrap script first:")
			print("	 python bootstrap_cookie.py")
			print(f"Expected at '{primary_path}' or '{fallback_path}'")
			return None

		with open(cookie_file_to_use, "rb") as f:
			for cookie in pickle.load(f):
				session.cookies.set(cookie["name"], cookie["value"])
		print(f"✅ Cookie session loaded from {cookie_file_to_use}")
		return session
	except Exception as e:
		print(f"❌ Error loading cookie: {e}")
		return None


def make_substitution_example(league_id: str, team_id: Optional[str] = None, cookie_path: Optional[str] = None):
	"""Example of how to make a substitution."""
	
	# Load authenticated session
	session = _load_authenticated_session(cookie_path or "deploy/fantraxloggedin.cookie")
	if session is None:
		return
	
	# Initialize API with authenticated session
	api = FantraxAPI(league_id, session=session)
	
	# Get the specified team or default to first team
	if team_id:
		try:
			my_team = api.team(team_id)
		except Exception as e:
			print(f"❌ Error finding team {team_id}: {e}")
			return
	else:
		my_team = api.teams[0]
		print(f"⚠️  No team_id provided, using first team: {my_team.name}")
	
	print(f"Working with team: {my_team.name}")
	
	# Get current roster
	roster = api.roster_info(my_team.team_id)
	
	# Show current lineup
	starters = roster.get_starters()
	bench = roster.get_bench_players()

	print("\n=== CURRENT LINEUP ===")
	print("Starters:")
	for idx, row in enumerate(starters, start=1):
		print(f"  {idx}. {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")
	
	print("\nBench:")
	if not bench:
		print("	 (no bench players detected)")
	else:
		for idx, row in enumerate(bench, start=1):
			print(f"  {idx}. {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")
	
	# Example: Find players to swap
	print("\n=== MAKING SUBSTITUTION ===")
	
	# Find a starter to move to bench (index or name)
	starter_input = input("Select starter to move to bench (number or name): ").strip()
	if not starter_input:
		print("No selection provided, skipping substitution.")
		return

	starter_row = None
	if starter_input.isdigit():
		idx = int(starter_input)
		if 1 <= idx <= len(starters):
			starter_row = starters[idx - 1]
		else:
			print("Invalid starter number.")
			return
	else:
		candidate = roster.get_player_by_name(starter_input)
		if candidate and candidate.pos_id != "0":
			starter_row = candidate
		else:
			print(f"Starter '{starter_input}' not found among starters.")
			return

	# Find a bench player to move to starters (index or name)
	if not bench:
		print("No bench players available to swap in.")
		return

	bench_input = input("Select bench player to move to starters (number or name): ").strip()
	if not bench_input:
		print("No selection provided, skipping substitution.")
		return

	bench_row = None
	if bench_input.isdigit():
		idx = int(bench_input)
		if 1 <= idx <= len(bench):
			bench_row = bench[idx - 1]
		else:
			print("Invalid bench number.")
			return
	else:
		candidate = roster.get_player_by_name(bench_input)
		if candidate and candidate.pos_id == "0":
			bench_row = candidate
		else:
			print(f"Bench player '{bench_input}' not found among bench players.")
			return
	
	# Confirm the swap
	print(f"\nAbout to swap:")
	print(f"  OUT: {starter_row.player.name} ({starter_row.pos.short_name}) → Bench")
	print(f"  IN:  {bench_row.player.name} ({starter_row.pos.short_name}) → Starters")
	
	confirm = input("\nProceed with this substitution? (yes/no): ").strip().lower()
	if confirm not in ['yes', 'y']:
		print("Substitution cancelled.")
		return
	
	# Make the substitution
	try:
		print("\nExecuting substitution...")
		success = api.swap_players(my_team.team_id, starter_row.player.id, bench_row.player.id)
		
		if success:
			print("✅ Substitution successful!")
			
			# Refresh roster to show changes
			print("\nRefreshing roster...")
			new_roster = api.roster_info(my_team.team_id)
			
			print("\n=== UPDATED LINEUP ===")
			print("Starters:")
			for row in new_roster.get_starters():
				print(f"  {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")
			
			print("\nBench:")
			for row in new_roster.get_bench_players():
				print(f"  {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")
		else:
			print("❌ Substitution failed!")
			
	except Exception as e:
		print(f"❌ Error making substitution: {e}")

def show_roster_analysis(league_id: str, team_id: Optional[str] = None, cookie_path: Optional[str] = None):
	"""Show detailed roster analysis."""
	
	# Load authenticated session
	session = _load_authenticated_session(cookie_path or "deploy/fantraxloggedin.cookie")
	if session is None:
		return
	
	api = FantraxAPI(league_id, session=session)
	
	# Get the specified team or default to first team
	if team_id:
		try:
			my_team = api.team(team_id)
		except Exception as e:
			print(f"❌ Error finding team {team_id}: {e}")
			return
	else:
		my_team = api.teams[0]
		print(f"⚠️  No team_id provided, using first team: {my_team.name}")
	
	roster = api.roster_info(my_team.team_id)
	
	print(f"\n=== ROSTER ANALYSIS FOR {my_team.name} ===")
	
	# Position breakdown
	positions = {}
	for row in roster.rows:
		if row.player:
			pos = row.pos.short_name
			if pos not in positions:
				positions[pos] = {"starters": 0, "bench": 0}
			
			if row.pos_id == "0":
				positions[pos]["bench"] += 1
			else:
				positions[pos]["starters"] += 1
	
	for pos, counts in positions.items():
		print(f"{pos}: {counts['starters']} starters, {counts['bench']} bench")
	
	# Top performers (by FPPG)
	starters = roster.get_starters()
	starters_with_fppg = [row for row in starters if row.fppg is not None]
	starters_with_fppg.sort(key=lambda x: x.fppg, reverse=True)
	
	if starters_with_fppg:
		print(f"\nTop 5 starters by FPPG:")
		for i, row in enumerate(starters_with_fppg[:5]):
			print(f"  {i+1}. {row.player.name}: {row.fppg:.1f} FPPG")

def main():
	# Load defaults from config.ini if present
	config = configparser.ConfigParser()
	config_path = Path("config.ini")
	if config_path.exists():
		config.read(config_path)
		cfg_league_id = config.get("fantrax", "league_id", fallback=None)
		cfg_team_id = config.get("fantrax", "team_id", fallback=None)
		cfg_cookie_path = config.get("fantrax", "cookie_path", fallback=None)
	else:
		cfg_league_id = None
		cfg_team_id = None
		cfg_cookie_path = None

	default_league_id = os.getenv('LEAGUE_ID') or cfg_league_id
	default_team_id = os.getenv('TEAM_ID') or cfg_team_id
	default_cookie_path = os.getenv('COOKIE_PATH') or cfg_cookie_path or 'deploy/fantraxloggedin.cookie'

	parser = argparse.ArgumentParser(description='FantraxAPI Lineup Substitution Example')
	parser.add_argument('--league-id', '-l', 
					   default=default_league_id,
					   help='Fantrax League ID (or set LEAGUE_ID env var)')
	parser.add_argument('--team-id', '-t',
					   default=default_team_id,
					   help='Fantrax Team ID (or set TEAM_ID env var)')
	parser.add_argument('--cookie-path', '-c',
					   default=default_cookie_path,
					   help='Path to saved Fantrax login cookies (or set COOKIE_PATH env var)')
	
	args = parser.parse_args()
	
	if not args.league_id:
		print("❌ Error: League ID is required!")
		print("Provide it as --league-id argument or set LEAGUE_ID environment variable")
		print("\nExample usage:")
		print("	 python subs_v1.py --league-id o90qdw15mc719reh")
		print("	 LEAGUE_ID=o90qdw15mc719reh python subs_v1.py")
		sys.exit(1)
	
	try:
		print("FantraxAPI Lineup Substitution Example")
		print("=" * 40)
		
		while True:
			print("\nOptions:")
			print("1. Make a substitution")
			print("2. Show roster analysis")
			print("3. Exit")
			
			choice = input("\nSelect an option (1-3): ").strip()
			
			if choice == "1":
				make_substitution_example(args.league_id, args.team_id, args.cookie_path)
			elif choice == "2":
				show_roster_analysis(args.league_id, args.team_id, args.cookie_path)
			elif choice == "3":
				print("Goodbye!")
				break
			else:
				print("Invalid choice. Please select 1, 2, or 3.")
				
	except Exception as e:
		print(f"Error: {e}")
		print("Make sure you have a valid Fantrax cookie and are logged in.")

if __name__ == "__main__":
	main()`

This script demonstrates the correct way to make lineup changes using the Fantrax API. Here are the key successful patterns:

1. Single Swap Approach:
```python
success = api.swap_players(my_team.team_id, starter_row.player.id, bench_row.player.id)
```
- Makes one change at a time
- Simply swaps one starter with one bench player
- No complex position mapping needed

2. Roster Validation:
```python
# Get current roster state before changes
roster = api.roster_info(my_team.team_id)
starters = roster.get_starters()
bench = roster.get_bench_players()

# After changes, verify the update
new_roster = api.roster_info(my_team.team_id)
```
- Gets fresh roster state before each operation
- Verifies changes after submission
- Maintains roster legality

3. Position Handling:
```python
# Uses player's current position when moving to starting lineup
print(f"  IN:  {bench_row.player.name} ({starter_row.pos.short_name}) → Starters")
```
- Preserves the position of the slot being replaced
- No need to send full fieldMap
- Lets server handle position validation

4. Simple API Flow:
```python
api = FantraxAPI(league_id, session=session)
my_team = api.team(team_id)
roster = api.roster_info(my_team.team_id)
success = api.swap_players(...)
```
- Clean API abstraction
- Simple method calls
- No complex payload construction

The key insight is that this script succeeds by:
1. Making one change at a time
2. Using simple swap operations
3. Letting the server handle position validation
4. Verifying changes after each operation

This is much simpler than our current approach of trying to submit all changes at once with complex fieldMaps.