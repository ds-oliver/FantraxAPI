"""
Live test of SofaScore lineup scraping.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd
import pytest

from fantraxapi.providers.sofascore.discover import get_json, API_BASE, get_scheduled_events
from fantraxapi.providers.sofascore.normalize import normalize_lineup_data, summarize_lineup
from fantraxapi.providers.sofascore.upsert import save_lineups

async def test_discover_and_fetch_future():
    """Test discovering upcoming games and fetching lineups."""
    # Get upcoming Premier League games
    tournament_ids = [17]  # Premier League
    
    async with httpx.AsyncClient() as client:
        # Get today's events and next 3 days
        all_events = []
        for days in range(4):
            future_date = datetime.now(timezone.utc) + timedelta(days=days)
            events = await get_scheduled_events(client, tournament_ids, future_date)
            all_events.extend(events)
        
        if not all_events:
            pytest.skip("No upcoming Premier League games found")
        
        print(f"\nFound {len(all_events)} upcoming events:")
        for e in all_events:
            print(
                f"- {e.kickoff_utc.strftime('%Y-%m-%d %H:%M')} "
                f"{e.home_team['name']} vs {e.away_team['name']} "
                f"(ID: {e.event_id})"
            )
        
        # Try to get lineups for each event
        for event in all_events:
            url = f"{API_BASE}/event/{event.event_id}/lineups"
            try:
                data = await get_json(client, url)
                print(f"\nFetched lineup data for {event.home_team['name']} vs {event.away_team['name']}")
                
                # Add required metadata
                data.update({
                    "event_id": event.event_id,
                    "tournament_id": event.tournament_id,
                    "tournament_name": event.tournament_name,
                    "kickoff_utc": event.kickoff_utc,
                    "home_team": event.home_team,
                    "away_team": event.away_team,
                    "captured_at_utc": datetime.now(timezone.utc)
                })
                
                # Normalize data
                records = normalize_lineup_data(data)
                if not records:
                    print("No lineup data available yet")
                    continue
                
                # Print lineup summary
                print(summarize_lineup(records))
                
                # Save to parquet with confirmation status in filename
                output_dir = Path("data/lineups")
                status = "confirmed" if data["confirmed"] else "preliminary"
                output_dir.mkdir(parents=True, exist_ok=True)
                
                # Use timestamp in filename to track updates
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_file = output_dir / f"lineups_{event.tournament_name.replace(' ', '')}_{event.event_id}_{status}_{timestamp}.parquet"
                
                df = pd.DataFrame([r.model_dump() for r in records])
                df.to_parquet(out_file, index=False)
                print(f"\nSaved to {out_file}")
                
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    print(f"\nNo lineup data yet for {event.home_team['name']} vs {event.away_team['name']}")
                else:
                    raise

if __name__ == "__main__":
    asyncio.run(test_discover_and_fetch_future())