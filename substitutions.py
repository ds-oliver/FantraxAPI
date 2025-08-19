#!/usr/bin/env python3
"""
Example script showing how to make lineup substitutions using FantraxAPI.
This demonstrates the actual substitution functionality.
"""

import os
import sys
import pickle
import argparse
from pathlib import Path
from configparser import ConfigParser
from fantraxapi import FantraxAPI
from requests import Session
from dotenv import load_dotenv

def load_config():
    """Load configuration from .env or config.ini"""
    # Try loading from .env first
    load_dotenv()
    
    # Then try config.ini, which will override .env values if present
    config = ConfigParser()
    config_path = Path("config.ini")
    if config_path.exists():
        config.read(config_path)
        if "fantrax" in config:
            return {
                "league_id": config["fantrax"].get("league_id") or os.getenv("LEAGUE_ID"),
                "team_id": config["fantrax"].get("team_id") or os.getenv("TEAM_ID"),
                "cookie_path": config["fantrax"].get("cookie_path") or os.getenv("COOKIE_PATH", "deploy/fantraxloggedin.cookie")
            }
    
    # Return environment variables if no config.ini
    return {
        "league_id": os.getenv("LEAGUE_ID"),
        "team_id": os.getenv("TEAM_ID"),
        "cookie_path": os.getenv("COOKIE_PATH", "deploy/fantraxloggedin.cookie")
    }

def make_substitution_example(league_id: str, team_id: str = None):
    """Example of how to make a substitution."""

    # Load authenticated session
    session = Session()
    config = load_config()
    cookie_path = config["cookie_path"]
    
    try:
        with open(cookie_path, "rb") as f:
            for cookie in pickle.load(f):
                session.cookies.set(cookie["name"], cookie["value"])
        print("✅ Cookie session loaded successfully")
    except FileNotFoundError:
        print(f"❌ Cookie file not found at {cookie_path}! Please run the bootstrap script first:")
        print("  python bootstrap_cookie.py")
        return
    except Exception as e:
        print(f"❌ Error loading cookie: {e}")
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
    print("\n=== CURRENT LINEUP ===")
    print("Starters:")
    for row in roster.get_starters():
        print(f"  {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")

    print("\nBench:")
    for row in roster.get_bench_players():
        print(f"  {row.pos.short_name}: {row.player.name} ({row.player.team_short_name})")

    # Example: Find players to swap
    print("\n=== MAKING SUBSTITUTION ===")

    # Find a starter to move to bench
    starter_name = input("Enter name of starter to move to bench: ").strip()
    if not starter_name:
        print("No starter name provided, skipping substitution.")
        return

    starter_row = roster.get_player_by_name(starter_name)
    if not starter_row:
        print(f"Starter '{starter_name}' not found!")
        return

    if starter_row.pos_id == "0":
        print(f"'{starter_name}' is already on the bench!")
        return

    # Find a bench player to move to starters
    bench_name = input("Enter name of bench player to move to starters: ").strip()
    if not bench_name:
        print("No bench player name provided, skipping substitution.")
        return

    bench_row = roster.get_player_by_name(bench_name)
    if not bench_row:
        print(f"Bench player '{bench_name}' not found!")
        return

    if bench_row.pos_id != "0":
        print(f"'{bench_name}' is already a starter!")
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

def show_roster_analysis(league_id: str, team_id: str = None):
    """Show detailed roster analysis."""

    # Load authenticated session
    session = Session()
    try:
        with open("deploy/fantraxloggedin.cookie", "rb") as f:
            for cookie in pickle.load(f):
                session.cookies.set(cookie["name"], cookie["value"])
    except FileNotFoundError:
        print("❌ Cookie file not found! Please run the bootstrap script first:")
        print("  cd utils && python bootstrap_cookie.py")
        return
    except Exception as e:
        print(f"❌ Error loading cookie: {e}")
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
    # Load configuration
    config = load_config()
    
    parser = argparse.ArgumentParser(description='FantraxAPI Lineup Substitution Example')
    parser.add_argument('--league-id', '-l', 
                       default=config.get('league_id'),
                       help='Fantrax League ID (override config file/env var)')
    parser.add_argument('--team-id', '-t',
                       default=config.get('team_id'),
                       help='Fantrax Team ID (override config file/env var)')

    args = parser.parse_args()

    if not args.league_id:
        print("❌ Error: League ID is required!")
        print("Provide it as --league-id argument or set LEAGUE_ID environment variable")
        print("\nExample usage:")
        print("  python example_substitution.py --league-id o90qdw15mc719reh")
        print("  LEAGUE_ID=o90qdw15mc719reh python example_substitution.py")
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
                make_substitution_example(args.league_id, args.team_id)
            elif choice == "2":
                show_roster_analysis(args.league_id, args.team_id)
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