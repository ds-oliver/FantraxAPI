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

def format_trade_moves(api, moves):
    """Format trade moves into readable text."""
    lines = []
    for move in moves:
        if "budgetAmountObj" in move:
            # FAAB move
            budget = move["budgetAmountObj"]
            from_team = api.team(move["from"]["teamId"])
            to_team = api.team(move["to"]["teamId"])
            lines.append(f"FAAB: {from_team.name} → {to_team.name}: {budget['display']}")
        elif "scorer" in move:
            # Player move
            player = move["scorer"]
            from_team = api.team(move["from"]["teamId"])
            to_team = api.team(move["to"]["teamId"])
            lines.append(
                f"Player: {from_team.name} → {to_team.name}: "
                f"{player['name']} ({player['posShortNames']}, {player['teamShortName']})"
            )
    return lines

def format_trade_info(api, trade):
    """Format a trade's details into readable text."""
    lines = []
    lines.append("=" * 80)
    
    # Basic info
    lines.append(f"Trade ID: {trade['txSetId']}")
    lines.append(f"Status: {trade['status']}")
    lines.append(f"Proposed by: {trade['creatorTeamName']}")
    
    # Useful info
    for info in trade["usefulInfo"]:
        lines.append(f"{info['name']}: {info['value']}")
    
    # Moves
    lines.append("\nMoves:")
    lines.extend("  " + line for line in format_trade_moves(api, trade["moves"]))
    
    # Roster warnings
    if "illegalRosterMsgs" in trade:
        lines.append("\nRoster Warnings:")
        for team_msgs in trade["illegalRosterMsgs"]:
            team = api.team(team_msgs["teamId"])
            lines.append(f"\n{team.name}:")
            for period_msgs in team_msgs["messagesPerPeriod"]:
                lines.append(f"  {period_msgs['period']}:")
                for msg in period_msgs["messages"]:
                    lines.append(f"    - {msg}")
    
    lines.append("=" * 80)
    return "\n".join(lines)

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
    
    # Get pending trades
    print(f"\nPending Trades as of {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    trades = api.trades.list_pending()
    
    if not trades:
        print("\nNo pending trades found.")
        return
        
    for trade in trades:
        # Get full trade details
        details = api.trades.get_trade_details(trade.trade_id)
        print(format_trade_info(api, details))

if __name__ == "__main__":
    main()
