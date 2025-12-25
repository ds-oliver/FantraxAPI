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
    ConditionalSwapEngine,
    ConditionalSwapRule,
    LineupStatus,
    PlayerLineupInfo,
    RosterView,
    RuleActionType,
    RuleState,
    RuleStorage,
    SwapCondition,
    can_swap_in_period,
    get_available_periods,
    is_row_locked,
    get_row_lock_flags,
    would_break_mandatory_slots,
)
from fantraxapi.waivers import WaiversService
from fantraxapi.lineups.fantrax_lineup_bridge import fetch_fa_status_map
from fantraxapi.lineups.lineup_resolver import LineupSourceStrategy, resolve_lineup_info
from fantraxapi.lineups.fantrax_lineup_bridge import debug_fx_lineup_context
try:
    from fantraxapi.lineups.sofascore_bridge import (
        debug_player_lineup_context,
        infer_current_gameweek,
        _team_code,
        DEFAULT_LINEUPS_DIR,
    )
except ImportError:  # pragma: no cover - fallback for older deployments
    def debug_player_lineup_context(*args, **kwargs):
        return None

    def infer_current_gameweek(*args, **kwargs):
        return None
    _team_code = None  # type: ignore
    DEFAULT_LINEUPS_DIR = Path("data/sofascore/lineups")
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


def _match_period_from_round(
    round_hint: Optional[str],
    period_id_map: Dict[str, str],
) -> Optional[str]:
    """
    Try to map a SofaScore 'round' (EPL gameweek) string to a Fantrax period id,
    using the period labels.

    We try, in order:
      - direct id match
      - substring in label
      - 'GW {round}', 'Gameweek {round}', 'Week {round}' in label
    """
    if not round_hint:
        return None

    round_str = str(round_hint).strip()
    if not round_str:
        return None

    if round_str in period_id_map:
        return round_str

    candidates: list[str] = []
    for pid, label in period_id_map.items():
        label_text = (label or "").lower()
        r = round_str.lower()
        if r and r in label_text:
            candidates.append(pid)
            continue
        if f"gw {r}" in label_text:
            candidates.append(pid)
            continue
        if f"gameweek {r}" in label_text:
            candidates.append(pid)
            continue
        if f"week {r}" in label_text:
            candidates.append(pid)
            continue

    if not candidates:
        return None

    try:
        return sorted(candidates, key=lambda x: int(x))[0]
    except Exception:
        return candidates[0]


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
waivers_service = WaiversService(request_callable=api._request, api=api)
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

# ---------------------------------------------------------------------
# EPL gameweek + Fantrax period selection (single source of truth)
# ---------------------------------------------------------------------
inferred_round = st.session_state.get("current_sofascore_round")
if inferred_round is None:
    inferred_round = infer_current_gameweek()
    if inferred_round:
        st.session_state["current_sofascore_round"] = inferred_round

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

selected_period_id: Optional[str] = None
selected_period_label: str = ""
period_id_map: Dict[str, str] = {}
detected_period: Optional[str] = None

if periods:
    period_id_map = {str(opt["id"]): opt["label"] for opt in periods}
    period_choices = list(period_id_map.keys())

    try:
        detected_period = str(api.resolve_active_period(team_id))
    except Exception:
        detected_period = None

    cached_period = st.session_state.get("selected_gameweek_period_id")
    if cached_period and cached_period in period_id_map:
        default_period_id = cached_period
    else:
        inferred_match = _match_period_from_round(inferred_round, period_id_map)
        if inferred_match and inferred_match in period_id_map:
            default_period_id = inferred_match
        elif detected_period and detected_period in period_id_map:
            default_period_id = detected_period
        else:
            default_period_id = period_choices[0]

    st.subheader("Gameweek / Fantrax period")
    col_gw, col_period = st.columns([1, 3])
    with col_gw:
        st.metric("Inferred EPL GW", inferred_round or "Unknown")
    with col_period:
        selected_period_id = st.selectbox(
            "Fantrax period to use for swaps and rules",
            options=period_choices,
            index=period_choices.index(default_period_id),
            format_func=lambda pid: period_id_map.get(pid, pid),
            key="selected_gameweek_period_id",
            help=(
                "We infer the current EPL gameweek from SofaScore and map it to your Fantrax "
                "scoring periods. You can override it here to target a future gameweek."
            ),
        )

    selected_period_label = period_id_map.get(selected_period_id, "")
    st.caption(
        f"Using Fantrax period **{selected_period_label or selected_period_id}** "
        f"for immediate swaps and new conditional rules."
    )
