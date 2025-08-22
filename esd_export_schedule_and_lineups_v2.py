#!/usr/bin/env python3
"""
Export SofaScore schedule + lineups via EasySoccerData (ESD), with optional raw-HTTP fallbacks.

Outputs (default out dir: data/sofascore):
  - schedules/{tournament_id}_{season_id}_{mode}.csv
  - lineups/{event_id}.json              (if --with-lineups is used)
  - lineups_index.csv

Examples:
  python esd_export_schedule_and_lineups.py --tournament-id 17 --output-dir data/sofascore
  python esd_export_schedule_and_lineups.py --season "2023/2024" --finished-only --with-lineups --limit 5 --disable-raw
"""

import argparse
import csv
import json
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Any

import httpx
import esd  # EasySoccerData

API_BASE = "https://api.sofascore.com/api/v1"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

# --------------------------- tiny dict/attr access helpers ---------------------------

def getv(obj: Any, *names: str, default=None):
    """Return first matching attribute or dict key."""
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default

def is_mapping(x) -> bool:
    return isinstance(x, dict)

# --------------------------- raw HTTP fallbacks ---------------------------

def raw_get_json(url: str, referer: Optional[str] = None) -> dict:
    headers = HEADERS.copy()
    if referer:
        headers["Referer"] = referer
    with httpx.Client(follow_redirects=True, headers=headers, timeout=30) as s:
        # small jitter to be polite
        time.sleep(0.3 + random.random() * 0.3)
        r = s.get(url, params={"_": int(datetime.now().timestamp() * 1000)})
        r.raise_for_status()
        return r.json()

def raw_get_seasons(tournament_id: int) -> list[dict]:
    url = f"{API_BASE}/unique-tournament/{tournament_id}/seasons"
    data = raw_get_json(url)
    return data.get("seasons") or []

def raw_iter_tournament_events(tournament_id: int, season_id: int, upcoming: bool) -> Iterable[dict]:
    page = 0
    path = "next" if upcoming else "last"
    while True:
        url = f"{API_BASE}/unique-tournament/{tournament_id}/season/{season_id}/events/{path}/{page}"
        data = raw_get_json(url)
        events = data.get("events") or []
        if not events:
            break
        for ev in events:
            yield ev
        page += 1

def raw_get_lineups(event_id: int) -> dict:
    url = f"{API_BASE}/event/{event_id}/lineups"
    referer = f"https://www.sofascore.com/football/match/-/-/#id:{event_id}"
    return raw_get_json(url, referer=referer)

# --------------------------- season resolution ---------------------------

def pick_season_id(client: "esd.SofascoreClient", tournament_id: int,
                   season_text: Optional[str], season_id: Optional[int],
                   disable_raw: bool = False) -> int:
    """
    Resolve season_id using ESD first; on failure, use raw HTTP (unless disabled).
    """
    if season_id is not None:
        print(f"[season] using explicit season_id={season_id}")
        return int(season_id)

    # 1) Try ESD
    seasons_esd = []
    try:
        seasons_esd = client.get_tournament_seasons(tournament_id)  # list[Season]
    except Exception as e:
        print(f"[season] ESD get_tournament_seasons failed: {e}")

    if seasons_esd:
        print(f"[season] ESD provided {len(seasons_esd)} seasons")
        sid = choose_season_from_list(seasons_esd, season_text)
        print(f"[season] picked season_id={sid} (from ESD)")
        return sid

    if disable_raw:
        raise RuntimeError("ESD could not load seasons and --disable-raw is set.")

    # 2) Fallback: raw HTTP
    seasons_raw = raw_get_seasons(tournament_id)
    if not seasons_raw:
        raise RuntimeError(f"No seasons available for tournament {tournament_id} (both ESD and raw).")

    print(f"[season] RAW provided {len(seasons_raw)} seasons")
    sid = choose_season_from_list(seasons_raw, season_text)
    print(f"[season] picked season_id={sid} (from RAW)")
    return sid

def season_label(s) -> str:
    # SofaScore seasons typically have year like "2023/2024"
    return str(getv(s, "year", "name", default=""))

def season_is_current(s) -> bool:
    return bool(getv(s, "current", default=False))

def season_id_of(s) -> int:
    return int(getv(s, "id"))

def choose_season_from_list(seasons: list[Any], season_text: Optional[str]) -> int:
    if season_text:
        target = season_text.strip().lower()
        # exact match on label
        for s in seasons:
            if season_label(s).strip().lower() == target:
                return season_id_of(s)
        # partial match
        for s in seasons:
            if target in season_label(s).strip().lower():
                return season_id_of(s)
        raise RuntimeError(f"Season '{season_text}' not found. Options: {[season_label(x) for x in seasons]}")

    # prefer 'current'
    current = [s for s in seasons if season_is_current(s)]
    if current:
        return season_id_of(current[0])

    # else highest id
    seasons_sorted = sorted(seasons, key=lambda x: season_id_of(x), reverse=True)
    return season_id_of(seasons_sorted[0])

