"""
Discover scheduled events from SofaScore.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional, Tuple

import httpx
from tenacity import retry, wait_exponential, stop_after_attempt

logger = logging.getLogger(__name__)

# API configuration
API_BASE = "https://api.sofascore.com/api/v1"
USER_AGENT = "Mozilla/5.0 (analytics/lineups)"

# Premier League configuration
PREMIER_LEAGUE_ID = 17
PREMIER_LEAGUE_NAME = "Premier League"

class Event:
    """SofaScore event."""
    def __init__(self, **kwargs):
        self.event_id = kwargs.get("id")
        self.tournament_id = kwargs.get("tournament", {}).get("id")
        self.tournament_name = kwargs.get("tournament", {}).get("name")
        self.kickoff_utc = kwargs.get("startTimestamp")
        self.home_team = kwargs.get("homeTeam", {})
        self.away_team = kwargs.get("awayTeam", {})

def get_season_dates(season: str) -> Tuple[datetime, datetime]:
    """
    Get start and end dates for a season.
    
    Args:
        season: Season in format "2025-26"
    
    Returns:
        Tuple of (start_date, end_date)
    """
    start_year = int(season.split("-")[0])
    # Premier League typically starts early August and ends mid-May
    return (
        datetime(start_year, 8, 1, tzinfo=timezone.utc),  # August 1st
        datetime(start_year + 1, 5, 31, tzinfo=timezone.utc)  # May 31st
    )

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

def is_premier_league_game(event: Dict) -> bool:
    """
    Check if event is a Premier League game.
    
    Args:
        event: Event data from SofaScore API
    
    Returns:
        bool: True if Premier League game
    """
    tournament = event.get("tournament", {})
    unique_tournament = tournament.get("uniqueTournament", {})
    
    # Validate it's a Premier League game
    if not (unique_tournament.get("id") == PREMIER_LEAGUE_ID and
            tournament.get("name") == PREMIER_LEAGUE_NAME):
        return False
        
    # Validate both teams exist
    if not (event.get("homeTeam", {}).get("name") and 
            event.get("awayTeam", {}).get("name")):
        return False
        
    return True

def get_event_key(event: Dict) -> str:
    """
    Get unique key for event to deduplicate.
    
    Args:
        event: Event data from SofaScore API
    
    Returns:
        str: Unique key combining home team and away team
    """
    return f"{event['homeTeam']['id']}_{event['awayTeam']['id']}"

def deduplicate_events(events: List[Event]) -> List[Event]:
    """
    Deduplicate events that appear on consecutive days.
    
    When the same match appears on consecutive days, we:
    1. Group by teams playing
    2. For each group, keep only the most likely date
    3. Most likely = the date that has the most other games
    
    Args:
        events: List of events to deduplicate
    
    Returns:
        Deduplicated list of events
    """
    if not events:
        return []
        
    # Group events by date
    events_by_date = {}
    for event in events:
        date = event.kickoff_utc.date()
        if date not in events_by_date:
            events_by_date[date] = []
        events_by_date[date].append(event)
    
    # Find the dates with the most games
    # These are most likely to be the actual matchdays
    dates_by_game_count = sorted(
        events_by_date.keys(),
        key=lambda d: len(events_by_date[d]),
        reverse=True
    )
    
    # Group events by teams playing
    events_by_teams = {}
    for event in events:
        key = f"{event.home_team['id']}_{event.away_team['id']}"
        if key not in events_by_teams:
            events_by_teams[key] = []
        events_by_teams[key].append(event)
    
    # For each set of teams, keep the event on the date with the most games
    result = []
    for team_events in events_by_teams.values():
        if len(team_events) == 1:
            # Only one date, keep it
            result.append(team_events[0])
        else:
            # Multiple dates, find the one with the most games
            best_event = max(
                team_events,
                key=lambda e: (
                    len(events_by_date[e.kickoff_utc.date()]),  # Prefer dates with more games
                    -abs((e.kickoff_utc.hour - 15))  # Prefer times closer to 3pm
                )
            )
            result.append(best_event)
    
    # Sort by kickoff time
    result.sort(key=lambda e: e.kickoff_utc)
    return result

async def get_scheduled_events(
    client: httpx.AsyncClient,
    date_utc: Optional[datetime] = None,
    verbose: bool = False
) -> List[Event]:
    """
    Get scheduled Premier League events for given date.
    
    Args:
        client: httpx AsyncClient instance
        date_utc: Target date (defaults to today)
        verbose: Print detailed logging
    
    Returns:
        List of Event instances
    """
    date_utc = date_utc or datetime.now(tz=timezone.utc)
    date_str = date_utc.strftime("%Y-%m-%d")
    
    # Get events from SofaScore API
    url = f"{API_BASE}/sport/football/scheduled-events/{date_str}"
    data = await get_json(client, url)
    events = data.get("events", [])
    
    # Filter for Premier League games and deduplicate
    seen_keys = set()
    result = []
    for e in events:
        if is_premier_league_game(e):
            # Use event key to deduplicate
            key = get_event_key(e)
            if key not in seen_keys:
                seen_keys.add(key)
                
                # Convert timestamp to datetime
                e["startTimestamp"] = utc_ts_to_dt(e["startTimestamp"])
                
                result.append(Event(**e))
    
    if verbose:
        if result:
            logger.info(f"\nFound {len(result)} Premier League games on {date_str}:")
            for e in result:
                logger.info(f"  • {e.kickoff_utc.strftime('%H:%M')} {e.home_team['name']} vs {e.away_team['name']}")
        else:
            logger.info(f"\nNo Premier League games on {date_str}")
    
    return result

async def get_season_events(
    season: str = "2025-26",
    verbose: bool = False
) -> List[Event]:
    """
    Get all Premier League events for the season.
    
    Args:
        season: Season in format "2025-26" (defaults to 2025-26)
        verbose: Print detailed logging
    
    Returns:
        List of Event instances sorted by kickoff time
    """
    season_start, season_end = get_season_dates(season)
    
    all_events = []
    current_date = season_start
    
    logger.info(f"\nFetching Premier League {season} season games from {season_start.date()} to {season_end.date()}")
    
    async with httpx.AsyncClient() as client:
        while current_date <= season_end:
            events = await get_scheduled_events(client, current_date, verbose)
            all_events.extend(events)
            current_date += timedelta(days=1)
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)
    
    # Log raw event count before deduplication
    raw_count = len(all_events)
    
    # Deduplicate events
    all_events = deduplicate_events(all_events)
    
    # Group by matchday for reporting
    matchdays = {}
    for e in all_events:
        date = e.kickoff_utc.date()
        if date not in matchdays:
            matchdays[date] = []
        matchdays[date].append(e)
    
    # Log final results
    logger.info(f"\nFound {len(all_events)} unique Premier League games across {len(matchdays)} matchdays")
    if raw_count > len(all_events):
        logger.info(f"Removed {raw_count - len(all_events)} duplicate games")
    
    return all_events

async def get_watchlist(
    window_minutes: int = 90,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None
) -> List[Event]:
    """
    Get list of Premier League events to watch for lineups.
    
    Args:
        window_minutes: Minutes before kickoff to start polling
        date_from: Start date for events (defaults to today)
        date_to: End date for events (defaults to date_from + 3 days)
    
    Returns:
        List of Event instances within polling window
    """
    date_from = date_from or datetime.now(timezone.utc)
    date_to = date_to or (date_from + timedelta(days=3))
    now_utc = datetime.now(timezone.utc)
    
    # Get all events in date range
    all_events = []
    current_date = date_from
    
    async with httpx.AsyncClient() as client:
        while current_date <= date_to:
            events = await get_scheduled_events(client, current_date)
            all_events.extend(events)
            current_date += timedelta(days=1)
    
    # Filter for events within polling window or upcoming in next 6 hours
    watch = []
    for e in all_events:
        if is_within_window(e.kickoff_utc, now_utc, window_minutes) or (
            e.kickoff_utc > now_utc and (e.kickoff_utc - now_utc) <= timedelta(hours=6)
        ):
            watch.append(e)
    
    return watch