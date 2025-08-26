#!/usr/bin/env python3
"""Script for dropping players from Fantrax teams."""

import argparse
import configparser
import logging
import os
from pathlib import Path
from typing import Optional

from fantraxapi import FantraxAPI
from fantraxapi.exceptions import FantraxException
from requests import Session
import pickle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_config(config_path="config.ini"):
	"""Load configuration from config.ini."""
	config = configparser.ConfigParser()
	if Path(config_path).exists():
		config.read(config_path)
	return {
		"league_id": config["fantrax"].get("league_id", ""),
		"team_id": config["fantrax"].get("team_id", ""),
		"cookie_path": config["fantrax"].get("cookie_path", "deploy/fantraxloggedin.cookie"),
		"team_name": config["fantrax"].get("team_name", ""),
	}


def load_session(cookie_path: str) -> Session:
	"""Load a requests Session with saved Fantrax cookies."""
	session = Session()
	if not Path(cookie_path).exists():
		raise FantraxException(f"Cookie file not found: {cookie_path}")
	
	with open(cookie_path, "rb") as f:
		for cookie in pickle.load(f):
			session.cookies.set(cookie["name"], cookie["value"])
	return session


def choose_league_and_team_interactively(api: FantraxAPI, prefer_id: str = "", prefer_name: str = "") -> tuple[str, str]:
	"""Interactive league and team selection with support for preferred team.
	
	Returns:
		Tuple of (league_id, team_id)
	"""
	print("\n" + "="*60)
	print(" League and Team Selection")
	print("="*60)

	# Get leagues and teams from API response
	json_data = {
		"msgs": [
			{
				"method": "getAllLeagues",
				"data": {
					"view": "LEAGUES"
				}
			}
		],
		"uiv": 3,
		"refUrl": "https://www.fantrax.com/fantasy/league",
		"dt": 0,
		"at": 0,
		"av": "0.0",
		"tz": "America/Los_Angeles",
		"v": "167.0.1"
	}
	
	# Make the request with required headers
	headers = {
		"accept": "application/json",
		"content-type": "text/plain",
		"referer": "https://www.fantrax.com/fantasy/league",
		"sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
		"sec-ch-ua-mobile": "?0",
		"sec-ch-ua-platform": '"macOS"'
	}
	
	response = api._session.post(
		"https://www.fantrax.com/fxpa/req",
		json=json_data,
		headers=headers
	).json()
	
	# Debug print
	print("\nAPI Response:", response)
	
	# Parse the response
	if not response or not isinstance(response, dict) or "responses" not in response:
		raise FantraxException(f"Invalid response format: {response}")
		
	user_data = response["responses"][0].get("data", {})
	if not user_data or "tableList" not in user_data:
		raise FantraxException(f"No leagues data in response: {user_data}")
	
	# Collect all leagues from all seasons
	leagues_data = []
	for table in user_data["tableList"]:
		for row in table.get("rows", []):
			cells = row.get("cells", [])
			if len(cells) >= 4:	 # We need at least year, type, name and team cells
				league_cell = cells[2]	# Name column
				team_cell = cells[3]   # Team column
				if ("leagueId" in league_cell and "content" in league_cell and
					"teamId" in team_cell and "content" in team_cell):
					leagues_data.append({
						"leagueId": league_cell["leagueId"],
						"teamId": team_cell["teamId"],
						"league": league_cell["content"],
						"team": team_cell["content"]
					})
	
	# Filter out leagues with "NULL" teamId and sort by league name
	active_leagues = [lt for lt in leagues_data if lt["teamId"] != "NULL"]
	active_leagues.sort(key=lambda x: x["league"])

	# Try preference hints first
	if prefer_id:
		for lt in active_leagues:
			if lt["teamId"] == prefer_id:
				print(f"Found team (from config team_id):")
				print(f"  {lt['team']} - {lt['league']} ({lt['teamId']})")
				yn = input("\nUse this team? [Y/n]: ").strip().lower()
				if yn in ("", "y", "yes"):
					return lt["leagueId"], lt["teamId"]

	if prefer_name:
		for lt in active_leagues:
			if prefer_name.lower() in lt["team"].lower():
				print(f"Found team (from config team_name):")
				print(f"  {lt['team']} - {lt['league']} ({lt['teamId']})")
				yn = input("\nUse this team? [Y/n]: ").strip().lower()
				if yn in ("", "y", "yes"):
					return lt["leagueId"], lt["teamId"]

	# Fallback: list leagues and teams for selection
	print("\nAvailable leagues and teams:")
	print("-"*60)
	for i, lt in enumerate(active_leagues, 1):
		print(f"{i:2d}. {lt['league']}")
		print(f"	Team: {lt['team']} ({lt['teamId']})")
	print("-"*60)
	
	while True:
		try:
			sel = input("Enter league number: ").strip()
			idx = int(sel)
			if 1 <= idx <= len(active_leagues):
				selected = active_leagues[idx - 1]
				return selected["leagueId"], selected["teamId"]
			print("Invalid selection, try again.")
		except ValueError:
			print("Invalid selection, try again.")


