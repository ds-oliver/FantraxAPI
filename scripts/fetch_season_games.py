#!/usr/bin/env python
"""
Fetch and save all Premier League games for the season.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd
from tenacity import retry, wait_exponential, stop_after_attempt

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
	return (
		# Must be Premier League tournament
		unique_tournament.get("id") == PREMIER_LEAGUE_ID and
		unique_tournament.get("name") == PREMIER_LEAGUE_NAME and
		# Must be regular league game (not cup/friendly)
		tournament.get("name") == PREMIER_LEAGUE_NAME and
		# Must have both teams
		event.get("homeTeam") and
		event.get("awayTeam")
	)

def get_event_key(event: Dict) -> str:
	"""
	Get unique key for event to deduplicate.
	
	Args:
		event: Event data from SofaScore API
	
	Returns:
		str: Unique key combining home team, away team, and date
	"""
	# Convert timestamp to date string
	date = utc_ts_to_dt(event["startTimestamp"]).date().isoformat()
	# Include home/away order to preserve both fixtures
	return f"{date}_{event['homeTeam']['id']}_vs_{event['awayTeam']['id']}"

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
	
	if verbose:
		print(f"\nFound {len(events)} total events on {date_str}")
	
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
	
	if verbose and result:
		print(f"- Premier League games:")
		for e in result:
			print(f"  • {e.kickoff_utc.strftime('%H:%M')} {e.home_team['name']} vs {e.away_team['name']}")
	
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
	
	print(f"\nFetching Premier League {season} season games from {season_start.date()} to {season_end.date()}")
	
	async with httpx.AsyncClient() as client:
		while current_date <= season_end:
			events = await get_scheduled_events(client, current_date, verbose)
			if events:
				print(f"Found {len(events)} Premier League games on {current_date.date()}")
				all_events.extend(events)
			current_date += timedelta(days=1)
			# Small delay to avoid rate limiting
			await asyncio.sleep(0.5)
	
	# Sort by kickoff time
	all_events.sort(key=lambda e: e.kickoff_utc)
	
	# Group by matchday
	matchdays = {}
	for e in all_events:
		date = e.kickoff_utc.date()
		if date not in matchdays:
			matchdays[date] = []
		matchdays[date].append(e)
	
	print(f"\nFound {len(all_events)} total Premier League games across {len(matchdays)} matchdays")
	
	return all_events

def deduplicate_schedule(df: pd.DataFrame) -> pd.DataFrame:
	"""
	Deduplicate schedule by keeping only one instance of each fixture.
	
	Args:
		df: DataFrame with schedule
	
	Returns:
		Deduplicated DataFrame
	"""
	# Create a date column for grouping into matchweeks
	df['date'] = pd.to_datetime(df['kickoff_utc']).dt.date
	
	# Sort by kickoff time to keep the earliest time for each game
	df = df.sort_values('kickoff_utc')
	
	# Create a unique game identifier that preserves home/away order
	df['game_id'] = df.apply(
		lambda x: f"{x['date']}_{x['home_team_id']}_vs_{x['away_team_id']}",
		axis=1
	)
	
	# Drop duplicates keeping first occurrence (earliest time)
	df = df.drop_duplicates(
		subset=['game_id'],
		keep='first'
	)
	
	# Drop temporary columns
	df = df.drop(['date', 'game_id'], axis=1)
	
	return df

def validate_schedule(df: pd.DataFrame) -> bool:
	"""
	Validate Premier League schedule.
	
	Args:
		df: DataFrame with schedule
	
	Returns:
		bool: True if schedule is valid
	"""
	is_valid = True
	
	# Count total games
	total_games = len(df)
	games_per_team = total_games / 20  # 20 teams in Premier League
	print(f"\nSchedule stats:")
	print(f"- Total games: {total_games}")
	print(f"- Games per team: {games_per_team:.1f}")
	if total_games != 380:	# 20 teams * 38 games / 2 (each game counts for 2 teams)
		print(f"WARNING: Expected 380 total games but found {total_games}")
		is_valid = False
	
	# Count games per team
	print("\nGames per team:")
	all_teams = sorted(set(df['home_team_name'].unique()) | set(df['away_team_name'].unique()))
	for team in all_teams:
		home = len(df[df['home_team_name'] == team])
		away = len(df[df['away_team_name'] == team])
		total = home + away
		print(f"- {team}: {total} games ({home} home, {away} away)")
		if total != 38:
			print(f"  WARNING: Expected 38 games but found {total}")
			is_valid = False
		if home != 19:
			print(f"  WARNING: Expected 19 home games but found {home}")
			is_valid = False
		if away != 19:
			print(f"  WARNING: Expected 19 away games but found {away}")
			is_valid = False
	
	# Check for duplicate fixtures
	df['fixture'] = df.apply(
		lambda x: tuple(sorted([x['home_team_name'], x['away_team_name']])),
		axis=1
	)
	fixture_counts = df.groupby('fixture').size()
	duplicates = fixture_counts[fixture_counts != 2]  # Each team should play home and away
	if not duplicates.empty:
		print("\nWARNING: Found irregular fixtures:")
		for fixture, count in duplicates.items():
			print(f"- {' vs '.join(fixture)}: {count} times (expected 2)")
			games = df[df['fixture'] == fixture].sort_values('kickoff_utc')
			for _, row in games.iterrows():
				print(f"  • {row['kickoff_utc']}: {row['home_team_name']} vs {row['away_team_name']}")
		is_valid = False
	
	df = df.drop('fixture', axis=1)
	return is_valid

async def main():
	# Parse arguments
	parser = argparse.ArgumentParser(description="Fetch Premier League season schedule")
	parser.add_argument(
		"--season",
		type=str,
		default="2025-26",
		help='Season in format "2025-26" (default: 2025-26)'
	)
	parser.add_argument(
		"--output-dir",
		type=str,
		default="data/schedule",
		help="Directory to save schedule (default: data/schedule)"
	)
	parser.add_argument(
		"--verbose",
		action="store_true",
		help="Print detailed logging"
	)
	args = parser.parse_args()
	
	# Get all games
	events = await get_season_events(season=args.season, verbose=args.verbose)
	
	if not events:
		print(f"\nNo Premier League games found for {args.season} season")
		return
	
	# Convert to DataFrame
	records = []
	for e in events:
		records.append({
			"event_id": e.event_id,
			"tournament_id": e.tournament_id,
			"tournament_name": e.tournament_name,
			"kickoff_utc": e.kickoff_utc,
			"home_team_id": e.home_team["id"],
			"home_team_name": e.home_team["name"],
			"away_team_id": e.away_team["id"],
			"away_team_name": e.away_team["name"],
			"season": args.season
		})
	
	df = pd.DataFrame(records)
	
	# Deduplicate schedule
	df = deduplicate_schedule(df)
	
	# Validate schedule
	is_valid = validate_schedule(df)
	if not is_valid:
		print("\nWARNING: Schedule validation failed!")
	
	# Save to parquet
	output_dir = Path(args.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	
	out_file = output_dir / f"schedule_{df['tournament_name'].iloc[0].replace(' ', '')}_{args.season}.parquet"
	df.to_parquet(out_file, index=False)
	print(f"\nSaved {len(df)} games to {out_file}")
	
	# Print schedule by month
	print("\nSchedule:")
	pd.set_option('display.max_rows', None)	 # Show all rows
	pd.set_option('display.width', None)
	pd.set_option('display.max_columns', None)
	
	# Group by month for cleaner display
	df['month'] = df['kickoff_utc'].dt.strftime('%B %Y')
	for month, month_df in df.groupby('month'):
		print(f"\n{month}:")
		display_df = month_df[["kickoff_utc", "home_team_name", "away_team_name"]]
		display_df = display_df.sort_values('kickoff_utc')
		print(display_df.to_string(index=False))

if __name__ == "__main__":
	asyncio.run(main())