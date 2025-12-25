"""
Players Page - Fantrax All Players export

Allows authenticated users to pull the complete Fantrax player list for
the selected league and download it as CSV directly from the UI.
"""

import streamlit as st
import pandas as pd
import logging
import json
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

st.set_page_config(page_title="Players", page_icon="📋", layout="wide")
st.title("All Fantrax Players")
st.caption("Export the full player pool for the selected league.")

if "auth_artifacts" not in st.session_state:
	st.error("Please authenticate on the Overview page first.")
	st.stop()

try:
	from utils.auth_helpers import load_requests_session_from_artifacts
	from apps.auth_login.context import select_league_and_team_in_sidebar
	session = load_requests_session_from_artifacts(st.session_state["auth_artifacts"])
except Exception as exc:
	st.error(f"Failed to build authenticated session: {exc}")
	st.stop()

league_id, _ = select_league_and_team_in_sidebar(session=session)
if not league_id:
	st.warning("Select a league in the sidebar to load players.")
	st.stop()

from fantraxapi.fantrax import FantraxAPI

@st.cache_data
def load_position_colors() -> dict:
	path = Path("position_id_color_map.json")
	if not path.exists():
		return {}
	try:
		with path.open() as f:
			data = json.load(f)
	except Exception:
		return {}
	color_keys = data.get("colorKeys") or {}
	position_map = data.get("positionMap") or {}
	mapping: dict[str, str] = {}
	for pid, meta in position_map.items():
		key = meta.get("colorKey")
		color = color_keys.get(key, "#d1d5db")
		mapping[str(pid)] = color
		short = meta.get("shortName")
		if short:
			mapping[short.upper()] = color
	return mapping

POSITION_COLORS = load_position_colors()

def _to_dataframe(players) -> pd.DataFrame:
	rows = []
	for pl in players:
		opponent = getattr(pl, "next_opponent", "") or ""
		if getattr(pl, "next_opponent_is_away", False) and opponent:
			opponent_display = f"@{opponent}"
		else:
			opponent_display = opponent
		icons = getattr(pl, "icons", []) or []
		icon_notes = "; ".join([i.get("tooltip", "") for i in icons if isinstance(i, dict) and i.get("tooltip")])
		rows.append(
			{
				"id": pl.id,
				"name": pl.name,
				"first_name": getattr(pl, "first_name", ""),
				"last_name": getattr(pl, "last_name", ""),
				"team": getattr(pl, "team", ""),
				"team_name": getattr(pl, "team_name", ""),
				"team_short_name": getattr(pl, "team_short_name", ""),
				"team_id": getattr(pl, "team_id", ""),
				"position": getattr(pl, "position", ""),
				"positions": ",".join(getattr(pl, "positions", []) or []),
				"default_pos_id": getattr(pl, "default_pos_id", ""),
				"pos_ids": ",".join(getattr(pl, "pos_ids", []) or []),
				"pos_ids_no_flex": ",".join(getattr(pl, "pos_ids_no_flex", []) or []),
				"status": getattr(pl, "status", ""),
				"injury_status": getattr(pl, "injury_status", ""),
				"rank": getattr(pl, "rank", ""),
				"short_name": getattr(pl, "short_name", ""),
				"url_name": getattr(pl, "url_name", ""),
				"headshot_url": getattr(pl, "headshot_url", ""),
				"upcoming_event_status": getattr(pl, "upcoming_event_status", ""),
				"table_rank": getattr(pl, "table_rank", ""),
				"owner_team": getattr(pl, "owner_team", ""),
				"owner_tooltip": getattr(pl, "owner_tooltip", ""),
				"owner_team_id": getattr(pl, "owner_team_id", ""),
				"next_opponent_display": opponent_display,
				"next_kickoff": getattr(pl, "next_kickoff", ""),
				"next_event_id": getattr(pl, "next_event_id", ""),
				"season_points": getattr(pl, "season_points", ""),
				"fppg_value": getattr(pl, "fppg_value", ""),
				"percent_owned": getattr(pl, "percent_owned", ""),
				"percent_started": getattr(pl, "percent_started", ""),
				"percent_started_delta": getattr(pl, "percent_started_delta", ""),
				"icon_tooltips": icon_notes,
			}
		)
	return pd.DataFrame(rows)