def select_leagues(api: FantraxAPI) -> list[tuple[str, str, str, str]]:
	"""Let user select which leagues to check.
	
	Returns:
		List of tuples (league_id, team_id, league_name, team_name)
	"""
	# Get leagues data
	json_data = {
		"msgs": [
			{
				"method": "getAllLeagues",
				"data": {
					"view": "LEAGUES"
				}
			}
		],
		"uiv": 3,
		"refUrl": "https://www.fantrax.com/fantasy/league",
		"dt": 0,
		"at": 0,
		"av": "0.0",
		"tz": "America/Los_Angeles",
		"v": "167.0.1"
	}
	
	# Make the request with required headers
	headers = {
		"accept": "application/json",
		"content-type": "text/plain",
		"referer": "https://www.fantrax.com/fantasy/league",
		"sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
		"sec-ch-ua-mobile": "?0",
		"sec-ch-ua-platform": '"macOS"'
	}
	
	response = api._session.post(
		"https://www.fantrax.com/fxpa/req",
		json=json_data,
		headers=headers
	).json()
	
	if not response or not isinstance(response, dict) or "responses" not in response:
		raise FantraxException(f"Invalid response format: {response}")
		
	user_data = response["responses"][0].get("data", {})
	if not user_data or "tableList" not in user_data:
		raise FantraxException(f"No leagues data in response: {user_data}")
	
	# Collect current season leagues
	current_leagues = []
	for table in user_data["tableList"]:
		if table.get("caption") != "Seasons Starting in 2025":
			continue
			
		for row in table.get("rows", []):
			cells = row.get("cells", [])
			if len(cells) >= 4:
				year_cell = cells[0]
				if not (year_cell.get("icon") == "EPL" and year_cell.get("content") == "2025-26"):
					continue
					
				league_cell = cells[2]
				team_cell = cells[3]
				if ("leagueId" in league_cell and "content" in league_cell and
					"teamId" in team_cell and "content" in team_cell):
					current_leagues.append((
						league_cell["leagueId"],
						team_cell["teamId"],
						league_cell["content"],
						team_cell["content"]
					))
	
	# Let user select leagues
	print("\nAvailable leagues (current season):")
	print("-"*60)
	for i, (_, _, league_name, team_name) in enumerate(current_leagues, 1):
		print(f"{i:2d}. {team_name} ({league_name})")
	print("-"*60)
	print("Enter league numbers to include (comma-separated, or 'all' for all leagues)")
	print("Example: 1,3,5 or all")
	
	while True:
		try:
			sel = input("Select leagues: ").strip().lower()
			if sel == "all":
				return current_leagues
				
			indices = [int(x.strip()) for x in sel.split(",")]
			selected = []
			for idx in indices:
				if 1 <= idx <= len(current_leagues):
					selected.append(current_leagues[idx - 1])
				else:
					print(f"Invalid selection: {idx}")
					break
			else:
				if selected:
					return selected
			
		except ValueError:
			print("Invalid input. Please enter comma-separated numbers or 'all'")

def get_all_rosters(api: FantraxAPI) -> dict:
	"""Get rosters from selected leagues.
	
	Returns:
		Dict mapping league_id -> team_id -> roster
	"""
	# Let user select leagues
	selected_leagues = select_leagues(api)
	
	# Collect rosters from selected leagues
	print("\nFetching rosters...")
	all_rosters = {}
	
	for league_id, team_id, league_name, team_name in selected_leagues:
		print(f"  - {team_name} ({league_name})")
		
		# Save original API league_id
		orig_league_id = api.league_id
		
		try:
			# Switch to this league
			api.league_id = league_id
			# Get roster
			roster = api.roster_info(team_id)
			
			# Store with league/team info
			if league_id not in all_rosters:
				all_rosters[league_id] = {}
			all_rosters[league_id][team_id] = {
				"roster": roster,
				"league_name": league_name,
				"team_name": team_name
			}
		except Exception as e:
			print(f"Warning: Failed to get roster for {league_name} - {team_name}: {e}")
		finally:
			# Restore original league_id
			api.league_id = orig_league_id
	
	return all_rosters

