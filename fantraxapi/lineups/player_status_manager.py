#!/usr/bin/env python3
"""
Manages player lineup status data from SofaScore lineups, mapping to Fantrax players.
Maintains a CSV with current player statuses that can be used to trigger lineup changes.

The status CSV contains:
- fantrax_player_id: ID from Fantrax system
- fantrax_player_name: Name in Fantrax system
- sofascore_player_id: ID from SofaScore system (for reliable tracking)
- sofascore_player_name: Name in SofaScore system
- lineup_status: starting/reserve/out/other
- lineup_confirmed: true/false
- event_id: SofaScore event ID this status is from
- kickoff_utc: When the match starts
- update_utc: When this status was last updated
"""

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Status constants
STATUS_STARTING = "starting"
STATUS_RESERVE = "reserve"
STATUS_OUT = "out"
STATUS_OTHER = "other"

class PlayerStatusManager:
    def __init__(self, 
                 status_csv_path: str = "data/player_status.csv",
                 mapping_path: str = "config/player_mappings.yaml"):
        self.status_csv_path = Path(status_csv_path)
        self.mapping_path = Path(mapping_path)
        self.current_statuses: Dict[str, dict] = {}  # sofascore_player_id -> status_dict
        self._load_current_statuses()
        
    def _load_current_statuses(self):
        """Load existing status data if file exists."""
        if not self.status_csv_path.exists():
            return
        
        with self.status_csv_path.open('r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Use SofaScore ID as key since it's what we get from lineups
                self.current_statuses[row['sofascore_player_id']] = row

    def _save_current_statuses(self):
        """Save all current statuses to CSV."""
        fieldnames = [
            'fantrax_player_id', 'fantrax_player_name',
            'sofascore_player_id', 'sofascore_player_name',
            'lineup_status', 'lineup_confirmed',
            'event_id', 'kickoff_utc', 'update_utc'
        ]
        
        self.status_csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.status_csv_path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for status in self.current_statuses.values():
                writer.writerow(status)

    def update_from_lineup(self, lineup_path: Path, schedule_info: Optional[dict] = None):
        """
        Update player statuses from a SofaScore lineup file.
        
        Args:
            lineup_path: Path to lineup JSON file
            schedule_info: Optional dict with event schedule info (kickoff time etc)
        """
        with lineup_path.open('r', encoding='utf-8') as f:
            lineup_data = json.load(f)
            
        event_id = str(lineup_data['event_id'])
        confirmed = lineup_data['confirmed']
        update_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")
        kickoff_time = schedule_info.get('kickoff_utc', '') if schedule_info else ''
        
        # Process both home and away teams
        for side in ('home', 'away'):
            team_data = lineup_data.get(side, {})
            
            # Process starters
            for player in team_data.get('starters', []):
                self._update_player_status(
                    player, STATUS_STARTING, confirmed,
                    event_id, kickoff_time, update_time
                )
            
            # Process substitutes
            for player in team_data.get('subs', []):
                self._update_player_status(
                    player, STATUS_RESERVE, confirmed,
                    event_id, kickoff_time, update_time
                )
            
            # Process missing players
            for player in team_data.get('missing', []):
                self._update_player_status(
                    player, STATUS_OUT, confirmed,
                    event_id, kickoff_time, update_time
                )
        
        # Save updates to file
        self._save_current_statuses()

    def _update_player_status(self, player: dict, status: str, 
                            confirmed: bool, event_id: str,
                            kickoff_utc: str, update_utc: str):
        """Update status for a single player."""
        player_id = str(player['id'])
        
        # Create or update player status
        self.current_statuses[player_id] = {
            'fantrax_player_id': '',  # TODO: Add mapping logic
            'fantrax_player_name': '', # TODO: Add mapping logic
            'sofascore_player_id': player_id,
            'sofascore_player_name': player['name'],
            'lineup_status': status,
            'lineup_confirmed': str(confirmed).lower(),
            'event_id': event_id,
            'kickoff_utc': kickoff_utc,
            'update_utc': update_utc
        }

def main():
    """Example usage of PlayerStatusManager."""
    # Initialize manager
    manager = PlayerStatusManager()
    
    # Example: Process a specific lineup file
    lineup_path = Path("data/sofascore/lineups/14025088.json")
    
    # Example schedule info (you'd normally get this from the schedule CSV)
    schedule_info = {
        'kickoff_utc': '2025-08-22 19:00:00+0000'
    }
    
    # Update statuses from this lineup
    manager.update_from_lineup(lineup_path, schedule_info)
    
if __name__ == "__main__":
    main()
