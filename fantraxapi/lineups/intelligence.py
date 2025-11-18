"""
Lineup Intelligence Module

Analyzes lineup predictions from SofaScore/FFScout and combines them with Fantrax
roster, waiver, and league data to provide decision-making insights.

This is separate from the automatic sync workflow - it's for planning and analysis.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import logging

import pandas as pd

from ..fantrax import FantraxAPI
from ..objs import Roster, RosterRow
from ..player_mapping import PlayerMappingManager
from .models import LineupRecord, LineupStatus
from .status import determine_lineup_status

logger = logging.getLogger(__name__)


class ActionType(str, Enum):
	"""Type of action recommended."""
	SWAP_TO_ACTIVE = "swap_to_active"  # Move bench player to starting lineup
	SWAP_TO_RESERVE = "swap_to_reserve"  # Move starter to bench
	WAIVER_CLAIM = "waiver_claim"  # Claim player on waivers
	FREE_AGENT_CLAIM = "free_agent_claim"  # Pick up free agent
	DROP_PLAYER = "drop_player"  # Drop player
	NO_ACTION = "no_action"  # No action needed


class PlayerStatus(str, Enum):
	"""Player status in lineup predictions."""
	CONFIRMED_STARTER = "confirmed_starter"
	PREDICTED_STARTER = "predicted_starter"
	CONFIRMED_BENCH = "confirmed_bench"
	PREDICTED_BENCH = "predicted_bench"
	OUT = "out"  # Injured/suspended
	UNKNOWN = "unknown"


@dataclass
class LineupIntelligence:
	"""Intelligence about a player's lineup status."""
	
	# Player identification
	player_name: str
	fantrax_id: Optional[str] = None
	sofascore_id: Optional[int] = None
	team_name: Optional[str] = None
	position: Optional[str] = None
	
	# Lineup prediction
	status: PlayerStatus = PlayerStatus.UNKNOWN
	lineup_status: Optional[LineupStatus] = None  # PRELIMINARY, CONFIRMED, etc.
	confidence: float = 0.0  # 0-1 confidence score
	source: str = "unknown"  # sofascore, ffscout, fantrax
	
	# Match details
	opponent: Optional[str] = None
	kickoff_time: Optional[datetime] = None
	is_home: bool = True
	
	# Fantrax context
	is_on_roster: bool = False
	is_starting: bool = False
	is_on_bench: bool = False
	is_available: bool = False  # On waivers or free agent
	availability_type: Optional[str] = None  # "waiver", "free_agent"
	owned_by_team: Optional[str] = None
	
	# Performance metrics (if available)
	fppg: Optional[float] = None
	last_5_avg: Optional[float] = None
	
	def __post_init__(self):
		"""Calculate confidence based on status and lineup status."""
		if self.confidence == 0.0:
			if self.lineup_status == LineupStatus.CONFIRMED:
				self.confidence = 1.0
			elif self.lineup_status == LineupStatus.FINAL:
				self.confidence = 1.0
			elif self.status == PlayerStatus.CONFIRMED_STARTER:
				self.confidence = 0.95
			elif self.status == PlayerStatus.PREDICTED_STARTER:
				self.confidence = 0.7
			elif self.status == PlayerStatus.CONFIRMED_BENCH:
				self.confidence = 0.9
			elif self.status == PlayerStatus.PREDICTED_BENCH:
				self.confidence = 0.6
			elif self.status == PlayerStatus.OUT:
				self.confidence = 0.95
	
	@property
	def is_likely_starter(self) -> bool:
		"""Is this player likely to start?"""
		return self.status in (PlayerStatus.CONFIRMED_STARTER, PlayerStatus.PREDICTED_STARTER) and self.confidence >= 0.7
	
	@property
	def is_likely_bench(self) -> bool:
		"""Is this player likely to be on the bench?"""
		return self.status in (PlayerStatus.CONFIRMED_BENCH, PlayerStatus.PREDICTED_BENCH) and self.confidence >= 0.6


