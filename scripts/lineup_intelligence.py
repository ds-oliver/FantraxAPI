#!/usr/bin/env python
"""
Lineup Intelligence CLI Tool

Analyzes lineup predictions and provides actionable insights for:
- Active/reserve swaps
- Waiver claims
- Free agent pickups
- Drop candidates

Separate from automatic sync - this is for planning and decision-making.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import logging

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fantraxapi import FantraxAPI
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.lineups.intelligence import (
	LineupIntelligenceAnalyzer,
	ActionType,
	PlayerStatus
)
from fantraxapi.providers.sofascore.discover import get_watchlist, get_season_events
from fantraxapi.providers.sofascore.poll import poll_events
from fantraxapi.lineups.sofascore_normalize import normalize_lineup_data

console = Console()


def setup_logging(verbose: bool = False):
	"""Setup logging configuration."""
	level = logging.DEBUG if verbose else logging.INFO
	logging.basicConfig(
		level=level,
		format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
		handlers=[
			logging.FileHandler('data/logs/lineup_intelligence.log'),
			logging.StreamHandler(sys.stdout)
		]
	)


async def fetch_latest_lineups(days_ahead: int = 7) -> list:
	"""Fetch latest lineup predictions from SofaScore."""
	console.print(f"\n[cyan]Fetching lineup predictions for next {days_ahead} days...[/cyan]")
	
	# Get matches in date range
	events = await get_season_events(verbose=False)
	
	# Filter for upcoming matches
	now = datetime.now(timezone.utc)
	upcoming = [
		e for e in events
		if now <= e.kickoff_utc <= now + timedelta(days=days_ahead)
	]
	
	if not upcoming:
		console.print("[yellow]No upcoming matches found[/yellow]")
		return []
	
	console.print(f"Found {len(upcoming)} upcoming matches")
	
	# Try to get lineups (may not be available yet)
	results = []
	for event in upcoming:
		try:
			from fantraxapi.providers.sofascore.client import get_match_lineups
			lineup = await get_match_lineups(event.event_id)
			if lineup:
				results.append(lineup)
		except Exception as e:
			logging.debug(f"No lineup yet for event {event.event_id}: {e}")
	
	console.print(f"Retrieved {len(results)} lineups")
	return results


def display_recommendations(recommendations: list, limit: int = 20):
	"""Display recommendations in a nice table."""
	if not recommendations:
		console.print("\n[green]✓ No actions recommended - your lineup looks good![/green]")
		return
	
	# Group by action type
	by_type = {}
	for rec in recommendations:
		action_type = rec.action_type.value
		if action_type not in by_type:
			by_type[action_type] = []
		by_type[action_type].append(rec)
	
	# Display each group
	for action_type, recs in by_type.items():
		# Create table
		table = Table(
			title=f"{action_type.replace('_', ' ').title()} Recommendations",
			box=box.ROUNDED,
			show_header=True,
			header_style="bold magenta"
		)
		
		table.add_column("Priority", style="cyan", width=8, justify="center")
		table.add_column("Player", style="white", width=20)
		table.add_column("Team", style="green", width=8, justify="center")
		table.add_column("Status", style="yellow", width=18)
		table.add_column("Confidence", style="cyan", width=10, justify="right")
		table.add_column("Reason", style="white")
		
		if action_type in ("swap_to_active", "swap_to_reserve"):
			table.add_column("Swap With", style="magenta", width=20)
		
		# Add rows
		for rec in recs[:limit]:
			player = rec.player
			
			# Status icon
			if player.is_on_roster:
				status_icon = "📋"
			elif player.is_available:
				status_icon = "🟢"
			else:
				status_icon = "🔴"
			
			# Format status
			status_text = f"{status_icon} {player.status.value.replace('_', ' ').title()}"
			
			row = [
				f"P{rec.priority}",
				player.player_name,
				player.team_name or "-",
				status_text,
				f"{player.confidence:.0%}",
				rec.reason
			]
			
			if action_type in ("swap_to_active", "swap_to_reserve"):
				swap_with = rec.swap_with.player_name if rec.swap_with else "-"
				row.append(swap_with)
			
			# Color by priority
			if rec.priority <= 2:
				style = "bold red"
			elif rec.priority <= 4:
				style = "yellow"
			else:
				style = "white"
			
			table.add_row(*row, style=style)
		
		console.print("\n")
		console.print(table)


def display_matchday_summary(df):
	"""Display matchday summary table."""
	if df.empty:
		console.print("\n[yellow]No lineup data available[/yellow]")
		return
	
	table = Table(
		title="Upcoming Lineup Summary",
		box=box.ROUNDED,
		show_header=True,
		header_style="bold magenta"
	)
	
	for col in df.columns:
		table.add_column(col, style="white")
	
	# Group by kickoff time
	if "Kickoff" in df.columns:
		df = df.sort_values("Kickoff")
	
	for _, row in df.iterrows():
		table.add_row(*[str(val) for val in row.values])
	
	console.print("\n")
	console.print(table)


async def analyze_lineup_intelligence(
	league_id: str,
	team_id: str,
	days_ahead: int = 7,
	min_confidence: float = 0.7,
	output_dir: Path = Path("data/lineup_intelligence")
):
	"""Main analysis workflow."""
	output_dir.mkdir(parents=True, exist_ok=True)
	
	# Initialize components
	console.print("\n[cyan]Initializing...[/cyan]")
	api = FantraxAPI(league_id=league_id)
	player_mapping = PlayerMappingManager("config/player_mappings.yaml")
	
	analyzer = LineupIntelligenceAnalyzer(
		fantrax_api=api,
		player_mapping=player_mapping,
		lineup_data_dir=Path("data/lineups")
	)
	
	# Fetch latest lineups
	lineup_data = await fetch_latest_lineups(days_ahead=days_ahead)
	
	if not lineup_data:
		console.print("\n[yellow]⚠ No lineup data available yet[/yellow]")
		console.print("Lineups are typically released 60-75 minutes before kickoff")
		return
	
	# Normalize lineup data
	console.print("\n[cyan]Analyzing lineup predictions...[/cyan]")
	all_records = []
	for data in lineup_data:
		records = normalize_lineup_data(data, player_mapping)
		all_records.extend(records)
	
	console.print(f"Processed {len(all_records)} player records")
	
	# Build intelligence
	console.print("\n[cyan]Building intelligence map...[/cyan]")
	intelligence_map = analyzer.build_intelligence(all_records, team_id=team_id)
	
	console.print(f"Intelligence data for {len(intelligence_map)} players")
	
	# Generate recommendations
	console.print("\n[cyan]Generating recommendations...[/cyan]")
	recommendations = analyzer.generate_recommendations(
		intelligence_map,
		min_confidence=min_confidence
	)
	
	# Display results
	console.print("\n" + "="*80)
	console.print(Panel.fit(
		"[bold cyan]Lineup Intelligence Report[/bold cyan]",
		border_style="cyan"
	))
	console.print("="*80)
	
	# Show summary stats
	roster_count = sum(1 for p in intelligence_map.values() if p.is_on_roster)
	starting_count = sum(1 for p in intelligence_map.values() if p.is_starting)
	bench_count = sum(1 for p in intelligence_map.values() if p.is_on_bench)
	available_starters = sum(
		1 for p in intelligence_map.values()
		if p.is_available and p.is_likely_starter
	)
	
	console.print(f"\n[bold]Your Roster:[/bold]")
	console.print(f"  • Total players: {roster_count}")
	console.print(f"  • Starting: {starting_count}")
	console.print(f"  • On bench: {bench_count}")
	console.print(f"\n[bold]Available:[/bold]")
	console.print(f"  • Predicted starters: {available_starters}")
	
	# Display recommendations
	display_recommendations(recommendations, limit=30)
	
	# Display matchday summary
	df = analyzer.get_matchday_summary(intelligence_map)
	# Filter to only rostered players
	if not df.empty:
		rostered_df = df[df["On Roster"] == "✓"]
		if not rostered_df.empty:
			display_matchday_summary(rostered_df)
	
	# Export recommendations
	if recommendations:
		timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
		csv_path = output_dir / f"recommendations_{timestamp}.csv"
		analyzer.export_recommendations(recommendations, csv_path)
		console.print(f"\n[green]✓ Exported recommendations to {csv_path}[/green]")
	
	# Export summary
	if not df.empty:
		summary_path = output_dir / f"matchday_summary_{timestamp}.csv"
		df.to_csv(summary_path, index=False)
		console.print(f"[green]✓ Exported summary to {summary_path}[/green]")


def parse_args():
	parser = argparse.ArgumentParser(
		description="Lineup Intelligence - Analyze predictions and get actionable insights"
	)
	
	parser.add_argument(
		"--league-id",
		required=True,
		help="Fantrax league ID"
	)
	
	parser.add_argument(
		"--team-id",
		required=True,
		help="Your Fantrax team ID"
	)
	
	parser.add_argument(
		"--days-ahead",
		type=int,
		default=7,
		help="Look ahead N days for matches (default: 7)"
	)
	
	parser.add_argument(
		"--min-confidence",
		type=float,
		default=0.7,
		help="Minimum confidence threshold for recommendations (default: 0.7)"
	)
	
	parser.add_argument(
		"--output-dir",
		type=str,
		default="data/lineup_intelligence",
		help="Output directory for reports (default: data/lineup_intelligence)"
	)
	
	parser.add_argument(
		"-v", "--verbose",
		action="store_true",
		help="Verbose logging"
	)
	
	return parser.parse_args()


async def main():
	args = parse_args()
	
	setup_logging(args.verbose)
	
	try:
		await analyze_lineup_intelligence(
			league_id=args.league_id,
			team_id=args.team_id,
			days_ahead=args.days_ahead,
			min_confidence=args.min_confidence,
			output_dir=Path(args.output_dir)
		)
	
	except KeyboardInterrupt:
		console.print("\n[yellow]Interrupted by user[/yellow]")
	
	except Exception as e:
		console.print(f"\n[red]Error: {e}[/red]")
		if args.verbose:
			import traceback
			traceback.print_exc()
		sys.exit(1)


if __name__ == "__main__":
	asyncio.run(main())

