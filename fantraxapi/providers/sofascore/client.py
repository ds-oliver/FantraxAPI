"""
SofaScore API client focused on Premier League lineups.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Dict, List, Optional

import httpx
from tenacity import retry, wait_exponential, stop_after_attempt

logger = logging.getLogger(__name__)

# API configuration
API_BASE = "https://api.sofascore.com/api/v1"
USER_AGENT = "Mozilla/5.0 (analytics/lineups)"

# Premier League configuration
PREMIER_LEAGUE_ID = 17
PREMIER_LEAGUE_SEASON = "2025-26"  # Current season

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

async def get_season_id() -> int:
	"""Get the current Premier League season ID."""
	async with httpx.AsyncClient() as client:
		url = f"{API_BASE}/unique-tournament/{PREMIER_LEAGUE_ID}/seasons"
		response = await get_json(client, url)
		# Find season by year
		for season in response["seasons"]:
			if season["year"] == PREMIER_LEAGUE_SEASON:
				return season["id"]
	raise ValueError(f"Season {PREMIER_LEAGUE_SEASON} not found")

async def get_matches(date: Optional[datetime] = None) -> List[Dict]:
	"""
	Get Premier League matches for a specific date.
	
	Args:
		date: Date to get matches for (defaults to today)
		
	Returns:
		List of match dictionaries
	"""
	date = date or datetime.now(timezone.utc)
	date_str = date.strftime("%Y-%m-%d")
	
	async with httpx.AsyncClient() as client:
		# Get all matches for the date
		url = f"{API_BASE}/sport/football/scheduled-events/{date_str}"
		response = await get_json(client, url)
		
		# Filter for Premier League matches
		matches = []
		for event in response.get("events", []):
			tournament = event.get("tournament", {}).get("uniqueTournament", {})
			if tournament.get("id") == PREMIER_LEAGUE_ID:
				matches.append(event)
		
		if matches:
			logger.info(f"Found {len(matches)} Premier League matches on {date_str}")
			for match in matches:
				logger.info(f"- {match['homeTeam']['name']} vs {match['awayTeam']['name']}")
		else:
			logger.info(f"No Premier League matches found on {date_str}")
			
		return matches

async def get_match_lineups(match_id: int) -> Optional[Dict]:
	"""
	Get lineups for a specific match.
	
	Args:
		match_id: SofaScore match ID
		
	Returns:
		Lineup data if available, None otherwise
	"""
	async with httpx.AsyncClient() as client:
		url = f"{API_BASE}/event/{match_id}/lineups"
		try:
			data = await get_json(client, url)
			
			# Add lineup status info
			data["is_confirmed"] = all(
				sum(1 for p in data.get(side, {}).get("players", [])
					if not p.get("substitute", False)) == 11
				for side in ["home", "away"]
			)
			
			# Add metadata
			match_data = await get_json(client, f"{API_BASE}/event/{match_id}")
			data.update({
				"event_id": match_id,
				"kickoff_utc": datetime.fromtimestamp(
					match_data["event"]["startTimestamp"],
					tz=timezone.utc
				),
				"home_team": match_data["event"]["homeTeam"],
				"away_team": match_data["event"]["awayTeam"],
				"captured_at_utc": datetime.now(timezone.utc)
			})
			
			return data
			
		except httpx.HTTPStatusError as e:
			if e.response.status_code == 404:
				# Lineups not available yet
				logger.debug(f"Lineups not yet available for match {match_id}")
				return None
			raise

async def get_match_url(match_id: int) -> str:
	"""
	Get the SofaScore web URL for a match.
	
	Args:
		match_id: SofaScore match ID
		
	Returns:
		URL to match page
	"""
	async with httpx.AsyncClient() as client:
		match_data = await get_json(client, f"{API_BASE}/event/{match_id}")
		event = match_data["event"]
		return (
			f"https://www.sofascore.com/{event['homeTeam']['slug']}-"
			f"{event['awayTeam']['slug']}/{event['customId']}#id:{match_id}"
		)
