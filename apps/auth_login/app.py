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
from importlib import reload
import fantraxapi
import fantraxapi.fantrax as fx

reload(fantraxapi)      # reload package
reload(fx)              # reload submodule that actually defines the class

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.objs import Roster
from fantraxapi.subs import SubsService

logger = logging.getLogger(__name__)
logger.info("fantraxapi.__file__=%s", getattr(fantraxapi, "__file__", "?"))
logger.info("fantraxapi.fantrax.__file__=%s", getattr(fx, "__file__", "?"))
logger.info("FantraxAPI.__module__=%s", FantraxAPI.__module__)
import importlib
import utils.log_helpers
importlib.reload(utils.log_helpers)
from utils.log_helpers import summarize_diff, fmap_digest, fmap_counts, fmap_delta

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


# ---- logging bootstrap (rotating + dedicated API logger) ----
try:
    from utils.auth_helpers import configure_logging  # type: ignore
except Exception:
    from logging.handlers import RotatingFileHandler
    def configure_logging(default_path: str = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log",
                          *, api_log_path: str = "/Users/hogan/FantraxAPI/data/logs/auth_api.log",
                          max_bytes: int = 2_000_000, backup_count: int = 5) -> None:
        Path(default_path).parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        # rotating root file
        if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == str(Path(default_path)) for h in root.handlers):
            fh = RotatingFileHandler(default_path, maxBytes=max_bytes, backupCount=backup_count)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        # console once
        if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
            ch = logging.StreamHandler(); ch.setFormatter(fmt); root.addHandler(ch)
        root.setLevel(logging.INFO)
        # dedicated API logger
        api_logger = logging.getLogger("auth_api")
        api_logger.propagate = False
        if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == str(Path(api_log_path)) for h in api_logger.handlers):
            ah = RotatingFileHandler(api_log_path, maxBytes=max_bytes, backupCount=backup_count)
            ah.setFormatter(fmt)
            api_logger.addHandler(ah)
        api_logger.setLevel(logging.DEBUG)

st.set_page_config(page_title="Fantrax (BYOC) — Simple Subs", page_icon="🔁", layout="wide")

LOG_PATH = "/Users/hogan/FantraxAPI/data/logs/auth_workflow.log"
configure_logging(LOG_PATH)
logger = logging.getLogger(__name__)
logger.info("=" * 100)
logger.info("Streamlit app started (BYOC SIMPLE SUBS mode)")

# Keep third-party logs at INFO unless debugging
logging.getLogger("fantraxapi").setLevel(logging.INFO)
logging.getLogger(__name__).setLevel(logging.INFO)


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

def _summarize_field_map(for_team: str, fmap: dict, *, highlight_ids: set[str] | None = None) -> dict:
    """
    Produce a tiny dict that shows only critical fieldMap rows:
    - out/in ids (if provided via highlight_ids)
    - counts by stId and posId buckets to spot formation mistakes
    """
    highlight_ids = highlight_ids or set()
    snips = {}
    starter_counts = {701:0, 702:0, 703:0, 704:0}
    bench_count = 0
    for pid, meta in fmap.items():
        pos_id = int(meta.get("posId", -1))
        st_id  = str(meta.get("stId", "2"))
        if st_id == "1" and pos_id in starter_counts:
            starter_counts[pos_id] += 1
        if st_id == "2":
            bench_count += 1
        if pid in highlight_ids:
            snips[pid] = {"posId": pos_id, "stId": st_id}
    return {
        "teamId": for_team,
        "starters_by_posId": starter_counts,  # 704=G,703=D,702=M,701=F
        "bench_count": bench_count,
        "focus_rows": snips
    }

