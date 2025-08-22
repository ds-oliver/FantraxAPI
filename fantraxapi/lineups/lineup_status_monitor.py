#!/usr/bin/env python3
"""
Monitors SofaScore schedules and lineups, updating player statuses and triggering lineup changes.

Flow:
1. Read upcoming schedule
2. For each match:
   - Check if we're within the lineup check window
   - Fetch/update lineup data
   - Update player statuses
   - Trigger lineup changes if status changes are detected
"""

import csv
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .player_status_manager import PlayerStatusManager

class LineupStatusMonitor:
    def __init__(self,
                 schedule_path: str = "data/sofascore/schedules/17_76986_upcoming.csv",
                 lineups_dir: str = "data/sofascore/lineups",
                 check_window_hours: int = 3):  # Start checking 3 hours before kickoff
        self.schedule_path = Path(schedule_path)
        self.lineups_dir = Path(lineups_dir)
        self.check_window = timedelta(hours=check_window_hours)
        self.status_manager = PlayerStatusManager()
        
    def _load_schedule(self) -> List[dict]:
        """Load upcoming matches from schedule CSV."""
        if not self.schedule_path.exists():
            return []
            
        matches = []
        with self.schedule_path.open('r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                matches.append(row)
        return matches
        
    def _parse_kickoff_time(self, kickoff_str: str) -> datetime:
        """Parse kickoff time string to datetime."""
        return datetime.strptime(kickoff_str, "%Y-%m-%d %H:%M:%S%z")
        
    def _should_check_lineup(self, kickoff_time: datetime) -> bool:
        """Determine if we should check lineup based on kickoff time."""
        now = datetime.now(timezone.utc)
        time_until_kickoff = kickoff_time - now
        return timedelta(0) <= time_until_kickoff <= self.check_window
        
    def check_upcoming_matches(self):
        """Check upcoming matches and update lineups/statuses as needed."""
        matches = self._load_schedule()
        
        for match in matches:
            try:
                kickoff_time = self._parse_kickoff_time(match['kickoff_utc'])
                event_id = match['event_id']
                
                if not self._should_check_lineup(kickoff_time):
                    continue
                    
                # Check if lineup file exists
                lineup_path = self.lineups_dir / f"{event_id}.json"
                if not lineup_path.exists():
                    print(f"No lineup file for event {event_id}")
                    continue
                
                # Update player statuses from this lineup
                self.status_manager.update_from_lineup(lineup_path, match)
                print(f"Updated player statuses for event {event_id}")
                
            except Exception as e:
                print(f"Error processing match {match.get('event_id')}: {e}")
                continue

def main():
    """Run the lineup status monitor."""
    monitor = LineupStatusMonitor()
    
    # In a real deployment, this would run continuously
    # For now, just do one check
    monitor.check_upcoming_matches()
    
if __name__ == "__main__":
    main()
