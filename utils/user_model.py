# utils/user_model.py
from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
	user_id: str
	email: str
	username: Optional[str] = None
	password_hash: Optional[str] = None
	created_at: Optional[str] = None
	# add more fields as needed later (e.g., display_name, last_login_at)