def _probe_eligible_period(api: FantraxAPI, *, team_id: str, fmap: dict, start_period: int, window: int = 6) -> int | None:
    """
    Probe confirm with explicit rosterLimitPeriod over a small window to find the first
    period where changeAllowed=True and playerPickDeadlinePassed=False. If all probed
    periods are past deadline, return the earliest candidate+1 to schedule into.
    """
    best_schedule: int | None = None
    for off in range(0, max(1, window)):
        p = max(1, int(start_period)) + off
        try:
            pre = api._request(
                "confirmOrExecuteTeamRosterChanges",
                rosterLimitPeriod=int(p),
                fantasyTeamId=team_id,
                daily=False,
                adminMode=False,
                confirm=True,
                applyToFuturePeriods=False,
                fieldMap=fmap,
            )
            model = (pre.get("textArray") or {}).get("model") or pre.get("model") or {}
            change_allowed = bool(model.get("changeAllowed", True))
            deadline_passed = bool(model.get("playerPickDeadlinePassed"))
            if change_allowed and not deadline_passed:
                return p
            if deadline_passed and best_schedule is None:
                best_schedule = p + 1
        except Exception:
            continue
    return best_schedule

def _log_fxpa_outcome(label: str, outcome: dict) -> None:
    """
    Compact, high-signal logging for Fantrax confirm/execute responses.
    """
    fr = outcome.get("fantasyResponse") or {}
    model = outcome.get("model") or {}
    illegal = outcome.get("illegalMsgs") or []
    log_parts = {
        "label": label,
        "ok": bool(outcome.get("ok")),
        "msgType": fr.get("msgType"),
        "mainMsg": fr.get("mainMsg"),
        "illegalCount": len(illegal),
        "illegalMsgs": illegal[:3],  # truncate spam
        "changeAllowed": model.get("changeAllowed"),
        "rosterLimitPeriod": model.get("rosterLimitPeriod"),
        "firstIllegalRosterPeriod": model.get("firstIllegalRosterPeriod"),
        "playerPickDeadlinePassed": model.get("playerPickDeadlinePassed"),
    }
    logger.info("[fxpa] %s", log_parts)

