#!/usr/bin/env python
"""
Script to build player mappings starting from Fantrax data.
"""
import argparse
from pathlib import Path
import yaml

import pandas as pd

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

def load_scout_picks_data(data_dir: Path) -> pd.DataFrame:
	"""Load the most recent scout picks data."""
	# Find most recent parquet file
	files = list(data_dir.glob("scout_picks_rosters_*.parquet"))
	if not files:
		raise FileNotFoundError("No scout picks data found")
		
	latest = max(files, key=lambda p: p.stat().st_mtime)
	return pd.read_parquet(latest)

def build_mappings(league_id: str, data_dir: Path, output_file: Path) -> None:
	"""
	Build player mappings starting from Fantrax data.
	
	Args:
		league_id: Fantrax league ID
		data_dir: Directory containing data files
		output_file: Where to save the mappings YAML
	"""
	# Initialize API and mapping manager
	api = FantraxAPI(league_id)
	manager = PlayerMappingManager(str(output_file))
	
	# Get all Fantrax players
	print("Fetching players from Fantrax...")
	fantrax_players = api.get_all_players()
	print(f"Found {len(fantrax_players)} players in Fantrax")
	
	# Load FFScout data if available
	scout_df = None
	try:
		scout_df = load_scout_picks_data(data_dir / "silver/scout_picks")
		print(f"Loaded FFScout data with {len(scout_df)} players")
	except FileNotFoundError:
		print("No FFScout data found")
	
	# Process each Fantrax player
	for player in fantrax_players:
		# Create base mapping
		mapping = PlayerMapping(
			fantrax_id=player.id,
			fantrax_name=player.name,
			sofascore_id=None,
			sofascore_name=None,
			ffscout_name=None,
			other_names=[]
		)
		
		# Add name variations
		if player.first_name and player.last_name:
			mapping.other_names.extend([
				f"{player.first_name} {player.last_name}",
				f"{player.last_name}, {player.first_name}",
				player.last_name,
				f"{player.first_name[0]}. {player.last_name}"
			])
		
		# Try to match with FFScout data
		if scout_df is not None:
			# Try exact match on full name
			scout_match = scout_df[
				(scout_df["player_full_from_title"] == player.name) |
				(scout_df["player_display"] == player.name)
			]
			
			if not scout_match.empty:
				row = scout_match.iloc[0]
				mapping.ffscout_name = row["player_full_from_title"]
				if row["player_display"] != row["player_full_from_title"]:
					mapping.other_names.append(row["player_display"])
		
		# Remove duplicates from other names
		mapping.other_names = list(set(mapping.other_names))
		
		# Add to manager
		manager.add_mapping(mapping)
		
	print(f"\nCreated mappings for {len(fantrax_players)} players")
	print(f"Saved to: {output_file}")

def main():
	parser = argparse.ArgumentParser(description="Build player mappings from Fantrax data")
	parser.add_argument(
		"--league-id",
		type=str,
		required=True,
		help="Fantrax league ID"
	)
	parser.add_argument(
		"--data-dir",
		type=str,
		default="data",
		help="Directory containing data files"
	)
	parser.add_argument(
		"--output",
		type=str,
		default="config/player_mappings.yaml",
		help="Output YAML file for mappings"
	)
	args = parser.parse_args()
	
	build_mappings(args.league_id, Path(args.data_dir), Path(args.output))

if __name__ == "__main__":
	main()
