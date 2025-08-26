"""
Data models for SofaScore API responses.
"""
from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

class Country(BaseModel):
	"""Country information."""
	name: str
	alpha2: str
	alpha3: str

class Player(BaseModel):
	"""Player information from SofaScore lineup."""
	player_id: int = Field(..., alias="id")
	name: str
	short_name: Optional[str] = Field(None, alias="shortName")
	position: Optional[str] = None
	jersey_number: Optional[str] = Field(None, alias="jerseyNumber")
	height: Optional[int] = None
	country: Optional[Country] = None

class PlayerInLineup(BaseModel):
	"""Player with lineup-specific information."""
	player: Player
	shirt_number: Optional[int] = Field(None, alias="shirtNumber")
	position: Optional[str] = None
	is_sub: bool = Field(False, alias="substitute")
	is_captain: bool = Field(False, alias="captain")
	team_id: int

class MissingPlayer(BaseModel):
	"""Player missing from lineup."""
	player: Player
	type: str  # 'missing' or 'doubtful'
	reason: int	 # reason code from API

class TeamColors(BaseModel):
	"""Team color scheme."""
	primary: str
	number: str
	outline: str
	fancy_number: str = Field(..., alias="fancyNumber")

class TeamLineup(BaseModel):
	"""Complete team lineup information."""
	players: List[PlayerInLineup]
	formation: Optional[str] = None
	missing_players: List[MissingPlayer] = Field([], alias="missingPlayers")
	player_color: TeamColors = Field(..., alias="playerColor")
	goalkeeper_color: TeamColors = Field(..., alias="goalkeeperColor")

class Event(BaseModel):
	"""Match event information."""
	event_id: int = Field(..., alias="id")
	tournament_id: int = Field(..., alias="uniqueTournamentId")
	tournament_name: str = Field(..., alias="uniqueTournamentName")
	kickoff_utc: datetime = Field(..., alias="startTimestamp")
	home_team: Dict = Field(..., alias="homeTeam")
	away_team: Dict = Field(..., alias="awayTeam")

class LineupResponse(BaseModel):
	"""Complete lineup response from SofaScore."""
	confirmed: bool
	home: TeamLineup
	away: TeamLineup

class LineupRecord(BaseModel):
	"""Normalized lineup record for storage."""
	# Event metadata
	event_id: int
	tournament_id: int
	tournament_name: str
	kickoff_utc: datetime
	captured_at_utc: datetime
	is_confirmed: bool
	
	# Team metadata
	team_id: int
	team_name: str
	side: str  # 'home' or 'away'
	formation: Optional[str] = None
	
	# Player data
	player_id: int
	player_name: str
	short_name: Optional[str] = None
	shirt_number: Optional[int] = None
	position: Optional[str] = None
	is_sub: bool
	is_captain: bool
	is_missing: bool = False
	missing_reason: Optional[str] = None
	
	# Additional data
	height: Optional[int] = None
	country: Optional[str] = None