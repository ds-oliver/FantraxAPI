"""
Main lineup automation functionality.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import logging
import asyncio

from ..fantrax import Fantrax
from ..player_mapping import PlayerMappingManager
from .models import LineupRecord, LineupStatus
from .normalize import normalize_lineup_data
from .fantrax_sync import LineupSynchronizer
from .testing import LineupTester

logger = logging.getLogger(__name__)

class LineupAutomation:
	"""
	Main lineup automation controller.
	
	This class:
	1. Monitors for new lineups
	2. Tracks lineup status
	3. Validates lineups
	4. Triggers synchronization
	5. Handles testing/dry runs
	"""
	
	def __init__(
		self,
		fantrax: Fantrax,
		player_mapping: PlayerMappingManager,
		output_dir: Path,
		dry_run: bool = False,
		test_mode: bool = False
	):
		"""
		Initialize automation controller.
		
		Args:
			fantrax: Fantrax API instance
			player_mapping: Player mapping manager
			output_dir: Directory for lineup storage
			dry_run: If True, don't make actual changes
			test_mode: If True, use preliminary lineups for testing
		"""
		self.fantrax = fantrax
		self.player_mapping = player_mapping
		self.output_dir = Path(output_dir)
		self.dry_run = dry_run
		self.test_mode = test_mode
		
		# Initialize components
		self.synchronizer = LineupSynchronizer(
			fantrax=fantrax,
			dry_run=dry_run,
			logger=logger
		)
		self.tester = LineupTester(
			output_dir=output_dir,
			player_mapping=player_mapping
		)
		
		# Track processed lineups
		self.processed_lineups: Dict[int, LineupRecord] = {}
		
	async def process_lineup(
		self,
		data: Dict,
		current_time: Optional[datetime] = None
	) -> bool:
		"""
		Process a new lineup.
		
		Args:
			data: Raw lineup data
			current_time: Optional current time override
			
		Returns:
			True if processing was successful
		"""
		if not current_time:
			current_time = datetime.now(timezone.utc)
			
		# Normalize lineup
		lineup = normalize_lineup_data(data, self.player_mapping)
		event_id = lineup.event_id
		
		# Check if we've seen this lineup before
		if event_id in self.processed_lineups:
			prev_lineup = self.processed_lineups[event_id]
			
			# Skip if no status change
			if prev_lineup.status == lineup.status:
				logger.debug(f"Lineup {event_id} status unchanged")
				return True
				
			# Skip if moving backwards (e.g. confirmed -> preliminary)
			if prev_lineup.is_confirmed and lineup.is_preliminary:
				logger.warning(f"Lineup {event_id} status went backwards")
				return False
		
		# Store lineup
		self.processed_lineups[event_id] = lineup
		
		# Handle based on mode
		if self.test_mode:
			# In test mode, process all lineups through tester
			result = self.tester.process_lineup(lineup)
			
			# Log differences if any
			if result.differences:
				logger.info(
					f"Found {len(result.differences)} differences in lineup {event_id}"
				)
				
			return True
			
		elif lineup.is_confirmed or (self.dry_run and lineup.is_preliminary):
			# Sync lineup
			success = self.synchronizer.sync_lineup(lineup)
			
			if not success:
				logger.error(f"Failed to sync lineup {event_id}")
				return False
				
			logger.info(f"Successfully synced lineup {event_id}")
			return True
			
		else:
			# Just store preliminary lineup
			logger.debug(f"Stored preliminary lineup {event_id}")
			return True
			
	async def run(
		self,
		poll_interval: int = 60,
		max_runtime: Optional[int] = None
	):
		"""
		Run the automation loop.
		
		Args:
			poll_interval: Seconds between polls
			max_runtime: Optional maximum runtime in seconds
		"""
		start_time = datetime.now()
		
		while True:
			try:
				# Check runtime
				if max_runtime:
					runtime = (datetime.now() - start_time).total_seconds()
					if runtime >= max_runtime:
						logger.info("Maximum runtime reached")
						break
				
				# TODO: Poll for new lineups
				# This would integrate with your existing lineup watching code
				
				# Wait for next poll
				await asyncio.sleep(poll_interval)
				
			except KeyboardInterrupt:
				logger.info("Stopping automation")
				break
				
			except Exception as e:
				logger.error(f"Error in automation loop: {e}")
				await asyncio.sleep(poll_interval)
		
		# Generate report if in test mode
		if self.test_mode:
			report_file = self.output_dir / "lineup_test_report.parquet"
			self.tester.save_report(report_file)
			logger.info(f"Test report saved to {report_file}")
			
			stats = self.tester.get_accuracy_stats()
			logger.info("Test Results:")
			for key, value in stats.items():
				logger.info(f"	{key}: {value}")
				
		# Log final stats
		logger.info(f"Processed {len(self.processed_lineups)} lineups")
		if not self.test_mode:
			logger.info(f"Made {len(self.synchronizer.changes)} lineup changes")
			if self.synchronizer.errors:
				logger.warning(f"Encountered {len(self.synchronizer.errors)} errors")
