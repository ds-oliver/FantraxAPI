"""
Premier League team data for validation.
"""

PREMIER_LEAGUE_TEAMS_2025_26 = {
    "Arsenal",
    "Aston Villa",
    "Bournemouth",
    "Brentford",
    "Brighton & Hove Albion",
    "Burnley",
    "Chelsea",
    "Crystal Palace",
    "Everton",
    "Fulham",
    "Liverpool",
    "Manchester City",
    "Manchester United",
    "Newcastle United",
    "Nottingham Forest",
    "Tottenham Hotspur",
    "West Ham United",
    "Wolverhampton Wanderers",  # Sometimes shown as just "Wolverhampton"
}

def is_valid_premier_league_team(team_name: str) -> bool:
    """Check if team is a valid Premier League team."""
    if team_name == "Wolverhampton":  # Handle common variation
        team_name = "Wolverhampton Wanderers"
    return team_name in PREMIER_LEAGUE_TEAMS_2025_26