def choose_player_to_drop(api: FantraxAPI, team_id: str, player_id: Optional[str] = None) -> tuple[str, bool]:
	"""Interactive player selection for dropping.
	
	Returns:
		Tuple of (player_id, drop_from_all_teams)
	"""
	if player_id:
		# Verify player is on any roster
		all_rosters = get_all_rosters(api)
		player_teams = []
		player_name = None
		
		for league_id, teams in all_rosters.items():
			for tid, info in teams.items():
				roster = info["roster"]
				for row in roster.rows:
					if row.player and row.player.id == player_id:
						player_name = row.player.name
						player_teams.append(f"{info['team_name']} ({info['league_name']})")
						break
		
		if player_teams:
			print(f"\nFound player to drop: {player_name} ({player_id})")
			print("Present on teams:")
			for team in player_teams:
				print(f"  - {team}")
			yn = input("\nDrop this player? [Y/n]: ").strip().lower()
			if yn in ("", "y", "yes"):
				return player_id, True	# Always drop from all teams when player_id provided
		else:
			print(f"Warning: Player {player_id} not found on any roster")

	# Show all players across all rosters
	print("\nPlayers across all rosters:")
	print("-"*100)
	
	# Get all rosters
	all_rosters = get_all_rosters(api)
	
	# Build player -> teams mapping
	player_teams = {}
	for league_id, teams in all_rosters.items():
		for team_id, info in teams.items():
			roster = info["roster"]
			for row in roster.rows:
				if row.player:
					if row.player.id not in player_teams:
						player_teams[row.player.id] = {
							"name": row.player.name,
							"teams": []
						}
					player_teams[row.player.id]["teams"].append(
						f"{info['team_name']} ({info['league_name']})"
					)
	
	# Display sorted by player name
	players = []
	for player_id, info in sorted(player_teams.items(), key=lambda x: x[1]["name"]):
		players.append({
			"id": player_id,
			"name": info["name"],
			"teams": info["teams"]
		})
		print(f"{len(players):2d}. {info['name']} - {player_id}")
		print("	  Teams:")
		for team in info["teams"]:
			print(f"	 - {team}")
	print("-"*100)
	
	while True:
		try:
			sel = input("Enter player number to drop: ").strip()
			idx = int(sel)
			if 1 <= idx <= len(players):
				player = players[idx - 1]
				if len(player["teams"]) > 1:
					print(f"\nPlayer {player['name']} is on multiple teams:")
					for team in player["teams"]:
						print(f"  - {team}")
					all_teams = input("\nDrop from all teams? [Y/n]: ").strip().lower()
					return player["id"], all_teams not in ("n", "no")
				return player["id"], True  # Always drop from all if only on one team
			print("Invalid selection, try again.")
		except ValueError:
			print("Invalid selection, try again.")


def show_action_menu() -> str:
	"""Show main action menu and get user choice."""
	print("\n" + "="*60)
	print(" Choose Action")
	print("="*60)
	print("1) Drop a player")
	print("2) Add a free agent")
	print("3) Submit waiver claim")
	print("-"*60)
	
	while True:
		choice = input("Enter choice (1-3): ").strip()
		if choice in ("1", "2", "3"):
			return choice
	
