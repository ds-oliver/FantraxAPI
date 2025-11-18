# services/job_manager.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from typing import Dict, Any, Callable
import logging

logger = logging.getLogger(__name__)

# Single global scheduler instance
scheduler = BackgroundScheduler()
scheduler.start()
logger.info("APScheduler background scheduler started")

# In-memory tracking; replace with DB table in real deployment
JOB_REGISTRY: Dict[str, Dict[str, Any]] = {}


def _job_id(user_id: str, job_name: str) -> str:
	"""Generate a unique job ID for a user/job combination."""
	return f"{user_id}:{job_name}"


def register_job(
	user_id: str,
	job_name: str,
	func: Callable,
	minutes: int,
	**kwargs,
) -> None:
	"""
	Register or update a periodic job for a user.
	
	Args:
		user_id: User identifier
		job_name: Name of the job (e.g., "lineup_sync", "league_sync")
		func: Function to call periodically
		minutes: Interval in minutes between job runs
		**kwargs: Additional keyword arguments to pass to the function
	"""
	job_id = _job_id(user_id, job_name)
	
	# Remove existing job if present
	if scheduler.get_job(job_id):
		scheduler.remove_job(job_id)
		logger.info(f"Removed existing job {job_id}")
	
	trigger = IntervalTrigger(minutes=minutes)
	job = scheduler.add_job(
		func,
		trigger=trigger,
		id=job_id,
		kwargs={"user_id": user_id, **kwargs},
		replace_existing=True,
		max_instances=1,
		coalesce=True,
	)
	
	JOB_REGISTRY[job_id] = {
		"user_id": user_id,
		"job_name": job_name,
		"interval_minutes": minutes,
		"next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
	}
	
	logger.info(f"Registered job {job_name} for user {user_id} (every {minutes} min)")


def deactivate_job(user_id: str, job_name: str) -> None:
	"""
	Deactivate a specific job for a user.
	
	Args:
		user_id: User identifier
		job_name: Name of the job to deactivate
	"""
	job_id = _job_id(user_id, job_name)
	if scheduler.get_job(job_id):
		scheduler.remove_job(job_id)
		logger.info(f"Deactivated job {job_id}")
	JOB_REGISTRY.pop(job_id, None)


def deactivate_all_user_jobs(user_id: str) -> None:
	"""
	Deactivate all jobs for a user.
	
	Args:
		user_id: User identifier
	"""
	jobs_to_remove = [
		jid for jid in JOB_REGISTRY.keys()
		if jid.startswith(f"{user_id}:")
	]
	
	for job_id in jobs_to_remove:
		if scheduler.get_job(job_id):
			scheduler.remove_job(job_id)
		JOB_REGISTRY.pop(job_id, None)
	
	logger.info(f"Deactivated all jobs for user {user_id} ({len(jobs_to_remove)} jobs)")


def get_jobs_for_user(user_id: str) -> list[Dict[str, Any]]:
	"""
	Get all registered jobs for a user.
	
	Args:
		user_id: User identifier
		
	Returns:
		List of job info dicts
	"""
	return [j for j in JOB_REGISTRY.values() if j["user_id"] == user_id]


def get_all_jobs() -> list[Dict[str, Any]]:
	"""
	Get all registered jobs across all users.
	
	Returns:
		List of job info dicts
	"""
	return list(JOB_REGISTRY.values())


def is_job_active(user_id: str, job_name: str) -> bool:
	"""
	Check if a specific job is currently active.
	
	Args:
		user_id: User identifier
		job_name: Name of the job
		
	Returns:
		True if job is registered and active
	"""
	job_id = _job_id(user_id, job_name)
	return job_id in JOB_REGISTRY and scheduler.get_job(job_id) is not None


# Cleanup on module unload
import atexit
def _shutdown_scheduler():
	"""Shut down the scheduler gracefully."""
	if scheduler.running:
		logger.info("Shutting down APScheduler...")
		scheduler.shutdown(wait=False)

atexit.register(_shutdown_scheduler)
