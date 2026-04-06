#!/usr/bin/env python3
"""
Print your Fantrax teamId for a league using getAllLeagues (same source as Streamlit).

Auth matches ``export_fantrax_players.py``: ``--user-id`` / ``FANTRAX_USER_ID``,
``--auth-artifacts``, or ``fantraxloggedin.cookie``.

League: ``--league-id`` or ``FANTRAX_LEAGUE_ID`` / ``LEAGUE_ID``.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_export_module():
    path = _REPO_ROOT / "scripts" / "export_fantrax_players.py"
    spec = importlib.util.spec_from_file_location("export_fantrax_players", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolved_league_id(args: argparse.Namespace) -> str | None:
    if args.league_id:
        return args.league_id
    return os.environ.get("FANTRAX_LEAGUE_ID") or os.environ.get("LEAGUE_ID")


def _resolved_user_id(args: argparse.Namespace) -> str | None:
    if args.user_id:
        return args.user_id
    return os.environ.get("FANTRAX_USER_ID")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Print your Fantrax teamId for a league.")
    p.add_argument(
        "--league-id",
        default=None,
        help="League to resolve (or FANTRAX_LEAGUE_ID / LEAGUE_ID)",
    )
    p.add_argument(
        "--user-id",
        default=None,
        help="Cookies from data/auth/<id>_cookies.json (or FANTRAX_USER_ID)",
    )
    p.add_argument(
        "--auth-artifacts",
        type=Path,
        default=None,
        help="Plain JSON cookies or artifacts dict",
    )
    p.add_argument(
        "--cookie-file",
        type=Path,
        default=None,
        help="Selenium cookie pickle (default fantraxloggedin.cookie)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print league name and team name as well as ids",
    )
    p.add_argument("--log-level", default="WARNING", help="Logging (default WARNING)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING), format="%(message)s")

    league_id = _resolved_league_id(args)
    if not league_id:
        raise SystemExit("Set --league-id or FANTRAX_LEAGUE_ID or LEAGUE_ID")

    user_id = _resolved_user_id(args)
    exp = _load_export_module()

    if user_id:
        session = exp.session_from_user_id(user_id)
    elif args.auth_artifacts:
        session = exp.session_from_auth_artifacts_json(args.auth_artifacts)
    else:
        cookie_path = args.cookie_file if args.cookie_file is not None else Path("fantraxloggedin.cookie")
        session = exp.build_session(cookie_path)

    from utils.auth_helpers import fetch_user_leagues, validate_logged_in

    if not validate_logged_in(session, league_id=league_id):
        raise SystemExit(
            "Fantrax session is not logged in. Refresh login in Streamlit or update cookies."
        )

    leagues = fetch_user_leagues(session) or []
    for row in leagues:
        if str(row.get("leagueId")) == str(league_id):
            tid = row.get("teamId")
            if args.verbose:
                print(
                    f"leagueId={row.get('leagueId')} teamId={tid} "
                    f"league={row.get('league')!r} team={row.get('team')!r}"
                )
            else:
                print(tid)
            return

    raise SystemExit(
        f"No team found for league_id={league_id!r}. "
        f"Your account has {len(leagues)} league(s) in getAllLeagues; "
        "check the id or use Streamlit (sidebar shows leagueId / teamId)."
    )


if __name__ == "__main__":
    main()
