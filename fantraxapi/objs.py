"""
Object models for Fantrax API responses.
"""
from typing import Optional, Dict, List, Any, Union
import re

class Player:
	"""Player information from Fantrax."""
	
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.id = data.get("id", "")  # Fantrax ID
		self.name = data.get("name", "")  # Full name
		self.first_name = data.get("firstName", "")
		self.last_name = data.get("lastName", "")
		self.team = data.get("proTeamAbbr", "")	 # Pro team abbreviation
		self.position = data.get("position", "")  # Primary position
		self.positions = data.get("eligiblePositions", [])	# All eligible positions
		self.status = data.get("status", "")  # Player status (e.g., Active, Injured)
		self.injury_status = data.get("injuryStatus", "")  # Detailed injury status
		
	def __str__(self) -> str:
		return f"{self.name} ({self.team} - {self.position})"
		
	def __repr__(self) -> str:
		return f"<Player {self.id}: {self.name}>"

class Position:
	"""Position information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.id = data.get("id", "")
		self.name = data.get("name", "")
		self.short_name = data.get("shortName", "")

class Team:
	"""Team information."""
	def __init__(self, api, team_id: str, name: str, short_name: str = "", logo_url: str = ""):
		self.api = api
		self.team_id = team_id
		self.name = name
		self.short_name = short_name
		self.logo_url = logo_url

class Roster:
	"""Team roster information."""
	def __init__(self, api, data: Dict[str, Any], team_id: str):
		self.api = api
		self.team_id = team_id
		self.data = data
		# Link the team object if available
		try:
			self.team = self.api.get_team_by_id(team_id)
		except Exception:
			self.team = None

		# Parse rows from response tables
		self.rows: List[RosterRow] = []
		tables = data.get("tables") or []
		for table in tables:
			for row in table.get("rows", []) or []:
				try:
					self.rows.append(RosterRow(self.api, row))
				except Exception:
					# Be tolerant to unexpected shapes; skip invalid row
					continue

	def get_starters(self) -> List["RosterRow"]:
		"""Return rows for starting lineup (non-bench)."""
		return [r for r in self.rows if r.pos_id != "0" and r.player is not None]

	def get_bench_players(self) -> List["RosterRow"]:
		"""Return rows for bench players (pos_id == '0')."""
		return [r for r in self.rows if r.pos_id == "0" and r.player is not None]

	def get_player_by_name(self, name: str) -> Optional["RosterRow"]:
		"""Find a roster row by player name (case-insensitive contains)."""
		if not name:
			return None
		q = name.strip().lower()
		for r in self.rows:
			if r.player and q in (r.player.name or "").lower():
				return r
		return None


class RosterPosition:
	"""Lightweight wrapper for a roster position slot."""
	def __init__(self, short_name: Optional[str]):
		self.short_name = short_name or ""


class RosterPlayer:
	"""Lightweight wrapper for a roster player as returned by getTeamRosterInfo."""
	def __init__(self, data: Dict[str, Any]):
		# Data is row['scorer'] structure
		self.id: Optional[str] = (
			data.get("scorerId") or data.get("playerId") or data.get("id")
		)
		self.name: str = data.get("name", "")
		self.first_name: Optional[str] = data.get("firstName")
		self.last_name: Optional[str] = data.get("lastName")
		self.team_name: Optional[str] = data.get("teamName")
		self.team_short_name: Optional[str] = data.get("teamShortName")
		# Support alternative attributes that other code may reference
		if not hasattr(self, "team_short"):
			setattr(self, "team_short", self.team_short_name)


class RosterRow:
	"""Represents a single roster slot row with associated player and position info."""
	def __init__(self, api, row_data: Dict[str, Any]):
		self.api = api
		self._raw = row_data
		# Ensure pos_id preserves numeric 0 (bench) by converting to string explicitly
		raw_pos_id = row_data.get("posId")
		self.pos_id: str = str(raw_pos_id) if raw_pos_id is not None else ""
		self.status_id: Optional[str] = (
			str(row_data.get("statusId")) if row_data.get("statusId") is not None else None
		)
		scorer = row_data.get("scorer") or {}
		self.player: Optional[RosterPlayer] = RosterPlayer(scorer) if scorer else None

		# Normalize starters vs bench based on statusId where possible:
		# statusId == "1" => starter; otherwise treat as bench/reserve
		if self.status_id is not None and self.status_id != "1":
			self.pos_id = "0"

		# Determine position short name for display
		short_name: Optional[str] = None
		if self.pos_id == "0":
			short_name = "Res"
		else:
			# Use scorer.posShortNames if available; try to pick matching index if possible
			pos_short_val = scorer.get("posShortNames") if isinstance(scorer, dict) else None
			if isinstance(pos_short_val, list) and pos_short_val:
				short_name = pos_short_val[0]
			elif isinstance(pos_short_val, str) and pos_short_val:
				short_name = pos_short_val
		self.pos = RosterPosition(short_name)

		# Attempt to parse FPPG metric from cells if present
		self.fppg: Optional[float] = self._extract_fppg(row_data.get("cells") or [])

	def _extract_fppg(self, cells: List[Any]) -> Optional[float]:
		# Heuristics: look for a tooltip mentioning FPPG or content including FPPG, else first numeric cell
		try:
			# Search by tooltip
			for cell in cells:
				if isinstance(cell, dict):
					tt = (cell.get("toolTip") or cell.get("tooltip") or "").lower()
					if "fppg" in tt or "fantasy points per game" in tt:
						val = self._parse_float(cell.get("content"))
						if val is not None:
							return val
			# Fallback: first numeric-looking content
			for cell in cells:
				if isinstance(cell, dict):
					val = self._parse_float(cell.get("content"))
					if val is not None:
						return val
		except Exception:
			return None
		return None

	@staticmethod
	def _parse_float(value: Any) -> Optional[float]:
		if value is None:
			return None
		if isinstance(value, (int, float)):
			return float(value)
		if isinstance(value, str):
			# Extract first float-like number
			m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
			if m:
				try:
					return float(m.group(0))
				except ValueError:
					return None
		return None

class Trade:
	"""Trade information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class TradeBlock:
	"""Trade block information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class Transaction:
	"""Transaction information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class ScoringPeriod:
	"""Scoring period information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		self.week = int(data.get("caption", "0").split()[-1])
		
	def add_matchups(self, data: Dict[str, Any]):
		"""Add matchup data to the scoring period."""
		pass
		
class Standings:
	"""League standings information."""
	def __init__(self, api, data: Dict[str, Any], week: Optional[Union[int, str]] = None):
		self.api = api
		self.data = data
		self.week = week
		
class Record:
	"""Team record information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class Matchup:
	"""Matchup information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class DraftPick:
	"""Draft pick information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data
		
class TradePlayer:
	"""Trade player information."""
	def __init__(self, api, data: Dict[str, Any]):
		self.api = api
		self.data = data