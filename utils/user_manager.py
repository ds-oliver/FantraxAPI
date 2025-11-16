"""
User management for multi-user cookie-based authentication.

Handles user identification, cookie storage, and retrieval for multiple users.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class UserManager:
    """Manages multiple users and their authentication artifacts."""
    
    def __init__(self, data_dir: Path = None):
        """
        Initialize user manager.
        
        Args:
            data_dir: Base directory for user data (defaults to data/auth/)
        """
        if data_dir is None:
            data_dir = Path(__file__).parent.parent / "data" / "auth"
        
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.users_file = self.data_dir / "users.json"
        self._ensure_users_file()
    
    def _ensure_users_file(self):
        """Create users.json if it doesn't exist."""
        if not self.users_file.exists():
            self.users_file.write_text(json.dumps({"users": {}}, indent=2))
    
    def _load_users(self) -> Dict[str, Any]:
        """Load users database."""
        try:
            with open(self.users_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load users file: {e}")
            return {"users": {}}
    
    def _save_users(self, data: Dict[str, Any]):
        """Save users database."""
        try:
            with open(self.users_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save users file: {e}")
    
    @staticmethod
    def _email_to_user_id(email: str) -> str:
        """Convert email to a stable user_id (hash of email)."""
        return hashlib.sha256(email.lower().encode()).hexdigest()[:16]
    
    def get_or_create_user(self, email: str) -> Dict[str, Any]:
        """
        Get existing user or create new one.
        
        Args:
            email: User's email address
            
        Returns:
            Dict with user_id, email, created_at, auth_status
        """
        email = email.lower().strip()
        user_id = self._email_to_user_id(email)
        
        data = self._load_users()
        users = data.get("users", {})
        
        if user_id in users:
            logger.info(f"Retrieved existing user: {email}")
            return users[user_id]
        
        # Create new user
        user = {
            "user_id": user_id,
            "email": email,
            "created_at": datetime.utcnow().isoformat(),
            "auth_status": "not_connected",
            "last_login": None
        }
        
        users[user_id] = user
        data["users"] = users
        self._save_users(data)
        
        logger.info(f"Created new user: {email} (ID: {user_id})")
        return user
    
    def get_user_cookie_path(self, user_id: str) -> Path:
        """
        Get path to user's cookie file.
        
        Args:
            user_id: User identifier
            
        Returns:
            Path to {user_id}_cookies.json
        """
        return self.data_dir / f"{user_id}_cookies.json"
    
    def save_user_cookies(self, user_id: str, artifacts: Dict[str, Any]) -> bool:
        """
        Save cookie artifacts for a user.
        
        Args:
            user_id: User identifier
            artifacts: Cookie and storage data from authentication
            
        Returns:
            True if saved successfully
        """
        try:
            cookie_path = self.get_user_cookie_path(user_id)
            
            # Add metadata
            artifacts_with_meta = {
                **artifacts,
                "saved_at": datetime.utcnow().isoformat(),
                "user_id": user_id
            }
            
            with open(cookie_path, 'w') as f:
                json.dump(artifacts_with_meta, f, indent=2)
            
            # Update user auth status
            self.update_user_auth_status(user_id, "connected")
            
            logger.info(f"Saved cookies for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save cookies for user {user_id}: {e}")
            return False
    
    def load_user_cookies(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Load saved cookie artifacts for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            Cookie artifacts dict or None if not found
        """
        try:
            cookie_path = self.get_user_cookie_path(user_id)
            
            if not cookie_path.exists():
                logger.debug(f"No saved cookies found for user {user_id}")
                return None
            
            with open(cookie_path, 'r') as f:
                artifacts = json.load(f)
            
            logger.info(f"Loaded cookies for user {user_id}")
            return artifacts
            
        except Exception as e:
            logger.error(f"Failed to load cookies for user {user_id}: {e}")
            return None
    
    def update_user_auth_status(self, user_id: str, status: str):
        """
        Update user's authentication status.
        
        Args:
            user_id: User identifier
            status: One of: not_connected, connected, expired
        """
        data = self._load_users()
        users = data.get("users", {})
        
        if user_id in users:
            users[user_id]["auth_status"] = status
            users[user_id]["last_updated"] = datetime.utcnow().isoformat()
            
            if status == "connected":
                users[user_id]["last_login"] = datetime.utcnow().isoformat()
            
            data["users"] = users
            self._save_users(data)
            logger.info(f"Updated auth status for user {user_id}: {status}")
    
    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user info by user_id."""
        data = self._load_users()
        return data.get("users", {}).get(user_id)
    
    def list_all_users(self) -> list[Dict[str, Any]]:
        """Get list of all users."""
        data = self._load_users()
        return list(data.get("users", {}).values())
    
    def delete_user_cookies(self, user_id: str) -> bool:
        """
        Delete saved cookies for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            True if deleted successfully
        """
        try:
            cookie_path = self.get_user_cookie_path(user_id)
            
            if cookie_path.exists():
                cookie_path.unlink()
                logger.info(f"Deleted cookies for user {user_id}")
            
            self.update_user_auth_status(user_id, "not_connected")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete cookies for user {user_id}: {e}")
            return False