else:
    st.warning(
        "No roster-change periods are available from Fantrax. "
        "Swaps and rules will fall back to Fantrax's own active period."
    )
    selected_period_id = None
    selected_period_label = ""
    detected_period = None
    st.stop()

# Decide which period to feed into lineup resolution
if selected_period_id is not None:
    try:
        lineup_period = int(selected_period_id)
    except Exception:
        lineup_period = None
else:
    try:
        lineup_period = api.resolve_active_period(team_id)
    except Exception:
        lineup_period = None

with st.spinner("Loading lineup data..."):
    try:
        mapping_manager = PlayerMappingManager()
        lineup_info_by_player = resolve_lineup_info(
            roster,
            session=session,
            league_id=league_id,
            period=lineup_period,
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


def _team_code_for_display(raw: Optional[str]) -> str:
    """
    Normalize to a 3-letter team code when possible.
    Uses SofaScore/Fantrax mapping when available; otherwise first 3 letters.
    """
    if raw in (None, "-", ""):
        return "-"
    try:
        candidate = _team_code(raw) if callable(_team_code) else None
    except Exception:
        candidate = None
    if candidate:
        return candidate
    trimmed = str(raw).strip()
    if not trimmed:
        return "-"
    if len(trimmed) <= 4 and trimmed.replace(" ", "").isalpha():
        return trimmed.upper()
    return trimmed[:3].upper()


def _parse_fetch_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    sanitized = str(raw).replace("+0000", "+00:00")
    try:
        return datetime.fromisoformat(sanitized)
    except Exception:
        return None


def _latest_lineup_fetch_timestamp(lineups_dir: Path) -> Optional[datetime]:
    """
    Scan SofaScore lineup JSON files and return the most recent fetched_at_utc timestamp.
    """
    if not lineups_dir.exists():
        return None
    latest: Optional[datetime] = None
    for path in lineups_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        ts = _parse_fetch_timestamp(data.get("fetched_at_utc"))
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


def _fantrax_team_and_opponent(row) -> tuple[str, str]:
    """
    Extract team and opponent strings from a roster row's Fantrax data.
    """
    player = getattr(row, "player", None)
    team_raw = (
        getattr(player, "team_short_name", None)
        or getattr(player, "team_name", None)
        or ""
    )
    raw = getattr(row, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}
    if not team_raw:
        team_raw = scorer.get("teamShortName") or scorer.get("teamName") or ""

    opp_raw = (
        scorer.get("nextOpponentShortName")
        or scorer.get("nextOpponent")
        or scorer.get("nextOpponentName")
        or ""
    )
    if opp_raw:
        is_away = scorer.get("nextOpponentIsAway")
    else:
        is_away = False

    team = _team_code_for_display(team_raw)
    opp_code = _team_code_for_display(opp_raw)
    opp = f"@{opp_code}" if is_away and opp_code != "-" else opp_code
    return team, opp


def _team_and_opponent_for_player(
    pid: str,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    player_lookup: Dict[str, RosterRow],
) -> tuple[str, str]:
    """
    Prefer schedule-derived team/opponent from PlayerLineupInfo; fallback to Fantrax scorer.
    """
    info = lineup_info_by_player.get(pid)
    if info and (getattr(info, "team_name", None) or getattr(info, "opponent_name", None)):
        team = _team_code_for_display(getattr(info, "team_name", None))
        opp = _team_code_for_display(getattr(info, "opponent_name", None))
        if getattr(info, "is_home", None) is False and opp != "-":
            opp = f"@{opp}"
        return team or "-", opp or "-"

    # Fallback to Fantrax scorer fields on the roster row
    row = player_lookup.get(pid)
    if row:
        return _fantrax_team_and_opponent(row)
    return "-", "-"

def _fmt_debug_datetime(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _opponent_source(info: Optional[PlayerLineupInfo]) -> str:
    """
    Best-effort tag for where team/opponent came from.
    SofaScore schedule entries carry event_id; otherwise we assume Fantrax scorer.
    """
    if info is None:
        return "-"
    if getattr(info, "event_id", None) is not None:
        return "sofascore_schedule"
    if getattr(info, "team_name", None) or getattr(info, "opponent_name", None):
        return "fantrax_scorer"
    return "-"


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

if selected_period_id is not None:
    try:
        swap_period_int = int(selected_period_id)
    except Exception:
        swap_period_int = None
else:
    try:
        swap_period_int = int(api.resolve_active_period(team_id))
    except Exception:
        swap_period_int = None

if swap_period_int is not None:
    st.caption(
        f"Immediate swaps will apply to Fantrax period **{selected_period_label or swap_period_int}**."
    )
else:
    st.warning("Unable to resolve a valid Fantrax period; swap requests may fail.")

if st.button("Execute swap now", type="primary", key="immediate_swap_button_top"):
    if swap_period_int is None:
        st.error("Cannot execute swap because no valid Fantrax period is selected.")
    else:
        try:
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
                    period=int(swap_period_int),
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
latest_lineup_fetch = _latest_lineup_fetch_timestamp(Path(DEFAULT_LINEUPS_DIR))
if latest_lineup_fetch:
    st.caption(f"Predicted lineups refreshed on {latest_lineup_fetch.strftime('%Y-%m-%d %H:%M:%S %Z')}")
else:
    st.caption("Predicted lineups refresh time unavailable.")

with st.expander("What do these columns mean?"):
    st.markdown(
        "- SS predicted / SS confirmed: SofaScore signals split by predicted vs. confirmed lineups (confirmed shows N/A until a confirmed XI is scraped).\n"
        "- SS status / FX status: Source-specific lineup signals; SS status prefers confirmed when present, otherwise predicted.\n"
        "- Effective status: The value used by the engine (SofaScore preferred unless missing).\n"
        "- Kickoff: Effective kickoff used for ordering and locking heuristics.\n"
        "- FX locked / markers: Fantrax disableLineupChange and visual markers that indicate player is locked and cannot be changed."
    )

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
            "SS predicted": _format_status(getattr(info, "ss_pred_status", None)) if info and getattr(info, "ss_pred_status", None) else "N/A",
            "SS confirmed": _format_status(getattr(info, "ss_conf_status", None)) if info and getattr(info, "ss_conf_status", None) else "N/A",
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
            "SS predicted": _format_status(getattr(info, "ss_pred_status", None)) if info and getattr(info, "ss_pred_status", None) else "N/A",
            "SS confirmed": _format_status(getattr(info, "ss_conf_status", None)) if info and getattr(info, "ss_conf_status", None) else "N/A",
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
                "opponent_source": _opponent_source(info),
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

rule_action_label = st.radio(
    "Rule action",
    options=["Lineup swap (bench player)", "FA claim/drop (free agent)"],
    index=0,
    help="Choose whether this rule swaps to an existing bench player or submits a free-agent claim.",
)
selected_action_type = (
    RuleActionType.LINEUP_SWAP
    if rule_action_label.startswith("Lineup")
    else RuleActionType.FA_CLAIM_DROP
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

if not periods:
    st.error("No roster-change periods available. Visit the Overview page to refresh your session.")
    period_id = None
    period_label = ""
else:
    period_id_map = {str(opt["id"]): opt["label"] for opt in periods}
    period_choices = list(period_id_map.keys())

    base_default = st.session_state.get("selected_gameweek_period_id")
    if not base_default or base_default not in period_id_map:
        base_default = period_choices[0]

    cached_period = str(st.session_state.get("conditional_period_id", "")) or None
    if cached_period and cached_period in period_id_map:
        default_period_id = cached_period
    else:
        default_period_id = base_default

    period_id = st.selectbox(
        "Apply rule during period",
        options=period_choices,
        index=period_choices.index(default_period_id),
        format_func=lambda pid: period_id_map.get(pid, pid),
        key="conditional_period_select",
    )
    period_label = period_id_map.get(period_id, "")
    st.session_state["conditional_period_id"] = period_id

candidate_rows: List[Dict[str, str]] = []
candidate_order: Dict[str, int] = {}
debug_candidates: List[Dict[str, object]] = []
selected_backup_ids: List[str] = []
fa_candidate: Optional[Dict[str, str]] = None
fa_bid_amount: float = 0.0

if selected_action_type == RuleActionType.LINEUP_SWAP:
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
                    if would_break_mandatory_slots(
                        roster_view,
                        active_player_id,
                        bench_id,
                        min_gks=1,
                    ):
                        swap_reason = "mandatory slot break"
                    elif not can_swap_in_period(
                        subs_service=subs_service,
                        roster=roster,
                        league_id=league_id,
                        team_id=team_id,
                        active_id=active_player_id,
                        reserve_id=bench_id,
                        period_id=period_id,
                    ):
                        swap_reason = "illegal by Fantrax/formation"
                    else:
                        can_swap_flag = True
                        swap_reason = "ok"

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
                if would_break_mandatory_slots(
                    roster_view,
                    active_player_id,
                    bench_id,
                    min_gks=1,
                ):
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
        if selected_action_type == RuleActionType.LINEUP_SWAP:
            st.info(
                "No eligible reserve players meet the kickoff / lineup / legality requirements right now. "
                "Once confirmed lineups or legal swaps become available, the list will populate automatically."
            )

if selected_action_type == RuleActionType.FA_CLAIM_DROP:
    st.markdown("**Free agent candidate (conditional claim target)**")
    if not active_lineup or not active_lineup.kickoff:
        st.info("Waiting for active player's kickoff/time before evaluating FA candidates.")
    else:
        fa_candidate_rows: List[Dict[str, str]] = []
        with st.spinner("Evaluating eligible free agents..."):
            try:
                fa_status_map = fetch_fa_status_map(session=session, league_id=league_id)
                fa_pool = waivers_service.list_players_by_name(
                    limit=150,
                    status="ALL_AVAILABLE",
                )
            except Exception as exc:
                st.error(f"Failed to load free agent pool: {exc}")
                fa_pool = []
                fa_status_map = {}

            for p in fa_pool:
                sid = str(p.get("id"))
                snapshot = fa_status_map.get(sid) if fa_status_map else None
                if not snapshot:
                    continue
                if snapshot.status != LineupStatus.STARTING:
                    continue
                if snapshot.kickoff:
                    if snapshot.kickoff <= now:
                        continue
                    if active_lineup.kickoff and snapshot.kickoff < active_lineup.kickoff:
                        continue
                if not ConditionalSwapEngine._drop_would_keep_roster_legal(
                    roster_view,
                    drop_id=active_player_id,
                    min_gks=1,
                ):
                    continue
                fa_candidate_rows.append(
                    {
                        "id": sid,
                        "Name": p.get("name") or "",
                        "Team": p.get("team") or "",
                        "Position": p.get("position") or "",
                        "default_pos_id": p.get("default_pos_id"),
                        "Kickoff (UTC)": _format_kickoff(snapshot.kickoff),
                        "Select": False,
                    }
                )

        if fa_candidate_rows:
            fa_df = pd.DataFrame(fa_candidate_rows)
            edited_fa_df = st.data_editor(
                fa_df,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Select": st.column_config.CheckboxColumn("Use as FA target"),
                    "Name": st.column_config.TextColumn("Name", disabled=True),
                    "Team": st.column_config.TextColumn("Team", disabled=True),
                    "Position": st.column_config.TextColumn("Pos", disabled=True),
                    "Kickoff (UTC)": st.column_config.TextColumn("Kickoff (UTC)", disabled=True),
                    "id": st.column_config.TextColumn("id", disabled=True),
                    "default_pos_id": st.column_config.TextColumn("default_pos_id", disabled=True),
                },
                key="conditional_fa_candidates_editor",
            )
            selected = edited_fa_df[edited_fa_df["Select"]]
            if len(selected) > 1:
                st.warning("Select exactly one free agent for this rule.")
            elif len(selected) == 1:
                row = selected.iloc[0]
                fa_candidate = {
                    "id": str(row["id"]),
                    "name": row["Name"],
                    "team": row["Team"],
                    "position": row["Position"],
                    "default_pos_id": str(row["default_pos_id"]),
                }
                st.info(
                    f"FA target selected: **{fa_candidate['name']}** ({fa_candidate['position']} – {fa_candidate['team']})"
                )
        else:
            st.info("No eligible free agents currently meet the starting/legality requirements.")

if selected_action_type == RuleActionType.LINEUP_SWAP:
    submit_disabled = not (
        active_player_id
        and period_id
        and selected_backup_ids
        and active_lineup
        and active_lineup.kickoff
    )
else:
    submit_disabled = not (
        active_player_id
        and period_id
        and fa_candidate
        and active_lineup
    )

submitted = st.button("Save Rule", disabled=submit_disabled, type="primary", key="save_rule_btn")

if submitted and not submit_disabled:
    try:
        if selected_action_type == RuleActionType.LINEUP_SWAP:
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
                action_type=RuleActionType.LINEUP_SWAP,
            )
        else:
            rule = ConditionalSwapRule(
                league_id=league_id,
                team_id=team_id,
                active_player_id=active_player_id,
                backups=[],
                period_id=str(period_id),
                period_label=period_label,
                condition=SwapCondition.NOT_STARTING,
                action_type=RuleActionType.FA_CLAIM_DROP,
                fa_add_scorer_id=fa_candidate["id"] if fa_candidate else None,
                fa_add_position_id=fa_candidate["default_pos_id"] if fa_candidate else None,
                fa_claim_to_status_id="2",
                fa_add_display_name=fa_candidate["name"] if fa_candidate else None,
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
        if rule.action_type == RuleActionType.LINEUP_SWAP:
            backups_display = []
            for backup in rule.sorted_backups():
                row = player_lookup.get(backup.reserve_player_id)
                backups_display.append(row.player.name if row else backup.reserve_player_id)  # type: ignore[union-attr]
            backups_chain = " > ".join(backups_display) if backups_display else "(none)"
            st.markdown(
                f"**Lineup swap rule:** When **{active_name}** is *not starting*, try backups in order: {backups_chain}."
            )
        else:
            fa_label = (
                getattr(rule, "fa_add_display_name", None)
                or getattr(rule, "fa_add_scorer_id", None)
                or "unknown FA"
            )
            st.markdown(
                f"**FA claim/drop rule:** When **{active_name}** is *not starting* and **{fa_label}** is *starting*, "
                f"submit FA claim to add **{fa_label}** and drop **{active_name}** during **{rule.period_label}**."
            )
        info_line = lineup_info_by_player.get(rule.active_player_id)
        status_text = _format_status(info_line.status) if info_line else "Unknown"
        kickoff_text = _format_kickoff(info_line.kickoff if info_line else None)
        condition_text = getattr(rule.condition, "value", str(rule.condition)).replace("_", " ")
        state_text = getattr(rule.state, "value", str(rule.state))
        st.caption(
            f"Period: {rule.period_label} | Type: {getattr(rule.action_type, 'value', str(rule.action_type))} | Condition: {condition_text} "
            f"| Max fires: {rule.max_fires_per_period} | State: {state_text} "
            f"| Current lineup: {status_text} ({kickoff_text})"
        )

        action_cols = st.columns(2)
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
