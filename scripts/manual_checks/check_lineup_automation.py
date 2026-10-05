"""
Script to test lineup automation with real data.
"""
import asyncio
import logging
from pathlib import Path
import sys

from tests.mocks.fantrax import MockFantrax as Fantrax
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.automation import LineupAutomation

# Configure logging
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	handlers=[
		logging.StreamHandler(sys.stdout),
		logging.FileHandler('lineup_automation_test.log')
	]
)

logger = logging.getLogger(__name__)

async def main():
	"""Run lineup automation test."""
	try:
		# Initialize components
		player_mapping = PlayerMappingManager("config/player_mappings.yaml")
		
		# Initialize Fantrax client
		# Note: In test mode, we don't actually need valid credentials
		fantrax = Fantrax(
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
			test_mode=True	# Use test mode
		)
		
		logger.info("Starting lineup automation test...")
		
		# Run for 1 hour
		await automation.run(
			poll_interval=60,  # Poll every minute
			max_runtime=3600   # Run for 1 hour
		)
		
		logger.info("Test completed successfully")
		
	except KeyboardInterrupt:
		logger.info("Test stopped by user")
		
	except Exception as e:
		logger.error(f"Test failed: {e}", exc_info=True)
		sys.exit(1)

if __name__ == "__main__":
	asyncio.run(main())
