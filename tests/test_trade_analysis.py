"""
Unit tests for trade analysis functionality.
Run with: python -m pytest tests/test_trade_analysis.py -v
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fantraxapi.trade_analysis import (
	TeamPositionProfile,
	TradeMatchSuggestion,
	_extract_primary_position,
	compute_surplus_and_needs,
	compute_trade_matches,
	default_position_limits,
)


class MockPos:
	"""Mock position object for testing."""
	def __init__(self, short_name):
		self.short_name = short_name


class MockPlayer:
	"""Mock player object for testing."""
	def __init__(self, name):
		self.name = name


class MockRosterRow:
	"""Mock roster row for testing."""
	def __init__(self, pos_short_name, player_name="Test Player", actual_position=None):
		self.pos = MockPos(pos_short_name)
		self.player = MockPlayer(player_name)
		# Simulate _raw data with scorer.posShortNames (for reserve players)
		if actual_position:
			self._raw = {"scorer": {"posShortNames": [actual_position]}}
		else:
			self._raw = None


def test_extract_primary_position():
	"""Test position extraction from roster rows."""
	# Test goalkeeper
	row = MockRosterRow("GK")
	assert _extract_primary_position(row) == "GK"
	
	row = MockRosterRow("G")
	assert _extract_primary_position(row) == "GK"
	
	# Test defender
	row = MockRosterRow("D")
	assert _extract_primary_position(row) == "D"
	
	row = MockRosterRow("DEF")
	assert _extract_primary_position(row) == "D"
	
	# Test midfielder
	row = MockRosterRow("M")
	assert _extract_primary_position(row) == "M"
	
	row = MockRosterRow("MID")
	assert _extract_primary_position(row) == "M"
	
	# Test forward
	row = MockRosterRow("F")
	assert _extract_primary_position(row) == "F"
	
	row = MockRosterRow("FWD")
	assert _extract_primary_position(row) == "F"
	
	# Test reserve/bench players with actual positions in raw data
	# This is the key feature - reserves should count with their actual position
	row = MockRosterRow("Res", actual_position="D")
	assert _extract_primary_position(row) == "D", "Reserve defender should be counted as D"
	
	row = MockRosterRow("Res", actual_position="M")
	assert _extract_primary_position(row) == "M", "Reserve midfielder should be counted as M"
	
	row = MockRosterRow("Res", actual_position="F")
	assert _extract_primary_position(row) == "F", "Reserve forward should be counted as F"
	
	row = MockRosterRow("BN", actual_position="GK")
	assert _extract_primary_position(row) == "GK", "Benched goalkeeper should be counted as GK"
	
	# Test reserve without actual position data (shouldn't happen but handle gracefully)
	row = MockRosterRow("Res")
	assert _extract_primary_position(row) is None
	
	print("✓ Position extraction tests passed (including reserves)")


def test_compute_surplus_and_needs():
	"""Test surplus/need computation."""
	# Create mock profiles
	profiles = {
		"team1": TeamPositionProfile(
			league_id="test",
			team_id="team1",
			team_name="Team 1",
			division="Test",
			position_counts={"GK": 3, "D": 8, "M": 6, "F": 4},
			total_players=21,
		),
		"team2": TeamPositionProfile(
			league_id="test",
			team_id="team2",
			team_name="Team 2",
			division="Test",
			position_counts={"GK": 2, "D": 4, "M": 8, "F": 6},
			total_players=20,
		),
		"team3": TeamPositionProfile(
			league_id="test",
			team_id="team3",
			team_name="Team 3",
			division="Test",
			position_counts={"GK": 2, "D": 6, "M": 6, "F": 5},
			total_players=19,
		),
	}
	
	# Compute surplus/needs
	compute_surplus_and_needs(profiles, min_delta=1, position_limits=default_position_limits())
	
	# Team 1 should have surplus D (8 vs avg ~6)
	assert "D" in profiles["team1"].surplus_positions
	
	# Team 2 should have surplus M (8 vs avg ~6.67)
	assert "M" in profiles["team2"].surplus_positions
	
	# Team 2 should need D (4 vs avg ~6)
	assert "D" in profiles["team2"].need_positions
	
	print("✓ Surplus/need computation tests passed")


def test_compute_surplus_and_needs_uses_limits():
	"""Constraints should trigger surplus/needs even without large average deltas."""
	profiles = {
		"team1": TeamPositionProfile(
			league_id="test",
			team_id="team1",
			team_name="Team 1",
			division="Test",
			position_counts={"GK": 2, "D": 2, "M": 7, "F": 3},
			total_players=14,
		),
		"team2": TeamPositionProfile(
			league_id="test",
			team_id="team2",
			team_name="Team 2",
			division="Test",
			position_counts={"GK": 1, "D": 4, "M": 5, "F": 3},
			total_players=13,
		),
	}
	
	# Use high min_delta so averages won't trip flags
	compute_surplus_and_needs(profiles, min_delta=5, position_limits=default_position_limits())
	
	assert "M" in profiles["team1"].surplus_positions, "7 midfielders exceeds 5 active + 1 buffer"
	assert "D" in profiles["team1"].need_positions, "Only 2 defenders violates minimum requirement"


def test_compute_trade_matches():
	"""Test trade partner matching."""
	# Create profiles with complementary needs
	profiles = {
		"your_team": TeamPositionProfile(
			league_id="test",
			team_id="your_team",
			team_name="Your Team",
			division="Test",
			position_counts={"GK": 2, "D": 8, "M": 4, "F": 6},
			total_players=20,
			surplus_positions=["D"],
			need_positions=["M"],
		),
		"partner1": TeamPositionProfile(
			league_id="test",
			team_id="partner1",
			team_name="Partner 1",
			division="Test",
			position_counts={"GK": 2, "D": 4, "M": 8, "F": 6},
			total_players=20,
			surplus_positions=["M"],
			need_positions=["D"],
		),
		"partner2": TeamPositionProfile(
			league_id="test",
			team_id="partner2",
			team_name="Partner 2",
			division="Test",
			position_counts={"GK": 2, "D": 6, "M": 6, "F": 6},
			total_players=20,
			surplus_positions=[],
			need_positions=[],
		),
	}
	
	# Compute trade matches
	suggestions = compute_trade_matches(profiles, "your_team")
	
	# Should have one suggestion (partner1)
	assert len(suggestions) == 1
	assert suggestions[0].partner_team_id == "partner1"
	assert suggestions[0].score == 2  # D and M match
	assert "D" in suggestions[0].your_surplus_their_need
	assert "M" in suggestions[0].their_surplus_your_need
	
	print("✓ Trade matching tests passed")


def test_division_name_parsing():
	"""Test division name parsing from team names."""
	from fantraxapi.utils import parse_division_from_team_name
	
	# Test 2-3 letter codes
	assert parse_division_from_team_name("BB - Team Name") == "BB"
	assert parse_division_from_team_name("PL - Another Team") == "PL"
	assert parse_division_from_team_name("GG - Test") == "GG"
	
	# Test alphanumeric patterns (digit + letter)
	assert parse_division_from_team_name("2A - DraftAlchemy") == "2A"
	assert parse_division_from_team_name("1A - Team") == "1A"
	assert parse_division_from_team_name("3B - Squad") == "3B"
	
	# Test single digits
	assert parse_division_from_team_name("1 - Team") == "Division 1"
	assert parse_division_from_team_name("2 - Team") == "Division 2"
	
	# Test with emojis/symbols before division code (edge case)
	assert parse_division_from_team_name("🏆🏆GG - Bantah Boyz") == "GG"
	assert parse_division_from_team_name("⭐BB - Star Team") == "BB"
	assert parse_division_from_team_name("🔥2A - Fire Squad") == "2A"
	assert parse_division_from_team_name("🎯1 - Target Team") == "Division 1"
	
	# Test invalid patterns (should return None)
	assert parse_division_from_team_name("Team Name") is None
	assert parse_division_from_team_name("InvalidPrefix - Team") is None
	assert parse_division_from_team_name("🏆🏆 - Only Emojis") is None  # Only emojis, no division code
	assert parse_division_from_team_name("") is None
	assert parse_division_from_team_name(None) is None
	
	print("✓ Division name parsing tests passed (including emoji edge cases)")


if __name__ == "__main__":
	print("Running trade analysis tests...")
	print()
	
	try:
		test_extract_primary_position()
		test_compute_surplus_and_needs()
		test_compute_trade_matches()
		test_division_name_parsing()
		print()
		print("=" * 50)
		print("All tests passed! ✓")
		print("=" * 50)
	except AssertionError as e:
		print()
		print("=" * 50)
		print(f"Test failed: {e}")
		print("=" * 50)
		sys.exit(1)
	except Exception as e:
		print()
		print("=" * 50)
		print(f"Error running tests: {e}")
		print("=" * 50)
		sys.exit(1)
