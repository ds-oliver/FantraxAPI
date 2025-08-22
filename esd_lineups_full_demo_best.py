#!/usr/bin/env python3
"""
Export SofaScore schedule + lineups via EasySoccerData (ESD).

Outputs (default out dir: data/sofascore):
  - schedules/{tournament_id}_{season_id}_{mode}.csv
  - lineups/{event_id}.json              (if --with-lineups is used)
  - lineups_index.csv                    (append/update index if --with-lineups)
  
Examples:
  # Premier League, current season, list already played matches ("last")
  python esd_export_schedule_and_lineups.py

  # Upcoming fixtures instead of played:
  python esd_export_schedule_and_lineups.py --upcoming

  # Fetch and store lineups too (slower):
  python esd_export_schedule_and_lineups.py --with-lineups

  # Specify season text or numeric season_id:
  python esd_export_schedule_and_lineups.py --season "2024/2025"
  python esd_export_schedule_and_lineups.py --season-id 60626

  # Different tournament (LaLiga=8), custom output dir:
  python esd_export_schedule_and_lineups.py --tournament-id 8 --output-dir mydump
"""

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import esd  # EasySoccerData


# --------------------------- helpers: season / events ---------------------------

def pick_season_id(client: "esd.SofascoreClient", tournament_id: int,
                   season_text: Optional[str], season_id: Optional[int]) -> int:
    """
    Resolve season_id:
      1) if --season-id provided, use it
      2) else match season_text to .year/.name
      3) else choose current season if available
      4) else newest (highest id)
    """
    seasons = client.get_tournament_seasons(tournament_id)  # list[Season]
    if not seasons:
        raise RuntimeError(f"No seasons returned for tournament {tournament_id}")

    if season_id is not None:
        return int(season_id)

    def _year(s):
        return getattr(s, "year", None) or getattr(s, "name", None) or ""

    def _is_current(s):
        return bool(getattr(s, "current", False))

    if season_text:
        target = season_text.strip().lower()
        # exact first
        for s in seasons:
            y = str(_year(s)).strip().lower()
            if y == target:
                return int(getattr(s, "id"))
        # partial fallback
        for s in seasons:
            y = str(_year(s)).strip().lower()
            if target in y:
                return int(getattr(s, "id"))
        raise RuntimeError(f"Season '{season_text}' not found among: {[ _year(x) for x in seasons ]}")

    current = [s for s in seasons if _is_current(s)]
    if current:
        return int(getattr(current[0], "id"))

    seasons_sorted = sorted(seasons, key=lambda s: int(getattr(s, "id")), reverse=True)
    return int(getattr(seasons_sorted[0], "id"))


def iter_tournament_events(client: "esd.SofascoreClient", tournament_id: int,
                           season_id: int, upcoming: bool) -> Iterable[object]:
    """
    Iterate all events for a tournament season (pages until empty).
    """
    page = 0
    seen = set()
    while True:
        batch = client.get_tournament_events(tournament_id, season_id, upcoming=upcoming, page=page)
        if not batch:
            break
        for ev in batch:
            ev_id = int(getattr(ev, "id"))
            if ev_id not in seen:
                seen.add(ev_id)
                yield ev
        page += 1


# --------------------------- helpers: safe accessors ---------------------------

def safe_team_name(team_obj) -> str:
    if not team_obj:
        return "?"
    return (
        getattr(team_obj, "name", None) or
        getattr(team_obj, "short_name", None) or
        getattr(team_obj, "slug", None) or
        "?"
    )

def safe_team_id(team_obj) -> Optional[int]:
    if not team_obj:
        return None
    tid = getattr(team_obj, "id", None)
    return int(tid) if tid is not None else None

def fmt_ts(ts) -> str:
    if ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")  # ISO-ish UTC
    except Exception:
        return ""

def event_to_row(ev, tournament_id: int, season_id: int) -> dict:
    start_ts = (
        getattr(ev, "start_timestamp", None) or
        getattr(ev, "startTimestamp", None) or
        getattr(ev, "startTime", None)
    )
    home = getattr(ev, "home_team", None) or getattr(ev, "home", None)
    away = getattr(ev, "away_team", None) or getattr(ev, "away", None)
    round_info = getattr(ev, "round_info", None) or getattr(ev, "roundInfo", None) or {}
    status = getattr(ev, "status", None) or {}

    return {
        "event_id": int(getattr(ev, "id")),
        "kickoff_utc": fmt_ts(start_ts),
        "home_team": safe_team_name(home),
        "away_team": safe_team_name(away),
        "home_team_id": safe_team_id(home) or "",
        "away_team_id": safe_team_id(away) or "",
        "tournament_id": tournament_id,
        "season_id": season_id,
        "round": getattr(round_info, "round", None) if hasattr(round_info, "round") else round_info.get("round", ""),
        "status_code": getattr(status, "code", None) if hasattr(status, "code") else status.get("code", ""),
    }


# --------------------------- helpers: lineups normalize ---------------------------

