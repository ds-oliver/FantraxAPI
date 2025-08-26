"""
Models for lineup data storage and tracking.
"""
from datetime import datetime
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field

class LineupStatus(str, Enum):
	"""Status of a lineup record."""
	PRELIMINARY = "preliminary"	 # Initial lineup before confirmation window
	PENDING_CONFIRMATION = "pending_confirmation"  # Within confirmation window but not confirmed
	CONFIRMED = "confirmed"	 # Confirmed lineup within 1h15m of kickoff
	FINAL = "final"	 # Match has started, lineup is final
	INVALID = "invalid"	 # Lineup was invalidated for some reason

class TeamLineup(BaseModel):
	"""Team lineup data."""
	team_id: int
	team_name: str
	side: str  # 'home' or 'away'
	formation: Optional[str] = None
	coach_name: Optional[str] = None
	players: List["PlayerRecord"] = Field(default_factory=list)

class PlayerRecord(BaseModel):
	"""Individual player record within a lineup."""
	# Source system IDs and names
	player_id: int	# SofaScore player ID
	player_name: str  # SofaScore player name
	short_name: Optional[str] = None
	
	# Fantrax mapping
	fantrax_id: Optional[str] = None
	fantrax_name: Optional[str] = None
	
	# Position and role info
	shirt_number: Optional[int] = None
	position: Optional[str] = None
	is_sub: bool = False
	is_captain: bool = False
	is_missing: bool = False
	missing_reason: Optional[str] = None
	
	# Additional metadata
	height: Optional[int] = None
	country: Optional[str] = None

class LineupRecord(BaseModel):
	"""Complete lineup record with status tracking."""
	# Event metadata
	event_id: int
	tournament_id: int
	tournament_name: str
	league_name: str
	league_country: str
	
	# Timing and status
	kickoff_utc: datetime
	captured_at_utc: datetime
	status: LineupStatus
	status_changed_at_utc: datetime = Field(default_factory=lambda: datetime.now())
	previous_status: Optional[LineupStatus] = None
	
	# Team data
	home_team: TeamLineup
	away_team: TeamLineup
	
	@property
	def is_confirmed(self) -> bool:
		"""Whether this lineup is confirmed."""
		return self.status in (LineupStatus.CONFIRMED, LineupStatus.FINAL)
	
	@property
	def is_preliminary(self) -> bool:
		"""Whether this lineup is preliminary."""
		return self.status in (LineupStatus.PRELIMINARY, LineupStatus.PENDING_CONFIRMATION)
	
	def update_status(self, new_status: LineupStatus) -> None:
		"""
		Update the lineup status.
		
		Args:
			new_status: New status to set
		"""
		if new_status != self.status:
			self.previous_status = self.status
			self.status = new_status
			self.status_changed_at_utc = datetime.now()
	
	class Config:
		"""Pydantic config."""
		json_encoders = {
			datetime: lambda v: v.isoformat()
		}
