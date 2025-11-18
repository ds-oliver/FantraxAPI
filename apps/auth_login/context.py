"""
Global sidebar context selector for league and team selection.

Provides a single source of truth for which league/team is currently active
across all pages in the app.
"""

from typing import Optional, Tuple, Dict, List
import streamlit as st
from utils.auth_helpers import fetch_user_leagues


def select_league_and_team_in_sidebar(
	session,
	league_state_key: str = "league_id",
	team_state_key: str = "team_id",
) -> Tuple[Optional[str], Optional[str]]:
	"""
	Renders a global league + team selector in the sidebar.
	
	Uses fetch_user_leagues(session) to get the user's leagues and their team in each.
	Since each user has exactly one team per league, we show a combined selector.
	
	Args:
		session: Authenticated requests.Session
		league_state_key: Session state key for league_id (default: "league_id")
		team_state_key: Session state key for team_id (default: "team_id")
	
	Returns:
		(league_id, team_id) tuple, or (None, None) if no leagues or errors occur
	
	Side effects:
		- Updates st.session_state[league_state_key] with selected league_id
		- Updates st.session_state[team_state_key] with selected team_id
		- Updates st.session_state["league_name"] with selected league name
		- Updates st.session_state["team_name"] with selected team name
	"""
	
	st.sidebar.markdown("### League & Team")
	
	# Fetch user's leagues
	try:
		leagues = fetch_user_leagues(session) or []
	except Exception as e:
		st.sidebar.error(f"Error fetching leagues: {e}")
		# Clear stale state
		st.session_state.pop(league_state_key, None)
		st.session_state.pop(team_state_key, None)
		return None, None
	
	if not leagues:
		st.sidebar.error("No leagues found for this Fantrax account.")
		st.session_state.pop(league_state_key, None)
		st.session_state.pop(team_state_key, None)
		return None, None
	
	# Build options: label -> (league_id, team_id, league_name, team_name)
	options: Dict[str, Dict[str, str]] = {}
	for league in leagues:
		league_id = league.get("leagueId")
		team_id = league.get("teamId")
		league_name = league.get("league", league_id)
		team_name = league.get("team", team_id)
		
		if not league_id or not team_id or team_id == "NULL":
			continue
		
		# Create display label
		label = f"{league_name} — {team_name}"
		options[label] = {
			"league_id": league_id,
			"team_id": team_id,
			"league_name": league_name,
			"team_name": team_name,
		}
	
	if not options:
		st.sidebar.error("No valid leagues/teams found.")
		st.session_state.pop(league_state_key, None)
		st.session_state.pop(team_state_key, None)
		return None, None
	
	# Get current selection from session state
	current_league_id = st.session_state.get(league_state_key)
	current_team_id = st.session_state.get(team_state_key)
	
	# Find current label, or default to first option
	current_label = None
	for label, data in options.items():
		if data["league_id"] == current_league_id and data["team_id"] == current_team_id:
			current_label = label
			break
	
	if current_label is None:
		current_label = list(options.keys())[0]
	
	# Render selectbox
	selected_label = st.sidebar.selectbox(
		"Select League & Team",
		options=list(options.keys()),
		index=list(options.keys()).index(current_label),
		key="global_league_team_selector",
	)
	
	# Get selected data
	selected_data = options[selected_label]
	selected_league_id = selected_data["league_id"]
	selected_team_id = selected_data["team_id"]
	selected_league_name = selected_data["league_name"]
	selected_team_name = selected_data["team_name"]
	
	# Update session state
	st.session_state[league_state_key] = selected_league_id
	st.session_state[team_state_key] = selected_team_id
	st.session_state["league_name"] = selected_league_name
	st.session_state["team_name"] = selected_team_name
	
	# Show current selection as caption
	st.sidebar.caption(f"League: {selected_league_name}")
	st.sidebar.caption(f"Team: {selected_team_name}")
	
	return selected_league_id, selected_team_id

