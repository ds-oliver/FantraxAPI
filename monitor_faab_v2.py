#!/usr/bin/env python3
import os
import pickle
import argparse
from datetime import datetime
from fantraxapi import FantraxAPI
from requests import Session

def load_session(cookie_path="fantraxloggedin.cookie"):
	"""Load authenticated session from cookie file."""
	print(f"\nDebug: Loading cookies from {cookie_path}")
	try:
		session = Session()
		with open(cookie_path, "rb") as f:
			cookies = pickle.load(f)
			print(f"Debug: Found {len(cookies)} cookies in file")
			for cookie in cookies:
				if "fantrax" in cookie.get("domain", ""):
					print(f"Debug: Setting cookie: {cookie['name']} for domain {cookie['domain']}")
				session.cookies.set(cookie["name"], cookie["value"])
		return session
	except FileNotFoundError:
		print(f"Debug: Cookie file not found at {cookie_path}")
		raise
	except Exception as e:
		print(f"Debug: Error loading cookies: {str(e)}")
		raise

def fetch_user_leagues(session):
	"""Fetch all leagues for the authenticated user."""
	probe = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}]}
	print("\nDebug: Sending request to Fantrax API...")
	try:
		response = session.post("https://www.fantrax.com/fxpa/req", json=probe, timeout=20)
		print(f"Debug: Response status code: {response.status_code}")
		
		if response.status_code != 200:
			print(f"Debug: Error response: {response.text}")
			return []
			
		response_json = response.json()
		print(f"Debug: Raw response: {response_json}")
		
		# Extract leagues from response
		leagues = []
		for msg in response_json.get("msgs", []):
			if msg.get("data", {}).get("leagues"):
				for league in msg["data"]["leagues"]:
					leagues.append({
						"league": league.get("leagueName", "Unknown League"),
						"leagueId": league.get("leagueId"),
						"team": league.get("userTeamName", "Unknown Team"),
						"teamId": league.get("userTeamId")
					})
		
		if not leagues:
			print("Debug: No leagues found in response data")
		else:
			print(f"Debug: Found {len(leagues)} leagues")
			
		return leagues
		
	except Exception as e:
		print(f"Debug: Exception occurred: {str(e)}")
		return []

def format_tables(api, budgets):
	"""Format budgets and claim info into pretty tables."""
	# Collect all info first
	team_info = {}
	for team_id, budget in budgets.items():
		team = api.team(team_id)
		claim_info = api.league.get_claim_info(team_id)
		team_info[team_id] = {
			'team': team,
			'budget': budget,
			'claims': claim_info
		}
	
	# Format FAAB table
	faab_rows = []
	faab_rows.append("=" * 80)
	faab_rows.append(f"{'Team Name':<30} {'FAAB':>10} {'Tradeable':>9} {'Claims':>6} {'Next Process':>25}")
	faab_rows.append("=" * 85)
	
	# Track all pending claims for details section
	all_claims = []
	
	for team_id, info in sorted(team_info.items(), key=lambda x: x[1]['budget']['value'], reverse=True):
		# Get process date from first claim if any
		next_process = ""
		if info['claims'].get('pendingClaims'):
			all_claims.extend(info['claims']['pendingClaims'])
			next_process = info['claims']['pendingClaims'][0]['process_date']
		if info['claims']['pendingClaims']:
			next_process = info['claims']['pendingClaims'][0]['process_date']
			
		faab_rows.append(
			f"{info['team'].name[:30]:<30} "
			f"{info['budget']['display']:>10} "
			f"{str(info['budget']['tradeable']):>9} "
			f"{len(info['claims']['pendingClaims']):>6} "
			f"{next_process:>25}"
		)
	
	faab_rows.append("=" * 80)
	
	# Format league settings and pending claims
	detail_rows = []
	# Take settings from first team since they're league-wide
	first_team = next(iter(team_info.values()))
	
	# League settings
	detail_rows.append("\nLeague Claim Settings")
	detail_rows.append("=" * 50)
	
	claim_types = first_team['claims']['claimTypes']
	detail_rows.append(f"Claim Types:")
	for code, name in claim_types.items():
		detail_rows.append(f"  - {name}")
	
	detail_rows.append(f"\nClaim Groups Enabled: {first_team['claims']['claimGroupsEnabled']}")
	detail_rows.append(f"FAAB Bidding Enabled: {first_team['claims']['showBidColumn']}")
	
	if 'miscData' in first_team['claims']:
		misc = first_team['claims']['miscData']
		if 'allowGroupChanges' in misc:
			detail_rows.append(f"Allow Group Changes: {misc['allowGroupChanges']}")
		if 'showAllTeamsChoice' in misc:
			detail_rows.append(f"Show All Teams Choice: {misc['showAllTeamsChoice']}")
	
	detail_rows.append("=" * 50)
	
	# Pending claims details
	for team_id, info in team_info.items():
		claims = info['claims']['pendingClaims']
		if claims:
			detail_rows.append(f"\nPending Claims for {info['team'].name}:")
			detail_rows.append("-" * 50)
			for claim in claims:
				claim_text = []
				claim_text.append(f"Process: {claim['process_date']}")
				if claim['claim_player']:
					claim_text.append(f"Add: {claim['claim_player']['name']} ({claim['claim_player']['position']}, {claim['claim_player']['team']}) -> {claim['claim_player']['to_position']}/{claim['claim_player']['to_status']}")
				if claim['drop_player']:
					claim_text.append(f"Drop: {claim['drop_player']['name']} ({claim['drop_player']['position']}, {claim['drop_player']['team']}) from {claim['drop_player']['from_position']}/{claim['drop_player']['from_status']}")
				if claim['bid_amount']:
					claim_text.append(f"Bid: ${claim['bid_amount']:.2f}")
				claim_text.append(f"Priority: {claim['priority']}")
				if claim['group']:
					claim_text.append(f"Group: {claim['group']}")
				claim_text.append(f"Submitted: {claim['submitted_date']}")
				detail_rows.append(" | ".join(claim_text))
			detail_rows.append("-" * 50)
	
	detail_rows.append("=" * 50)
	
	return "\n".join(faab_rows + detail_rows)

