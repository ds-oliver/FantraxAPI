"""Utility functions for the Fantrax API."""

from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
	from fantraxapi.fantrax import FantraxAPI


def parse_division_from_team_name(team_name: str) -> Optional[str]:
    """Parse division name from team name patterns like 'BB - TeamName', '1 - TeamName', or '2A - TeamName'.
    
    Handles edge cases like emojis before division: '🏆🏆GG - TeamName'
    
    Args:
        team_name: The team name to parse
        
    Returns:
        The division name if found, None otherwise
    """
    if not team_name or not isinstance(team_name, str):
        return None
        
    # Try to split on " - "
    parts = team_name.split(" - ", 1)
    if len(parts) != 2:
        return None
        
    prefix = parts[0].strip()
    
    # Strip leading non-alphanumeric characters (emojis, symbols, etc.)
    # This handles cases like "🏆🏆GG - TeamName" -> "GG"
    cleaned_prefix = ""
    for char in prefix:
        if char.isalnum():
            cleaned_prefix += char
    
    # If nothing alphanumeric remains, not a valid division
    if not cleaned_prefix:
        return None
    
    # Check if cleaned prefix is a valid division identifier
    # Case 1: 2-3 letter code (e.g. "BB", "PL", "GG")
    if len(cleaned_prefix) in (2, 3) and cleaned_prefix.isalpha():
        return cleaned_prefix
    
    # Case 2: Digit + letter(s) (e.g. "1A", "2A", "3B")
    if len(cleaned_prefix) >= 2 and cleaned_prefix[0].isdigit():
        # Check if it's a valid pattern: starts with digit(s), followed by letter(s)
        digit_part = ""
        alpha_part = ""
        for char in cleaned_prefix:
            if char.isdigit():
                if alpha_part:  # Digit after letter is not a valid pattern
                    break
                digit_part += char
            elif char.isalpha():
                alpha_part += char
            else:
                break
        
        if digit_part and alpha_part and digit_part + alpha_part == cleaned_prefix:
            return cleaned_prefix
    
    # Case 3: Single digit or number (e.g. "1", "2")
    if cleaned_prefix.isdigit():
        return f"Division {cleaned_prefix}"
        
    return None


def group_teams_by_division(
	team_names: List[str],
	fallback_divisions: Optional[Dict[str, List[str]]] = None
) -> Dict[str, List[str]]:
	"""Group team names by their division based on name patterns.
	
	Args:
		team_names: List of team names to group
		fallback_divisions: Optional fallback division mapping if name parsing fails
		
	Returns:
		Dict mapping division names to lists of team names in that division
	"""
	divisions: Dict[str, List[str]] = {}
	unmatched = []
	
	# First try to parse divisions from team names
	for team in team_names:
		div = parse_division_from_team_name(team)
		if div:
			divisions.setdefault(div, []).append(team)
		else:
			unmatched.append(team)
			
	# If we have unmatched teams and a fallback mapping
	if unmatched and fallback_divisions:
		# Create reverse lookup of team name -> division
		reverse_map = {}
		for div, teams in fallback_divisions.items():
			for team_id in teams:
				reverse_map[team_id] = div
				
		# Try to place unmatched teams using fallback
		for team in unmatched:
			if team in reverse_map:
				div = reverse_map[team]
				divisions.setdefault(div, []).append(team)
			else:
				divisions.setdefault("Other", []).append(team)
	else:
		# If no fallback and we have unmatched teams, put them in Other
		if unmatched:
			divisions["Other"] = unmatched
			
	return divisions


def group_teams_by_division_api(api: "FantraxAPI") -> Dict[str, List[str]]:
	"""
	Return mapping of division name -> list of team_ids for a FantraxAPI instance.
	
	Priority:
		1) Parse division from team_name (parse_division_from_team_name)
		2) Fallback to standings metadata (getStandings)
		3) If nothing else, put all teams into 'All Teams'
	
	Args:
		api: FantraxAPI instance with populated teams
		
	Returns:
		Dict mapping division name to list of team_ids
	"""
	# 1) Build base from team name parsing
	name_divisions: Dict[str, List[str]] = {}
	unknown_team_ids: List[str] = []
	
	for team in api.teams:
		div = parse_division_from_team_name(team.name)
		if div:
			name_divisions.setdefault(div, []).append(team.team_id)
		else:
			unknown_team_ids.append(team.team_id)
	
	# If we got meaningful groups from names and no unknowns, we're done
	if name_divisions and not unknown_team_ids:
		return name_divisions
	
	# 2) Try standings-based detection as fallback / supplement
	api_divisions: Dict[str, List[str]] = {}
	
	# Only use API detection for teams not already assigned via name parsing
	already_assigned = set()
	for ids in name_divisions.values():
		already_assigned.update(ids)
	
	if unknown_team_ids:  # Only query API if we have unassigned teams
		try:
			resp = api._request("getStandings")  # Internal call already used elsewhere
		except Exception:
			resp = {}
		
		team_info = resp.get("fantasyTeamInfo", {}) if isinstance(resp, dict) else {}
		
		for team_id, info in team_info.items():
			# Skip teams already assigned via name parsing
			if team_id in already_assigned:
				continue
			
			div_name = None
			if not isinstance(info, dict):
				continue
			
			for key, value in info.items():
				lk = key.lower()
				if any(tok in lk for tok in ["division", "div", "conference", "group"]):
					if isinstance(value, str):
						div_name = value
					elif isinstance(value, dict):
						div_name = (
							value.get("name")
							or value.get("shortName")
							or value.get("caption")
						)
					if div_name:
						break
			
			# Only add to api_divisions if we found a valid division
			if div_name:
				api_divisions.setdefault(div_name, []).append(team_id)
	
	# 3) Merge name-based and api-based where possible
	#    Name-based has priority if conflict.
	final_divisions: Dict[str, List[str]] = {}
	
	# Start with API divisions if any
	for div_name, ids in api_divisions.items():
		final_divisions.setdefault(div_name, [])
		for tid in ids:
			if tid not in final_divisions[div_name]:
				final_divisions[div_name].append(tid)
	
	# Overlay name-based divisions (these win)
	for div_name, ids in name_divisions.items():
		final_divisions.setdefault(div_name, [])
		for tid in ids:
			if tid not in final_divisions[div_name]:
				final_divisions[div_name].append(tid)
	
	# If nothing meaningful, fall back to single group
	if not final_divisions:
		final_divisions = {"All Teams": [t.team_id for t in api.teams]}
	
	return final_divisions
