"""
User management for multi-user cookie-based authentication.

Handles user identification, cookie storage, and retrieval for multiple users.
Uses Fantrax Secret ID as authentication mechanism (read-only credential).
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# Encryption for Secret IDs
try:
    from cryptography.fernet import Fernet
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not installed - Secret IDs will be hashed but not encrypted")


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
        
        # Initialize encryption for Secret IDs
        self._init_encryption()
    
    def _ensure_users_file(self):
        """Create users.json if it doesn't exist."""
        if not self.users_file.exists():
            self.users_file.write_text(json.dumps({"users": {}}, indent=2))
    
    def _init_encryption(self):
        """Initialize encryption key for Secret IDs."""
        if not CRYPTO_AVAILABLE:
            self.cipher = None
            return
        
        # Get encryption key from environment or generate one
        key_file = self.data_dir / ".encryption_key"
        
        if key_file.exists():
            with open(key_file, 'rb') as f:
                key = f.read()
        else:
            # Generate new key
            key = Fernet.generate_key()
            with open(key_file, 'wb') as f:
                f.write(key)
            # Secure the key file
            os.chmod(key_file, 0o600)
            logger.info("Generated new encryption key for Secret IDs")
        
        self.cipher = Fernet(key)
    
    def _encrypt_secret_id(self, secret_id: str) -> str:
        """Encrypt a Secret ID for storage."""
        if not CRYPTO_AVAILABLE or not self.cipher:
            # Fallback: just hash it (not reversible, but secure)
            return hashlib.sha256(secret_id.encode()).hexdigest()
        
        return self.cipher.encrypt(secret_id.encode()).decode()
    
    def _decrypt_secret_id(self, encrypted: str) -> Optional[str]:
        """Decrypt a stored Secret ID."""
        if not CRYPTO_AVAILABLE or not self.cipher:
            # Can't decrypt hashed values
            return None
        
        try:
            return self.cipher.decrypt(encrypted.encode()).decode()
        except Exception as e:
            logger.error(f"Failed to decrypt Secret ID: {e}")
            return None
    
    @staticmethod
    def _hash_secret_id(secret_id: str) -> str:
        """Hash Secret ID for verification (one-way)."""
        return hashlib.sha256(secret_id.encode()).hexdigest()
    
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
    
    def save_secret_id(self, user_id: str, secret_id: str) -> bool:
        """
        Save encrypted Secret ID for a user.
        
        Args:
            user_id: User identifier
            secret_id: Fantrax Secret ID from user's profile
            
        Returns:
            True if saved successfully
        """
        try:
            data = self._load_users()
            users = data.get("users", {})
            
            if user_id not in users:
                logger.error(f"User {user_id} not found")
                return False
            
            # Store both encrypted (for display/recovery) and hashed (for verification)
            users[user_id]["secret_id_encrypted"] = self._encrypt_secret_id(secret_id)
            users[user_id]["secret_id_hash"] = self._hash_secret_id(secret_id)
            users[user_id]["secret_id_set_at"] = datetime.utcnow().isoformat()
            
            data["users"] = users
            self._save_users(data)
            
            logger.info(f"Saved Secret ID for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save Secret ID for user {user_id}: {e}")
            return False
    
    def verify_secret_id(self, user_id: str, secret_id: str) -> bool:
        """
        Verify a Secret ID matches the stored one for a user.
        
        Args:
            user_id: User identifier
            secret_id: Secret ID to verify
            
        Returns:
            True if Secret ID matches
        """
        try:
            user = self.get_user_by_id(user_id)
            if not user:
                return False
            
            stored_hash = user.get("secret_id_hash")
            if not stored_hash:
                return False
            
            provided_hash = self._hash_secret_id(secret_id)
            return provided_hash == stored_hash
            
        except Exception as e:
            logger.error(f"Failed to verify Secret ID for user {user_id}: {e}")
            return False
    
    def authenticate_user(self, email: str, secret_id: str) -> Optional[Dict[str, Any]]:
        """
        Authenticate a user with email and Secret ID.
        
        Args:
            email: User's email address
            secret_id: Fantrax Secret ID
            
        Returns:
            User dict if authenticated, None otherwise
        """
        user_id = self._email_to_user_id(email)
        
        # Check if user exists
        user = self.get_user_by_id(user_id)
        if not user:
            logger.warning(f"Authentication failed: User not found for email {email}")
            return None
        
        # Verify Secret ID
        if not self.verify_secret_id(user_id, secret_id):
            logger.warning(f"Authentication failed: Invalid Secret ID for user {user_id}")
            return None
        
        # Update last login
        data = self._load_users()
        data["users"][user_id]["last_login"] = datetime.utcnow().isoformat()
        self._save_users(data)
        
        logger.info(f"User authenticated: {email}")
        return user
    
    def revoke_secret_id(self, user_id: str) -> bool:
        """
        Revoke (delete) Secret ID for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            True if revoked successfully
        """
        try:
            data = self._load_users()
            users = data.get("users", {})
            
            if user_id not in users:
                return False
            
            # Remove Secret ID fields
            users[user_id].pop("secret_id_encrypted", None)
            users[user_id].pop("secret_id_hash", None)
            users[user_id].pop("secret_id_set_at", None)
            users[user_id]["secret_id_revoked_at"] = datetime.utcnow().isoformat()
            
            data["users"] = users
            self._save_users(data)
            
            logger.info(f"Revoked Secret ID for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to revoke Secret ID for user {user_id}: {e}")
            return False
    
    def has_secret_id(self, user_id: str) -> bool:
        """Check if user has a Secret ID set."""
        user = self.get_user_by_id(user_id)
        return user is not None and "secret_id_hash" in user
    
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

