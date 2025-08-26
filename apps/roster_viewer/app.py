"""
Streamlit app for viewing Fantrax rosters across multiple leagues.
"""

from __future__ import annotations
import yaml
import streamlit as st
from typing import Dict, List
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster, RosterRow
from requests import Session

import colorsys, hashlib

# Per-position accents (distinct + readable on dark)
POS_COLORS = {
	"G":  "#7DA6FF",   # GK = cornflower blue (clearly different from D)
	"D":  "#42EADD",   # DEF = teal/aqua
	"M":  "#8BE89B",   # MID = green-cyan
	"F":  "#FFD966",   # FWD = warm yellow
	"RES": "#9AA4B2",  # Bench/reserves label
}

# Team → primary color (good defaults for PL + a few extras)
TEAM_COLORS = {
	"ARS": "#EF0107", "AVL": "#670E36", "BOU": "#DA291C", "BRF": "#E30613", "BRE": "#E30613",
	"BHA": "#0057B8", "BUR": "#6C1D45", "CHE": "#034694", "CRY": "#1B458F", "EVE": "#003399",
	"FUL": "#FFFFFF", "IPS": "#003D8E", "LEE": "#FFCD00", "LEI": "#003090", "LIV": "#C8102E",
	"MCI": "#6CABDD", "MUN": "#DA020E", "NEW": "#FFFFFF", "NFO": "#DD0000", "NOT": "#DD0000",
	"SHU": "#EE2737", "SOU": "#D71920", "TOT": "#F4F7FF", "WHU": "#7A263A", "WOL": "#FDB913",
	"BRI": "#0057B8"  # alias for Brighton sometimes
}

# Force a specific label color for teams that don't contrast well on dark bg
SPECIAL_TEAM_TEXT_COLORS = {
	"TOT": "#F4F7FF",	# Spurs = near-white label on dark background
	# add more here if needed
}

def _pos_color(pos: str) -> str:
	p = (pos or "").upper()
	if p in POS_COLORS:
		return POS_COLORS[p]
	if p.startswith("R"):	  # Res/BN/etc.
		return POS_COLORS["RES"]
	# Common long forms
	if p in {"GK"}:	 return POS_COLORS["G"]
	if p in {"DEF"}: return POS_COLORS["D"]
	if p in {"MID"}: return POS_COLORS["M"]
	if p in {"FWD"}: return POS_COLORS["F"]
	return POS_COLORS["RES"]

def _hex_to_rgb(h):
	h = h.strip("#"); return tuple(int(h[i:i+2], 16) for i in (0,2,4))

def _rgb_to_hex(rgb):
	return "#{:02X}{:02X}{:02X}".format(*rgb)

def _rel_lum(rgb):
	# sRGB → linearized
	def f(c):
		c = c/255.0
		return c/12.92 if c <= 0.03928 else ((c+0.055)/1.055)**2.4
	r,g,b = map(f, rgb)
	return 0.2126*r + 0.7152*g + 0.0722*b

def _contrast(c1, c2):
	L1, L2 = _rel_lum(_hex_to_rgb(c1)), _rel_lum(_hex_to_rgb(c2))
	L1, L2 = (L1,L2) if L1>=L2 else (L2,L1)
	return (L1+0.05)/(L2+0.05)

def _shift_hue(hex_color, deg):
	r,g,b = _hex_to_rgb(hex_color)
	h,l,s = colorsys.rgb_to_hls(r/255, g/255, b/255)
	h = (h + deg/360.0) % 1.0
	r,g,b = colorsys.hls_to_rgb(h, l, s)
	return _rgb_to_hex((int(r*255), int(g*255), int(b*255)))

def _tweak_lightness(hex_color, factor):
	r,g,b = _hex_to_rgb(hex_color)
	h,l,s = colorsys.rgb_to_hls(r/255, g/255, b/255)
	l = max(0, min(1, l*factor))
	r,g,b = colorsys.hls_to_rgb(h, l, s)
	return _rgb_to_hex((int(r*255), int(g*255), int(b*255)))

def _ensure_contrast(color, bg, min_ratio=4.5):
	# try lightening then darkening to hit contrast
	c = color
	for f in (1.25, 1.4, 1.6, 0.85, 0.7, 0.55):
		if _contrast(c, bg) >= min_ratio:
			return c
		c = _tweak_lightness(c, f)
	return c  # best effort

def _hash_color(key, base_bg="#101620"):
	# stable hash → hue
	h = int(hashlib.md5(key.encode()).hexdigest(), 16) % 360
	# vivid, readable default
	r,g,b = colorsys.hls_to_rgb(h/360.0, 0.6, 0.78)
	c = _rgb_to_hex((int(r*255), int(g*255), int(b*255)))
	return _ensure_contrast(c, base_bg)


