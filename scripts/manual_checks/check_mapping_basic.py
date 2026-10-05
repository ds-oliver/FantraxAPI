#!/usr/bin/env python3
"""
Basic test script for the lineup mapping functionality (no Fantrax connection required)
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from scripts.map_lineups_to_fantrax import (
	load_existing_mappings, 
	canon, 
	pick_best_matches,
	create_review_csv
)
import pandas as pd

def test_basic_functions():
	"""Test basic functions without requiring Fantrax connection"""
	print("Testing basic mapping functions...")
	
	# Test canonical name function
	test_names = [
		"Mohamed Salah",
		"Mohamed Salah Jr.",
		"Mohamed Salah III",
		"Mohamed Salah IV",
		"Mohamed Salah Sr.",
		"Mohamed Salah Jr",
		"Mohamed Salah III",
		"Mohamed Salah IV",
		"Mohamed Salah Sr",
		"Mohamed Salah II",
	]
	
	print("\nTesting canonical name function:")
	for name in test_names:
		canonical = canon(name)
		print(f"  '{name}' -> '{canonical}'")
	
	# Test loading existing mappings
	mappings_path = Path("config/player_mappings.yaml")
	if mappings_path.exists():
		id_mappings, alias_mappings = load_existing_mappings(mappings_path)
		print(f"\nLoaded {len(id_mappings)} existing ID mappings")
		print(f"Loaded {len(alias_mappings)} alias mappings")
		
		if id_mappings:
			print("Sample ID mappings:")
			for i, (sofa_id, fantrax_id) in enumerate(list(id_mappings.items())[:3]):
				print(f"  SofaScore {sofa_id} -> Fantrax {fantrax_id}")
		
		if alias_mappings:
			print("Sample alias mappings:")
			for i, (alias, fantrax_ids) in enumerate(list(alias_mappings.items())[:3]):
				print(f"  '{alias}' -> Fantrax IDs: {fantrax_ids}")
	else:
		print(f"\nNo mappings file found at {mappings_path}")
	
	# Test pick_best_matches function
	print("\nTesting pick_best_matches function:")
	test_data = [
		{'sofa_player_id': 1, 'sofa_name': 'Player A', 'priority': 1, 'match_score': 100},
		{'sofa_player_id': 1, 'sofa_name': 'Player A', 'priority': 2, 'match_score': 90},
		{'sofa_player_id': 2, 'sofa_name': 'Player B', 'priority': 3, 'match_score': 80},
		{'sofa_player_id': 2, 'sofa_name': 'Player B', 'priority': 1, 'match_score': 95},
	]
	test_df = pd.DataFrame(test_data)
	best_matches = pick_best_matches(test_df)
	print(f"Original suggestions: {len(test_df)} rows")
	print(f"Best matches: {len(best_matches)} rows")
	print("Best matches data:")
	for _, row in best_matches.iterrows():
		print(f"  Player {row['sofa_player_id']}: priority={row['priority']}, score={row['match_score']}")
	
	print("\nBasic function tests completed!")

if __name__ == "__main__":
	test_basic_functions()
