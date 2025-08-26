"""
Authentication helpers for Fantrax: Selenium login and cookie/session utilities.
"""

from __future__ import annotations

import os
import json
import pickle
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import certifi
import requests
from requests.cookies import create_cookie

UA_CHROME = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
	"AppleWebKit/537.36 (KHTML, like Gecko) "
	"Chrome/123.0.0.0 Safari/537.36"
)

FANTRAX_FXPA = "https://www.fantrax.com/fxpa/req"
FANTRAX_HOSTS = {".fantrax.com", "www.fantrax.com", "login.fantrax.com"}


# ───────────────────────── Cookie helpers ─────────────────────────

def _norm_domain(dom: Optional[str]) -> str:
	"""Normalize a cookie domain; default everything to .fantrax.com when in doubt."""
	if not dom:
		return ".fantrax.com"
	d = dom.lstrip(".").lower()
	return ".fantrax.com" if d.endswith("fantrax.com") else f".{d}"

def _norm_path(path: Optional[str]) -> str:
	return path or "/"

def _set_cookie_multi(sess: requests.Session, name: str, value: str,
					  domain: Optional[str], path: Optional[str],
					  secure: bool, http_only: bool) -> None:
	"""Set the cookie on multiple common Fantrax scopes so fxpa always receives it."""
	base_dom = _norm_domain(domain)
	base_path = _norm_path(path)
	for dom in {base_dom, *FANTRAX_HOSTS}:
		ck = create_cookie(
			name=name,
			value=value,
			domain=dom,
			path=base_path,
			secure=bool(secure),
			rest={"HttpOnly": bool(http_only)},
		)
		sess.cookies.set_cookie(ck)


# ───────────────────────── Selenium capture ─────────────────────────