def browse_available_players(api: FantraxAPI, status: str = "FREE_AGENT") -> dict:
	"""Browse or search for available players.
	
	Args:
		status: "FREE_AGENT" or "WAIVER_WIRE"
	"""
	# If looking for free agents, show general note
	if status == "FREE_AGENT":
		print("\nNote: Free agents may not be available during waiver periods")
	print("\n" + "="*60)
	print(" Player Selection")
	print("="*60)
	print("Choose how to select a player:")
	print("	 1) Search by name")
	print("	 2) Browse by position")
	mode = input("\nEnter 1 or 2: ").strip()
	
	players = []
	if mode == "1":
		query = input("\nSearch player name: ").strip()
		players = api.waivers.search_players(query)
	else:
		print("\nPosition group to browse:")
		print("-"*60)
		print("	 1) All players")
		print("	 2) Defenders")
		print("	 3) Midfielders")
		print("	 4) Forwards")
		print("	 5) Goalkeepers")
		print("-"*60)
		grp = input("Enter 1-5 [default 1]: ").strip()
		# For "All players" we pass None to not send posOrGroup
		mapping = {
			"1": None,			# All players
			"2": "POS_703",		# D
			"3": "POS_702",		# M
			"4": "POS_701",		# F
			"5": "POS_704",		# GK
		}
		pos = mapping.get(grp or "1")
		players = api.waivers.list_players_by_name(
			limit=15,
			pos_or_group=pos,
			status=status
		)
	
	if not players:
		print("\nNo players found.")
		return None
		
	print("\nAvailable players:")
	print("-"*60)
	for idx, p in enumerate(players, 1):
		print(f"{idx:2d}. {p['name']} ({p.get('position') or ''}, {p.get('team') or ''}) - {p['id']}")
	print("-"*60)
	
	while True:
		try:
			sel = int(input("Enter player number (or 0 to cancel): ").strip())
			if sel == 0:
				return None
			if 1 <= sel <= len(players):
				return players[sel - 1]
		except ValueError:
			print("Invalid selection, try again.")

