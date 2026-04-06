#!/usr/bin/env python3
"""
Export Fantrax players using a saved cookie (e.g. fantraxloggedin.cookie).

League and user can be set via environment: ``FANTRAX_LEAGUE_ID`` (or ``LEAGUE_ID``)
and ``FANTRAX_USER_ID``; CLI flags override env.

Also supports the same auth the Streamlit app persists: encrypted
``data/auth/<user_id>_cookies.json`` via ``--user-id``, or a plain JSON
artifacts file via ``--auth-artifacts`` (dict with cookies/storage or a list
of cookie dicts), matching ``conditional_runner.py`` / ``auth_helpers``.

This avoids relying on manually curated CSVs by pulling the canonical list
straight from Fantrax's private API via FantraxAPI.get_all_players().
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import sys
from pathlib import Path
from typing import Any, List, Union

import pandas as pd
import requests
from fantraxapi.fantrax import FantraxAPI

log = logging.getLogger(__name__)

# Repo root on sys.path when run as ``python scripts/export_fantrax_players.py``
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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


def session_from_artifacts(artifacts: Union[dict, list]) -> requests.Session:
    """Build a Fantrax API session from Streamlit/conditional_runner-style artifacts."""
    from utils.auth_helpers import (
        load_requests_session_from_artifacts,
        load_requests_session_from_cookie_list,
    )

    if isinstance(artifacts, list):
        return load_requests_session_from_cookie_list(artifacts)
    if isinstance(artifacts, dict):
        return load_requests_session_from_artifacts(artifacts)
    raise SystemExit(f"Unsupported artifacts type: {type(artifacts)}")


def session_from_user_id(user_id: str) -> requests.Session:
    """Load encrypted (or plain) cookies from ``data/auth/<user_id>_cookies.json``."""
    from utils.user_manager import UserManager

    um = UserManager()
    artifacts = um.load_user_cookies(user_id)
    if not artifacts:
        raise SystemExit(
            f"No saved cookies for user_id={user_id!r} "
            f"(expected {um.get_user_cookie_path(user_id)})"
        )
    return session_from_artifacts(artifacts)


def session_from_auth_artifacts_json(path: Path) -> requests.Session:
    """Load plain JSON: either a list of cookies or a dict with cookies (+ optional storage)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot read auth artifacts file {path}: {exc}") from exc
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    return session_from_artifacts(data)


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


def _resolved_league_id(args: argparse.Namespace) -> str | None:
    """CLI overrides env: FANTRAX_LEAGUE_ID, then LEAGUE_ID."""
    if args.league_id:
        return args.league_id
    return os.environ.get("FANTRAX_LEAGUE_ID") or os.environ.get("LEAGUE_ID")


def _resolved_user_id(args: argparse.Namespace) -> str | None:
    """CLI overrides env FANTRAX_USER_ID."""
    if args.user_id:
        return args.user_id
    return os.environ.get("FANTRAX_USER_ID")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Fantrax players using saved cookies.")
    parser.add_argument(
        "--league-id",
        default=None,
        help="Fantrax league ID (or env FANTRAX_LEAGUE_ID or LEAGUE_ID)",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Use cookies from data/auth/<user_id>_cookies.json (or env FANTRAX_USER_ID)",
    )
    parser.add_argument(
        "--auth-artifacts",
        type=Path,
        default=None,
        help="Plain JSON path: cookie list or {cookies, storage} dict (not encrypted)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=None,
        help="Selenium-style cookie pickle (default path fantraxloggedin.cookie if --user-id and --auth-artifacts omitted)",
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

    league_id = _resolved_league_id(args)
    if not league_id:
        raise SystemExit(
            "Missing league id: pass --league-id or set FANTRAX_LEAGUE_ID or LEAGUE_ID"
        )

    user_id = _resolved_user_id(args)

    if user_id:
        log.info("Using saved auth for user_id=%s", user_id)
        session = session_from_user_id(user_id)
    elif args.auth_artifacts:
        log.info("Using auth artifacts from %s", args.auth_artifacts)
        session = session_from_auth_artifacts_json(args.auth_artifacts)
    else:
        cookie_path = args.cookie_file if args.cookie_file is not None else Path("fantraxloggedin.cookie")
        session = build_session(cookie_path)

    from utils.auth_helpers import validate_logged_in

    if not validate_logged_in(session, league_id=league_id):
        raise SystemExit(
            "Fantrax session is not logged in (getAllLeagues). "
            "Refresh login in Streamlit or update cookies, then retry."
        )

    api = FantraxAPI(league_id=league_id, session=session)

    log.info("Fetching players for league %s ...", league_id)
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
