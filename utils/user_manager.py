"""
User management for multi-user cookie-based authentication.

Handles user identification, cookie storage, and retrieval for multiple users.
Supports password-based authentication with bcrypt hashing.
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

# Import secure storage for encrypted cookies
try:
	from utils.secure_storage import save_encrypted_json, load_encrypted_json
	SECURE_STORAGE_AVAILABLE = True
except ImportError:
	logger.warning("secure_storage not available - using plaintext storage")
	SECURE_STORAGE_AVAILABLE = False

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
			"username": None,  # Set via set_username()
			"created_at": datetime.utcnow().isoformat(),
			"auth_status": "not_connected",
			"last_login": None,
			"password_hash": None,  # Set via set_user_password()
			"timezone": "UTC",
			"timezone_updated_at": None,
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
		Save cookie artifacts for a user (encrypted at rest).
		
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
			
			# Use encrypted storage if available
			if SECURE_STORAGE_AVAILABLE:
				save_encrypted_json(cookie_path, artifacts_with_meta)
			else:
				with open(cookie_path, 'w') as f:
					json.dump(artifacts_with_meta, f, indent=2)
			
			# Update user auth status
			self.update_user_auth_status(user_id, "connected")
			
			logger.info(f"Saved cookies for user {user_id} (encrypted={SECURE_STORAGE_AVAILABLE})")
			return True
			
		except Exception as e:
			logger.error(f"Failed to save cookies for user {user_id}: {e}")
			return False
	
	def load_user_cookies(self, user_id: str) -> Optional[Dict[str, Any]]:
		"""
		Load saved cookie artifacts for a user (decrypted from encrypted storage).
		
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
			
			# Try encrypted storage first, fallback to plaintext
			if SECURE_STORAGE_AVAILABLE:
				try:
					artifacts = load_encrypted_json(cookie_path)
				except Exception as decrypt_error:
					logger.warning(f"Failed to decrypt cookies for {user_id}, trying plaintext: {decrypt_error}")
					with open(cookie_path, 'r') as f:
						artifacts = json.load(f)
			else:
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

	def get_timezone(self, user_id: str) -> str:
		"""Return the preferred timezone for a user (fallback UTC)."""
		user = self.get_user_by_id(user_id) or {}
		return user.get("timezone") or "UTC"

	def set_timezone(self, user_id: str, timezone_str: str) -> bool:
		"""Persist a timezone preference for the user."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			users[user_id]["timezone"] = str(timezone_str)
			users[user_id]["timezone_updated_at"] = datetime.utcnow().isoformat()
			data["users"] = users
			self._save_users(data)
			logger.info("Set timezone=%s for user=%s", timezone_str, user_id)
			return True
		except Exception as e:
			logger.error(f"Failed to set timezone for user {user_id}: {e}")
			return False

	def get_default_league(self, user_id: str) -> Optional[Dict[str, Any]]:
		"""Return the user's preferred default league/team if set."""
		user = self.get_user_by_id(user_id)
		if not user:
			return None
		league_id = user.get("default_league_id")
		if not league_id:
			return None
		return {
			"league_id": str(league_id),
			"team_id": str(user.get("default_team_id")) if user.get("default_team_id") else None,
			"league_name": user.get("default_league_name") or "",
			"team_name": user.get("default_team_name") or "",
			"updated_at": user.get("default_league_updated_at"),
		}

	def set_default_league(
		self,
		user_id: str,
		league_id: str,
		team_id: str,
		league_name: str = "",
		team_name: str = "",
	) -> bool:
		"""Persist the user's preferred default league/team selection."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False

			users[user_id]["default_league_id"] = str(league_id)
			users[user_id]["default_team_id"] = str(team_id)
			users[user_id]["default_league_name"] = league_name or ""
			users[user_id]["default_team_name"] = team_name or ""
			users[user_id]["default_league_updated_at"] = datetime.utcnow().isoformat()
			data["users"] = users
			self._save_users(data)
			logger.info(f"Saved default league for user {user_id}: {league_name} ({league_id})")
			return True
		except Exception as e:
			logger.error(f"Failed to save default league for user {user_id}: {e}")
			return False

	def get_auto_rules_pref(self, user_id: str, league_id: str) -> Optional[Dict[str, Any]]:
		"""Return the auto-rules preference for a user/league."""
		user = self.get_user_by_id(user_id)
		if not user:
			return None
		auto_rules = user.get("auto_rules") or {}
		return auto_rules.get(str(league_id))

	def is_auto_rules_enabled(
		self,
		user_id: str,
		league_id: str,
		feature: str = "lineup_swaps",
	) -> bool:
		"""Check if a specific auto feature is enabled for this user/league."""
		pref = self.get_auto_rules_pref(user_id, league_id)
		if not pref:
			return False
		# Backward compatibility: treat "enabled" as lineup swap opt-in
		if feature == "lineup_swaps" and "enabled" in pref:
			return bool(pref.get("enabled"))
		features = pref.get("features") or {}
		return bool(features.get(feature))

	def set_auto_rules_enabled(
		self,
		user_id: str,
		league_id: str,
		team_id: Optional[str],
		enabled: bool,
		feature: str = "lineup_swaps",
	) -> bool:
		"""Enable/disable a specific auto feature for a user/league."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False

			auto_rules = users[user_id].get("auto_rules") or {}
			entry = auto_rules.get(str(league_id)) or {}
			features = entry.get("features") or {}
			features[feature] = bool(enabled)
			entry.update(
				{
					"features": features,
					"team_id": str(team_id) if team_id else entry.get("team_id"),
					"updated_at": datetime.utcnow().isoformat(),
				}
			)
			# Preserve legacy "enabled" for lineup swaps
			if feature == "lineup_swaps":
				entry["enabled"] = bool(enabled)
			auto_rules[str(league_id)] = entry
			users[user_id]["auto_rules"] = auto_rules
			data["users"] = users
			self._save_users(data)
			logger.info(
				"Set auto feature=%s enabled=%s for user=%s league=%s",
				feature,
				enabled,
				user_id,
				league_id,
			)
			return True
		except Exception as e:
			logger.error(f"Failed to set auto rules for user {user_id}: {e}")
			return False

	def get_do_not_move(self, user_id: str, league_id: str) -> list[str]:
		"""Return list of player ids that should not move unless confirmed out."""
		user = self.get_user_by_id(user_id) or {}
		do_not_move = user.get("do_not_move") or {}
		return list(do_not_move.get(str(league_id)) or [])

	def set_do_not_move(
		self,
		user_id: str,
		league_id: str,
		player_ids: list[str],
		team_id: Optional[str] = None,
	) -> bool:
		"""Set the do-not-move list for a user/league."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			do_not_move = users[user_id].get("do_not_move") or {}
			do_not_move[str(league_id)] = [str(pid) for pid in player_ids]
			users[user_id]["do_not_move"] = do_not_move
			users[user_id]["do_not_move_updated_at"] = datetime.utcnow().isoformat()
			if team_id:
				users[user_id]["do_not_move_team_id"] = str(team_id)
			data["users"] = users
			self._save_users(data)
			logger.info("Updated do-not-move list for user=%s league=%s", user_id, league_id)
			return True
		except Exception as e:
			logger.error(f"Failed to set do-not-move for user {user_id}: {e}")
			return False

	def get_claims_ack(self, user_id: str, league_id: str) -> bool:
		"""Return whether the user acknowledged claim/drop rules for this league."""
		user = self.get_user_by_id(user_id) or {}
		claims_ack = user.get("claims_ack") or {}
		entry = claims_ack.get(str(league_id))
		if isinstance(entry, dict):
			return bool(entry.get("acknowledged"))
		if isinstance(entry, bool):
			return entry
		return False

	def set_claims_ack(
		self,
		user_id: str,
		league_id: str,
		acknowledged: bool = True,
		team_id: Optional[str] = None,
	) -> bool:
		"""Persist claim/drop acknowledgement for this user/league."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			claims_ack = users[user_id].get("claims_ack") or {}
			claims_ack[str(league_id)] = {
				"acknowledged": bool(acknowledged),
				"updated_at": datetime.utcnow().isoformat(),
				"team_id": str(team_id) if team_id else None,
			}
			users[user_id]["claims_ack"] = claims_ack
			data["users"] = users
			self._save_users(data)
			logger.info("Updated claims acknowledgement for user=%s league=%s", user_id, league_id)
			return True
		except Exception as e:
			logger.error(f"Failed to set claims acknowledgement for user {user_id}: {e}")
			return False

	def get_never_drop(self, user_id: str, league_id: str) -> list[str]:
		"""Return list of player ids that should never be dropped."""
		state = self.get_never_drop_state(user_id, league_id)
		return state.get("effective", [])

	def get_never_drop_state(self, user_id: str, league_id: str) -> Dict[str, Any]:
		"""Return never-drop state including auto/manual overrides."""
		user = self.get_user_by_id(user_id) or {}
		never_drop = user.get("never_drop") or {}
		entry = never_drop.get(str(league_id))
		legacy = False
		auto_ids: list[str] = []
		manual_add: list[str] = []
		manual_remove: list[str] = []
		if isinstance(entry, dict):
			auto_ids = [str(pid) for pid in (entry.get("auto") or [])]
			manual_add = [str(pid) for pid in (entry.get("manual_add") or [])]
			manual_remove = [str(pid) for pid in (entry.get("manual_remove") or [])]
		elif isinstance(entry, list):
			legacy = True
			manual_add = [str(pid) for pid in entry]
		effective = (set(auto_ids) - set(manual_remove)) | set(manual_add)
		return {
			"auto": auto_ids,
			"manual_add": manual_add,
			"manual_remove": manual_remove,
			"effective": list(effective),
			"legacy": legacy,
		}

	def set_never_drop(
		self,
		user_id: str,
		league_id: str,
		player_ids: list[str],
		team_id: Optional[str] = None,
	) -> bool:
		"""Set the never-drop list for a user/league (manual-only fallback)."""
		return self.set_never_drop_state(
			user_id=user_id,
			league_id=league_id,
			auto_ids=[],
			manual_add=player_ids,
			manual_remove=[],
			team_id=team_id,
		)

	def set_never_drop_state(
		self,
		user_id: str,
		league_id: str,
		auto_ids: list[str],
		manual_add: list[str],
		manual_remove: list[str],
		team_id: Optional[str] = None,
	) -> bool:
		"""Set never-drop state with auto list and manual overrides."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			never_drop = users[user_id].get("never_drop") or {}
			never_drop[str(league_id)] = {
				"auto": [str(pid) for pid in auto_ids],
				"manual_add": [str(pid) for pid in manual_add],
				"manual_remove": [str(pid) for pid in manual_remove],
			}
			users[user_id]["never_drop"] = never_drop
			users[user_id]["never_drop_updated_at"] = datetime.utcnow().isoformat()
			if team_id:
				users[user_id]["never_drop_team_id"] = str(team_id)
			data["users"] = users
			self._save_users(data)
			logger.info("Updated never-drop state for user=%s league=%s", user_id, league_id)
			return True
		except Exception as e:
			logger.error(f"Failed to set never-drop for user {user_id}: {e}")
			return False

	def get_late_kos_policy(self, user_id: str, league_id: str) -> str:
		"""Return late KOS policy for this user/league."""
		user = self.get_user_by_id(user_id) or {}
		policies = user.get("late_kos_policy") or {}
		entry = policies.get(str(league_id)) or {}
		return entry.get("mode") or "trust"

	def set_late_kos_policy(
		self,
		user_id: str,
		league_id: str,
		mode: str,
		team_id: Optional[str] = None,
	) -> bool:
		"""Set late KOS coverage policy for this user/league."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			policies = users[user_id].get("late_kos_policy") or {}
			policies[str(league_id)] = {
				"mode": str(mode),
				"updated_at": datetime.utcnow().isoformat(),
				"team_id": str(team_id) if team_id else None,
			}
			users[user_id]["late_kos_policy"] = policies
			data["users"] = users
			self._save_users(data)
			logger.info("Set late KOS policy=%s for user=%s league=%s", mode, user_id, league_id)
			return True
		except Exception as e:
			logger.error(f"Failed to set late KOS policy for user {user_id}: {e}")
			return False

	def get_preferred_period(self, user_id: str, league_id: str) -> Optional[str]:
		"""Return preferred period id for a user/league if stored."""
		user = self.get_user_by_id(user_id) or {}
		period_prefs = user.get("period_prefs") or {}
		entry = period_prefs.get(str(league_id)) or {}
		return entry.get("period_id")

	def set_preferred_period(
		self,
		user_id: str,
		league_id: str,
		period_id: str,
		period_label: str = "",
		team_id: Optional[str] = None,
	) -> bool:
		"""Persist preferred period for a user/league."""
		try:
			data = self._load_users()
			users = data.get("users", {})
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			period_prefs = users[user_id].get("period_prefs") or {}
			period_prefs[str(league_id)] = {
				"period_id": str(period_id),
				"period_label": period_label or "",
				"updated_at": datetime.utcnow().isoformat(),
				"team_id": str(team_id) if team_id else None,
			}
			users[user_id]["period_prefs"] = period_prefs
			data["users"] = users
			self._save_users(data)
			logger.info(
				"Set preferred period=%s for user=%s league=%s",
				period_id,
				user_id,
				league_id,
			)
			return True
		except Exception as e:
			logger.error(f"Failed to set preferred period for user {user_id}: {e}")
			return False
	
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
	
	# -------- Password Management (bcrypt hashing) --------
	
	def set_user_password(self, user_id: str, password_hash: str) -> bool:
		"""
		Set password hash for a user.
		
		Args:
			user_id: User identifier
			password_hash: Bcrypt hash from utils.passwords.hash_password()
			
		Returns:
			True if saved successfully
		"""
		try:
			data = self._load_users()
			users = data.get("users", {})
			
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			
			users[user_id]["password_hash"] = password_hash
			users[user_id]["password_set_at"] = datetime.utcnow().isoformat()
			
			data["users"] = users
			self._save_users(data)
			
			logger.info(f"Set password for user {user_id}")
			return True
			
		except Exception as e:
			logger.error(f"Failed to set password for user {user_id}: {e}")
			return False
	
	def has_password(self, user_id: str) -> bool:
		"""Check if user has a password set."""
		user = self.get_user_by_id(user_id)
		return user is not None and user.get("password_hash") is not None
	
	def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
		"""Get user info by email address."""
		user_id = self._email_to_user_id(email)
		return self.get_user_by_id(user_id)
	
	# -------- Username Management --------
	
	def is_username_taken(self, username: str, exclude_user_id: str = None) -> bool:
		"""
		Check if a username is already taken.
		
		Args:
			username: Username to check
			exclude_user_id: Optional user_id to exclude from check (for updates)
			
		Returns:
			True if username is taken by another user
		"""
		if not username:
			return False
		
		username_lower = username.lower().strip()
		data = self._load_users()
		users = data.get("users", {})
		
		for uid, user in users.items():
			if uid == exclude_user_id:
				continue
			if user.get("username", "").lower().strip() == username_lower:
				return True
		
		return False
	
	def set_username(self, user_id: str, username: str) -> bool:
		"""
		Set username for a user.
		
		Args:
			user_id: User identifier
			username: Username to set
			
		Returns:
			True if set successfully
		"""
		try:
			username = username.strip()
			
			# Validate username
			if not username:
				logger.error("Username cannot be empty")
				return False
			
			if len(username) < 3:
				logger.error("Username must be at least 3 characters")
				return False
			
			if len(username) > 30:
				logger.error("Username must be 30 characters or less")
				return False
			
			# Check if taken
			if self.is_username_taken(username, exclude_user_id=user_id):
				logger.error(f"Username '{username}' is already taken")
				return False
			
			data = self._load_users()
			users = data.get("users", {})
			
			if user_id not in users:
				logger.error(f"User {user_id} not found")
				return False
			
			users[user_id]["username"] = username
			users[user_id]["username_set_at"] = datetime.utcnow().isoformat()
			
			data["users"] = users
			self._save_users(data)
			
			logger.info(f"Set username for user {user_id}: {username}")
			return True
			
		except Exception as e:
			logger.error(f"Failed to set username for user {user_id}: {e}")
			return False
	
	def get_username(self, user_id: str) -> Optional[str]:
		"""Get username for a user (returns None if not set)."""
		user = self.get_user_by_id(user_id)
		return user.get("username") if user else None
