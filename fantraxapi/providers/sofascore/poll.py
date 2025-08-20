"""
Async polling for SofaScore lineups.
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from .discover import API_BASE, get_json
from .models import Event

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
    event: Event,
    poll_interval: int = 60,
    max_minutes: int = 120
) -> Optional[Dict]:
    """
    Poll event lineups until confirmed or timeout.
    
    Args:
        client: httpx AsyncClient instance
        event: Event metadata
        poll_interval: Seconds between polls
        max_minutes: Maximum minutes to poll
    
    Returns:
        Dict with lineup data if found, None if timeout
    """
    url = f"{API_BASE}/event/{event.event_id}/lineups"
    
    print(
        f"\nStarting to poll: {event.kickoff_utc.strftime('%H:%M')} - "
        f"{event.home_team['name']} vs {event.away_team['name']}"
    )
    
    for minute in range(max_minutes):
        try:
            data = await get_json(client, url)
            if xi_is_confirmed(data):
                print(f"✓ Found confirmed lineups!")
                # Add metadata to lineup data
                data.update({
                    "event_id": event.event_id,
                    "tournament_id": event.tournament_id,
                    "tournament_name": event.tournament_name,
                    "kickoff_utc": event.kickoff_utc,
                    "home_team": event.home_team,
                    "away_team": event.away_team,
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

async def poll_events(
    events: List[Event],
    poll_interval: int = 60,
    max_minutes: int = 120
) -> List[Dict]:
    """
    Poll multiple events concurrently.
    
    Args:
        events: List of events to poll
        poll_interval: Seconds between polls
        max_minutes: Maximum minutes to poll per event
    
    Returns:
        List of events with lineup data
    """
    if not events:
        return []
    
    print(f"\nPolling {len(events)} events:")
    for e in events:
        print(
            f"- {e.kickoff_utc.strftime('%H:%M')} "
            f"{e.home_team['name']} vs {e.away_team['name']}"
        )
    
    async with httpx.AsyncClient() as client:
        tasks = [
            poll_event(
                client, e,
                poll_interval=poll_interval,
                max_minutes=max_minutes
            ) 
            for e in events
        ]
        results = await asyncio.gather(*tasks)
    
    # Filter out None results (timeouts)
    return [r for r in results if r is not None]
