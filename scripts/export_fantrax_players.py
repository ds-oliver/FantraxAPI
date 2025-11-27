#!/usr/bin/env python3
"""
Export Fantrax players using a saved cookie (e.g. fantraxloggedin.cookie).

This avoids relying on manually curated CSVs by pulling the canonical list
straight from Fantrax's private API via FantraxAPI.get_all_players().
"""
from __future__ import annotations

import argparse
import logging
import pickle
from pathlib import Path
from typing import List

import pandas as pd
import requests
from fantraxapi.fantrax import FantraxAPI

log = logging.getLogger(__name__)


def build_session(cookie_path: Path) -> requests.Session:
    session = requests.Session()
    try:
        with cookie_path.open("rb") as f:
            cookies: List[dict] = pickle.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Cookie file not found: {cookie_path}")
    except Exception as exc:
        raise SystemExit(f"Failed to load cookie file {cookie_path}: {exc}") from exc

    for cookie in cookies:
        session.cookies.set(
            cookie.get("name"),
            cookie.get("value"),
            domain=cookie.get("domain"),
            path=cookie.get("path"),
        )
    return session


def to_dataframe(players) -> pd.DataFrame:
    rows = []
    for player in players:
        rows.append(
            {
                "id": player.id,
                "name": player.name,
                "first_name": getattr(player, "first_name", ""),
                "last_name": getattr(player, "last_name", ""),
                "team": getattr(player, "team", ""),
                "team_name": getattr(player, "team_name", ""),
                "team_short_name": getattr(player, "team_short_name", ""),
                "team_id": getattr(player, "team_id", ""),
                "position": getattr(player, "position", ""),
                "positions": ",".join(getattr(player, "positions", []) or []),
                "default_pos_id": getattr(player, "default_pos_id", ""),
                "pos_ids": ",".join(getattr(player, "pos_ids", []) or []),
                "pos_ids_no_flex": ",".join(getattr(player, "pos_ids_no_flex", []) or []),
                "status": getattr(player, "status", ""),
                "injury_status": getattr(player, "injury_status", ""),
                "rank": getattr(player, "rank", ""),
                "short_name": getattr(player, "short_name", ""),
                "url_name": getattr(player, "url_name", ""),
                "headshot_url": getattr(player, "headshot_url", ""),
                "upcoming_event_status": getattr(player, "upcoming_event_status", ""),
                "table_rank": getattr(player, "table_rank", ""),
                "owner_team": getattr(player, "owner_team", ""),
                "owner_tooltip": getattr(player, "owner_tooltip", ""),
                "owner_team_id": getattr(player, "owner_team_id", ""),
                "next_opponent_raw": getattr(player, "next_opponent_raw", ""),
                "next_opponent": getattr(player, "next_opponent", ""),
                "next_opponent_is_away": getattr(player, "next_opponent_is_away", ""),
                "next_kickoff": getattr(player, "next_kickoff", ""),
                "next_event_id": getattr(player, "next_event_id", ""),
                "season_points": getattr(player, "season_points", ""),
                "fppg_value": getattr(player, "fppg_value", ""),
                "percent_owned": getattr(player, "percent_owned", ""),
                "percent_started": getattr(player, "percent_started", ""),
                "percent_started_delta": getattr(player, "percent_started_delta", ""),
            }
        )
    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Fantrax players using saved cookies.")
    parser.add_argument("--league-id", required=True, help="Fantrax league ID")
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=Path("fantraxloggedin.cookie"),
        help="Path to Selenium-style cookie pickle (default: fantraxloggedin.cookie)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("players.csv"),
        help="CSV file to write (default: players.csv in repo root)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(message)s")

    session = build_session(args.cookie_file)
    api = FantraxAPI(league_id=args.league_id, session=session)

    log.info("Fetching players for league %s ...", args.league_id)
    players = api.get_all_players()
    if not players:
        raise SystemExit("Fantrax returned zero players; check cookies/league id.")

    df = to_dataframe(players)
    df.sort_values("name", inplace=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    log.info("Wrote %s players -> %s", len(df), args.output)


if __name__ == "__main__":
    main()
