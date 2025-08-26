"""
Normalize SofaScore lineup data into tidy records.
"""
from datetime import datetime, timezone
from typing import Dict, List

from .models import LineupResponse, LineupRecord

def normalize_lineup_data(data: Dict) -> List[LineupRecord]:
	"""
	Normalize raw SofaScore lineup data into tidy records.
	
	Args:
		data: Raw lineup data from SofaScore API with added metadata
			{
				"event_id": 123,
				"tournament_id": 17,
				"tournament_name": "Premier League",
				"kickoff_utc": datetime(...),
				"home_team": {"id": 1, "name": "Arsenal"},
				"away_team": {"id": 2, "name": "Liverpool"},
				"captured_at_utc": datetime(...),
				"confirmed": false,
				"home": {
					"formation": "4-3-3",
					"players": [...],
					"missingPlayers": [...]
				},
				"away": {
					"formation": "4-2-3-1",
					"players": [...],
					"missingPlayers": [...]
				}
			}
	
	Returns:
		List of normalized LineupRecord instances
	"""
	records = []
	
	# Process each team
	for side in ["home", "away"]:
		team_data = data[f"{side}_team"]
		lineup = data[side]
		
		# Process active players
		for p in lineup.get("players", []) or []:
			player_data = p.get("player", {})
			if not player_data:
				continue
			
			records.append(LineupRecord(
				# Event metadata
				event_id=data["event_id"],
				tournament_id=data["tournament_id"],
				tournament_name=data["tournament_name"],
				kickoff_utc=data["kickoff_utc"],
				captured_at_utc=data["captured_at_utc"],
				is_confirmed=data.get("confirmed", False),
				
				# Team metadata
				team_id=team_data["id"],
				team_name=team_data["name"],
				side=side,
				formation=lineup.get("formation"),
				
				# Player data
				player_id=player_data["id"],
				player_name=player_data["name"],
				short_name=player_data.get("shortName"),
				shirt_number=p.get("shirtNumber"),
				position=player_data.get("position"),
				is_sub=p.get("substitute", False),
				is_captain=p.get("captain", False),
				is_missing=False,
				
				# Additional data
				height=player_data.get("height"),
				country=player_data.get("country", {}).get("name")
			))
		
		# Process missing players
		for m in lineup.get("missingPlayers", []) or []:
			player_data = m.get("player", {})
			if not player_data:
				continue
			
			records.append(LineupRecord(
				# Event metadata
				event_id=data["event_id"],
				tournament_id=data["tournament_id"],
				tournament_name=data["tournament_name"],
				kickoff_utc=data["kickoff_utc"],
				captured_at_utc=data["captured_at_utc"],
				is_confirmed=data.get("confirmed", False),
				
				# Team metadata
				team_id=team_data["id"],
				team_name=team_data["name"],
				side=side,
				formation=lineup.get("formation"),
				
				# Player data
				player_id=player_data["id"],
				player_name=player_data["name"],
				short_name=player_data.get("shortName"),
				shirt_number=int(player_data["jerseyNumber"]) if player_data.get("jerseyNumber") else None,
				position=player_data.get("position"),
				is_sub=False,  # Missing players aren't in the squad
				is_captain=False,
				is_missing=True,
				missing_reason=m.get("type"),  # 'missing' or 'doubtful'
				
				# Additional data
				height=player_data.get("height"),
				country=player_data.get("country", {}).get("name")
			))
	
	return records

def summarize_lineup(records: List[LineupRecord]) -> str:
	"""Generate a human-readable summary of a lineup."""
	summary = []
	
	# Group by team
	by_team = {}
	for r in records:
		if r.team_name not in by_team:
			by_team[r.team_name] = {
				"formation": r.formation,
				"starters": [],
				"subs": [],
				"missing": []
			}
		
		if r.is_missing:
			by_team[r.team_name]["missing"].append(r)
		elif r.is_sub:
			by_team[r.team_name]["subs"].append(r)
		else:
			by_team[r.team_name]["starters"].append(r)
	
	# Generate summary
	for team, data in by_team.items():
		summary.append(f"\n{team} ({data['formation']})")
		
		summary.append("\nStarting XI:")
		for p in sorted(data["starters"], key=lambda x: (x.position or "", x.shirt_number or 99)):
			captain = " (C)" if p.is_captain else ""
			summary.append(f"- {p.shirt_number or '?'} {p.player_name} ({p.position or '?'}){captain}")
		
		if data["subs"]:
			summary.append("\nSubstitutes:")
			for p in sorted(data["subs"], key=lambda x: (x.position or "", x.shirt_number or 99)):
				summary.append(f"- {p.shirt_number or '?'} {p.player_name} ({p.position or '?'})")
		
		if data["missing"]:
			summary.append("\nUnavailable:")
			for p in sorted(data["missing"], key=lambda x: x.missing_reason):
				reason = " (doubtful)" if p.missing_reason == "doubtful" else ""
				summary.append(f"- {p.player_name}{reason}")
	
	return "\n".join(summary)