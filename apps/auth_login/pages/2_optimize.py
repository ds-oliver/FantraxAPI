"""
Optimize Page - Fantrax x SofaScore Integration

Shows predicted and confirmed lineups from SofaScore for your rostered players,
helping you make informed decisions about active/reserve swaps, waiver claims, and drops.
"""

import streamlit as st
import logging
from pathlib import Path
import json
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict

logger = logging.getLogger(__name__)

# Configure page
st.set_page_config(page_title="Optimize", page_icon="⚽", layout="wide")

# Page header
st.title("Optimize: Lineup Intelligence")
st.markdown("**Predicted and confirmed lineups to help optimize your roster decisions**")

# Check for authentication
if "auth_artifacts" not in st.session_state:
	st.error("Please authenticate first")
	st.markdown("""
	### How to get started:
	
	1. Go back to Overview page
	2. Upload your Fantrax cookies OR use Selenium login
	3. Return here to view lineup predictions
	""")
	st.stop()

# Build session from artifacts
try:
	from utils.auth_helpers import load_requests_session_from_artifacts
	from apps.auth_login.context import select_league_and_team_in_sidebar
	session = load_requests_session_from_artifacts(st.session_state["auth_artifacts"])
except Exception as e:
	st.error(f"Failed to create session: {e}")
	st.stop()

# Global league/team selector (sidebar)
league_id, team_id = select_league_and_team_in_sidebar(session=session)

if not league_id:
	st.warning("Select a league in the sidebar to use Lineup Intelligence.")
	st.stop()
if not team_id:
	st.warning("Select a team/roster in the sidebar to use Lineup Intelligence.")
	st.stop()

# Store in session state for consistency
st.session_state["league_id"] = league_id
st.session_state["team_id"] = team_id

# Load roster
try:
	from fantraxapi.fantrax import FantraxAPI
	api = FantraxAPI(league_id=league_id, session=session)
	
	with st.spinner("Loading roster..."):
		roster = api.roster_info(team_id)
	
	# Get league/team names from session state
	league_name = st.session_state.get("league_name", league_id)
	team_name = st.session_state.get("team_name", team_id)
	
	st.success(f"Roster loaded: **{league_name} — {team_name}**")
	st.caption(f"League ID: {league_id} | Team ID: {team_id}")

except Exception as e:
	logger.exception("Failed to load roster")
	st.error(f"Failed to load roster: {e}")
	st.stop()

# Main content area
st.subheader("Your Players Lineup Status")

