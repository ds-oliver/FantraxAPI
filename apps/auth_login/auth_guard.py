# apps/auth_login/auth_guard.py
import streamlit as st
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from apps.auth_login.login import login_ui
from utils.user_manager import UserManager

# Initialize user manager
user_mgr = UserManager()


def require_auth() -> str:
	"""
	Ensure a user is logged in.
	- If not, render the login/signup UI and stop the app.
	- Returns the authenticated user's user_id.
	"""
	user_id = st.session_state.get("current_user_id")
	if not user_id:
		login_ui()
		st.stop()

	user = user_mgr.get_user_by_id(user_id)
	if not user:
		# Session has a stale user_id, force re-login
		st.warning("Session expired or user not found. Please log in again.")
		st.session_state.pop("current_user_id", None)
		login_ui()
		st.stop()

	return user["user_id"]


def get_current_user_email() -> str:
	"""
	Get the email of the currently authenticated user.
	Must be called after require_auth().
	"""
	user_id = st.session_state.get("current_user_id")
	if not user_id:
		return ""
	
	user = user_mgr.get_user_by_id(user_id)
	return user.get("email", "") if user else ""


def get_current_username() -> str:
	"""
	Get the username of the currently authenticated user.
	Falls back to email if username not set.
	Must be called after require_auth().
	"""
	user_id = st.session_state.get("current_user_id")
	if not user_id:
		return ""
	
	user = user_mgr.get_user_by_id(user_id)
	if not user:
		return ""
	
	# Return username if set, otherwise fall back to email
	return user.get("username") or user.get("email", "")


def logout():
	"""Log out the current user."""
	if "current_user_id" in st.session_state:
		st.session_state.pop("current_user_id")
	st.rerun()