def main():
	parser = argparse.ArgumentParser(description="Manage your Fantrax roster - add, drop, and waiver claims")
	parser.add_argument("--league-id", help="League ID (defaults to config)")
	parser.add_argument("--team-id", help="Team ID (defaults to config or interactive)")
	parser.add_argument("--action", choices=["drop", "add", "claim"], help="Action to perform")
	parser.add_argument("--player-id", help="Player ID to add/drop/claim")
	parser.add_argument("--drop-id", help="Player ID to drop (for add/claim)")
	parser.add_argument("--bid", type=float, help="FAAB bid amount for waiver claim")
	parser.add_argument("--all-teams", action="store_true", help="Drop from all teams that have the player")
	parser.add_argument("--period", type=int, help="Period/gameweek number (defaults to current)")
	parser.add_argument("--skip-validation", action="store_true", help="Skip validation checks")
	args = parser.parse_args()

	# Load config
	cfg = load_config()

	# Initialize API - we'll get the league ID after team selection
	try:
		session = load_session(cfg["cookie_path"])
		# Initialize with a temporary league ID, we'll update it after selection
		api = FantraxAPI("temp", session=session)
	except Exception as e:
		print(f"Error initializing API: {e}")
		return 1

	# Get team ID and league ID if needed
	team_id = None
	league_id = None
	if not args.all_teams:
		# Get from args/config first
		team_id = args.team_id or cfg.get("team_id")
		league_id = args.league_id or cfg.get("league_id")
		
		# If not provided or invalid, use interactive selection
		try:
			if league_id and team_id:
				# Verify the league/team combo exists
				json_data = {
					"msgs": [
						{
							"method": "getAllLeagues",
							"data": {
								"view": "LEAGUES"
							}
						}
					],
					"uiv": 3,
					"refUrl": "https://www.fantrax.com/fantasy/league",
					"dt": 0,
					"at": 0,
					"av": "0.0",
					"tz": "America/Los_Angeles",
					"v": "167.0.1"
				}
				
				# Make the request with required headers
				headers = {
					"accept": "application/json",
					"content-type": "text/plain",
					"referer": "https://www.fantrax.com/fantasy/league",
					"sec-ch-ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
					"sec-ch-ua-mobile": "?0",
					"sec-ch-ua-platform": '"macOS"'
				}
				
				response = api._session.post(
					"https://www.fantrax.com/fxpa/req",
					json=json_data,
					headers=headers
				).json()
				
				if not response or not isinstance(response, dict) or "responses" not in response:
					raise FantraxException(f"Invalid response format: {response}")
					
				user_data = response["responses"][0].get("data", {})
				if not user_data or "tableList" not in user_data:
					raise FantraxException(f"No leagues data in response: {user_data}")
				
				# Collect all leagues from all seasons
				leagues_data = []
				for table in user_data["tableList"]:
					for row in table.get("rows", []):
						cells = row.get("cells", [])
						if len(cells) >= 4:	 # We need at least year, type, name and team cells
							league_cell = cells[2]	# Name column
							team_cell = cells[3]   # Team column
							if ("leagueId" in league_cell and "content" in league_cell and
								"teamId" in team_cell and "content" in team_cell):
								leagues_data.append({
									"leagueId": league_cell["leagueId"],
									"teamId": team_cell["teamId"],
									"league": league_cell["content"],
									"team": team_cell["content"]
								})
				valid = False
				for lt in leagues_data:
					if lt["leagueId"] == league_id and lt["teamId"] == team_id:
						valid = True
						break
				if not valid:
					league_id, team_id = choose_league_and_team_interactively(
						api, 
						prefer_id=team_id,
						prefer_name=cfg.get("team_name")
					)
			else:
				league_id, team_id = choose_league_and_team_interactively(
					api,
					prefer_id=team_id,
					prefer_name=cfg.get("team_name")
				)
		except Exception as e:
			print(f"Error selecting league/team: {e}")
			return 1

		# Update API with selected league
		api.league_id = league_id

	try:
		# Get action from args or menu
		action = args.action
		if not action:
			choice = show_action_menu()
			if choice == "1":
				action = "drop"
			elif choice == "2":
				action = "add"
			else:
				action = "claim"

		if action == "drop":
			# Handle drop action
			player_id = args.player_id
			drop_from_all = args.all_teams
			
			if not args.all_teams and not args.player_id:
				# Interactive selection
				player_id, drop_from_all = choose_player_to_drop(api, team_id)
			
			if drop_from_all:
				print(f"\nAttempting to drop player {player_id} from all teams...")
				results = api.drops.drop_player_from_all_teams(
					scorer_id=player_id,
					period=args.period,
					skip_validation=args.skip_validation
				)
				
				# Show results
				print("\nDrop results:")
				print("="*60)
				for team_id, result in results.items():
					status = "✅ Success" if result["success"] else "❌ Failed"
					if result["error"]:
						status += f": {result['error']}"
					print(f"{result['team_name']} ({team_id}): {status}")
					
			else:
				# Single team drop
				print(f"\nDropping player {player_id} from team {team_id}...")
				success = api.drops.drop_player(
					team_id=team_id,
					scorer_id=player_id,
					period=args.period,
					skip_validation=args.skip_validation
				)
				print("✅ Drop successful!" if success else "❌ Drop failed!")

		elif action == "add":
			# Handle free agent add
			print("\nNote: Free agents may not be available during waiver period")
			player = args.player_id
			if not player:
				player_info = browse_available_players(api, status="FREE_AGENT")
				if not player_info:
					print("No player selected.")
					return 1
				player = player_info["id"]

			# See if we need to drop someone
			drop_id = args.drop_id
			if not drop_id:
				yn = input("\nDo you need to drop a player? [y/N]: ").strip().lower()
				if yn == "y":
					drop_info, _ = choose_player_to_drop(api, team_id)
					if drop_info:
						drop_id = drop_info

			# Submit the add
			print(f"\nAdding player {player} to team {team_id}...")
			if drop_id:
				print(f"Dropping player {drop_id}")
			
			result = api.waivers.submit_claim(
				team_id=team_id,
				claim_scorer_id=player,
				bid_amount=0,  # Free agent
				drop_scorer_id=drop_id,
				to_status_id="2",  # Reserve by default
			)
			print("\nAdd result:")
			print("-"*60)
			print(result)
			print("-"*60)

		else:  # claim
			# Handle waiver claim
			player = args.player_id
			if not player:
				player_info = browse_available_players(api, status="WAIVER_WIRE")
				if not player_info:
					print("No player selected.")
					return 1
				player = player_info["id"]

			# Get bid amount
			bid = args.bid
			if bid is None:
				while True:
					try:
						bid = float(input("\nEnter bid amount: ").strip())
						break
					except ValueError:
						print("Invalid bid amount, try again.")

			# See if we need to drop someone
			drop_id = args.drop_id
			if not drop_id:
				yn = input("\nDo you need to drop a player? [y/N]: ").strip().lower()
				if yn == "y":
					drop_info, _ = choose_player_to_drop(api, team_id)
					if drop_info:
						drop_id = drop_info

			# Submit the claim
			print(f"\nSubmitting claim for player {player} (bid: ${bid:.2f})...")
			if drop_id:
				print(f"Will drop player {drop_id}")
			
			result = api.waivers.submit_claim(
				team_id=team_id,
				claim_scorer_id=player,
				bid_amount=bid,
				drop_scorer_id=drop_id,
				to_status_id="2",  # Reserve by default
			)
			print("\nClaim result:")
			print("-"*60)
			print(result)
			print("-"*60)
			
	except Exception as e:
		print(f"Error: {e}")
		return 1

	return 0


if __name__ == "__main__":
	exit(main())
