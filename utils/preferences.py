# utils/preferences.py
from dataclasses import dataclass, field, asdict
from typing import List, Dict
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

PREF_DIR = Path("data/preferences")
PREF_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class LeaguePreferences:
	"""Preferences for a specific league's automation."""
	league_id: str
	auto_lineups: bool = False
	auto_waivers: bool = False
	min_minutes_odds: int = 45  # Minimum % odds of playing to auto-sub
	protected_players: List[str] = field(default_factory=list)  # Fantrax playerIds
	bench_priority: Dict[str, int] = field(default_factory=dict)  # playerId -> priority


@dataclass
class UserPreferences:
	"""All preferences for a user."""
	user_id: str
	leagues: Dict[str, LeaguePreferences] = field(default_factory=dict)


def load_user_preferences(user_id: str) -> UserPreferences:
	"""
	Load preferences for a user.
	
	Args:
		user_id: User identifier
		
	Returns:
		UserPreferences object (creates default if not found)
	"""
	PREF_DIR.mkdir(parents=True, exist_ok=True)
	path = PREF_DIR / f"{user_id}.json"
	
	if not path.exists():
		logger.debug(f"No preferences found for user {user_id}, using defaults")
		return UserPreferences(user_id=user_id)
	
	try:
		data = json.loads(path.read_text())
		leagues = {
			lid: LeaguePreferences(**lp)
			for lid, lp in data.get("leagues", {}).items()
		}
		return UserPreferences(user_id=user_id, leagues=leagues)
	except Exception as e:
		logger.error(f"Failed to load preferences for user {user_id}: {e}")
		return UserPreferences(user_id=user_id)


def save_user_preferences(prefs: UserPreferences) -> None:
	"""
	Save user preferences to disk.
	
	Args:
		prefs: UserPreferences object to save
	"""
	PREF_DIR.mkdir(parents=True, exist_ok=True)
	path = PREF_DIR / f"{prefs.user_id}.json"
	
	try:
		data = {
			"user_id": prefs.user_id,
			"leagues": {
				lid: asdict(lp)
				for lid, lp in prefs.leagues.items()
			},
		}
		path.write_text(json.dumps(data, indent=2))
		logger.info(f"Saved preferences for user {prefs.user_id}")
	except Exception as e:
		logger.error(f"Failed to save preferences for user {prefs.user_id}: {e}")


def get_league_preferences(user_id: str, league_id: str) -> LeaguePreferences:
	"""
	Get preferences for a specific league.
	
	Args:
		user_id: User identifier
		league_id: League identifier
		
	Returns:
		LeaguePreferences object (creates default if not found)
	"""
	prefs = load_user_preferences(user_id)
	
	if league_id not in prefs.leagues:
		# Create default preferences for this league
		prefs.leagues[league_id] = LeaguePreferences(league_id=league_id)
		save_user_preferences(prefs)
	
	return prefs.leagues[league_id]


def update_league_preferences(
	user_id: str,
	league_id: str,
	**updates
) -> LeaguePreferences:
	"""
	Update preferences for a specific league.
	
	Args:
		user_id: User identifier
		league_id: League identifier
		**updates: Keyword arguments to update (e.g., auto_lineups=True)
		
	Returns:
		Updated LeaguePreferences object
	"""
	prefs = load_user_preferences(user_id)
	
	if league_id not in prefs.leagues:
		prefs.leagues[league_id] = LeaguePreferences(league_id=league_id)
	
	# Update fields
	league_prefs = prefs.leagues[league_id]
	for key, value in updates.items():
		if hasattr(league_prefs, key):
			setattr(league_prefs, key, value)
		else:
			logger.warning(f"Unknown preference key: {key}")
	
	save_user_preferences(prefs)
	logger.info(f"Updated preferences for user {user_id} league {league_id}")
	
	return league_prefs

