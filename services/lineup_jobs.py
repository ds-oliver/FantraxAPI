# services/lineup_jobs.py
import logging
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.user_manager import UserManager
from utils.auth_helpers import validate_cookies
from fantraxapi.lineups.fantrax_sync import LineupSynchronizer
import requests

logger = logging.getLogger(__name__)

# Initialize managers
user_mgr = UserManager()


def lineup_sync_job(user_id: str, league_id: str) -> None:
	"""
	Periodic job to sync confirmed lineups to Fantrax for one league.
	
	Args:
		user_id: User identifier
		league_id: Fantrax league ID
	"""
	logger.info(f"Starting lineup sync for user={user_id} league={league_id}")
	
	# Load user's cookies
	artifacts = user_mgr.load_user_cookies(user_id)
	if not artifacts:
		logger.warning(f"No auth artifacts for user {user_id}, marking auth as stale")
		user_mgr.update_user_auth_status(user_id, "expired")
		return
	
	# Build session from cookies
	session = requests.Session()
	cookies = artifacts.get("cookies", [])
	for cookie in cookies:
		session.cookies.set(
			name=cookie["name"],
			value=cookie["value"],
			domain=cookie.get("domain", ".fantrax.com"),
			path=cookie.get("path", "/"),
		)
	
	# Copy local storage if present
	local_storage = artifacts.get("local_storage", {})
	
	# Validate session is still valid
	status = validate_cookies(session)
	if not status.get("valid"):
		logger.warning(f"Auth invalid for user {user_id}, marking stale")
		user_mgr.update_user_auth_status(user_id, "expired")
		return
	
	# Run lineup sync
	sync = LineupSynchronizer(
		session=session,
		user_id=user_id,
		league_id=league_id
	)
	
	try:
		changes = sync.determine_changes()
		if not changes:
			logger.info(f"No lineup changes for user={user_id} league={league_id}")
			return
		
		sync.execute_changes(changes)
		logger.info(
			f"Executed {len(changes)} lineup changes for user={user_id} league={league_id}"
		)
		
	except Exception as e:
		logger.exception(f"Error during lineup sync for user {user_id}: {e}")


def league_sync_job(user_id: str, league_id: str) -> None:
	"""
	Periodic job to sync league data (rosters, schedules, standings).
	
	Args:
		user_id: User identifier
		league_id: Fantrax league ID
	"""
	logger.info(f"Starting league sync for user={user_id} league={league_id}")
	
	# Load user's cookies
	artifacts = user_mgr.load_user_cookies(user_id)
	if not artifacts:
		logger.warning(f"No auth artifacts for user {user_id}")
		user_mgr.update_user_auth_status(user_id, "expired")
		return
	
	# Build session
	session = requests.Session()
	cookies = artifacts.get("cookies", [])
	for cookie in cookies:
		session.cookies.set(
			name=cookie["name"],
			value=cookie["value"],
			domain=cookie.get("domain", ".fantrax.com"),
			path=cookie.get("path", "/"),
		)
	
	# Validate session
	status = validate_cookies(session)
	if not status.get("valid"):
		logger.warning(f"Auth invalid for user {user_id}")
		user_mgr.update_user_auth_status(user_id, "expired")
		return
	
	try:
		# TODO: Implement actual league sync logic
		# For now, just log that we would sync
		logger.info(f"League sync completed for user={user_id} league={league_id}")
		
	except Exception as e:
		logger.exception(f"Error during league sync for user {user_id}: {e}")

