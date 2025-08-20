#!/usr/bin/env python
"""
Script to list all players from Fantrax.
"""
import argparse
import pickle
import pandas as pd
import requests

from fantraxapi.fantrax import FantraxAPI

def main():
    parser = argparse.ArgumentParser(description="List all players from Fantrax")
    parser.add_argument(
        "--league-id",
        type=str,
        required=True,
        help="Fantrax league ID"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Optional CSV file to save results"
    )
    parser.add_argument(
        "--cookie-file",
        type=str,
        default="fantraxloggedin.cookie",
        help="Path to cookie file (default: fantraxloggedin.cookie)"
    )
    args = parser.parse_args()
    
    # Initialize session with saved cookies
    session = requests.Session()
    try:
        with open(args.cookie_file, "rb") as f:
            cookies = pickle.load(f)
            for cookie in cookies:
                session.cookies.set(cookie["name"], cookie["value"])
    except FileNotFoundError:
        print(f"Cookie file not found: {args.cookie_file}")
        print("Please run bootstrap_cookie.py first")
        return
    
    # Initialize API
    api = FantraxAPI(args.league_id, session=session)
    
    # Get all players with debug info
    print("Fetching players from Fantrax...")
    players = api.get_all_players(debug=True)
    
    if not players:
        print("\nNo players found in the response.")
        print("Please check the API response details above.")
        return
        
    # Convert to DataFrame for easier viewing
    data = []
    for player in players:
        data.append({
            "id": player.id,
            "name": player.name,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "team": player.team,
            "position": player.position,
            "positions": ", ".join(player.positions),
            "status": player.status,
            "injury_status": player.injury_status
        })
    
    df = pd.DataFrame(data)
    
    # Display summary
    print(f"\nFound {len(players)} players")
    
    if not df.empty:
        print("\nPositions breakdown:")
        if "position" in df.columns:
            print(df["position"].value_counts())
        else:
            print("No position data found")
            
        print("\nTeams breakdown:")
        if "team" in df.columns:
            print(df["team"].value_counts())
        else:
            print("No team data found")
    
    # Save to CSV if requested
    if args.output:
        df.to_csv(args.output, index=False)
        print(f"\nSaved player data to: {args.output}")
    
    # Display first few players as example
    print("\nExample players:")
    pd.set_option('display.max_columns', None)
    print(df.head())

if __name__ == "__main__":
    main()
