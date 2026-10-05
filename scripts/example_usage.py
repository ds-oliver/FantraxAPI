#!/usr/bin/env python3
"""
Example usage of the enhanced lineup mapping functionality

This script demonstrates how to:
1. Map SofaScore lineups to Fantrax players
2. Get team-specific lineups for specific events
3. Use the schedule data for proper team context
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

def show_example_usage():
	"""Show example usage commands and explain the workflow"""
	
	print("=" * 80)
	print("ENHANCED LINEUP MAPPING - EXAMPLE USAGE")
	print("=" * 80)
	
	print("\n📋 WORKFLOW OVERVIEW:")
	print("1. Export SofaScore schedules and lineups using pipelines/export_schedule_and_lineups.py")
	print("2. Map the lineups to Fantrax players using map_lineups_to_fantrax.py")
	print("3. Get team-specific lineups for specific events")
	
	print("\n🚀 STEP 1: EXPORT SOFASCORE DATA")
	print("First, export the schedule and lineups from SofaScore:")
	print("python pipelines/export_schedule_and_lineups.py --tournament-id 17 --season '2024/2025' --with-lineups --limit 10")
	
	print("\n🔍 STEP 2: MAP LINEUPS TO FANTRAX")
	print("Then map the lineups to Fantrax players:")
	print("python scripts/map_lineups_to_fantrax.py --tournament-id 17 --season-id 76986 --mode last")
	
	print("\n🏟️ STEP 3: GET TEAM-SPECIFIC LINEUPS")
	print("Get lineup for a specific team in a specific event:")
	print("python scripts/map_lineups_to_fantrax.py --event-id 14025044 --team-name 'Liverpool'")
	
	print("\n📊 STEP 4: VALIDATE MAPPINGS")
	print("Validate existing mappings without creating new files:")
	print("python scripts/map_lineups_to_fantrax.py --validate-only")
	
	print("\n⚙️ KEY FEATURES:")
	print("• Loads schedule data for proper team context")
	print("• Maps SofaScore players to Fantrax using existing mappings + fuzzy matching")
	print("• Creates team-specific lineup structures")
	print("• Generates review CSVs for manual verification")
	print("• Validates lineup counts and match rates")
	
	print("\n📁 OUTPUT FILES:")
	print("• {event_id}_mapped.json - Full event lineup (both teams)")
	print("• {event_id}_{team_name}_lineup.json - Team-specific lineup")
	print("• lineup_mapping_review.csv - All suggestions for manual review")
	print("• lineup_mapping_best.csv - Best matches per player")
	
	print("\n🔧 CONFIGURATION:")
	print("• config.ini - Fantrax API credentials and league ID")
	print("• config/player_mappings.yaml - Existing player mappings")
	print("• data/sofascore/ - SofaScore export directory")
	print("• data/mapped_lineups/ - Output directory for mapped lineups")
	
	print("\n💡 TIPS:")
	print("• Use --min-match-score to control fuzzy matching quality")
	print("• Use --team-name to get lineups for specific teams")
	print("• Use --event-id to process specific events only")
	print("• Check the review CSV for any unmatched players")
	
	print("\n" + "=" * 80)

def show_sample_commands():
	"""Show sample commands for common use cases"""
	
	print("\n📝 SAMPLE COMMANDS:")
	print("-" * 50)
	
	print("\n1. Process all recent lineups:")
	print("python scripts/map_lineups_to_fantrax.py --tournament-id 17 --season-id 76986 --mode last")
	
	print("\n2. Get Liverpool lineup for specific event:")
	print("python scripts/map_lineups_to_fantrax.py --event-id 14025044 --team-name 'Liverpool'")
	
	print("\n3. Process upcoming fixtures:")
	print("python scripts/map_lineups_to_fantrax.py --tournament-id 17 --season-id 76986 --mode upcoming")
	
	print("\n4. High-quality matches only:")
	print("python scripts/map_lineups_to_fantrax.py --min-match-score 90")
	
	print("\n5. Validate existing mappings:")
	print("python scripts/map_lineups_to_fantrax.py --validate-only")

if __name__ == "__main__":
	show_example_usage()
	show_sample_commands()
