"""
Secure storage utilities for encrypting data at rest.

Uses Fernet symmetric encryption to protect sensitive data like cookies.
"""
import json
from pathlib import Path
from typing import Any, Dict
from cryptography.fernet import Fernet
import logging

logger = logging.getLogger(__name__)

# In production, manage this key via environment variable or secret manager
KEY_FILE = Path("data/secret.key")
AUTH_DIR = Path("data/auth")

def _get_fernet() -> Fernet:
	"""Get or create Fernet cipher instance."""
	AUTH_DIR.mkdir(parents=True, exist_ok=True)
	if not KEY_FILE.exists():
		key = Fernet.generate_key()
		KEY_FILE.write_bytes(key)
		logger.info("Generated new encryption key at %s", KEY_FILE)
	else:
		key = KEY_FILE.read_bytes()
	return Fernet(key)

def save_encrypted_json(path: Path, data: Dict[str, Any]) -> None:
	"""
	Save JSON data encrypted to disk.
	
	Args:
		path: File path to save to
		data: Dictionary to save
	"""
	f = _get_fernet()
	raw = json.dumps(data).encode("utf-8")
	token = f.encrypt(raw)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(token)
	logger.debug("Saved encrypted data to %s", path)

def load_encrypted_json(path: Path) -> Dict[str, Any]:
	"""
	Load and decrypt JSON data from disk.
	
	Args:
		path: File path to load from
	
	Returns:
		Decrypted dictionary
	
	Raises:
		FileNotFoundError: If file doesn't exist
		cryptography.fernet.InvalidToken: If decryption fails
	"""
	f = _get_fernet()
	token = path.read_bytes()
	raw = f.decrypt(token)
	return json.loads(raw.decode("utf-8"))

