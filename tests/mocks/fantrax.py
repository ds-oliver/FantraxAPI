"""
Mock Fantrax client for testing.
"""
from typing import Dict, List, Optional
from datetime import datetime

class MockFantrax:
    """Mock Fantrax client that simulates API behavior."""
    
    def __init__(self, username: str, password: str, league_id: str):
        self.username = username
        self.password = password
        self.league_id = league_id
        self._lineups: Dict[int, Dict] = {}  # team_id -> lineup data
        
    def get_team_lineup(self, team_id: int) -> Dict:
        """Get current lineup for a team."""
        if team_id not in self._lineups:
            # Return default empty lineup
            return {
                "starters": [],
                "subs": []
            }
        return self._lineups[team_id]
        
    async def swap_players(self, player_in_id: str, player_out_id: str):
        """Simulate swapping players in lineup."""
        # In real implementation this would make API calls
        pass
        
    async def move_to_lineup(self, player_id: str):
        """Simulate moving player to starting lineup."""
        # In real implementation this would make API calls
        pass
        
    def set_mock_lineup(self, team_id: int, lineup: Dict):
        """Set mock lineup data for testing."""
        self._lineups[team_id] = lineup
