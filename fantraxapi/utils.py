"""Utility functions for the Fantrax API."""

from typing import Dict, List, Optional, Tuple


def parse_division_from_team_name(team_name: str) -> Optional[str]:
    """Parse division name from team name patterns like 'BB - TeamName' or '1 - TeamName'.
    
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
    
    # Check if prefix is a valid division identifier
    # Case 1: 2-3 letter code (e.g. "BB", "PL")
    if len(prefix) in (2, 3) and prefix.isalpha():
        return prefix
        
    # Case 2: Single digit or number (e.g. "1", "2")
    if prefix.isdigit():
        return f"Division {prefix}"
        
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
