#!/usr/bin/env python3

"""
Test script for ScraperFC package to fetch Premier League match data.
"""

from ScraperFC.sofascore import Sofascore
import json
from datetime import datetime

def main():
    # Initialize the Sofascore object
    print("Initializing Sofascore scraper...")
    sofascore = Sofascore()
    
    print(f"Fetching Premier League matches for 2023-2024 season...")
    
    try:
        print("Calling get_match_dicts with year='2025' and league='EPL'...")
        # Get match dicts for Premier League season 2023-2024
        matches = sofascore.get_match_dicts('2025', 'EPL')
        
        print(f"Raw matches data type: {type(matches)}")
        print(f"Raw matches data: {matches}")

        # Flatten the list of match dicts
        matches = [m for match_list in matches for m in match_list]
        print(f"Flattened matches length: {len(matches)}")

        if not matches:
            print("\nNo matches found. This could mean either:")
            print("1. The season data is not yet available")
            print("2. The league code might have changed")
            print("3. The API structure might have been updated")
            return

        # Print the first match with nice formatting
        print("\nFirst match details:")
        print(json.dumps(matches[0], indent=2))

        # Print total number of matches found
        print(f"\nTotal matches found: {len(matches)}")

        # Save the matches to a file with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"data/test_lineups/pl_matches_{timestamp}.json"
        
        with open(output_file, "w") as f:
            json.dump(matches, f, indent=2)
        
        print(f"\nSaved all matches to: {output_file}")

    except KeyError as e:
        print(f"\nError: Failed to fetch season data. The API response structure might have changed.")
        print(f"Specific error: {str(e)}")
        print(f"Full error details: {e}")
    except Exception as e:
        print(f"\nUnexpected error occurred: {str(e)}")
        print("\nValid leagues are: ['Champions League', 'Europa League', 'Europa Conference League',")
        print("                    'EPL', 'La Liga', 'Bundesliga', 'Serie A', 'Ligue 1',")
        print("                    'Turkish Super Lig', 'Argentina Liga Profesional',")
        print("                    'Argentina Copa de la Liga Profesional', 'Liga 1 Peru',")
        print("                    'Copa Libertadores', 'MLS', 'USL Championship', 'USL1',")
        print("                    'USL2', 'Saudi Pro League', 'World Cup', 'Euros',")
        print("                    'Gold Cup', \"Women's World Cup\"]")

if __name__ == "__main__":
    main()