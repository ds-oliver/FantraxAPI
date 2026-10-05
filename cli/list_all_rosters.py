#!/usr/bin/env python3
"""
Script to display all rosters from multiple Fantrax leagues side by side.
"""

import yaml
from typing import Dict, List
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster, RosterRow
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

def load_league_config() -> dict:
	"""Load league configuration from YAML."""
	with open('config/fantrax_leagues.yaml', 'r') as f:
		return yaml.safe_load(f)

def get_roster_for_league(league_id: str, team_id: str, session) -> Roster:
	"""Get roster for a specific league and team."""
	api = FantraxAPI(league_id=league_id, session=session)
	return api.roster_info(team_id)

def format_player_row(row: RosterRow) -> tuple:
	"""Format a roster row for display."""
	if row.player:
		pos = row.pos.short_name or "BN"
		name = row.player.name
		team = row.player.team_short_name or row.player.team_name or ""
		fppg = f"{row.fppg:.1f}" if row.fppg is not None else "-"
		return pos, name, team, fppg
	return "", "", "", ""

def display_rosters(rosters: Dict[str, Roster]):
	"""Display all rosters side by side using rich tables."""
	console = Console()
	
	# Create title
	console.print("\n[bold cyan]All League Rosters[/bold cyan]\n")

	# Create roster tables
	roster_tables = []
	for league_name, roster in rosters.items():
		table = Table(
			title=f"[bold]{league_name}[/bold]",
			box=box.SIMPLE,
			show_header=True,
			header_style="bold magenta"
		)
		table.add_column("Pos", justify="center", style="cyan", width=4)
		table.add_column("Player", style="white", width=20)
		table.add_column("Team", justify="center", style="green", width=5)
		table.add_column("FPPG", justify="right", style="yellow", width=5)

		# Add starters
		for row in roster.get_starters():
			pos, name, team, fppg = format_player_row(row)
			table.add_row(pos, name, team, fppg)

		# Add separator
		table.add_row("", "", "", "")

		# Add bench
		for row in roster.get_bench_players():
			pos, name, team, fppg = format_player_row(row)
			table.add_row(pos, name, team, fppg)

		roster_tables.append(table)

	# Create a table to hold all roster tables side by side
	main_table = Table(box=None, padding=1, show_header=False)
	
	# Calculate how many tables per row based on terminal width
	tables_per_row = 3	# Adjust this based on your needs
	
	# Add tables in rows
	for i in range(0, len(roster_tables), tables_per_row):
		row_tables = roster_tables[i:i + tables_per_row]
		main_table.add_row(*row_tables)

	console.print(main_table)

def main():
	"""Main function to display all rosters."""
	from requests import Session
	
	# Load configuration
	config = load_league_config()
	
	# Create session for reuse
	session = Session()
	
	# Get rosters for all leagues
	rosters = {}
	for league_name, league_info in config['leagues'].items():
		try:
			roster = get_roster_for_league(
				league_info['league_id'],
				league_info['team_id'],
				session
			)
			rosters[league_name] = roster
		except Exception as e:
			print(f"Error fetching roster for {league_name}: {e}")
	
	# Display rosters
	display_rosters(rosters)

if __name__ == "__main__":
	main()