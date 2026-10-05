#!/usr/bin/env python3
"""
Script to set up Fantrax authentication cookies using Selenium.
This script will:
1. Open a Chrome browser
2. Let you log in to Fantrax
3. Save the authentication cookies
4. Visit your league page to ensure cookies are properly scoped
"""

import time
import pickle
import os
from pathlib import Path
from configparser import ConfigParser
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from requests import Session
from fantraxapi import FantraxAPI

def load_config():
	"""Load configuration from config.ini"""
	config = ConfigParser()
	config_path = Path("config.ini")
	
	if not config_path.exists():
		raise FileNotFoundError(
			"config.ini not found! Please create it with your league_id and cookie_path."
		)
	
	config.read(config_path)
	if "fantrax" not in config:
		raise KeyError(
			"config.ini must have a [fantrax] section with league_id and cookie_path."
		)
		
	return {
		"league_id": config["fantrax"]["league_id"],
		"cookie_path": config["fantrax"]["cookie_path"]
	}

def main():
	# Load configuration
	config = load_config()
	cookie_path = config["cookie_path"]
	league_id = config["league_id"]
	
	# Ensure cookie directory exists
	os.makedirs(os.path.dirname(cookie_path), exist_ok=True)

	# Set up Chrome options
	chrome_options = Options()
	chrome_options.add_argument("--window-size=1920,1600")
	chrome_options.add_argument(
		"user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
		"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36"
	)
	
	# Initialize Chrome driver
	service = Service(ChromeDriverManager().install())
	driver = webdriver.Chrome(service=service, options=chrome_options)

	try:
		# Go to login page
		print("Opening Fantrax login page...")
		driver.get("https://www.fantrax.com/login")
		print("\nPlease log in to Fantrax in the browser window that just opened.")
		print("After you're fully logged in (homepage loads), press Enter here to continue...")
		input()

		# Visit league homepage to ensure cookies are properly scoped
		print(f"\nVisiting your league homepage to validate cookies...")
		driver.get(f"https://www.fantrax.com/fantasy/league/{league_id}/home")
		time.sleep(2)  # Brief pause to ensure page loads

		# Save cookies
		cookies = driver.get_cookies()
		with open(cookie_path, "wb") as f:
			pickle.dump(cookies, f)
		
		print(f"\nSuccessfully saved cookies to: {cookie_path}")

		# Optional: detect and persist team id/name for this cookie/session
		try:
			print("\nAttempting to detect your team in this league using the saved cookies…")
			session = Session()
			with open(cookie_path, "rb") as f:
				for c in pickle.load(f):
					session.cookies.set(c["name"], c["value"])

			api = FantraxAPI(league_id, session=session)
			teams = api.teams
			print("\nSelect your fantasy team to bind with this cookie:")
			for i, t in enumerate(teams, 1):
				print(f"{i:2d}. {t.name} ({t.short}) - {t.team_id}")
			sel = None
			while sel is None:
				try:
					sel_in = input("Enter number (or press Enter to skip): ").strip()
					if not sel_in:
						break
					sel = int(sel_in)
					if not (1 <= sel <= len(teams)):
						sel = None
						print("Invalid selection, try again.")
				except ValueError:
					print("Invalid selection, try again.")

			if sel:
				chosen = teams[sel - 1]
				# Persist to config.ini
				cfg = ConfigParser()
				cfg_path = Path("config.ini")
				cfg.read(cfg_path)
				if "fantrax" not in cfg:
					cfg["fantrax"] = {}
				cfg["fantrax"]["team_id"] = chosen.team_id
				cfg["fantrax"]["team_name"] = chosen.name
				with open(cfg_path, "w") as fp:
					cfg.write(fp)
				print(f"\nBound this cookie to team: {chosen.name} (TeamID={chosen.team_id})")
			else:
				print("\nSkipped binding team; you can select it later when running scripts.")
		except Exception as e_det:
			print(f"\nCould not auto-detect team from cookies: {e_det}")
			print("You can still run scripts; they'll prompt you to select your team and will persist it.")

		print("\nYou can now use the substitutions.py or submit_claim.py scripts to manage your roster!")
		
	except Exception as e:
		print(f"\n❌ Error: {e}")
		print("Please try again or check your configuration.")
	finally:
		print("\nClosing browser...")
		driver.quit()

if __name__ == "__main__":
	main()
