"""
Normalize SofaScore lineup data into tidy records.
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from fantraxapi.player_mapping import PlayerMappingManager

class Player(BaseModel):
	"""Player information from SofaScore lineup."""
	player_id: int = Field(..., alias="id")
	name: str
	shirt_number: Optional[int] = Field(None, alias="shirtNumber")
	position: Optional[str] = None
	is_sub: bool = Field(False, alias="substitute")
	is_captain: bool = Field(False, alias="captain")

class TeamLineup(BaseModel):
	"""Team lineup information."""
	team_id: int
	team_name: str
	side: str  # 'home' or 'away'
	formation: Optional[str] = None
	coach_name: Optional[str] = None
	players: List[Player]

class LineupRecord(BaseModel):
	"""Normalized lineup record for storage."""
	# Event metadata
	event_id: int
	league_name: str
	league_country: str
	kickoff_utc: datetime
	captured_at_utc: datetime
	
	# Team metadata
	team_id: int
	team_name: str
	side: str
	formation: Optional[str] = None
	coach_name: Optional[str] = None
	
	# Player data
	player_id: int	# SofaScore player ID
	player_name: str  # SofaScore player name
	fantrax_id: Optional[str] = None  # Mapped Fantrax ID
	fantrax_name: Optional[str] = None	# Mapped Fantrax name
	shirt_number: Optional[int] = None
	position: Optional[str] = None
	is_sub: bool
	is_captain: bool

def normalize_lineup_data(data: Dict, player_mapping: Optional[PlayerMappingManager] = None) -> List[LineupRecord]:
	"""
	Normalize raw SofaScore lineup data into tidy records.
	
	Args:
		data: Raw lineup data from SofaScore API
			Example: /event/{id}/lineups response with added metadata
			{
				"event_id": 123,
				"league_name": "Premier League",
				"league_country": "England",
				"kickoff_utc": "2024-02-17T15:00:00Z",
				"home_team": {"id": 1, "name": "Arsenal"},
				"away_team": {"id": 2, "name": "Liverpool"},
				"home": {
					"formation": "4-3-3",
					"coach": {"name": "Mikel Arteta"},
					"players": [...]
				},
				"away": {
					"formation": "4-3-3",
					"coach": {"name": "Jurgen Klopp"},
					"players": [...]
				}
			}
		player_mapping: Optional PlayerMappingManager instance for mapping players
			to their Fantrax IDs and names
	
	Returns:
		List of normalized LineupRecord instances
	"""
	# Initialize player mapping if not provided
	if player_mapping is None:
		player_mapping = PlayerMappingManager()
	records = []
	
	# Process each team
	for side in ["home", "away"]:
		# Get team data
		team_data = data[f"{side}_team"]
		lineup_data = data.get(side, {}) or {}
		
		# Create team lineup
		team = TeamLineup(
			team_id=team_data["id"],
			team_name=team_data["name"],
			side=side,
			formation=lineup_data.get("formation"),
			coach_name=(lineup_data.get("coach") or {}).get("name"),
			players=[]
		)
		
		# Process players
		for p in lineup_data.get("players", []) or []:
			player_data = p.get("player", {})
			if not player_data:
				continue
				
			player = Player(
				id=player_data["id"],
				name=player_data["name"],
				shirtNumber=p.get("shirtNumber"),
				position=player_data.get("position"),
				substitute=p.get("substitute", False),
				captain=p.get("captain", False)
			)
			team.players.append(player)
		
		# Create records for each player
		for player in team.players:
			# Try to map player to Fantrax
			mapping = player_mapping.get_by_sofascore_id(player.player_id)
			if not mapping:
				# Try by name if ID mapping not found
				mapping = player_mapping.get_by_name(player.name)
				
			# Create record with optional Fantrax mapping
			records.append(LineupRecord(
				# Event metadata
				event_id=data["event_id"],
				league_name=data["league_name"],
				league_country=data["league_country"],
				kickoff_utc=data["kickoff_utc"],
				captured_at_utc=data["captured_at_utc"],
				
				# Team metadata
				team_id=team.team_id,
				team_name=team.team_name,
				side=team.side,
				formation=team.formation,
				coach_name=team.coach_name,
				
				# Player data
				player_id=player.player_id,
				player_name=player.name,
				fantrax_id=mapping.fantrax_id if mapping else None,
				fantrax_name=mapping.fantrax_name if mapping else None,
				shirt_number=player.shirt_number,
				position=player.position,
				is_sub=player.is_sub,
				is_captain=player.is_captain
			))
	
	return records
