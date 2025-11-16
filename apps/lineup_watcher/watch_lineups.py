#!/usr/bin/env python
"""
CLI script to watch SofaScore lineups and trigger Fantrax actions.
"""
import argparse
import asyncio
from pathlib import Path

from fantraxapi.providers.sofascore.discover import get_watchlist
from fantraxapi.providers.sofascore.poll import poll_events
from fantraxapi.providers.sofascore.normalize import normalize_lineup_data
from fantraxapi.providers.sofascore.upsert import save_lineups


async def sync_lineups_to_fantrax(records):
	"""
	Sync confirmed lineups to Fantrax for all users with lineup automation enabled.
	
	Args:
		records: List of LineupRecord objects from SofaScore
	"""
	import logging
	from utils.user_manager import UserManager
	from utils.auth_helpers import load_requests_session_from_artifacts
	from fantraxapi import FantraxAPI
	from fantraxapi.lineups.fantrax_sync import LineupSynchronizer
	from fantraxapi.lineups.status import determine_lineup_status, LineupStatus
	
	logger = logging.getLogger(__name__)
	
	# Get all users with lineup automation enabled
	user_mgr = UserManager()
	all_users = user_mgr.list_all_users()
	
	# Filter to users with active automation
	# For now, this is a placeholder - in future, check user preferences
	users_with_automation = [
		u for u in all_users 
		if u.get("auth_status") == "connected"
	]
	
	if not users_with_automation:
		logger.debug("No users with active lineup automation")
		return
	
	logger.info(f"Processing lineups for {len(users_with_automation)} user(s)")
	
	for user in users_with_automation:
		user_id = user['user_id']
		
		try:
			# Load user's session
			artifacts = user_mgr.load_user_cookies(user_id)
			if not artifacts:
				logger.warning(f"No cookies found for user {user_id}")
				continue
			
			session = load_requests_session_from_artifacts(artifacts)
			
			# Process each lineup record
			for record in records:
				# Check if lineup is confirmed and ready to sync
				status = determine_lineup_status(record)
				
				if status not in [LineupStatus.CONFIRMED, LineupStatus.FINAL]:
					logger.debug(f"Skipping lineup {record.match_id} (status: {status})")
					continue
				
				# TODO: Get user's league_id and team_id from config
				# For now, this is a placeholder
				league_id = user.get("league_id")
				team_id = user.get("team_id")
				
				if not league_id or not team_id:
					logger.warning(f"No league_id or team_id configured for user {user_id}")
					continue
				
				# Initialize Fantrax API
				api = FantraxAPI(league_id, session=session)
				
				# Create synchronizer
				syncer = LineupSynchronizer(
					fantrax=api,
					dry_run=False,  # Set to True for testing
					logger=logger
				)
				
				# Sync the lineup
				success = syncer.sync_lineup(record, team_id)
				
				if success:
					logger.info(f"Successfully synced lineup for user {user_id}")
				else:
					logger.error(f"Failed to sync lineup for user {user_id}")
		
		except Exception as e:
			logger.exception(f"Error syncing lineups for user {user_id}: {e}")
			continue


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
	return parser.parse_args()

async def main():
	args = parse_args()
	output_dir = Path(args.output_dir)
	
	while True:
		try:
			# Get events to watch
			events = await get_watchlist(
				window_minutes=args.window
			)
			
			if events:
				# Poll for lineups
				results = await poll_events(
					events,
					poll_interval=args.interval
				)
				
				if results:
					print(f"\nProcessing {len(results)} lineup sets...")
					
					# Process each result
					for data in results:
						# Normalize data
						records = normalize_lineup_data(data)
						
						# Save to parquet
						save_lineups(records, output_dir)
						
						# Fantrax integration - sync confirmed lineups
						await sync_lineups_to_fantrax(records)
			
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
