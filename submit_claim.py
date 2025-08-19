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
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Submit a waiver claim")
    parser.add_argument("team_id", help="Your fantasy team ID")
    parser.add_argument("claim_player_id", help="Player ID to claim")
    parser.add_argument("--bid", type=float, default=0.0, help="FAAB bid amount (default 0)")
    parser.add_argument("--drop", dest="drop_player_id", help="Player ID to drop (optional)")
    parser.add_argument("--to-pos", dest="to_position_id", help="Position ID to place claimed player (optional)")
    parser.add_argument("--to-status", dest="to_status_id", default="2", help="Status ID for claimed player (1=Active, 2=Reserve)")
    parser.add_argument("--priority", type=int, help="Claim priority (optional)")
    parser.add_argument("--group", type=int, help="Claim group number (optional)")
    args = parser.parse_args()

    config = load_config()
    session = load_session(config["cookie_path"])
    api = FantraxAPI(config["league_id"], session=session)

    result = api.waivers.submit_claim(
        team_id=args.team_id,
        claim_scorer_id=args.claim_player_id,
        bid_amount=args.bid,
        drop_scorer_id=args.drop_player_id,
        to_position_id=args.to_position_id,
        to_status_id=args.to_status_id,
        priority=args.priority,
        group=args.group,
    )

    print("Claim submission result:")
    print(result)


if __name__ == "__main__":
    main()


