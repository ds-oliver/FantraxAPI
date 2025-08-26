#!/usr/bin/env python3
"""
Fetch SofaScore lineups (starters, substitutes, and missing players)
using EasySoccerData.

Usage:
  python esd_lineups_demo.py --event-id 14025088
Optional:
  --browser-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
Returns:
# This script generates the following outputs:
# 1. Prints to the terminal:
#	 - The formation, starters, substitutes, and missing players for each team involved in the event.
#	 - Each player's name, position, captain status, and statistics (rating and minutes played) if available.
#	 - Missing players' names and the reason code for their absence.
#
# 2. Saves files:
#	 - None. This script is designed to fetch and display lineups but does not save any data to files.
"""

import argparse
import esd


def fmt_player_line(pl):
	"""Format a PlayerLineup row safely."""
	name = getattr(pl.info, "name", "Unknown")
	pos	 = getattr(pl.info, "position", "?")
	cap	 = " (C)" if getattr(pl, "captain", False) else ""
	# Ratings/minutes only if stats present
	st	 = getattr(pl, "statistics", None)
	stat_bits = []
	if st and getattr(st, "rating", None) is not None:
		stat_bits.append(f"rating {st.rating}")
	if st and getattr(st, "minutes_played", None) is not None:
		stat_bits.append(f"{st.minutes_played}′")
	stats_suffix = f" | {' · '.join(stat_bits)}" if stat_bits else ""
	return f"- {name} ({pos}){cap}{stats_suffix}"


def fmt_missing(mp):
	"""Format a MissingPlayer row safely."""
	name = getattr(mp.player, "name", "Unknown")
	# ESD exposes only an integer 'reason'. We'll show the code; you can map it if needed.
	reason = getattr(mp, "reason", None)
	reason_txt = f"reason={reason}" if reason is not None else "reason=?"
	return f"- {name} ({reason_txt})"


def print_team_lineup(team, label):
	"""Print formation, starters, subs, and missing players for one side."""
	print(f"\n{label}:")
	if not team:
		print("	 (no data)")
		return

	formation = team.formation or "N/A"
	starters  = [p for p in (team.players or []) if not getattr(p, "substitute", False)]
	subs	  = [p for p in (team.players or []) if getattr(p, "substitute", False)]
	missing	  = list(team.missing_players or [])

	print(f"Formation: {formation}")
	print(f"\nStarters ({len(starters)}):")
	for pl in starters:
		print(fmt_player_line(pl))

	if subs:
		print(f"\nSubstitutes ({len(subs)}):")
		for pl in subs:
			print(fmt_player_line(pl))
	else:
		print("\nSubstitutes: none listed")

	if missing:
		print(f"\nMissing players ({len(missing)}):")
		for mp in missing:
			print(fmt_missing(mp))
	else:
		print("\nMissing players: none listed")


def fetch_lineups(event_id: int, browser_path: str | None):
	print("Initializing EasySoccerData (SofascoreClient)...")
	client_kwargs = {}
	if browser_path:
		client_kwargs["browser_path"] = browser_path
	client = esd.SofascoreClient(**client_kwargs)

	print(f"Fetching lineups for event ID: {event_id} ...")
	lineups = client.get_match_lineups(event_id)
	if not lineups:
		print("\nNo lineups found. Either the event ID is wrong or data isn’t available yet.")
		return

	print("\nLineups details:")
	print(f"Confirmed: {getattr(lineups, 'confirmed', False)}")

	print_team_lineup(getattr(lineups, "home", None), "Home team")
	print_team_lineup(getattr(lineups, "away", None), "Away team")


def main():
	parser = argparse.ArgumentParser(description="Fetch SofaScore lineups using EasySoccerData.")
	parser.add_argument("--event-id", type=int, required=True, help="SofaScore event ID")
	parser.add_argument(
		"--browser-path",
		type=str,
		default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
		help="Path to Chrome (or Chromium). If omitted and your system PATH has a browser, ESD may still work.",
	)
	args = parser.parse_args()
	fetch_lineups(args.event_id, args.browser_path)


if __name__ == "__main__":
	main()
