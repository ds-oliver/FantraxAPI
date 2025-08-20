#!/usr/bin/env python3
import pickle
from fantraxapi import FantraxAPI
from requests import Session


def load_session(cookie_path: str) -> Session:
    session = Session()
    with open(cookie_path, "rb") as f:
        for cookie in pickle.load(f):
            session.cookies.set(cookie["name"], cookie["value"])
    return session


def load_config(config_path="config.ini"):
    import configparser
    config = configparser.ConfigParser()
    config.read(config_path)
    if "fantrax" not in config:
        raise ValueError("config.ini must have a [fantrax] section")
    return {
        "league_id": config["fantrax"]["league_id"],
        "cookie_path": config["fantrax"]["cookie_path"],
        "team_id": config["fantrax"].get("team_id", ""),
        "team_name": config["fantrax"].get("team_name", ""),
    }


def choose_team_interactively(api: FantraxAPI, prefer_id: str = "", prefer_name: str = "") -> str:
    print("\n" + "="*60)
    print(" Team Selection")
    print("="*60)

    # Try preference hints first
    if prefer_id:
        try:
            t = api.get_team_by_id(prefer_id)
            print(f"Found team (from config team_id):")
            print(f"  {t.name} ({getattr(t, 'short_name', '')}) - {t.team_id}")
            yn = input("\nUse this team? [Y/n]: ").strip().lower()
            if yn in ("", "y", "yes"):
                return t.team_id
        except Exception:
            pass

    if prefer_name:
        t = api.find_team_by_name(prefer_name)
        if t:
            print(f"Found team (from config team_name):")
            print(f"  {t.name} ({getattr(t, 'short_name', '')}) - {t.team_id}")
            yn = input("\nUse this team? [Y/n]: ").strip().lower()
            if yn in ("", "y", "yes"):
                return t.team_id

    # Fallback: list teams for selection
    print("\nAvailable teams:")
    print("-"*60)
    for i, t in enumerate(api.teams, 1):
        print(f"{i:2d}. {t.name} ({getattr(t, 'short_name', '')}) - {t.team_id}")
    print("-"*60)
    sel = input("Enter team number: ").strip()
    idx = int(sel)
    return api.teams[idx - 1].team_id


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Submit a waiver claim")
    parser.add_argument("--team-id", dest="team_id", help="Your fantasy team ID (defaults to config or prompt)")
    parser.add_argument("--claim-player-id", dest="claim_player_id", help="Player ID to claim (omit to use interactive search)")
    parser.add_argument("--bid", type=float, default=0.0, help="FAAB bid amount (default 0)")
    parser.add_argument("--drop", dest="drop_player_id", help="Player ID to drop (optional; omit to select interactively)")
    parser.add_argument("--to-pos", dest="to_position_id", help="Position ID to place claimed player (optional)")
    parser.add_argument("--to-status", dest="to_status_id", default="2", help="Status ID for claimed player (1=Active, 2=Reserve)")
    parser.add_argument("--priority", type=int, help="Claim priority (optional)")
    parser.add_argument("--group", type=int, help="Claim group number (optional)")
    args = parser.parse_args()

    cfg = load_config()
    session = load_session(cfg["cookie_path"])
    api = FantraxAPI(cfg["league_id"], session=session)

    # Resolve team_id → prefer CLI, then config stored values, else prompt
    team_id = args.team_id or cfg.get("team_id", "")
    team_id = choose_team_interactively(api, prefer_id=team_id, prefer_name=cfg.get("team_name", ""))

    # Interactive selection if player not provided
    claim_player_id = args.claim_player_id
    if not claim_player_id:
        print("\n" + "="*60)
        print(" Player Selection")
        print("="*60)
        print("Choose how to select a player:")
        print("  1) Search by name")
        print("  2) Browse a list")
        mode = input("\nEnter 1 or 2: ").strip()
        players = []
        if mode == "1":
            query = input("\nSearch player name: ").strip()
            players = api.waivers.search_players(query)
        else:
            print("\nPosition group to browse:")
            print("-"*60)
            print("  1) All players")
            print("  2) Defenders")
            print("  3) Midfielders")
            print("  4) Forwards")
            print("  5) Goalkeepers")
            print("-"*60)
            grp = input("Enter 1-5 [default 1]: ").strip()
            # IMPORTANT: for "All players" we pass None so we DO NOT send posOrGroup to the API.
            mapping = {
                "1": None,          # All players
                "2": "POS_703",     # D
                "3": "POS_702",     # M
                "4": "POS_701",     # F
                "5": "POS_704",     # GK
            }
            pos = mapping.get(grp or "1")
            players = api.waivers.list_players_by_name(limit=15, pos_or_group=pos, status="ALL_AVAILABLE")
        if not players:
            print("\nNo players found.")
            return

        print("\nAvailable players:")
        print("-"*60)
        for idx, p in enumerate(players, 1):
            print(f"{idx:2d}. {p['name']} ({p.get('position') or ''}, {p.get('team') or ''}) - {p['id']}")
        print("-"*60)
        sel = int(input("Enter player number: ").strip())
        chosen = players[sel - 1]
        claim_player_id = chosen["id"]
        # If no --to-pos provided, prefer the player's defaultPosId when sending to Active
        if not args.to_position_id and args.to_status_id == "1":
            args.to_position_id = (chosen.get("default_pos_id") or "").strip() or None

    # Interactive bid amount if not provided (keep default shown)
    print("\n" + "="*60)
    print(" Claim Details")
    print("="*60)
    bid_amount = args.bid
    bid_in = input(f"Bid amount [default {bid_amount}]: ").strip()
    if bid_in:
        bid_amount = float(bid_in)

    # Optional drop selection if not provided
    drop_player_id = args.drop_player_id
    if drop_player_id is None:
        choice = input("\nDrop a player? [y/N]: ").strip().lower()
        if choice == "y":
            roster = api.roster_info(team_id)
            roster_players = []
            for row in roster.rows:
                if row.player:
                    roster_players.append(row.player)
            print("\nYour roster:")
            print("-"*60)
            for idx, pl in enumerate(roster_players, 1):
                pos_short = getattr(pl, "position_short", "") or ""
                team_short = getattr(pl, "team_short", "") or ""
                print(f"{idx:2d}. {pl.name} ({pos_short}, {team_short}) - {pl.id}")
            print("-"*60)
            sel = int(input("Enter number to drop: ").strip())
            drop_player_id = roster_players[sel - 1].id

    print("\n" + "="*60)
    print(" Submitting Claim")
    print("="*60)

    result = api.waivers.submit_claim(
        team_id=team_id,
        claim_scorer_id=claim_player_id,
        bid_amount=bid_amount,
        drop_scorer_id=drop_player_id,
        to_position_id=args.to_position_id,
        to_status_id=args.to_status_id,
        priority=args.priority,
        group=args.group,
    )

    print("\nClaim submission result:")
    print("-"*60)
    print(result)
    print("-"*60)


if __name__ == "__main__":
    main()