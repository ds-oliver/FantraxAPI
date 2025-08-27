#!/usr/bin/env python3
"""
Streamlit (BYOC) — Simple Substitutions GUI

Keeps original auth/cookie practices:
- Upload artifacts (cookies+storage) or legacy cookie file
- Selenium capture (visible) and headless login
- Cookie debug expander, soft validation via fxpa, profile card

Lineup changes:
- Ultra-simplified, same as substitutions_v2.py (FantraxAPI.swap_players)
- Supports dropdown pick OR "get player by name" text fields
- Brief verify loop (eventual consistency)

Also keeps:
- Drop a player flow via DropService
"""

from __future__ import annotations

import io
import time
import logging
import pickle
from pathlib import Path
from typing import Optional, Dict, Union, Any

import pandas as pd
import streamlit as st
from requests import Session
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster

# --- auth + cookie helpers (unchanged from your original) ---
from utils.cookie_import import read_auth_file  # -> {"cookies":[...], "storage": {...}}
from utils.auth_helpers import (
    FantraxAuth,
    fetch_user_leagues,
    validate_logged_in,
)

# Prefer the token-aware builder; fall back to cookies-only builder if absent.
try:
    from utils.auth_helpers import load_requests_session_from_artifacts  # cookies + storage → headers
except Exception:
    from utils.auth_helpers import load_requests_session_from_cookie_list as load_requests_session_from_artifacts  # type: ignore

# Optional user card
try:
    from utils.auth_helpers import fetch_user_profile  # type: ignore
except Exception:
    def fetch_user_profile(session: Session):
        return {}

# --- Drop player helper (kept) ---
from utils.roster_ops import DropService  # ONLY using DropService; no LineupService imports


# ---- logging bootstrap (same behavior as your original) ----
try:
    from utils.auth_helpers import configure_logging  # type: ignore
