#!/usr/bin/env python3
"""
Fetch Fantrax icon/typeId lineup signals once and cache globally.

This avoids per-league FXPA calls on every Streamlit page load by storing
fantrax_player_id -> status metadata in data/fantrax/global_icons.json.
"""
from __future__ import annotations

import argparse
import json
import pickle
import logging
from datetime import datetime, timezone
from pathlib import Path

from fantraxapi.lineups.fantrax_lineup_bridge import (
    DEFAULT_GLOBAL_STATUS_PATH,
    fetch_fantrax_player_status_snapshot,
    global_status_path_for_user,
    parse_fantrax_player_statuses,
)
from utils.user_manager import UserManager

try:
    from utils.auth_helpers import load_requests_session_from_artifacts
except ImportError:
    load_requests_session_from_artifacts = None  # type: ignore


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(path: Path, players: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": iso_now(), "players": players}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def build_players_dict(statuses: dict) -> dict:
    result = {}
    for pid, st in statuses.items():
        result[pid] = {
            "status": st.status.value if hasattr(st.status, "value") else str(st.status),
            "icons": st.icons,
            "event_id": st.event_id,
            "kickoff": st.kickoff.isoformat() if st.kickoff else None,
            "team_name": st.team_name,
            "opponent_name": st.opponent_name,
            "is_home": st.is_home,
        }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cache Fantrax icon/typeId lineup statuses globally.")
    parser.add_argument("--league-id", required=True, help="Any league id you can authenticate against (FXPA requires it).")
    parser.add_argument("--user-id", help="Load cookies for this user_id from data/auth (preferred for multi-user).")
    parser.add_argument(
        "--auth-artifacts",
        type=Path,
        default=Path("auth_artifacts.json"),
        help="Auth artifacts used to rebuild Fantrax session (JSON with cookies or pickle produced by bootstrap_cookie.py).",
    )
    parser.add_argument(
        "--cookie-pickle",
        type=Path,
        default=Path("fantraxloggedin.cookie"),
        help="Optional pickle of cookies from bootstrap_cookie.py; used if auth artifacts are missing.",
    )
    parser.add_argument(
        "--materialize-auth-artifacts",
        type=Path,
        default=None,
        help="If set, write the derived artifacts JSON to this path (useful to keep a JSON copy).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write the cached snapshot (defaults to per-user path when --user-id is set).",
    )
    parser.add_argument("--max-results", type=int, default=600, help="Max players to request from FXPA.")
    parser.add_argument("--misc-display-type", default="10", help="FXPA miscDisplayType (default=10: Starting view).")
    parser.add_argument("--status-filter", default="ALL", help="FXPA statusOrTeam filter (e.g., ALL, ALL_AVAILABLE).")
    return parser.parse_args()


def _normalize_artifacts(artifacts):
    if isinstance(artifacts, list):
        return {"cookies": artifacts}
    return artifacts


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if load_requests_session_from_artifacts is None:
        raise SystemExit("utils.auth_helpers.load_requests_session_from_artifacts is required for auth.")
    artifacts = None
    if args.user_id:
        user_mgr = UserManager()
        artifacts = user_mgr.load_user_cookies(str(args.user_id))
        if artifacts is None:
            raise SystemExit(f"No stored cookies for user_id={args.user_id}.")
    else:
        # Preferred: explicit auth artifacts if present
        if args.auth_artifacts and args.auth_artifacts.exists():
            if args.auth_artifacts.suffix == ".cookie":
                cookies = pickle.load(args.auth_artifacts.open("rb"))
                artifacts = {"cookies": cookies}
            else:
                with args.auth_artifacts.open("r", encoding="utf-8") as fh:
                    artifacts = json.load(fh)
        # Fallback: use cookie pickle
        if artifacts is None and args.cookie_pickle and args.cookie_pickle.exists():
            cookies = pickle.load(args.cookie_pickle.open("rb"))
            artifacts = {"cookies": cookies}
            if args.materialize_auth_artifacts:
                args.materialize_auth_artifacts.parent.mkdir(parents=True, exist_ok=True)
                args.materialize_auth_artifacts.write_text(json.dumps(artifacts, indent=2), encoding="utf-8")
                logging.info("Materialized auth artifacts JSON -> %s", args.materialize_auth_artifacts)
    artifacts = _normalize_artifacts(artifacts)
    if artifacts is None:
        raise SystemExit("No auth artifacts found; provide --user-id, --auth-artifacts, or --cookie-pickle.")
    if args.output:
        out_path = args.output
    elif args.user_id:
        out_path = global_status_path_for_user(str(args.user_id))
    else:
        out_path = DEFAULT_GLOBAL_STATUS_PATH
    session = load_requests_session_from_artifacts(artifacts)
    payload = fetch_fantrax_player_status_snapshot(
        session=session,
        league_id=args.league_id,
        misc_display_type=args.misc_display_type,
        status_filter=args.status_filter,
        max_results=args.max_results,
    )
    statuses = parse_fantrax_player_statuses(payload)
    players = build_players_dict(statuses)
    save_snapshot(out_path, players)
    logging.info("Cached %s player statuses -> %s", len(players), out_path)


if __name__ == "__main__":
    main()
