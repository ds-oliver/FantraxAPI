#!/usr/bin/env python
"""
CLI script to watch SofaScore lineups and trigger Fantrax actions.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys

import pandas as pd

from fantraxapi.lineups.sofascore_watch import watch_lineups
from fantraxapi.lineups.sofascore_normalize import normalize_lineup_data
from fantraxapi.player_mapping import PlayerMappingManager
from tests.mocks.fantrax import MockFantrax	 # Only for testing

def setup_logging(output_dir: Path, test_mode: bool = False):
	"""Setup logging to both file and terminal."""
	# Create logs directory
	log_dir = output_dir / "logs"
	log_dir.mkdir(parents=True, exist_ok=True)
	
	# Create timestamped log file name
	timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	mode = "test" if test_mode else "live"
	log_file = log_dir / f"lineup_watcher_{mode}_{timestamp}.log"
	
	# Configure logging
	handlers = [
		# File handler with detailed formatting
		logging.FileHandler(log_file),
		# Stream handler for terminal output
		logging.StreamHandler(sys.stdout)
	]
	
	# Configure format for each handler
	for handler in handlers:
		if isinstance(handler, logging.FileHandler):
			# Detailed format for file
			handler.setFormatter(logging.Formatter(
				'%(asctime)s - %(name)s - %(levelname)s - %(message)s'
			))
		else:
			# Simpler format for terminal
			handler.setFormatter(logging.Formatter('%(message)s'))
	
	# Setup root logger
	logging.basicConfig(
		level=logging.INFO,
		handlers=handlers
	)
	
	logger = logging.getLogger()
	logger.info(f"Starting lineup watcher in {'test' if test_mode else 'live'} mode")
	logger.info(f"Logging to {log_file}")
	
	return logger

def parse_args():
	parser = argparse.ArgumentParser(description="Watch SofaScore lineups")
	parser.add_argument(
		"--window",
		type=int,
		default=90,
		help="Minutes before kickoff to start polling (default: 90)"
	)
	parser.add_argument(
		"--interval",
		type=int,
		default=60,
		help="Seconds between polls (default: 60)"
	)
	parser.add_argument(
		"--output-dir",
		type=str,
		default="data/lineups",
		help="Directory to save lineup data (default: data/lineups)"
	)
	parser.add_argument(
		"--days-ahead",
		type=int,
		default=7,
		help="Number of days ahead to look for games (default: 7)"
	)
	parser.add_argument(
		"--test-mode",
		action="store_true",
		help="Run in test mode to process future games"
	)
	return parser.parse_args()

async def main():
	args = parse_args()
	
	# Create output directory
	output_dir = Path(args.output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)
	
	# Setup logging
	logger = setup_logging(output_dir, args.test_mode)
	
	# Initialize components
	logger.info("Initializing components...")
	player_mapping = PlayerMappingManager("config/player_mappings.yaml")
	fantrax = MockFantrax("test", "test", "test")  # Mock client for testing
	
	while True:
		try:
			if args.test_mode:
				# In test mode, look ahead for future games
				now = datetime.now(timezone.utc)
				date_to = now + timedelta(days=args.days_ahead)
				print(f"\nLooking for games from {now.date()} to {date_to.date()}")
				
				# Import here to avoid circular dependency
				from fantraxapi.providers.sofascore.discover import get_season_events
				events = await get_season_events(verbose=True)
				
				# Filter for games in our date range
				events = [
					e for e in events 
					if now <= e.kickoff_utc <= date_to
				]
				
				if events:
					print(f"\nFound {len(events)} games in date range:")
					for e in events:
						print(f"- {e.kickoff_utc.strftime('%Y-%m-%d %H:%M')} {e.home_team['name']} vs {e.away_team['name']}")
				else:
					print("No games found in date range")
					break
					
				# Process each game
				results = []
				for event in events:
					# Create mock lineup data
					data = {
						"event_id": event.event_id,
						"league_name": "Premier League",
						"league_country": "England",
						"kickoff_utc": event.kickoff_utc,
						"home_team": event.home_team,
						"away_team": event.away_team,
						"captured_at_utc": datetime.now(timezone.utc),
						"home": {
							"formation": "4-3-3",
							"players": []  # We'll get real players from API
						},
						"away": {
							"formation": "4-4-2",
							"players": []  # We'll get real players from API
						}
					}
					results.append(data)
				
				# Only process once in test mode
				if not results:
					break
			else:
				# Normal mode - watch for real lineups
				results = await watch_lineups(
					window_minutes=args.window,
					poll_interval=args.interval
				)
			
			if results:
				print(f"\nProcessing {len(results)} lineup sets...")
				
				# Process each result
				for data in results:
					# Normalize data with player mapping
					records = normalize_lineup_data(data, player_mapping)
					
					# Log player mapping stats
					mapped_players = sum(1 for r in records if r.fantrax_id)
					unmapped_players = sum(1 for r in records if not r.fantrax_id)
					print(f"\nPlayer mapping stats:")
					print(f"- Mapped: {mapped_players}")
					print(f"- Unmapped: {unmapped_players}")
					
					# Log unmapped players
					if unmapped_players > 0:
						print("\nUnmapped players:")
						for r in records:
							if not r.fantrax_id:
								print(f"- {r.player_name} ({r.team_name})")
					
					# Convert to DataFrame
					df = pd.DataFrame([r.model_dump() for r in records])
					
					# Save to parquet
					event_id = data["event_id"]
					league = data["league_name"].replace(" ", "")
					status = "preliminary"	# Since we know these aren't confirmed yet
					out_file = output_dir / f"lineups_{league}_{event_id}_{status}.parquet"
					
					df.sort_values([
						"side",
						"is_sub",
						"shirt_number"
					]).to_parquet(out_file, index=False)
					
					print(f"Saved lineups to {out_file}")
					
					# Simulate what changes would be needed (without making them)
					team_lineups = {}
					for r in records:
						if r.fantrax_id:
							team_id = r.team_id
							if team_id not in team_lineups:
								team_lineups[team_id] = {"starters": [], "subs": []}
							if r.is_sub:
								team_lineups[team_id]["subs"].append(r.fantrax_id)
							else:
								team_lineups[team_id]["starters"].append(r.fantrax_id)
					
					# Set mock lineups for comparison
					for team_id, lineup in team_lineups.items():
						fantrax.set_mock_lineup(team_id, lineup)
					
					print("\nLineup changes that would be made when confirmed:")
					for team_id, lineup in team_lineups.items():
						print(f"\nTeam {team_id}:")
						print(f"- Starters: {len(lineup['starters'])}")
						print(f"- Subs: {len(lineup['subs'])}")
			
			# Wait before next check
			await asyncio.sleep(args.interval)
			
		except KeyboardInterrupt:
			print("\nStopping lineup watcher...")
			break
			
		except Exception as e:
			print(f"Error in main loop: {e}")
			await asyncio.sleep(args.interval)

if __name__ == "__main__":
	asyncio.run(main())
