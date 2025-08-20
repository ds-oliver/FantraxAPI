"""
Tests for SofaScore lineup normalization.
"""
from datetime import datetime, timezone

import pytest

from fantraxapi.providers.sofascore.normalize import normalize_lineup_data

@pytest.fixture
def sample_lineup_data():
    """Sample lineup data from SofaScore API."""
    return {
        "event_id": 12345,
        "tournament_id": 17,
        "tournament_name": "Premier League",
        "kickoff_utc": datetime(2024, 2, 17, 15, 0, tzinfo=timezone.utc),
        "captured_at_utc": datetime(2024, 2, 17, 14, 0, tzinfo=timezone.utc),
        "home_team": {
            "id": 1,
            "name": "Arsenal"
        },
        "away_team": {
            "id": 2,
            "name": "Liverpool"
        },
        "home": {
            "formation": "4-3-3",
            "coach": {"name": "Mikel Arteta"},
            "players": [
                {
                    "player": {
                        "id": 101,
                        "name": "David Raya",
                        "position": "G"
                    },
                    "shirtNumber": 1,
                    "substitute": False,
                    "captain": False
                },
                {
                    "player": {
                        "id": 102,
                        "name": "Aaron Ramsdale",
                        "position": "G"
                    },
                    "shirtNumber": 23,
                    "substitute": True,
                    "captain": False
                }
            ]
        },
        "away": {
            "formation": "4-3-3",
            "coach": {"name": "Jurgen Klopp"},
            "players": [
                {
                    "player": {
                        "id": 201,
                        "name": "Alisson",
                        "position": "G"
                    },
                    "shirtNumber": 1,
                    "substitute": False,
                    "captain": True
                },
                {
                    "player": {
                        "id": 202,
                        "name": "Caoimhin Kelleher",
                        "position": "G"
                    },
                    "shirtNumber": 62,
                    "substitute": True,
                    "captain": False
                }
            ]
        }
    }

def test_normalize_lineup_data(sample_lineup_data):
    """Test lineup data normalization."""
    records = normalize_lineup_data(sample_lineup_data)
    
    assert len(records) == 4  # 2 players per team
    
    # Check home team starter
    raya = next(r for r in records if r.player_id == 101)
    assert raya.team_name == "Arsenal"
    assert raya.side == "home"
    assert raya.formation == "4-3-3"
    assert raya.coach_name == "Mikel Arteta"
    assert raya.shirt_number == 1
    assert raya.position == "G"
    assert not raya.is_sub
    assert not raya.is_captain
    
    # Check away team starter
    alisson = next(r for r in records if r.player_id == 201)
    assert alisson.team_name == "Liverpool"
    assert alisson.side == "away"
    assert alisson.formation == "4-3-3"
    assert alisson.coach_name == "Jurgen Klopp"
    assert alisson.shirt_number == 1
    assert alisson.position == "G"
    assert not alisson.is_sub
    assert alisson.is_captain
    
    # Check substitutes
    ramsdale = next(r for r in records if r.player_id == 102)
    assert ramsdale.is_sub
    kelleher = next(r for r in records if r.player_id == 202)
    assert kelleher.is_sub

def test_normalize_lineup_data_missing_fields(sample_lineup_data):
    """Test handling of missing optional fields."""
    # Remove optional fields
    del sample_lineup_data["home"]["formation"]
    del sample_lineup_data["home"]["coach"]
    del sample_lineup_data["home"]["players"][0]["shirtNumber"]
    del sample_lineup_data["home"]["players"][0]["player"]["position"]
    
    records = normalize_lineup_data(sample_lineup_data)
    
    raya = next(r for r in records if r.player_id == 101)
    assert raya.formation is None
    assert raya.coach_name is None
    assert raya.shirt_number is None
    assert raya.position is None
