#!/usr/bin/env python3
"""
Simplified script to fetch Premier League matches and lineups using ESD.
Stores both schedule and lineup data in organized directories.

Outputs (default out dir: data/premier_league):
  - schedules/schedule_{mode}_{timestamp}.json  # Contains all match data
  - lineups/{event_id}.json                     # Contains lineup data per match
  - lineups_index.csv                           # Tracks all saved lineups

Examples:
  # Get upcoming matches and their lineups
  python esd_export_schedule_and_lineups_v3.py --upcoming --with-lineups

  # Get recent matches only
  python esd_export_schedule_and_lineups_v3.py --recent
"""
import argparse
import json
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

import esd

# Browser and API constants
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}

class PremierLeagueData:
    def __init__(self, 
                 output_dir: Path = Path("data/premier_league"),
                 browser_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        # Initialize ESD client with proper browser settings
        self.client = esd.SofascoreClient(
            browser_path=browser_path,
            headers=HEADERS,
            timeout=30
        )
        
        self.output_dir = output_dir
        self.schedules_dir = output_dir / "schedules"
        self.lineups_dir = output_dir / "lineups"
        self.index_file = output_dir / "lineups_index.csv"
        
        # Create directories
        for d in [self.schedules_dir, self.lineups_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def get_current_season(self) -> int:
        """Get current Premier League season ID."""
        seasons = self.client.get_tournament_seasons(17)  # Premier League
        current = [s for s in seasons if getattr(s, "current", False)]
        if current:
            return getattr(current[0], "id")
        # Get most recent season
        seasons_sorted = sorted(seasons, key=lambda x: int(getattr(x, "id")), reverse=True)
        return getattr(seasons_sorted[0], "id")

    def fetch_matches(self, upcoming: bool = True, limit: Optional[int] = None) -> List[Dict]:
        """Fetch upcoming or recent matches."""
        season_id = self.get_current_season()
        matches = []
        page = 0
        
        while True:
            events = self.client.get_tournament_events(
                17,  # Premier League
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
                    "round": getattr(event, "round", None),
                }
                matches.append(match_data)
                
                if limit and len(matches) >= limit:
                    break
            
            if limit and len(matches) >= limit:
                break
            page += 1
            
        # Store matches
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")
        mode = "upcoming" if upcoming else "recent"
        matches_file = self.schedules_dir / f"schedule_{mode}_{timestamp}.json"
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
    ap = argparse.ArgumentParser(description="Export Premier League schedule and lineups using EasySoccerData.")
    ap.add_argument("--upcoming", action="store_true", help="Get upcoming matches (default: recent matches)")
    ap.add_argument("--with-lineups", action="store_true", help="Also fetch & save lineups")
    ap.add_argument("--output-dir", type=Path, default=Path("data/premier_league"), help="Base output directory")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of matches to fetch")
    ap.add_argument("--browser-path", type=str, 
                   default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   help="Path to Chrome/Chromium browser executable")
    args = ap.parse_args()

    pl = PremierLeagueData(
        output_dir=args.output_dir,
        browser_path=args.browser_path
    )
    
    # Fetch matches
    matches = pl.fetch_matches(upcoming=args.upcoming, limit=args.limit)
    
    # Fetch lineups if requested
    if args.with_lineups and matches:
        print(f"\nFetching lineups for {len(matches)} matches...")
        for match in matches:
            pl.fetch_lineup(match["event_id"])

if __name__ == "__main__":
    main()