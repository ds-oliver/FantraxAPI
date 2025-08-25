"""
Streamlit: Bring your own Fantrax cookie/artifacts, list leagues, browse rosters.
"""

# auth_login/app.py

from __future__ import annotations

import io
import logging
import pickle
from pathlib import Path
from typing import Optional

import streamlit as st
from requests import Session

from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster

# --- auth + cookie helpers ---
from utils.cookie_import import read_auth_file  # NEW: accepts pickle/JSON and returns {"cookies","storage"}
from utils.auth_helpers import (
    FantraxAuth,
    fetch_user_leagues,
    validate_logged_in,
)

# --- roster ops ---
from utils.roster_ops import DropService

# Prefer the token-aware builder; fall back to cookies-only builder if absent.
try:
    from utils.auth_helpers import load_requests_session_from_artifacts  # type: ignore
except Exception:
    from utils.auth_helpers import load_requests_session_from_cookie_list as load_requests_session_from_artifacts  # type: ignore

# Optional user card
try:
    from utils.auth_helpers import fetch_user_profile  # type: ignore
except Exception:
    def fetch_user_profile(session: Session):
        return {}

# ---- logging bootstrap (kept from your original) ----
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


st.set_page_config(page_title="Fantrax (BYOC) — Leagues & Rosters", page_icon="🔐", layout="wide")

LOG_PATH = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log"
configure_logging(LOG_PATH)
logger = logging.getLogger(__name__)
logger.info("Streamlit app started (BYOC mode)")


def get_roster_for_league(league_id: str, team_id: str, session: Session) -> Roster:
    api = FantraxAPI(league_id=league_id, session=session)
    return api.roster_info(team_id)


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
            st.session_state.pop("auth_artifacts", None)
            st.session_state.pop("artifacts_pickle_bytes", None)
            st.session_state.pop("cookies_pickle_bytes", None)
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

    # --- Tab 2: Selenium capture (your existing helper; supports non-headless) ---
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

    # --- Tab 3: NEW headless background login using credentials ---
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

    # Quick debug pane for storage presence (no values logged)
    with st.expander("Auth debug", expanded=False):
        art = st.session_state.get("auth_artifacts") or {}
        loc = (art.get("storage") or {}).get("local", {}) or {}
        ses = (art.get("storage") or {}).get("session", {}) or {}
        st.caption(f"localStorage keys: {len(loc)}; sessionStorage keys: {len(ses)}")