# A) strengthen the driver to be "stealthy"
class FantraxAuth:
	LOGIN_URL = "https://www.fantrax.com/login"

	def build_driver(self, headless: bool = True):
		from selenium import webdriver
		from selenium.webdriver.chrome.options import Options
		from selenium.webdriver.chrome.service import Service
		from webdriver_manager.chrome import ChromeDriverManager

		opts = Options()
		if headless:
			opts.add_argument("--headless=new")
		# --- Stealth-ish hardening (helps with Cloudflare/automation heuristics)
		opts.add_argument("--disable-blink-features=AutomationControlled")
		opts.add_experimental_option("excludeSwitches", ["enable-automation"])
		opts.add_experimental_option("useAutomationExtension", False)

		# --- Make it look like a normal desktop Chrome session
		opts.add_argument("--window-size=1280,900")
		opts.add_argument("--disable-gpu")
		opts.add_argument("--no-sandbox")
		opts.add_argument("--disable-dev-shm-usage")
		opts.add_argument("--lang=en-US,en")
		opts.add_argument(f"--user-agent={UA_CHROME}")

		logging.getLogger(__name__).info("Setting up ChromeDriver via webdriver-manager")
		service = Service(ChromeDriverManager().install())
		driver = webdriver.Chrome(service=service, options=opts)

		# Mask webdriver flag as early as possible
		try:
			driver.execute_cdp_cmd(
				"Page.addScriptToEvaluateOnNewDocument",
				{"source": """
				  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
				  window.chrome = { runtime: {} };
				  Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
				  Object.defineProperty(navigator, 'language', {get: () => 'en-US'});
				  Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
				"""}
			)
		except Exception:
			pass

		return driver


	def cookies_from_driver(self, driver) -> List[Dict]:
		out: List[Dict] = []
		for c in driver.get_cookies():
			out.append({
				"name": c.get("name"),
				"value": c.get("value"),
				"domain": c.get("domain") or ".fantrax.com",
				"path": c.get("path") or "/",
				"secure": bool(c.get("secure", True)),
				"httpOnly": bool(c.get("httpOnly", True)),
			})
		return out

	def login_and_get_cookies(self, username: str, password: str, headless: bool = True) -> Dict[str, Dict]:
		from selenium.webdriver.common.by import By
		from selenium.webdriver.support.ui import WebDriverWait
		from selenium.webdriver.support import expected_conditions as EC
		from selenium.common.exceptions import NoSuchElementException
		import datetime as dt
		import time

		def save_debug(driver, tag="login_debug"):
			ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
			default_dir = Path(__file__).resolve().parent.parent / "data" / "logs" / "auth_debugs"
			outdir = Path(os.getenv("FANTRAX_DEBUG_DIR") or default_dir)
			outdir.mkdir(parents=True, exist_ok=True)
			(outdir / f"{tag}_{ts}.html").write_text(driver.page_source, encoding="utf-8")
			try:
				driver.save_screenshot(str(outdir / f"{tag}_{ts}.png"))
			except Exception:
				pass

		def wait_dom_ready(drv):
			WebDriverWait(drv, 20).until(lambda d: d.execute_script("return document.readyState") == "complete")

		def try_click_signin(drv):
			if "login" in (drv.current_url or "").lower():
				return
			xpaths = [
				"//a[contains(@href,'login')]",
				"//button[contains(translate(., 'LOGIN', 'login'),'login')]",
				"//a[contains(translate(., 'SIGN IN', 'sign in'),'sign in')]",
				"//button[contains(translate(., 'SIGN IN', 'sign in'),'sign in')]",
			]
			for xp in xpaths:
				try:
					drv.find_element(By.XPATH, xp).click()
					return
				except NoSuchElementException:
					continue

		def locate_in_context(drv, selectors):
			for sel in selectors:
				try:
					return drv.find_element(By.CSS_SELECTOR, sel)
				except NoSuchElementException:
					pass
			return None

		def locate_in_page_or_iframes(drv, selectors):
			el = locate_in_context(drv, selectors)
			if el:
				return el, None
			for idx, fr in enumerate(drv.find_elements(By.CSS_SELECTOR, "iframe")):
				try:
					drv.switch_to.frame(fr)
					el = locate_in_context(drv, selectors)
					if el:
						return el, idx
				finally:
					drv.switch_to.default_content()
			return None, None

		log = logging.getLogger(__name__)
		driver = self.build_driver(headless=headless)
		try:
			driver.get(self.LOGIN_URL)
			wait_dom_ready(driver)
			try_click_signin(driver)
			time.sleep(1.0)
			wait_dom_ready(driver)

			user_selectors = [
				"input[formcontrolname='email']","input[name='username']","input[name='email']",
				"input[type='email']","input#username","input#email","input[name='j_username']",
				"input[name='loginUsername']",
			]
			pass_selectors = [
				"input[formcontrolname='password']","input[type='password']","input#password",
				"input[name='password']","input[name='j_password']",
			]

			user_el, frame_idx = locate_in_page_or_iframes(driver, user_selectors)
			if frame_idx is not None:
				driver.switch_to.frame(driver.find_elements(By.CSS_SELECTOR, "iframe")[frame_idx])
			pwd_el = locate_in_context(driver, pass_selectors)

			if not user_el or not pwd_el:
				save_debug(driver, "fantrax_login_no_fields")
				raise RuntimeError("Could not locate Fantrax login fields.")

			user_el.clear(); user_el.send_keys(username)
			pwd_el.clear();	 pwd_el.send_keys(password)

			# Dismiss cookie banner if present
			try:
				driver.switch_to.default_content()
				btns = driver.find_elements(By.CSS_SELECTOR, "button.toast__button--DISMISS")
				if btns: btns[0].click()
			except Exception:
				pass

			# Submit
			btn = locate_in_context(driver, ["button[type='submit']","input[type='submit']","button[name='login']"])
			if btn:
				btn.click()
			else:
				from selenium.webdriver.common.keys import Keys
				pwd_el.send_keys(Keys.RETURN)

			# Wait for cookies or navigation
			WebDriverWait(driver, 25).until(lambda d: ("fantasy" in (d.current_url or "").lower()) or (len(d.get_cookies()) > 0))

			# Hydrate SPA storage and cookies
			try:
				driver.get("https://www.fantrax.com/fantasy/league")
				WebDriverWait(driver, 12).until(lambda d: d.execute_script("return document.readyState") == "complete")
				WebDriverWait(driver, 8).until(
					lambda d: d.execute_script("return (localStorage && localStorage.length) || (sessionStorage && sessionStorage.length)")
				)
			except Exception:
				pass

			# Cloudflare check
			driver.switch_to.default_content()
			if driver.find_elements(By.CSS_SELECTOR, "iframe[title*='Cloudflare']"):
				save_debug(driver, "fantrax_cloudflare_challenge")
				raise RuntimeError("Cloudflare challenge detected. Complete it once, then reuse cookies.")

			cookies_list = self.cookies_from_driver(driver)
			if not cookies_list:
				save_debug(driver, "fantrax_login_no_cookies")
				raise RuntimeError("Login completed but no cookies were captured.")

			# Capture web storage (optional tokens)
			try:
				storage = driver.execute_script("""
					const out={local:{}, session:{}};
					try{ for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i); out.local[k]=localStorage.getItem(k);} }catch(e){}
					try{ for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i); out.session[k]=sessionStorage.getItem(k);} }catch(e){}
					return out;
				""") or {"local": {}, "session": {}}
			except Exception:
				storage = {"local": {}, "session": {}}

			# Log only key counts
			try:
				log.info(f"Captured cookies={len(cookies_list)}	 storage(local={len(storage.get('local',{}))}, session={len(storage.get('session',{}))})")
			except Exception:
				pass

			return {"cookies": cookies_list, "storage": storage}

		finally:
			try: driver.quit()
			except Exception: pass


