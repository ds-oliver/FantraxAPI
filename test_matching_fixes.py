#!/usr/bin/env python3
"""
Test script to verify the player matching fixes work correctly.
"""

import sys
from pathlib import Path

# Add the scripts directory to the path so we can import the functions
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from update_player_mappings_v2 import (
    standardize_team, 
    normalize_name, 
    check_name_variations,
    find_matches
)

def test_team_standardization():
    """Test team code standardization."""
    print("=== Testing Team Code Standardization ===")
    
    # Test Brentford variations
    test_cases = [
        ("BRF", "BRE"),
        ("BRE", "BRE"), 
        ("brentford", "BRE"),
        ("Brentford", "BRE"),
        ("brf", "BRE"),
        ("bre", "BRE"),
    ]
    
    for input_team, expected in test_cases:
        result = standardize_team(input_team, {}, {})
        status = "✓" if result == expected else "✗"
        print(f"{status} {input_team} -> {result} (expected: {expected})")
    
    print()

def test_name_normalization():
    """Test name normalization."""
    print("=== Testing Name Normalization ===")
    
    test_cases = [
        ("Đorđe Petrović", "djordje petrovic"),
        ("Hamed Junior Traorè", "hamed junior traore"),
        ("Pape Matar Sarr", "pape matar sarr"),
        ("Reinildo Isnard Mandava", "reinildo isnard mandava"),
        ("Yehor Yarmolyuk", "yehor yarmolyuk"),
        ("Ehor Yarmolyuk", "ehor yarmolyuk"),
    ]
    
    for input_name, expected in test_cases:
        result = normalize_name(input_name)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{input_name}' -> '{result}' (expected: '{expected}')")
    
    print()

def test_name_variations():
    """Test name variation detection."""
    print("=== Testing Name Variation Detection ===")
    
    test_cases = [
        ("Pape Sarr", "Pape Matar Sarr", True),
        ("Reinildo Mandava", "Reinildo Isnard Mandava", True),
        ("Yehor Yarmolyuk", "Ehor Yarmolyuk", True),
        ("Đorđe Petrović", "Djordje Petrovic", True),
        ("Hamed Traore", "Hamed Junior Traorè", True),
        ("Igor Jesus", "Igor Jesus Maciel da Cruz", True),
        ("Joe Gomez", "Joseph Gomez", True),
        ("Toti", "Toti Gomes", True),
        ("John Smith", "Jane Doe", False),  # Should not match
    ]
    
    for name1, name2, expected in test_cases:
        result = check_name_variations(name1, name2)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{name1}' vs '{name2}' -> {result} (expected: {expected})")
    
    print()

def test_matching_algorithm():
    """Test the improved matching algorithm."""
    print("=== Testing Matching Algorithm ===")
    
    # Test candidates with team mismatches
    candidates = [
        ("Dango Ouattara", "BRE"),  # SofaScore
        ("Mikkel Damsgaard", "BRE"),  # SofaScore
        ("Đorđe Petrović", "BOU"),  # SofaScore
        ("Pape Matar Sarr", "TOT"),  # SofaScore
        ("Reinildo Mandava", "SUN"),  # SofaScore
    ]
    
    # Test Fantrax players
    test_cases = [
        ("Dango Ouattara", "BRF"),  # Should match with BRE (team variation)
        ("Mikkel Damsgaard", "BRF"),  # Should match with BRE (team variation)
        ("Djordje Petrovic", "BOU"),  # Should match with Đorđe Petrović (name variation)
        ("Pape Sarr", "TOT"),  # Should match with Pape Matar Sarr (name variation)
        ("Reinildo Isnard Mandava", "SUN"),  # Should match with Reinildo Mandava (name variation)
    ]
    
    for fantrax_name, fantrax_team in test_cases:
        print(f"\nTesting: {fantrax_name} ({fantrax_team})")
        matches = find_matches(fantrax_name, fantrax_team, candidates, {}, {})
        
        if matches:
            best_match = matches[0]
            print(f"  ✓ Best match: {best_match[0]} (score: {best_match[1]})")
        else:
            print(f"  ✗ No matches found")
    
    print()

def test_false_positive_prevention():
    """Test that we prevent false positive matches."""
    print("=== Testing False Positive Prevention ===")
    
    # Create proper team mappings for testing
    code_mappings = {
        "BRE": {"variations": ["brentford", "bre", "brf"]},
        "BUR": {"variations": ["burnley", "bur"]},
        "WHU": {"variations": ["west_ham", "whu", "wham"]},
        "ARS": {"variations": ["arsenal", "ars"]},
        "SEV": {"variations": ["sevilla", "sev"]},
    }
    
    club_mappings = {
        "BRE": {"long_name": "Brentford", "short_name": "BRE", "short_name_variations": ["BRF"]},
        "BUR": {"long_name": "Burnley", "short_name": "BUR"},
        "WHU": {"long_name": "West Ham United", "short_name": "WHU"},
        "ARS": {"long_name": "Arsenal", "short_name": "ARS"},
        "SEV": {"long_name": "Sevilla", "short_name": "SEV"},
    }
    
    # Test candidates that should NOT match
    candidates = [
        ("Gabriel", "ARS"),  # Single name Gabriel
        ("Walker", "BUR"),   # Single name Walker
        ("Jesus", "ARS"),    # Single name Jesus
        ("Kyle Walker", "BUR"),  # Different player than Kyle Walker-Peters
    ]
    
    # Test cases that should NOT match (false positives)
    test_cases = [
        ("Gabriel Jesus", "ARS"),      # Should NOT match to Gabriel
        ("Kyle Walker-Peters", "WHU"), # Should NOT match to Kyle Walker
        ("Jesus Navas", "SEV"),        # Should NOT match to Jesus
    ]
    
    for fantrax_name, fantrax_team in test_cases:
        print(f"\nTesting: {fantrax_name} ({fantrax_team})")
        matches = find_matches(fantrax_name, fantrax_team, candidates, code_mappings, club_mappings)
        
        if matches:
            best_match = matches[0]
            print(f"  ⚠️  Found match: {best_match[0]} (score: {best_match[1]}) - This might be a false positive!")
        else:
            print(f"  ✓ No matches found - correctly prevented false positive")
    
    print()

def main():
    """Run all tests."""
    print("Testing Player Matching Fixes\n")
    
    test_team_standardization()
    test_name_normalization()
    test_name_variations()
    test_matching_algorithm()
    test_false_positive_prevention()
    
    print("=== Test Summary ===")
    print("If you see mostly ✓ marks, the fixes are working correctly!")
    print("The matching algorithm should now properly handle:")
    print("- BRF ↔ BRE team code variations")
    print("- Special characters in names (Đ, è, ć)")
    print("- Name variations (Pape Sarr vs Pape Matar Sarr)")
    print("- Reduced penalties for high-confidence matches with team mismatches")
    print("- Prevention of false positives (Gabriel Jesus ≠ Gabriel)")

if __name__ == "__main__":
    main()