def ui_leagues_and_rosters_section():
    st.header("Your Leagues and Rosters")

    if "auth_artifacts" not in st.session_state:
        st.info("Upload cookie or capture via Selenium above.")
        st.stop()

    logger.info("Hydrating requests session from artifacts (cookies + storage if present)")
    try:
        session = load_requests_session_from_artifacts(st.session_state["auth_artifacts"])

        service = DropService(session)
    except Exception:
        logger.exception("Failed to build session from artifacts")
        st.error("Could not create a session from your cookie/artifacts.")
        st.stop()

    # Optional: quick cookie/header sanity check
    with st.expander("Cookie debug", expanded=False):
        try:
            import requests as _rq
            req = _rq.Request(
                "POST",
                "https://www.fantrax.com/fxpa/req",
                data="{}",
            )
            prepped = session.prepare_request(req)
            st.write({
                "CookieHeaderLen": len(prepped.headers.get("Cookie", "")),
                "Content-Type": prepped.headers.get("Content-Type"),
                "HasAuthHeader": bool(prepped.headers.get("Authorization")),
                "HasXSRFHeader": bool(prepped.headers.get("X-XSRF-TOKEN")),
            })
            sent = [
                {"name": c.name, "domain": c.domain, "path": c.path}
                for c in session.cookies
                if "fantrax" in (c.domain or "")
            ][:50]
            st.write(sent)
        except Exception as _e:
            st.write(f"prep failed: {_e}")

    # Prefer fxpa-based validation (soft); still attempt league listing if it fails
    is_valid = validate_logged_in(session)
    if not is_valid:
        st.warning("Your cookie may be expired or missing tokens. We'll still try to list leagues from cookies.")
        with st.expander("fxpa probe (debug)", expanded=False):
            try:
                probe = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}]}
                j = session.post("https://www.fantrax.com/fxpa/req", json=probe, timeout=20).json()
                import json as _json
                st.code(_json.dumps(j, indent=2)[:1500])
            except Exception:
                st.caption("Probe failed.")

    # ---- Normal path: list leagues via fxpa, then show rosters ----
    logger.info("Fetching user leagues via fxpa getAllLeagues")
    leagues = fetch_user_leagues(session)
    if not leagues:
        st.error("No leagues found (cookie may be expired).")
        # Manual fallback: ask for leagueId then proceed with cookies-only auth
        st.subheader("Manual league selection (fallback)")
        league_id_input = st.text_input("Enter a Fantrax league ID")
        if not league_id_input:
            st.info("You can find the league ID in the Fantrax league URL.")
            st.stop()

        try:
            api = FantraxAPI(league_id_input, session=session)
            teams = api.teams
        except Exception as e:
            logger.exception("Failed to load teams for manual league ID")
            st.error(f"Failed to load teams for league {league_id_input}: {e}")
            st.stop()

        team_options = {f"{t.name} ({t.team_id})": t.team_id for t in teams}
        team_label = st.selectbox("Select your team", options=list(team_options.keys()))
        team_id = team_options[team_label]

        try:
            roster = api.roster_info(team_id)
        except Exception as e:
            logger.exception("Failed to fetch roster for manual league path")
            st.error(f"Failed to fetch roster: {e}")
            st.stop()

        st.subheader(f"Roster — {team_label}")
        starters = roster.get_starters()
        bench = roster.get_bench_players()
        if starters:
            st.markdown("**Starters**")
            st.table(_make_table(starters))
        if bench:
            st.markdown("**Bench**")
            st.table(_make_table(bench))
        st.stop()

    # Sidebar user card
    with st.sidebar:
        info = {}
        try:
            info = fetch_user_profile(session) or {}
        except Exception:
            pass

        if info:
            st.subheader("Account")
            if info.get("logo"): st.image(info["logo"], width=64)
            st.write(info.get("username", ""))
            tz = info.get("timezone") or info.get("timezoneCode", "")
            line = " • ".join([x for x in (info.get("email", ""), tz) if x])
            if line: st.caption(line)
            if info.get("numLeagues"): st.caption(f"Leagues: {info['numLeagues']}")

        st.subheader("Leagues")
        starters_only = st.checkbox("Show starters only", value=False)
        show_all_rosters = st.checkbox("Show all rosters in league", value=False)

    # Choose a league (showing user's team)
    choices = {f"{lt['league']} — your team: {lt['team']}": lt for lt in leagues}
    label = st.selectbox("Choose a league", list(choices.keys()))
    picked = choices[label]
    league_id = picked["leagueId"]
    team_id = picked["teamId"]
    st.caption(f"Selected leagueId={league_id}, your teamId={team_id}")

    if show_all_rosters:
        try:
            logger.info(f"Fetching ALL rosters for league={league_id}")
            api = FantraxAPI(league_id=league_id, session=session)
            rosters = api.league.list_rosters()
        except Exception as e:
            logger.exception("Failed to list rosters for league")
            st.error(f"Failed to list rosters: {e}")
            return

        cols = st.columns(3)
        for idx, roster in enumerate(rosters):
            t = roster.team.name if getattr(roster, 'team', None) else f"Team {idx+1}"
            with cols[idx % 3]:
                st.caption(t)
                starters = roster.get_starters()
                bench = [] if starters_only else roster.get_bench_players()
                st.table(_make_table(starters))
                if bench:
                    st.markdown("<div style='margin-top:6px;'></div>", unsafe_allow_html=True)
                    st.table(_make_table(bench))
    else:
        try:
            logger.info(f"Fetching roster: league={league_id} team={team_id}")
            roster = get_roster_for_league(league_id, team_id, session)
        except Exception as e:
            logger.exception("Failed to fetch roster")
            st.error(f"Failed to fetch roster: {e}")
            return

        st.subheader(label)
        starters = roster.get_starters()
        bench = [] if starters_only else roster.get_bench_players()
        st.markdown("### Starters")
        st.table(_make_table(starters))
        if bench:
            st.markdown("### Bench")
            st.table(_make_table(bench))

        # app.py (inside the "else:" branch where a single roster is shown)

        # --- Drop player flow ---
        st.divider()
        st.subheader("Manage Roster — Drop a Player")

        try:
            label_to_meta = {}
            for row in roster.rows:
                if not row.player or not row.player.id:
                    continue

                pid = row.player.id
                team_abbr = row.player.team_short_name or row.player.team_name or ""

                # Use service’s status inference for labeling (does not refetch roster)
                st_info = service._infer_drop_status_from_row(row, league_id)  # or expose a wrapper if you prefer not to use "_"
                if not st_info["can_drop_now"]:
                    suffix = " — LOCKED"
                else:
                    suffix = ""

                label = f"{row.player.name} ({team_abbr}){suffix}"
                if label in label_to_meta:
                    label = f"{label} [{pid}]"
                label_to_meta[label] = {"pid": pid, "locked": st_info["locked"]}

            if not label_to_meta:
                st.info("No players found on this roster.")
            else:
                with st.form("drop_form"):
                    choice = st.selectbox("Select a player to drop", options=list(label_to_meta.keys()))
                    skip_validation = st.checkbox("Skip validation checks", value=False)
                    submit_drop = st.form_submit_button("Drop Player", type="primary")

                if submit_drop:
                    try:
                        meta = label_to_meta[choice]
                        logger.info(f"Drop attempt initiated for {choice}")
                        
                        # Log pre-drop state
                        try:
                            roster_before = service.get_roster(league_id, team_id)
                            player_row = service._find_row(roster_before, meta["pid"])
                            if player_row and player_row.player:
                                logger.info(f"Pre-drop roster state: Player {player_row.player.name} ({meta['pid']}) "
                                          f"found in position {player_row.pos.short_name if player_row.pos else 'unknown'}")
                                # Log raw data for debugging
                                raw_data = getattr(player_row, '_raw', {})
                                logger.info(f"Player row raw data: {raw_data}")
                            else:
                                logger.warning(f"Player {meta['pid']} not found in pre-drop roster check")
                        except Exception as e:
                            logger.warning(f"Failed to log pre-drop state: {e}")

                        # Attempt the drop
                        logger.info(f"Executing drop with skip_validation={skip_validation}")
                        ok = service.drop_player_single(
                            league_id=league_id,
                            team_id=team_id,
                            scorer_id=meta["pid"],
                            skip_validation=skip_validation,
                        )
                        
                        # Log post-drop state
                        logger.info(f"Drop API result: {ok}")
                        if ok:
                            try:
                                roster_after = service.get_roster(league_id, team_id)
                                player_still_present = service._find_row(roster_after, meta["pid"]) is not None
                                logger.info(f"Post-drop roster check: player still present = {player_still_present}")
                                
                                if meta["locked"]:
                                    logger.info("Drop successful but player locked - will take effect next gameweek")
                                    st.success("Drop submitted and will be effective next gameweek.")
                                else:
                                    if player_still_present:
                                        logger.warning("Drop reported success but player still on roster")
                                        st.warning("Drop submitted but player still appears on roster. This may take a few minutes to update.")
                                    else:
                                        logger.info("Drop successful and player removed from roster")
                                        st.success("Drop submitted. Your roster will update shortly.")
                            except Exception as e:
                                logger.warning(f"Failed to verify post-drop state: {e}")
                                st.success("Drop submitted but could not verify roster update.")
                            st.rerun()
                        else:
                            logger.error("Drop failed - no confirmation from API")
                            st.error("Drop failed (no confirmation).")
                    except Exception as e:
                        logger.exception(f"Drop failed with exception: {str(e)}")
                        st.error(f"Drop failed: {e}")

        except Exception as e:
            logger.exception("Drop UI error")
            st.error(f"Could not load drop UI: {e}")



def main():
    st.title("Fantrax Auth (BYOC) — Testbed")
    ui_login_section()
    st.divider()
    ui_leagues_and_rosters_section()


if __name__ == "__main__":
    main()