# Load lineup predictions button
if st.button("Load Latest Lineup Predictions", type="primary", use_container_width=True):
	try:
		lineup_dir = Path("data/sofascore/lineups")
		schedule_dir = Path("data/sofascore/schedules")
		
		if not lineup_dir.exists():
			st.warning("No lineup data. Run: `python esd_export_schedule_and_lineups_v2.py --tournament-id 17 --upcoming --with-lineups`")
		else:
			with st.spinner("Loading lineup predictions..."):
				# Load schedule to get match context
				schedule_df = None
				schedule_files = sorted(schedule_dir.glob("*_upcoming.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
				if schedule_files:
					schedule_df = pd.read_csv(schedule_files[0])
					# Parse kickoff times and filter to upcoming only
					schedule_df['kickoff_dt'] = pd.to_datetime(schedule_df['kickoff_utc'])
					now = datetime.now(timezone.utc)
					schedule_df = schedule_df[schedule_df['kickoff_dt'] > now].copy()
				
				# Load lineups for upcoming matches only
				relevant_lineups = {}
				team_next_match = {}  # Track next match per team
				
				if schedule_df is not None:
					# Sort by kickoff to process earliest matches first
					schedule_df = schedule_df.sort_values('kickoff_dt')
					
					for _, match in schedule_df.iterrows():
						event_id = match['event_id']
						lineup_file = lineup_dir / f"{event_id}.json"
						
						if not lineup_file.exists():
							continue
						
						try:
							with open(lineup_file, 'r') as f:
								lineup_data = json.load(f)
							
							# Only process if we have actual player data
							home_starters = lineup_data.get("home", {}).get("starters", [])
							away_starters = lineup_data.get("away", {}).get("starters", [])
							
							if not home_starters and not away_starters:
								continue
							
							home_team_id = match['home_team_id']
							away_team_id = match['away_team_id']
							
							# Store match context
							match_context = {
								"opponent": match['away_team'] if home_team_id else match['home_team'],
								"kickoff": match['kickoff_utc'],
								"confirmed": lineup_data.get("confirmed", False),
								"home_team": match['home_team'],
								"away_team": match['away_team'],
							}
							
							# Store lineup for home team (only if this is their next match)
							if home_team_id not in team_next_match:
								team_next_match[home_team_id] = event_id
								relevant_lineups[event_id] = {
									"lineup_data": lineup_data,
									"match_context": match_context,
								}
							
							# Store lineup for away team (only if this is their next match)
							if away_team_id not in team_next_match:
								team_next_match[away_team_id] = event_id
								if event_id not in relevant_lineups:
									relevant_lineups[event_id] = {
										"lineup_data": lineup_data,
										"match_context": match_context,
									}
						except Exception as e:
							logger.warning(f"Failed to load lineup {lineup_file}: {e}")
							continue
				
				# Extract players from relevant lineups only
				all_players = []
				for event_id, data in relevant_lineups.items():
					lineup_data = data["lineup_data"]
					match_context = data["match_context"]
					confirmed = lineup_data.get("confirmed", False)
					
					for side_key in ["home", "away"]:
						side = lineup_data.get(side_key, {})
						starters = side.get("starters", [])
						subs = side.get("subs", [])
						missing = side.get("missing", [])
						
						# Get team_id from first starter (missing players don't have team_id in JSON)
						side_team_id = starters[0].get("team_id") if starters else None
						
						# Process starters
						for player in starters:
							all_players.append({
								"sofascore_id": player.get("id"),
								"player_name": player.get("name"),
								"position": player.get("position"),
								"team_id": player.get("team_id"),
								"is_sub": False,
								"is_missing": False,
								"captain": player.get("captain", False),
								"event_id": event_id,
								"confirmed": confirmed,
								"match_context": match_context,
							})
						
						# Process subs
						for player in subs:
							all_players.append({
								"sofascore_id": player.get("id"),
								"player_name": player.get("name"),
								"position": player.get("position"),
								"team_id": player.get("team_id"),
								"is_sub": True,
								"is_missing": False,
								"captain": player.get("captain", False),
								"event_id": event_id,
								"confirmed": confirmed,
								"match_context": match_context,
							})
							
						# Process missing/injured/unavailable players
						for player in missing:
							reason = player.get("reason", 0)
							# Reason codes from SofaScore:
							# 1 = Doubtful (uncertain injury status)
							# 2 = Out (confirmed unavailable - more severe/certain)
							all_players.append({
								"sofascore_id": player.get("id"),
								"player_name": player.get("name"),
								"position": player.get("position"),
								"team_id": side_team_id,  # Missing players don't have team_id, use from starters
								"is_sub": False,
								"is_missing": True,
								"missing_reason": reason,
								"captain": False,
								"event_id": event_id,
								"confirmed": confirmed,
								"match_context": match_context,
							})
				
				if all_players:
					# Map SofaScore IDs to Fantrax IDs
					try:
						from fantraxapi.player_mapping import PlayerMappingManager
						mapping_mgr = PlayerMappingManager()
						
						for player in all_players:
							sofascore_id = player.get("sofascore_id")
							if sofascore_id:
								mapping = mapping_mgr.get_by_sofascore_id(sofascore_id)
								if mapping:
									player["fantrax_id"] = mapping.fantrax_id
									player["fantrax_name"] = mapping.fantrax_name
					except Exception as e:
						logger.warning(f"Player mapping failed: {e}")
					
					st.session_state["lineup_predictions"] = all_players
					unique_teams = len(set(p.get("team_id") for p in all_players if p.get("team_id")))
					st.success(f"Loaded {len(all_players)} predictions from next matches for {unique_teams} teams")
					st.rerun()
				else:
					st.info("No upcoming lineup data found")
	except Exception as e:
		logger.exception("Lineup loading error")
		st.error(f"Error: {e}")

# Display ALL roster players (with or without lineup data)
st.divider()

# Build dataframe from roster
rows = []
position_order = {"G": 0, "D": 1, "M": 2, "F": 3}

# Create lookup dict for lineup predictions (if loaded)
lineup_lookup = {}
if "lineup_predictions" in st.session_state:
	all_players = st.session_state["lineup_predictions"]
	for p in all_players:
		fantrax_id = p.get("fantrax_id")
		if fantrax_id:
			lineup_lookup[fantrax_id] = p

# Iterate through ALL roster players
for roster_row in roster.rows:
	if not roster_row.player:
		continue
	
	fantrax_id = roster_row.player.id
	player_name = roster_row.player.name
	is_starting = roster_row.pos_id != "0"
	
	# Get actual player position (not "Res" for bench)
	player_obj = roster_row.player
	if player_obj and hasattr(player_obj, 'position'):
		actual_pos = player_obj.position
	else:
		# Fallback to roster position if available
		actual_pos = roster_row.pos.short_name if roster_row.pos else "?"
	
	# Look up lineup prediction data
	prediction = lineup_lookup.get(fantrax_id)
	
	if prediction:
		# Has lineup data
		is_missing = prediction.get("is_missing", False)
		confirmed = prediction.get("confirmed", False)
		match_context = prediction.get("match_context", {})
		
		# Format match
		match_str = ""
		if match_context:
			home = match_context.get("home_team", "")
			away = match_context.get("away_team", "")
			kickoff = match_context.get("kickoff", "")
			if kickoff:
				try:
					dt = datetime.fromisoformat(kickoff.replace('+0000', '+00:00'))
					kickoff_str = dt.strftime("%a %b %d, %H:%M")
					match_str = f"{home} v {away} ({kickoff_str})"
				except:
					match_str = f"{home} v {away}"
		
		# Handle missing/injured players
		if is_missing:
			missing_reason = prediction.get("missing_reason", 1)
			# SofaScore reason codes:
			# 1 = Doubtful (uncertain injury status)
			# 2 = Out (confirmed unavailable - injury/suspension/etc)
			reason_map = {
				1: "Doubtful",
				2: "Out"
			}
			reason_str = reason_map.get(missing_reason, "Unavailable")
			
			rows.append({
				"Player": player_name,
				"Pos": actual_pos,
				"Roster Status": "Starting" if is_starting else "Benched",
				"Predicted": reason_str,
				"Status": "Confirmed" if confirmed else "Predicted",
				"Action": "Bench" if is_starting else "-",
				"Match": match_str,
				"_pos_order": position_order.get(actual_pos[0] if actual_pos else "M", 2),
				"_status_order": 0 if is_starting else 1,
				"_has_data": True
			})
		else:
			# Available player - determine starting status
			# Note: In predicted lineups (before ~1.25hrs before kickoff), subs array is empty
			# Only confirmed lineups have subs. So is_sub=False means "in starting XI"
			predicted_starting = not prediction.get("is_sub", False)
			
			# Determine action
			if is_starting and not predicted_starting:
				action = "Bench"
			elif not is_starting and predicted_starting:
				action = "Start"
			else:
				action = "OK"
			
			rows.append({
				"Player": player_name,
				"Pos": actual_pos,
				"Roster Status": "Starting" if is_starting else "Benched",
				"Predicted": "Starter" if predicted_starting else "Bench",
				"Status": "Confirmed" if confirmed else "Predicted",
				"Action": action,
				"Match": match_str,
				"_pos_order": position_order.get(actual_pos[0] if actual_pos else "M", 2),
				"_status_order": 0 if is_starting else 1,
				"_has_data": True
			})
	else:
		# No lineup data - show as Unknown
		rows.append({
			"Player": player_name,
			"Pos": actual_pos,
			"Roster Status": "Starting" if is_starting else "Benched",
			"Predicted": "Unknown",
			"Status": "No Data",
			"Action": "-",
			"Match": "",
			"_pos_order": position_order.get(actual_pos[0] if actual_pos else "M", 2),
			"_status_order": 0 if is_starting else 1,
			"_has_data": False
		})

if rows:
	# Create dataframe and sort: Starting players first (by position), then benched players (by position)
	df = pd.DataFrame(rows)
	df = df.sort_values(["_status_order", "_pos_order", "Player"])
	df = df.drop(columns=["_pos_order", "_status_order", "_has_data"])
	
	# Show summary stats
	total_players = len(rows)
	players_with_data = sum(1 for r in rows if r.get("_has_data", False))
	needs_action_count = len([r for r in rows if r["Action"] not in ["OK", "-"]])
	
	col_a, col_b, col_c = st.columns(3)
	with col_a:
		st.metric("Total Players", total_players)
	with col_b:
		st.metric("With Lineup Data", players_with_data)
	with col_c:
		if needs_action_count > 0:
			st.metric("Need Attention", needs_action_count)
		else:
			st.metric("Need Attention", 0)
	
	st.markdown("### Your Players Lineup Status")
	st.dataframe(
		df,
		use_container_width=True,
		hide_index=True,
		column_config={
			"Player": st.column_config.TextColumn("Player", width="medium"),
			"Pos": st.column_config.TextColumn("Pos", width="small"),
			"Roster Status": st.column_config.TextColumn("Roster Status", width="small"),
			"Predicted": st.column_config.TextColumn("SofaScore Pred", width="small"),
			"Status": st.column_config.TextColumn("Confirmation", width="small"),
			"Action": st.column_config.TextColumn("Action", width="small"),
			"Match": st.column_config.TextColumn("Next Match", width="large"),
		}
	)
	
	# Show helpful message if some players are missing data
	missing_data_count = total_players - players_with_data
	if missing_data_count > 0:
		with st.expander(f"Why are {missing_data_count} players showing 'Unknown'?"):
			st.markdown("""
			**Players show as 'Unknown' when:**
			- No lineup data available for their next match yet
			- Player mapping between SofaScore and Fantrax is missing
			- Their team's match is too far in the future
			- Their league isn't being tracked
			
			**To improve coverage:**
			- Run lineup fetch closer to match time
			- Update player mappings: `python scripts/update_player_mappings.py`
			""")
else:
	st.info("No roster data available")

