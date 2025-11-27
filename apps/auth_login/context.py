"""
Global sidebar context selector for league and team selection.

Provides a single source of truth for which league/team is currently active
across all pages in the app.
"""

import logging
from typing import Optional, Tuple, Dict, List
import streamlit as st
from utils.auth_helpers import fetch_user_leagues


def select_league_and_team_in_sidebar(
	session,
	league_state_key: str = "league_id",
	team_state_key: str = "team_id",
	user_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
	"""
	Renders a global league + team selector in the sidebar.
	
	Uses fetch_user_leagues(session) to get the user's leagues and their team in each.
	Since each user has exactly one team per league, we show a combined selector.
	
	Args:
		session: Authenticated requests.Session
		league_state_key: Session state key for league_id (default: "league_id")
		team_state_key: Session state key for team_id (default: "team_id")
		user_id: Optional app user_id to load/save a preferred default league
	
	Returns:
		(league_id, team_id) tuple, or (None, None) if no leagues or errors occur
	
	Side effects:
		- Updates st.session_state[league_state_key] with selected league_id
		- Updates st.session_state[team_state_key] with selected team_id
		- Updates st.session_state["league_name"] with selected league name
		- Updates st.session_state["team_name"] with selected team name
	"""

	st.sidebar.markdown("### League & Team")

	user_id = user_id or st.session_state.get("current_user_id")
	user_mgr = None
	preferred_default = None
	if user_id:
		try:
			from utils.user_manager import UserManager
			user_mgr = UserManager()
			preferred_default = user_mgr.get_default_league(user_id)
		except Exception as exc:  # pragma: no cover - Streamlit runtime feedback
			logging.getLogger(__name__).debug("Could not load user default league: %s", exc)
			preferred_default = None
			user_mgr = None
		if preferred_default:
			default_label = preferred_default.get("league_name") or preferred_default.get("league_id")
			st.sidebar.caption(f"Default league preference: {default_label}")

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

	# Determine pre-selected label (session state -> preferred default -> first option)
	option_labels: List[str] = list(options.keys())
	current_label = None
	for label, data in options.items():
		if data["league_id"] == current_league_id and data["team_id"] == current_team_id:
			current_label = label
			break

	if current_label is None and preferred_default:
		for label, data in options.items():
			if data["league_id"] == preferred_default.get("league_id") and data["team_id"] == preferred_default.get("team_id"):
				current_label = label
				break

	if current_label is None:
		current_label = option_labels[0]

	# Render selectbox
	selected_label = st.sidebar.selectbox(
		"Select League & Team",
		options=option_labels,
		index=option_labels.index(current_label),
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

	if user_mgr and user_id:
		if st.sidebar.button("Set as default league", key=f"{league_state_key}_set_default"):
			ok = user_mgr.set_default_league(
				user_id=user_id,
				league_id=selected_league_id,
				team_id=selected_team_id,
				league_name=selected_league_name,
				team_name=selected_team_name,
			)
			if ok:
				st.sidebar.success("Saved as your default league.")
			else:
				st.sidebar.error("Couldn't save default league. Please try again.")

	# Show current selection as caption
	st.sidebar.caption(f"League: {selected_league_name}")
	st.sidebar.caption(f"Team: {selected_team_name}")
	
	return selected_league_id, selected_team_id