# ───────────────────────── Session builders ─────────────────────────

def _apply_spa_headers(sess: requests.Session) -> None:
	sess.headers.update({
		"User-Agent": UA_CHROME,
		"Accept": "application/json, text/plain, */*",
		"Origin": "https://www.fantrax.com",
		"Referer": "https://www.fantrax.com/fantasy/league",
		"X-Requested-With": "XMLHttpRequest",
		"Accept-Language": "en-US,en;q=0.9",
		# This matters for fxpa — the site expects text/plain payloads
		"Content-Type": "text/plain",
	})

def _maybe_add_tokens_from_storage(sess: requests.Session, storage: Dict[str, Dict]) -> None:
	flat: Dict[str, str] = {}
	for src in (storage.get("local") or {}, storage.get("session") or {}):
		if isinstance(src, dict):
			for k, v in src.items():
				if isinstance(v, str):
					flat[k] = v

	def _is_jwt(s: str) -> bool:
		import re
		return bool(re.match(r"^[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_+/=]+$", s))

	# Prefer explicit Authorization-like keys; fall back to JWT-looking values
	auth = None
	for k, v in flat.items():
		kl = k.lower()
		if "authorization" in kl or kl in {"auth", "bearer"}:
			auth = v.strip()
			break
	if not auth:
		for v in flat.values():
			vv = (v or "").strip()
			if vv.lower().startswith("bearer ") or _is_jwt(vv):
				auth = vv if vv.lower().startswith("bearer ") else f"Bearer {vv}"
				break

	xsrf = None
	for k, v in flat.items():
		if "xsrf" in k.lower() or "csrf" in k.lower():
			xsrf = v
			break

	if auth and auth.lower().startswith("bearer ") and "." in auth:
		sess.headers["Authorization"] = auth
	if xsrf:
		sess.headers["X-XSRF-TOKEN"] = xsrf

def load_requests_session_from_artifacts(artifacts: Dict[str, Dict]) -> requests.Session:
	"""Build a session from Selenium artifacts (cookies + optional storage tokens)."""
	s = requests.Session()
	os.environ["SSL_CERT_FILE"] = certifi.where()
	os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

	# Cookies on multiple scopes (mirrors the permissive upload workflow)
	for c in artifacts.get("cookies", []) or []:
		n, v = c.get("name"), c.get("value")
		if not n or v is None:
			continue
		_set_cookie_multi(
			s, n, v,
			domain=c.get("domain"),
			path=c.get("path"),
			secure=c.get("secure", True),
			http_only=c.get("httpOnly", True),
		)

	_apply_spa_headers(s)
	_maybe_add_tokens_from_storage(s, artifacts.get("storage") or {})
	return s

