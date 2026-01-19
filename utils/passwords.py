# utils/passwords.py
import os
import logging

# Enable passlib's builtin bcrypt backend as a safe fallback.
os.environ.setdefault("PASSLIB_BUILTIN_BCRYPT", "1")

from passlib.context import CryptContext
from passlib.hash import bcrypt as passlib_bcrypt

# Use bcrypt_sha256 to avoid bcrypt's 72-byte input limit while staying bcrypt-based.
_pwd_ctx = CryptContext(
	schemes=["bcrypt_sha256", "bcrypt"],
	deprecated="auto",
)

_log = logging.getLogger(__name__)

# Prefer builtin backend if the installed bcrypt module rejects long passwords.
try:
	passlib_bcrypt.set_backend("bcrypt", dryrun=True)
except Exception:
	try:
		passlib_bcrypt.set_backend("builtin")
		_log.warning("bcrypt backend unavailable; using passlib builtin backend.")
	except Exception:
		# Last resort: keep default backend selection.
		pass


def hash_password(raw: str) -> str:
	"""
	Hash a raw password or secretId using bcrypt.
	You never store the raw string, only this hash.
	"""
	if not raw:
		raise ValueError("Cannot hash empty password")
	return _pwd_ctx.hash(raw)


def verify_password(raw: str, stored_hash: str) -> bool:
	"""
	Verify a raw password against a stored bcrypt hash.
	"""
	if not raw or not stored_hash:
		return False
	try:
		return _pwd_ctx.verify(raw, stored_hash)
	except Exception:
		return False
