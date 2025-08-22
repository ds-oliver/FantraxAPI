"""
Async watcher for SofaScore lineups.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Dict, List, Optional

from ..providers.sofascore.client import get_matches, get_match_lineups

logger = logging.getLogger(__name__)

async def watch_lineups(
    window_minutes: int = 90,
    poll_interval: int = 60,
    max_minutes: int = 120
) -> List[Dict]:
    """
    Watch for Premier League lineups from SofaScore.
    
    Args:
        window_minutes: Minutes before kickoff to start polling
        poll_interval: Seconds between polls
        max_minutes: Maximum minutes to poll per match
        
    Returns:
        List of events with lineup data
    """
    now = datetime.now(timezone.utc)
    
    # Get today's matches
    matches = await get_matches()
    if not matches:
        logger.info("No Premier League matches today")
        return []
        
    # Filter for matches within polling window or upcoming in next 6 hours
    watch = []
    for match in matches:
        ko = datetime.fromtimestamp(match["startTimestamp"], tz=timezone.utc)
        time_to_ko = ko - now
        
        if (
            timedelta(minutes=-window_minutes) <= time_to_ko <= timedelta(minutes=75) or
            (time_to_ko > timedelta(0) and time_to_ko <= timedelta(hours=6))
        ):
            watch.append(match)
            
    if not watch:
        logger.info("No matches within polling window")
        return []
        
    logger.info(f"\nWatching {len(watch)} matches:")
    for match in watch:
        ko = datetime.fromtimestamp(match["startTimestamp"], tz=timezone.utc)
        logger.info(
            f"- {ko.strftime('%H:%M')} {match['homeTeam']['name']} vs "
            f"{match['awayTeam']['name']}"
        )
        
    # Poll each match for lineups
    results = []
    for match in watch:
        match_id = match["id"]
        ko = datetime.fromtimestamp(match["startTimestamp"], tz=timezone.utc)
        
        logger.info(
            f"\nPolling lineups for {match['homeTeam']['name']} vs "
            f"{match['awayTeam']['name']} ({ko.strftime('%H:%M')})"
        )
        
        for minute in range(max_minutes):
            lineup_data = await get_match_lineups(match_id)
            
            if lineup_data:
                if lineup_data["is_confirmed"]:
                    logger.info("✓ Found confirmed lineups!")
                    results.append(lineup_data)
                    break
                else:
                    logger.info("Found preliminary lineups")
                    results.append(lineup_data)
                    
            if minute > 0 and minute % 5 == 0:
                logger.info(f"Still polling ({minute}m)...")
                
            await asyncio.sleep(poll_interval)
            
        else:
            logger.warning(f"× Timeout polling match {match_id}")
            
    return results