def _hydrate_requests_session(session: requests.Session, cookies_list: List[Dict]) -> requests.Session:
	"""Legacy: cookies-only builder, now also duplicates domains + sets SPA headers."""
	os.environ["SSL_CERT_FILE"] = certifi.where()
	os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

	for c in cookies_list:
		n, v = c.get("name"), c.get("value")
		if not n or v is None:
			continue
		_set_cookie_multi(
			session, n, v,
			domain=c.get("domain"),
			path=c.get("path"),
			secure=c.get("secure", True),
			http_only=c.get("httpOnly", True),
		)

	_apply_spa_headers(session)
	return session

def load_requests_session_from_pickle(cookie_file: str) -> requests.Session:
	with open(cookie_file, "rb") as f:
		cookies_list = pickle.load(f)
	return _hydrate_requests_session(requests.Session(), cookies_list)

def load_requests_session_from_cookie_list(cookies_list: List[Dict]) -> requests.Session:
	return _hydrate_requests_session(requests.Session(), cookies_list)


# ───────────────────────── fxpa helpers ─────────────────────────

def _fxpa_post(sess: requests.Session, payload: Dict, timeout: int = 20) -> requests.Response:
	"""Post to fxpa with the SPA's expected content-type (text/plain)."""
	# Session already has Content-Type: text/plain; keep it consistent here
	return sess.post(FANTRAX_FXPA, data=json.dumps(payload), timeout=timeout)

def validate_logged_in(sess: requests.Session, league_id: Optional[str] = None) -> bool:
	try:
		probe = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}]}
		j = _fxpa_post(sess, probe).json()
		if (j.get("pageError") or {}).get("code") == "WARNING_NOT_LOGGED_IN":
			return False
		resp0 = (j.get("responses") or [{}])[0]
		if (resp0.get("pageError") or {}).get("code") == "WARNING_NOT_LOGGED_IN":
			return False
		d = resp0.get("data") or {}
		return bool(d.get("tableList") or d.get("leagues") or d.get("numLeagues"))
	except Exception:
		logging.getLogger(__name__).exception("validate_logged_in exception")
		return False

def fetch_user_leagues(session: requests.Session) -> List[Dict[str, str]]:
	"""Return active leagues with the user's team in each league."""
	payload = {
		"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}],
		"uiv": 3, "refUrl": "https://www.fantrax.com/fantasy/league",
		"dt": 0, "at": 0, "av": "0.0", "tz": "America/Los_Angeles", "v": "167.0.1",
	}
	logger = logging.getLogger(__name__)
	logger.info("Fetching user leagues via fxpa getAllLeagues")

	try:
		data = _fxpa_post(session, payload).json()
	except Exception:
		logger.warning("getAllLeagues: non-JSON or request error", exc_info=True)
		return []

	if (data.get("pageError") or {}).get("code") == "WARNING_NOT_LOGGED_IN":
		return []

	resp0 = (data.get("responses") or [{}])[0]
	if (resp0.get("pageError") or {}).get("code") == "WARNING_NOT_LOGGED_IN":
		return []

	leagues: List[Dict[str, str]] = []

	def _add(league_id, team_id, league_name, team_name):
		if league_id and team_id and str(team_id) != "NULL":
			leagues.append({
				"leagueId": str(league_id),
				"teamId": str(team_id),
				"league": league_name or "",
				"team": team_name or "",
			})

	d = resp0.get("data") or {}

	# A) tableList → rows → cells
	for table in d.get("tableList", []) or []:
		for row in table.get("rows", []) or []:
			cells = row.get("cells", []) or []
			if len(cells) >= 4 and isinstance(cells[2], dict) and isinstance(cells[3], dict):
				_add(cells[2].get("leagueId"), cells[3].get("teamId"),
					 cells[2].get("content", ""), cells[3].get("content", ""))

	# B) fallback: leagues[].leaguesTeams[]
	if not leagues:
		for lg in d.get("leagues", []) or []:
			for lt in lg.get("leaguesTeams", []) or []:
				_add(lt.get("leagueId"), lt.get("teamId"), lt.get("league", ""), lt.get("team", ""))

	# Write raw JSON (debug)
	try:
		_debug_json_path = Path(__file__).resolve().parent.parent / "data" / "logs" / "raw_getAllLeagues.json"
		_debug_json_path.parent.mkdir(parents=True, exist_ok=True)
		_debug_json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	except Exception:
		pass

	active = [lt for lt in leagues if lt.get("teamId") and lt["teamId"] != "NULL"]
	logger.info(f"Found {len(active)} active leagues")
	return active