def _coerce_numeric(series: pd.Series) -> pd.Series:
	"""Normalize numeric-looking strings (commas, percent signs) into numbers."""
	cleaned = series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False)
	return pd.to_numeric(cleaned, errors="coerce")

try:
	api = FantraxAPI(league_id=league_id, session=session)
	with st.spinner("Fetching players from Fantrax..."):
		players = api.get_all_players()
except Exception as exc:
	logger.exception("Failed to fetch player list")
	st.error(f"Error fetching players: {exc}")
	st.stop()

if not players:
	st.info("Fantrax returned zero players for this league.")
	st.stop()

df = _to_dataframe(players)
df = df.sort_values("name")

# Normalize key numeric columns so Streamlit can sort them properly
for col in ["rank", "table_rank", "season_points", "fppg_value", "percent_owned", "percent_started"]:
	if col in df.columns:
		df[col] = _coerce_numeric(df[col])

display_df = df.rename(
	columns={
		"id": "Fantrax ID",
		"name": "Name",
		"position": "Pos",
		"positions": "Eligible",
		"status": "Status",
		"injury_status": "Injury",
		"team": "Team",
		"team_name": "Team Name",
		"team_short_name": "Team Short",
		"default_pos_id": "Default Pos Id",
		"rank": "Overall Rank",
		"short_name": "Short Name",
		"upcoming_event_status": "Upcoming Status",
		"table_rank": "Rank",
		"owner_team": "Owner",
		"next_opponent_display": "Next Opponent",
		"next_kickoff": "Next Kickoff",
		"season_points": "FPts",
		"fppg_value": "FP/G",
		"percent_owned": "% Owned",
		"percent_started": "% Started",
		"percent_started_delta": "% Started Δ",
		"icon_tooltips": "Icon Notes",
	}
)
display_df["Eligible"] = display_df["Eligible"].apply(lambda v: v.replace(",", ", ") if isinstance(v, str) else v)
display_df["Owner"] = display_df["Owner"].fillna("")
display_df["Next Opponent"] = display_df["Next Opponent"].fillna("")
display_df["Next Kickoff"] = display_df["Next Kickoff"].fillna("")
display_df["Icon Notes"] = display_df["Icon Notes"].fillna("")

# Force certain datatypes for sorting in the UI
numeric_display_cols = ["Rank", "Overall Rank", "FPts", "FP/G", "% Owned", "% Started"]
for col in numeric_display_cols:
	if col in display_df.columns:
		display_df[col] = _coerce_numeric(display_df[col])

# Round decimal-friendly columns for cleaner display
decimal_cols = ["FPts", "FP/G", "% Owned", "% Started"]
for col in decimal_cols:
	if col in display_df.columns:
		display_df[col] = display_df[col].round(2)

display_columns = [
	"Fantrax ID",
	"Rank",
	"Name",
	"Team",
	"Team Name",
	"Pos",
	"Eligible",
	"Owner",
	"Next Opponent",
	"Next Kickoff",
	"FPts",
	"FP/G",
	"% Owned",
	"% Started",
	"Icon Notes",
]

available_cols = [c for c in display_columns if c in display_df.columns]
display_df = display_df[available_cols]

st.success(f"Loaded {len(df)} players for league **{league_id}**.")

col1, col2 = st.columns(2)
with col1:
	st.metric("Total players", len(df))
with col2:
	distinct_teams = display_df["Team"].nunique()
	st.metric("Teams represented", distinct_teams)

csv_bytes = df.to_csv(index=False).encode("utf-8")
timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
default_name = f"fantrax_players_{league_id}_{timestamp}.csv"
st.download_button(
	label="⬇️ Download CSV",
	data=csv_bytes,
	file_name=default_name,
	mime="text/csv",
	use_container_width=True,
)

def _pos_style(row):
	color = None
	pos_val = str(row.get("Pos", "") or "").upper()
	if pos_val:
		color = POSITION_COLORS.get(pos_val)
	if not color:
		default_id = str(row.get("Default Pos Id") or "")
		if default_id:
			color = POSITION_COLORS.get(default_id)
	if color:
		return [f"background-color: {color}; color: #0f172a; font-weight: 600"]
	return [""]

styled_df = display_df.style.apply(_pos_style, subset=["Pos"], axis=1)

st.markdown("### Player Pool")
st.dataframe(styled_df, use_container_width=True, hide_index=True)
