"""
Framework for testing lineup automation with preliminary lineups.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd

from .models import LineupRecord, LineupStatus
from .normalize import normalize_lineup_data
from ..player_mapping import PlayerMappingManager

class LineupTestResult:
    """Results from a preliminary lineup test run."""
    def __init__(
        self,
        preliminary: LineupRecord,
        confirmed: Optional[LineupRecord] = None
    ):
        self.preliminary = preliminary
        self.confirmed = confirmed
        self.differences: List[Dict] = []
        self.proposed_changes: List[Dict] = []
        self._analyze()
    
    def _analyze(self):
        """Analyze differences between preliminary and confirmed lineups."""
        if not self.confirmed:
            return
            
        # Compare home team
        self._compare_team(self.preliminary.home_team, self.confirmed.home_team)
        
        # Compare away team
        self._compare_team(self.preliminary.away_team, self.confirmed.away_team)
    
    def _compare_team(self, prelim_team, confirmed_team):
        """Compare preliminary and confirmed team lineups."""
        # Map players by ID for easy comparison
        prelim_players = {p.player_id: p for p in prelim_team.players}
        confirmed_players = {p.player_id: p for p in confirmed_team.players}
        
        # Check for changes
        for player_id, prelim_player in prelim_players.items():
            if player_id not in confirmed_players:
                # Player removed
                self.differences.append({
                    "type": "player_removed",
                    "team": prelim_team.team_name,
                    "player_id": player_id,
                    "player_name": prelim_player.player_name
                })
                continue
                
            confirmed_player = confirmed_players[player_id]
            
            # Check for role changes
            if prelim_player.is_sub != confirmed_player.is_sub:
                self.differences.append({
                    "type": "role_change",
                    "team": prelim_team.team_name,
                    "player_id": player_id,
                    "player_name": prelim_player.player_name,
                    "preliminary_role": "sub" if prelim_player.is_sub else "starter",
                    "confirmed_role": "sub" if confirmed_player.is_sub else "starter"
                })
        
        # Check for added players
        for player_id, confirmed_player in confirmed_players.items():
            if player_id not in prelim_players:
                self.differences.append({
                    "type": "player_added",
                    "team": prelim_team.team_name,
                    "player_id": player_id,
                    "player_name": confirmed_player.player_name
                })

class LineupTester:
    """
    Framework for testing lineup automation with preliminary lineups.
    
    This class helps validate the lineup automation workflow by:
    1. Processing preliminary lineups
    2. Comparing to confirmed lineups when available
    3. Tracking accuracy of preliminary predictions
    4. Simulating lineup changes that would be made
    """
    
    def __init__(
        self,
        output_dir: Path,
        player_mapping: Optional[PlayerMappingManager] = None
    ):
        """
        Initialize the tester.
        
        Args:
            output_dir: Directory containing lineup files
            player_mapping: Optional player mapping manager
        """
        self.output_dir = Path(output_dir)
        self.player_mapping = player_mapping
        self.results: List[LineupTestResult] = []
    
    def process_lineup(
        self,
        data: Dict,
        current_time: Optional[datetime] = None
    ) -> LineupTestResult:
        """
        Process a lineup and track results.
        
        Args:
            data: Raw lineup data
            current_time: Optional override for current time
        
        Returns:
            LineupTestResult instance
        """
        # Normalize the lineup
        lineup = normalize_lineup_data(data, self.player_mapping)
        
        # Find existing result or create new one
        result = self._find_or_create_result(lineup)
        
        # Update result based on lineup status
        if lineup.is_preliminary:
            result.preliminary = lineup
        else:
            result.confirmed = lineup
            
        # Add to results if new
        if result not in self.results:
            self.results.append(result)
            
        return result
    
    def _find_or_create_result(self, lineup: LineupRecord) -> LineupTestResult:
        """Find existing result for this event or create new one."""
        for result in self.results:
            if result.preliminary.event_id == lineup.event_id:
                return result
        return LineupTestResult(preliminary=lineup)
    
    def get_accuracy_stats(self) -> Dict:
        """
        Get accuracy statistics for preliminary lineups.
        
        Returns:
            Dict with accuracy statistics
        """
        total = len(self.results)
        with_confirmed = len([r for r in self.results if r.confirmed])
        total_differences = sum(len(r.differences) for r in self.results)
        
        return {
            "total_lineups": total,
            "confirmed_lineups": with_confirmed,
            "total_differences": total_differences,
            "avg_differences": total_differences / with_confirmed if with_confirmed else 0,
            "perfect_matches": len([r for r in self.results if not r.differences])
        }
    
    def save_report(self, output_file: Path):
        """
        Save a detailed report of test results.
        
        Args:
            output_file: Path to save report to
        """
        # Convert results to DataFrame
        records = []
        for result in self.results:
            record = {
                "event_id": result.preliminary.event_id,
                "tournament": result.preliminary.tournament_name,
                "kickoff": result.preliminary.kickoff_utc,
                "home_team": result.preliminary.home_team.team_name,
                "away_team": result.preliminary.away_team.team_name,
                "has_confirmed": bool(result.confirmed),
                "num_differences": len(result.differences),
                "differences": result.differences
            }
            records.append(record)
            
        df = pd.DataFrame(records)
        df.to_parquet(output_file)