# ───────────────────────── user profile ─────────────────────────

def fetch_user_profile(session: requests.Session) -> Dict[str, str]:
	"""Return user profile info from the same getAllLeagues call."""
	logger = logging.getLogger(__name__)
	logger.info("Fetching user profile via fxpa getAllLeagues")

	payload = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}]}
	try:
		data = _fxpa_post(session, payload).json()
		info: Dict[str, str] = {}

		for r in (data.get("responses") or []):
			d = r.get("data") or {}
			ui = d.get("userInfo") or {}
			if ui:
				info = {
					"username": ui.get("username") or "",
					"email": ui.get("email") or "",
					"userId": ui.get("userId") or "",
					"timezone": ui.get("timezoneDisplay") or ui.get("timezoneCode") or ui.get("timezone") or "",
					"timezoneCode": ui.get("timezoneCode") or "",
					"logo": ui.get("logo") or "",
					"numLeagues": str(d.get("numLeagues") or ""),
				}
				break
		logger.info(f"User profile parsed: username={info.get('username','')}")
		return info
	except Exception:
		logger.exception("Failed fetching user profile")
		return {}


# ───────────────────────── misc utils ─────────────────────────

def write_cookie_pickle(cookies_list: List[Dict], path: str) -> None:
	logging.getLogger(__name__).info(f"Writing cookie pickle to {path}")
	with open(path, "wb") as f:
		pickle.dump(cookies_list, f)

def json_dumps_safe(obj) -> str:
	try:
		return json.dumps(obj, ensure_ascii=False, indent=2)
	except Exception:
		return "{}"

def _ensure_log_dir(path: str) -> None:
	Path(path).mkdir(parents=True, exist_ok=True)

def configure_logging(default_path: str = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log") -> None:
	_ensure_log_dir(str(Path(default_path).parent))
	logger = logging.getLogger()
	if any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == str(Path(default_path)) for h in logger.handlers):
		return
	logger.setLevel(logging.INFO)
	fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
	fh = logging.FileHandler(default_path); fh.setFormatter(fmt); logger.addHandler(fh)
	if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
		ch = logging.StreamHandler(); ch.setFormatter(fmt); logger.addHandler(ch)

# B) NEW: one-call helper for Streamlit to run a true headless login
def headless_login_build_session(
	username: str,
	password: str,
	*,
	headless: bool = True,
	validate: bool = True,
) -> Tuple[requests.Session, Dict[str, Dict]]:
	"""
	Perform a headless login using Selenium, hydrate cookies + storage, and
	return (requests.Session, artifacts). Raises RuntimeError on failure when
	validate=True and we cannot confirm auth via fxpa/getAllLeagues.
	"""
	logger = logging.getLogger(__name__)
	auth = FantraxAuth()
	artifacts = auth.login_and_get_cookies(username, password, headless=headless)

	# Build the session exactly like the BYOC path
	sess = load_requests_session_from_artifacts(artifacts)

	if validate:
		ok = validate_logged_in(sess)
		if not ok:
			# Give hydration one more shot (sometimes headers/cookies need a warm hit)
			try:
				payload = {"msgs":[{"method":"getAllLeagues","data":{"view":"LEAGUES"}}]}
				_fxpa_post(sess, payload, timeout=15)
				ok = validate_logged_in(sess)
			except Exception:
				ok = False

		if not ok:
			# Provide a helpful, actionable error
			cks = [c for c in sess.cookies if "fantrax" in (c.domain or "")]
			raise RuntimeError(
				"Headless login failed validation (getAllLeagues said not logged in). "
				f"Captured cookies={len(artifacts.get('cookies') or [])} "
				f"(session carries {len(cks)} cookies). "
				"Try again with visible (non-headless) browser once to clear any challenge, "
				"then headless will work with the saved cookies."
			)

	return sess, artifacts