# ── MUST be the first Streamlit call ───────────────────────────────────────────
st.set_page_config(
	page_title="Fantrax Roster Viewer",
	page_icon="⚽",
	layout="wide",
	initial_sidebar_state="expanded",
)
# ───────────────────────────────────────────────────────────────────────────────

# Global CSS (after set_page_config)
st.markdown("""
<style>
  :root {
	--bg: #0E1117;
	--fg: #FAFAFA;
	--cyan: #03dac6;
	--green: #70c1b3;
	--yellow: #ffd700;
	--magenta: #ff79c6;
	--muted: #2b2f36;
  }
  html, body, .stApp { background-color: var(--bg); color: var(--fg); }
  .cli-title { color: var(--cyan); font-weight: 700; margin-bottom: .5rem; }
  .league-title { font-weight: 700; margin: .25rem 0 .5rem 0; }
  .roster-table { 
	  width: 100%; border-collapse: collapse; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
	  background: #0d1016; border: 1px solid var(--muted); border-radius: 6px; overflow: hidden;
  }
  .roster-table th, .roster-table td { padding: 6px 10px; font-size: 13.5px; }
  .roster-table thead th { text-align: left; background: #121621; color: var(--magenta); border-bottom: 1px solid var(--muted); }
  .roster-table tbody tr { border-bottom: 1px solid #151a22; }
  .roster-table tbody tr:last-child { border-bottom: none; }
  .c-pos { color: var(--cyan); width: 48px; text-align: center; font-weight: 700; }
  .c-name { color: #ffffff; }
  .c-team { color: var(--green); width: 64px; text-align: center; }
  .c-fppg { color: var(--yellow); width: 56px; text-align: right; }
  .section-row td { padding-top: 10px; color: #9aa4b2; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


def load_league_config() -> dict:
	with open('config/fantrax_leagues.yaml', 'r') as f:
		return yaml.safe_load(f)


def get_roster_for_league(league_id: str, team_id: str, session: Session) -> Roster:
	api = FantraxAPI(league_id=league_id, session=session)
	return api.roster_info(team_id)


def _fmt_row(row: RosterRow) -> tuple[str, str, str, str]:
	if row.player:
		pos = row.pos.short_name or 'BN'
		name = row.player.name
		team = row.player.team_short_name or row.player.team_name or ''
		fppg = f"{row.fppg:.1f}" if row.fppg is not None else "-"
		return pos, name, team, fppg
	return "", "", "", ""


def roster_to_html(league_name: str, roster: Roster, starters_only: bool) -> str:
	base = (st.get_option("theme.base") or "dark").lower()
	if base == "light":
		PAGE_BG = "transparent"; ROW_BG = "#ffffff"; FG = "#1f2937"; HEAD="#f5f7fa"; MUTED="#d1d5db"
	else:
		PAGE_BG = "transparent"; ROW_BG = "#101620"; FG = "#E8EDF5"; HEAD="#151C27"; MUTED="#2A3442"

	# cache to keep team colors consistent within a table
	team_color_cache: dict[str, str] = {}
	used_colors: dict[str, int] = {}

	def team_color(team_code: str) -> str:
		if not team_code:
			return _hash_color("UNKNOWN", ROW_BG)

		t = team_code.upper()
		# normalize aliases
		t = {"BRI":"BHA", "BRE":"BRF", "NOT":"NFO", "LEEDS":"LEE"}.get(t, t)

		# hard override for readability
		if t in SPECIAL_TEAM_TEXT_COLORS:
			c = SPECIAL_TEAM_TEXT_COLORS[t]
			return _ensure_contrast(c, ROW_BG, min_ratio=7.0)  # keep it bright

		# otherwise use club primary (then ensure contrast; de-dupe if needed)
		base_c = TEAM_COLORS.get(t, _hash_color(t, ROW_BG))
		c = _ensure_contrast(base_c, ROW_BG, 4.5)
		if c in used_colors:
			bump = 12 * used_colors[c]	   # shift hue if duplicate
			c = _ensure_contrast(_shift_hue(c, bump), ROW_BG, 4.5)
			used_colors[c] = 1
		else:
			used_colors[c] = 1
		team_color_cache[t] = c
		return c

	def build_rows(rows):
		html = []
		for r in rows:
			pos = (r.pos.short_name if r.pos else "BN") or "BN"
			name = r.player.name if r.player else ""
			tcode = (r.player.team_short_name or r.player.team_name or "").upper() if r.player else ""
			# normalize a few aliases
			tcode = {"BRI":"BHA","BRE":"BRF","NOT":"NFO","LEEDS":"LEE"}.get(tcode, tcode)
			team = tcode
			fppg = f"{r.fppg:.1f}" if r.fppg is not None else "-"
			pc = _pos_color(pos)
			tc = team_color(team)
			html.append(
				f'<tr>'
				f'<td class="c-pos" style="color:{pc}">{pos}</td>'
				f'<td class="c-name">{name}</td>'
				f'<td class="c-team" style="color:{tc}">{team}</td>'
				f'<td class="c-fppg">{fppg}</td>'
				f'</tr>'
			)
		return "".join(html)

	starters_html = build_rows(roster.get_starters())
	bench_html = "" if starters_only else build_rows(roster.get_bench_players())

	return f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<style>
  html, body {{ background: {PAGE_BG}; color: {FG}; margin: 0; padding: 0; }}
  /* Use the same monospace as table so league name pairs perfectly */
  .league-title {{
	  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
	  font-weight: 700; margin: .25rem 0 .5rem 0;
  }}
  .roster-table {{
	  width: 100%; border-collapse: collapse;
	  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
	  background: {ROW_BG}; border: 1px solid {MUTED}; border-radius: 6px; overflow: hidden;
	  box-shadow: 0 2px 6px rgba(0,0,0,.25);
  }}
  .roster-table th, .roster-table td {{ padding: 7px 10px; font-size: 14px; }}
  thead th {{ text-align: left; background: {HEAD}; color: #FF79C6; border-bottom: 1px solid {MUTED}; }}
  tbody tr {{ border-bottom: 1px solid {MUTED}; }}
  tbody tr:last-child {{ border-bottom: none; }}

  .c-pos  {{ width: 52px; text-align: center; font-weight: 700; }}
  .c-name {{ color: {FG}; }}
  .c-team {{ width: 68px; text-align: center; font-weight: 700; }}
  .c-fppg {{ width: 56px; text-align: right; font-variant-numeric: tabular-nums; color: #FFD966; }}

  .section-row td {{ padding-top: 10px; color: {FG}; font-weight: 700; opacity: .95; }}
  .spacer td {{ height: 6px; padding: 0; border: none; }}
</style>
</head>
<body>
  <div class="league-title">{league_name}</div>
  <table class="roster-table">
	<thead>
	  <tr>
		<th style="text-align:center; width:52px;">Pos</th>
		<th>Player</th>
		<th style="text-align:center; width:68px;">Team</th>
		<th style="text-align:right;  width:56px;">FPPG</th>
	  </tr>
	</thead>
	<tbody>
	  <tr class="section-row"><td colspan="4">Starters</td></tr>
	  {starters_html}
	  {"<tr class='spacer'><td colspan='4'></td></tr><tr class='section-row'><td colspan='4'>Bench</td></tr>"+bench_html if bench_html else ""}
	</tbody>
  </table>
</body></html>
"""


def display_roster(roster: Roster, starters_only: bool = False):
	# (kept for compatibility; not used with the HTML path)
	st.markdown("### Starters")
	starters_data = []
	for row in roster.get_starters():
		if row.player:
			pos = row.pos.short_name or 'BN'
			name = row.player.name
			team = row.player.team_short_name or row.player.team_name or ''
			fppg = f"{row.fppg:.1f}" if row.fppg is not None else "-"
			starters_data.append([f"**{pos}**", name, team, fppg])
	if starters_data:
		st.table(starters_data)
	else:
		st.info("No starters found")

	if not starters_only:
		st.markdown("### Bench")
		bench_data = []
		for row in roster.get_bench_players():
			if row.player:
				pos = row.pos.short_name or 'BN'
				name = row.player.name
				team = row.player.team_short_name or row.player.team_name or ''
				fppg = f"{row.fppg:.1f}" if row.fppg is not None else "-"
				bench_data.append([f"**{pos}**", name, team, fppg])
		if bench_data:
			st.table(bench_data)


def main():
	st.markdown('<div class="cli-title">All League Rosters</div>', unsafe_allow_html=True)

	# Sidebar
	with st.sidebar:
		st.header("Filters / Data")
		show_starters = st.checkbox("Show Starters Only", value=False)
		refresh = st.button("🔄 Refresh Data", type="primary")

	# Load / refresh
	if refresh or "rosters" not in st.session_state:
		st.session_state.rosters = {}
		session = Session()
		config = load_league_config()
		for league_name, league_info in config['leagues'].items():
			try:
				roster = get_roster_for_league(
					league_info['league_id'], league_info['team_id'], session
				)
				st.session_state.rosters[league_name] = roster
			except Exception as e:
				st.error(f"Error fetching roster for {league_name}: {e}")

	if not st.session_state.rosters:
		st.info("Click 'Refresh Data' to load rosters")
		return

	# League multiselect
	with st.sidebar:
		available_leagues = list(st.session_state.rosters.keys())
		selected_leagues = st.multiselect(
			"Select Leagues", options=available_leagues, default=available_leagues
		)

	# 3-up grid
	cols = st.columns(3)
	i = 0
	for league_name, roster in st.session_state.rosters.items():
		if league_name not in selected_leagues:
			continue
		html = roster_to_html(league_name, roster, starters_only=show_starters)
		with cols[i % 3]:
			st.components.v1.html(html, height=480, scrolling=True)
		i += 1


if __name__ == "__main__":
	main()
