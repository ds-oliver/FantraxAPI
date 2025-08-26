"""
Tests for lineup status management functionality.
"""
from datetime import datetime, timedelta, timezone
import pytest

from fantraxapi.lineups.models import LineupRecord, LineupStatus, TeamLineup
from fantraxapi.lineups.status import (
	determine_lineup_status,
	validate_lineup_status,
	update_lineup_status,
	CONFIRMATION_WINDOW_MINUTES
)

@pytest.fixture
def sample_lineup():
	"""Create a sample lineup record for testing."""
	return LineupRecord(
		event_id=12345,
		tournament_id=17,
		tournament_name="Premier League",
		league_name="Premier League",
		league_country="England",
		kickoff_utc=datetime.now(timezone.utc) + timedelta(hours=2),
		captured_at_utc=datetime.now(timezone.utc),
		status=LineupStatus.PRELIMINARY,
		home_team=TeamLineup(
			team_id=1,
			team_name="Arsenal",
			side="home"
		),
		away_team=TeamLineup(
			team_id=2,
			team_name="Chelsea",
			side="away"
		)
	)

def test_determine_lineup_status_preliminary(sample_lineup):
	"""Test status determination for preliminary lineups."""
	# Way before kickoff
	current_time = sample_lineup.kickoff_utc - timedelta(hours=3)
	status = determine_lineup_status(sample_lineup, current_time)
	assert status == LineupStatus.PRELIMINARY

def test_determine_lineup_status_pending_confirmation(sample_lineup):
	"""Test status determination within confirmation window."""
	# Just inside confirmation window
	current_time = sample_lineup.kickoff_utc - timedelta(minutes=CONFIRMATION_WINDOW_MINUTES - 5)
	status = determine_lineup_status(sample_lineup, current_time)
	assert status == LineupStatus.PENDING_CONFIRMATION

def test_determine_lineup_status_confirmed(sample_lineup):
	"""Test status determination for confirmed lineups."""
	# Within confirmation window and confirmed
	current_time = sample_lineup.kickoff_utc - timedelta(minutes=30)
	sample_lineup.status = LineupStatus.CONFIRMED
	status = determine_lineup_status(sample_lineup, current_time)
	assert status == LineupStatus.CONFIRMED

def test_determine_lineup_status_final(sample_lineup):
	"""Test status determination after kickoff."""
	# After kickoff
	current_time = sample_lineup.kickoff_utc + timedelta(minutes=10)
	status = determine_lineup_status(sample_lineup, current_time)
	assert status == LineupStatus.FINAL

def test_validate_lineup_status(sample_lineup):
	"""Test lineup status validation."""
	# Valid preliminary status
	current_time = sample_lineup.kickoff_utc - timedelta(hours=3)
	assert validate_lineup_status(sample_lineup, current_time) is True
	
	# Invalid status for timing
	sample_lineup.status = LineupStatus.CONFIRMED
	assert validate_lineup_status(sample_lineup, current_time) is False
	
	# Final status is always valid
	sample_lineup.status = LineupStatus.FINAL
	assert validate_lineup_status(sample_lineup, current_time) is True

def test_update_lineup_status(sample_lineup):
	"""Test lineup status updates."""
	# Start with preliminary
	assert sample_lineup.status == LineupStatus.PRELIMINARY
	
	# Move into confirmation window
	current_time = sample_lineup.kickoff_utc - timedelta(minutes=CONFIRMATION_WINDOW_MINUTES - 5)
	changed = update_lineup_status(sample_lineup, current_time)
	assert changed is True
	assert sample_lineup.status == LineupStatus.PENDING_CONFIRMATION
	assert sample_lineup.previous_status == LineupStatus.PRELIMINARY
	
	# No change needed
	changed = update_lineup_status(sample_lineup, current_time)
	assert changed is False
	
	# Move to final
	current_time = sample_lineup.kickoff_utc + timedelta(minutes=10)
	changed = update_lineup_status(sample_lineup, current_time)
	assert changed is True
	assert sample_lineup.status == LineupStatus.FINAL
	assert sample_lineup.previous_status == LineupStatus.PENDING_CONFIRMATION
