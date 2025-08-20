"""
Async watcher for SofaScore lineups.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
import os

import httpx
import pandas as pd
from tenacity import retry, wait_exponential, stop_after_attempt

# API configuration
API_BASE = "https://api.sofascore.com/api/v1"
USER_AGENT = "Mozilla/5.0 (analytics/lineups)"

# Default tournament IDs
PREMIER_LEAGUE_ID = 17

@retry(wait=wait_exponential(multiplier=0.5, min=0.5, max=8), stop=stop_after_attempt(5))
async def get_json(client: httpx.AsyncClient, url: str) -> dict:
    """Make GET request and return JSON response."""
    r = await client.get(
        url, 
        headers={"User-Agent": USER_AGENT},
        timeout=20
    )
    r.raise_for_status()
    return r.json()

def utc_ts_to_dt(ts: int) -> datetime:
    """Convert SofaScore timestamp to UTC datetime."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)

def is_within_window(kickoff_utc: datetime, now_utc: datetime, window_minutes: int) -> bool:
    """Check if event is within polling window."""
    return timedelta(minutes=-window_minutes) <= (now_utc - kickoff_utc) <= timedelta(minutes=75)

async def get_scheduled_events(
    client: httpx.AsyncClient,
    tournament_ids: List[int],
    date_utc: Optional[datetime] = None
) -> List[Dict]:
    """
    Get scheduled events for given date.
    
    Args:
        client: httpx AsyncClient instance
        tournament_ids: List of SofaScore tournament IDs to monitor
        date_utc: Target date (defaults to today)
    
    Returns:
        List of event dictionaries
    """
    date_utc = date_utc or datetime.now(tz=timezone.utc)
    date_str = date_utc.strftime("%Y-%m-%d")
    
    # Get events from SofaScore API
    url = f"{API_BASE}/sport/football/scheduled-events/{date_str}"
    data = await get_json(client, url)
    events = data.get("events", [])
    
    # Filter for our target tournaments
    events = [
        e for e in events 
        if e.get("tournament", {}).get("uniqueTournament", {}).get("id") in tournament_ids
    ]
    
    return events

def xi_is_confirmed(lineup_data: dict) -> bool:
    """
    Check if lineup is confirmed (11 non-subs per side).
    
    Args:
        lineup_data: Raw lineup response from SofaScore API
    
    Returns:
        bool: True if both teams have 11 confirmed starters
    """
    for side in ["home", "away"]:
        side_obj = lineup_data.get(side, {}) or {}
        players = side_obj.get("players", []) or []
        starters = sum(1 for p in players if not p.get("substitute", False))
        if starters != 11:
            return False
    return True

async def poll_event(
    client: httpx.AsyncClient,
    event: Dict,
    poll_interval: int = 60,
    max_minutes: int = 120
) -> Optional[Dict]:
    """
    Poll event lineups until confirmed or timeout.
    
    Args:
        client: httpx AsyncClient instance
        event: Event metadata from schedule
        poll_interval: Seconds between polls
        max_minutes: Maximum minutes to poll
    
    Returns:
        Dict with lineup data if found, None if timeout
    """
    event_id = event["id"]
    url = f"{API_BASE}/event/{event_id}/lineups"
    
    ko = utc_ts_to_dt(event["startTimestamp"])
    print(f"\nStarting to poll: {ko.strftime('%H:%M')} - {event['homeTeam']['name']} vs {event['awayTeam']['name']}")
    
    for minute in range(max_minutes):
        try:
            data = await get_json(client, url)
            if xi_is_confirmed(data):
                print(f"✓ Found confirmed lineups!")
                # Add metadata to lineup data
                data.update({
                    "event_id": event_id,
                    "league_name": event.get("league_name", "Premier League"),
                    "league_country": event.get("league_country", "England"),
                    "kickoff_utc": ko,
                    "home_team": event["homeTeam"],
                    "away_team": event["awayTeam"],
                    "captured_at_utc": datetime.now(timezone.utc)
                })
                return data
            
            if minute > 0 and minute % 5 == 0:
                print(f"Still polling ({minute}m)...")
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 429, 500, 503):
                # Not ready or rate-limited; keep trying
                if minute > 0 and minute % 5 == 0:
                    print(f"Waiting for data ({minute}m)...")
        
        await asyncio.sleep(poll_interval)
    
    print("× Timeout polling event")
    return None

async def watch_lineups(
    tournament_ids: Optional[List[int]] = None,
    window_minutes: int = 90,
    poll_interval: int = 60
) -> List[Dict]:
    """
    Watch for lineups from SofaScore.
    
    Args:
        tournament_ids: List of tournament IDs to monitor (defaults to Premier League)
        window_minutes: Minutes before kickoff to start polling
        poll_interval: Seconds between polls
    
    Returns:
        List of events with lineup data
    """
    tournament_ids = tournament_ids or [PREMIER_LEAGUE_ID]
    now_utc = datetime.now(tz=timezone.utc)
    
    async with httpx.AsyncClient() as client:
        # Get today's events
        events = await get_scheduled_events(client, tournament_ids)
        
        # Filter for events within polling window or upcoming in next 6 hours
        watch = []
        for e in events:
            ko = utc_ts_to_dt(e["startTimestamp"])
            if is_within_window(ko, now_utc, window_minutes) or (
                ko > now_utc and (ko - now_utc) <= timedelta(hours=6)
            ):
                watch.append(e)
        
        if not watch:
            print("No events to watch")
            return []
        
        print(f"\nWatching {len(watch)} events:")
        for e in watch:
            ko = utc_ts_to_dt(e["startTimestamp"])
            print(f"- {ko.strftime('%H:%M')} {e['homeTeam']['name']} vs {e['awayTeam']['name']}")
        
        # Poll all events
        tasks = [
            poll_event(
                client, e,
                poll_interval=poll_interval,
                max_minutes=120
            ) 
            for e in watch
        ]
        results = await asyncio.gather(*tasks)
    
    # Filter out None results (timeouts)
    return [r for r in results if r is not None]
