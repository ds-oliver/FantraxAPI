#!/usr/bin/env python3
"""
Test script for the complete mapping pipeline with mock data
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from scripts.map_lineups_to_fantrax import (
	load_existing_mappings, 
	canon, 
	suggest_matches,
	pick_best_matches,
	create_review_csv,
	validate_lineup_counts,
	create_lineup_change_structure
)
import pandas as pd

def test_complete_pipeline():
	"""Test the complete mapping pipeline with mock data"""
	print("Testing complete mapping pipeline with mock data...")
	
	# Load existing mappings
	mappings_path = Path("config/player_mappings.yaml")
	if not mappings_path.exists():
		print("✗ No mappings file found")
		return
	
	id_mappings, alias_mappings = load_existing_mappings(mappings_path)
	print(f"✓ Loaded {len(id_mappings)} ID mappings and {len(alias_mappings)} alias mappings")
	
	# Create mock SofaScore lineup data
	mock_sofa_data = [
		{
			"event_id": 14025044,
			"team_side": "home",
			"role": "starter",
			"sofa_player_id": 1085180,	# Brennan Johnson (should match existing mapping)
			"sofa_name": "Brennan Johnson",
			"sofa_team_id": 33,
			"sofa_pos": "F",
			"jersey_number": 9,
			"confirmed": True,
			"sofa_name_canon": canon("Brennan Johnson")
		},
		{
			"event_id": 14025044,
			"team_side": "home",
			"role": "starter",
			"sofa_player_id": 999999,  # Unknown player (should use fuzzy matching)
			"sofa_name": "John Smith",
			"sofa_team_id": 33,
			"sofa_pos": "M",
			"jersey_number": 10,
			"confirmed": True,
			"sofa_name_canon": canon("John Smith")
		},
		{
			"event_id": 14025044,
			"team_side": "away",
			"role": "sub",
			"sofa_player_id": 99090,  # Dan Burn (should match existing mapping)
			"sofa_name": "Dan Burn",
			"sofa_team_id": 39,
			"sofa_pos": "D",
			"jersey_number": 33,
			"confirmed": True,
			"sofa_name_canon": canon("Dan Burn")
		}
	]
	
	mock_sofa_df = pd.DataFrame(mock_sofa_data)
	print(f"✓ Created mock SofaScore data with {len(mock_sofa_df)} players")
	
	# Create mock Fantrax player data
	mock_fx_data = [
		{
			"id": "061ya",
			"name": "Brennan Johnson",
			"team": "TOT",
			"pos": "F",
			"name_canon": canon("Brennan Johnson")
		},
		{
			"id": "02lsa",
			"name": "Dan Burn",
			"team": "NEW",
			"pos": "D",
			"name_canon": canon("Dan Burn")
		},
		{
			"id": "test123",
			"name": "John Smith",
			"team": "TOT",
			"pos": "M",
			"name_canon": canon("John Smith")
		},
		{
			"id": "test456",
			"name": "Johnny Smith",
			"team": "TOT",
			"pos": "M",
			"name_canon": canon("Johnny Smith")
		}
	]
	
	mock_fx_df = pd.DataFrame(mock_fx_data)
	print(f"✓ Created mock Fantrax data with {len(mock_fx_df)} players")
	
	# Test the complete pipeline
	print("\n--- Testing Complete Pipeline ---")
	
	# 1. Generate matches
	print("\n1. Generating matches...")
	matches_df = suggest_matches(mock_sofa_df, mock_fx_df, id_mappings, alias_mappings)
	print(f"   Generated {len(matches_df)} total suggestions")
	
	# 2. Create review CSV
	print("\n2. Creating review CSV...")
	output_dir = Path("data/test_output")
	review_df, best_matches = create_review_csv(matches_df, output_dir)
	
	# 3. Pick best matches
	print("\n3. Picking best matches...")
	best_matches = pick_best_matches(matches_df)
	print(f"   Selected {len(best_matches)} best matches")
	
	# 4. Validate lineup counts
	print("\n4. Validating lineup counts...")
	is_valid, validation_info = validate_lineup_counts(mock_sofa_df, best_matches)
	print(f"   Validation result: {'✓ PASS' if is_valid else '✗ FAIL'}")
	print(f"   Match rate: {validation_info.get('match_rate', 0):.1%}")
	
	# 5. Create lineup change structure
	print("\n5. Creating lineup change structure...")
	lineup_structure = create_lineup_change_structure(best_matches)
	print(f"   Created structure for event {lineup_structure.get('event_id', 'unknown')}")
	
	# Print summary
	print(f"\n{'='*50}")
	print("PIPELINE TEST SUMMARY")
	print(f"{'='*50}")
	print(f"Input players: {len(mock_sofa_df)}")
	print(f"Total suggestions: {len(matches_df)}")
	print(f"Best matches: {len(best_matches)}")
	print(f"Validation: {'PASS' if is_valid else 'FAIL'}")
	print(f"Output files: {output_dir}")
	
	print("\n✓ Complete pipeline test completed!")

if __name__ == "__main__":
	test_complete_pipeline()
