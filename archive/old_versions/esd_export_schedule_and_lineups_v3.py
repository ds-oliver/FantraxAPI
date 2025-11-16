#!/usr/bin/env python3
"""
Simple script to fetch and store Premier League matches and lineups using ESD.
"""
import json
import csv
from datetime import datetime, timezone
from pathlib import Path
import esd
from typing import Optional, Dict, List

class PremierLeagueDataFetcher:
    def __init__(self, data_dir: Path = Path("data/premier_league")):
        self.client = esd.SofascoreClient()
        self.data_dir = data_dir
        self.matches_dir = data_dir / "matches"
        self.lineups_dir = data_dir / "lineups"
        self.index_file = data_dir / "lineups_index.csv"
        
        # Premier League constants
        self.tournament_id = 17
        self.current_season_id = None  # Will be resolved automatically
        
        # Create directories
        self.matches_dir.mkdir(parents=True, exist_ok=True)
        self.lineups_dir.mkdir(parents=True, exist_ok=True)

    def get_current_season_id(self) -> int:
        """Get current Premier League season ID."""
        if not self.current_season_id:
            seasons = self.client.get_tournament_seasons(self.tournament_id)
            current = [s for s in seasons if getattr(s, "current", False)]
            if current:
                self.current_season_id = getattr(current[0], "id")
            else:
                # Get most recent season
                seasons_sorted = sorted(seasons, key=lambda x: int(getattr(x, "id")), reverse=True)
                self.current_season_id = getattr(seasons_sorted[0], "id")
        return self.current_season_id

    def fetch_matches(self, upcoming: bool = True) -> List[Dict]:
        """Fetch upcoming or recent matches."""
        season_id = self.get_current_season_id()
        matches = []
        page = 0
        
        while True:
            events = self.client.get_tournament_events(
                self.tournament_id, 
                season_id,
                upcoming=upcoming,
                page=page
            )
            if not events:
                break
                
            for event in events:
                match_data = {
                    "event_id": event.id,
                    "slug": event.slug,
                    "kickoff_utc": datetime.fromtimestamp(event.start_timestamp, tz=timezone.utc).isoformat(),
                    "home_team": event.home_team.name,
                    "away_team": event.away_team.name,
                    "home_team_id": event.home_team.id,
                    "away_team_id": event.away_team.id,
                    "status": getattr(event, "status", {}).get("code", ""),
                }
                matches.append(match_data)
            page += 1
            
        # Store matches
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        mode = "upcoming" if upcoming else "recent"
        matches_file = self.matches_dir / f"matches_{mode}_{timestamp}.json"
        with matches_file.open("w") as f:
            json.dump(matches, f, indent=2)
            
        print(f"Saved {len(matches)} {mode} matches to {matches_file}")
        return matches

    def fetch_lineup(self, event_id: int) -> Optional[Dict]:
        """Fetch and store lineup for a specific match."""
        try:
            lineups = self.client.get_match_lineups(event_id)
            if not lineups:
                print(f"No lineup data for event {event_id}")
                return None

            # Convert to storable format
            lineup_data = {
                "event_id": event_id,
                "confirmed": lineups.confirmed,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "home": {
                    "formation": lineups.home.formation,
                    "starters": [],
                    "substitutes": []
                },
                "away": {
                    "formation": lineups.away.formation,
                    "starters": [],
                    "substitutes": []
                }
            }

            # Process home team
            for player in lineups.home.players:
                player_data = {
                    "name": player.info.name,
                    "position": player.info.position,
                    "jersey_number": getattr(player.info, "jersey_number", None),
                    "id": player.info.id,
                    "captain": player.captain
                }
                if player.substitute:
                    lineup_data["home"]["substitutes"].append(player_data)
                else:
                    lineup_data["home"]["starters"].append(player_data)

            # Process away team
            for player in lineups.away.players:
                player_data = {
                    "name": player.info.name,
                    "position": player.info.position,
                    "jersey_number": getattr(player.info, "jersey_number", None),
                    "id": player.info.id,
                    "captain": player.captain
                }
                if player.substitute:
                    lineup_data["away"]["substitutes"].append(player_data)
                else:
                    lineup_data["away"]["starters"].append(player_data)

            # Save lineup
            lineup_file = self.lineups_dir / f"lineup_{event_id}.json"
            with lineup_file.open("w") as f:
                json.dump(lineup_data, f, indent=2)

            # Update index
            self._update_lineup_index(lineup_data)
            
            print(f"Saved lineup for event {event_id} (confirmed: {lineups.confirmed})")
            return lineup_data

        except Exception as e:
            print(f"Error fetching lineup for event {event_id}: {e}")
            return None

    def _update_lineup_index(self, lineup_data: Dict):
        """Update the lineup index CSV."""
        index_row = {
            "event_id": lineup_data["event_id"],
            "confirmed": lineup_data["confirmed"],
            "timestamp_utc": lineup_data["timestamp_utc"],
            "home_starters": len(lineup_data["home"]["starters"]),
            "away_starters": len(lineup_data["away"]["starters"]),
            "home_subs": len(lineup_data["home"]["substitutes"]),
            "away_subs": len(lineup_data["away"]["substitutes"])
        }

        # Append to CSV
        file_exists = self.index_file.exists()
        mode = "a" if file_exists else "w"
        with self.index_file.open(mode, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=index_row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(index_row)

def main():
    fetcher = PremierLeagueDataFetcher()
    
    # Fetch upcoming matches
    upcoming_matches = fetcher.fetch_matches(upcoming=True)
    
    # Fetch recent matches
    recent_matches = fetcher.fetch_matches(upcoming=False)
    
    # Example: Fetch lineup for first upcoming match
    if upcoming_matches:
        first_match = upcoming_matches[0]
        fetcher.fetch_lineup(first_match["event_id"])

if __name__ == "__main__":
    main()