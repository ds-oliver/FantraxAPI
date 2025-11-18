# apps/auth_login/login.py
import streamlit as st
from pathlib import Path
import sys

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from utils.user_manager import UserManager
from utils.passwords import hash_password, verify_password

# Initialize user manager
user_mgr = UserManager()


def _set_current_user(user_id: str) -> None:
	st.session_state["current_user_id"] = user_id


def login_ui():
	"""
	Main login/signup UI.
	Call this from your main app if no user is authenticated.
	"""
	st.title("Fantrax Automation – Login")

	tab_login, tab_signup = st.tabs(["Login", "Create account"])

	# ---------- LOGIN TAB ----------
	with tab_login:
		st.subheader("Login")

		email = st.text_input("Email", key="login_email")
		password = st.text_input("Password", type="password", key="login_password")

		if st.button("Sign in"):
			if not email or not password:
				st.error("Email and password are required.")
			else:
				user = user_mgr.get_user_by_email(email)
				if not user or not user.get("password_hash"):
					st.error("Invalid email or password.")
				else:
					if not verify_password(password, user["password_hash"]):
						st.error("Invalid email or password.")
					else:
						_set_current_user(user["user_id"])
						st.success("Logged in.")
						st.rerun()

	# ---------- SIGNUP TAB ----------
	with tab_signup:
		st.subheader("Create account")

		email_new = st.text_input("Email", key="signup_email")
		username_new = st.text_input(
			"Username",
			key="signup_username",
			help="This will be displayed in the app instead of your email. Must be 3-30 characters.",
			placeholder="e.g., hmarhoefer"
		)

		mode = st.radio(
			"Choose how to set your password:",
			options=["Use my Fantrax secretId", "Create a separate password"],
			key="signup_mode",
		)

		secret_id = ""
		pw1 = ""
		pw2 = ""

		if mode == "Use my Fantrax secretId":
			st.info(
				"Your **secretId** can be found at: https://www.fantrax.com/user/profile\n\n"
				"This secretId is only used for read-only API access and cannot make changes to your roster. "
				"We hash it with bcrypt and never store it in plaintext."
			)
			secret_id = st.text_input(
				"Enter your Fantrax secretId",
				type="password",
				help="We never store this in plaintext. It is hashed using bcrypt.",
				key="signup_secretid",
			)
		else:
			pw1 = st.text_input("Create password", type="password", key="signup_pw1")
			pw2 = st.text_input("Confirm password", type="password", key="signup_pw2")

		if st.button("Create account"):
			# Basic validation
			if not email_new:
				st.error("Email is required.")
				return
			
			if not username_new:
				st.error("Username is required.")
				return
			
			if len(username_new.strip()) < 3:
				st.error("Username must be at least 3 characters.")
				return
			
			if len(username_new.strip()) > 30:
				st.error("Username must be 30 characters or less.")
				return
			
			# Check if username is taken
			if user_mgr.is_username_taken(username_new):
				st.error(f"Username '{username_new}' is already taken. Please choose another.")
				return

			if mode == "Use my Fantrax secretId":
				if not secret_id:
					st.error("secretId cannot be empty.")
					return
				raw_pw = secret_id
			else:
				if not pw1:
					st.error("Password cannot be empty.")
					return
				if pw1 != pw2:
					st.error("Passwords do not match.")
					return
				raw_pw = pw1

			# Check if user exists
			existing_user = user_mgr.get_user_by_email(email_new)
			if existing_user:
				# Check if they have a password already
				if existing_user.get("password_hash"):
					st.error("An account with this email already exists with a password. Please log in instead.")
					return
				else:
					# User exists but no password - let them set one
					st.info("Found existing account without password. Setting password and username now...")
					try:
						pwd_hash = hash_password(raw_pw)
						user_mgr.set_user_password(existing_user["user_id"], pwd_hash)
						
						# Set username if not already set
						if not existing_user.get("username"):
							user_mgr.set_username(existing_user["user_id"], username_new.strip())
						
						_set_current_user(existing_user["user_id"])
						st.success("Password and username set successfully! You are now logged in.")
						st.info(
							"**Security note:** your password/secretId is never stored in plaintext. "
							"We store only a one-way bcrypt hash, and your Fantrax cookies are encrypted at rest."
						)
						st.rerun()
						return
					except Exception as e:
						st.error(f"Failed to set password/username: {e}")
						return

			# Hash and create new user
			try:
				pwd_hash = hash_password(raw_pw)
			except Exception as e:
				st.error(f"Failed to hash password: {e}")
				return

			try:
				# Create user via user_manager
				user = user_mgr.get_or_create_user(email_new)
				user_mgr.set_user_password(user["user_id"], pwd_hash)
				user_mgr.set_username(user["user_id"], username_new.strip())
			except ValueError as ve:
				st.error(str(ve))
				return
			except Exception as e:
				st.error(f"Failed to create user: {e}")
				return

			_set_current_user(user["user_id"])
			st.success("Account created and logged in.")
			st.info(
				"**Security note:** your password/secretId is never stored in plaintext. "
				"We store only a one-way bcrypt hash, and your Fantrax cookies are encrypted at rest."
			)
			st.rerun()


if __name__ == "__main__":
	# Test the login UI standalone
	login_ui()

