"""
Background job manager for Fantrax automation.

Handles scheduling and execution of background tasks like:
- League/roster synchronization
- Lineup automation (SofaScore → Fantrax)
- Waiver/free agent automation (future)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
import json

from requests import Session

logger = logging.getLogger(__name__)


class BackgroundJobManager:
    """
    Manages background automation tasks for users.
    
    This is a skeleton implementation. Future versions will integrate with
    APScheduler or similar for periodic task execution.
    """
    
    def __init__(self, data_dir: Path = None):
        """
        Initialize job manager.
        
        Args:
            data_dir: Directory for job state files (defaults to data/jobs/)
        """
        if data_dir is None:
            data_dir = Path(__file__).parent.parent / "data" / "jobs"
        
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"BackgroundJobManager initialized (data_dir: {self.data_dir})")
    
    def get_user_status_file(self, user_id: str) -> Path:
        """Get path to user's job status file."""
        return self.data_dir / f"{user_id}_status.json"
    
    def start_user_jobs(self, user_id: str, session: Session, leagues: list = None) -> Dict[str, Any]:
        """
        Initialize all background tasks for a user.
        
        Args:
            user_id: User identifier
            session: Authenticated requests session
            leagues: Optional list of league configs to monitor
            
        Returns:
            Dict with job status information
        """
        logger.info(f"Starting background jobs for user {user_id}")
        
        status = {
            "user_id": user_id,
            "started_at": datetime.utcnow().isoformat(),
            "last_updated": datetime.utcnow().isoformat(),
            "jobs": {
                "league_sync": {
                    "enabled": True,
                    "status": "pending",
                    "last_run": None,
                    "next_run": None,
                    "description": "Sync league rosters and schedules"
                },
                "lineup_watcher": {
                    "enabled": False,  # Disabled until fully implemented
                    "status": "pending",
                    "last_run": None,
                    "next_run": None,
                    "description": "Monitor SofaScore for confirmed lineups"
                },
                "waiver_automation": {
                    "enabled": False,  # Future feature
                    "status": "not_implemented",
                    "last_run": None,
                    "next_run": None,
                    "description": "Automated waiver claims and drops"
                }
            }
        }
        
        # Save status
        self._save_user_status(user_id, status)
        
        # TODO: Actually start background threads/processes
        # For now, this is just a placeholder that tracks state
        
        logger.info(f"Background jobs initialized for user {user_id}")
        return status
    
    def stop_user_jobs(self, user_id: str) -> bool:
        """
        Stop all jobs for a user (e.g., on logout).
        
        Args:
            user_id: User identifier
            
        Returns:
            True if stopped successfully
        """
        logger.info(f"Stopping background jobs for user {user_id}")
        
        try:
            status = self.get_job_status(user_id)
            if status:
                status["last_updated"] = datetime.utcnow().isoformat()
                status["stopped_at"] = datetime.utcnow().isoformat()
                
                # Mark all jobs as stopped
                for job_name, job_info in status.get("jobs", {}).items():
                    job_info["status"] = "stopped"
                
                self._save_user_status(user_id, status)
            
            # TODO: Actually stop background threads/processes
            
            logger.info(f"Background jobs stopped for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to stop jobs for user {user_id}: {e}")
            return False
    
    def get_job_status(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get current status of all user's jobs.
        
        Args:
            user_id: User identifier
            
        Returns:
            Dict with job status or None if not found
        """
        status_file = self.get_user_status_file(user_id)
        
        if not status_file.exists():
            return None
        
        try:
            with open(status_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load job status for user {user_id}: {e}")
            return None
    
    def update_job_status(self, user_id: str, job_name: str, status_update: Dict[str, Any]):
        """
        Update status of a specific job.
        
        Args:
            user_id: User identifier
            job_name: Name of the job to update
            status_update: Dict with status fields to update
        """
        current_status = self.get_job_status(user_id)
        
        if not current_status:
            logger.warning(f"No status found for user {user_id}, creating new")
            # Initialize if doesn't exist
            self.start_user_jobs(user_id, None)
            current_status = self.get_job_status(user_id)
        
        if current_status and job_name in current_status.get("jobs", {}):
            current_status["jobs"][job_name].update(status_update)
            current_status["jobs"][job_name]["last_updated"] = datetime.utcnow().isoformat()
            current_status["last_updated"] = datetime.utcnow().isoformat()
            
            self._save_user_status(user_id, current_status)
            logger.debug(f"Updated job status for {user_id}/{job_name}")
    
    def _save_user_status(self, user_id: str, status: Dict[str, Any]):
        """Save user's job status to file."""
        status_file = self.get_user_status_file(user_id)
        
        try:
            with open(status_file, 'w') as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save job status for user {user_id}: {e}")
    
    def list_all_active_users(self) -> list[Dict[str, Any]]:
        """
        Get list of all users with active jobs.
        
        Returns:
            List of user status dicts
        """
        active_users = []
        
        for status_file in self.data_dir.glob("*_status.json"):
            try:
                with open(status_file, 'r') as f:
                    status = json.load(f)
                    
                # Check if any jobs are active
                has_active_jobs = any(
                    job.get("status") not in ["stopped", "not_implemented"]
                    for job in status.get("jobs", {}).values()
                )
                
                if has_active_jobs:
                    active_users.append(status)
                    
            except Exception as e:
                logger.error(f"Failed to read status file {status_file}: {e}")
        
        return active_users


# Convenience functions for integration with app
def start_user_jobs(user_id: str, session: Session, leagues: list = None):
    """Start background jobs for a user (convenience function)."""
    mgr = BackgroundJobManager()
    return mgr.start_user_jobs(user_id, session, leagues)


def stop_user_jobs(user_id: str):
    """Stop background jobs for a user (convenience function)."""
    mgr = BackgroundJobManager()
    return mgr.stop_user_jobs(user_id)


def get_job_status(user_id: str) -> Optional[Dict[str, Any]]:
    """Get job status for a user (convenience function)."""
    mgr = BackgroundJobManager()
    return mgr.get_job_status(user_id)

