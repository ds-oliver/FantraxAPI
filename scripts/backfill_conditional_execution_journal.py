#!/usr/bin/env python3
"""
Backfill conditional execution journal from fired rules in per-user rule files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from utils.conditional_rule_store import (
    append_execution_event_for_user,
    load_rules_for_user,
)


def _iter_user_ids(rules_dir: Path) -> list[str]:
    out = []
    for path in sorted(rules_dir.glob("*.json")):
        out.append(path.stem)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill conditional execution journal from fired rules.")
    parser.add_argument("--user-id", default="", help="Backfill only one user.")
    args = parser.parse_args()

    rules_dir = Path("data/conditional_rules")
    user_ids = [args.user_id] if args.user_id else _iter_user_ids(rules_dir)
    inserted = 0
    deduped = 0

    for user_id in user_ids:
        rules = load_rules_for_user(str(user_id))
        for rule in rules:
            if str(rule.get("state") or "").lower() != "fired":
                continue
            fired_at = str(rule.get("fired_at") or "").strip()
            if not fired_at:
                continue
            result = str(rule.get("result") or "").strip() or "executed"
            rec = {
                "event_type": "legacy_backfill",
                "occurred_at_utc": fired_at,
                "fired_at": fired_at,
                "league_id": str(rule.get("league_id") or ""),
                "team_id": str(rule.get("team_id") or ""),
                "period": str(rule.get("period") or ""),
                "rule_id": str(rule.get("rule_id") or ""),
                "group_id": str(rule.get("group_id") or ""),
                "source": str(rule.get("source") or ""),
                "action_type": str(rule.get("action_type") or "lineup_swap"),
                "active_id": str(rule.get("active_id") or ""),
                "reserve_id": str(rule.get("reserve_id") or ""),
                "result": result,
                "reason": "legacy_fired_rule_backfill",
                "rule_snapshot": dict(rule),
            }
            resp = append_execution_event_for_user(str(user_id), rec)
            if resp.get("deduped"):
                deduped += 1
            else:
                inserted += 1

    print(f"backfill complete: inserted={inserted} deduped={deduped} users={len(user_ids)}")


if __name__ == "__main__":
    main()
