"""
Conditional swaps page – define tiered backup rules per Fantrax period.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

from apps.auth_login.context import select_league_and_team_in_sidebar
from fantraxapi.fantrax import FantraxAPI
from fantraxapi.lineups.conditional_swaps import (
    BackupOption,
    ConditionalSwapRule,
    LineupStatus,
    PlayerLineupInfo,
    RosterView,
    RuleState,
    RuleStorage,
    SwapCondition,
    can_swap_in_period,
    get_available_periods,
    is_row_locked,
    get_row_lock_flags,
)
from fantraxapi.lineups.lineup_resolver import LineupSourceStrategy, resolve_lineup_info
from fantraxapi.lineups.fantrax_lineup_bridge import debug_fx_lineup_context
try:
    from fantraxapi.lineups.sofascore_bridge import (
        debug_player_lineup_context,
        infer_current_gameweek,
    )
except ImportError:  # pragma: no cover - fallback for older deployments
    def debug_player_lineup_context(*args, **kwargs):
        return None

    def infer_current_gameweek(*args, **kwargs):
        return None
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.subs import SubsService
from urllib.parse import unquote
from requests import Session

try:
    from utils.auth_helpers import load_requests_session_from_artifacts
except ImportError:  # pragma: no cover - fallback for older deployments
    load_requests_session_from_artifacts = None  # type: ignore

logger = logging.getLogger(__name__)
_LOG_PATH = Path("data/logs/conditional_swaps.log")
if not any(getattr(h, "baseFilename", None) == str(_LOG_PATH) for h in logger.handlers):
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(_LOG_PATH)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [conditional_swaps] %(message)s"))
        logger.addHandler(fh)
    except Exception:
        # Fall back to default handlers if file handler setup fails
        pass

st.set_page_config(page_title="Conditional Swaps", page_icon="♻️", layout="wide")
st.title("Conditional Swap Rules")
st.caption("Define conditional active ↔ reserve swaps with ordered backups per Fantrax period.")
st.info(
    "Swap options are determined by your current starting XI. Players with early kickoffs make better "
    "conditional anchors because you can queue backups that play later. Adjust your lineup below before "
    "defining swap rules."
)


# ---------------------------------------------------------------------
# Session / auth helpers
# ---------------------------------------------------------------------
def _require_session() -> Optional["requests.Session"]:
    if "session" in st.session_state:
        return st.session_state["session"]
    if "auth_artifacts" not in st.session_state or load_requests_session_from_artifacts is None:
        return None
    try:
        session = load_requests_session_from_artifacts(st.session_state["auth_artifacts"])
    except Exception as exc:  # pragma: no cover - Streamlit runtime feedback
        logger.exception("Failed to rebuild session from artifacts")
        st.error(f"Failed to rebuild Fantrax session: {exc}")
        return None
    st.session_state["session"] = session
    return session


def _ensure_xsrf_header(session: Session) -> None:
    token = None
    for c in session.cookies:
        if c.name.upper().startswith("XSRF-TOKEN"):
            token = unquote(c.value or "")
            break
    if token:
        session.headers["X-XSRF-TOKEN"] = token


def _apply_fxpa_client_hints(session: Session) -> None:
    if session.headers.get("X-Fantrax-UI-Version") and session.headers.get("X-TZ"):
        return
    payload = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}], "uiv": 3}
    headers = {
        "Accept": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        res = session.post(
            "https://www.fantrax.com/fxpa/req",
            json=payload,
            timeout=20,
            headers=headers,
        )
        try:
            j = res.json()
        except Exception:
            j = {}
    except Exception:
        j = {}
    ui_version = ((j.get("data") or {}).get("up")) or ""
    if ui_version:
        session.headers["X-Fantrax-UI-Version"] = ui_version
    session.headers.setdefault("X-TZ", "America/Los_Angeles")


session = _require_session()
if session is None:
    st.error("Please authenticate on the Overview page first.")
    st.stop()

_ensure_xsrf_header(session)
_apply_fxpa_client_hints(session)

league_id, team_id = select_league_and_team_in_sidebar(session=session)
if not league_id or not team_id:
    st.warning("Select a league and team in the sidebar to manage conditional rules.")
    st.stop()

current_api = st.session_state.get("api")
cached_league = st.session_state.get("api_league_id")
if current_api is None or cached_league != league_id:
    try:
        api = FantraxAPI(league_id=league_id, session=session)
    except Exception as exc:
        logger.exception("Failed to initialize Fantrax API")
        st.error(f"Unable to initialize Fantrax API: {exc}")
        st.stop()
    st.session_state["api"] = api
    st.session_state["api_league_id"] = league_id
else:
    api = current_api

try:
    roster = api.roster_info(team_id)
except Exception as exc:
    st.error(f"Failed to load roster: {exc}")
    st.stop()

subs_service = SubsService(session=session, league_id=league_id)
roster_view = RosterView(roster)
player_lookup = {
    str(getattr(row.player, "id")): row
    for row in roster.rows
    if getattr(row, "player", None) and getattr(row.player, "id", None)
}

lineup_source_label = st.radio(
    "Lineup status source",
    options=[
        "SofaScore (recommended – fastest)",
        "Fantrax internal flags (slower fallback)",
    ],
    index=0,
    help=(
        "SofaScore updates within minutes of confirmed lineups. "
        "Fantrax's internal player icons can lag but cover cases with missing SofaScore data."
    ),
)
strategy = (
    LineupSourceStrategy.SOFASCORE_PRIMARY
    if lineup_source_label.startswith("SofaScore")
    else LineupSourceStrategy.FANTRAX_PRIMARY
)

inferred_round = st.session_state.get("current_sofascore_round")
if inferred_round is None:
    inferred_round = infer_current_gameweek()
    if inferred_round:
        st.session_state["current_sofascore_round"] = inferred_round

with st.spinner("Loading lineup data..."):
    try:
        mapping_manager = PlayerMappingManager()
        try:
            detected_period = api.resolve_active_period(team_id)
        except Exception:
            detected_period = None
        lineup_info_by_player = resolve_lineup_info(
            roster,
            session=session,
            league_id=league_id,
            period=detected_period,
            strategy=strategy,
            mapping_manager=mapping_manager,
            round_hint=inferred_round,
        )
    except Exception as exc:
        logger.exception("Failed to resolve lineup info map")
        st.error(f"Unable to build lineup info map: {exc}")
        st.stop()

storage = RuleStorage()
now = datetime.now(timezone.utc)


# ---------------------------------------------------------------------
# Formatting / debug helpers
# ---------------------------------------------------------------------
def _format_status(status: LineupStatus) -> str:
    if not status:
        return "Unknown"
    return status.value.replace("_", " ").title()


def _format_kickoff(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%b %d %H:%M UTC")


def _fantrax_team_and_opponent(row) -> tuple[str, str]:
    """
    Extract team and opponent strings from a roster row's Fantrax data.
    """
    player = getattr(row, "player", None)
    team = (
        getattr(player, "team_short_name", None)
        or getattr(player, "team_name", None)
        or ""
    )
    raw = getattr(row, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}
    if not team:
        team = scorer.get("teamShortName") or scorer.get("teamName") or ""

    opp = (
        scorer.get("nextOpponentShortName")
        or scorer.get("nextOpponent")
        or scorer.get("nextOpponentName")
        or ""
    )
    if opp:
        is_away = scorer.get("nextOpponentIsAway")
        if is_away:
            opp = f"@ {opp}"
    return team or "-", opp or "-"


def _team_and_opponent_for_player(
    pid: str,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    player_lookup: Dict[str, RosterRow],
) -> tuple[str, str]:
    """
    Prefer schedule-derived team/opponent from PlayerLineupInfo; fallback to Fantrax scorer.
    """
    info = lineup_info_by_player.get(pid)
<<<<<<< ours
    if info:
        # 1) Fantrax scores-based fields (fx_*)
        if getattr(info, "fx_team_name", None) or getattr(info, "fx_opponent_name", None):
            team = getattr(info, "fx_team_name", None) or "-"
            opp = getattr(info, "fx_opponent_name", None) or "-"
            if getattr(info, "fx_is_home", None) is False and opp != "-":
                opp = f"@ {opp}"
            return team or "-", opp or "-"

        # 2) SofaScore schedule-derived fields
        if getattr(info, "team_name", None) or getattr(info, "opponent_name", None):
            team = getattr(info, "team_name", None) or "-"
            opp = getattr(info, "opponent_name", None) or "-"
            if getattr(info, "is_home", None) is False and opp != "-":
                opp = f"@ {opp}"
            return team or "-", opp or "-"

    # 3) Fallback to Fantrax scorer fields on the roster row
=======
    if info and (getattr(info, "team_name", None) or getattr(info, "opponent_name", None)):
        team = getattr(info, "team_name", None) or "-"
        opp = getattr(info, "opponent_name", None) or "-"
        if getattr(info, "is_home", None) is False and opp != "-":
            opp = f"@ {opp}"
        return team or "-", opp or "-"

>>>>>>> theirs
    row = player_lookup.get(pid)
    if row:
        return _fantrax_team_and_opponent(row)
    return "-", "-"

            opp = f"@ {opp}"
        return team or "-", opp or "-"

    row = player_lookup.get(pid)
    if row:
        return _fantrax_team_and_opponent(row)
    return "-", "-"

def _fmt_debug_datetime(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _dump_debug_for_player(fantrax_player_id: Optional[str]) -> None:
    if not fantrax_player_id:
        return
    row = player_lookup.get(fantrax_player_id)
    raw_lock_flags = {}
    if row:
        raw = getattr(row, "_raw", {}) or {}
        raw_lock_flags = {
            k: v
            for k, v in raw.items()
            if isinstance(k, str) and "lock" in k.lower()
        }
        for key in ("canMove", "periodLocked", "lineupAdjustmentAllowed"):
            if key in raw:
                raw_lock_flags[key] = raw[key]
        try:
            logger.info(
                "[lock-debug] raw row for %s: %s",
                fantrax_player_id,
                json.dumps(raw, default=str),
            )
        except Exception:
            try:
                logger.info("[lock-debug] raw row keys for %s: %s", fantrax_player_id, list(raw.keys()))
            except Exception:
                pass

    debug_ctx = debug_player_lineup_context(
        roster,
        fantrax_player_id,
        mapping_manager=mapping_manager,
        round_hint=inferred_round,
    )
    fx_debug = debug_fx_lineup_context(roster, fantrax_player_id)
    if not debug_ctx:
        st.info("No SofaScore debug info available for the selected player.")
        return
    player_name = player_lookup[fantrax_player_id].player.name
    payload = {
        "fantrax_player_id": debug_ctx.get("fantrax_player_id"),
        "sofascore_player_id": debug_ctx.get("sofascore_player_id"),
        "event_id": debug_ctx.get("snapshot_event_id"),
        "confirmed": debug_ctx.get("snapshot_confirmed"),
        "role": debug_ctx.get("snapshot_role"),
        "lineup_status": (
            debug_ctx.get("lineup_status").value
            if isinstance(debug_ctx.get("lineup_status"), LineupStatus)
            else debug_ctx.get("lineup_status")
        ),
        "kickoff": _fmt_debug_datetime(debug_ctx.get("kickoff")),
        "fantrax_kickoff": _fmt_debug_datetime(debug_ctx.get("fantrax_kickoff")),
        "schedule_event_id": debug_ctx.get("schedule_event_id"),
        "schedule_kickoff": _fmt_debug_datetime(debug_ctx.get("schedule_kickoff")),
        "snapshot_source": str(debug_ctx.get("snapshot_source")),
        "locked_local": roster_view.is_locked(
            fantrax_player_id,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        ),
        "raw_lock_flags": raw_lock_flags,
    }
    st.markdown(f"**SofaScore debug for {player_name} ({fantrax_player_id})**")
    st.code(json.dumps(payload, indent=2))
    if fx_debug:
        st.markdown("**Fantrax lineup debug**")
        st.code(json.dumps(fx_debug, indent=2, default=str))


# ----------------------------------------------------------------------
# Immediate swap section (single)
# ----------------------------------------------------------------------
st.divider()
st.subheader("Make lineup adjustments before defining conditional rules")
st.write(
    "Conditional swaps move an active player to the bench (or vice versa) when certain conditions are met. "
    "Because of that, the current starting XI is the foundation for every rule. "
    "If you want more options later—especially for early kickoffs—make sure your active spots start with those players now."
)

st.subheader("Immediate active ↔ reserve swap (apply now)")

live_active_ids = [
    pid for pid in roster_view.active_player_ids() if pid in player_lookup
]
live_reserve_ids = [
    pid for pid in roster_view.reserve_player_ids() if pid in player_lookup
]

swap_active = st.selectbox(
    "Active to swap out",
    options=live_active_ids,
    format_func=lambda pid: f"{player_lookup[pid].player.name} ({getattr(player_lookup[pid].pos, 'short_name', '')})",
    key="immediate_swap_active_top",
)
swap_reserve = st.selectbox(
    "Reserve to swap in",
    options=live_reserve_ids,
    format_func=lambda pid: f"{player_lookup[pid].player.name} ({getattr(player_lookup[pid].pos, 'short_name', '')})",
    key="immediate_swap_reserve_top",
)

_dump_debug_for_player(swap_active)

try:
    detected_swap_period = api.resolve_active_period(team_id)
except Exception:
    detected_swap_period = 1

swap_period = st.number_input(
    "Period (gameweek) for immediate swap",
    min_value=1,
    max_value=50,
    value=max(1, detected_swap_period),
    step=1,
    key="immediate_swap_period_top",
    help="Set the Fantrax period this immediate swap should apply to.",
)

if st.button("Execute swap now", type="primary", key="immediate_swap_button_top"):
    try:
        try:
            current_period = int(swap_period)
        except Exception:
            current_period = api.resolve_active_period(team_id)
        fresh_roster = api.roster_info(team_id)
        starters = [r.player.id for r in fresh_roster.get_starters() if getattr(r, "player", None)]
        if swap_active not in starters:
            st.error("Selected active player is no longer a starter.")
        elif swap_reserve in starters:
            st.info("Reserve player is already a starter.")
        else:
            result = subs_service.swap_players(
                team_id=team_id,
                out_player_id=swap_active,
                in_player_id=swap_reserve,
                period=int(current_period),
            )
            if result.get("success"):
                msg = result.get("message") or "Swap executed successfully."
                st.success(f"✅ {msg}")
                st.rerun()
            else:
                st.error(
                    f"Swap failed: {result.get('error') or result.get('message') or 'Unknown error'}"
                )
    except Exception as exc:  # pragma: no cover - runtime feedback
        logger.exception("Immediate swap failed")
        st.error(f"Immediate swap failed: {exc}")


# ----------------------------------------------------------------------
# Current lineup snapshot (single)
# ----------------------------------------------------------------------
st.subheader("Current Lineup Snapshot")

active_display = []
for pid in roster_view.active_player_ids():
    row = player_lookup.get(pid)
    if not row:
        continue
    info = lineup_info_by_player.get(pid)
    team_name, opponent = _team_and_opponent_for_player(
        pid, lineup_info_by_player, player_lookup
    )
    if hasattr(roster_view, "lock_flags"):
        lock_flags = roster_view.lock_flags(
            pid,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    else:  # fallback for older RosterView without lock_flags
        lock_flags = get_row_lock_flags(
            row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    locked = lock_flags.get("fx_locked", False)
    visually_locked = lock_flags.get("visually_locked", False)
    effective_kickoff = None if visually_locked else (info.kickoff if info else None)
    active_display.append(
        {
            "Player": row.player.name,
            "Pos": getattr(row.pos, "short_name", ""),
            "Team": team_name,
            "Opponent": opponent,
            "SS status": _format_status(getattr(info, "ss_status", None)) if info and getattr(info, "ss_status", None) else "Unknown",
            "FX status": _format_status(getattr(info, "fx_status", None)) if info and getattr(info, "fx_status", None) else "Unknown",
            "Effective status": _format_status(info.status) if info else "Unknown",
            "Kickoff": _format_kickoff(effective_kickoff),
            "FX locked": "Yes" if locked else "No",
            "Kickoff passed": "Yes" if lock_flags.get("kickoff_passed") else "No",
            "Finished marker": "Yes" if lock_flags.get("finished_marker") else "No",
            "Visually locked": "Yes" if visually_locked else "No",
        }
    )

reserve_display = []
for pid in roster_view.reserve_player_ids():
    row = player_lookup.get(pid)
    if not row:
        continue
    info = lineup_info_by_player.get(pid)
    team_name, opponent = _team_and_opponent_for_player(
        pid, lineup_info_by_player, player_lookup
    )
    if hasattr(roster_view, "lock_flags"):
        lock_flags = roster_view.lock_flags(
            pid,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    else:
        lock_flags = get_row_lock_flags(
            row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    locked = lock_flags.get("fx_locked", False)
    visually_locked = lock_flags.get("visually_locked", False)
    effective_kickoff = None if visually_locked else (info.kickoff if info else None)
    reserve_display.append(
        {
            "Player": row.player.name,
            "Pos": getattr(row.pos, "short_name", ""),
            "Team": team_name,
            "Opponent": opponent,
            "SS status": _format_status(getattr(info, "ss_status", None)) if info and getattr(info, "ss_status", None) else "Unknown",
            "FX status": _format_status(getattr(info, "fx_status", None)) if info and getattr(info, "fx_status", None) else "Unknown",
            "Effective status": _format_status(info.status) if info else "Unknown",
            "Kickoff": _format_kickoff(effective_kickoff),
            "FX locked": "Yes" if locked else "No",
            "Kickoff passed": "Yes" if lock_flags.get("kickoff_passed") else "No",
            "Finished marker": "Yes" if lock_flags.get("finished_marker") else "No",
            "Visually locked": "Yes" if visually_locked else "No",
        }
    )

formation = (
    f"Formation: GK {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'G')} "
    f"/ DEF {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'D')} "
    f"/ MID {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'M')} "
    f"/ FWD {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'F')}"
)
st.caption(formation)

active_df = pd.DataFrame(active_display)
reserve_df = pd.DataFrame(reserve_display)

def _style_locked(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    def _row_style(row):
        if row.get("Visually locked") == "Yes":
            return ["background-color: #333333; color: #FF9966;" for _ in row]
        return [""] * len(row)

    styler = df.style.apply(_row_style, axis=1)
    try:
        styler = styler.hide_columns(
            [
                "FX locked",
                "Kickoff passed",
                "Finished marker",
                "Visually locked",
            ]
        )
    except Exception:
        pass
    return styler

st.caption("Active XI (Lineup status is the latest designation)")
st.dataframe(_style_locked(active_df), use_container_width=True, hide_index=True)
st.caption("Reserves (Lineup status is the latest designation)")
st.dataframe(_style_locked(reserve_df), use_container_width=True, hide_index=True)

with st.expander("🔍 Debug: Team/Opponent context"):
    debug_rows = []
    for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
        row = player_lookup.get(pid)
        if not row:
            continue
        info = lineup_info_by_player.get(pid)
        if hasattr(roster_view, "lock_flags"):
            lock_flags = roster_view.lock_flags(
                pid,
                now=now,
                lineup_info_by_player=lineup_info_by_player,
            )
        else:
            lock_flags = get_row_lock_flags(
                row,
                now=now,
                lineup_info_by_player=lineup_info_by_player,
            )
        team_name, opponent = _team_and_opponent_for_player(
            pid, lineup_info_by_player, player_lookup
        )
        debug_rows.append(
            {
                "Player": row.player.name,
                "Team": team_name,
                "Opponent": opponent,
                "Effective status": _format_status(info.status) if info else "Unknown",
                "Kickoff": _fmt_debug_datetime(getattr(info, "kickoff", None)) if info else None,
                "FX locked": bool(lock_flags.get("fx_locked")),
                "event_id": getattr(info, "event_id", None) if info else None,
                "status_source": getattr(info, "status_source", None) if info else None,
                "team_name": getattr(info, "team_name", None) if info else None,
                "opponent_name": getattr(info, "opponent_name", None) if info else None,
                "is_home": getattr(info, "is_home", None) if info else None,
            }
        )
    if debug_rows:
        st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)
    else:
        st.caption("No lineup snapshot data available.")

# ----------------------------------------------------------------------
# Create Rule
# ----------------------------------------------------------------------
st.divider()
st.subheader("Create Rule")

active_rows = [
    row
    for row in roster.rows
    if getattr(row, "player", None) and getattr(row, "pos_id", "0") != "0"
]

if not active_rows:
    st.warning("No active players detected on this roster.")
    st.stop()

active_labels = {
    str(row.player.id): f"{row.player.name} ({getattr(row.pos, 'short_name', '')})"
    for row in active_rows
}

# Live-updating controls (outside the form)
active_player_id = st.selectbox(
    "Active player to monitor",
    options=list(active_labels.keys()),
    format_func=lambda pid: active_labels.get(pid, pid),
    key="conditional_active_select",
)

active_row = player_lookup.get(active_player_id)
active_lineup = lineup_info_by_player.get(active_player_id)

if active_row:
    status_text = _format_status(active_lineup.status) if active_lineup else "Unknown"
    kickoff_text = _format_kickoff(
        active_lineup.kickoff if active_lineup else None
    )
    st.markdown(
        f"**Selected**: {active_row.player.name} (`{active_player_id}`) — lineup status: "
        f"`{status_text}` (kickoff {kickoff_text})"
    )

periods: List[Dict[str, str]] = []
try:
    periods = get_available_periods(
        league_id=league_id,
        team_id=team_id,
        session=session,
    )
except Exception as exc:
    logger.exception("Failed to load roster-change periods")
    st.error(f"Unable to load Fantrax periods: {exc}")
    periods = []

if not periods:
    st.error("No roster-change periods available. Visit the Overview page to refresh your session.")
    period_id = None
    period_label = ""
else:
    period_id_map = {str(opt["id"]): opt["label"] for opt in periods}
    period_choices = list(period_id_map.keys())

    def _match_period_from_round(round_hint: Optional[str]) -> Optional[str]:
        if not round_hint:
            return None
        round_hint = str(round_hint)
        if round_hint in period_id_map:
            return round_hint
        for pid, label in period_id_map.items():
            if round_hint in (label or ""):
                return pid
        return None

    default_idx = 0
    cached_period = str(st.session_state.get("conditional_period_id", ""))
    if cached_period and cached_period in period_id_map:
        default_idx = period_choices.index(cached_period)
    else:
        inferred_period_id = _match_period_from_round(inferred_round)
        if inferred_period_id and inferred_period_id in period_id_map:
            default_idx = period_choices.index(inferred_period_id)

    period_id = st.selectbox(
        "Apply rule during period",
        options=period_choices,
        index=default_idx,
        format_func=lambda pid: period_id_map.get(pid, pid),
        key="conditional_period_select",
    )
    period_label = period_id_map.get(period_id, "")
    st.session_state["conditional_period_id"] = period_id

# --- Form for backup selection + SAVE button ---
with st.form("conditional_rule_form", clear_on_submit=False):

    candidate_rows: List[Dict[str, str]] = []
    candidate_order: Dict[str, int] = {}
    debug_candidates: List[Dict[str, object]] = []

    if (
        active_player_id
        and active_lineup
        and active_lineup.kickoff
        and active_lineup.status != LineupStatus.UNKNOWN
        and period_id
    ):
        bench_rows = [
            row
            for row in roster.rows
            if getattr(row, "player", None) and getattr(row, "pos_id", "0") == "0"
        ]
        with st.spinner("Evaluating eligible backups..."):
            for row in bench_rows:
                bench_id = str(row.player.id)
                locked = is_row_locked(
                    row,
                    now=now,
                    lineup_info_by_player=lineup_info_by_player,
                )
                info = lineup_info_by_player.get(bench_id)
                status_known = bool(info and info.status != LineupStatus.UNKNOWN)
                kickoff_ok = bool(
                    info
                    and info.kickoff
                    and info.kickoff >= now
                    and info.kickoff >= active_lineup.kickoff
                )
                can_swap_flag = False
                swap_reason = ""
                if locked:
                    swap_reason = "locked"
                elif not info:
                    swap_reason = "no status"
                elif info.status != LineupStatus.STARTING:
                    swap_reason = f"status={info.status.value}"
                elif not kickoff_ok:
                    swap_reason = "kickoff earlier than active or missing"
                else:
                    if can_swap_in_period(
                        subs_service=subs_service,
                        roster=roster,
                        league_id=league_id,
                        team_id=team_id,
                        active_id=active_player_id,
                        reserve_id=bench_id,
                        period_id=period_id,
                    ):
                        can_swap_flag = True
                        swap_reason = "ok"
                    else:
                        swap_reason = "illegal by Fantrax/formation"

                debug_candidates.append(
                    {
                        "Player": row.player.name,
                        "Status": info.status.value if info else "unknown",
                        "Kickoff": _format_kickoff(info.kickoff) if info else "—",
                        "Locked": locked,
                        "Has lineup": status_known,
                        "Future kickoff ok": kickoff_ok,
                        "Swap legal": can_swap_flag,
                        "Reason": swap_reason,
                    }
                )

                if locked:
                    continue
                info = lineup_info_by_player.get(bench_id)
                if not info or info.status != LineupStatus.STARTING:
                    continue
                if not info.kickoff or info.kickoff < now:
                    continue
                if info.kickoff < active_lineup.kickoff:
                    continue
                if not can_swap_in_period(
                    subs_service=subs_service,
                    roster=roster,
                    league_id=league_id,
                    team_id=team_id,
                    active_id=active_player_id,
                    reserve_id=bench_id,
                    period_id=period_id,
                ):
                    continue
                pos = SubsService._pos_of_row(row)  # type: ignore[attr-defined]
                candidate_rows.append(
                    {
                        "player_id": bench_id,
                        "Player": f"{row.player.name} ({pos})",
                        "Kickoff (UTC)": _format_kickoff(info.kickoff),
                        "Status": _format_status(info.status),
                        "Select": False,
                        "Priority": len(candidate_rows) + 1,
                    }
                )
                candidate_order[bench_id] = len(candidate_rows)
    elif active_lineup and active_lineup.status == LineupStatus.UNKNOWN:
        st.info("Waiting for confirmed SofaScore lineup before backups can be evaluated.")
    else:
        if active_lineup and not active_lineup.kickoff:
            st.info("Active player has no upcoming kickoff; waiting for schedule/lineup data.")

    if debug_candidates:
        with st.expander("🔍 Debug: backup eligibility checks"):
            st.dataframe(pd.DataFrame(debug_candidates))

    selected_backup_ids: List[str] = []
    if candidate_rows:
        candidate_df = pd.DataFrame(candidate_rows)
        edited_df = st.data_editor(
            candidate_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Select": st.column_config.CheckboxColumn(
                    "Use as backup", help="Enable to include this reserve in the rule."
                ),
                "Priority": st.column_config.NumberColumn(
                    "Priority",
                    min_value=1,
                    max_value=len(candidate_rows),
                    step=1,
                    help="Lower numbers fire first when multiple backups are eligible.",
                ),
                "Player": st.column_config.TextColumn("Player", disabled=True),
                "Kickoff (UTC)": st.column_config.TextColumn("Kickoff (UTC)", disabled=True),
                "Status": st.column_config.TextColumn("Status", disabled=True),
            },
            key="conditional_candidates_editor",
        )
        selected = edited_df[edited_df["Select"]].copy()
        if not selected.empty:
            selected["Priority"] = pd.to_numeric(selected["Priority"], errors="coerce")
            ordering = []
            for _, row in selected.iterrows():
                pid = str(row["player_id"])
                priority = row["Priority"]
                if pd.isna(priority):
                    priority = float("inf")
                ordering.append((priority, candidate_order.get(pid, 0), pid))
            ordering.sort(key=lambda item: (item[0], item[1]))
            selected_backup_ids = [pid for _, _, pid in ordering]
    else:
        st.info(
            "No eligible reserve players meet the kickoff / lineup / legality requirements right now. "
            "Once confirmed lineups or legal swaps become available, the list will populate automatically."
        )

    submit_disabled = not (
        active_player_id
        and period_id
        and selected_backup_ids
        and active_lineup
        and active_lineup.kickoff
    )

    # THIS is the submit button Streamlit was complaining about missing
    submitted = st.form_submit_button("Save Rule", disabled=submit_disabled, type="primary")

    if submitted and not submit_disabled:
        try:
            rule = ConditionalSwapRule(
                league_id=league_id,
                team_id=team_id,
                active_player_id=active_player_id,
                backups=[
                    BackupOption(reserve_player_id=pid, priority=index + 1)
                    for index, pid in enumerate(selected_backup_ids)
                ],
                period_id=str(period_id),
                period_label=period_label,
                condition=SwapCondition.NOT_STARTING,
            )
            storage.save_rule(rule)
            st.success("Rule saved successfully.")
            st.rerun()
        except ValueError as exc:
            st.warning(str(exc))
        except Exception as exc:  # pragma: no cover - runtime feedback
            logger.exception("Failed to save conditional rule")
            st.error(f"Failed to save rule: {exc}")


# ----------------------------------------------------------------------
# Existing rules
# ----------------------------------------------------------------------
rules = storage.load_rules_for_team(league_id, team_id)
if not rules:
    st.info("No conditional swap rules configured yet.")
else:
    st.divider()
    st.subheader("Existing Conditional Rules")
    for rule in rules:
        active_name = (
            player_lookup.get(rule.active_player_id).player.name  # type: ignore[union-attr]
            if player_lookup.get(rule.active_player_id)
            else rule.active_player_id
        )
        backups_display = []
        for backup in rule.sorted_backups():
            row = player_lookup.get(backup.reserve_player_id)
            backups_display.append(row.player.name if row else backup.reserve_player_id)  # type: ignore[union-attr]
        backups_chain = " > ".join(backups_display) if backups_display else "(none)"

        st.markdown(f"**{active_name}** ⇢ {backups_chain}")
        info_line = lineup_info_by_player.get(rule.active_player_id)
        status_text = _format_status(info_line.status) if info_line else "Unknown"
        kickoff_text = _format_kickoff(info_line.kickoff if info_line else None)
        st.caption(
            f"Period: {rule.period_label} | Condition: {rule.condition.value.replace('_', ' ')} "
            f"| Max fires: {rule.max_fires_per_period} | State: {rule.state.value} "
            f"| Current lineup: {status_text} ({kickoff_text})"
        )

        action_cols = st.columns(3)
        toggle_label = "Disable" if rule.state == RuleState.ACTIVE else "Enable"
        if action_cols[0].button(
            f"{toggle_label} Rule",
            key=f"toggle_{rule.id}",
            use_container_width=True,
        ):
            if rule.state == RuleState.ACTIVE:
                storage.disable_rule(rule.id)
            else:
                storage.enable_rule(rule.id)
            st.rerun()

        if action_cols[1].button(
            "Delete",
            key=f"delete_{rule.id}",
            use_container_width=True,
        ):
            storage.delete_rule(rule.id)
            st.rerun()

        st.caption(f"Rule ID: {rule.id}")
        st.markdown("---")
