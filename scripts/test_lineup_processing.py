"""
Test script for real SofaScore lineup ingestion and processing pipeline.

This script:
1. Fetches real lineups from SofaScore API
2. Tests player mapping
3. Validates lineup processing
4. Simulates change detection
But does NOT make any actual Fantrax changes.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
import sys

from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.models import LineupStatus
from fantraxapi.providers.sofascore.discover import get_watchlist
from fantraxapi.providers.sofascore.poll import poll_events
from fantraxapi.providers.sofascore.normalize import normalize_lineup_data
from fantraxapi.lineups.automation import LineupAutomation
from tests.mocks.fantrax import MockFantrax

# Configure logging
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	handlers=[
		logging.StreamHandler(sys.stdout),
		logging.FileHandler('lineup_processing_test.log')
	]
)

logger = logging.getLogger(__name__)

async def main():
	"""Run lineup processing test with real SofaScore data."""
	try:
		# Initialize components
		player_mapping = PlayerMappingManager("config/player_mappings.yaml")
		
		# Use mock Fantrax client (since we won't make changes)
		fantrax = MockFantrax(
			username="test",
			password="test",
			league_id="test"
		)
		
		# Create output directory
		output_dir = Path("data/test_lineups")
		output_dir.mkdir(parents=True, exist_ok=True)
		
		# Initialize automation in test mode
		automation = LineupAutomation(
			fantrax=fantrax,
			player_mapping=player_mapping,
			output_dir=output_dir,
			test_mode=True,	 # Prevent any actual changes
			dry_run=True	 # Extra safety
		)
		
		logger.info("Starting lineup processing test...")
		
		# Get events to watch
		events = await get_watchlist(
			window_minutes=90  # Look for games in next 90 minutes
		)
		
		if not events:
			logger.info("No upcoming events found to test with")
			return
			
		logger.info(f"Found {len(events)} events to monitor:")
		for event in events:
			logger.info(f"- {event.kickoff_utc.strftime('%H:%M')} {event.home_team['name']} vs {event.away_team['name']}")
		
		# Poll for lineups
		results = await poll_events(
			events,
			poll_interval=60  # Poll every minute
		)
		
		if not results:
			logger.info("No lineup data available yet")
			return
			
		logger.info(f"\nProcessing {len(results)} lineup sets...")
		
		# Process each lineup
		for data in results:
			# Test lineup normalization
			try:
				lineup = normalize_lineup_data(data, player_mapping)
				logger.info(f"\nProcessed lineup for {lineup.home_team.team_name} vs {lineup.away_team.team_name}")
				
				# Log player mapping stats
				mapped_players = 0
				unmapped_players = 0
				for team in [lineup.home_team, lineup.away_team]:
					for player in team.players:
						if player.fantrax_id:
							mapped_players += 1
						else:
							unmapped_players += 1
							logger.warning(f"Unmapped player: {player.player_name} ({team.team_name})")
				
				logger.info(f"Player mapping: {mapped_players} mapped, {unmapped_players} unmapped")
				
				# Test lineup processing
				success = await automation.process_lineup(data)
				if success:
					logger.info("Successfully processed lineup")
					
					# Log what changes would be made if confirmed
					changes = automation.synchronizer.determine_changes(lineup)
					if changes:
						logger.info("Changes that would be made when confirmed:")
						for change in changes:
							logger.info(f"- {change}")
					else:
						logger.info("No changes would be needed")
						
				else:
					logger.error("Failed to process lineup")
					
			except Exception as e:
				logger.error(f"Error processing lineup: {e}", exc_info=True)
				continue
		
		# Generate test report
		report_file = output_dir / "lineup_processing_report.parquet"
		automation.tester.save_report(report_file)
		logger.info(f"\nTest report saved to {report_file}")
		
		stats = automation.tester.get_accuracy_stats()
		logger.info("\nTest Results:")
		for key, value in stats.items():
			logger.info(f"	{key}: {value}")
		
	except KeyboardInterrupt:
		logger.info("Test stopped by user")
		
	except Exception as e:
		logger.error(f"Test failed: {e}", exc_info=True)
		sys.exit(1)

if __name__ == "__main__":
	asyncio.run(main())
