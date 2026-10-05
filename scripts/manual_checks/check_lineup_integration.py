"""
Integration test script for lineup automation.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from tests.mocks.fantrax import MockFantrax as Fantrax
from tests.mocks.sofascore import MockSofaScore, create_mock_player
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.automation import LineupAutomation

# Configure logging
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
	handlers=[
		logging.StreamHandler(sys.stdout),
		logging.FileHandler('lineup_integration_test.log')
	]
)

logger = logging.getLogger(__name__)

async def main():
	"""Run lineup integration test."""
	try:
		# Initialize components
		player_mapping = PlayerMappingManager("config/player_mappings.yaml")
		
		# Initialize mock clients
		fantrax = Fantrax(
			username="test",
			password="test",
			league_id="test"
		)
		sofascore = MockSofaScore()
		
		# Create output directory
		output_dir = Path("data/test_lineups")
		output_dir.mkdir(parents=True, exist_ok=True)
		
		# Add mock events
		now = datetime.now(timezone.utc)
		
		# Event 1: Starting soon
		event1_ko = now + timedelta(minutes=30)
		sofascore.add_mock_event(
			event_id=12345,
			home_team="Arsenal",
			away_team="Chelsea",
			kickoff_time=event1_ko
		)
		
		# Event 2: Later today
		event2_ko = now + timedelta(hours=3)
		sofascore.add_mock_event(
			event_id=12346,
			home_team="Manchester City",
			away_team="Liverpool",
			kickoff_time=event2_ko
		)
		
		# Add preliminary lineups
		sofascore.add_mock_lineup(
			event_id=12345,
			home_players=[
				create_mock_player(101, "Player 1", "F"),
				create_mock_player(102, "Player 2", "M", is_sub=True)
			],
			away_players=[
				create_mock_player(201, "Player 3", "F"),
				create_mock_player(202, "Player 4", "M", is_sub=True)
			],
			confirmed=False
		)
		
		sofascore.add_mock_lineup(
			event_id=12346,
			home_players=[
				create_mock_player(301, "Player 5", "F"),
				create_mock_player(302, "Player 6", "M", is_sub=True)
			],
			away_players=[
				create_mock_player(401, "Player 7", "F"),
				create_mock_player(402, "Player 8", "M", is_sub=True)
			],
			confirmed=False
		)
		
		# Add mock Fantrax lineups
		fantrax.set_mock_lineup(1, {
			"starters": [{"id": "101"}],
			"subs": [{"id": "102"}]
		})
		fantrax.set_mock_lineup(2, {
			"starters": [{"id": "201"}],
			"subs": [{"id": "202"}]
		})
		
		# Initialize automation in test mode
		automation = LineupAutomation(
			fantrax=fantrax,
			player_mapping=player_mapping,
			output_dir=output_dir,
			test_mode=True
		)
		
		logger.info("Starting lineup integration test...")
		
		# Process preliminary lineups
		for event_id in [12345, 12346]:
			lineup = await sofascore.get_event_lineup(event_id)
			await automation.process_lineup(lineup)
			
		# Wait a bit then confirm first event's lineup
		await asyncio.sleep(2)
		sofascore.add_mock_lineup(
			event_id=12345,
			home_players=[
				create_mock_player(101, "Player 1", "F"),
				create_mock_player(102, "Player 2", "M", is_sub=True)
			],
			away_players=[
				create_mock_player(201, "Player 3", "F"),
				create_mock_player(202, "Player 4", "M", is_sub=True)
			],
			confirmed=True
		)
		
		# Process confirmed lineup
		lineup = await sofascore.get_event_lineup(12345)
		await automation.process_lineup(lineup)
		
		# Wait a bit then make some changes to second event
		await asyncio.sleep(2)
		sofascore.add_mock_lineup(
			event_id=12346,
			home_players=[
				create_mock_player(301, "Player 5", "F"),
				create_mock_player(302, "Player 6", "M")  # No longer sub
			],
			away_players=[
				create_mock_player(401, "Player 7", "F", is_sub=True),	# Now a sub
				create_mock_player(402, "Player 8", "M")
			],
			confirmed=True
		)
		
		# Process updated lineup
		lineup = await sofascore.get_event_lineup(12346)
		await automation.process_lineup(lineup)
		
		logger.info("Test completed successfully")
		
	except KeyboardInterrupt:
		logger.info("Test stopped by user")
		
	except Exception as e:
		logger.error(f"Test failed: {e}", exc_info=True)
		sys.exit(1)

if __name__ == "__main__":
	asyncio.run(main())
