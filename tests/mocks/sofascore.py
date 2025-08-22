"""
Mock SofaScore client for testing.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fantraxapi.providers.sofascore.models import Event, LineupResponse, TeamLineup

class MockSofaScore:
    """Mock SofaScore client that simulates API behavior."""
    
    def __init__(self):
        self._events: Dict[int, Dict] = {}
        self._lineups: Dict[int, Dict] = {}
        
    def add_mock_event(
        self,
        event_id: int,
        home_team: str,
        away_team: str,
        kickoff_time: Optional[datetime] = None,
        tournament_id: int = 17,
        tournament_name: str = "Premier League"
    ):
        """Add a mock event."""
        if not kickoff_time:
            kickoff_time = datetime.now(timezone.utc) + timedelta(hours=2)
            
        self._events[event_id] = {
            "id": event_id,
            "tournament": {
                "id": tournament_id,
                "name": tournament_name
            },
            "homeTeam": {
                "id": len(self._events) * 2 + 1,
                "name": home_team
            },
            "awayTeam": {
                "id": len(self._events) * 2 + 2,
                "name": away_team
            },
            "startTimestamp": int(kickoff_time.timestamp())
        }
        
    def add_mock_lineup(
        self,
        event_id: int,
        home_players: List[Dict],
        away_players: List[Dict],
        confirmed: bool = False
    ):
        """Add mock lineup data."""
        self._lineups[event_id] = {
            "confirmed": confirmed,
            "home": {
                "formation": "4-3-3",
                "players": home_players
            },
            "away": {
                "formation": "4-4-2",
                "players": away_players
            }
        }
    
    async def get_scheduled_events(
        self,
        tournament_ids: Optional[List[int]] = None
    ) -> List[Dict]:
        """Get scheduled events."""
        return list(self._events.values())
        
    async def get_event_lineup(self, event_id: int) -> Optional[Dict]:
        """Get lineup for an event."""
        if event_id not in self._lineups:
            return None
            
        lineup = self._lineups[event_id].copy()
        event = self._events[event_id]
        
        # Add metadata
        lineup.update({
            "event_id": event_id,
            "tournament_id": event["tournament"]["id"],
            "tournament_name": event["tournament"]["name"],
            "kickoff_utc": datetime.fromtimestamp(event["startTimestamp"], timezone.utc),
            "home_team": event["homeTeam"],
            "away_team": event["awayTeam"],
            "captured_at_utc": datetime.now(timezone.utc)
        })
        
        return lineup

def create_mock_player(
    player_id: int,
    name: str,
    position: str = "F",
    is_sub: bool = False,
    is_captain: bool = False
) -> Dict:
    """Create mock player data."""
    return {
        "player": {
            "id": player_id,
            "name": name,
            "position": position
        },
        "substitute": is_sub,
        "captain": is_captain
    }
