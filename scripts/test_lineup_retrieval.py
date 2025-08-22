#!/usr/bin/env python
"""
Test script for retrieving lineups from SofaScore.
This version is robust to inconsistent 'substitute' flags and missing fields.
It will:
- Prefer 'formationPlace' to identify the XI,
- Fall back to 'substitute is False',
- Finally, fall back to top 11 by (position order, shirt number).
"""

import argparse
import asyncio
from datetime import datetime, timezone
import httpx
import json
import logging
from pathlib import Path
import sys

import time
import random

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    # Referer will be filled per event below to look like a real page visit
}

def build_headers_for_event(event_id: int) -> dict:
    h = DEFAULT_HEADERS.copy()
    # Any valid-looking match URL works; using id is the important bit
    h["Referer"] = f"https://www.sofascore.com/football/match/-/-#id:{event_id}"
    return h


logger = logging.getLogger(__name__)

# ------------------------- Logging -------------------------

def setup_logging(output_dir: Path):
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"lineup_test_{timestamp}.log"

    handlers = [logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]
    for h in handlers:
        if isinstance(h, logging.FileHandler):
            h.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        else:
            h.setFormatter(logging.Formatter('%(message)s'))

    logging.basicConfig(level=logging.INFO, handlers=handlers)
    logger.info("Starting lineup test")
    logger.info(f"Logging to {log_file}")
    return logger

# ------------------------- CLI -------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Test SofaScore lineup retrieval")
    p.add_argument("--date", type=str, help="Date (YYYY-MM-DD). Defaults to today (UTC).")
    p.add_argument("--output-dir", type=str, default="data/test_lineups",
                   help="Directory to save lineup data (default: data/test_lineups)")
    return p.parse_args()

# ------------------------- HTTP -------------------------