def make_substitution_example(
    league_id: str,
    team_id: Optional[str] = None,
    *,
    starter_select: Optional[Union[int, str]] = None,
    bench_select: Optional[Union[int, str]] = None,
    verify_retries: int = 4,
    verify_sleep_s: float = 0.8,
    session=None,
) -> Dict[str, Any]:
    """
    BYOC-auth aware swap:
      - Preflight CONFIRM binds a *real* period and sets applyToFuturePeriods=True
      - Execute FINALIZE with browser-like client hints (handled in SubsService)
      - Surfaces pageError if the platform rejects the request
    """
    import time
    from typing import Any, Dict, Optional, Union, Set

    if session is None:
        raise RuntimeError("make_substitution_example requires an authenticated requests.Session")

    api = FantraxAPI(league_id, session=session)
    subs = SubsService(session, league_id)

    def _as_int(x, default=None):
        try:
            return int(str(x))
        except Exception:
            return default

    def _tri(val) -> Optional[bool]:
        return (val if isinstance(val, bool) else None)

    # ---- pick team + resolve rows
    my_team = api.team(team_id) if team_id else api.teams[0]
    roster = api.roster_info(my_team.team_id)
    starters = roster.get_starters()
    bench = roster.get_bench_players()

    def _resolve_row(select, pool, *, bench_expected: bool):
        if select is None:
            return None
        if isinstance(select, int) or (isinstance(select, str) and select.isdigit()):
            idx = int(select); assert 1 <= idx <= len(pool), f"Invalid selection number: {idx}"
            return pool[idx - 1]
        if isinstance(select, str):
            cand = roster.get_player_by_name(select.strip())
            if not cand:
                raise ValueError(f"Player '{select}' not found on roster.")
            is_bench = cand.pos_id == "0"
            if bench_expected and not is_bench:
                raise ValueError(f"Player '{select}' is not on the bench.")
            if (not bench_expected) and is_bench:
                raise ValueError(f"Player '{select}' is not a starter.")
            return cand
        return None

    starter_row = _resolve_row(starter_select, starters, bench_expected=False)
    bench_row   = _resolve_row(bench_select,   bench,    bench_expected=True)
    if not starter_row or not bench_row:
        raise ValueError("Both a valid starter and a valid bench player must be provided.")

    out_id, in_id = starter_row.player.id, bench_row.player.id

    # ---- current/desired + fieldMap
    current = api.roster_info(my_team.team_id)
    curr_starters = {r.player.id for r in current.get_starters() if getattr(r, "player", None)}
    desired = set(curr_starters); desired.discard(out_id); desired.add(in_id)

    fmap_raw = SubsService(session, league_id).build_field_map(current, list(desired))
    fmap = {pid: {"posId": str(v.get("posId", "0")), "stId": str(v.get("stId", "2"))} for pid, v in fmap_raw.items()}

    logger.info("[swap] submit fmap: size=%d starters=%d bench=%d",
                len(fmap),
                sum(1 for v in fmap.values() if v.get("stId") == "1"),
                sum(1 for v in fmap.values() if v.get("stId") == "2"))

    # ---- find current period (soft)
    # ---- find current period (robust)
    try:
        # use your new resolver in fantrax.py (it does A/B/C probing)
        fxpa_current = api.resolve_active_period(my_team.team_id)
    except Exception:
        fxpa_current = 1

    # ---- Preflight CONFIRM (bind a real period; future-apply=True)
    pre = subs.confirm_or_execute_lineup(
        league_id=league_id,
        fantasy_team_id=my_team.team_id,
        roster_limit_period=int(fxpa_current),
        field_map=fmap,
        apply_to_future=True,
        do_finalize=False,
    )

    pre_fr = pre.get("fantasyResponse") or {}
    pre_model = pre.get("model") or {}

    # NEW: if the server shows a confirm dialog, ACK it with a second CONFIRM
    if pre_fr.get("showConfirmWindow") or pre_fr.get("msgType") == "WARNING":
        pre = subs.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=my_team.team_id,
            roster_limit_period=int(fxpa_current or 1),
            field_map=fmap,
            apply_to_future=True,     # mirrors clicking "OK" in the UI
            do_finalize=False,        # still CONFIRM
        )
        pre_fr  = pre.get("fantasyResponse") or {}
        pre_model = pre.get("model") or {}

    pre_deadline = _tri(pre_model.get("playerPickDeadlinePassed"))
    pre_change_ok = pre_model.get("changeAllowed")
    pre_first_illegal = _as_int(pre_model.get("firstIllegalRosterPeriod"))

    logger.info("[swap][pre][response] %s", {
        "msgType": pre_fr.get("msgType"),
        "mainMsg": pre_fr.get("mainMsg"),
        "changeAllowed": pre_change_ok,
        "playerPickDeadlinePassed": pre_deadline,
        "firstIllegalRosterPeriod": pre_first_illegal,
    })

    # ---- choose execute period
    # ---- choose execute period (don’t auto-schedule on WARNING alone)
    change_ok     = pre_model.get("changeAllowed")
    deadline_passed = bool(pre_model.get("playerPickDeadlinePassed"))
    first_illegal = pre_model.get("firstIllegalRosterPeriod")
    try:
        first_illegal = int(first_illegal) if first_illegal is not None else None
    except Exception:
        first_illegal = None

    if deadline_passed or change_ok is False:
        # must schedule
        seed = (fxpa_current or 1) + 1
        probed = subs._probe_eligible_period(
            league_id=league_id, team_id=my_team.team_id, fmap=fmap,
            start_period=(first_illegal or seed), window=6
        )
        fin_period = int(probed or (first_illegal or seed))
        apply_to_future = True
    else:
        # same period is fine — prefer the model echo if present
        rai = (pre_model.get("rosterAdjustmentInfo") or {})
        fin_period = int(rai.get("rosterLimitPeriod") or fxpa_current or 1)
        apply_to_future = False

    logger.info("[swap] using period=%s (for FIN), apply_to_future=%s", fin_period, apply_to_future)

    # ---- FINALIZE
    fin = subs.confirm_or_execute_lineup(
        league_id=league_id,
        fantasy_team_id=my_team.team_id,
        roster_limit_period=fin_period,
        field_map=fmap,
        apply_to_future=apply_to_future,
        do_finalize=True,
    )

    fin_fr = fin.get("fantasyResponse") or {}
    fin_page_error = fin.get("pageError") or {}
    fin_ok = bool(fin.get("ok"))

    logger.info("[swap][fin][response] %s", {
        "ok": fin_ok,
        "msgType": fin_fr.get("msgType"),
        "mainMsg": fin_fr.get("mainMsg") or fin.get("mainMsg"),
        "illegalMsgs_len": len(fin_fr.get("illegalRosterMsgs") or []),
        "targetPeriod": fin.get("targetPeriod"),
    })

    # Early exit on server error
    if fin_page_error.get("code"):
        reason = f"server_error[{fin_page_error.get('code')}]: {(fin_page_error.get('text') or '')[:180]}"
        return {"ok": False, "verified": False, "reason": reason, "out_id": out_id, "in_id": in_id, "team_id": my_team.team_id}

    # Optional verify
    verified = False
    if fin_ok:
        for _ in range(max(0, verify_retries)):
            time.sleep(max(0.0, verify_sleep_s))
            after = api.roster_info(my_team.team_id)
            starter_ids = {r.player.id for r in after.get_starters() if getattr(r, "player", None)}
            verified = (in_id in starter_ids) and (out_id not in starter_ids)
            if verified:
                break

    reason = None
    if not fin_ok:
        tper = fin.get("targetPeriod")
        msg = (fin_fr.get("mainMsg") or fin.get("mainMsg") or "execute_not_ok")
        reason = (f"{msg} (scheduled for period {tper})" if tper not in (None, 0) else msg)
    elif fin_ok and not verified:
        reason = "optimistic (server accepted swap but roster view not yet updated)"

    logger.info("[swap] result ok=%s verified=%s reason=%s", fin_ok, verified, reason or None)

    return {
        "ok": fin_ok,
        "verified": bool(verified),
        "reason": reason,
        "out_id": out_id,
        "in_id": in_id,
        "team_id": my_team.team_id,
    }

