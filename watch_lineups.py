#!/usr/bin/env python
"""
CLI script to watch SofaScore lineups and trigger Fantrax actions.
"""
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

import pandas as pd

from fantraxapi.lineups.sofascore_watch import watch_lineups
from fantraxapi.lineups.sofascore_normalize import normalize_lineup_data

def parse_args():
    parser = argparse.ArgumentParser(description="Watch SofaScore lineups")
    parser.add_argument(
        "--window",
        type=int,
        default=90,
        help="Minutes before kickoff to start polling (default: 90)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Seconds between polls (default: 60)"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/lineups",
        help="Directory to save lineup data (default: data/lineups)"
    )
    return parser.parse_args()

async def main():
    args = parse_args()
    
    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    while True:
        try:
            # Watch for lineups
            results = await watch_lineups(
                window_minutes=args.window,
                poll_interval=args.interval
            )
            
            if results:
                print(f"\nProcessing {len(results)} lineup sets...")
                
                # Process each result
                for data in results:
                    # Normalize data
                    records = normalize_lineup_data(data)
                    
                    # Convert to DataFrame
                    df = pd.DataFrame([r.model_dump() for r in records])
                    
                    # Save to parquet
                    event_id = data["event_id"]
                    league = data["league_name"].replace(" ", "")
                    out_file = output_dir / f"lineups_{league}_{event_id}.parquet"
                    
                    df.sort_values([
                        "side",
                        "is_sub",
                        "shirt_number"
                    ]).to_parquet(out_file, index=False)
                    
                    print(f"Saved lineups to {out_file}")
                    
                    # TODO: Add Fantrax integration here
                    # from fantraxapi.integrations.sofascore_to_fantrax import update_lineups
                    # await update_lineups(records)
            
            # Wait before next check
            await asyncio.sleep(args.interval)
            
        except KeyboardInterrupt:
            print("\nStopping lineup watcher...")
            break
            
        except Exception as e:
            print(f"Error in main loop: {e}")
            await asyncio.sleep(args.interval)

if __name__ == "__main__":
    asyncio.run(main())