@dataclass
class ActionRecommendation:
	"""Recommended action based on lineup intelligence."""
	
	action_type: ActionType
	player: LineupIntelligence
	priority: int = 5  # 1=highest, 10=lowest
	reason: str = ""
	
	# For swap actions
	swap_with: Optional[LineupIntelligence] = None
	
	# For claim/drop actions
	estimated_value: Optional[float] = None
	
	# Additional context
	notes: List[str] = field(default_factory=list)
	
	def __str__(self) -> str:
		"""Human-readable representation."""
		if self.action_type == ActionType.SWAP_TO_ACTIVE:
			swap_info = f" (swap with {self.swap_with.player_name})" if self.swap_with else ""
			return f"[P{self.priority}] Move {self.player.player_name} to active lineup{swap_info}: {self.reason}"
		
		elif self.action_type == ActionType.SWAP_TO_RESERVE:
			swap_info = f" (swap with {self.swap_with.player_name})" if self.swap_with else ""
			return f"[P{self.priority}] Move {self.player.player_name} to bench{swap_info}: {self.reason}"
		
		elif self.action_type == ActionType.WAIVER_CLAIM:
			return f"[P{self.priority}] Claim {self.player.player_name} from waivers: {self.reason}"
		
		elif self.action_type == ActionType.FREE_AGENT_CLAIM:
			return f"[P{self.priority}] Pick up {self.player.player_name} (free agent): {self.reason}"
		
		elif self.action_type == ActionType.DROP_PLAYER:
			return f"[P{self.priority}] Consider dropping {self.player.player_name}: {self.reason}"
		
		else:
			return f"[P{self.priority}] {self.player.player_name}: {self.reason}"


