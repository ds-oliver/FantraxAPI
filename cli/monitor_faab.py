#!/usr/bin/env python3
import os
import pickle
from datetime import datetime
from fantraxapi import FantraxAPI
from requests import Session

def load_session(cookie_path="fantraxloggedin.cookie"):
	"""Load authenticated session from cookie file."""
	session = Session()
	with open(cookie_path, "rb") as f:
		for cookie in pickle.load(f):
			session.cookies.set(cookie["name"], cookie["value"])
	return session

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
	import configparser
	config = configparser.ConfigParser()
	config.read(config_path)
	
	if "fantrax" not in config:
		raise ValueError("config.ini must have a [fantrax] section")
		
	return {
		"league_id": config["fantrax"]["league_id"],
		"cookie_path": config["fantrax"]["cookie_path"]
	}

def main():
	# Load config from config.ini
	config = load_config()
	
	# Initialize API client
	session = load_session(config["cookie_path"])
	api = FantraxAPI(config["league_id"], session=session)
	
	# Get all FAAB budgets and format tables
	print(f"\nLeague FAAB & Claims Status as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
	budgets = api.league.faab_budgets()
	print(format_tables(api, budgets))

if __name__ == "__main__":
	main()
