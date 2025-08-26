#!/usr/bin/env python3
"""
Minimal demo: fetch SofaScore lineups using EasySoccerData.

Usage:
  python esd_lineups_demo.py --event-id 14025088
"""

import esd
import json
import argparse


def fetch_lineups(event_id):
	# Initialize the EasySoccerData object
	print("Initializing EasySoccerData...")
	client = esd.SofascoreClient(browser_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

	print(f"Fetching lineups for event ID: {event_id}...")
	
	try:
		# Fetch lineups for the given event ID
		lineups = client.get_match_lineups(event_id)
		
		if not lineups:
			print("\nNo lineups found. This could mean either:")
			print("1. The event ID is incorrect")
			print("2. The data is not yet available")
			return

		# Print the lineups with nice formatting
		print("\nLineups details:")
		print(f"Confirmed: {lineups.confirmed}")
		
		if lineups.home:
			print("\nHome team:")
			print(f"Formation: {lineups.home.formation}")
			print("\nStarters:")
			for player in [p for p in lineups.home.players if not p.substitute]:
				print(f"- {player.info.name} ({player.info.position})")
			print("\nSubstitutes:")
			for player in [p for p in lineups.home.players if p.substitute]:
				print(f"- {player.info.name} ({player.info.position})")
				
		if lineups.away:
			print("\nAway team:")
			print(f"Formation: {lineups.away.formation}")
			print("\nStarters:")
			for player in [p for p in lineups.away.players if not p.substitute]:
				print(f"- {player.info.name} ({player.info.position})")
			print("\nSubstitutes:")
			for player in [p for p in lineups.away.players if p.substitute]:
				print(f"- {player.info.name} ({player.info.position})")

	except KeyError as e:
		print(f"\nError: Failed to fetch lineups data. The API response structure might have changed.")
		print(f"Specific error: {str(e)}")
	except Exception as e:
		print(f"\nUnexpected error occurred: {str(e)}")

def main():
	parser = argparse.ArgumentParser(description="Fetch SofaScore lineups using EasySoccerData.")
	parser.add_argument('--event-id', type=int, required=True, help='The event ID for which to fetch lineups')
	args = parser.parse_args()

	fetch_lineups(args.event_id)

if __name__ == "__main__":
	main()
