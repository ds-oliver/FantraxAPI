"""
Persist SofaScore lineup data to Parquet/DB.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

from .models import LineupRecord

def save_lineups(
    records: List[LineupRecord],
    output_dir: Path
) -> None:
    """
    Save lineup records to Parquet files.
    
    Args:
        records: List of normalized lineup records
        output_dir: Directory to save Parquet files
    """
    if not records:
        return
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Convert to DataFrame
    df = pd.DataFrame([r.model_dump() for r in records])
    
    # Save per tournament and event
    for event_id, event_df in df.groupby("event_id"):
        tournament = event_df["tournament_name"].iloc[0].replace(" ", "")
        out_file = output_dir / f"lineups_{tournament}_{event_id}.parquet"
        
        # Sort by team and role
        event_df = event_df.sort_values([
            "side",
            "is_sub",
            "shirt_number"
        ])
        
        # Save to parquet
        event_df.to_parquet(out_file, index=False)
        print(f"Saved lineups to {out_file}")

def load_lineups(
    output_dir: Path,
    tournament_name: Optional[str] = None,
    event_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None
) -> pd.DataFrame:
    """
    Load lineup records from Parquet files.
    
    Args:
        output_dir: Directory containing Parquet files
        tournament_name: Filter by tournament name
        event_id: Filter by event ID
        date_from: Filter by kickoff date from
        date_to: Filter by kickoff date to
    
    Returns:
        DataFrame of lineup records
    """
    # List parquet files
    files = []
    for f in output_dir.glob("*.parquet"):
        if tournament_name and tournament_name.replace(" ", "") not in f.stem:
            continue
        if event_id and f"{event_id}" not in f.stem:
            continue
        files.append(f)
    
    if not files:
        return pd.DataFrame()
    
    # Read and combine
    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        if date_from:
            df = df[df["kickoff_utc"] >= date_from]
        if date_to:
            df = df[df["kickoff_utc"] <= date_to]
        if not df.empty:
            dfs.append(df)
    
    if not dfs:
        return pd.DataFrame()
    
    return pd.concat(dfs, ignore_index=True)

def get_latest_lineups(
    output_dir: Path,
    tournament_name: Optional[str] = None,
    hours_lookback: int = 24
) -> pd.DataFrame:
    """
    Get latest lineups within lookback window.
    
    Args:
        output_dir: Directory containing Parquet files
        tournament_name: Filter by tournament name
        hours_lookback: Hours to look back
    
    Returns:
        DataFrame of latest lineup records
    """
    now = datetime.now()
    date_from = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    
    df = load_lineups(
        output_dir,
        tournament_name=tournament_name,
        date_from=date_from
    )
    
    if df.empty:
        return df
    
    # Get latest version of each lineup
    return df.sort_values("captured_at_utc").groupby([
        "event_id",
        "team_id",
        "player_id"
    ]).last().reset_index()