# --------------------------- events (ESD with optional raw fallback) ---------------------------

def iter_tournament_events(client: "esd.SofascoreClient",
                           tournament_id: int,
                           season_id: int,
                           upcoming: bool,
                           disable_raw: bool) -> Iterable[Any]:
    # Try ESD first
    page = 0
    seen = set()
    try:
        total = 0
        while True:
            batch = client.get_tournament_events(tournament_id, season_id, upcoming=upcoming, page=page)
            if not batch:
                break
            for ev in batch:
                ev_id = int(getv(ev, "id"))
                if ev_id not in seen:
                    seen.add(ev_id)
                    total += 1
                    yield ev
            page += 1
        print(f"[events] ESD returned {total} events for season_id={season_id} (mode={'upcoming' if upcoming else 'last'})")
        return
    except Exception as e:
        print(f"[events] ESD get_tournament_events failed: {e}")

    if disable_raw:
        print("[events] RAW fallback disabled; returning nothing.")
        return

    # Fallback: raw HTTP
    total = 0
    for ev in raw_iter_tournament_events(tournament_id, season_id, upcoming):
        total += 1
        yield ev
    print(f"[events] RAW returned {total} events for season_id={season_id} (mode={'upcoming' if upcoming else 'last'})")

# --------------------------- schedule helpers ---------------------------

def safe_team_name(team_obj) -> str:
    if not team_obj:
        return "?"
    if is_mapping(team_obj):
        return team_obj.get("name") or team_obj.get("shortName") or team_obj.get("short_name") or team_obj.get("slug") or "?"
    return (
        getattr(team_obj, "name", None) or
        getattr(team_obj, "short_name", None) or
        getattr(team_obj, "slug", None) or
        "?"
    )

def safe_team_id(team_obj) -> Optional[int]:
    if not team_obj:
        return None
    if is_mapping(team_obj):
        tid = team_obj.get("id")
    else:
        tid = getattr(team_obj, "id", None)
    return int(tid) if tid is not None else None

def fmt_ts(ts) -> str:
    if ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")
    except Exception:
        return ""

def event_to_row(ev, tournament_id: int, season_id: int) -> dict:
    start_ts = getv(ev, "startTimestamp", "start_timestamp", "startTime")
    home = getv(ev, "homeTeam", "home_team", "home")
    away = getv(ev, "awayTeam", "away_team", "away")
    round_info = getv(ev, "roundInfo", "round_info", default={}) or {}
    status = getv(ev, "status", default={}) or {}

    round_val = getv(round_info, "round", default="")
    status_code = getv(status, "code", default="")

    return {
        "event_id": int(getv(ev, "id")),
        "kickoff_utc": fmt_ts(start_ts),
        "home_team": safe_team_name(home),
        "away_team": safe_team_name(away),
        "home_team_id": safe_team_id(home) or "",
        "away_team_id": safe_team_id(away) or "",
        "tournament_id": tournament_id,
        "season_id": season_id,
        "round": round_val,
        "status_code": status_code,
    }

def event_is_finished(ev) -> bool:
    # SofaScore uses 100 for 'finished'
    status = getv(ev, "status", default={}) or {}
    return int(getv(status, "code", default=0)) == 100

# --------------------------- lineups normalize (ESD or raw) ---------------------------

