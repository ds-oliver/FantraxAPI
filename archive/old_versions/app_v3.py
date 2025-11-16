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
    import time
    from typing import Any, Dict, Optional, Union, Set

    if session is None:
        raise RuntimeError("make_substitution_example requires an authenticated requests.Session")

    api = FantraxAPI(league_id, session=session)
    subs = SubsService(session, league_id)

    def _tri(val) -> Optional[bool]:
        # keep None if missing; only coerce to True/False if explicitly provided
        return (val if isinstance(val, bool) else None)

    def _as_int(x, default=None):
        try:
            return int(str(x))
        except Exception:
            return default

    def _extract_model(blob: dict) -> tuple[dict, str]:
        """
        Return (model, source_tag) where model is the dict we parsed and
        source_tag tells us which branch we used for logging.
        """
        if blob.get("model"):
            return blob["model"], "top.model"
        ta = blob.get("textArray")
        if isinstance(ta, dict) and isinstance(ta.get("model"), dict):
            return ta["model"], "textArray.model"
        fr = blob.get("fantasyResponse")
        if isinstance(fr, dict) and isinstance(fr.get("model"), dict):
            return fr["model"], "fantasyResponse.model"
        return {}, "missing"

    def _summarize_field_map(for_team: str, fmap: dict, *, highlight_ids: set[str] | None = None) -> dict:
        highlight_ids = highlight_ids or set()
        snips = {}
        starter_counts = {701:0, 702:0, 703:0, 704:0}
        bench_count = 0
        for pid, meta in fmap.items():
            pos_id = _as_int(meta.get("posId"), -1)
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

    def _fmap_from_roster(roster) -> dict:
        # Build the server’s current map shape (strings) so we can diff
        fm = {}
        for r in roster.rows:
            if not getattr(r, "player", None):
                continue
            pos_id = str(getattr(r, "pos_id", "0") or "0")
            fm[r.player.id] = {"posId": pos_id, "stId": ("1" if pos_id != "0" else "2")}
        return fm

    def _fmap_delta(a: dict, b: dict, focus: set[str] | None = None) -> dict:
        """
        Tiny diff between two fieldMaps. Only logs changed rows; can be narrowed to focus ids.
        """
        focus = focus or set()
        out = {}
        for pid in set(a) | set(b):
            if focus and pid not in focus:
                continue
            av, bv = a.get(pid), b.get(pid)
            if av != bv:
                out[pid] = {"from": av, "to": bv}
        return out

    logger.info("[debug] has resolve_active_period? %s", hasattr(api, "resolve_active_period"))
    logger.info("[debug] has _resolve_active_period? %s", hasattr(api, "_resolve_active_period"))

    # ---- pick team
    my_team = api.team(team_id) if team_id else api.teams[0]
    roster = api.roster_info(my_team.team_id)
    starters = roster.get_starters()
    bench = roster.get_bench_players()

    def _resolve_row(select, pool, *, bench_expected: bool):
        if select is None:
            return None
        if isinstance(select, int) or (isinstance(select, str) and select.isdigit()):
            idx = int(select)
            assert 1 <= idx <= len(pool), f"Invalid {'bench' if bench_expected else 'starter'} number: {idx}"
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

    # ---- current/desired
    current = api.roster_info(my_team.team_id)
    curr_starters = {r.player.id for r in current.get_starters() if getattr(r, "player", None)}

    if out_id not in curr_starters and in_id in curr_starters:
        logger.info("[swap] already satisfied")
        return {"ok": True, "verified": True, "reason": "already_satisfied", "out_id": out_id, "in_id": in_id, "team_id": my_team.team_id}

    desired = set(curr_starters)
    desired.discard(out_id)
    desired.add(in_id)

    def _summarize_diff(curr: Set[str], desired: Set[str]) -> Dict[str, list]:
        return {"to_bench": sorted(list(curr - desired)), "to_start": sorted(list(desired - curr))}

    # pretty logs
    out_pos_short = getattr(getattr(starter_row, "pos", None), "short_name", None) or "UNK"
    in_pos_short  = getattr(getattr(bench_row,   "pos", None), "short_name", None) or "UNK"
    logger.info("[swap] plan team=%s out=%s in=%s diff=%s", my_team.team_id, out_id, in_id, _summarize_diff(curr_starters, desired))
    logger.info("[swap] players: %s (%s) → bench, %s (%s) → starters", starter_row.player.name, out_pos_short, bench_row.player.name, in_pos_short)
    logger.info("[swap] eligibility out_pos=%s bench_elig=%s", subs._pos_of_row(starter_row), sorted(list(subs.eligible_positions_of_row(bench_row))))

    # --- Build fmap (normalize to strings for Fantrax)
    fmap_raw = subs.build_field_map(current, list(desired))
    fmap = {pid: {"posId": str(v.get("posId", "0")), "stId": str(v.get("stId", "2"))} for pid, v in fmap_raw.items()}
    n = len(fmap)
    s = sum(1 for v in fmap.values() if v.get("stId") == "1")
    b = n - s
    logger.info("[swap] submit fmap: size=%d starters=%d bench=%d", n, s, b)

    # Log current vs desired map for the focus pair
    current_map = _fmap_from_roster(current)
    focus = {out_id, in_id}
    delta = _fmap_delta(current_map, fmap, focus=focus)
    logger.info("[swap] fmap delta (focus=%s): %s", list(focus), delta or "<none>")

    # ----------------------------------------------------------------
    # RAW CONFIRM-ECHO (rosterLimitPeriod=0): server should *echo* the real period/flags
    # ----------------------------------------------------------------
    pre_req_snapshot = {
        "method": "confirmOrExecuteTeamRosterChanges",
        "fantasyTeamId": my_team.team_id,
        "rosterLimitPeriod": 0,
        "applyToFuturePeriods": False,
        "fieldMapDigest": _summarize_field_map(my_team.team_id, fmap, highlight_ids=focus),
    }
    logger.info("[swap][pre][request] %s", pre_req_snapshot)

    pre_raw = api._request(
        "confirmOrExecuteTeamRosterChanges",
        rosterLimitPeriod=0,                # let server pick & echo
        fantasyTeamId=my_team.team_id,
        daily=False,
        adminMode=False,
        confirm=True,
        applyToFuturePeriods=False,
        fieldMap=fmap,
    )

    pre_fr = pre_raw.get("fantasyResponse") or {}
    pre_model, model_source = _extract_model(pre_raw)
    rai       = (pre_model.get("rosterAdjustmentInfo") or {})
    period    = _as_int(rai.get("rosterLimitPeriod"), None)
    deadline  = _tri(pre_model.get("playerPickDeadlinePassed"))
    change_ok = pre_model.get("changeAllowed")

    pre_resp_snapshot = {
        "model_source": model_source,
        "echo_period": period,
        "changeAllowed": change_ok,
        "playerPickDeadlinePassed": deadline,
        "firstIllegalRosterPeriod": (pre_model.get("firstIllegalRosterPeriod")),
        "msgType": pre_fr.get("msgType"),
        "mainMsg": pre_fr.get("mainMsg"),
        "illegalRosterMsgs_len": len(pre_fr.get("illegalRosterMsgs") or []),
        "applyToFuturePeriods_echo": pre_fr.get("applyToFuturePeriods"),
    }
    logger.info("[swap][pre][response] %s", pre_resp_snapshot)

    # If we didn't get a concrete period, DON'T translate it to 0=False; keep Unknown (None)
    if period is None:
        logger.info("[swap][pre] server did not echo a concrete period (period=None).")

    # Decide scheduling flags based on preflight model
    pre_change_allowed = bool(pre_model.get("changeAllowed", True)) if isinstance(pre_model, dict) else True
    deadline_passed = bool(pre_model.get("playerPickDeadlinePassed")) if isinstance(pre_model, dict) else False
    pre_main_msg_lc = str(pre_fr.get("mainMsg") or "").lower()
    pre_says_locked = ("locked" in pre_main_msg_lc) or (not pre_change_allowed) or deadline_passed

    server_period = None
    try:
        server_period = int(((pre_model.get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")))
    except Exception:
        server_period = None

    # Resolve an explicit active period as a fallback (prefer roster sniff, then SCHEDULE currentPeriod; ignore rp==1)
    roster_period = None
    roster_deadline = None
    try:
        rp2, dl2 = subs._sniff_period_and_deadline_from_roster(current)
        roster_period, roster_deadline = int(rp2), bool(dl2)
    except Exception:
        roster_period, roster_deadline = None, None
    fxpa_current = None
    try:
        fxpa_current = int(subs._current_period_via_fxpa(league_id) or 0)
    except Exception:
        fxpa_current = None
    # Fallback: direct SCHEDULE via API with robust parsing
    if not fxpa_current:
        try:
            sched = api._request("getStandings", view="SCHEDULE")
            cand = (
                sched.get("currentPeriod")
                or (sched.get("scheduleInfo") or {}).get("currentPeriod")
                or (sched.get("fantasyResponse") or {}).get("currentPeriod")
            )
            if not cand:
                cap = str(((sched.get("tableList") or [{}])[0] or {}).get("caption") or "").strip()
                parts = cap.split()
                if parts and parts[-1].isdigit():
                    cand = int(parts[-1])
            if cand and int(cand) > 0:
                fxpa_current = int(cand)
        except Exception:
            pass
    rp = None
    try:
        cand = api.resolve_active_period(team_id=my_team.team_id, use_confirm_probe=True)
        rp = int(cand) if cand is not None else None
        if rp == 1:
            rp = None  # treat unresolved fallback (1) as unknown to avoid using past period
    except Exception:
        rp = None

    # If server says locked, explicitly target the next active period; otherwise use server-echoed/current
    first_illegal = None
    try:
        first_illegal = int((pre_model.get("firstIllegalRosterPeriod"))) if isinstance(pre_model, dict) else None
    except Exception:
        first_illegal = None

    next_period = None
    if isinstance(first_illegal, int) and first_illegal > 0:
        next_period = first_illegal
    elif fxpa_current and fxpa_current > 0:
        next_period = fxpa_current + 1

    apply_to_future = bool(pre_fr.get("applyToFuturePeriods") is True or deadline_passed or pre_says_locked or roster_deadline is True)
    if apply_to_future:
        # Prefer server's hint; else probe a small window to pick the true next active
        probed = None
        try:
            if not first_illegal:
                seed = (fxpa_current or 0) + 1 if fxpa_current else 1
                probed = _probe_eligible_period(api, team_id=my_team.team_id, fmap=fmap, start_period=seed, window=6)
        except Exception:
            probed = None
        fin_period = int(first_illegal or probed or ((fxpa_current or 0) + 1))
        chosen_basis = f"schedule_next (fxpa={fxpa_current}, firstIllegal={first_illegal}, probed={probed}, computed={fin_period})"
    else:
        # Use server-echoed period, else SCHEDULE currentPeriod, else API resolver; ignore roster_period (can be season-end)
        chosen_fallback = (
            (fxpa_current if (fxpa_current and fxpa_current > 0) else (
            rp if (rp and rp > 0) else 0))
        )
        fin_period = (server_period if server_period is not None else chosen_fallback)
        # If still 0 but fxpa_current exists, snap to fxpa_current (apply now in current period)
        if (not fin_period) and (fxpa_current and fxpa_current > 0):
            fin_period = fxpa_current
        chosen_basis = f"server={server_period} fxpa_current={fxpa_current} rp={rp}"

    logger.info("[swap] period select: %s -> chosen=%s apply_to_future=%s", chosen_basis, fin_period, apply_to_future)

    logger.info("[swap] using period=%s (for FIN), apply_to_future=%s", fin_period, apply_to_future)

    # ---- FIN (execute with the chosen period)
    fin_req_snapshot = {
        "method": "confirmOrExecuteTeamRosterChanges",
        "fantasyTeamId": my_team.team_id,
        "rosterLimitPeriod": fin_period,
        "applyToFuturePeriods": apply_to_future,
        "fieldMapDigest": _summarize_field_map(my_team.team_id, fmap, highlight_ids=focus),
    }
    logger.info("[swap][fin][request] %s", fin_req_snapshot)

    fin = subs.confirm_or_execute_lineup(
        league_id=league_id,
        fantasy_team_id=my_team.team_id,
        roster_limit_period=int(fin_period),
        field_map=fmap,
        apply_to_future=apply_to_future,
        do_finalize=True,
    )

    # If FIN reports locks and we weren't scheduling, retry as scheduled now
    fin_fr = fin.get("fantasyResponse") or {}
    fin_main_lc = str(fin_fr.get("mainMsg") or fin.get("mainMsg") or "").lower()
    if (not bool(fin.get("ok"))) and (("locked" in fin_main_lc) or ("no changes detected" in fin_main_lc)) and (not apply_to_future):
        # Retry as a scheduled change targeting the next active period explicitly
        sched_period = 0
        try:
            if isinstance(first_illegal, int) and first_illegal > 0:
                sched_period = int(first_illegal)
            else:
                seed = (fxpa_current or 0) + 1 if fxpa_current else 1
                probed = _probe_eligible_period(api, team_id=my_team.team_id, fmap=fmap, start_period=seed, window=6)
                sched_period = int(probed or seed)
        except Exception:
            sched_period = 0
        logger.info("[swap] FIN reported locks; retrying as scheduled change (period=%s, apply_to_future=True)", sched_period)
        fin_req_retry_snapshot = {
            "method": "confirmOrExecuteTeamRosterChanges",
            "fantasyTeamId": my_team.team_id,
            "rosterLimitPeriod": sched_period,
            "applyToFuturePeriods": True,
            "fieldMapDigest": _summarize_field_map(my_team.team_id, fmap, highlight_ids=focus),
        }
        logger.info("[swap][fin][request][retry] %s", fin_req_retry_snapshot)
        fin = subs.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=my_team.team_id,
            roster_limit_period=sched_period,
            field_map=fmap,
            apply_to_future=True,
            do_finalize=True,
        )

        # If still locked, advance through the next few periods to find the first eligible one
        fin_fr_retry = fin.get("fantasyResponse") or {}
        fin_main_retry_lc = str(fin_fr_retry.get("mainMsg") or fin.get("mainMsg") or "").lower()
        if (not bool(fin.get("ok"))) and ("locked" in fin_main_retry_lc):
            try:
                base = int(sched_period or ((fxpa_current or 0) + 1))
            except Exception:
                base = 1
            for extra in range(1, 6):  # try up to 5 more periods ahead
                try_p = base + extra
                logger.info("[swap] retry advancing period -> %s (apply_to_future=True)", try_p)
                logger.info("[swap][fin][request][retry] %s", {
                    "method": "confirmOrExecuteTeamRosterChanges",
                    "fantasyTeamId": my_team.team_id,
                    "rosterLimitPeriod": try_p,
                    "applyToFuturePeriods": True,
                    "fieldMapDigest": _summarize_field_map(my_team.team_id, fmap, highlight_ids=focus),
                })
                fin2 = subs.confirm_or_execute_lineup(
                    league_id=league_id,
                    fantasy_team_id=my_team.team_id,
                    roster_limit_period=try_p,
                    field_map=fmap,
                    apply_to_future=True,
                    do_finalize=True,
                )
                if bool(fin2.get("ok")):
                    fin = fin2
                    break
                msg2 = str(((fin2.get("fantasyResponse") or {}).get("mainMsg") or fin2.get("mainMsg") or "")).lower()
                if "locked" not in msg2:
                    fin = fin2
                    break

    fin_fr = fin.get("fantasyResponse") or {}
    fin_model = fin.get("model") or {}
    fin_snapshot = {
        "ok": bool(fin.get("ok")),
        "msgType": fin_fr.get("msgType"),
        "mainMsg": fin_fr.get("mainMsg") or fin.get("mainMsg"),
        "illegalMsgs_len": len(fin_fr.get("illegalRosterMsgs") or fin.get("illegalMsgs") or []),
        "targetPeriod": fin.get("targetPeriod") or ((fin_model.get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")),
    }
    logger.info("[swap][fin][response] %s", fin_snapshot)

    ok = bool(fin.get("ok"))
    verified = False
    reason = None
    after = None

    # Verify (eventual consistency)
    if ok:
        for _ in range(max(0, verify_retries)):
            time.sleep(max(0.0, verify_sleep_s))
            after = api.roster_info(my_team.team_id)
            starter_ids = {r.player.id for r in after.get_starters() if getattr(r, "player", None)}
            verified = (in_id in starter_ids) and (out_id not in starter_ids)
            if verified:
                break

    if not ok:
        reason = (
            fin_fr.get("mainMsg")
            or fin.get("mainMsg")
            or "; ".join(map(str, (fin_fr.get("illegalRosterMsgs") or fin.get("illegalMsgs") or [])))
            or "execute_not_ok"
        )
        if fin_snapshot.get("targetPeriod") not in (None, 0):
            reason = f"{reason} (scheduled for period {fin_snapshot['targetPeriod']})"
    elif ok and not verified:
        reason = "optimistic (server accepted swap but roster view not yet updated)"

    logger.info("[swap] result ok=%s verified=%s reason=%s", ok, verified, (reason or None))

    if reason and "no changes detected" in (reason or "").lower():
        if not after:
            after = api.roster_info(my_team.team_id)
        after_starters = {r.player.id for r in after.get_starters() if getattr(r, "player", None)}
        logger.info("[swap] no-op diffs: %s", _summarize_diff(after_starters, desired))

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
        Send a CONFIRM (no-op) lineup request to fetch the server's roster model echo.
        Robust to flaky getStandings and non-JSON responses.
        Returns the parsed JSON (or a small dict with http_status/text on parse error).
        """
        # --- Build a fieldMap that mirrors the CURRENT roster exactly ---
        roster = api.roster_info(team_id)
        fmap: dict[str, dict[str, str]] = {}
        for r in roster.rows:
            if not getattr(r, "player", None):
                continue
            is_starter = (str(getattr(r, "pos_id", "0")) != "0")
            fmap[r.player.id] = {
                "posId": str(r.pos_id if is_starter else "0"),
                "stId":  "1" if is_starter else "2",
            }

        # --- Resolve current period (getStandings; soft fallback on failure) ---
        try:
            from fantraxapi.exceptions import FantraxException  # type: ignore
        except Exception:
            class FantraxException(Exception):  # graceful local fallback if not present
                pass

        def _parse_current_period(resp: dict) -> int:
            try:
                return int((((resp.get("responses") or [{}])[0].get("data") or {}).get("currentPeriod") or 0))
            except Exception:
                return 0

        try:
            gs = api._request("getStandings", view="SCHEDULE")
        except FantraxException as e:
            logger.warning("getStandings failed (%s); using soft fallback for period.", e)
            try:
                # Prefer our resilient resolver; then FantraxAPI helper; default to 0
                svc = SubsService(session, league_id=league_id)
                period = svc._current_period_via_fxpa(league_id) or api.drops.get_current_period() or 0
            except Exception:
                period = 0
            gs = {"responses": [{"data": {"currentPeriod": period}, "pageError": None}]}
        except Exception as e:
            logger.warning("getStandings raised (%s); using soft fallback for period.", e)
            try:
                svc = SubsService(session, league_id=league_id)
                period = svc._current_period_via_fxpa(league_id) or api.drops.get_current_period() or 0
            except Exception:
                period = 0
            gs = {"responses": [{"data": {"currentPeriod": period}, "pageError": None}]}

        period_id = _parse_current_period(gs)
        logger.info("[probe] confirm-noop period=%s", period_id)

        # --- Build the CONFIRM payload (include leagueId, confirm=True) ---
        payload = {
            "msgs": [{
                "method": "confirmOrExecuteTeamRosterChanges",
                "data": {
                    "leagueId": league_id,             # keep explicit
                    "rosterLimitPeriod": int(period_id),
                    "fantasyTeamId": team_id,
                    "teamId": team_id,
                    "daily": False,
                    "adminMode": False,
                    "applyToFuturePeriods": False,
                    "fieldMap": fmap,
                    "confirm": True,                   # CONFIRM, not EXECUTE
                    "action": "CONFIRM",
                }
            }],
            "uiv": 3,
            "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster",
            "dt": 0, "at": 0, "av": "0.0",
        }

        # --- Propagate XSRF header (defensive – some pods require it) ---
        _ensure_xsrf_header(session)

        # --- POST and be tolerant of non-JSON replies ---
        try:
            res = session.post(
                "https://www.fantrax.com/fxpa/req",
                params={"leagueId": league_id},
                json=payload,
                timeout=25,
                headers={"Accept": "application/json"},
            )
            try:
                j = res.json()
            except Exception:
                j = {"http_status": res.status_code, "text": (res.text or "")[:800]}
        except Exception as e:
            logger.warning("confirm-noop POST failed: %s", e)
            j = {"http_error": str(e)}

        # Compact, high-signal log
        try:
            fr = (j or {}).get("fantasyResponse") or {}
            model = (j or {}).get("model") or {}
            logger.info(
                "[probe][confirm] type=%s main=%s illegal=%s changeAllowed=%s deadline=%s periodEcho=%s",
                fr.get("msgType"),
                fr.get("mainMsg"),
                len(fr.get("illegalRosterMsgs") or []),
                (model.get("changeAllowed") if isinstance(model, dict) else None),
                (model.get("playerPickDeadlinePassed") if isinstance(model, dict) else None),
                ((((model or {}).get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")) if isinstance(model, dict) else None),
            )
        except Exception:
            pass

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
                resp = api._request("getStandings", view="SCHEDULE")
                period_id = int(resp.get("currentPeriod", 0) or 0)

                confirm_payload = {
                    "rosterLimitPeriod": period_id,
                    "fantasyTeamId": team_id,
                    "daily": False,
                    "adminMode": False,
                    "confirm": True,
                    "applyToFuturePeriods": False,
                    "fieldMap": {
                        **{r.player.id: {"posId": str(r.pos_id), "stId": ("1" if r.pos_id != "0" else "2")}
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