def lineup_to_json(event_id: int, lu) -> dict:
    """
    Normalize ESD Lineups -> a compact JSON you can reuse easily
    with starters, subs, and missing players.
    """
    def pack_player(pl):
        info = getattr(pl, "info", None) or {}
        # jersey/shirt number isn’t always present; keep minimal core
        return {
            "id": getattr(info, "id", None),
            "name": getattr(info, "name", None),
            "position": getattr(info, "position", None),
            "team_id": getattr(pl, "team_id", None),
            "substitute": bool(getattr(pl, "substitute", False)),
            "captain": bool(getattr(pl, "captain", False)),
        }

    def pack_missing(mp):
        p = getattr(mp, "player", None) or {}
        return {
            "id": getattr(p, "id", None),
            "name": getattr(p, "name", None),
            "position": getattr(p, "position", None),
            "reason": getattr(mp, "reason", None),
        }

    def pack_side(side):
        if not side:
            return None
        players = list(getattr(side, "players", []) or [])
        starters = [pack_player(p) for p in players if not getattr(p, "substitute", False)]
        subs     = [pack_player(p) for p in players if getattr(p, "substitute", False)]
        missing  = [pack_missing(m) for m in (getattr(side, "missing_players", []) or [])]
        return {
            "formation": getattr(side, "formation", None),
            "starters": starters,
            "subs": subs,
            "missing": missing,
        }

    return {
        "event_id": event_id,
        "confirmed": bool(getattr(lu, "confirmed", False)),
        "home": pack_side(getattr(lu, "home", None)),
        "away": pack_side(getattr(lu, "away", None)),
    }


# --------------------------- IO ---------------------------

def write_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        # ensure file exists with header
        with path.open("w", newline="", encoding="utf-8") as f:
            pass
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

def append_lineups_index(index_path: Path, items: list[dict]):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    exists = index_path.exists()
    fieldnames = ["event_id", "confirmed", "home_starters", "away_starters", "saved_at_utc"]
    with index_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        for it in items:
            w.writerow(it)


# --------------------------- main ---------------------------

def main():
    ap = argparse.ArgumentParser(description="Export SofaScore schedule + lineups using EasySoccerData.")
    ap.add_argument("--tournament-id", type=int, default=17, help="SofaScore tournament id (default: 17 = Premier League)")
    ap.add_argument("--season", type=str, default=None, help="Season text to match (e.g. '2024/2025' or '24/25')")
    ap.add_argument("--season-id", type=int, default=None, help="Explicit season_id (overrides --season)")
    ap.add_argument("--upcoming", action="store_true", help="List upcoming fixtures instead of 'last' (already played)")
    ap.add_argument("--with-lineups", action="store_true", help="Also fetch & save lineups JSON files")
    ap.add_argument("--output-dir", type=Path, default=Path("data/sofascore"), help="Base output directory")
    ap.add_argument("--browser-path", type=str, default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    help="Path to Chrome/Chromium if ESD needs it")
    args = ap.parse_args()

    # Init client
    client = esd.SofascoreClient(browser_path=args.browser_path)

    # Resolve season id
    season_id = pick_season_id(client, args.tournament_id, args.season, args.season_id)
    mode = "upcoming" if args.upcoming else "last"

    # Output paths
    schedules_dir = args.output_dir / "schedules"
    lineups_dir   = args.output_dir / "lineups"
    schedule_csv  = schedules_dir / f"{args.tournament_id}_{season_id}_{mode}.csv"
    lineups_index = args.output_dir / "lineups_index.csv"

    # Collect and save schedule
    rows = []
    events = list(iter_tournament_events(client, args.tournament_id, season_id, upcoming=args.upcoming))
    for ev in events:
        rows.append(event_to_row(ev, args.tournament_id, season_id))
    write_csv(rows, schedule_csv)
    print(f"[schedule] wrote {len(rows)} rows -> {schedule_csv}")

    if not args.with_lineups:
        return

    # Fetch lineups per event and save JSON
    saved = []
    for r in rows:
        event_id = int(r["event_id"])
        try:
            lu = client.get_match_lineups(event_id)
            if not lu:
                print(f"[lineups] {event_id}: no data")
                continue
            data = lineup_to_json(event_id, lu)
            lineups_dir.mkdir(parents=True, exist_ok=True)
            out_path = lineups_dir / f"{event_id}.json"
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            hs = len((data.get("home") or {}).get("starters") or [])
            as_ = len((data.get("away") or {}).get("starters") or [])
            saved.append({
                "event_id": event_id,
                "confirmed": bool(data.get("confirmed", False)),
                "home_starters": hs,
                "away_starters": as_,
                "saved_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z"),
            })
            print(f"[lineups] saved -> {out_path} (H:{hs} A:{as_} confirmed={data.get('confirmed')})")
        except Exception as e:
            print(f"[lineups] {event_id}: ERROR {e}")

    if saved:
        append_lineups_index(lineups_index, saved)
        print(f"[lineups] updated index -> {lineups_index}")


if __name__ == "__main__":
    main()
