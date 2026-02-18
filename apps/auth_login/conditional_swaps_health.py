"""
Pure logic helpers for conditional swaps health reconciliation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


_ROUND_PATTERNS = [
    re.compile(r"\bgw\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bgameweek\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bweek\s*(\d+)\b", re.IGNORECASE),
]


def resolve_round_from_period(
    selected_period_id: Optional[str],
    selected_period_label: Optional[str],
    inferred_round: Optional[str],
) -> Optional[str]:
    """
    Resolve EPL round from user-selected period label/id.
    Returns None when no round can be inferred from the selected period.
    """
    label = str(selected_period_label or "")
    for pattern in _ROUND_PATTERNS:
        match = pattern.search(label)
        if match:
            return str(int(match.group(1)))

    period_id = str(selected_period_id or "").strip()
    if period_id.isdigit():
        # Many leagues use period id == gameweek number.
        return str(int(period_id))

    return None


def fetch_lineup_change_history(api: Any, team_id: str, max_rows: int = 200) -> List[dict]:
    """
    Fetch raw lineup-change transaction rows for one team.
    """
    if not team_id:
        return []
    data = api._request(
        "getTransactionDetailsHistory",
        maxResultsPerPage=str(max_rows),
        view="LINEUP_CHANGE",
        executedOnly=True,
        includeDeleted=False,
        team=str(team_id),
    )
    table = (data or {}).get("table") or {}
    rows = table.get("rows") or []
    return rows if isinstance(rows, list) else []


def _parse_fantrax_date_to_utc(raw_date: Optional[str], user_tz: str = "America/Los_Angeles") -> Optional[datetime]:
    if not raw_date:
        return None
    text = str(raw_date).strip()
    formats = [
        "%a %b %d, %Y, %I:%M%p",
        "%a %b %d, %Y, %I:%M %p",
    ]
    for fmt in formats:
        try:
            naive = datetime.strptime(text, fmt)
            local_dt = naive.replace(tzinfo=ZoneInfo(user_tz))
            return local_dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _cell_value_map(cells: Any) -> Dict[str, dict]:
    mapped: Dict[str, dict] = {}
    if not isinstance(cells, list):
        return mapped
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        key = str(cell.get("key") or "").strip()
        if key:
            mapped[key] = cell
    return mapped


def normalize_lineup_change_rows(rows: List[dict]) -> List[dict]:
    """
    Normalize Fantrax lineup-change rows into transaction-set level events.
    """
    grouped: Dict[str, dict] = {}
    row_order = 0
    for row in rows:
        row_order += 1
        if not isinstance(row, dict):
            continue
        tx_set_id = str(row.get("txSetId") or "").strip()
        if not tx_set_id:
            tx_set_id = f"unknown:{row_order}"
        cells = _cell_value_map(row.get("cells") or [])
        team_cell = cells.get("team") or {}
        date_cell = cells.get("date") or {}
        period_cell = cells.get("weekOrPeriod") or {}
        from_cell = cells.get("from") or {}
        to_cell = cells.get("to") or {}

        event = grouped.setdefault(
            tx_set_id,
            {
                "tx_set_id": tx_set_id,
                "team_id": team_cell.get("teamId"),
                "executed": bool(row.get("executed")),
                "week_or_period": None,
                "date_local": None,
                "date_utc": None,
                "moves": [],
                "_first_seen": row_order,
            },
        )

        if not event.get("team_id") and team_cell.get("teamId"):
            event["team_id"] = team_cell.get("teamId")
        if period_cell.get("content"):
            event["week_or_period"] = str(period_cell.get("content"))

        date_local = date_cell.get("content")
        if date_local:
            event["date_local"] = str(date_local)
            event["date_utc"] = _parse_fantrax_date_to_utc(str(date_local))

        scorer = row.get("scorer") or {}
        move = {
            "player_id": str(scorer.get("scorerId") or ""),
            "player_name": scorer.get("name"),
            "from_slot": from_cell.get("content"),
            "to_slot": to_cell.get("content"),
        }
        if move["player_id"] or move["from_slot"] or move["to_slot"]:
            event["moves"].append(move)

    normalized = list(grouped.values())
    for event in normalized:
        moves = event.get("moves") or []
        deduped = []
        seen = set()
        for move in moves:
            key = (
                str(move.get("player_id") or ""),
                str(move.get("from_slot") or ""),
                str(move.get("to_slot") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(move)
        event["moves"] = deduped

    normalized.sort(
        key=lambda e: (
            e.get("date_utc") or datetime.min.replace(tzinfo=timezone.utc),
            -int(e.get("_first_seen") or 0),
        ),
        reverse=True,
    )
    for event in normalized:
        event.pop("_first_seen", None)
    return normalized


def build_fired_conditional_events(rules: List[dict], league_id: str, team_id: str) -> List[dict]:
    """
    Build normalized app-side fired conditional lineup swap events from saved rules.
    """
    events: List[dict] = []
    for rule in rules or []:
        if str(rule.get("league_id") or "") != str(league_id):
            continue
        if str(rule.get("team_id") or "") != str(team_id):
            continue
        if str(rule.get("state") or "").lower() != "fired":
            continue
        action_type = str(rule.get("action_type") or "").strip().lower()
        if action_type and action_type != "lineup_swap":
            continue
        fired_at = str(rule.get("fired_at") or "").strip()
        if not fired_at:
            continue
        try:
            fired_at_utc = datetime.fromisoformat(fired_at)
        except Exception:
            continue
        if fired_at_utc.tzinfo is None:
            fired_at_utc = fired_at_utc.replace(tzinfo=timezone.utc)
        else:
            fired_at_utc = fired_at_utc.astimezone(timezone.utc)

        active_id = str(rule.get("active_id") or "").strip()
        reserve_id = str(rule.get("reserve_id") or "").strip()
        if not active_id or not reserve_id:
            continue
        events.append(
            {
                "rule_id": str(rule.get("rule_id") or ""),
                "fired_at_utc": fired_at_utc,
                "active_id": active_id,
                "reserve_id": reserve_id,
                "period": str(rule.get("period") or ""),
                "source": str(rule.get("source") or ""),
                "result": str(rule.get("result") or ""),
                "team_id": str(team_id),
            }
        )
    events.sort(key=lambda e: e["fired_at_utc"], reverse=True)
    return events


def _slot_has(slot: Any, needle: str) -> bool:
    return needle.lower() in str(slot or "").lower()


def _event_has_expected_swap(event: dict, active_id: str, reserve_id: str) -> bool:
    moves = event.get("moves") or []
    active_ok = False
    reserve_ok = False
    for move in moves:
        pid = str(move.get("player_id") or "")
        from_slot = move.get("from_slot")
        to_slot = move.get("to_slot")
        if pid == active_id:
            active_ok = _slot_has(from_slot, "active") and _slot_has(to_slot, "reserve")
        if pid == reserve_id:
            reserve_ok = _slot_has(from_slot, "reserve") and _slot_has(to_slot, "active")
    return active_ok and reserve_ok


def match_events(app_events: List[dict], fantrax_events: List[dict], window_seconds: int = 120) -> List[dict]:
    """
    Match app-fired conditional swap events to Fantrax lineup-change events.
    """
    matched_rows: List[dict] = []
    used_tx_ids: set[str] = set()

    for app_event in app_events:
        best: Optional[dict] = None
        best_delta: Optional[int] = None
        app_time = app_event.get("fired_at_utc")
        active_id = str(app_event.get("active_id") or "")
        reserve_id = str(app_event.get("reserve_id") or "")
        for fan_event in fantrax_events:
            tx_id = str(fan_event.get("tx_set_id") or "")
            if not tx_id or tx_id in used_tx_ids:
                continue
            if str(fan_event.get("team_id") or "") != str(app_event.get("team_id") or ""):
                continue
            fan_time = fan_event.get("date_utc")
            if not isinstance(app_time, datetime) or not isinstance(fan_time, datetime):
                continue
            delta = int(abs((fan_time - app_time).total_seconds()))
            if delta > window_seconds:
                continue
            if not _event_has_expected_swap(fan_event, active_id=active_id, reserve_id=reserve_id):
                continue
            if best_delta is None or delta < best_delta:
                best = fan_event
                best_delta = delta

        if best:
            tx_id = str(best.get("tx_set_id") or "")
            used_tx_ids.add(tx_id)
            matched_rows.append(
                {
                    **app_event,
                    "health_status": "matched",
                    "matched_tx_set_id": tx_id,
                    "fantrax_time_utc": best.get("date_utc"),
                    "fantrax_time_local": best.get("date_local"),
                    "fantrax_week_or_period": best.get("week_or_period"),
                    "delta_seconds": best_delta,
                }
            )
        else:
            matched_rows.append(
                {
                    **app_event,
                    "health_status": "unmatched",
                    "matched_tx_set_id": None,
                    "fantrax_time_utc": None,
                    "fantrax_time_local": None,
                    "fantrax_week_or_period": None,
                    "delta_seconds": None,
                }
            )
    return matched_rows


def compute_health_metrics(matches: List[dict]) -> dict:
    """
    Aggregate health metrics from matched rows.
    """
    total = len(matches)
    matched = sum(1 for row in matches if row.get("health_status") == "matched")
    unmatched = total - matched
    success_rate = round((matched / total) * 100, 2) if total else 0.0
    return {
        "total_fired": total,
        "matched": matched,
        "unmatched": unmatched,
        "success_rate": success_rate,
    }