async def get_lineups(client: httpx.AsyncClient, event_id: int, max_tries: int = 4, min_sleep: float = 0.4) -> dict | None:
    """Get lineups for a specific event with browser headers and retries if truncated."""
    url = f"https://api.sofascore.com/api/v1/event/{event_id}/lineups"

    last = None
    for i in range(1, max_tries + 1):
        try:
            # cache-buster to avoid stale/partial edge responses
            params = {"_": int(time.time() * 1000)}
            headers = build_headers_for_event(event_id)

            r = await client.get(url, headers=headers, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            last = data

            home_players = len(((data.get("home") or {}).get("players") or []))
            away_players = len(((data.get("away") or {}).get("players") or []))

            # happy path: both sides have full XI
            if home_players >= 11 and away_players >= 11:
                return data

            # if truncated, backoff and try again
            # jitter helps when hitting different CDN edges
            sleep_for = min_sleep * i + random.uniform(0, 0.25)
            await asyncio.sleep(sleep_for)

        except Exception as e:
            logger.error(f"Error getting lineups for event {event_id} (try {i}/{max_tries}): {e}")
            # brief pause before retrying network/parse errors
            await asyncio.sleep(min_sleep * i)

    # return the last (possibly truncated) payload so the caller can log diagnostics
    return last

async def get_matches(client: httpx.AsyncClient, date: str) -> list:
    url = f"https://api.sofascore.com/api/v1/sport/football/scheduled-events/{date}"
    try:
        r = await client.get(url, headers=DEFAULT_HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        pl_matches = []
        target_date = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        for event in data.get("events", []):
            ut = (event.get("tournament", {}) or {}).get("uniqueTournament", {}) or {}
            if ut.get("id") != 17:  # Premier League
                continue
            ts = event.get("startTimestamp")
            if not ts:
                continue
            event_date = datetime.fromtimestamp(ts, tz=timezone.utc)
            if event_date.date() == target_date.date():
                pl_matches.append(event)
        return pl_matches
    except Exception as e:
        logger.error(f"Error getting matches: {e}")
        return []

# ------------------------- Helpers -------------------------

POSITION_ORDER = {"G": 0, "D": 1, "M": 2, "F": 3}  # fallback sort

def _position_letter(p: dict) -> str:
    """Prefer top-level position (match role) then nested player.position, else '?'."""
    pos = p.get("position")
    if not pos:
        pos = (p.get("player") or {}).get("position")
    return pos or "?"

def _shirt_num(p: dict):
    """Return an int-ish shirt number for sorting; try shirtNumber then jerseyNumber, else 999."""
    sn = p.get("shirtNumber", None)
    if sn is None:
        sn = p.get("jerseyNumber", None)
    # Normalize to int if possible
    try:
        return int(sn)
    except Exception:
        return 999

def _split_players(team_data: dict):
    players = team_data.get("players", []) or []
    starters_by_place = [p for p in players if p.get("formationPlace")]
    starters_by_flag  = [p for p in players if p.get("substitute") is False]
    subs_by_flag      = [p for p in players if p.get("substitute") is True]
    return players, starters_by_place, starters_by_flag, subs_by_flag

def _select_xi(players: list, starters_by_place: list, starters_by_flag: list) -> list:
    """Pick 11 starters robustly with sensible fallbacks."""
    # 1) formationPlace is the most reliable when present
    if len(starters_by_place) >= 11:
        return sorted(starters_by_place, key=lambda p: p.get("formationPlace"))[:11]

    # 2) Otherwise, use explicit substitute flag
    if len(starters_by_flag) >= 11:
        # Sort them into GK, D, M, F and within that by shirt number
        return sorted(
            starters_by_flag,
            key=lambda p: (POSITION_ORDER.get(_position_letter(p), 99), _shirt_num(p))
        )[:11]

    # 3) Fallback: take first 11 non-subs (including missing flag) by position then number
    non_subs = [p for p in players if not p.get("substitute", False)]
    if len(non_subs) >= 11:
        return sorted(
            non_subs,
            key=lambda p: (POSITION_ORDER.get(_position_letter(p), 99), _shirt_num(p))
        )[:11]

    # 4) Last resort: just take the first 11 players by sensible order
    return sorted(
        players,
        key=lambda p: (p.get("substitute") is True, POSITION_ORDER.get(_position_letter(p), 99), _shirt_num(p))
    )[:11]

def _print_players(hdr: str, plist: list):
    if not plist:
        logger.info(f"\n{hdr}: (none)")
        return
    logger.info(f"\n{hdr}:")
    for p in plist:
        info = (p.get("player") or {})
        name = info.get("name") or info.get("shortName") or "Unknown"
        pos  = _position_letter(p)
        num  = p.get("shirtNumber", p.get("jerseyNumber", "?"))
        logger.info(f"- [{num}] {name} ({pos})")

def format_lineup_info(lineup_data: dict, side: str, team_name: str) -> None:
    team_data = lineup_data.get(side, {}) or {}
    formation = team_data.get("formation")
    players, starters_by_place, starters_by_flag, subs_by_flag = _split_players(team_data)

    # Diagnostics to explain what we’re doing
    logger.info(f"\n{team_name} ({'Home' if side=='home' else 'Away'}):")
    logger.info(f"\nFormation: {formation}")
    logger.info(
        f"\nDiagnostics — total players: {len(players)}, "
        f"with formationPlace: {len(starters_by_place)}, "
        f"starters by flag: {len(starters_by_flag)}, "
        f"subs by flag: {len(subs_by_flag)}"
    )

    starters = _select_xi(players, starters_by_place, starters_by_flag)

    # Derive bench as "players not in starters but marked sub (or not picked)"
    starter_ids = { (p.get('player') or {}).get('id') for p in starters }
    bench = [p for p in players if (p.get("player") or {}).get("id") not in starter_ids and p.get("substitute") is True]

    if len(starters) != 11:
        logger.warning(
            f"⚠️ Starters resolved to {len(starters)} (expected 11). "
            "This can happen when SofaScore misflags 'substitute' or omits 'formationPlace' in early/preview states. "
            "Applied fallback selection."
        )

    _print_players("Starting XI", starters)
    _print_players("Substitutes", bench)

    missing = team_data.get("missingPlayers", []) or []
    if missing:
        logger.info("\nMissing Players:")
        for m in missing:
            info = (m.get("player") or {})
            nm   = info.get("name") or info.get("shortName") or "Unknown"
            reason = m.get("type") or "unknown"
            logger.info(f"- {nm} ({'Doubtful' if reason=='doubtful' else 'Out'})")

# ------------------------- Main -------------------------

async def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(output_dir)

    try:
        target_date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info(f"Looking for matches on {target_date}")

        async with httpx.AsyncClient() as client:
            matches = await get_matches(client, target_date)
            if not matches:
                logger.info("No Premier League matches found for this date")
                return

            logger.info(f"\nFound {len(matches)} Premier League matches:")
            for m in matches:
                ko = datetime.fromtimestamp(m["startTimestamp"], tz=timezone.utc)
                logger.info(f"- {ko.strftime('%H:%M')} {m['homeTeam']['name']} vs {m['awayTeam']['name']} (ID: {m['id']})")

            for m in matches:
                match_id = m["id"]
                ko = datetime.fromtimestamp(m["startTimestamp"], tz=timezone.utc)
                logger.info("\n" + "="*80)
                logger.info(f"Checking lineups for {m['homeTeam']['name']} vs {m['awayTeam']['name']} ({ko.strftime('%H:%M')})")

                lineup_data = await get_lineups(client, match_id)
                if not lineup_data:
                    logger.info("No lineup data available yet")
                    continue

                out_file = output_dir / f"lineups_match_{match_id}.json"
                with open(out_file, "w") as f:
                    json.dump(lineup_data, f, indent=2)
                logger.info(f"Saved raw lineup data to {out_file}")

                status = "confirmed" if lineup_data.get("confirmed") else "preliminary"
                logger.info(f"\nLineup Status: {status}")

                format_lineup_info(lineup_data, "home", m["homeTeam"]["name"])
                format_lineup_info(lineup_data, "away", m["awayTeam"]["name"])

        logger.info("\nLineup retrieval test completed.")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