class LineupIntelligenceAnalyzer:
	"""
	Analyzes lineup predictions and provides decision-making insights.
	
	Combines:
	- Lineup predictions from SofaScore/FFScout
	- Current roster data
	- Waiver wire availability
	- Other managers' rosters
	"""
	
	def __init__(
		self,
		fantrax_api: FantraxAPI,
		player_mapping: PlayerMappingManager,
		lineup_data_dir: Path = Path("data/lineups")
	):
		"""
		Initialize analyzer.
		
		Args:
			fantrax_api: FantraxAPI instance
			player_mapping: Player mapping manager
			lineup_data_dir: Directory containing lineup data files
		"""
		self.api = fantrax_api
		self.player_mapping = player_mapping
		self.lineup_data_dir = Path(lineup_data_dir)
		
		# Cached data
		self._roster: Optional[Roster] = None
		self._all_players: Optional[List[dict]] = None
		self._lineup_intelligence: Dict[str, LineupIntelligence] = {}
	
	def load_lineup_predictions(
		self,
		source: str = "sofascore",
		from_date: Optional[datetime] = None,
		to_date: Optional[datetime] = None
	) -> List[LineupRecord]:
		"""
		Load lineup predictions from files.
		
		Args:
			source: Data source ("sofascore" or "ffscout")
			from_date: Start date filter
			to_date: End date filter
			
		Returns:
			List of LineupRecord objects
		"""
		records = []
		
		# Find lineup files
		pattern = f"lineups_*_{source}_*.parquet" if source != "sofascore" else "lineups_*.parquet"
		lineup_files = list(self.lineup_data_dir.glob(pattern))
		
		logger.info(f"Found {len(lineup_files)} lineup files")
		
		for file_path in lineup_files:
			try:
				df = pd.read_parquet(file_path)
				
				# Filter by date if specified
				if from_date or to_date:
					if "kickoff_utc" in df.columns:
						df["kickoff_utc"] = pd.to_datetime(df["kickoff_utc"])
						if from_date:
							df = df[df["kickoff_utc"] >= from_date]
						if to_date:
							df = df[df["kickoff_utc"] <= to_date]
				
				# Convert to records
				# (Would need to properly reconstruct LineupRecord objects here)
				logger.info(f"Loaded {len(df)} lineup records from {file_path.name}")
				
			except Exception as e:
				logger.error(f"Error loading {file_path}: {e}")
		
		return records
	
	def build_intelligence(
		self,
		lineup_records: List[LineupRecord],
		team_id: Optional[str] = None
	) -> Dict[str, LineupIntelligence]:
		"""
		Build intelligence map from lineup records.
		
		Args:
			lineup_records: Lineup predictions
			team_id: Fantrax team ID for roster context
			
		Returns:
			Dict mapping player IDs to LineupIntelligence
		"""
		# Get roster if team_id provided
		roster = None
		if team_id:
			roster = self.api.roster_info(team_id)
			self._roster = roster
		
		# Get all available players
		try:
			available_players = self.api.waivers.list_players_by_name(
				limit=1000,
				status="ALL_AVAILABLE"
			)
			available_ids = {p["id"] for p in available_players}
		except Exception as e:
			logger.warning(f"Could not fetch available players: {e}")
			available_ids = set()
		
		intelligence_map = {}
		
		# Process each lineup record
		for record in lineup_records:
			# Determine lineup status
			status = determine_lineup_status(record)
			
			# Create intelligence for each player
			fantrax_id = record.fantrax_id
			if not fantrax_id:
				continue
			
			# Determine player status
			if record.is_sub:
				player_status = (PlayerStatus.CONFIRMED_BENCH if status == LineupStatus.CONFIRMED
								else PlayerStatus.PREDICTED_BENCH)
			else:
				player_status = (PlayerStatus.CONFIRMED_STARTER if status == LineupStatus.CONFIRMED
								else PlayerStatus.PREDICTED_STARTER)
			
			# Check roster status
			is_on_roster = False
			is_starting = False
			is_on_bench = False
			fppg = None
			
			if roster:
				for row in roster.rows:
					if row.player and row.player.id == fantrax_id:
						is_on_roster = True
						is_starting = row.pos_id != "0"
						is_on_bench = row.pos_id == "0"
						fppg = row.fppg
						break
			
			# Create intelligence
			intel = LineupIntelligence(
				player_name=record.player_name,
				fantrax_id=fantrax_id,
				sofascore_id=record.player_id,
				team_name=record.team_name,
				position=record.position,
				status=player_status,
				lineup_status=status,
				source="sofascore",
				opponent=None,  # Would need to extract from record
				kickoff_time=record.kickoff_utc,
				is_on_roster=is_on_roster,
				is_starting=is_starting,
				is_on_bench=is_on_bench,
				is_available=fantrax_id in available_ids,
				fppg=fppg
			)
			
			intelligence_map[fantrax_id] = intel
		
		self._lineup_intelligence = intelligence_map
		return intelligence_map
	
	def generate_recommendations(
		self,
		intelligence_map: Optional[Dict[str, LineupIntelligence]] = None,
		min_confidence: float = 0.7
	) -> List[ActionRecommendation]:
		"""
		Generate action recommendations based on intelligence.
		
		Args:
			intelligence_map: Intelligence data (uses cached if None)
			min_confidence: Minimum confidence threshold
			
		Returns:
			List of ActionRecommendation objects, sorted by priority
		"""
		if intelligence_map is None:
			intelligence_map = self._lineup_intelligence
		
		if not intelligence_map:
			logger.warning("No intelligence data available")
			return []
		
		recommendations = []
		
		# Analyze roster vs predictions
		for fantrax_id, intel in intelligence_map.items():
			if intel.confidence < min_confidence:
				continue
			
			# Case 1: Player on bench but predicted to start
			if intel.is_on_bench and intel.is_likely_starter:
				rec = ActionRecommendation(
					action_type=ActionType.SWAP_TO_ACTIVE,
					player=intel,
					priority=2 if intel.confidence >= 0.9 else 3,
					reason=f"Predicted to start with {intel.confidence:.0%} confidence"
				)
				recommendations.append(rec)
			
			# Case 2: Player starting but predicted on bench/out
			elif intel.is_starting and (intel.is_likely_bench or intel.status == PlayerStatus.OUT):
				rec = ActionRecommendation(
					action_type=ActionType.SWAP_TO_RESERVE,
					player=intel,
					priority=1 if intel.status == PlayerStatus.OUT else 2,
					reason=f"Not expected to start ({intel.status.value})"
				)
				recommendations.append(rec)
			
			# Case 3: Available player predicted to start
			elif intel.is_available and intel.is_likely_starter:
				action_type = (ActionType.FREE_AGENT_CLAIM if intel.availability_type == "free_agent"
							  else ActionType.WAIVER_CLAIM)
				rec = ActionRecommendation(
					action_type=action_type,
					player=intel,
					priority=4,
					reason=f"Available player predicted to start ({intel.confidence:.0%} confidence)"
				)
				recommendations.append(rec)
			
			# Case 4: Rostered player consistently not starting
			elif intel.is_on_roster and intel.status in (PlayerStatus.PREDICTED_BENCH, PlayerStatus.CONFIRMED_BENCH):
				rec = ActionRecommendation(
					action_type=ActionType.DROP_PLAYER,
					player=intel,
					priority=7,
					reason="Consistently on bench"
				)
				recommendations.append(rec)
		
		# Sort by priority
		recommendations.sort(key=lambda r: (r.priority, -r.player.confidence))
		
		return recommendations
	
	def generate_swap_recommendations(
		self,
		intelligence_map: Optional[Dict[str, LineupIntelligence]] = None
	) -> List[ActionRecommendation]:
		"""
		Generate specific swap recommendations (bench ↔ starters).
		
		Args:
			intelligence_map: Intelligence data (uses cached if None)
			
		Returns:
			List of swap ActionRecommendations with swap_with populated
		"""
		if intelligence_map is None:
			intelligence_map = self._lineup_intelligence
		
		recommendations = []
		
		# Find players on bench who should start
		bench_should_start = [
			intel for intel in intelligence_map.values()
			if intel.is_on_bench and intel.is_likely_starter
		]
		
		# Find starters who should be benched
		starters_should_bench = [
			intel for intel in intelligence_map.values()
			if intel.is_starting and (intel.is_likely_bench or intel.status == PlayerStatus.OUT)
		]
		
		# Match them up by position (if possible)
		for bench_player in bench_should_start:
			best_match = None
			best_score = 0
			
			for starter in starters_should_bench:
				# Score the match
				score = 0
				
				# Same position
				if bench_player.position == starter.position:
					score += 10
				
				# Confidence differential
				score += (bench_player.confidence - starter.confidence) * 5
				
				if score > best_score:
					best_score = score
					best_match = starter
			
			rec = ActionRecommendation(
				action_type=ActionType.SWAP_TO_ACTIVE,
				player=bench_player,
				swap_with=best_match,
				priority=2 if best_match else 3,
				reason=f"Swap bench player (starting) with starter (benched/out)"
			)
			recommendations.append(rec)
		
		recommendations.sort(key=lambda r: r.priority)
		return recommendations
	
	def get_matchday_summary(
		self,
		intelligence_map: Optional[Dict[str, LineupIntelligence]] = None,
		matchday: Optional[datetime] = None
	) -> pd.DataFrame:
		"""
		Get summary of lineup intelligence for a matchday.
		
		Args:
			intelligence_map: Intelligence data (uses cached if None)
			matchday: Date to filter (defaults to next 7 days)
			
		Returns:
			DataFrame with summary statistics
		"""
		if intelligence_map is None:
			intelligence_map = self._lineup_intelligence
		
		# Filter by matchday if specified
		if matchday:
			intelligence_map = {
				k: v for k, v in intelligence_map.items()
				if v.kickoff_time and v.kickoff_time.date() == matchday.date()
			}
		
		# Convert to DataFrame
		rows = []
		for intel in intelligence_map.values():
			rows.append({
				"Player": intel.player_name,
				"Team": intel.team_name,
				"Position": intel.position,
				"Status": intel.status.value,
				"Confidence": f"{intel.confidence:.0%}",
				"On Roster": "✓" if intel.is_on_roster else "",
				"Starting": "✓" if intel.is_starting else "",
				"On Bench": "✓" if intel.is_on_bench else "",
				"Available": "✓" if intel.is_available else "",
				"Kickoff": intel.kickoff_time.strftime("%a %H:%M") if intel.kickoff_time else "",
				"FPPG": f"{intel.fppg:.1f}" if intel.fppg else "-"
			})
		
		df = pd.DataFrame(rows)
		return df
	
	def export_recommendations(
		self,
		recommendations: List[ActionRecommendation],
		output_path: Path
	) -> None:
		"""
		Export recommendations to CSV.
		
		Args:
			recommendations: List of recommendations
			output_path: Path to save CSV
		"""
		rows = []
		for rec in recommendations:
			rows.append({
				"Priority": rec.priority,
				"Action": rec.action_type.value,
				"Player": rec.player.player_name,
				"Team": rec.player.team_name,
				"Position": rec.player.position,
				"Status": rec.player.status.value,
				"Confidence": f"{rec.player.confidence:.0%}",
				"Reason": rec.reason,
				"Swap With": rec.swap_with.player_name if rec.swap_with else "",
				"On Roster": "Yes" if rec.player.is_on_roster else "No",
				"Currently Starting": "Yes" if rec.player.is_starting else "No",
				"FPPG": f"{rec.player.fppg:.1f}" if rec.player.fppg else "-"
			})
		
		df = pd.DataFrame(rows)
		df.to_csv(output_path, index=False)
		logger.info(f"Exported {len(recommendations)} recommendations to {output_path}")

