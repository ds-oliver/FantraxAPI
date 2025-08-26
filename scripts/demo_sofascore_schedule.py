"""
Demo script to fetch and display the Premier League schedule for the current season using SofaScore.
"""
import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path

import pandas as pd

from fantraxapi.providers.sofascore.client import get_matches
from fantraxapi.providers.sofascore.discover import get_season_events

# Configure logging
logging.basicConfig(level=logging.INFO, 
				   format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def get_current_schedule():
	"""Fetch and format the current Premier League schedule."""
	# Get all season events
	logger.info("Fetching Premier League schedule for 2025-26 season...")
	events = await get_season_events(season="2025-26")
	
	# Convert events to DataFrame
	schedule_data = []
	for event in events:
		schedule_data.append({
			'date': datetime.fromtimestamp(event.start_timestamp, tz=timezone.utc),
			'home_team': event.home_team['name'],
			'away_team': event.away_team['name'],
			'game_id': event.event_id,
			'round': event.round_info.get('round', ''),
			'week': event.round,
			'home_score': event.home_score,
			'away_score': event.away_score
		})
	
	df = pd.DataFrame(schedule_data)
	
	# Sort by date
	df = df.sort_values('date')
	
	# Format date column
	df['date'] = df['date'].dt.strftime('%Y-%m-%d %H:%M UTC')
	
	return df

def save_schedule(df: pd.DataFrame, output_dir: Path):
	"""Save schedule to CSV and Parquet formats."""
	# Create output directory if it doesn't exist
	output_dir.mkdir(parents=True, exist_ok=True)
	
	# Generate filename with timestamp
	timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
	base_name = f'premier_league_schedule_{timestamp}'
	
	# Save as CSV
	csv_path = output_dir / f'{base_name}.csv'
	df.to_csv(csv_path, index=False)
	logger.info(f"Schedule saved to CSV: {csv_path}")
	
	# Save as Parquet
	parquet_path = output_dir / f'{base_name}.parquet'
	df.to_parquet(parquet_path, index=False)
	logger.info(f"Schedule saved to Parquet: {parquet_path}")

async def main():
	try:
		# Get schedule
		df = await get_current_schedule()
		
		# Display summary
		logger.info("\nSchedule Summary:")
		logger.info(f"Total matches: {len(df)}")
		logger.info(f"Date range: {df['date'].iloc[0]} to {df['date'].iloc[-1]}")
		
		# Display first few matches
		logger.info("\nUpcoming Matches:")
		pd.set_option('display.max_rows', 10)
		pd.set_option('display.max_columns', None)
		pd.set_option('display.width', None)
		print(df[['date', 'home_team', 'away_team', 'round']].head(5))
		
		# Save to files
		output_dir = Path('data/schedule')
		save_schedule(df, output_dir)
		
	except Exception as e:
		logger.error(f"Error fetching schedule: {e}")
		raise

if __name__ == "__main__":
	asyncio.run(main())
