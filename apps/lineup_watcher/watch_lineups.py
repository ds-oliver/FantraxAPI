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
						
						# TODO: Add Fantrax integration
						# from fantraxapi.lineup_rules.fantrax_actions import update_lineups
						# await update_lineups(records)
			
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
