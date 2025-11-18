"""Trade partner analysis based on roster composition."""

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.objs import RosterRow

PositionKey = Literal["GK", "D", "M", "F"]


@dataclass
class TeamPositionProfile:
	"""Position composition profile for a fantasy team."""
	league_id: str
	team_id: str
	team_name: str
	division: str
	position_counts: Dict[PositionKey, int] = field(default_factory=dict)
	total_players: int = 0
	faab_budget: float = 0.0
	faab_tradeable: bool = False
	
	# These will be filled later
	surplus_positions: List[PositionKey] = field(default_factory=list)
	need_positions: List[PositionKey] = field(default_factory=list)


@dataclass
class TradeMatchSuggestion:
	"""A suggested trade partner with complementary roster needs."""
	partner_team_id: str
	partner_team_name: str
	score: int
	your_surplus_their_need: List[PositionKey]
	their_surplus_your_need: List[PositionKey]


def _extract_primary_position(row: RosterRow) -> Optional[PositionKey]:
	"""
	Map a roster row to a primary fantasy position: GK/D/M/F.
	
	For reserve/bench players, extracts their actual position from the raw player data
	rather than just seeing "Res".
	
	Args:
		row: RosterRow from roster_info
		
	Returns:
		One of 'GK', 'D', 'M', 'F' or None if unclassified
	"""
	position_code = None
	
	# First, try to get actual position from raw scorer data (works for both starters and reserves)
	if hasattr(row, "_raw") and row._raw:
		scorer = row._raw.get("scorer", {})
		if scorer:
			pos_short_names = scorer.get("posShortNames")
			if isinstance(pos_short_names, list) and pos_short_names:
				position_code = pos_short_names[0]
			elif isinstance(pos_short_names, str) and pos_short_names:
				position_code = pos_short_names
	
	# Fallback to row.pos.short_name if we didn't find it in raw data
	if not position_code and row.pos and row.pos.short_name:
		position_code = row.pos.short_name
	
	if not position_code:
		return None
	
	code = position_code.upper().strip()
	
	# Handle common position codes
	if code in ("GK", "G"):
		return "GK"
	if code.startswith("D"):
		return "D"
	if code.startswith("M"):
		return "M"
	if code.startswith("F"):
		return "F"
	
	# If we still have "RES" or "BN" here, it means we couldn't find actual position
	if code in ("RES", "BN", "BENCH"):
		return None
	
	return None


def build_division_profiles(
	api: FantraxAPI,
	league_id: str,
	division_name: str,
	division_team_ids: List[str],
	faab_budgets: Optional[Dict[str, Dict]] = None,
) -> Dict[str, TeamPositionProfile]:
	"""
	Build position composition profiles for all teams in a given division.
	
	Args:
		api: FantraxAPI instance
		league_id: League ID
		division_name: Name of the division
		division_team_ids: List of team IDs in this division
		faab_budgets: Optional dict of team_id -> FAAB budget info from api.league.faab_budgets()
		
	Returns:
		Dict mapping team_id to TeamPositionProfile
	"""
	profiles: Dict[str, TeamPositionProfile] = {}
	faab_budgets = faab_budgets or {}
	
	# Initialize profiles
	for team in api.teams:
		if team.team_id not in division_team_ids:
			continue
		
		# Get FAAB info for this team
		faab_info = faab_budgets.get(team.team_id, {})
		faab_budget = faab_info.get("value", 0.0)
		faab_tradeable = faab_info.get("tradeable", False)
		
		profiles[team.team_id] = TeamPositionProfile(
			league_id=league_id,
			team_id=team.team_id,
			team_name=team.name,
			division=division_name,
			position_counts={"GK": 0, "D": 0, "M": 0, "F": 0},
			total_players=0,
			faab_budget=faab_budget,
			faab_tradeable=faab_tradeable,
		)
	
	# Populate counts from roster_info
	for team_id in division_team_ids:
		profile = profiles.get(team_id)
		if not profile:
			continue
		
		try:
			roster = api.roster_info(team_id)
		except Exception:
			# Skip teams that fail to load
			continue
		
		for row in roster.rows:
			if not row.player:
				continue
			
			primary = _extract_primary_position(row)
			if primary is None:
				continue
			
			profile.position_counts[primary] += 1
			profile.total_players += 1
	
	return profiles


def compute_surplus_and_needs(
	profiles: Dict[str, TeamPositionProfile],
	min_delta: int = 1,
) -> None:
	"""
	Mutates each TeamPositionProfile in-place to set surplus and need positions.
	
	Strategy:
		- Compute division-average count per position.
		- A team is:
			surplus if count >= avg + min_delta
			need    if count <= avg - min_delta
	
	Args:
		profiles: Dict of team profiles to analyze
		min_delta: Minimum difference from average to be considered surplus/need
	"""
	if not profiles:
		return
	
	# 1) Compute averages
	sums: Dict[PositionKey, int] = {"GK": 0, "D": 0, "M": 0, "F": 0}
	n = len(profiles)
	
	for p in profiles.values():
		for pos in sums.keys():
			sums[pos] += p.position_counts.get(pos, 0)
	
	avgs: Dict[PositionKey, float] = {pos: sums[pos] / n for pos in sums.keys()}
	
	# 2) Classify each team
	for p in profiles.values():
		p.surplus_positions = []
		p.need_positions = []
		
		for pos in ["GK", "D", "M", "F"]:
			count = p.position_counts.get(pos, 0)
			avg = avgs[pos]
			
			if count >= avg + min_delta:
				p.surplus_positions.append(pos)
			elif count <= avg - min_delta:
				p.need_positions.append(pos)


def compute_trade_matches(
	profiles: Dict[str, TeamPositionProfile],
	your_team_id: str,
) -> List[TradeMatchSuggestion]:
	"""
	Compute ranked trade partner suggestions for your_team_id, based on
	complementary surpluses/needs.
	
	Scoring:
		+1 for each position where:
			- you have surplus & they have need
			- they have surplus & you have need
	
	Args:
		profiles: Dict of team profiles
		your_team_id: Your team ID
		
	Returns:
		List of TradeMatchSuggestion sorted by score (best first)
	"""
	your_profile = profiles.get(your_team_id)
	if not your_profile:
		return []
	
	your_surplus = set(your_profile.surplus_positions)
	your_need = set(your_profile.need_positions)
	
	suggestions: List[TradeMatchSuggestion] = []
	
	for team_id, profile in profiles.items():
		if team_id == your_team_id:
			continue
		
		their_surplus = set(profile.surplus_positions)
		their_need = set(profile.need_positions)
		
		# Positions where you give something you have in surplus to fill their need
		ys_tn = sorted(list(your_surplus & their_need))
		# Positions where they give something they have in surplus to fill your need
		ts_yn = sorted(list(their_surplus & your_need))
		
		score = len(ys_tn) + len(ts_yn)
		if score <= 0:
			continue  # Not a good trade match
		
		suggestions.append(
			TradeMatchSuggestion(
				partner_team_id=profile.team_id,
				partner_team_name=profile.team_name,
				score=score,
				your_surplus_their_need=ys_tn,
				their_surplus_your_need=ts_yn,
			)
		)
	
	# sort best first
	suggestions.sort(key=lambda s: s.score, reverse=True)
	return suggestions

