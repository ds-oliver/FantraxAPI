#!/usr/bin/env python3
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
	main()