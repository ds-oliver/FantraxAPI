"""
Tests for lineup automation system.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from unittest.mock import Mock, AsyncMock

from ..mocks.fantrax import MockFantrax as Fantrax
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.models import LineupStatus
from fantraxapi.lineups.automation import LineupAutomation

# Test data
SAMPLE_LINEUP_DATA = {
	"event_id": 12345,
	"tournament_id": 17,
	"tournament_name": "Premier League",
	"league_name": "Premier League",
	"league_country": "England",
	"kickoff_utc": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
	"home_team": {
		"id": 1,
		"name": "Arsenal"
	},
	"away_team": {
		"id": 2,
		"name": "Chelsea"
	},
	"home": {
		"formation": "4-3-3",
		"players": [
			{
				"player": {
					"id": 101,
					"name": "Player 1",
					"position": "F"
				},
				"substitute": False
			},
			{
				"player": {
					"id": 102,
					"name": "Player 2",
					"position": "M"
				},
				"substitute": True
			}
		]
	},
	"away": {
		"formation": "4-4-2",
		"players": [
			{
				"player": {
					"id": 201,
					"name": "Player 3",
					"position": "F"
				},
				"substitute": False
			},
			{
				"player": {
					"id": 202,
					"name": "Player 4",
					"position": "M"
				},
				"substitute": True
			}
		]
	}
}

@pytest.fixture
def mock_fantrax():
	"""Create mock Fantrax client."""
	mock = Mock(spec=Fantrax)
	mock.get_team_lineup = Mock(return_value={
		"starters": [{"id": "101"}, {"id": "201"}],
		"subs": [{"id": "102"}, {"id": "202"}]
	})
	mock.swap_players = AsyncMock()
	mock.move_to_lineup = AsyncMock()
	return mock

@pytest.fixture
def mock_player_mapping():
	"""Create mock player mapping manager."""
	mock = Mock(spec=PlayerMappingManager)
	mock.get_by_sofascore_id = Mock(return_value=Mock(
		fantrax_id="test_id",
		fantrax_name="Test Player"
	))
	return mock

@pytest.fixture
def test_output_dir(tmp_path):
	"""Create temporary output directory."""
	output_dir = tmp_path / "test_lineups"
	output_dir.mkdir()
	return output_dir

@pytest.fixture
def automation(mock_fantrax, mock_player_mapping, test_output_dir):
	"""Create LineupAutomation instance for testing."""
	return LineupAutomation(
		fantrax=mock_fantrax,
		player_mapping=mock_player_mapping,
		output_dir=test_output_dir,
		test_mode=True
	)

@pytest.mark.asyncio
async def test_process_preliminary_lineup(automation):
	"""Test processing a preliminary lineup."""
	# Process lineup
	success = await automation.process_lineup(SAMPLE_LINEUP_DATA)
	assert success
	
	# Check it was stored
	assert 12345 in automation.processed_lineups
	lineup = automation.processed_lineups[12345]
	assert lineup.status == LineupStatus.PRELIMINARY
	
	# Check no changes were made
	assert not automation.synchronizer.changes

@pytest.mark.asyncio
async def test_process_confirmed_lineup(automation):
	"""Test processing a confirmed lineup."""
	# Modify data to be confirmed
	data = SAMPLE_LINEUP_DATA.copy()
	data["confirmed"] = True
	
	# Set to production mode
	automation.test_mode = False
	
	# Process lineup
	success = await automation.process_lineup(data)
	assert success
	
	# Check it was stored
	assert 12345 in automation.processed_lineups
	lineup = automation.processed_lineups[12345]
	assert lineup.status == LineupStatus.CONFIRMED
	
	# Check changes were made
	assert len(automation.synchronizer.changes) > 0

@pytest.mark.asyncio
async def test_dry_run_mode(automation):
	"""Test dry run mode."""
	# Set to dry run mode
	automation.test_mode = False
	automation.dry_run = True
	
	# Process lineup
	success = await automation.process_lineup(SAMPLE_LINEUP_DATA)
	assert success
	
	# Check no actual changes were made
	automation.fantrax.swap_players.assert_not_called()
	automation.fantrax.move_to_lineup.assert_not_called()

@pytest.mark.asyncio
async def test_test_mode_reporting(automation, test_output_dir):
	"""Test generation of test mode reports."""
	# Process both preliminary and confirmed versions
	await automation.process_lineup(SAMPLE_LINEUP_DATA)
	
	confirmed_data = SAMPLE_LINEUP_DATA.copy()
	confirmed_data["confirmed"] = True
	await automation.process_lineup(confirmed_data)
	
	# Run automation briefly
	await automation.run(max_runtime=1)
	
	# Check report was generated
	report_file = test_output_dir / "lineup_test_report.parquet"
	assert report_file.exists()
	
	# Check stats were generated
	stats = automation.tester.get_accuracy_stats()
	assert stats["total_lineups"] > 0

@pytest.mark.asyncio
async def test_error_handling(automation, mock_fantrax):
	"""Test handling of Fantrax API errors."""
	# Make Fantrax API fail
	mock_fantrax.swap_players.side_effect = Exception("API Error")
	
	# Set to production mode
	automation.test_mode = False
	
	# Process confirmed lineup
	data = SAMPLE_LINEUP_DATA.copy()
	data["confirmed"] = True
	
	success = await automation.process_lineup(data)
	assert not success
	
	# Check error was tracked
	assert len(automation.synchronizer.errors) > 0

if __name__ == "__main__":
	pytest.main([__file__])