def load_config(config_path="config.ini"):
	"""Load configuration from config.ini."""
	print(f"\nDebug: Loading config from {config_path}")
	import configparser
	config = configparser.ConfigParser()
	config.read(config_path)
	
	if "fantrax" not in config:
		print("Debug: No [fantrax] section found in config.ini")
		print("Debug: Using default cookie path: fantraxloggedin.cookie")
		return {"cookie_path": "fantraxloggedin.cookie"}
		
	cookie_path = config["fantrax"].get("cookie_path", "fantraxloggedin.cookie")
	print(f"Debug: Using cookie path from config: {cookie_path}")
	return {"cookie_path": cookie_path}

def main():
	# Parse command line arguments
	parser = argparse.ArgumentParser(description='Monitor FAAB for Fantrax leagues')
	parser.add_argument('--league-id', help='Specific league ID to monitor (optional)')
	args = parser.parse_args()

	# Load config from config.ini
	config = load_config()
	
	# Initialize session
	session = load_session(config["cookie_path"])
	
	# Fetch all available leagues
	leagues = fetch_user_leagues(session)
	if not leagues:
		print("No leagues found or error fetching leagues. Check your cookie.")
		return

	# If league_id is provided, filter to that league
	if args.league_id:
		leagues = [l for l in leagues if l["leagueId"] == args.league_id]
		if not leagues:
			print(f"League ID {args.league_id} not found in your leagues")
			return
	
	# If multiple leagues and no specific league selected, let user choose
	if len(leagues) > 1 and not args.league_id:
		print("\nAvailable leagues:")
		for i, league in enumerate(leagues, 1):
			print(f"{i}. {league['league']} (Your team: {league['team']})")
		
		while True:
			try:
				choice = int(input("\nSelect league number (or 0 to monitor all): "))
				if choice == 0:
					break
				if 1 <= choice <= len(leagues):
					leagues = [leagues[choice - 1]]
					break
				print("Invalid choice. Please try again.")
			except ValueError:
				print("Please enter a valid number.")
	
	# Monitor FAAB for selected leagues
	for league in leagues:
		api = FantraxAPI(league["leagueId"], session=session)
		
		print(f"\n{'=' * 30} {league['league']} {'=' * 30}")
		print(f"League FAAB & Claims Status as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
		
		try:
			budgets = api.league.faab_budgets()
			print(format_tables(api, budgets))
		except Exception as e:
			print(f"Error fetching FAAB data: {e}")

if __name__ == "__main__":
	main()