"""
Trade Opportunities Page - Division-Aware Roster Composition Analysis

Analyzes position composition (GK/D/M/F) across teams in your division,
identifies surpluses and needs, and ranks trade partners by complementary imbalances.
"""

import streamlit as st
import pandas as pd

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.utils import group_teams_by_division_api
from fantraxapi.trade_analysis import (
	build_division_profiles,
	compute_surplus_and_needs,
	compute_trade_matches,
)

# Configure page
st.set_page_config(page_title="Trade Opportunities", page_icon="🔄", layout="wide")


def main():
	"""Main function for Trade Opportunities page."""
	st.title("Trade Opportunities")
	st.markdown("**Division-aware roster composition analysis to find ideal trade partners**")
	
	# Check for authentication
	if "auth_artifacts" not in st.session_state:
		st.error("Please authenticate first")
		st.markdown("""
		### How to get started:
		
		1. Go to Overview page
		2. Upload your Fantrax cookies OR use Selenium login
		3. Return here to analyze trade opportunities
		""")
		st.stop()
	
	# Build session from artifacts if needed
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
		st.warning("Select a league in the sidebar to analyze trade opportunities.")
		st.stop()
	if not team_id:
		st.warning("Select a team in the sidebar to analyze trade opportunities.")
		st.stop()
	
	# Store in session state for consistency
	st.session_state["league_id"] = league_id
	st.session_state["team_id"] = team_id
	
	# Initialize API - always create fresh instance if league changed
	# Check if we need to reinitialize (league changed or api doesn't exist)
	current_api = st.session_state.get("api")
	cached_league_id = st.session_state.get("api_league_id")
	
	if current_api is None or cached_league_id != league_id:
		try:
			api = FantraxAPI(league_id=league_id, session=session)
			st.session_state["api"] = api
			st.session_state["api_league_id"] = league_id
		except Exception as e:
			st.error(f"Failed to initialize Fantrax API: {e}")
			st.stop()
	else:
		api = current_api
	
	your_team_id = team_id
	
	st.divider()
	
	# Get league/team names for display
	league_name = st.session_state.get("league_name", league_id)
	team_name = st.session_state.get("team_name", team_id)
	st.info(f"Analyzing: **{league_name}** — Team: **{team_name}**")
	
	# 1) Determine divisions (runs fresh every time)
	with st.spinner("Detecting divisions..."):
		try:
			divisions = group_teams_by_division_api(api)
		except Exception as e:
			st.error(f"Failed to detect divisions: {e}")
			st.caption("Falling back to single group with all teams.")
			divisions = {"All Teams": [t.team_id for t in api.teams]}
	
	st.caption(f"Detected divisions: {', '.join(divisions.keys())}")
	
	# Debug: Show team names to help diagnose division detection
	with st.expander("🔍 Debug: View Team Names & Roster Sizes"):
		st.caption("Team names are used to detect divisions. Pattern: 'DIV - TeamName'")
		st.markdown("**Team Names:**")
		team_names = [f"{t.name} (ID: {t.team_id})" for t in api.teams]
		st.write(team_names)
		
		st.markdown("**Sample Roster Check (Your Team):**")
		try:
			debug_roster = api.roster_info(your_team_id)
			st.write(f"Total rows in roster.rows: {len(debug_roster.rows)}")
			st.write(f"Starters: {len(debug_roster.get_starters())}")
			st.write(f"Bench: {len(debug_roster.get_bench_players())}")
			
			# Show first few players with their positions
			st.markdown("**First 5 players (with position info):**")
			for i, row in enumerate(debug_roster.rows[:5]):
				if row.player:
					pos_display = row.pos.short_name if row.pos else "?"
					actual_pos = None
					if hasattr(row, "_raw") and row._raw:
						scorer = row._raw.get("scorer", {})
						actual_pos = scorer.get("posShortNames")
					st.write(f"{i+1}. {row.player.name} - Display: {pos_display}, Actual: {actual_pos}, pos_id: {row.pos_id}")
		except Exception as e:
			st.error(f"Debug roster load failed: {e}")
	
	# 2) Find your division (prioritize specific divisions over "All Teams")
	your_division_name = None
	your_division_team_ids = None
	
	# First pass: look for specific divisions (not "All Teams")
	for div_name, team_ids in divisions.items():
		if div_name != "All Teams" and your_team_id in team_ids:
			your_division_name = div_name
			your_division_team_ids = team_ids
			break
	
	# Second pass: fall back to "All Teams" if that's the only option
	if not your_division_name:
		for div_name, team_ids in divisions.items():
			if your_team_id in team_ids:
				your_division_name = div_name
				your_division_team_ids = team_ids
				break
	
	if not your_division_name or not your_division_team_ids:
		# Fallback: treat all teams as one group
		st.warning("Could not detect your division; using all teams in league.")
		your_division_name = "All Teams"
		your_division_team_ids = [t.team_id for t in api.teams]
	
	st.subheader(f"Division: {your_division_name}")
	st.caption(f"{len(your_division_team_ids)} teams in this division")
	
	# 3) Fetch FAAB budgets for all teams
	with st.spinner("Fetching FAAB budgets..."):
		try:
			faab_budgets = api.league.faab_budgets()
		except Exception as e:
			st.warning(f"Could not fetch FAAB budgets: {e}")
			faab_budgets = {}
	
	# 4) Build position profiles for this division
	with st.spinner("Analyzing roster compositions..."):
		try:
			profiles = build_division_profiles(
				api=api,
				league_id=league_id,
				division_name=your_division_name,
				division_team_ids=your_division_team_ids,
				faab_budgets=faab_budgets,
			)
		except Exception as e:
			st.error(f"Failed to build position profiles: {e}")
			st.stop()
	
	if not profiles:
		st.error("Could not build profiles for any teams in division.")
		st.stop()
	
	# 5) Compute surplus/needs
	compute_surplus_and_needs(profiles, min_delta=1)
	
	your_profile = profiles.get(your_team_id)
	if not your_profile:
		st.error("Could not find your team profile in division.")
		st.stop()
	
	# 6) Show your composition
	st.markdown("### Your Roster Composition")
	
	col1, col2 = st.columns([1, 2])
	
	with col1:
		st.markdown("**Position Counts**")
		position_df = pd.DataFrame({
			"Position": ["GK", "D", "M", "F"],
			"Count": [
				your_profile.position_counts.get("GK", 0),
				your_profile.position_counts.get("D", 0),
				your_profile.position_counts.get("M", 0),
				your_profile.position_counts.get("F", 0),
			],
		})
		st.dataframe(position_df, hide_index=True, use_container_width=True)
		st.caption(f"Total: {your_profile.total_players} players")
		if your_profile.faab_budget > 0:
			st.caption(f"FAAB: ${your_profile.faab_budget:.0f}")
	
	with col2:
		st.markdown("**Your Surplus & Needs**")
		if your_profile.surplus_positions:
			st.markdown(f"**Surplus:** {', '.join(your_profile.surplus_positions)}")
		else:
			st.markdown("**Surplus:** None")
		
		if your_profile.need_positions:
			st.markdown(f"**Needs:** {', '.join(your_profile.need_positions)}")
		else:
			st.markdown("**Needs:** None")
		
		if not your_profile.surplus_positions and not your_profile.need_positions:
			st.info("Your roster is well-balanced relative to division averages.")
	
	# 7) Show all division teams composition
	st.divider()
	st.markdown("### Division Position Overview")
	
	table_rows = []
	for p in profiles.values():
		table_rows.append({
			"Team": p.team_name,
			"GK": p.position_counts.get("GK", 0),
			"D": p.position_counts.get("D", 0),
			"M": p.position_counts.get("M", 0),
			"F": p.position_counts.get("F", 0),
			"Total": p.total_players,
			"FAAB": f"${p.faab_budget:.0f}" if p.faab_budget > 0 else "-",
			"Surplus": ", ".join(p.surplus_positions) if p.surplus_positions else "-",
			"Needs": ", ".join(p.need_positions) if p.need_positions else "-",
		})
	
	if table_rows:
		df = pd.DataFrame(table_rows)
		# Highlight your team
		def highlight_your_team(row):
			if row["Team"] == your_profile.team_name:
				return ["background-color: #e6f3ff"] * len(row)
			return [""] * len(row)
		
		st.dataframe(
			df.style.apply(highlight_your_team, axis=1),
			use_container_width=True,
			hide_index=True,
		)
	else:
		st.info("No teams found in this division.")
		return
	
	# 8) Check if we should jump straight to trade builder
	if st.session_state.get("show_trade_builder"):
		# Skip to trade builder section below
		pass
	else:
		# 8a) Manual Trade Builder - Player Selection (Always Available)
		st.divider()
		st.markdown("### Build a Trade")
		st.caption("Select any player from your division to start building a trade")
		
			# Collect all players from division teams (excluding your team)
		all_division_players = []
		for team_id in your_division_team_ids:
			if team_id == your_team_id:
				continue  # Skip your own team
			
			team_profile = profiles.get(team_id)
			if not team_profile:
				continue
			
			try:
				roster = api.roster_info(team_id)
				for row in roster.rows:
					if row.player:
						pos_display = row.pos.short_name if row.pos else "?"
						all_division_players.append({
							"player_id": row.player.id,
							"player_name": row.player.name,
							"team_id": team_id,
							"team_name": team_profile.team_name,
							"pos": pos_display,
							"fppg": row.fppg if hasattr(row, 'fppg') and row.fppg is not None else 0.0,
						})
			except Exception:
				continue
		
		if not all_division_players:
			st.warning("No players found in division to trade with.")
			return
		
		# Create DataFrame
		player_df = pd.DataFrame(all_division_players)
		player_df["select"] = False
		
		st.markdown("#### Step 1: Select Target Player")
		st.caption(f"Showing {len(all_division_players)} players from {len(your_division_team_ids) - 1} teams in your division")
		
		edited_df = st.data_editor(
			player_df,
			hide_index=True,
			use_container_width=True,
			column_config={
				"select": st.column_config.CheckboxColumn("Select", default=False, help="Check to select this player as trade target"),
				"player_name": st.column_config.TextColumn("Player", width="medium"),
				"team_name": st.column_config.TextColumn("Team", width="medium"),
				"pos": st.column_config.TextColumn("Pos", width="small"),
				"fppg": st.column_config.NumberColumn("FPPG", format="%.1f", width="small"),
			},
			disabled=["player_id", "player_name", "team_id", "team_name", "pos", "fppg"],
			key="player_selection_table"
		)
		
		# Check selection
		selected_rows = edited_df[edited_df["select"]]
		
		if len(selected_rows) == 0:
			st.info("👆 Check the box next to one player to start building a trade with their team.")
			
			# Show algorithmic suggestions below as optional reference
			st.divider()
			st.markdown("### 💡 Suggested Trade Partners (Optional)")
			st.caption("Teams ranked by complementary roster composition - these are suggestions only")
			
			suggestions = compute_trade_matches(profiles, your_team_id=your_team_id)
			
			if not suggestions:
				st.info("No strong positional matches found, but you can still select any player above to build a custom trade.")
			else:
				for idx, sug in enumerate(suggestions[:5], 1):  # Show top 5
					st.markdown(f"**#{idx}: {sug.partner_team_name}** (match score: {sug.score})")
					if sug.your_surplus_their_need:
						st.caption(f"You give {', '.join(sug.your_surplus_their_need)} → they need")
					if sug.their_surplus_your_need:
						st.caption(f"They give {', '.join(sug.their_surplus_your_need)} → you need")
			
			st.stop()
		
		if len(selected_rows) > 1:
			st.warning(f"⚠️ You've selected {len(selected_rows)} players. Please uncheck until only ONE player is selected.")
			st.stop()
		
		# Exactly one player selected - proceed to trade builder
		target_row = selected_rows.iloc[0]
		target_player_id = target_row["player_id"]
		target_player_name = target_row["player_name"]
		target_team_id = target_row["team_id"]
		target_team_name = target_row["team_name"]
		
		st.success(f"✅ Trade target: **{target_player_name}** ({target_row['pos']}) from **{target_team_name}**")
		
		if st.button("Proceed to Trade Builder", type="primary", key="proceed_to_builder"):
			st.session_state["trade_builder_partner"] = target_team_id
			st.session_state["show_trade_builder"] = True
			st.session_state["preselected_target_player"] = target_player_name
			st.rerun()
		
		if st.button("Cancel Selection", key="cancel_selection"):
			st.rerun()
		
		st.stop()  # Don't continue to trade builder here - user needs to click "Proceed"
	
	# 9) Trade Builder UI (HIGH-STAKES - Requires Explicit Confirmation)
	if st.session_state.get("show_trade_builder"):
		st.divider()
		st.markdown("### Trade Builder")
		st.warning("⚠️ **IMPORTANT**: Trades are OFFICIAL once submitted and can be accepted by the other team immediately. Please review carefully!")
		
		partner_id = st.session_state["trade_builder_partner"]
		partner_profile = profiles.get(partner_id)
		
		if not partner_profile:
			st.error("Partner team not found.")
			if st.button("Close Trade Builder"):
				st.session_state["show_trade_builder"] = False
				st.rerun()
			st.stop()
		
		# Load rosters
		with st.spinner("Loading rosters..."):
			try:
				your_roster = api.roster_info(your_team_id)
				their_roster = api.roster_info(partner_id)
			except Exception as e:
				st.error(f"Failed to load rosters: {e}")
				if st.button("Close Trade Builder"):
					st.session_state["show_trade_builder"] = False
					st.rerun()
				st.stop()
		
		# Selection columns
		col1, col2 = st.columns(2)
		
		with col1:
			st.markdown(f"### Your Team: {your_profile.team_name}")
			st.caption(f"FAAB Available: ${your_profile.faab_budget:.0f}")
			
			# Player selection - Build dict with player name as key
			your_players = {}
			for r in your_roster.rows:
				if r.player:
					pos_display = r.pos.short_name if r.pos else "?"
					label = f"{r.player.name} ({pos_display})"
					your_players[label] = r.player.id
			
			players_to_give = st.multiselect(
				"Players to Trade Away",
				options=sorted(your_players.keys()),
				key="players_to_give",
				help="Select the players from YOUR roster that you want to trade away"
			)
			
			# FAAB to give
			max_faab_give = float(your_profile.faab_budget) if your_profile.faab_tradeable else 0.0
			faab_to_give = st.number_input(
				"FAAB to Give",
				min_value=0.0,
				max_value=max_faab_give,
				step=1.0,
				value=0.0,
				disabled=not your_profile.faab_tradeable,
				help="Amount of FAAB budget to include in the trade" if your_profile.faab_tradeable else "FAAB not tradeable in this league"
			)
		
		with col2:
			st.markdown(f"### Partner: {partner_profile.team_name}")
			st.caption(f"FAAB Available: ${partner_profile.faab_budget:.0f}")
			
			# Their player selection
			their_players = {}
			for r in their_roster.rows:
				if r.player:
					pos_display = r.pos.short_name if r.pos else "?"
					label = f"{r.player.name} ({pos_display})"
					their_players[label] = r.player.id
			
			players_to_receive = st.multiselect(
				"Players to Receive",
				options=sorted(their_players.keys()),
				key="players_to_receive",
				help="Select the players from THEIR roster that you want to receive"
			)
			
			# FAAB to receive
			max_faab_receive = float(partner_profile.faab_budget) if partner_profile.faab_tradeable else 0.0
			faab_to_receive = st.number_input(
				"FAAB to Receive",
				min_value=0.0,
				max_value=max_faab_receive,
				step=1.0,
				value=0.0,
				disabled=not partner_profile.faab_tradeable,
				help="Amount of FAAB budget to receive in the trade" if partner_profile.faab_tradeable else "FAAB not tradeable in this league"
			)
		
		# Build player ID lists
		giving_player_ids = [your_players[p] for p in players_to_give]
		receiving_player_ids = [their_players[p] for p in players_to_receive]
		
		# Validation
		has_assets = (
			giving_player_ids or receiving_player_ids 
			or faab_to_give > 0 or faab_to_receive > 0
		)
		
		# Trade Summary
		st.divider()
		st.markdown("### Trade Summary - Please Review Carefully")
		
		col_preview_1, col_preview_2 = st.columns(2)
		
		with col_preview_1:
			st.markdown("**You Give:**")
			if not players_to_give and faab_to_give == 0:
				st.info("Nothing selected")
			else:
				for p in players_to_give:
					st.write(f"- {p}")
				if faab_to_give > 0:
					st.write(f"- ${faab_to_give:.0f} FAAB")
		
		with col_preview_2:
			st.markdown("**You Receive:**")
			if not players_to_receive and faab_to_receive == 0:
				st.info("Nothing selected")
			else:
				for p in players_to_receive:
					st.write(f"- {p}")
				if faab_to_receive > 0:
					st.write(f"- ${faab_to_receive:.0f} FAAB")
		
		if not has_assets:
			st.error("⚠️ You must select at least one asset (players or FAAB) to trade.")
		
		# Final confirmation section
		st.divider()
		st.markdown("### Final Confirmation")
		
		confirm_checkbox = st.checkbox(
			f"I confirm that I want to propose this trade to {partner_profile.team_name}. "
			"I understand this trade will be OFFICIAL once submitted.",
			key="confirm_trade_checkbox"
		)
		
		# Action buttons
		col_action_1, col_action_2, col_action_3 = st.columns([1, 1, 2])
		
		with col_action_1:
			submit_disabled = not (has_assets and confirm_checkbox)
			if st.button(
				"Submit Trade",
				disabled=submit_disabled,
				type="primary",
				key="submit_trade_button"
			):
				# Final confirmation dialog
				st.session_state["confirm_submit"] = True
		
		with col_action_2:
			if st.button("Cancel", key="cancel_trade_button"):
				st.session_state["show_trade_builder"] = False
				st.session_state.pop("confirm_submit", None)
				st.rerun()
		
		# Final confirmation modal (using session state)
		if st.session_state.get("confirm_submit"):
			st.error("### ⚠️ FINAL CONFIRMATION REQUIRED")
			st.markdown(f"""
			**You are about to submit an OFFICIAL trade proposal to {partner_profile.team_name}.**
			
			**You Give:** {', '.join(players_to_give) if players_to_give else 'No players'}{f' + ${faab_to_give:.0f} FAAB' if faab_to_give > 0 else ''}
			
			**You Receive:** {', '.join(players_to_receive) if players_to_receive else 'No players'}{f' + ${faab_to_receive:.0f} FAAB' if faab_to_receive > 0 else ''}
			
			This action cannot be undone. The trade will appear in Fantrax and can be accepted by the other team.
			""")
			
			col_final_1, col_final_2, col_final_3 = st.columns([1, 1, 2])
			
			with col_final_1:
				if st.button("YES, SUBMIT TRADE", type="primary", key="final_submit_yes"):
					with st.spinner("Submitting trade to Fantrax..."):
						try:
							result = api.trades.propose_trade(
								from_team_id=your_team_id,
								to_team_id=partner_id,
								player_ids_to_give=giving_player_ids if giving_player_ids else None,
								player_ids_to_receive=receiving_player_ids if receiving_player_ids else None,
								faab_to_give=faab_to_give if faab_to_give > 0 else None,
								faab_to_receive=faab_to_receive if faab_to_receive > 0 else None
							)
							st.success(f"✅ Trade proposed successfully! Trade ID: {result.get('txSetId', 'N/A')}")
							st.info("The trade has been sent to Fantrax and is now pending acceptance.")
							# Clear all trade builder state
							st.session_state["show_trade_builder"] = False
							st.session_state.pop("confirm_submit", None)
							st.session_state.pop("players_to_give", None)
							st.session_state.pop("players_to_receive", None)
							st.balloons()
							st.rerun()
						except Exception as e:
							st.error(f"❌ Failed to submit trade: {e}")
							st.session_state.pop("confirm_submit", None)
			
			with col_final_2:
				if st.button("NO, CANCEL", key="final_submit_no"):
					st.session_state.pop("confirm_submit", None)
					st.rerun()


if __name__ == "__main__":
	main()