def _handle_swap_result(res: Dict[str, Any]):
    if res.get("ok"):
        if res.get("verified"):
            st.success("Substitution completed and verified.")
        else:
            st.info("Substitution submitted (optimistic). Lineup view may take a few seconds to reflect.")
    else:
        msg = res.get("reason") or "Substitution failed."
        st.error(msg)


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

from urllib.parse import unquote

def _ensure_xsrf_header(session):
    # propagate cookie -> header for stricter pods
    token = None
    for c in session.cookies:
        if c.name.upper().startswith("XSRF-TOKEN"):
            token = unquote(c.value or "")
            break
    if token:
        session.headers["X-XSRF-TOKEN"] = token

def _apply_fxpa_client_hints(session: Session) -> None:
    """
    Fetch server UI version and set browser-like client hints on the session:
      - X-Fantrax-UI-Version (used as 'v' root field by subs service)
      - X-TZ (IANA timezone string, used as 'tz' root field)
    Safe to call multiple times; it will no-op if already set.
    """
    # If already set, don't re-probe
    if session.headers.get("X-Fantrax-UI-Version") and session.headers.get("X-TZ"):
        return

    payload = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}], "uiv": 3}
    headers = {
        "Accept": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        res = session.post("https://www.fantrax.com/fxpa/req", json=payload, timeout=20, headers=headers)
        try:
            j = res.json()
        except Exception:
            j = {}
    except Exception:
        j = {}

    ui_version = ((j.get("data") or {}).get("up")) or ""
    if ui_version:
        session.headers["X-Fantrax-UI-Version"] = ui_version

    # Pick a reasonable default IANA tz if not present
    session.headers.setdefault("X-TZ", "America/Los_Angeles")


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

    _ensure_xsrf_header(session)
    _apply_fxpa_client_hints(session)

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

    def probe_confirm_noop(api: FantraxAPI, league_id: str, team_id: str, session) -> dict:
        """
        CONFIRM (no-op) lineup probe that mirrors the browser:
        - binds to a *real* rosterLimitPeriod
        - uses applyToFuturePeriods=True
        - includes tz/v root fields (pulled from the session)
        """
        roster = api.roster_info(team_id)
        fmap: dict[str, dict[str, str]] = {}
        for r in roster.rows:
            if not getattr(r, "player", None):
                continue
            is_starter = (str(getattr(r, "pos_id", "0")) != "0")
            fmap[r.player.id] = {"posId": str(r.pos_id if is_starter else "0"), "stId": ("1" if is_starter else "2")}

        # Try to get current period; fall back to 1
        # Try to get current period; fall back to API resolver
        try:
            seed = api.resolve_active_period(team_id)
        except Exception:
            seed = 1


        payload = {
            "msgs": [{
                "method": "confirmOrExecuteTeamRosterChanges",
                "data": {
                    "leagueId": league_id,
                    "rosterLimitPeriod": int(seed),
                    "fantasyTeamId": team_id,
                    "teamId": team_id,
                    "daily": False,
                    "adminMode": False,
                    "applyToFuturePeriods": True,   # <-- browser semantics
                    "confirm": True,
                    "fieldMap": fmap,
                }
            }],
            "uiv": 3,
            "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster;period={seed}",
            "dt": 0, "at": 0, "av": "0.0",
        }

        # Add client hints (tz/v) from the session, like the SPA does
        tz = session.headers.get("X-TZ")
        ui_ver = session.headers.get("X-Fantrax-UI-Version")
        if tz:
            payload["tz"] = tz
        if ui_ver:
            payload["v"] = ui_ver

        try:
            res = session.post(
                "https://www.fantrax.com/fxpa/req",
                params={"leagueId": league_id},
                json=payload,
                timeout=25,
                headers={"Accept": "application/json; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"},
            )
            try:
                j = res.json()
            except Exception:
                j = {"http_status": res.status_code, "text": (res.text or "")[:1000]}
        except Exception as e:
            j = {"http_error": str(e)}

        fr = (j or {}).get("fantasyResponse") or {}
        model = (j or {}).get("model") or {}
        logger.info("[probe][confirm] type=%s main=%s illegal=%s changeAllowed=%s deadline=%s periodEcho=%s",
                    fr.get("msgType"),
                    fr.get("mainMsg"),
                    len(fr.get("illegalRosterMsgs") or []),
                    (model.get("changeAllowed") if isinstance(model, dict) else None),
                    (model.get("playerPickDeadlinePassed") if isinstance(model, dict) else None),
                    ((((model or {}).get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")) if isinstance(model, dict) else None))
        return j

    probe_confirm_noop(api, league_id, team_id, session)
                
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

            # Resolve the selected rows, honoring exact-name overrides if provided
            def _resolve_row_by_name_or_choice(name_txt: str, choice_label: str, opts_map: dict, expect_bench: bool):
                if name_txt.strip():
                    cand = roster.get_player_by_name(name_txt.strip())
                    if not cand:
                        raise ValueError(f"Player '{name_txt}' not found on roster.")
                    is_bench = str(getattr(cand, "pos_id", "0")) == "0"
                    if expect_bench and not is_bench:
                        raise ValueError(f"Player '{name_txt}' is not on the bench.")
                    if (not expect_bench) and is_bench:
                        raise ValueError(f"Player '{name_txt}' is not a starter.")
                    return cand
                # fallback to dropdown
                return opts_map[choice_label]

            starter_row = _resolve_row_by_name_or_choice(
                starter_by_name, starter_choice, starter_opts, expect_bench=False
            )
            bench_row = _resolve_row_by_name_or_choice(
                bench_by_name, bench_choice, bench_opts, expect_bench=True
            )

            # Execute using the robust SubsService (handles WARNING/locked → schedules/probes)
            subs = SubsService(session, league_id)
            ok = subs.swap_players(
                team_id=team_id,
                out_player_id=starter_row.player.id,  # move starter out
                in_player_id=bench_row.player.id      # bring bench in
            )

            # Update UI (mirror your existing result handling)
            res = {"ok": bool(ok), "verified": None, "reason": None}
            if res["ok"]:
                st.info("Substitution submitted. Verifying roster view…")
                new_roster = _refresh_roster(api, team_id)
                st.markdown("### Updated Lineup")
                _render_roster_tables(new_roster, starters_only=False)
                st.rerun()
            else:
                _handle_swap_result(res)

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

    # --- League FAAB & Claims ---
    st.divider()
    st.subheader("League FAAB & Claims")
    if st.button("Load FAAB & Claims for selected league"):
        from datetime import datetime as _dt
        try:
            with st.spinner("Loading FAAB budgets and claims…"):
                budgets = api.league.faab_budgets()

                # Collect per-team info
                summary_rows = []
                team_claims: Dict[str, dict] = {}
                for t_id, budget in budgets.items():
                    try:
                        team = api.team(t_id)
                    except Exception:
                        # Fallback minimal team object
                        class _T:
                            name = f"Team {t_id}"
                        team = _T()

                    try:
                        claim_info = api.league.get_claim_info(t_id) or {}
                    except Exception as _e:
                        logger.warning("get_claim_info failed for %s: %s", t_id, _e)
                        claim_info = {}
                    team_claims[t_id] = claim_info

                    pending = (claim_info.get("pendingClaims") or [])
                    next_process = pending[0].get("process_date") if pending else ""
                    summary_rows.append({
                        "Team Name": getattr(team, "name", str(t_id))[:30],
                        "FAAB": budget.get("display"),
                        "FAAB_value": budget.get("value", 0),
                        "Tradeable": str(budget.get("tradeable")),
                        "Claims": len(pending),
                        "Next Process": next_process or "",
                    })

                # Summary table (sorted by FAAB value desc)
                if summary_rows:
                    df = pd.DataFrame(summary_rows).sort_values("FAAB_value", ascending=False)
                    st.caption(f"As of {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    st.table(df[["Team Name", "FAAB", "Tradeable", "Claims", "Next Process"]])
                else:
                    st.info("No FAAB data available.")

                # League-wide settings (from first team's claim info)
                first_claims = next(iter(team_claims.values()), {})
                if first_claims:
                    st.markdown("**League Claim Settings**")
                    claim_types = (first_claims.get("claimTypes") or {})
                    if claim_types:
                        st.write({"Claim Types": list(claim_types.values())})
                    st.write({
                        "Claim Groups Enabled": first_claims.get("claimGroupsEnabled"),
                        "FAAB Bidding Enabled": first_claims.get("showBidColumn"),
                    })
                    misc = first_claims.get("miscData") or {}
                    if misc:
                        st.write({k: misc[k] for k in ("allowGroupChanges", "showAllTeamsChoice") if k in misc})

                # Pending claims details per team
                for t_id, claim_info in team_claims.items():
                    pending = (claim_info.get("pendingClaims") or [])
                    if not pending:
                        continue
                    try:
                        t = api.team(t_id)
                        team_name = getattr(t, "name", str(t_id))
                    except Exception:
                        team_name = str(t_id)
                    with st.expander(f"Pending Claims — {team_name}"):
                        rows = []
                        for c in pending:
                            parts = []
                            if c.get("process_date"):
                                parts.append(f"Process: {c['process_date']}")
                            cp = c.get("claim_player") or {}
                            if cp:
                                parts.append(
                                    f"Add: {cp.get('name')} ({cp.get('position')}, {cp.get('team')}) -> "
                                    f"{cp.get('to_position')}/{cp.get('to_status')}"
                                )
                            dp = c.get("drop_player") or {}
                            if dp:
                                parts.append(
                                    f"Drop: {dp.get('name')} ({dp.get('position')}, {dp.get('team')}) from "
                                    f"{dp.get('from_position')}/{dp.get('from_status')}"
                                )
                            if c.get("bid_amount") is not None:
                                try:
                                    parts.append(f"Bid: ${float(c['bid_amount']):.2f}")
                                except Exception:
                                    parts.append(f"Bid: {c['bid_amount']}")
                            if c.get("priority") is not None:
                                parts.append(f"Priority: {c['priority']}")
                            if c.get("group"):
                                parts.append(f"Group: {c['group']}")
                            if c.get("submitted_date"):
                                parts.append(f"Submitted: {c['submitted_date']}")
                            rows.append(" | ".join(parts))
                        if rows:
                            for r in rows:
                                st.write(r)
                        else:
                            st.write("No details available.")
        except Exception as e:
            logger.exception("FAAB & Claims section failed")
            st.error(f"Failed to load FAAB & Claims: {e}")


def main():
    st.title("Fantrax (BYOC) — Simple Substitutions GUI")
    ui_login_section()
    st.divider()
    ui_simple_subs_section()


if __name__ == "__main__":
    main()
