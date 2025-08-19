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
    faab_rows.append(f"{'Team Name':<30} {'FAAB':>10} {'Tradeable':>9} {'Pending Claims':>13} {'Bid Enabled':>11}")
    faab_rows.append("=" * 80)
    
    for team_id, info in sorted(team_info.items(), key=lambda x: x[1]['budget']['value'], reverse=True):
        faab_rows.append(
            f"{info['team'].name[:30]:<30} "
            f"{info['budget']['display']:>10} "
            f"{str(info['budget']['tradeable']):>9} "
            f"{info['claims']['numPendingClaims']:>13} "
            f"{str(info['claims']['showBidColumn']):>11}"
        )
    
    faab_rows.append("=" * 80)
    
    # Format league settings table
    settings_rows = []
    # Take settings from first team since they're league-wide
    first_team = next(iter(team_info.values()))
    settings_rows.append("\nLeague Claim Settings")
    settings_rows.append("=" * 50)
    
    claim_types = first_team['claims']['claimTypes']
    settings_rows.append(f"Claim Types:")
    for code, name in claim_types.items():
        settings_rows.append(f"  - {name}")
    
    settings_rows.append(f"\nClaim Groups Enabled: {first_team['claims']['claimGroupsEnabled']}")
    settings_rows.append(f"FAAB Bidding Enabled: {first_team['claims']['showBidColumn']}")
    
    if 'miscData' in first_team['claims']:
        misc = first_team['claims']['miscData']
        if 'allowGroupChanges' in misc:
            settings_rows.append(f"Allow Group Changes: {misc['allowGroupChanges']}")
        if 'showAllTeamsChoice' in misc:
            settings_rows.append(f"Show All Teams Choice: {misc['showAllTeamsChoice']}")
    
    settings_rows.append("=" * 50)
    
    return "\n".join(faab_rows + settings_rows)

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
