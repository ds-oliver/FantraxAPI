#!/usr/bin/env python3
"""
Test script for the lineup mapping functionality
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from scripts.map_lineups_to_fantrax import (
	load_existing_mappings, 
	canon, 
	suggest_matches,
	validate_lineup_counts
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
		"Mohamed Salah II",
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
		mappings = load_existing_mappings(mappings_path)
		print(f"\nLoaded {len(mappings)} existing mappings")
		if mappings:
			print("Sample mappings:")
			for i, (sofa_id, fantrax_id) in enumerate(list(mappings.items())[:5]):
				print(f"  SofaScore {sofa_id} -> Fantrax {fantrax_id}")
	else:
		print(f"\nNo mappings file found at {mappings_path}")
	
	print("\nBasic function tests completed!")

if __name__ == "__main__":
	test_basic_functions()
