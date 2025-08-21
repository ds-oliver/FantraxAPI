"""
Normalize lineup data from various sources into standardized format.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..player_mapping import PlayerMappingManager
from .models import LineupRecord, LineupStatus, TeamLineup, PlayerRecord
from .status import determine_lineup_status

def create_player_record(
    player_data: Dict,
    player_mapping: Optional[PlayerMappingManager] = None
) -> PlayerRecord:
    """
    Create a PlayerRecord from raw player data.
    
    Args:
        player_data: Raw player data from source
        player_mapping: Optional player mapping manager
    
    Returns:
        PlayerRecord instance
    """
    # Extract base player info
    player = player_data.get("player", {}) if "player" in player_data else player_data
    
    # Create base record
    record = PlayerRecord(
        player_id=player["id"],
        player_name=player["name"],
        short_name=player.get("shortName"),
        shirt_number=player_data.get("shirtNumber"),
        position=player.get("position"),
        is_sub=player_data.get("substitute", False),
        is_captain=player_data.get("captain", False),
        height=player.get("height"),
        country=player.get("country", {}).get("name")
    )
    
    # Add Fantrax mapping if available
    if player_mapping:
        mapping = player_mapping.get_by_sofascore_id(record.player_id)
        if not mapping:
            # Try by name if ID mapping not found
            mapping = player_mapping.get_by_name(record.player_name)
            
        if mapping:
            record.fantrax_id = mapping.fantrax_id
            record.fantrax_name = mapping.fantrax_name
            
    return record

def create_team_lineup(
    team_data: Dict,
    lineup_data: Dict,
    side: str,
    player_mapping: Optional[PlayerMappingManager] = None
) -> TeamLineup:
    """
    Create a TeamLineup from raw team and lineup data.
    
    Args:
        team_data: Raw team metadata
        lineup_data: Raw lineup data for the team
        side: Which side this team is on ('home' or 'away')
        player_mapping: Optional player mapping manager
    
    Returns:
        TeamLineup instance
    """
    # Create base team record
    team = TeamLineup(
        team_id=team_data["id"],
        team_name=team_data["name"],
        side=side,
        formation=lineup_data.get("formation"),
        coach_name=lineup_data.get("coach", {}).get("name")
    )
    
    # Add players
    for player_data in lineup_data.get("players", []) or []:
        if not player_data:
            continue
            
        player = create_player_record(player_data, player_mapping)
        team.players.append(player)
        
    # Add missing players
    for missing in lineup_data.get("missingPlayers", []) or []:
        if not missing:
            continue
            
        player = create_player_record(missing)
        player.is_missing = True
        player.missing_reason = missing.get("reason")
        team.players.append(player)
        
    return team

def normalize_lineup_data(
    data: Dict,
    player_mapping: Optional[PlayerMappingManager] = None
) -> LineupRecord:
    """
    Normalize raw lineup data into standardized format.
    
    Args:
        data: Raw lineup data with metadata
        player_mapping: Optional player mapping manager
    
    Returns:
        Normalized LineupRecord
    """
    # Create home and away team lineups
    home_team = create_team_lineup(
        data["home_team"],
        data["home"],
        "home",
        player_mapping
    )
    away_team = create_team_lineup(
        data["away_team"],
        data["away"],
        "away",
        player_mapping
    )
    
    # Determine status
    current_time = datetime.now(timezone.utc)
    status = determine_lineup_status(
        kickoff_utc=data["kickoff_utc"],
        is_confirmed=data.get("confirmed", False),
        current_time=current_time
    )
    
    # Create full record
    record = LineupRecord(
        # Event metadata
        event_id=data["event_id"],
        tournament_id=data["tournament_id"],
        tournament_name=data["tournament_name"],
        league_name=data.get("league_name", data["tournament_name"]),
        league_country=data.get("league_country", "England"),
        
        # Timing and status
        kickoff_utc=data["kickoff_utc"],
        captured_at_utc=current_time,
        status=status,
        
        # Team data
        home_team=home_team,
        away_team=away_team
    )
    
    return record
