#!/usr/bin/env python
"""
Script to help build player mappings from various data sources.
"""
import argparse
from pathlib import Path

import pandas as pd
import yaml

from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

def load_scout_picks_data(data_dir: Path) -> pd.DataFrame:
	"""Load the most recent scout picks data."""
	# Find most recent parquet file
	files = list(data_dir.glob("scout_picks_rosters_*.parquet"))
	if not files:
		raise FileNotFoundError("No scout picks data found")
		
	latest = max(files, key=lambda p: p.stat().st_mtime)
	return pd.read_parquet(latest)

def build_mappings(data_dir: Path, output_file: Path) -> None:
	"""
	Build player mappings from available data sources.
	
	Args:
		data_dir: Directory containing data files
		output_file: Where to save the mappings YAML
	"""
	# Load existing mappings if any
	manager = PlayerMappingManager(str(output_file))
	
	# Load scout picks data
	scout_df = load_scout_picks_data(data_dir / "silver/scout_picks")
	
	# Process each player
	for _, row in scout_df.iterrows():
		# Get the full name if available, otherwise use display name
		name = row["player_full_from_title"] or row["player_display"]
		if not name:
			continue
			
		# Create mapping entry
		mapping = PlayerMapping(
			fantrax_id="TBD",  # Need to get from Fantrax
			fantrax_name="TBD",	 # Need to get from Fantrax
			sofascore_id=None,	# Will come from SofaScore integration
			sofascore_name=None,  # Will come from SofaScore integration
			ffscout_name=name,
			other_names=[]
		)
		
		# Add any alternate names
		if row["player_display"] and row["player_display"] != name:
			mapping.other_names.append(row["player_display"])
			
		print(f"Found player: {name}")
		print(f"  Display name: {row['player_display']}")
		print(f"  Team: {row['team_name']}")
		print(f"  Position: {row['position']}")
		print("	 Enter Fantrax ID (or press Enter to skip):")
		fantrax_id = input().strip()
		if not fantrax_id:
			continue
			
		print("	 Enter Fantrax name:")
		fantrax_name = input().strip()
		if not fantrax_name:
			continue
			
		# Update mapping with Fantrax info
		mapping.fantrax_id = fantrax_id
		mapping.fantrax_name = fantrax_name
		
		# Add to manager
		manager.add_mapping(mapping)
		print("	 Added mapping\n")

def main():
	parser = argparse.ArgumentParser(description="Build player mappings from data sources")
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
	
	build_mappings(Path(args.data_dir), Path(args.output))

if __name__ == "__main__":
	main()
