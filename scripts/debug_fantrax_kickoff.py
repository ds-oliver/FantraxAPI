"""
Quick inspection tool to verify Fantrax `next_kickoff` data for a roster.

Usage:
    python scripts/debug_fantrax_kickoff.py \
        --auth-artifacts auth_artifacts.json \
        --league-id <LEAGUE_ID> \
        --team-id <TEAM_ID> \
        [--limit 5]

The script prints, per player:
  - player.next_kickoff
  - raw scorer.nextKickoff (if present)
  - Parsed datetime via sofascore_bridge._kickoff_from_fantrax_row
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.lineups.sofascore_bridge import _kickoff_from_fantrax_row
from utils.auth_helpers import load_requests_session_from_artifacts


def _load_artifacts(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def debug_next_kickoff(
    *,
    artifacts_path: Path,
    league_id: str,
    team_id: str,
    limit: int | None,
) -> None:
    artifacts = _load_artifacts(artifacts_path)
    session = load_requests_session_from_artifacts(artifacts)
    api = FantraxAPI(league_id=league_id, session=session)
    roster = api.roster_info(team_id)

    now = datetime.now(timezone.utc)
    print(f"Now: {now.isoformat()}")
    print("=" * 120)

    count = 0
    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue

        scorer_raw = getattr(row, "_raw", {}) or {}
        scorer_meta = scorer_raw.get("scorer") or {}

        player_next = getattr(player, "next_kickoff", None)
        scorer_next = scorer_meta.get("nextKickoff")
        parsed = _kickoff_from_fantrax_row(row)

        print(f"{player.name} ({player.id})")
        print(f"  player.next_kickoff : {player_next!r}")
        print(f"  scorer.nextKickoff  : {scorer_next!r}")
        print(f"  parsed datetime     : {parsed}")
        print(f"  fantrax raw scorer keys: {sorted(scorer_meta.keys())}")
        print("-" * 120)

        count += 1
        if limit is not None and count >= limit:
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Fantrax next_kickoff values for a roster.")
    parser.add_argument("--auth-artifacts", required=True, help="Path to auth_artifacts JSON file.")
    parser.add_argument("--league-id", required=True, help="Fantrax league ID.")
    parser.add_argument("--team-id", required=True, help="Fantrax team ID.")
    parser.add_argument("--limit", type=int, default=None, help="Optional number of players to print.")
    args = parser.parse_args()

    debug_next_kickoff(
        artifacts_path=Path(args.auth_artifacts),
        league_id=args.league_id,
        team_id=args.team_id,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