def lineup_to_json(event_id: int, lu: Any) -> dict:
    """Normalize ESD Lineups OR raw dict into a compact, uniform JSON."""
    def pack_from_esd_player(pl):
        info = getattr(pl, "info", None) or {}
        return {
            "id": getattr(info, "id", None),
            "name": getattr(info, "name", None),
            "position": getattr(info, "position", None),
            "team_id": getattr(pl, "team_id", None),
            "substitute": bool(getattr(pl, "substitute", False)),
            "captain": bool(getattr(pl, "captain", False)),
        }

    def pack_from_raw_player(d):
        info = d.get("player", {}) if isinstance(d, dict) else {}
        return {
            "id": info.get("id"),
            "name": info.get("name"),
            "position": info.get("position"),
            "team_id": d.get("teamId"),
            "substitute": bool(d.get("substitute", False)),
            "captain": bool(d.get("captain", False)),
        }

    def pack_from_esd_missing(mp):
        p = getattr(mp, "player", None) or {}
        return {"id": getattr(p, "id", None), "name": getattr(p, "name", None), "position": getattr(p, "position", None), "reason": getattr(mp, "reason", None)}

    def pack_from_raw_missing(d):
        p = d.get("player", {}) if isinstance(d, dict) else {}
        return {"id": p.get("id"), "name": p.get("name"), "position": p.get("position"), "reason": d.get("reason")}

    def pack_side_esd(side):
        players = list(getattr(side, "players", []) or [])
        starters = [pack_from_esd_player(p) for p in players if not getattr(p, "substitute", False)]
        subs     = [pack_from_esd_player(p) for p in players if getattr(p, "substitute", False)]
        missing  = [pack_from_esd_missing(m) for m in (getattr(side, "missing_players", []) or [])]
        return {"formation": getattr(side, "formation", None), "starters": starters, "subs": subs, "missing": missing}

    def pack_side_raw(side: dict):
        players = side.get("players") or []
        starters = [pack_from_raw_player(p) for p in players if not p.get("substitute", False)]
        subs     = [pack_from_raw_player(p) for p in players if p.get("substitute", False)]
        missing  = [pack_from_raw_missing(m) for m in (side.get("missingPlayers") or [])]
        return {"formation": side.get("formation"), "starters": starters, "subs": subs, "missing": missing}

    # Choose ESD vs raw
    if isinstance(lu, dict):
        home = lu.get("home") or {}
        away = lu.get("away") or {}
        return {
            "event_id": event_id,
            "confirmed": bool(lu.get("confirmed", False)),
            "home": pack_side_raw(home),
            "away": pack_side_raw(away),
        }
    else:
        return {
            "event_id": event_id,
            "confirmed": bool(getv(lu, "confirmed", default=False)),
            "home": pack_side_esd(getv(lu, "home")),
            "away": pack_side_esd(getv(lu, "away")),
        }

# --------------------------- IO ---------------------------

def write_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["event_id","kickoff_utc","home_team","away_team","home_team_id","away_team_id","tournament_id","season_id","round","status_code"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

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
    ap = argparse.ArgumentParser(description="Export SofaScore schedule + lineups using EasySoccerData with optional raw fallbacks.")
    ap.add_argument("--tournament-id", type=int, default=17, help="SofaScore tournament id (default: 17 = Premier League)")
    ap.add_argument("--season", type=str, default=None, help="Season label to match (e.g. '2023/2024' or '24/25')")
    ap.add_argument("--season-id", type=int, default=None, help="Explicit season_id (overrides --season)")
    ap.add_argument("--upcoming", action="store_true", help="List upcoming fixtures instead of 'last' (already played)")
    ap.add_argument("--finished-only", action="store_true", help="Keep only finished events (status.code==100)")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N events (for quick testing)")
    ap.add_argument("--with-lineups", action="store_true", help="Also fetch & save lineups JSON files")
    ap.add_argument("--output-dir", type=Path, default=Path("data/sofascore"), help="Base output directory")
    ap.add_argument("--browser-path", type=str, default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    help="Path to Chrome/Chromium if ESD needs it")
    ap.add_argument("--disable-raw", action="store_true", help="Use EasySoccerData only; do not hit raw HTTP fallbacks")
    args = ap.parse_args()

    # Init client
    client = esd.SofascoreClient(browser_path=args.browser_path)

    # Resolve season id (ESD -> optional raw fallback)
    season_id = pick_season_id(client, args.tournament_id, args.season, args.season_id, disable_raw=args.disable_raw)
    mode = "upcoming" if args.upcoming else "last"

    # Output paths
    schedules_dir = args.output_dir / "schedules"
    lineups_dir   = args.output_dir / "lineups"
    schedule_csv  = schedules_dir / f"{args.tournament_id}_{season_id}_{mode}.csv"
    lineups_index = args.output_dir / "lineups_index.csv"

    # Collect events
    rows = []
    kept = 0
    for ev in iter_tournament_events(client, args.tournament_id, season_id, upcoming=args.upcoming, disable_raw=args.disable_raw):
        if args.finished_only and not event_is_finished(ev):
            continue
        rows.append(event_to_row(ev, args.tournament_id, season_id))
        kept += 1
        if args.limit and kept >= args.limit:
            break

    # Save schedule
    write_csv(rows, schedule_csv)
    print(f"[schedule] wrote {len(rows)} rows -> {schedule_csv}")

    if not args.with_lineups or not rows:
        return

    # Fetch lineups per event (ESD first, then raw if enabled)
    saved = []
    for r in rows:
        event_id = int(r["event_id"])
        try:
            try:
                lu = client.get_match_lineups(event_id)
            except Exception:
                if args.disable_raw:
                    raise
                lu = raw_get_lineups(event_id)  # raw fallback

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