except Exception:
    def configure_logging(default_path: str = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log") -> None:
        Path(default_path).parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        if not any(getattr(h, "baseFilename", "") == str(Path(default_path)) for h in root.handlers if isinstance(h, logging.FileHandler)):
            fh = logging.FileHandler(default_path)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
            ch = logging.StreamHandler()
            ch.setFormatter(fmt)
            root.addHandler(ch)

st.set_page_config(page_title="Fantrax (BYOC) — Simple Subs", page_icon="🔁", layout="wide")

LOG_PATH = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log"
configure_logging(LOG_PATH)
logger = logging.getLogger(__name__)
logger.info("=" * 100)
logger.info("Streamlit app started (BYOC SIMPLE SUBS mode)")

# Set debug level for fantraxapi and main app
logging.getLogger("fantraxapi").setLevel(logging.DEBUG)
logging.getLogger(__name__).setLevel(logging.DEBUG)


# ---------- tiny helpers ----------
def _build_session() -> Optional[Session]:
    """Build a fresh requests.Session from whatever the user provided."""
    artifacts = st.session_state.get("auth_artifacts")
    if not artifacts:
        return None
    try:
        # Primary: token-aware builder (cookies + storage → headers)
        return load_requests_session_from_artifacts(artifacts)
    except Exception:
        logger.exception("Failed to build session from artifacts")
        return None

# helper to turn a selected label into a 1-based index string (as subs_v2 expects)
def _label_to_index_str(choice: str, opts: Dict[str, Any]) -> str:
    return str(list(opts.keys()).index(choice) + 1)

def _make_table(rows):
    data = []
    for r in rows:
        if r.player:
            pos = r.pos.short_name or "BN"
            name = r.player.name
            team = r.player.team_short_name or r.player.team_name or ""
            fppg = f"{r.fppg:.1f}" if r.fppg is not None else "-"
            data.append([pos, name, team, fppg])
    return data


def _render_roster_tables(roster: Roster, starters_only: bool = False):
    starters = roster.get_starters()
    bench = [] if starters_only else roster.get_bench_players()

    st.markdown("### Starters")
    st.table(_make_table(starters))
    if bench:
        st.markdown("### Bench")
        st.table(_make_table(bench))

    return starters, bench


def _refresh_roster(api: FantraxAPI, team_id: str) -> Roster:
    # Small delay helps when the site is eventually consistent
    time.sleep(0.6)
    return api.roster_info(team_id)

def make_substitution_example(
	league_id: str,
	team_id: Optional[str] = None,
	*,
	starter_select: Optional[Union[int, str]] = None,
	bench_select: Optional[Union[int, str]] = None,
	verify_retries: int = 4,
	verify_sleep_s: float = 0.8,
	session: Optional[Session] = None,
) -> Dict[str, Any]:
	"""
	Perform a simple substitution using FantraxAPI.swap_players, aligned with substitutions_v2,
	but authenticated via the existing BYOC auth in app.py (artifacts → session).

	Args:
		league_id: Fantrax league id.
		team_id: Optional team id. If None, uses api.teams[0] (same as substitutions_v2).
		starter_select: Which starter to bench. Either a 1-based index (int or numeric str) or an exact player name (str).
		bench_select: Which bench player to start. Either a 1-based index (int or numeric str) or an exact player name (str).
		verify_retries: How many times to poll for verification after swap (eventual consistency).
		verify_sleep_s: Sleep seconds between verify attempts.
		session: (Optional) an already-authenticated requests.Session. If None, built from st.session_state["auth_artifacts"].

	Returns:
		{
			"ok": bool,                 # swap_players return value
			"verified": bool,           # whether post-check shows expected starter/bench state
			"out_id": str, "in_id": str,
			"team_id": str,
			"reason": Optional[str],    # if not verified, best-effort reason
		}
	"""
	# --- Build/obtain authenticated session (BYOC artifacts) ---
	if session is None:
		artifacts = st.session_state.get("auth_artifacts")
		if not artifacts:
			raise RuntimeError("No auth artifacts in session. Upload/capture cookies first.")
		try:
			session = load_requests_session_from_artifacts(artifacts)
		except Exception as e:
			logger.exception("Failed to build session from artifacts")
			raise RuntimeError(f"Could not create an authenticated session from artifacts: {e}") from e

	api = FantraxAPI(league_id, session=session)

	# --- Resolve team ---
	if team_id:
		try:
			my_team = api.team(team_id)
		except Exception as e:
			raise RuntimeError(f"Error finding team {team_id}: {e}") from e
	else:
		my_team = api.teams[0]  # match substitutions_v2 fallback
		logger.info("No team_id provided, using first team: %s", my_team.name)

	# --- Fetch roster & pools ---
	roster = api.roster_info(my_team.team_id)
	starters = roster.get_starters()
	bench = roster.get_bench_players()

	# --- Helpers to resolve a row from user selection ---
	def _resolve_row(select: Union[int, str, None], pool, *, bench_expected: bool) -> Optional[Any]:
		if select is None:
			return None
		# numeric index (1-based), e.g., 1, "2"
		if isinstance(select, int) or (isinstance(select, str) and select.isdigit()):
			idx = int(select)
			if 1 <= idx <= len(pool):
				return pool[idx - 1]
			raise ValueError(f"Invalid {'bench' if bench_expected else 'starter'} number: {idx}")
		# exact name
		if isinstance(select, str):
			cand = roster.get_player_by_name(select.strip())
			if not cand:
				raise ValueError(f"Player '{select}' not found on roster.")
			if bench_expected and cand.pos_id != "0":
				raise ValueError(f"Player '{select}' is not on the bench.")
			if (not bench_expected) and cand.pos_id == "0":
				raise ValueError(f"Player '{select}' is not a starter.")
			return cand
		return None

	# --- Resolve selected rows ---
	starter_row = _resolve_row(starter_select, starters, bench_expected=False)
	bench_row   = _resolve_row(bench_select, bench, bench_expected=True)

	if not starter_row or not bench_row:
		raise ValueError("Both a valid starter and a valid bench player must be provided.")

	out_id = starter_row.player.id
	in_id  = bench_row.player.id

	# --- Log current state (useful for debugging) ---
	logger.info("\n=== PRE-SUBSTITUTION STATE ===")
	for r in starters:
		if getattr(r, "player", None):
			logger.info("  Starter: %s (%s) - Pos: %s", r.player.name, r.player.id, r.pos.short_name)
	for r in bench:
		if getattr(r, "player", None):
			logger.info("  Bench: %s (%s) - Pos: %s", r.player.name, r.player.id, r.pos.short_name)

	logger.info("\n=== PLANNED SUBSTITUTION ===")
	logger.info("Moving OUT: %s — %s (ID: %s) → Bench", starter_row.pos.short_name, starter_row.player.name, out_id)
	logger.info("Moving IN:  %s — %s (ID: %s) → Starting XI", starter_row.pos.short_name, bench_row.player.name, in_id)

	# --- Execute swap (exact substitutions_v2 behavior) ---
	logger.info("\n=== EXECUTING SUBSTITUTION (swap_players) ===")
	ok = bool(api.swap_players(my_team.team_id, out_id, in_id))
	logger.info("[swap] api.swap_players -> %s", ok)

	# --- Verify loop (eventual consistency) ---
	verified = False
	reason = None
	for i in range(max(0, verify_retries)):
		time.sleep(max(0.0, verify_sleep_s))
		after = api.roster_info(my_team.team_id)
		starter_ids = {r.player.id for r in after.get_starters() if getattr(r, "player", None)}
		verified = (in_id in starter_ids) and (out_id not in starter_ids)
		logger.info("[swap] verify attempt %d/%d -> %s", i + 1, verify_retries, verified)
		if verified:
			break
	if ok and not verified:
		reason = "optimistic (server accepted swap but roster view not yet updated)"

	# --- Post-state logging ---
	logger.info("\n=== POST-SUBSTITUTION STATE ===")
	new_roster = api.roster_info(my_team.team_id)
	for r in new_roster.get_starters():
		if getattr(r, "player", None):
			logger.info("  Starter: %s (%s) - Pos: %s", r.player.name, r.player.id, r.pos.short_name)
	for r in new_roster.get_bench_players():
		if getattr(r, "player", None):
			logger.info("  Bench: %s (%s) - Pos: %s", r.player.name, r.player.id, r.pos.short_name)

	return {
		"ok": ok,
		"verified": bool(verified),
		"reason": reason,
		"out_id": out_id,
		"in_id": in_id,
		"team_id": my_team.team_id,
	}

# ---------- UI: Auth (kept from your original) ----------
def ui_login_section():
    st.header("Authenticate")
    tabs = st.tabs(["Upload cookie/artifacts (recommended)", "Capture via Selenium (one-time)", "Headless login (background)"])

    # --- Tab 1: Upload artifacts/cookies ---
    with tabs[0]:
        st.caption(
            "Upload a Selenium cookie pickle (e.g., `fantraxloggedin.cookie`) or a Cookie-Editor JSON export. "
            "We keep everything **in memory**; nothing is written to disk."
        )
        up = st.file_uploader("Upload your Fantrax cookie or artifacts", type=["cookie", "pkl", "pickle", "bin", "json"])
        col_a, col_b = st.columns([1, 1])
        with col_a:
            use_btn = st.button("Use uploaded file", type="primary", disabled=up is None)
        with col_b:
            clear_btn = st.button("Forget my cookie")

        if clear_btn:
            for k in ("auth_artifacts", "artifacts_pickle_bytes", "cookies_pickle_bytes"):
                st.session_state.pop(k, None)
            st.success("Cookie cleared from this session.")

        if use_btn and up:
            try:
                artifacts = read_auth_file(up)  # -> {"cookies":[...], "storage": {"local":{...},"session":{...}}}
                st.session_state["auth_artifacts"] = artifacts

                # Prepare convenience downloads (kept in-memory)
                buf_art = io.BytesIO(); pickle.dump(artifacts, buf_art)
                st.session_state["artifacts_pickle_bytes"] = buf_art.getvalue()
                buf_ck = io.BytesIO(); pickle.dump(artifacts.get("cookies", []), buf_ck)
                st.session_state["cookies_pickle_bytes"] = buf_ck.getvalue()

                st.success("Cookie/artifacts loaded.")
                logger.info("User uploaded cookie/artifacts successfully")
            except Exception as e:
                logger.exception("Cookie import failed")
                st.error(f"Could not read cookie/artifacts: {e}")

        # Optional: give users their normalized downloads back
        dl_cols = st.columns(2)
        with dl_cols[0]:
            if st.session_state.get("artifacts_pickle_bytes"):
                st.download_button(
                    "Download artifacts (cookies + storage)",
                    data=st.session_state["artifacts_pickle_bytes"],
                    file_name="fantrax_artifacts.pkl",
                    mime="application/octet-stream",
                )
        with dl_cols[1]:
            if st.session_state.get("cookies_pickle_bytes"):
                st.download_button(
                    "Download cookies-only (legacy)",
                    data=st.session_state["cookies_pickle_bytes"],
                    file_name="fantraxloggedin.cookie",
                    mime="application/octet-stream",
                )

    # --- Tab 2: Selenium capture (visible window) ---
    with tabs[1]:
        with st.form("login_form"):
            user = st.text_input("Fantrax username or email")
            pw = st.text_input("Fantrax password", type="password")
            non_headless = st.checkbox("Open a visible browser window (recommended for first time)", value=True)
            submit = st.form_submit_button("Log in and capture")

        if submit:
            try:
                logger.info("Submitting login via FantraxAuth")
                auth = FantraxAuth()
                artifacts = auth.login_and_get_cookies(user, pw, headless=not non_headless)
                # Persist in memory
                st.session_state["auth_artifacts"] = artifacts

                # Prepare downloads (optional)
                buf_art = io.BytesIO(); pickle.dump(artifacts, buf_art)
                st.session_state["artifacts_pickle_bytes"] = buf_art.getvalue()
                buf_ck = io.BytesIO(); pickle.dump(artifacts.get("cookies", []), buf_ck)
                st.session_state["cookies_pickle_bytes"] = buf_ck.getvalue()

                st.success("Logged in. Cookies captured.")
                logger.info("Login successful; artifacts stored in session")
            except Exception as e:
                logger.exception("Login failed")
                st.error(f"Login failed: {e}")

    # --- Tab 3: Headless background login ---
    with tabs[2]:
        st.caption("Runs a full login in a background headless browser, then hydrates a session.")
        with st.form("login_form_headless"):
            hu = st.text_input("Fantrax username or email", key="h_user")
            hp = st.text_input("Fantrax password", type="password", key="h_pw")
            submit_h = st.form_submit_button("Log in (headless)")

        if submit_h:
            if not hu or not hp:
                st.warning("Enter username and password.")
            else:
                with st.spinner("Signing in headlessly…"):
                    try:
                        from utils.auth_helpers import headless_login_build_session
                        sess, artifacts = headless_login_build_session(hu, hp, headless=True, validate=True)
                        st.session_state["auth_artifacts"] = artifacts
                        # Optional: keep a ready-to-use session in cache
                        st.session_state["__fantrax_cached_session__"] = sess
                        st.success("Headless login successful.")
                    except Exception as e:
                        logger.exception("Headless login failed")
                        st.error(str(e))

    # Debug pane (unchanged)
    with st.expander("Auth debug", expanded=False):
        art = st.session_state.get("auth_artifacts") or {}
        loc = (art.get("storage") or {}).get("local", {}) or {}
        ses = (art.get("storage") or {}).get("session", {}) or {}
        st.caption(f"localStorage keys: {len(loc)}; sessionStorage keys: {len(ses)}")


# ---------- UI: Simple substitutions (exact substitutions_v2 flow in GUI) ----------
def ui_simple_subs_section():
    st.header("Your Leagues and Rosters")

    if "auth_artifacts" not in st.session_state:
        st.info("Upload cookie or capture via Selenium above.")
        st.stop()

    logger.info("Hydrating requests session from artifacts (cookies + storage if present)")
    session = _build_session()
    if not session:
        st.error("Could not create a session from your cookie/artifacts.")
        st.stop()

    # Optional: quick cookie/header sanity check
    with st.expander("Cookie debug", expanded=False):
        try:
            import requests as _rq
            req = _rq.Request("POST", "https://www.fantrax.com/fxpa/req", data="{}")
            prepped = session.prepare_request(req)
            st.write({
                "CookieHeaderLen": len(prepped.headers.get("Cookie", "")),
                "Content-Type": prepped.headers.get("Content-Type"),
                "HasAuthHeader": bool(prepped.headers.get("Authorization")),
                "HasXSRFHeader": bool(prepped.headers.get("X-XSRF-TOKEN")),
            })
            sent = [{"name": c.name, "domain": c.domain, "path": c.path}
                    for c in session.cookies if "fantrax" in (c.domain or "")][:50]
            st.write(sent)
        except Exception as _e:
            st.write(f"prep failed: {_e}")

    # Soft validation (we still proceed if False)
    is_valid = validate_logged_in(session)
    if not is_valid:
        st.warning("Your cookie may be expired or missing tokens. We'll still try to list leagues from cookies.")
        with st.expander("fxpa probe (debug)", expanded=False):
            try:
                probe = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}]}
                j = session.post("https://www.fantrax.com/fxpa/req", json=probe, timeout=20).json()
                import json as _json
                st.code((_json.dumps(j, indent=2)[:1500]))
            except Exception:
                st.caption("Probe failed.")

    # List leagues via fxpa
    logger.info("Fetching user leagues via fxpa getAllLeagues")
    leagues = fetch_user_leagues(session)
    if not leagues:
        st.error("No leagues found (cookie may be expired).")
        st.stop()

    # Sidebar user card
    with st.sidebar:
        try:
            info = fetch_user_profile(session) or {}
            logger.info(f"User info: {info}")
        except Exception:
            info = {}
        if info:
            st.subheader("Account")
            if info.get("logo"): st.image(info["logo"], width=64)
            st.write(info.get("username", ""))
            tz = info.get("timezone") or info.get("timezoneCode", "")
            line = " • ".join([x for x in (info.get("email", ""), tz) if x])
            if line: st.caption(line)
            if info.get("numLeagues"): st.caption(f"Leagues: {info['numLeagues']}")

    # Choose a league (showing user's team)
    choices = {f"{lt['league']} — your team: {lt['team']}": lt for lt in leagues}
    label = st.selectbox("Choose a league", list(choices.keys()))
    picked = choices[label]
    league_id = picked["leagueId"]
    team_id = picked["teamId"]
    st.caption(f"Selected leagueId={league_id}, your teamId={team_id}")

    api = FantraxAPI(league_id=league_id, session=session)

    # Current roster
    try:
        roster = api.roster_info(team_id)
    except Exception as e:
        logger.exception("Failed to fetch roster")
        st.error(f"Failed to fetch roster: {e}")
        return

    st.subheader(label)
    starters_only = st.checkbox("Show starters only", value=False)
    starters, bench = _render_roster_tables(roster, starters_only=starters_only)

    st.divider()
    st.subheader("Actions")
                
    # --- Make a substitution (SIMPLE: just swap_players, like substitutions_v2) ---
    st.markdown("### Make a Substitution (simple swap)")
    with st.form("simple_swap_form", clear_on_submit=False):
        st.caption("Pick any current starter to bench and any bench player to start. "
                   "This uses FantraxAPI.swap_players (no extra logic).")

        starter_opts = {f"{i+1}. {r.pos.short_name} — {r.player.name} ({r.player.team_short_name or r.player.team_name})": r
                        for i, r in enumerate(starters)}
        bench_opts = {f"{i+1}. {r.pos.short_name} — {r.player.name} ({r.player.team_short_name or r.player.team_name})": r
                      for i, r in enumerate(bench)}

        starter_choice = st.selectbox("Starter to move to bench",
                                      options=list(starter_opts.keys()) or ["—"],
                                      index=0 if starter_opts else None)
        bench_choice = st.selectbox("Bench player to move to starters",
                                    options=list(bench_opts.keys()) or ["—"],
                                    index=0 if bench_opts else None)

        # Optional name inputs (exact match), mirroring substitutions_v2
        st.write("Or pick by name (overrides the dropdowns):")
        coln1, coln2 = st.columns(2)
        with coln1:
            starter_by_name = st.text_input("Starter name (exact)")
        with coln2:
            bench_by_name = st.text_input("Bench name (exact)")

        go_swap = st.form_submit_button("Execute Swap", type="primary")

    # --- Use make_substitution_example()
    if go_swap:
        try:
            # guard against empty pools / placeholder
            if not starter_opts or starter_choice == "—":
                st.error("No valid starter selected.")
                st.stop()
            if not bench_opts or bench_choice == "—":
                st.error("No valid bench player selected.")
                st.stop()

            # prefer exact-name text inputs if provided; otherwise use the 1-based index from the dropdown
            starter_sel = starter_by_name.strip() if starter_by_name.strip() else _label_to_index_str(starter_choice, starter_opts)
            bench_sel   = bench_by_name.strip()   if bench_by_name.strip()   else _label_to_index_str(bench_choice,   bench_opts)

            # call the BYOC-auth aware substitutions_v2 equivalent
            res = make_substitution_example(
                league_id=league_id,
                team_id=team_id,
                starter_select=starter_sel,   # "2" or "Oscar Bobb"
                bench_select=bench_sel,       # "1" or "Eberechi Eze"
                verify_retries=4,
                verify_sleep_s=0.8,
                session=session,              # use the already-built BYOC session
            )

            # DEBUG: peek at confirm only (no execute) to surface messages in logs
            try:
                # Get current period for debug probe
                period_id = api._current_roster_limit_period()

                logger.info(f"Current period: {period_id}")
                
                confirm_payload = {
                    "rosterLimitPeriod": period_id,
                    "fantasyTeamId": team_id,
                    "daily": False,
                    "adminMode": False,
                    "confirm": True,
                    "applyToFuturePeriods": False,
                    "fieldMap": {
                        # just re-send current state; we only want the messages
                        **{r.player.id: {"posId": r.pos_id, "stId": ("1" if r.pos_id != "0" else "2")}
                        for r in roster.rows if getattr(r, "player", None)}
                    }
                }
                dbg = api._request("confirmOrExecuteTeamRosterChanges", **confirm_payload)
                fr = (dbg or {}).get("fantasyResponse", {}) or {}
                logger.info("fantasyResponse (confirm-only) mainMsg=%s illegal=%s",
                            fr.get("mainMsg"), fr.get("illegalRosterMsgs"))
            except Exception as _e:
                logger.warning("Confirm-only probe failed: %s", _e)


            # update UI
            if res["ok"]:
                if res.get("verified"):
                    st.success("Substitution completed and verified.")
                else:
                    st.info("Substitution submitted (optimistic). Lineup view may take a few seconds to reflect.")
                # refresh the roster view
                new_roster = _refresh_roster(api, team_id)
                st.markdown("### Updated Lineup")
                _render_roster_tables(new_roster, starters_only=False)
                st.rerun()
            else:
                st.error("Substitution failed (swap_players returned False).")

        except ValueError as ve:
            # validation errors from name/index resolution
            st.error(str(ve))
        except Exception as e:
            logger.exception("Error during substitution")
            st.error(f"Error making substitution: {e}")
            st.info("Make sure both players are eligible for the swap and not locked.")
    # --- Roster analysis (same spirit as substitutions_v2) ---
    st.markdown("### Roster Analysis")
    if st.button("Compute Position Breakdown & Top-5 Starters by FPPG"):
        try:
            ro = api.roster_info(team_id)
            positions: Dict[str, Dict[str, int]] = {}
            for row in ro.rows:
                if row.player:
                    pos = row.pos.short_name
                    if pos not in positions:
                        positions[pos] = {"starters": 0, "bench": 0}
                    if row.pos_id == "0":
                        positions[pos]["bench"] += 1
                    else:
                        positions[pos]["starters"] += 1
            if positions:
                st.write(pd.DataFrame.from_dict(positions, orient="index"))

            starters_now = ro.get_starters()
            starters_with = [r for r in starters_now if r.fppg is not None]
            starters_with.sort(key=lambda x: x.fppg, reverse=True)
            if starters_with:
                st.write("**Top 5 starters by FPPG:**")
                top = [{
                    "Name": r.player.name,
                    "Team": r.player.team_short_name or r.player.team_name,
                    "FPPG": round(r.fppg, 2)
                } for r in starters_with[:5]]
                st.table(top)
        except Exception as e:
            st.error(f"Analysis failed: {e}")

    # --- Drop player (kept) ---
    st.divider()
    st.subheader("Manage Roster — Drop a Player")
    try:
        service = DropService(session)
        label_to_meta: Dict[str, Dict] = {}
        for row in roster.rows:
            if not row.player or not row.player.id:
                continue
            pid = row.player.id
            team_abbr = row.player.team_short_name or row.player.team_name or ""
            st_info = service._infer_drop_status_from_row(row, league_id)
            suffix = "" if st_info["can_drop_now"] else " — LOCKED"
            label = f"{row.player.name} ({team_abbr}){suffix}"
            if label in label_to_meta:
                label = f"{label} [{pid}]"
            label_to_meta[label] = {"pid": pid, "locked": st_info["locked"]}

        if not label_to_meta:
            st.info("No players found on this roster.")
        else:
            with st.form("drop_form"):
                choice = st.selectbox("Select a player to drop", options=list(label_to_meta.keys()))
                skip_validation = st.checkbox("Skip validation checks", value=True)
                submit_drop = st.form_submit_button("Drop Player", type="primary")

            if submit_drop:
                try:
                    meta = label_to_meta[choice]
                    logger.info(f"Drop attempt initiated for {choice}")
                    # (Optional: pre-drop logging can be added here)
                    ok = service.drop_player_single(
                        league_id=league_id,
                        team_id=team_id,
                        scorer_id=meta["pid"],
                        skip_validation=skip_validation,
                    )
                    if ok:
                        st.success("Drop submitted.")
                        st.rerun()
                    else:
                        st.error("Drop failed (no confirmation).")
                except Exception as e:
                    logger.exception("Drop failed")
                    st.error(f"Drop failed: {e}")

    except Exception as e:
        logger.exception("Drop UI error")
        st.error(f"Could not load drop UI: {e}")


def main():
    st.title("Fantrax (BYOC) — Simple Substitutions GUI")
    ui_login_section()
    st.divider()
    ui_simple_subs_section()


if __name__ == "__main__":
    main()
