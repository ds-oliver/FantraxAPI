"""
Test the complete lineup workflow from Sofascore to Fantrax.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

from fantraxapi.providers.sofascore.discover import get_json, API_BASE, get_scheduled_events
from fantraxapi.providers.sofascore.normalize import normalize_lineup_data, summarize_lineup
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.fantrax_sync import LineupSynchronizer
from fantraxapi.lineups.automation import LineupAutomation
from fantraxapi.fantrax import FantraxAPI

async def test_lineup_workflow(tournament_ids=[17]):  # Default to Premier League
	"""
	Test the complete workflow:
	1. Fetch lineups from Sofascore
	2. Map players to Fantrax IDs
	3. Prepare lineup changes
	4. (Optional) Apply changes to Fantrax
	"""
	# Initialize components
	player_mapping = PlayerMappingManager()
	
	print("\n1. Fetching lineups from Sofascore...")
	async with httpx.AsyncClient() as client:
		# Get today's events and next day
		all_events = []
		for days in range(2):
			future_date = datetime.now(timezone.utc) + timedelta(days=days)
			events = await get_scheduled_events(client, tournament_ids, future_date)
			all_events.extend(events)
		
		if not all_events:
			print("No upcoming games found")
			return
		
		print(f"\nFound {len(all_events)} upcoming events:")
		for e in all_events:
			print(
				f"- {e.kickoff_utc.strftime('%Y-%m-%d %H:%M')} "
				f"{e.home_team['name']} vs {e.away_team['name']} "
				f"(ID: {e.event_id})"
			)
		
		# Try to get lineups for each event
		for event in all_events:
			url = f"{API_BASE}/event/{event.event_id}/lineups"
			try:
				data = await get_json(client, url)
				print(f"\nFetched lineup data for {event.home_team['name']} vs {event.away_team['name']}")
				
				# Add required metadata
				data.update({
					"event_id": event.event_id,
					"tournament_id": event.tournament_id,
					"tournament_name": event.tournament_name,
					"kickoff_utc": event.kickoff_utc,
					"home_team": event.home_team,
					"away_team": event.away_team,
					"captured_at_utc": datetime.now(timezone.utc)
				})
				
				print("\n2. Normalizing lineup data and mapping players...")
				# Normalize data with player mapping
				records = normalize_lineup_data(data, player_mapping=player_mapping)
				if not records:
					print("No lineup data available yet")
					continue
				
				# Print lineup summary
				print("\nLineup Summary:")
				print(summarize_lineup(records))
				
				# Print mapping stats
				mapped_players = [r for r in records if r.fantrax_id]
				unmapped_players = [r for r in records if not r.fantrax_id]
				print(f"\nPlayer Mapping Stats:")
				print(f"- Mapped players: {len(mapped_players)}")
				print(f"- Unmapped players: {len(unmapped_players)}")
				if unmapped_players:
					print("\nUnmapped players:")
					for r in unmapped_players:
						print(f"- {r.name} ({r.team_name})")
				
				print("\n3. Testing lineup synchronization (dry run)...")
				# Initialize Fantrax components in dry-run mode
				fantrax = FantraxAPI("YOUR_LEAGUE_ID_HERE")
				sync = LineupSynchronizer(fantrax, dry_run=True)
				
				# Create automation with dry run
				automation = LineupAutomation(
					fantrax,
					player_mapping,
					dry_run=True
				)
				
				# Process lineup
				success = await automation.process_lineup(data)
				if success:
					print("Successfully processed lineup!")
					if sync.changes:
						print("\nProposed changes:")
						for change in sync.changes:
							print(f"- {change}")
				else:
					print("Failed to process lineup")
					if sync.errors:
						print("\nErrors:")
						for error in sync.errors:
							print(f"- {error}")
				
			except httpx.HTTPStatusError as e:
				if e.response.status_code == 404:
					print(f"\nNo lineup data yet for {event.home_team['name']} vs {event.away_team['name']}")
				else:
					raise

if __name__ == "__main__":
	asyncio.run(test_lineup_workflow())
