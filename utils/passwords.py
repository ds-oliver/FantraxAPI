# utils/passwords.py
from passlib.context import CryptContext

# Use bcrypt as the main scheme
_pwd_ctx = CryptContext(
	schemes=["bcrypt"],
	deprecated="auto",
)


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
