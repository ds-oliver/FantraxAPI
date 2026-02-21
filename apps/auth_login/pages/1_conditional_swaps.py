"""
Conditional swaps page – define tiered backup rules per Fantrax period.
"""

from __future__ import annotations

import csv
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import yaml

from apps.auth_login.conditional_swaps_health import (
    build_fired_conditional_events,
    compute_health_metrics,
    fetch_lineup_change_history,
    match_events,
    normalize_lineup_change_rows,
    resolve_round_from_period,
)
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
    test_swap_in_period,
    would_break_mandatory_slots,
)
from utils.conditional_rule_store import (
    SOURCE_AUTO_LINEUP_SWAPS,
    apply_rule_operations_for_user,
    append_rules,
    is_conditional_state_writer,
    load_rules_for_user_state,
    normalize_rule_source,
)
from fantraxapi.objs import RosterRow
from fantraxapi.waivers import WaiversService
try:
    from fantraxapi.lineups.fantrax_lineup_bridge import (
        fetch_fa_status_map,
        global_status_path_for_user,
    )
except ImportError:  # pragma: no cover - fallback for older deployments
    from fantraxapi.lineups.fantrax_lineup_bridge import (
        fetch_fa_status_map,
        DEFAULT_GLOBAL_STATUS_PATH,
    )

    def global_status_path_for_user(user_id: Optional[str]) -> Path:
        if not user_id:
            return DEFAULT_GLOBAL_STATUS_PATH
        safe_id = "".join(ch for ch in str(user_id).strip() if ch.isalnum() or ch in ("-", "_"))
        if not safe_id:
            return DEFAULT_GLOBAL_STATUS_PATH
        return Path("data/fantrax") / f"global_icons_{safe_id}.json"
from fantraxapi.lineups.lineup_resolver import LineupSourceStrategy, resolve_lineup_info
from fantraxapi.lineups.fantrax_lineup_bridge import debug_fx_lineup_context
from fantraxapi.lineups.team_strengths import (
    DEFAULT_STRENGTH_PATH,
    load_strengths,
    strength_for_team,
)
from services.sofascore_lineup_service import (
    raw_get_lineups,
    lineup_to_json,
    raw_iter_tournament_events,
    safe_team_id,
    parse_kickoff_dt,
    raw_get_seasons,
    choose_season_from_list,
)
try:
    from fantraxapi.lineups.sofascore_bridge import (
        debug_player_lineup_context,
        infer_current_gameweek,
        _team_code,
        TEAM_NAME_ALIASES,
        DEFAULT_LINEUPS_DIR,
    )
except ImportError:  # pragma: no cover - fallback for older deployments
    def debug_player_lineup_context(*args, **kwargs):
        return None

    def infer_current_gameweek(*args, **kwargs):
        return None
    _team_code = None  # type: ignore
    DEFAULT_LINEUPS_DIR = Path("data/sofascore/lineups")
    TEAM_NAME_ALIASES = {}
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.subs import SubsService
from urllib.parse import unquote
from requests import Session
from zoneinfo import ZoneInfo

try:
    from utils.auth_helpers import load_requests_session_from_artifacts
except ImportError:  # pragma: no cover - fallback for older deployments
    load_requests_session_from_artifacts = None  # type: ignore
from utils.user_manager import UserManager

logger = logging.getLogger(__name__)
_LOG_PATH = Path("data/logs/conditional_swaps.log")
_HEALTH_DEBUG_PATH = Path("data/logs/conditional_swaps_health_debug.jsonl")
LOG_TIMEZONE = "America/Los_Angeles"
HEALTH_MATCH_WINDOW_SECONDS = 120
HEALTH_TIMEZONE = "America/Los_Angeles"

def _log_time_converter(*_args):
    return datetime.now(ZoneInfo(LOG_TIMEZONE)).timetuple()

if not any(getattr(h, "baseFilename", None) == str(_LOG_PATH) for h in logger.handlers):
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Keep this log bounded in size to avoid multi-GB growth.
        fh = RotatingFileHandler(_LOG_PATH, maxBytes=300 * 1024 * 1024, backupCount=2)
        formatter = logging.Formatter("%(asctime)s %(levelname)s [conditional_swaps] %(message)s")
        formatter.converter = _log_time_converter
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception:
        # Fall back to default handlers if file handler setup fails
        pass

st.set_page_config(page_title="Conditional Swaps", page_icon="♻️", layout="wide")
if "auth_artifacts" not in st.session_state or load_requests_session_from_artifacts is None:
    st.error("Please authenticate on the Overview page first.")
    st.stop()
current_user_id = st.session_state.get("current_user_id")
if current_user_id and st.session_state.get("user_id") != current_user_id:
    for key in ("auth_artifacts", "session", "user_id", "user_email"):
        st.session_state.pop(key, None)
    st.error("Please authenticate on the Overview page first.")
    st.stop()

st.title("Conditional Swap Rules")
st.caption("Define conditional active ↔ reserve swaps with ordered backups per Fantrax period.")
st.info(
    "Swap options are determined by your current starting XI. Players with early kickoffs make better "
    "conditional anchors because you can queue backups that play later. Adjust your lineup below before "
    "defining swap rules."
)

FA_ACTION_ADD_ONLY = "fa_add_only"
FA_ACTION_DROP_ONLY = "drop_only"
FA_TRIGGER_MODE_FA_STARTING_ONLY = "fa_starting_only"
FA_TRIGGER_MODE_DROP_AND_FA_STARTING = "drop_not_starting_and_fa_starting"
FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE = "drop_not_starting_then_claim_immediate"
FA_SIMPLE_MODE_CLAIM_BASED = "claim_based"
FA_SIMPLE_MODE_DROP_BASED = "drop_based"
TEST_CLAIMS_LEAGUE_ID = "0z7r5871mc1yqc0s"

readme_fields_md = """
This page **writes directly to your Fantrax roster** (immediate swaps and optimized lineup applies).
Double-check the period and lineup before you click any action buttons.

**Sidebar (top to bottom)**
1. League / team selector.
2. Auto lineup swaps (per league).
3. Auto claims/drops (disabled during testing).
4. (Beta) FA add/drop builder toggle (per league).
5. Late KOS coverage note (warns when a late kickoff lacks same-KOS cover).
6. Late KOS policy (what to do when late KOS has no same-KOS cover).
7. Do not move unless confirmed out (block swaps for selected players unless "OUT").
8. Never drop list (default = top 50 FPts, league-wide).

**League scope**
Rules and sidebar settings are saved per league. Swaps and claims only apply to the league you select.

**Page flow**
1. Select the Fantrax period (gameweek) to target.
2. Review the lineup feed and any status changes.
3. Make final lineup edits (optimized XI or one-off swap).
4. Review suggested conditional swaps and queue rules.

**Lineup confirmation sources**
- SofaScore is fastest and most consistent.
- Fantrax flags can lag; use as fallback.

**Terms**
- KOS = Kickoff Slot (1 = earliest). Same-KOS swaps share kickoff time.
- FX = Fantrax, SS = SofaScore.

**Auto lineup swaps logic**
- Rules fire on current active/reserve status at execution time.
- Priority is per active player; first legal swap wins.
- Fantrax legality (positions, locks, kickoff timing) is enforced at confirm.
- Optimization sets your XI; conditional swaps react after.

**Swap types and priority**
- Auto: system-generated.
- Recommended: suggested, user must queue these.
- Manual: user-created.
- Auto runs first; Recommended/Manual only run when Auto is off.

**Live Google Sheet fields (TDS/@Draftlad)**
- `TDS` (projGS) in lineup feed.
- `ProjFPts` and `ProjGS` in optimized XI and suggested swaps.
""".strip()

readme_dialog_md = """
**Heads up:** This page makes **live changes** to your Fantrax roster.

Please read the expanded "Read This First" section for the full app flow.
""".strip()

def _show_conditional_swaps_notice_inline() -> None:
    st.markdown(readme_dialog_md)
    st.checkbox("Don't remind me again this session", key="conditional_swaps_dont_remind")
    if st.button("Continue", type="primary"):
        st.session_state["conditional_swaps_notice_seen"] = True
        if st.session_state.get("conditional_swaps_dont_remind"):
            st.session_state["conditional_swaps_notice_suppressed"] = True
        st.rerun()

def _show_saved_rule_notice_inline() -> None:
    preview_lines = st.session_state.get("advanced_rule_preview_lines", [])
    st.success("Rule saved.")
    if preview_lines:
        st.markdown(
            "\n".join(f"{index + 1}. {line}" for index, line in enumerate(preview_lines))
        )
    st.button("Dismiss", key="dismiss_saved_rule_notice")

if not st.session_state.get("conditional_swaps_notice_seen") and not st.session_state.get(
    "conditional_swaps_notice_suppressed"
):
    with st.container(border=True):
        st.subheader("Read This First: Conditional Swaps are Live")
        _show_conditional_swaps_notice_inline()

with st.expander("Read This First (Live Fantrax Changes)", expanded=True):
    st.warning(
        "Actions on this page can change your Fantrax lineup immediately. "
        "Review the period and lineups carefully before applying swaps or optimized lineups."
    )
    st.markdown(readme_fields_md)


# ---------------------------------------------------------------------
# Session / auth helpers
# ---------------------------------------------------------------------
def _require_session() -> Optional["requests.Session"]:
    """
    Require a requests session from auth artifacts.
    """
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


def _mark_period_manual_override() -> None:
    """
    Track that the user explicitly changed the Fantrax period selector.
    """
    st.session_state["period_manual_override"] = True
    st.session_state["period_override_round"] = st.session_state.get("current_sofascore_round")


def _record_immediate_swap_event(
    *,
    action: str,
    out_player_id: str,
    in_player_id: str,
    period: Optional[int],
) -> None:
    events = st.session_state.get("conditional_immediate_swap_events")
    if not isinstance(events, list):
        events = []
    events.append(
        {
            "action": str(action),
            "out_player_id": str(out_player_id),
            "in_player_id": str(in_player_id),
            "period": str(period) if period is not None else "",
            "fired_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    st.session_state["conditional_immediate_swap_events"] = events


def _ensure_xsrf_header(session: Session) -> None:
    """
    Ensure an X-XSRF-TOKEN header is set in the session.
    """
    token = None
    for c in session.cookies:
        if c.name.upper().startswith("XSRF-TOKEN"):
            token = unquote(c.value or "")
            break
    if token:
        session.headers["X-XSRF-TOKEN"] = token


def _apply_fxpa_client_hints(session: Session) -> None:
    """
    Apply FXPA client hints to the session.
    """
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


def _safe_rerun() -> None:
    """
    Streamlit renamed experimental_rerun -> rerun; support either without breaking older versions.
    """
    fn = getattr(st, "experimental_rerun", None) or getattr(st, "rerun", None)
    if callable(fn):
        fn()


def _mark_never_drop_dirty() -> None:
    st.session_state["never_drop_dirty"] = True


def _rules_revision_key(user_id: str) -> str:
    return f"conditional_rules_revision::{user_id}"


def _load_rules_for_user_with_revision(user_id: str) -> List[Dict[str, Any]]:
    rules, _locks, meta = load_rules_for_user_state(str(user_id))
    try:
        rev = int((meta or {}).get("revision") or 0)
    except Exception:
        rev = 0
    st.session_state[_rules_revision_key(str(user_id))] = rev
    return list(rules or [])


def _apply_user_rule_ops(
    *,
    user_id: str,
    operations: List[Dict[str, Any]],
    retry_on_conflict: bool = True,
) -> Dict[str, Any]:
    revision_key = _rules_revision_key(str(user_id))
    expected_revision = st.session_state.get(revision_key)
    if expected_revision is None:
        try:
            _rules, _locks, meta = load_rules_for_user_state(str(user_id))
            expected_revision = int((meta or {}).get("revision") or 0)
        except Exception:
            expected_revision = 0
    actor_env = str(os.getenv("CONDITIONAL_WRITER_ENV", "unknown"))
    result = apply_rule_operations_for_user(
        str(user_id),
        operations,
        expected_revision=expected_revision,
        actor_env=actor_env,
    )
    st.session_state[revision_key] = int(result.get("revision_after") or expected_revision or 0)
    if result.get("conflict") and retry_on_conflict:
        result = apply_rule_operations_for_user(
            str(user_id),
            operations,
            expected_revision=int(result.get("revision_after") or 0),
            actor_env=actor_env,
        )
        st.session_state[revision_key] = int(result.get("revision_after") or st.session_state.get(revision_key) or 0)
    logger.info(
        "[rule-mutation] user=%s rev_before=%s rev_after=%s op_count=%s actor_env=%s conflict=%s changed=%s",
        user_id,
        result.get("revision_before"),
        result.get("revision_after"),
        len(operations or []),
        actor_env,
        result.get("conflict"),
        result.get("changed_count"),
    )
    return result


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

user_id = st.session_state.get("user_id")
state_writer_enabled = is_conditional_state_writer()
if not state_writer_enabled:
    st.warning(
        "Conditional rule state is running in read-only mode (`CONDITIONAL_STATE_ROLE=reader`). "
        "Save/toggle/delete actions are disabled in this environment."
    )
claims_allowed = str(league_id) == TEST_CLAIMS_LEAGUE_ID
global_status_path = global_status_path_for_user(str(user_id)) if user_id else global_status_path_for_user(None)
if user_id:
    user_mgr = UserManager()
    if hasattr(user_mgr, "is_auto_rules_enabled"):
        auto_swaps_enabled = user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
        auto_claims_enabled = user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "claims")
    else:
        auto_swaps_enabled = False
        auto_claims_enabled = False
    with st.sidebar:
        auto_swaps_toggle = st.toggle(
            "Auto lineup swaps",
            value=auto_swaps_enabled,
            help="Opt-in per league; automated conditional lineup swaps are disabled by default.",
            key="auto_rules_lineup_swaps_toggle",
        )
        auto_claims_toggle = st.toggle(
            "Auto claims/drops (disabled during testing)",
            value=False,
            help="Auto claims are disabled during testing; only manual claim rules are allowed.",
            key="auto_rules_claims_toggle",
            disabled=True,
        )
        show_claims_builder = st.toggle(
            "(beta) Show FA add/drop builder",
            value=st.session_state.get("show_claims_builder", True) if claims_allowed else False,
            help=(
                "Beta and limited to the test league. "
                "If you want this enabled for your league, DM @hogan_m on Discord with your league ID."
            ),
            key="show_claims_builder_toggle",
            disabled=not claims_allowed,
        )
        if not claims_allowed:
            st.caption("FA add/drop builder is locked to the test league for now.")
        st.session_state["show_claims_builder"] = show_claims_builder
    if auto_swaps_toggle != auto_swaps_enabled and hasattr(user_mgr, "set_auto_rules_enabled"):
        user_mgr.set_auto_rules_enabled(
            user_id=str(user_id),
            league_id=str(league_id),
            team_id=str(team_id),
            enabled=auto_swaps_toggle,
            feature="lineup_swaps",
        )
    # Auto claims are disabled during testing; no persistence for now.

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

subs_service = SubsService(session=session, league_id=league_id)
waivers_service = WaiversService(request_callable=api._request, api=api)

lineup_source_label = st.radio(
    "Lineup status source",
    options=[
        "Fantrax internal flags (default – reliable)",
        "SofaScore (faster but incomplete; opt-in)",
    ],
    index=0,
    help=(
        "Fantrax internal flags are the default source. "
        "SofaScore can be faster, but our backend collection is non-perfect, so it is opt-in only."
    ),
)
strategy = (
    LineupSourceStrategy.SOFASCORE_PRIMARY
    if lineup_source_label.startswith("SofaScore")
    else LineupSourceStrategy.FANTRAX_PRIMARY
)

# ---------------------------------------------------------------------
# Formatting / debug helpers
# ---------------------------------------------------------------------
def _format_status(status: LineupStatus) -> str:
    if not status:
        return "Unknown"
    return status.value.replace("_", " ").title()


def _format_binary_status(status: Optional[LineupStatus]) -> str:
    if status == LineupStatus.STARTING:
        return "STARTER"
    if status in (LineupStatus.BENCH, LineupStatus.OUT, LineupStatus.DOUBTFUL):
        return "NON STARTER"
    return "UNCONFIRMED"


def _format_kickoff(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%b %d %H:%M UTC")


def _status_code(val: Optional[LineupStatus]) -> int:
    if val == LineupStatus.STARTING:
        return 1
    if val in (LineupStatus.OUT, LineupStatus.DOUBTFUL):
        return -1
    return 0


def _kos_label_for_player(player_id: str, kos_map: dict[str, int]) -> str:
    idx = kos_map.get(str(player_id))
    return f"KOS {idx}" if idx else "KOS ?"


def _swap_type_label(
    active_id: str,
    backup_ids: List[str],
    kos_map: dict[str, int],
) -> str:
    active_kos = _kos_label_for_player(active_id, kos_map)
    if not backup_ids:
        return active_kos
    backup_kos = [_kos_label_for_player(pid, kos_map) for pid in backup_ids]
    return f"{active_kos} for " + " else ".join(backup_kos)


def _swap_description(
    active_name: str,
    backup_names: List[str],
    condition: Optional[str] = None,
) -> str:
    if not backup_names:
        return "No backups configured."
    if condition == SwapCondition.RESERVE_STARTING.value:
        if len(backup_names) == 1:
            return (
                f"If {backup_names[0]} (bench) is confirmed STARTER, move to active, "
                f"move {active_name} (active) to bench."
            )
        chain = " \u2192 ".join(backup_names)
        return (
            f"If a bench backup is confirmed STARTER, move to active, move {active_name} (active) to bench. "
            f"Backups in order: {chain}."
        )
    if len(backup_names) == 1:
        return (
            f"If {active_name} is confirmed NON STARTER, {backup_names[0]} swaps in "
            "(status checked near its kickoff)."
        )
    chain = " \u2192 ".join(backup_names)
    return (
        f"If {active_name} is confirmed NON STARTER, try backups in order as their "
        f"kickoff windows arrive: {chain}."
    )


def _fa_projection_row(name: Optional[str], team: Optional[str], projections: dict) -> Optional[dict]:
    if not projections or not name:
        return None
    name_key = _normalize_player_name(name)
    if not name_key:
        return None
    team_key = _canonical_team_code(team)
    return projections.get((name_key, team_key)) or projections.get((name_key, ""))


def _open_roster_slots(roster) -> tuple[list[RosterRow], list[RosterRow]]:
    open_active: list[RosterRow] = []
    open_reserve: list[RosterRow] = []
    for row in roster.rows:
        if getattr(row, "player", None):
            continue
        pos_id = str(getattr(row, "pos_id", "0"))
        if pos_id != "0":
            open_active.append(row)
        else:
            open_reserve.append(row)
    return open_active, open_reserve


def _slot_label(row: RosterRow) -> str:
    pos = _display_pos(row) or "Slot"
    pos_id = getattr(row, "pos_id", "")
    return f"{pos} slot (pos_id {pos_id})"


def _top_fpts_ids_cached(
    waivers_service: WaiversService,
    *,
    league_id: str,
    limit: int = 50,
    ttl_minutes: int = 30,
) -> set[str]:
    cache = st.session_state.get("top_fpts_cache") or {}
    now = datetime.now(timezone.utc)
    cached_league = str(cache.get("league_id") or "")
    if cached_league == str(league_id):
        fetched_at = cache.get("fetched_at")
        if fetched_at:
            try:
                ts = datetime.fromisoformat(str(fetched_at))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = None
            if ts and (now - ts) < timedelta(minutes=ttl_minutes):
                return set(str(pid) for pid in (cache.get("ids") or []))

    try:
        top_players = waivers_service.list_top_players_by_fpts(limit=limit, status="ALL")
    except Exception as exc:
        logger.info("Failed to load top FPts list: %s", exc)
        return set()
    ids = [str(p.get("id")) for p in top_players if p.get("id")]
    st.session_state["top_fpts_cache"] = {
        "league_id": str(league_id),
        "ids": ids,
        "fetched_at": now.isoformat(),
    }
    return set(ids)


def _default_never_drop_ids(
    roster,
    top_fpts_ids: Optional[set[str]],
) -> list[str]:
    if not top_fpts_ids:
        return []
    roster_ids: list[str] = []
    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue
        roster_ids.append(str(player.id))
    return [pid for pid in roster_ids if pid in top_fpts_ids]


def _partition_backups(
    backup_ids: List[str],
    roster_view: RosterView,
) -> tuple[List[str], List[str]]:
    eligible: List[str] = []
    ineligible: List[str] = []
    for bid in backup_ids:
        if roster_view.is_reserve(bid):
            eligible.append(bid)
        else:
            ineligible.append(bid)
    return eligible, ineligible


def _is_manual_rule(rule: dict) -> bool:
    if rule.get("source_type") == 3:
        return True
    return str(rule.get("source") or "").lower() == "manual"


def _normalized_rule_state(value: Optional[str]) -> str:
    state = str(value or "").lower()
    if state in {"disabled", "inactive", "off"}:
        return "disabled"
    if state == "fired":
        return "fired"
    return "active"


def _display_pos(row: RosterRow) -> str:
    try:
        return SubsService._pos_of_row(row)  # type: ignore[attr-defined]
    except Exception:
        pass
    for attr in ("short_name", "position_short", "positionShort", "position"):
        if hasattr(getattr(row, "pos", None), attr):
            val = getattr(row.pos, attr, "")
            if val:
                raw = str(val).strip()
                if raw.upper() not in {"RES", "R", "BENCH"}:
                    return raw
    player = getattr(row, "player", None)
    for attr in ("position_short", "positionShort", "position"):
        if hasattr(player, attr):
            val = getattr(player, attr, "")
            if val:
                return str(val)
    return ""


def _format_player_label(name: str, pos: Optional[str]) -> str:
    pos_clean = (pos or "").strip()
    return f"{name} ({pos_clean})" if pos_clean else name


def _build_swap_legality_debug(
    *,
    roster,
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    subs_service: SubsService,
    league_id: str,
    team_id: str,
    swap_period_int: Optional[int],
    active_candidates: List[dict],
    reserve_candidates: List[dict],
) -> List[dict]:
    rows: List[dict] = []
    if swap_period_int is None:
        return rows
    for bad in active_candidates:
        for good in reserve_candidates:
            reserve_ok = roster_view.is_reserve(good["pid"])
            row = roster_view.get_row(good["pid"])
            lock_flags = {}
            if row:
                lock_flags = get_row_lock_flags(
                    row,
                    lineup_info_by_player=lineup_info_by_player,
                )
            locked_now = bool(lock_flags.get("visually_locked")) if lock_flags else False
            res = test_swap_in_period(
                subs_service=subs_service,
                roster=roster,
                league_id=league_id,
                team_id=team_id,
                active_id=bad["pid"],
                reserve_id=good["pid"],
                period_id=swap_period_int,
            )
            warning_note = ""
            confirm = res.get("confirm") or {}
            model = confirm.get("model") or {}
            try:
                first_illegal = model.get("firstIllegalRosterPeriod")
                if first_illegal is not None and int(first_illegal) > int(swap_period_int):
                    warning_note = f"future_period_warning (GW {first_illegal})"
            except Exception:
                warning_note = ""
            illegal_msgs = res.get("illegal_msgs") or []
            warnings = res.get("warnings") or []
            result = "ok" if res.get("ok") else "blocked"
            if res.get("ok") and warnings:
                result = "ok_with_warning"
            if not reserve_ok:
                result = "not_reserve"
            elif locked_now:
                result = "locked_or_played"
            rows.append(
                {
                    "Active": bad.get("name") or bad.get("pid"),
                    "Reserve": good.get("name") or good.get("pid"),
                    "Result": result,
                    "Reserve now": "yes" if reserve_ok else "no",
                    "Locked now": "yes" if locked_now else "no",
                    "Reason": res.get("reason"),
                    "Illegal": "; ".join(str(m) for m in illegal_msgs if m),
                    "Warnings": "; ".join(str(m) for m in warnings if m),
                    "Warning Type": warning_note,
                }
            )
    return rows


def _get_api_football_key() -> Optional[str]:
    try:
        return st.secrets.get("api_football_key")  # type: ignore[attr-defined]
    except Exception:
        pass
    return os.environ.get("API_FOOTBALL_KEY")


def _normalize_team_name(name: str) -> str:
    norm = (name or "").lower()
    try:
        norm = TEAM_NAME_ALIASES.get(norm, norm)
    except Exception:
        pass
    for token in ("fc", "afc", "cf", "sc", "fc.", "afc.", "cf.", " sc", " fc"):
        norm = norm.replace(token, "")
    return "".join(ch for ch in norm if ch.isalnum())


def _odds_score(info: Optional[PlayerLineupInfo], odds_map: dict) -> Optional[float]:
    """
    Derive an odds score for a player based on event odds in session state.
    Expects odds_map format:
    {
        <event_id>: {
            "home": {"expected": <num>, "actual": <num>, ...},
            "away": {"expected": <num>, "actual": <num>, ...},
        },
        ...
    }
    """
    if not info:
        return None
    event_id = getattr(info, "event_id", None)
    if not event_id:
        return None
    key = str(event_id)
    odds = odds_map.get(key) or odds_map.get(event_id) or {}
    side = "home" if getattr(info, "is_home", None) is not False else "away"
    side_odds = odds.get(side) or {}
    for k in ("actual", "expected"):
        try:
            if side_odds.get(k) is not None:
                return float(side_odds.get(k))
        except Exception:
            continue
    return None


def _odds_score(info: Optional[PlayerLineupInfo], odds_map: dict) -> Optional[float]:
    """
    Derive an odds score for a player based on event odds in session state.
    Expects odds_map format:
    {
        <event_id>: {
            "home": {"expected": <num>, "actual": <num>, ...},
            "away": {"expected": <num>, "actual": <num>, ...},
        },
        ...
    }
    """
    if not info:
        return None
    event_id = getattr(info, "event_id", None)
    if not event_id:
        return None
    key = str(event_id)
    odds = odds_map.get(key) or odds_map.get(event_id) or {}
    side = "home" if getattr(info, "is_home", None) is not False else "away"
    side_odds = odds.get(side) or {}
    for k in ("actual", "expected"):
        try:
            if side_odds.get(k) is not None:
                return float(side_odds.get(k))
        except Exception:
            continue
    return None


TEAM_STRENGTH_PATH = DEFAULT_STRENGTH_PATH


def _load_team_strength_map(path: Path = TEAM_STRENGTH_PATH) -> dict:
    try:
        return load_strengths(path)
    except Exception:
        return {}


def _team_strength(info: Optional[PlayerLineupInfo], strength_map: dict) -> Optional[float]:
    if not info or not strength_map:
        return None
    team_name = getattr(info, "team_name", None)
    team_code = None
    try:
        team_code = _team_code(team_name) if callable(_team_code) else None
    except Exception:
        team_code = None
    season_hint = None
    try:
        season_hint = getattr(info, "season_id", None)
    except Exception:
        season_hint = None
    return strength_for_team(
        team_name=team_name,
        team_code=team_code,
        season=season_hint,
        strengths=strength_map,
    )


PROJECTIONS_PATH = Path("data/derived/projections.parquet")
PROJECTIONS_SHEET_URL = os.environ.get(
    "PROJECTIONS_SHEET_URL",
    "https://docs.google.com/spreadsheets/d/13jcGcFBvJ4sU811elZ7psuvxTejuifsUX7L0PmjLKLg/",
)
PROJECTIONS_SHEET_WORKSHEET = os.environ.get("PROJECTIONS_SHEET_WORKSHEET")
SERVICE_ACCOUNT_DEFAULT_PATH = Path("config/service_account.json")
TEAM_MAPPINGS_PATH = Path("config/team_mappings.yaml")
CLUB_MAPPINGS_PATH = Path("config/club_team_mappings.yaml")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


PROJECTIONS_REFRESH_TTL_MINUTES = max(1, _env_int("PROJECTIONS_REFRESH_TTL_MINUTES", 5))
PROJECTIONS_STALE_WARNING_MINUTES = max(
    PROJECTIONS_REFRESH_TTL_MINUTES, _env_int("PROJECTIONS_STALE_WARNING_MINUTES", 120)
)


def _normalize_player_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    # Strip accents and keep alphanumerics only for loose matching
    normalized = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in normalized.lower() if ch.isalnum())


def _canonical_team_code(raw: Optional[str]) -> str:
    """
    Map arbitrary team string to Fantrax 3-letter code using config mappings.
    Falls back to uppercased alnum string.
    """
    if raw is None:
        return ""
    base = "".join(ch for ch in str(raw).lower() if ch.isalnum())
    if not base:
        return ""
    # Load mappings once
    if "team_code_map" not in st.session_state:
        st.session_state["team_code_map"] = _load_team_code_map()
    mapping: dict = st.session_state.get("team_code_map", {})
    if base in mapping:
        return mapping[base]
    return base.upper()


def _load_team_code_map() -> dict:
    """
    Build variation -> standard code map from config files.
    """
    variation_map: dict[str, str] = {}
    # team_mappings.yaml
    try:
        with TEAM_MAPPINGS_PATH.open() as fh:
            code_cfg = yaml.safe_load(fh) or {}
            for std, data in code_cfg.items():
                std_code = std.strip().upper()
                variation_map["".join(ch for ch in std_code.lower() if ch.isalnum())] = std_code
                for var in data.get("variations", []):
                    key = "".join(ch for ch in str(var).lower() if ch.isalnum())
                    if key:
                        variation_map[key] = std_code
    except Exception:
        pass
    # club_team_mappings.yaml
    try:
        with CLUB_MAPPINGS_PATH.open() as fh:
            club_cfg = yaml.safe_load(fh) or {}
            for std, data in club_cfg.items():
                std_code = str(data.get("short_name") or std).strip().upper()
                keys = [std, data.get("long_name"), data.get("short_name")]
                keys += data.get("long_name_variations", []) or []
                keys += data.get("short_name_variations", []) or []
                keys += data.get("nicknames", []) or []
                for k in keys:
                    if not k:
                        continue
                    key = "".join(ch for ch in str(k).lower() if ch.isalnum())
                    if key:
                        variation_map[key] = std_code
    except Exception:
        pass
    # Overrides for known Fantrax codes
    overrides = {
        "nfo": "NOT",
        "nottinghamforest": "NOT",
        "forest": "NOT",
        "not": "NOT",
    }
    variation_map.update(overrides)
    return variation_map


def _service_account_path() -> Path:
    env_path = os.environ.get("SERVICE_ACCOUNT_JSON")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p.resolve()
    # Resolve robustly even when Streamlit CWD is not repo root.
    here = Path(__file__).resolve()
    candidates = [
        SERVICE_ACCOUNT_DEFAULT_PATH,
        here.parents[3] / SERVICE_ACCOUNT_DEFAULT_PATH,  # project root
        Path.cwd() / SERVICE_ACCOUNT_DEFAULT_PATH,
    ]
    for candidate in candidates:
        c = candidate.expanduser()
        if c.exists():
            return c.resolve()
    # Return default resolved-to-cwd path for clearer logging.
    return (Path.cwd() / SERVICE_ACCOUNT_DEFAULT_PATH).resolve()


def _set_projection_metadata(source: str, updated_at: Optional[datetime] = None) -> None:
    try:
        now = datetime.now(timezone.utc)
        source_ts = (updated_at or now).astimezone(timezone.utc).isoformat()
        st.session_state["projections_meta"] = {
            "source": source,
            "updated_at": source_ts,
            "fetched_at": now.isoformat(),
        }
    except Exception:
        return


def _record_projection_sync_status(
    *,
    status: str,
    detail: Optional[str] = None,
) -> None:
    try:
        st.session_state["projections_sync_status"] = {
            "status": status,
            "detail": detail or "",
            "attempted_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return


def _projection_sync_status_message() -> Optional[tuple[str, str]]:
    raw = st.session_state.get("projections_sync_status")
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "")
    detail = str(raw.get("detail") or "")
    attempted_at = _parse_iso_utc(raw.get("attempted_at"))
    at_label = attempted_at.strftime("%Y-%m-%d %H:%M UTC") if attempted_at else "unknown time"
    if status == "google_sheet_ok":
        return ("success", f"Projection sync OK ({at_label}): loaded from Google Sheet.")
    if status == "cache_fallback":
        suffix = f" Reason: {detail}" if detail else ""
        return ("warning", f"Projection sync used parquet cache ({at_label}).{suffix}")
    if status == "service_account_missing":
        reason = detail or "service account key path not found in runtime environment"
        return ("warning", f"Google Sheet sync unavailable ({at_label}). Missing service account key at: {reason}")
    if status == "sheet_unavailable":
        reason = detail or "gspread dependency missing"
        return ("warning", f"Google Sheet sync unavailable ({at_label}). {reason}")
    if status == "google_sheet_error":
        reason = detail or "unknown Google API error"
        return ("warning", f"Google Sheet sync failed ({at_label}). {reason}")
    if status:
        suffix = f" Reason: {detail}" if detail else ""
        return ("error", f"Projection sync failed ({at_label}).{suffix}")
    return None


def _gspread_import_status() -> tuple[bool, str]:
    try:
        import gspread  # type: ignore

        return True, f"ok (v{getattr(gspread, '__version__', 'unknown')})"
    except Exception as exc:
        return False, str(exc)


def _projection_diagnostics_payload() -> dict[str, Any]:
    key_path = _service_account_path()
    gspread_ok, gspread_detail = _gspread_import_status()
    return {
        "active_sys_executable": sys.executable,
        "service_account_path": str(key_path),
        "service_account_exists": key_path.exists(),
        "gspread_import_ok": gspread_ok,
        "gspread_import_detail": gspread_detail,
        "sheet_url": PROJECTIONS_SHEET_URL,
        "sheet_worksheet": PROJECTIONS_SHEET_WORKSHEET or "(sheet1 default)",
        "last_google_fetch_exception": st.session_state.get("projections_google_last_exception"),
    }


def _parse_iso_utc(raw_ts: Optional[str]) -> Optional[datetime]:
    if not raw_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(raw_ts))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _projection_last_updated_label() -> Optional[str]:
    meta = st.session_state.get("projections_meta")
    if not isinstance(meta, dict):
        return None
    raw_ts = meta.get("updated_at")
    source = str(meta.get("source") or "unknown")
    ts = _parse_iso_utc(str(raw_ts) if raw_ts else None)
    if not ts:
        return None
    source_label = {
        "google_sheet": "Google Sheet",
        "cache_parquet": "Parquet cache",
    }.get(source, source)
    return f"{ts.strftime('%Y-%m-%d %H:%M UTC')} ({source_label})"


def _projection_stale_message(now: Optional[datetime] = None) -> Optional[str]:
    meta = st.session_state.get("projections_meta")
    if not isinstance(meta, dict):
        return "Projection freshness unknown (missing metadata)."
    source = str(meta.get("source") or "unknown")
    source_ts = _parse_iso_utc(meta.get("updated_at"))
    if not source_ts:
        return "Projection freshness unknown (invalid last-updated timestamp)."
    current = now or datetime.now(timezone.utc)
    age_minutes = int((current - source_ts).total_seconds() // 60)
    if age_minutes <= PROJECTIONS_STALE_WARNING_MINUTES:
        return None
    source_label = "Google Sheet" if source == "google_sheet" else "Parquet cache" if source == "cache_parquet" else source
    return (
        f"Projections may be stale: last update {age_minutes} min ago from {source_label} "
        f"(threshold {PROJECTIONS_STALE_WARNING_MINUTES} min)."
    )


def _get_projections_map(*, force_refresh: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    projections_map = st.session_state.get("projections_map")
    fetched_at = _parse_iso_utc(st.session_state.get("projections_map_fetched_at"))
    refresh_due = fetched_at is None or (now - fetched_at) >= timedelta(minutes=PROJECTIONS_REFRESH_TTL_MINUTES)
    if force_refresh or projections_map is None or refresh_due:
        projections_map = _load_projections()
        st.session_state["projections_map"] = projections_map
        st.session_state["projections_map_fetched_at"] = now.isoformat()
    return projections_map or {}


def _normalize_projection_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "projfpts": "ProjFPts",
        "projgpts": "ProjGPts",
        "%rost": "Ros%",
        "rost%": "Ros%",
        "projmins": "ProjMins",
    }
    return df.rename(columns={col: rename_map.get(col.lower(), col) for col in df.columns})


def _fetch_projections_from_sheet() -> Optional[pd.DataFrame]:
    """
    Pull projections directly from the shared Google Sheet using the service account.
    """
    if not PROJECTIONS_SHEET_URL:
        return None
    try:
        import gspread
    except Exception as exc:
        logger.info("gspread unavailable; skipping projections sheet fetch: %s", exc)
        st.session_state["projections_google_last_exception"] = str(exc)
        _record_projection_sync_status(status="sheet_unavailable", detail=str(exc))
        return None

    key_path = _service_account_path()
    if not key_path.exists():
        logger.info("Service account key missing at %s; skipping sheet fetch", key_path)
        st.session_state["projections_google_last_exception"] = f"service account missing: {key_path.resolve()}"
        _record_projection_sync_status(status="service_account_missing", detail=str(key_path.resolve()))
        return None

    try:
        client = gspread.service_account(filename=str(key_path))
        sheet = client.open_by_url(PROJECTIONS_SHEET_URL)
        ws = sheet.worksheet(PROJECTIONS_SHEET_WORKSHEET) if PROJECTIONS_SHEET_WORKSHEET else sheet.sheet1
        df = _normalize_projection_columns(pd.DataFrame(ws.get_all_records()))
        if df.empty:
            raise ValueError("projections sheet returned no rows")
        logger.info("Loaded %s projection rows from Google Sheet", len(df))
        st.session_state["projections_google_last_exception"] = None
        _set_projection_metadata("google_sheet")
        _record_projection_sync_status(status="google_sheet_ok", detail=f"{len(df)} rows")
        try:
            PROJECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(PROJECTIONS_PATH, index=False)
        except Exception as cache_exc:
            logger.info("Unable to cache projections to %s: %s", PROJECTIONS_PATH, cache_exc)
        return df
    except Exception as exc:
        logger.warning("Failed to load projections from Google Sheet: %s", exc)
        st.session_state["projections_google_last_exception"] = str(exc)
        _record_projection_sync_status(status="google_sheet_error", detail=str(exc))
        return None


def _load_projections(path: Path = PROJECTIONS_PATH) -> dict:
    """
    Return a mapping keyed by (player_name, team_code) -> projection row.
    """
    df: Optional[pd.DataFrame] = _fetch_projections_from_sheet()
    if df is None:
        try:
            df = pd.read_parquet(path)
            logger.info("Loaded projections from cache at %s", path)
            df = _normalize_projection_columns(df)
            _set_projection_metadata(
                "cache_parquet", datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            )
            if str((st.session_state.get("projections_sync_status") or {}).get("status")) != "google_sheet_ok":
                detail = str((st.session_state.get("projections_sync_status") or {}).get("detail") or "")
                _record_projection_sync_status(status="cache_fallback", detail=detail)
        except Exception as exc:
            logger.warning("Unable to load projections from sheet or cache: %s", exc)
            st.warning("Projections unavailable (Google Sheet fetch failed and no cached file found).")
            _record_projection_sync_status(status="projection_load_failed", detail=str(exc))
            return {}
    proj_map: dict[tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        name_key = _normalize_player_name(row.get("Player"))
        team_key = _canonical_team_code(row.get("Team"))
        if name_key:
            proj_map[(name_key, team_key)] = row.to_dict()
            # also allow name-only lookup
            proj_map[(name_key, "")] = row.to_dict()
    return proj_map


def _load_projections_df(path: Path = PROJECTIONS_PATH) -> Optional[pd.DataFrame]:
    """
    Return the projections dataframe for top-N comparisons.
    """
    df: Optional[pd.DataFrame] = _fetch_projections_from_sheet()
    if df is None:
        try:
            df = pd.read_parquet(path)
            df = _normalize_projection_columns(df)
            _set_projection_metadata(
                "cache_parquet", datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            )
        except Exception:
            return None
    return df


def _player_projection(
    info: Optional[PlayerLineupInfo],
    *,
    projections: dict,
    fallback_name: Optional[str] = None,
) -> Optional[float]:
    row = _player_projection_row(info, projections=projections, fallback_name=fallback_name)
    if not row:
        return None
    try:
        return float(row.get("ProjFPts"))
    except Exception:
        return None


def _player_projection_row(
    info: Optional[PlayerLineupInfo],
    *,
    projections: dict,
    fallback_name: Optional[str] = None,
) -> Optional[dict]:
    if not projections:
        return None
    name = fallback_name or getattr(info, "player_name", None)
    if not name and info:
        name = getattr(info, "team_name", None)
    name_key = _normalize_player_name(name)
    if not name_key:
        return None
    team_code_norm = ""
    if info:
        try:
            tname = getattr(info, "team_name", None)
            code = _team_code(tname) if callable(_team_code) else tname
            team_code_norm = _canonical_team_code(code)
        except Exception:
            team_code_norm = ""

    row = projections.get((name_key, team_code_norm)) or projections.get((name_key, ""))
    if not row:
        return None
    return row


def _has_projection_row(
    info: Optional[PlayerLineupInfo],
    *,
    projections: dict,
    fallback_name: Optional[str] = None,
) -> bool:
    return _player_projection_row(info, projections=projections, fallback_name=fallback_name) is not None


def _confirmed_status(info: Optional[PlayerLineupInfo]) -> Optional[LineupStatus]:
    if not info:
        return None
    return getattr(info, "ss_conf_status", None)


def _is_confirmed_starting(info: Optional[PlayerLineupInfo]) -> bool:
    return _confirmed_status(info) == LineupStatus.STARTING


def _is_confirmed_not_starting(info: Optional[PlayerLineupInfo]) -> bool:
    return _confirmed_status(info) in {LineupStatus.BENCH, LineupStatus.OUT, LineupStatus.DOUBTFUL}


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
    if trimmed.startswith("@"):
        trimmed = trimmed[1:].strip()
    if not trimmed:
        return "-"
    if len(trimmed) <= 4 and trimmed.replace(" ", "").isalpha():
        return trimmed.upper()
    return trimmed[:3].upper()


@st.cache_data(show_spinner=False)
def _load_team_logo_map(path: Path = Path("players.csv")) -> dict[str, str]:
    if not path.exists():
        return {}
    logo_map: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                logo = (row.get("headshot_url") or "").strip()
                if not logo:
                    continue
                team_name = (row.get("team_name") or "").strip()
                team_short = (row.get("team_short_name") or "").strip()
                if team_name and team_name not in logo_map:
                    logo_map[team_name] = logo
                if team_short and team_short not in logo_map:
                    logo_map[team_short] = logo
    except Exception as exc:
        logger.info("Unable to load team logo map: %s", exc)
    return logo_map


def _team_logo_url(team_name: Optional[str], logo_map: dict[str, str]) -> Optional[str]:
    if not team_name:
        return None
    if team_name in logo_map:
        return logo_map[team_name]
    alias = TEAM_NAME_ALIASES.get(str(team_name).strip().lower())
    if alias and alias in logo_map:
        return logo_map[alias]
    try:
        code = _team_code(team_name) if callable(_team_code) else None
    except Exception:
        code = None
    if code and code in logo_map:
        return logo_map[code]
    display = _team_code_for_display(team_name)
    return logo_map.get(display)


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
    # Fallback to the index CSV if explicit fetched_at_utc is missing on the JSON records
    index_path = lineups_dir.parent / "lineups_index.csv"
    if latest is None and index_path.exists():
        try:
            import pandas as pd  # local import to avoid unnecessary dependency at module load

            df = pd.read_csv(index_path)
            if "saved_at_utc" in df.columns and not df["saved_at_utc"].empty:
                candidate = df["saved_at_utc"].dropna().max()
                ts = _parse_fetch_timestamp(candidate)
                if ts:
                    latest = ts
        except Exception:
            pass
    return latest


def _build_kos_index_map(
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
) -> tuple[dict[str, int], Optional[int]]:
    kickoff_times = sorted({info.kickoff for info in lineup_info_by_player.values() if info and info.kickoff})
    kickoff_to_index = {ko: idx + 1 for idx, ko in enumerate(kickoff_times)}
    kos_map: dict[str, int] = {}
    for pid, info in lineup_info_by_player.items():
        if info and info.kickoff in kickoff_to_index:
            kos_map[str(pid)] = kickoff_to_index[info.kickoff]
    last_kos_index = max(kos_map.values(), default=None)
    return kos_map, last_kos_index


def _build_gameweek_meta(
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    gw_label: str,
) -> Optional[dict]:
    kickoffs = sorted({info.kickoff for info in lineup_info_by_player.values() if info and info.kickoff})
    if not kickoffs:
        return None
    kickoff_to_index = {ko: idx + 1 for idx, ko in enumerate(kickoffs)}
    days: dict[datetime.date, list[datetime]] = {}
    matches_by_kickoff: dict[datetime, dict[str, dict]] = {}
    for ko in kickoffs:
        days.setdefault(ko.date(), []).append(ko)
    for info in lineup_info_by_player.values():
        if not info or not info.kickoff:
            continue
        kickoff = info.kickoff
        team = getattr(info, "team_name", None)
        opp = getattr(info, "opponent_name", None)
        if not team or not opp:
            continue
        is_home = getattr(info, "is_home", None)
        if is_home is True:
            home_team, away_team = team, opp
        elif is_home is False:
            home_team, away_team = opp, team
        else:
            home_team, away_team = team, opp
        event_id = getattr(info, "event_id", None)
        key = f"event:{event_id}" if event_id is not None else f"teams:{home_team}|{away_team}"
        matches_by_kickoff.setdefault(kickoff, {})[key] = {
            "home": str(home_team),
            "away": str(away_team),
            "event_id": event_id,
        }
    gw_payload: dict[str, dict] = {
        gw_label: {
            "days": len(days),
            "kickoff_slots": len(kickoffs),
        }
    }
    day_index_map = {day_key: idx for idx, day_key in enumerate(sorted(days.keys()), start=1)}
    for day_key in sorted(days.keys()):
        day_idx = day_index_map[day_key]
        md_key = f"MD {day_idx}"
        day_entries: dict[str, dict] = {}
        for ko in sorted(days[day_key]):
            kos_idx = kickoff_to_index.get(ko)
            label = f"KOS {kos_idx}" if kos_idx else "KOS ?"
            matches = list(matches_by_kickoff.get(ko, {}).values())
            matches.sort(key=lambda m: str(m.get("home") or "").lower())
            for idx, match in enumerate(matches):
                letter = chr(ord("a") + idx) if idx < 26 else str(idx + 1)
                match["match_id"] = f"{day_idx}_{kos_idx or '?'}{letter}"
            count = len(matches)
            day_entries[label] = {
                "kickoff": ko.strftime("%Y-%m-%d %H:%M UTC"),
                "count": count,
                "matches": matches,
            }
        gw_payload[gw_label][md_key] = day_entries
    return gw_payload


def _load_schedule_events_from_cache(
    inferred_round: Optional[str],
) -> Tuple[List[dict], Optional[str]]:
    if not inferred_round:
        return [], None
    schedule_dir = Path("data/sofascore/schedules")
    if not schedule_dir.exists():
        return [], None
    tournament_id = st.session_state.get("sofascore_tournament_id", 17)
    target_round = str(inferred_round)
    season_hint = st.session_state.get("sofascore_season_id")

    def _read_for_season(season_id: str) -> List[dict]:
        events: List[dict] = []
        seen: set[int] = set()
        for mode in ("upcoming", "last"):
            path = schedule_dir / f"{tournament_id}_{season_id}_{mode}.csv"
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if str(row.get("round", "")) != target_round:
                            continue
                        kickoff = parse_kickoff_dt(row.get("kickoff_utc"))
                        if not kickoff:
                            continue
                        try:
                            event_id = int(row.get("event_id") or 0)
                        except Exception:
                            continue
                        if not event_id or event_id in seen:
                            continue
                        seen.add(event_id)
                        events.append(
                            {
                                "event_id": event_id,
                                "kickoff": kickoff,
                                "home_team": row.get("home_team"),
                                "away_team": row.get("away_team"),
                            }
                        )
            except Exception:
                continue
        return events

    candidates: List[Tuple[str, List[dict], float]] = []
    if season_hint:
        events = _read_for_season(str(season_hint))
        if events:
            try:
                path = schedule_dir / f"{tournament_id}_{season_hint}_upcoming.csv"
                mtime = path.stat().st_mtime if path.exists() else 0.0
            except Exception:
                mtime = 0.0
            candidates.append((str(season_hint), events, mtime))

    for path in schedule_dir.glob(f"{tournament_id}_*_upcoming.csv"):
        parts = path.name.split("_")
        if len(parts) < 3:
            continue
        season_id = parts[1]
        if season_hint and season_id == str(season_hint):
            continue
        events = _read_for_season(season_id)
        if not events:
            continue
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        candidates.append((season_id, events, mtime))

    if not candidates:
        return [], None
    candidates.sort(key=lambda item: (len(item[1]), item[2]), reverse=True)
    season_id, events, _mtime = candidates[0]
    st.session_state["sofascore_season_id"] = season_id
    return events, f"cache:{tournament_id}_{season_id}"


def _fetch_gameweek_events(inferred_round: Optional[str]) -> Tuple[List[dict], Optional[str]]:
    if not inferred_round:
        return [], None
    try:
        tournament_id = st.session_state.get("sofascore_tournament_id", 17)
        expected_matches = 10 if int(tournament_id) == 17 else None
        cached_events, cache_source = _load_schedule_events_from_cache(inferred_round)
        if cached_events and (expected_matches is None or len(cached_events) >= expected_matches):
            return cached_events, cache_source
        season_id = st.session_state.get("sofascore_season_id")
        if not season_id:
            try:
                seasons = raw_get_seasons(tournament_id)
                season_id = choose_season_from_list(seasons, None)
                st.session_state["sofascore_season_id"] = season_id
            except Exception:
                season_id = None
        if not season_id:
            return cached_events, cache_source

        events: List[dict] = []
        seen: set[int] = set()
        target_round = str(inferred_round)

        def _collect(upcoming: bool) -> None:
            for ev in raw_iter_tournament_events(tournament_id, season_id or 0, upcoming):
                round_val = (ev.get("roundInfo") or {}).get("round")
                if round_val is None:
                    continue
                round_str = str(round_val)
                if round_str != target_round:
                    continue
                kickoff = parse_kickoff_dt(ev.get("startTimestamp") or ev.get("startTime"))
                if not kickoff:
                    continue
                try:
                    event_id = int(ev.get("id"))
                except Exception:
                    continue
                if event_id in seen:
                    continue
                seen.add(event_id)
                home_team = (ev.get("homeTeam") or {}).get("name") or (ev.get("homeTeam") or {}).get(
                    "shortName"
                )
                away_team = (ev.get("awayTeam") or {}).get("name") or (ev.get("awayTeam") or {}).get(
                    "shortName"
                )
                events.append(
                    {
                        "event_id": event_id,
                        "kickoff": kickoff,
                        "home_team": home_team,
                        "away_team": away_team,
                    }
                )

        _collect(False)
        _collect(True)
        if cached_events:
            merged = {ev["event_id"]: ev for ev in cached_events}
            for ev in events:
                existing = merged.get(ev["event_id"])
                if not existing:
                    merged[ev["event_id"]] = ev
                    continue
                for key, value in ev.items():
                    if value and not existing.get(key):
                        existing[key] = value
            events = list(merged.values())
        source = "live"
        if cache_source and cached_events:
            source = "cache+live"
        return events, source
    except Exception as exc:
        logger.info("[gw-meta] schedule fetch failed: %s", exc)
        return [], None


def _build_gameweek_meta_from_events(events: List[dict], gw_label: str) -> Optional[dict]:
    kickoffs = sorted({ev.get("kickoff") for ev in events if ev.get("kickoff")})
    if not kickoffs:
        return None
    kickoff_to_index = {ko: idx + 1 for idx, ko in enumerate(kickoffs)}
    days: dict[datetime.date, list[datetime]] = {}
    events_by_kickoff: dict[datetime, list[dict]] = {}
    for ev in events:
        kickoff = ev.get("kickoff")
        if not kickoff:
            continue
        days.setdefault(kickoff.date(), []).append(kickoff)
        events_by_kickoff.setdefault(kickoff, []).append(ev)
    gw_payload: dict[str, dict] = {
        gw_label: {
            "days": len(days),
            "kickoff_slots": len(kickoffs),
        }
    }
    day_index_map = {day_key: idx for idx, day_key in enumerate(sorted(days.keys()), start=1)}
    for day_key in sorted(days.keys()):
        day_idx = day_index_map[day_key]
        md_key = f"MD {day_idx}"
        day_entries: dict[str, dict] = {}
        for ko in sorted(days[day_key]):
            kos_idx = kickoff_to_index.get(ko)
            label = f"KOS {kos_idx}" if kos_idx else "KOS ?"
            seen_keys: set[str] = set()
            matches = []
            for ev in events_by_kickoff.get(ko, []):
                event_id = ev.get("event_id")
                home_team = (ev.get("home_team") or "").strip()
                away_team = (ev.get("away_team") or "").strip()
                match_key = f"event:{event_id}" if event_id is not None else f"teams:{home_team}|{away_team}"
                if match_key in seen_keys:
                    continue
                seen_keys.add(match_key)
                matches.append(
                    {
                        "home": home_team or "-",
                        "away": away_team or "-",
                        "event_id": event_id,
                    }
                )
            matches.sort(key=lambda m: str(m.get("home") or "").lower())
            for idx, match in enumerate(matches):
                letter = chr(ord("a") + idx) if idx < 26 else str(idx + 1)
                match["match_id"] = f"{day_idx}_{kos_idx or '?'}{letter}"
            count = len(matches)
            day_entries[label] = {
                "kickoff": ko.strftime("%Y-%m-%d %H:%M UTC"),
                "count": count,
                "matches": matches,
            }
        gw_payload[gw_label][md_key] = day_entries
    return gw_payload


def _render_gameweek_overview_table(gw_meta: dict, gw_label: str) -> None:
    payload = gw_meta.get(gw_label)
    if not isinstance(payload, dict):
        return
    logo_map = _load_team_logo_map()
    rows: list[dict] = []
    for md_key, day_entries in payload.items():
        if md_key in {"days", "kickoff_slots"}:
            continue
        if not isinstance(day_entries, dict):
            continue
        for kos_label, slot in day_entries.items():
            if not isinstance(slot, dict):
                continue
            kickoff = slot.get("kickoff") or ""
            matches = slot.get("matches") or []
            try:
                kos_index = int(str(kos_label).split()[-1])
            except Exception:
                kos_index = 999
            rows.append(
                {
                    "kos_label": kos_label,
                    "kos_index": kos_index,
                    "kickoff": kickoff,
                    "matches": matches,
                }
            )
    if not rows:
        return
    rows.sort(key=lambda r: r["kos_index"])

    def _badge(team_name: Optional[str]) -> str:
        label = _team_code_for_display(team_name)
        title = html.escape(team_name or label or "-")
        logo_url = _team_logo_url(team_name, logo_map)
        if logo_url:
            return (
                "<span class='gw-team-badge' title='{title}'>"
                "<img class='gw-team-logo' src='{logo}' alt='{label}' />"
                "</span>"
            ).format(title=title, logo=html.escape(logo_url), label=html.escape(label))
        return f"<span class='gw-team-badge gw-team-fallback' title='{title}'>{html.escape(label)}</span>"

    table_rows = []
    for row in rows:
        match_chunks = []
        for match in row["matches"]:
            home = match.get("home")
            away = match.get("away")
            match_chunks.append(
                "<div class='gw-match'>"
                f"{_badge(home)}<span class='gw-vs'>vs</span>{_badge(away)}"
                "</div>"
            )
        matches_html = "".join(match_chunks) if match_chunks else "<span class='gw-empty'>-</span>"
        table_rows.append(
            "<tr>"
            f"<td class='gw-kos'>{html.escape(str(row['kos_label']))}</td>"
            f"<td class='gw-matches'>{matches_html}</td>"
            f"<td class='gw-time'>{html.escape(str(row['kickoff']))}</td>"
            "</tr>"
        )

    st.markdown(
        """
        <style>
        .gw-overview-table { width: 100%; border-collapse: collapse; }
        .gw-overview-table th, .gw-overview-table td {
            padding: 6px 8px;
            border-bottom: 1px solid rgba(0, 0, 0, 0.08);
            vertical-align: top;
            font-size: 13px;
        }
        .gw-overview-table { border-bottom: 1px solid rgba(0, 0, 0, 0.08); }
        .gw-overview-table th { text-align: left; font-weight: 600; }
        .gw-team-badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 999px;
            border: 1px solid #cbd5e1;
            background: #f8fafc;
            color: #0f172a;
            font-weight: 700;
            letter-spacing: 0.3px;
            margin: 2px 4px 2px 0;
            line-height: 16px;
            min-width: 24px;
            text-align: center;
        }
        .gw-team-logo {
            width: 18px;
            height: 18px;
            vertical-align: middle;
        }
        .gw-team-fallback {
            padding: 2px 8px;
        }
        .gw-vs {
            margin: 0 6px;
            color: #64748b;
            font-weight: 600;
        }
        .gw-match { display: inline-block; margin-right: 10px; }
        .gw-kos { width: 90px; font-weight: 700; }
        .gw-time { width: 170px; color: #475569; }
        .gw-empty { color: #94a3b8; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    table_html = (
        "<table class='gw-overview-table'>"
        "<thead><tr><th>KOS</th><th>Matches</th><th>Kickoff (UTC)</th></tr></thead>"
        "<tbody>"
        + "".join(table_rows)
        + "</tbody></table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _late_kos_risk(
    roster_view: RosterView,
    kos_map: dict[str, int],
    last_kos_index: Optional[int],
) -> list[str]:
    if last_kos_index is None:
        return []
    active_ids = [str(pid) for pid in roster_view.active_player_ids()]
    reserve_ids = [str(pid) for pid in roster_view.reserve_player_ids()]
    reserve_kos = {rid: kos_map.get(rid) for rid in reserve_ids}
    risks = []
    for aid in active_ids:
        if kos_map.get(aid) != last_kos_index:
            continue
        has_same_kos = any(kos == last_kos_index for kos in reserve_kos.values())
        if not has_same_kos:
            risks.append(aid)
    return risks


def _build_event_index_from_lineups(lineup_info_by_player: Dict[str, PlayerLineupInfo]) -> dict:
    """
    Build event_id -> {home, away, kickoff} map from lineup info.
    """
    idx: dict[int, dict] = {}
    for info in lineup_info_by_player.values():
        if not info or getattr(info, "event_id", None) is None:
            continue
        eid = int(info.event_id)
        entry = idx.setdefault(eid, {"home": None, "away": None, "kickoff": getattr(info, "kickoff", None)})
        tname = getattr(info, "team_name", None)
        if getattr(info, "is_home", None) is True:
            entry["home"] = tname
        elif getattr(info, "is_home", None) is False:
            entry["away"] = tname
        entry["kickoff"] = entry["kickoff"] or getattr(info, "kickoff", None)
    return idx


def _build_event_odds_map_from_api_football(event_index: dict, season: Optional[int] = None) -> dict:
    """
    Fetch odds from API-Football for EPL (league 39) and map to our events by home/away name + kickoff.
    Stores simple implied probabilities from Match Winner market.
    """
    key = _get_api_football_key()
    if not key or not event_index:
        return {}

    # Collect unique dates for fetch
    dates: set[str] = set()
    for data in event_index.values():
        ko = data.get("kickoff")
        if isinstance(ko, datetime):
            dates.add(ko.strftime("%Y-%m-%d"))
    if not dates:
        return {}

    odds_map: dict = {}
    session = requests.Session()
    headers = {
        "x-rapidapi-host": "v3.football.api-sports.io",
        "x-rapidapi-key": key,
    }

    # helper to try matching fixture to our event by names + kickoff tolerance
    def match_fixture(fixture: dict) -> Optional[int]:
        try:
            kickoff_raw = fixture.get("fixture", {}).get("date")
            if not kickoff_raw:
                return None
            kickoff_dt = datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
            home_name = fixture.get("teams", {}).get("home", {}).get("name")
            away_name = fixture.get("teams", {}).get("away", {}).get("name")
            if not home_name or not away_name:
                return None
            nh = _normalize_team_name(home_name)
            na = _normalize_team_name(away_name)
            for eid, data in event_index.items():
                ko = data.get("kickoff")
                if not ko:
                    continue
                if abs((kickoff_dt - ko).total_seconds()) > 3 * 3600:
                    continue
                eh = _normalize_team_name(data.get("home") or "")
                ea = _normalize_team_name(data.get("away") or "")
                if nh == eh and na == ea:
                    return eid
            return None
        except Exception:
            return None

    season_year = season
    if season_year is None:
        now_utc = datetime.now(timezone.utc)
        season_year = now_utc.year if now_utc.month >= 7 else now_utc.year - 1

    for date_str in sorted(dates):
        try:
            resp = session.get(
                "https://v3.football.api-sports.io/odds",
                headers=headers,
                params={"league": 39, "season": season_year, "date": date_str, "bookmaker": 1},
                timeout=15,
            )
            payload = resp.json()
            for item in payload.get("response", []):
                eid = match_fixture(item)
                if eid is None:
                    continue
                bookmakers = item.get("bookmakers") or []
                if not bookmakers:
                    continue
                bets = bookmakers[0].get("bets") or []
                market = None
                for b in bets:
                    name = (b.get("name") or "").lower()
                    if "match winner" in name or "winner" == name or "1x2" in name:
                        market = b
                        break
                if not market:
                    continue
                values = market.get("values") or []
                entry = {"home": {}, "away": {}}
                for v in values:
                    val_name = (v.get("value") or "").lower()
                    odd = v.get("odd")
                    if not odd:
                        continue
                    try:
                        prob = 100.0 / float(odd)
                    except Exception:
                        prob = None
                    if "home" in val_name or val_name == "1":
                        entry["home"]["expected"] = prob
                        entry["home"]["actual"] = prob
                    elif "away" in val_name or val_name == "2":
                        entry["away"]["expected"] = prob
                        entry["away"]["actual"] = prob
                if entry["home"] or entry["away"]:
                    odds_map[eid] = entry
        except Exception:
            continue

    return odds_map


# ---------------------------------------------------------------------
# Session meta helpers
# ---------------------------------------------------------------------
def _last_refresh_from_meta(meta: dict) -> Optional[datetime]:
    """
    Return the most recent per-event refresh timestamp from ss_refresh_meta.
    """
    if not isinstance(meta, dict):
        return None
    timestamps = [v.get("last") for k, v in meta.items() if isinstance(v, dict) and k != "_call_count"]
    timestamps = [t for t in timestamps if t]
    if not timestamps:
        return None
    try:
        return max(datetime.fromisoformat(str(t)) for t in timestamps)
    except Exception:
        return None


def _log_mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return None


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(dt)


# ---------------------------------------------------------------------
# Lineup feed helpers
# ---------------------------------------------------------------------
# Align ETA display with live poll cadence (safe to hardcode to avoid forward refs)
SOFASCORE_SCRAPE_MINUTES = 5  # align with kickoff watcher cron (*/5)
FANTRAX_SCRAPE_MINUTES = 5
SOFASCORE_WATCHER_LOG = Path("logs/sofascore_kickoff_watcher.log")
SOFASCORE_PREDICTIONS_LOG = Path("logs/sofascore_predictions.log")


# ---------------------------------------------------------------------
# SofaScore live refresh (session-scoped, rate-limited)
# ---------------------------------------------------------------------
SS_REFRESH_MIN_INTERVAL_MINUTES_LONG = 60  # >75m out or unknown kickoff
SS_REFRESH_MIN_INTERVAL_MINUTES_TIGHT = 1  # T-75m .. kickoff (until confirmed)
SS_REFRESH_MAX_CALLS_PER_SESSION = 120


def _load_cached_lineup(event_id: int) -> tuple[Optional[dict], Optional[Path]]:
    path = DEFAULT_LINEUPS_DIR / f"{event_id}.json"
    if not path.exists():
        return None, path
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except Exception:
        return None, path


def _lineup_changed(old: Optional[dict], new: dict) -> bool:
    if old is None:
        return True
    # Compare key fields to avoid false positives on ordering
    for key in ("confirmed",):
        if old.get(key) != new.get(key):
            return True

    def _players(side):
        return [
            (p.get("id"), p.get("name"), p.get("position"), p.get("team_id"), p.get("substitute"))
            for p in side.get("starters", [])
        ] + [
            (p.get("id"), p.get("name"), p.get("position"), p.get("team_id"), p.get("substitute"))
            for p in side.get("subs", [])
        ]

    for side_key in ("home", "away"):
        if _players(old.get(side_key, {})) != _players(new.get(side_key, {})):
            return True
    if old.get("home", {}).get("formation") != new.get("home", {}).get("formation"):
        return True
    if old.get("away", {}).get("formation") != new.get("away", {}).get("formation"):
        return True
    return False


def _should_refresh(now: datetime, kickoff: Optional[datetime], confirmed: bool, last: Optional[datetime]) -> tuple[bool, int]:
    """
    Return (should_refresh, interval_minutes_used_for_decision) based on policy:
    - If confirmed: skip further refreshes.
    - If kickoff missing: hourly.
    - If now < kickoff-75m: hourly.
    - If kickoff-75m <= now < kickoff and not confirmed: every minute.
    - If now >= kickoff: skip until kickoff+2h.
    """
    if confirmed:
        return False, 0
    if kickoff is None:
        min_interval = SS_REFRESH_MIN_INTERVAL_MINUTES_LONG
        if last and (now - last).total_seconds() < min_interval * 60:
            return False, min_interval
        return True, min_interval

    delta_min = (kickoff - now).total_seconds() / 60
    if delta_min > 75:
        min_interval = SS_REFRESH_MIN_INTERVAL_MINUTES_LONG
        if last and (now - last).total_seconds() < min_interval * 60:
            return False, min_interval
        return True, min_interval
    if 0 <= delta_min <= 75:
        min_interval = SS_REFRESH_MIN_INTERVAL_MINUTES_TIGHT
        if last and (now - last).total_seconds() < min_interval * 60:
            return False, min_interval
        return True, min_interval
    # After kickoff
    if delta_min < 0:
        if delta_min <= -120:
            return False, 120  # done with this event
        return False, 0
    return False, 0


def _maybe_refresh_sofascore_live(event_kickoffs: dict[int, Optional[datetime]]) -> None:
    now = datetime.now(timezone.utc)
    meta = st.session_state.setdefault("ss_refresh_meta", {})
    meta["_last_error"] = None
    meta["_last_debug"] = f"enter events={len(event_kickoffs)} call_count={meta.get('_call_count', 0)}"
    call_count = meta.get("_call_count", 0)
    if call_count >= SS_REFRESH_MAX_CALLS_PER_SESSION:
        return

    for event_id, kickoff in event_kickoffs.items():
        cached, path = _load_cached_lineup(event_id)
        confirmed = bool(cached.get("confirmed")) if cached else False

        last_raw = meta.get(event_id, {}).get("last")
        last_dt = None
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(str(last_raw))
            except Exception:
                last_dt = None

        should, _interval = _should_refresh(now, kickoff, confirmed, last_dt)
        if not should:
            meta["_last_debug"] = (
                f"skip should_refresh=False event={event_id} confirmed={confirmed} "
                f"kickoff={kickoff} last_dt={last_dt}"
            )
            continue

        if call_count >= SS_REFRESH_MAX_CALLS_PER_SESSION:
            break

        try:
            fresh_raw = raw_get_lineups(event_id)
            fresh = lineup_to_json(event_id, fresh_raw)
        except Exception as exc:  # pragma: no cover - runtime fetch
            logger.info("[ss-live] fetch failed for %s: %s", event_id, exc)
            meta["_last_error"] = str(exc)
            meta["_last_debug"] = f"fetch failed event={event_id}"
            continue

        if not _lineup_changed(cached, fresh):
            meta[event_id] = {"last": now.isoformat()}
            call_count += 1
            meta["_last_debug"] = f"no change event={event_id}"
            continue

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh, indent=2), encoding="utf-8")
            st.toast(f"Live SofaScore update applied for event {event_id}", icon="✅")
            logger.info("[ss-live] updated lineup for event %s", event_id)
        except Exception as exc:
            logger.warning("[ss-live] failed to write lineup %s: %s", event_id, exc)
        finally:
            meta[event_id] = {"last": now.isoformat()}
            call_count += 1
            meta["_last_debug"] = f"updated event={event_id}"

    meta["_call_count"] = call_count


def _augment_event_kickoffs_if_empty(
    *,
    league_id: str,
    inferred_round: Optional[str],
    existing: dict[int, Optional[datetime]],
) -> dict[int, Optional[datetime]]:
    """
    If no event_ids were found from cached lineups, fetch upcoming schedule
    on the fly and derive next-match event_ids for roster teams in this league.
    """
    if existing:
        return existing
    try:
        # Attempt to infer tournament_id from session (default EPL=17).
        tournament_id = st.session_state.get("sofascore_tournament_id", 17)
        season_id = st.session_state.get("sofascore_season_id")
        if not season_id:
            try:
                seasons = raw_get_seasons(tournament_id)
                season_id = choose_season_from_list(seasons, None)
                st.session_state["sofascore_season_id"] = season_id
            except Exception:
                season_id = None

        # Pull upcoming events via raw iter; filter to next match per team
        schedule_rows = []
        for ev in raw_iter_tournament_events(tournament_id, season_id or 0, upcoming=True):
            kickoff = parse_kickoff_dt(fmt_ts := ev.get("startTimestamp") or ev.get("startTime"))
            home_tid = safe_team_id(ev.get("homeTeam") or ev.get("home_team"))
            away_tid = safe_team_id(ev.get("awayTeam") or ev.get("away_team"))
            schedule_rows.append(
                {
                    "event_id": int(ev.get("id")),
                    "kickoff": kickoff,
                    "home_team_id": home_tid,
                    "away_team_id": away_tid,
                    "round": (ev.get("roundInfo") or {}).get("round"),
                }
            )
        if not schedule_rows:
            return existing
        # Build next-match map per team
        next_by_team: dict[int, tuple[int, Optional[datetime], Optional[str]]] = {}
        now = datetime.now(timezone.utc)
        for row in sorted(schedule_rows, key=lambda r: r.get("kickoff") or now + timedelta(days=365)):
            if inferred_round and row.get("round") and str(row["round"]) != str(inferred_round):
                continue
            ko = row.get("kickoff")
            if not ko or ko <= now:
                continue
            eid = row["event_id"]
            for tid in (row.get("home_team_id"), row.get("away_team_id")):
                if tid is None or tid in next_by_team:
                    continue
                next_by_team[tid] = (eid, ko, row.get("round"))
        # Map roster teams from session (if available)
        roster_team_ids: set[int] = set()
        roster_team_ids.update(st.session_state.get("sofascore_team_ids", []))
        # If we have no roster teams mapped, just take the next N events as fallback
        target_events: dict[int, Optional[datetime]] = {}
        if roster_team_ids:
            for tid in roster_team_ids:
                val = next_by_team.get(tid)
                if val:
                    target_events[val[0]] = val[1]
        if not target_events:
            # fallback: take first 10 upcoming events
            for row in sorted(schedule_rows, key=lambda r: r.get("kickoff") or now + timedelta(days=365))[:10]:
                target_events[row["event_id"]] = row.get("kickoff")
        return target_events or existing
    except Exception as exc:
        logger.info("[ss-live] schedule augment skipped: %s", exc)
        return existing


# ---------------------------------------------------------------------
# EPL gameweek + Fantrax period selection (single source of truth)
# ---------------------------------------------------------------------
cached_round = st.session_state.get("current_sofascore_round")
inferred_round = infer_current_gameweek()
if inferred_round:
    st.session_state["current_sofascore_round"] = inferred_round
else:
    inferred_round = cached_round

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
    inferred_match = _match_period_from_round(inferred_round, period_id_map)

    try:
        detected_period = str(api.resolve_active_period(team_id))
    except Exception:
        detected_period = None

    cached_period = st.session_state.get("selected_gameweek_period_id")
    override_round = st.session_state.get("period_override_round")
    manual_override_active = bool(st.session_state.get("period_manual_override")) and (
        str(override_round or "") == str(inferred_round or "")
    )

    if inferred_match and inferred_match in period_id_map and not manual_override_active:
        default_period_id = inferred_match
    elif cached_period and cached_period in period_id_map:
        default_period_id = cached_period
    else:
        if detected_period and detected_period in period_id_map:
            default_period_id = detected_period
        else:
            default_period_id = period_choices[0]

    period_widget_key = "selected_gameweek_period_id"
    stale_selected = st.session_state.get(period_widget_key) not in period_id_map
    if (
        (not manual_override_active)
        or period_widget_key not in st.session_state
        or stale_selected
    ):
        st.session_state[period_widget_key] = default_period_id

    st.subheader("Gameweek / Fantrax period")
    col_gw, col_period = st.columns([1, 3])
    with col_gw:
        st.metric("Inferred EPL GW", inferred_round or "Unknown")
    with col_period:
        inferred_period_for_button = (
            inferred_match if inferred_match and inferred_match in period_id_map else default_period_id
        )

        if st.button(
            "Use Inferred GW",
            key="use_inferred_gw_period_btn",
            help="Reset the Fantrax period selector to the period mapped from the inferred EPL gameweek.",
        ):
            st.session_state[period_widget_key] = inferred_period_for_button
            st.session_state["period_manual_override"] = False
            st.session_state["period_override_round"] = inferred_round
            _safe_rerun()
        selected_period_id = st.selectbox(
            "Fantrax period to use for swaps and rules",
            options=period_choices,
            format_func=lambda pid: period_id_map.get(pid, pid),
            key=period_widget_key,
            on_change=_mark_period_manual_override,
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
    if user_id and selected_period_id:
        pref_mgr = UserManager()
        if hasattr(pref_mgr, "set_preferred_period"):
            try:
                stored_pref = pref_mgr.get_preferred_period(str(user_id), str(league_id))
            except Exception:
                stored_pref = None
            if str(stored_pref or "") != str(selected_period_id):
                pref_mgr.set_preferred_period(
                    user_id=str(user_id),
                    league_id=str(league_id),
                    period_id=str(selected_period_id),
                    period_label=selected_period_label or "",
                    team_id=str(team_id),
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

try:
    if lineup_period is not None:
        roster = api.roster_info(team_id, period=lineup_period)
    else:
        roster = api.roster_info(team_id)
except Exception as exc:
    st.error(f"Failed to load roster: {exc}")
    st.stop()

roster_view = RosterView(roster)
player_lookup = {
    str(getattr(row.player, "id")): row
    for row in roster.rows
    if getattr(row, "player", None) and getattr(row.player, "id", None)
}

with st.spinner("Refreshing lineup data..."):
    lineup_fetch_completed_at = datetime.now(timezone.utc)
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
            global_status_path=global_status_path,
        )
    except Exception as exc:
        logger.exception("Failed to resolve lineup info map")
        st.error(f"Unable to build lineup info map: {exc}")
        st.stop()
    else:
        st.session_state["fantrax_lineup_fetch_at"] = lineup_fetch_completed_at

    # ------------------------------------------------------------------
    # Session-scoped SofaScore live refresh (rate-limited)
    # ------------------------------------------------------------------
    ss_meta = st.session_state.setdefault("ss_refresh_meta", {"_call_count": 0})
    try:
        event_kickoffs: dict[int, Optional[datetime]] = {}
        for info in lineup_info_by_player.values():
            if info and getattr(info, "event_id", None):
                event_kickoffs[int(info.event_id)] = getattr(info, "kickoff", None)
        event_kickoffs = _augment_event_kickoffs_if_empty(
            league_id=league_id, inferred_round=inferred_round, existing=event_kickoffs
        )
        if event_kickoffs:
            _maybe_refresh_sofascore_live(event_kickoffs)
            # Rebuild lineup map so terminal/feed views reflect fresh SofaScore data.
            lineup_info_by_player = resolve_lineup_info(
                roster,
                session=session,
                league_id=league_id,
                period=lineup_period,
                strategy=strategy,
                mapping_manager=mapping_manager,
                round_hint=inferred_round,
                global_status_path=global_status_path,
            )
    except Exception as exc:
        # Capture the failure in session state so the UI can surface it.
        ss_meta["_last_error"] = str(exc)
        ss_meta["_last_debug"] = "refresh skipped due to error"
        logger.info("[ss-live] refresh skipped due to error: %s", exc)

    ss_meta = st.session_state.get("ss_refresh_meta", {})
    last_err = None
    if isinstance(ss_meta, dict):
        last_err = ss_meta.get("_last_error")
    last_debug = None
    if isinstance(ss_meta, dict):
        last_debug = ss_meta.get("_last_debug")
    last_any = None
    if isinstance(ss_meta, dict):
        timestamps = [
            v.get("last") for k, v in ss_meta.items() if isinstance(v, dict) and k != "_call_count"
        ]
        timestamps = [t for t in timestamps if t]
        if timestamps:
            try:
                last_any = max(datetime.fromisoformat(str(t)) for t in timestamps)
            except Exception:
                last_any = None
    call_count = ss_meta.get("_call_count", 0)
    st.caption(
        f"SofaScore live refresh: calls this session={call_count}; "
        f"last live fetch={last_any.strftime('%Y-%m-%d %H:%M:%S UTC') if last_any else 'N/A'}"
    )

    # with st.expander("🔍 SofaScore live refresh debug"):
    #     st.write("Target event kickoffs (event_id -> kickoff UTC):")
    #     if event_kickoffs:
    #         st.write(
    #             {
    #                 eid: ko.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ko, datetime) else str(ko)
    #                 for eid, ko in event_kickoffs.items()
    #             }
    #         )
    #     else:
    #         st.write("None")
    #     st.write(
    #         "Last ss_refresh_meta:",
    #         {k: v for k, v in ss_meta.items() if k != "_call_count"} if isinstance(ss_meta, dict) else ss_meta,
    #     )
    #     if last_err:
    #         st.write("Last live fetch error:", str(last_err))
    #     if last_debug:
    #         st.write("Last live refresh debug:", str(last_debug))

    # with st.expander("🧩 Event ID debug"):
    #     event_rows = []
    #     for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
    #         row = player_lookup.get(pid)
    #         name = getattr(getattr(row, "player", None), "name", str(pid))
    #         info = lineup_info_by_player.get(pid)
    #         event_rows.append(
    #             {
    #                 "Player": name,
    #                 "Team": getattr(info, "team_name", None) if info else None,
    #                 "Opponent": getattr(info, "opponent_name", None) if info else None,
    #                 # Event ID should be a string in the dataframe
    #                 "Event ID": str(getattr(info, "event_id", None)) if info else None,
    #                 "Kickoff (UTC)": _format_kickoff(getattr(info, "kickoff", None)) if info else "—",
    #             }
    #         )
    #     if event_rows:
    #         st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
    #     else:
    #         st.write("No event data available for this roster.")

kos_map, last_kos_index = _build_kos_index_map(lineup_info_by_player)
never_drop_ids: set[str] = set()
with st.sidebar:
    st.markdown("**Projections sync**")
    if st.button("Refresh projections now", key="refresh_projections_now"):
        _get_projections_map(force_refresh=True)
    sync_msg = _projection_sync_status_message()
    if sync_msg:
        level, text = sync_msg
        if level == "success":
            st.success(text)
        elif level == "warning":
            st.warning(text)
        else:
            st.error(text)
    st.caption(f"Auto-refresh interval: {PROJECTIONS_REFRESH_TTL_MINUTES} min")
    with st.expander("Projections diagnostics", expanded=False):
        st.json(_projection_diagnostics_payload())

projections_map = _get_projections_map()
projection_stale_message = _projection_stale_message()

if user_id:
    user_mgr = UserManager()
    late_kos_risks = _late_kos_risk(roster_view, kos_map, last_kos_index)
    policy_value = user_mgr.get_late_kos_policy(str(user_id), str(league_id))
    policy_options = {
        "Trust projections (no cover swap)": "trust",
        "Cover with earlier confirmed starter": "cover",
    }
    policy_labels = list(policy_options.keys())
    policy_index = 0
    for idx, label in enumerate(policy_labels):
        if policy_options[label] == policy_value:
            policy_index = idx
            break

    player_options = []
    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue
        label = f"{player.name} ({_display_pos(row)})"
        player_options.append((label, str(player.id)))
    player_options.sort(key=lambda x: x[0])
    id_to_label = {pid: label for label, pid in player_options}
    label_to_id = {label: pid for label, pid in player_options}
    stored_do_not_move = user_mgr.get_do_not_move(str(user_id), str(league_id))
    default_labels = [id_to_label[pid] for pid in stored_do_not_move if pid in id_to_label]
    auto_never_drop: list[str] = []
    top_fpts_ids = _top_fpts_ids_cached(
        waivers_service,
        league_id=str(league_id),
        limit=50,
    )
    if top_fpts_ids:
        auto_never_drop = _default_never_drop_ids(roster, top_fpts_ids)
    never_drop_state = user_mgr.get_never_drop_state(str(user_id), str(league_id))
    stored_auto = set(never_drop_state.get("auto") or [])
    manual_add = set(never_drop_state.get("manual_add") or [])
    manual_remove = set(never_drop_state.get("manual_remove") or [])
    stored_effective = set(never_drop_state.get("effective") or [])
    auto_set = set(auto_never_drop) if auto_never_drop else set(stored_auto)

    if auto_never_drop:
        if never_drop_state.get("legacy"):
            manual_add = set()
            manual_remove = set()
            user_mgr.set_never_drop_state(
                user_id=str(user_id),
                league_id=str(league_id),
                auto_ids=list(auto_set),
                manual_add=[],
                manual_remove=[],
                team_id=str(team_id),
            )
        elif stored_auto != auto_set:
            user_mgr.set_never_drop_state(
                user_id=str(user_id),
                league_id=str(league_id),
                auto_ids=list(auto_set),
                manual_add=list(manual_add),
                manual_remove=list(manual_remove),
                team_id=str(team_id),
            )
    stored_effective = (auto_set - manual_remove) | manual_add
    default_never_drop_labels = [id_to_label[pid] for pid in stored_effective if pid in id_to_label]
    never_drop_ids = set(stored_effective or [])

    with st.sidebar:
        st.markdown("**Late KOS coverage**")
        if late_kos_risks:
            risk_labels = [id_to_label.get(pid, pid) for pid in late_kos_risks]
            st.warning("Late KOS actives without same-KOS cover: " + ", ".join(risk_labels))
        policy_choice = st.selectbox(
            "Late KOS policy",
            options=policy_labels,
            index=policy_index,
            help="Applies per league when late-KOS actives have no same-KOS cover.",
            key="late_kos_policy_select",
        )
        if policy_options.get(policy_choice) != policy_value:
            user_mgr.set_late_kos_policy(
                user_id=str(user_id),
                league_id=str(league_id),
                mode=policy_options.get(policy_choice, "trust"),
                team_id=str(team_id),
            )

        st.markdown("**Do not move unless confirmed out**")
        selected_labels = st.multiselect(
            "Players",
            options=[label for label, _ in player_options],
            default=default_labels,
            key="do_not_move_players",
        )
        selected_ids = [label_to_id[label] for label in selected_labels if label in label_to_id]
        if set(selected_ids) != set(stored_do_not_move):
            user_mgr.set_do_not_move(
                user_id=str(user_id),
                league_id=str(league_id),
                player_ids=selected_ids,
                team_id=str(team_id),
            )

        st.markdown("**Never drop (default = top 50 by FPts, league-wide)**")
        if st.button("Reset to auto list", key="never_drop_reset"):
            auto_set = set(auto_never_drop) if auto_never_drop else set(stored_auto)
            user_mgr.set_never_drop_state(
                user_id=str(user_id),
                league_id=str(league_id),
                auto_ids=list(auto_set),
                manual_add=[],
                manual_remove=[],
                team_id=str(team_id),
            )
            st.session_state["never_drop_players"] = [
                id_to_label[pid] for pid in auto_set if pid in id_to_label
            ]
            st.session_state["never_drop_dirty"] = False
            _safe_rerun()
            st.stop()
        current_never_drop = st.session_state.get("never_drop_players")
        never_drop_dirty = bool(st.session_state.get("never_drop_dirty", False))
        if current_never_drop is None:
            st.session_state["never_drop_players"] = list(default_never_drop_labels)
            st.session_state["never_drop_dirty"] = False
        elif not never_drop_dirty and set(current_never_drop) != set(default_never_drop_labels):
            st.session_state["never_drop_players"] = list(default_never_drop_labels)
        never_drop_labels = st.multiselect(
            "Players",
            options=[label for label, _ in player_options],
            key="never_drop_players",
            on_change=_mark_never_drop_dirty,
        )
        never_drop_ids = [label_to_id[label] for label in never_drop_labels if label in label_to_id]
        if set(never_drop_ids) != set(stored_effective):
            auto_set = set(auto_never_drop) if auto_never_drop else set(stored_auto)
            manual_add = set(never_drop_ids) - auto_set
            manual_remove = auto_set - set(never_drop_ids)
            user_mgr.set_never_drop_state(
                user_id=str(user_id),
                league_id=str(league_id),
                auto_ids=list(auto_set),
                manual_add=list(manual_add),
                manual_remove=list(manual_remove),
                team_id=str(team_id),
            )
            st.session_state["never_drop_dirty"] = False

    if not isinstance(never_drop_ids, set):
        never_drop_ids = set(never_drop_ids)

legacy_storage = RuleStorage()
now = datetime.now(timezone.utc)

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


def _format_eta(
    now: datetime,
    last_fetch: Optional[datetime],
    interval_minutes: int,
) -> tuple[str, Optional[datetime]]:
    """
    Return a human-readable countdown string and the inferred next scrape time.
    """
    if not last_fetch:
        return "Unknown", None
    next_at = last_fetch + timedelta(minutes=interval_minutes)
    delta = next_at - now
    if delta.total_seconds() <= 0:
        return "Due now", next_at
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts), next_at


def _format_status_tuple(value: Optional[tuple]) -> str:
    if not value:
        return "—"
    vals = list(value) + [None] * 6
    status, ss_status, ss_pred, ss_conf, fx_conf, fx_pred = vals[:6]
    return (
        f"status={_format_status(status)} | "
        f"ss={_format_status(ss_status)} | "
        f"ss_pred={_format_status(ss_pred)} | "
        f"ss_conf={_format_status(ss_conf)} | "
        f"fx_conf={_format_status(fx_conf)} | "
        f"fx_pred={_format_status(fx_pred)}"
    )


def _build_lineup_feed_rows(
    *,
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    player_lookup: Dict[str, RosterRow],
    projections: Optional[dict] = None,
) -> list[dict]:
    """
    Build a simple feed payload sorted by Active/Reserve, then position (G/D/M/F).
    """
    rows: list[dict] = []
    active_ids = roster_view.active_player_ids()
    reserve_ids = roster_view.reserve_player_ids()
    active_set = set(active_ids)
    order_index = {pid: i for i, pid in enumerate(active_ids + reserve_ids)}
    pos_order = {"G": 0, "D": 1, "M": 2, "F": 3}
    for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
        info = lineup_info_by_player.get(pid)
        row = player_lookup.get(pid)
        if not row:
            continue
        team, opponent = _team_and_opponent_for_player(pid, lineup_info_by_player, player_lookup)
        kickoff = info.kickoff if info else None
        ss_effective = None
        ss_confirmed = 0
        if info:
            if getattr(info, "status_source", None) == "sofascore" and getattr(
                info, "status", None
            ) not in (None, LineupStatus.UNKNOWN):
                ss_effective = getattr(info, "status", None)
                if getattr(info, "ss_conf_status", None):
                    ss_confirmed = 1
            elif getattr(info, "ss_conf_status", None):
                ss_effective = getattr(info, "ss_conf_status")
                ss_confirmed = 1
            elif getattr(info, "ss_pred_status", None):
                ss_effective = getattr(info, "ss_pred_status")
            else:
                ss_effective = getattr(info, "ss_status", None)
        ss_code = _status_code(ss_effective)
        ss_pred_code = _status_code(getattr(info, "ss_pred_status", None) if info else None)

        fx_effective = getattr(info, "fx_conf_status", None) if info else None
        fx_code = _status_code(fx_effective)
        proj_gs = None
        projection_missing = False
        if projections:
            proj_row = _player_projection_row(info, projections=projections, fallback_name=row.player.name)
            if proj_row:
                try:
                    proj_gs = int(proj_row.get("ProjGS"))
                except Exception:
                    proj_gs = None
            else:
                projection_missing = True

        pos = _display_pos(row).upper()
        slot_order = 0 if pid in active_set else 1
        rows.append(
            {
                "team": team or "-",
                "player": row.player.name,
                "ss": ss_code,
                "ss_pred": ss_pred_code,
                "fx": fx_code,
                "confirmed": ss_confirmed,
                "tds": ("MISS" if projection_missing else proj_gs),
                "projection_missing": projection_missing,
                "kickoff": kickoff,
                "kickoff_label": _format_kickoff(kickoff),
                "opponent": opponent,
                "slot_order": slot_order,
                "pos_order": pos_order.get(pos, 9),
                "order": order_index.get(pid, 999),
            }
        )
    rows.sort(key=lambda r: (r["slot_order"], r["pos_order"], r["order"]))
    return rows


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
    raw: dict[str, Any] = {}
    scorer: dict[str, Any] = {}
    if row:
        raw = getattr(row, "_raw", {}) or {}
        scorer = raw.get("scorer") or {}
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
        "team_name_raw": debug_ctx.get("team_name_raw"),
        "team_name_normalized": debug_ctx.get("team_name"),
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
        "scorer_team_id": scorer.get("teamId") or scorer.get("team_id"),
        "scorer_team_name": scorer.get("teamName"),
        "scorer_team_short_name": scorer.get("teamShortName"),
        "scorer_event_id": scorer.get("eventId"),
        "scorer_next_kickoff": scorer.get("nextKickoff"),
        "scorer_next_opponent": scorer.get("nextOpponent"),
        "lineup_row_raw": row._raw,
    }
    # st.markdown(f"**SofaScore debug for {player_name} ({fantrax_player_id})**")
    # st.code(json.dumps(payload, indent=2))
    logger.info("[sofa-debug] %s", json.dumps(payload, separators=(',', ':')))
    # if fx_debug:
    #     st.markdown("**Fantrax lineup debug**")
    #     st.code(json.dumps(fx_debug, indent=2, default=str))


# ----------------------------------------------------------------------
# Immediate swap section (single)
# ----------------------------------------------------------------------
st.divider()
st.subheader("Roster & Gameweek Overview")
st.caption(
    "Projected lineups (SofaScore vs. Fantrax) ordered by active/reserve then position. "
    "1 = predicted starter, 0 = predicted bench, -1 = predicted out. "
    "Confirmed = 1 only when the provider marks a lineup as confirmed "
    "(SofaScore confirmed flag, Fantrax confirmed icon); otherwise 0. "
    "TDS = projGS (1 = projected starter, 0 = projected bench)."
)

ss_meta = st.session_state.get("ss_refresh_meta", {})
sofascore_last_fetch_live = _last_refresh_from_meta(ss_meta)
sofascore_last_fetch_disk = _latest_lineup_fetch_timestamp(Path(DEFAULT_LINEUPS_DIR))
sofascore_last_fetch = sofascore_last_fetch_live or sofascore_last_fetch_disk
sofascore_last_log = _log_mtime(SOFASCORE_WATCHER_LOG)
sofascore_predictions_log = _log_mtime(SOFASCORE_PREDICTIONS_LOG)
fantrax_last_fetch = st.session_state.get("fantrax_lineup_fetch_at")

ss_eta_label, ss_next = _format_eta(now, sofascore_last_fetch, SOFASCORE_SCRAPE_MINUTES)
fx_eta_label, fx_next = _format_eta(now, fantrax_last_fetch, FANTRAX_SCRAPE_MINUTES)

# wrap these in an expander component labeled Live Lineup Fetch Diagnostics
with st.expander("Live Lineup Fetch Diagnostics", expanded=False):   
    col_ss, col_fx = st.columns(2)
    with col_ss:
        st.metric(
            "Next SofaScore scrape (live)",
            ss_eta_label,
            delta=(
                f"Last cache: {_fmt_dt(sofascore_last_fetch)} | Last watcher log: {_fmt_dt(sofascore_last_log)}"
                if (sofascore_last_fetch or sofascore_last_log)
                else "No live refresh detected yet"
            ),
        )
        st.caption(f"Predictions (hourly) last log: {_fmt_dt(sofascore_predictions_log)}")
    with col_fx:
        st.metric(
            "Next Fantrax scrape (est.)",
            fx_eta_label,
            delta=(
                f"Last: {fantrax_last_fetch.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                if fantrax_last_fetch
                else "Using current session"
            ),
        )

selected_round = resolve_round_from_period(
    selected_period_id=selected_period_id,
    selected_period_label=selected_period_label,
    inferred_round=inferred_round,
)
overview_round = selected_round or inferred_round
gw_num = overview_round or selected_period_id
gw_label = f"GW {gw_num}" if gw_num else "GW"
schedule_events, schedule_source = _fetch_gameweek_events(overview_round)
gw_meta = _build_gameweek_meta_from_events(schedule_events, gw_label)
if not gw_meta:
    gw_meta = _build_gameweek_meta(lineup_info_by_player, gw_label)
if gw_meta:
    st.markdown("**Gameweek overview**")
    # st.code(json.dumps(gw_meta, indent=2), language="json")
    _render_gameweek_overview_table(gw_meta, gw_label)
    if schedule_events:
        tournament_id = st.session_state.get("sofascore_tournament_id", 17)
        expected_matches = 10 if int(tournament_id) == 17 else None
        source_label = schedule_source or "live"
        count_label = f"{len(schedule_events)} events"
        if expected_matches and len(schedule_events) != expected_matches:
            count_label += f" (expected {expected_matches})"
        st.caption(
            "Schedule source: "
            f"{source_label} | {count_label} | Selected period: {selected_period_label or selected_period_id or 'n/a'} "
            f"| Resolved round: {overview_round or 'n/a'} | Inferred round: {inferred_round or 'n/a'}"
        )
    else:
        st.caption(
            "Schedule fallback: derived from this roster's kickoffs only. "
            f"Selected period: {selected_period_label or selected_period_id or 'n/a'} | "
            f"Resolved round: {overview_round or 'n/a'} | Inferred round: {inferred_round or 'n/a'}"
        )

projections_map = _get_projections_map()

feed_rows = _build_lineup_feed_rows(
    roster_view=roster_view,
    lineup_info_by_player=lineup_info_by_player,
    player_lookup=player_lookup,
    projections=projections_map,
)

# Track status changes after refresh
status_changes: list[dict] = []
current_status_map: dict[str, tuple] = {}
prev_status_map = st.session_state.get("prev_lineup_statuses", {})
status_refresh_at = st.session_state.get("fantrax_lineup_fetch_at") or datetime.now(timezone.utc)
status_refresh_label = _fmt_dt(status_refresh_at)
for pid, info in lineup_info_by_player.items():
    if not info:
        continue
    key = str(pid)
    current_status_map[key] = (
        getattr(info, "status", None),
        getattr(info, "ss_status", None),
        getattr(info, "ss_pred_status", None),
        getattr(info, "ss_conf_status", None),
        getattr(info, "fx_conf_status", None),
        getattr(info, "fx_pred_status", None),
    )
    old = prev_status_map.get(key)
    if old and old != current_status_map[key]:
        status_changes.append(
            {
                "Player": player_lookup.get(pid).player.name if pid in player_lookup else pid,
                "From": _format_status_tuple(old),
                "To": _format_status_tuple(current_status_map[key]),
                "Note": getattr(info, "note", None),
                "Source": getattr(info, "status_source", None),
                "Refreshed (UTC)": status_refresh_label,
            }
        )
st.session_state["prev_lineup_statuses"] = current_status_map

feed_styles = """
<style>
.lineup-feed-terminal {
    background: #031403;
    color: #56ff76;
    border: 1px solid #0f5e19;
    box-shadow: 0 0 12px rgba(0, 255, 90, 0.3);
    font-family: SFMono-Regular, Menlo, Consolas, "Courier New", monospace;
    padding: 12px;
    border-radius: 4px;
    max-height: 420px;
    overflow-y: auto;
}
.lineup-feed-table {
    width: 100%;
    border-collapse: collapse;
}
.lineup-feed-table th,
.lineup-feed-table td {
    padding: 6px 8px;
    border-bottom: 1px solid rgba(86, 255, 118, 0.12);
    white-space: nowrap;
    color: #56ff76;
    font-size: 13px;
}
.lineup-feed-table th {
    text-align: left;
    font-weight: 700;
    color: #8dffb1;
}
.lineup-feed-table tr:last-child td {
    border-bottom: none;
}
.lineup-feed-team {
    color: #6df19b;
    font-weight: 700;
}
.col-player { width: 34%; }
.col-ss { width: 12%; }
.col-sspred { width: 12%; }
.col-fx { width: 12%; }
.col-tds { width: 8%; }
.col-conf { width: 8%; }
.col-ko { width: 20%; }
</style>
"""

st.subheader("Roster with compiled lineup statuses")
projection_updated_label = _projection_last_updated_label()
if projection_updated_label:
    st.caption(f"Projections last updated: {projection_updated_label}")
if projection_stale_message:
    st.warning(projection_stale_message)
missing_projection_names = [str(r.get("player")) for r in feed_rows if r.get("projection_missing")]
if missing_projection_names:
    unique_missing = sorted(set(missing_projection_names))
    st.warning(
        "Missing projection this GW (treated as likely unavailable in auto swap logic): "
        + ", ".join(unique_missing[:12])
        + (" ..." if len(unique_missing) > 12 else "")
    )

if feed_rows:
    header = """
    <tr>
        <th class='col-team'>Team</th>
        <th class='col-player'>Player vs Opp</th>
        <th class='col-ss'>SofaScore</th>
        <th class='col-sspred'>SS Pred</th>
        <th class='col-fx'>Fantrax</th>
        <th class='col-tds'>TDS</th>
        <th class='col-conf'>Confirmed</th>
        <th class='col-ko'>KO (UTC)</th>
    </tr>
    """
    body_rows = []
    for row in feed_rows:
        body_rows.append(
            "<tr>"
            f"<td class='col-team'><span class='lineup-feed-team'>{row['team']}</span></td>"
            f"<td class='col-player'>{row['player']} vs {row['opponent']}</td>"
            f"<td class='col-ss'>{row['ss']}</td>"
            f"<td class='col-sspred'>{row['ss_pred']}</td>"
            f"<td class='col-fx'>{row['fx']}</td>"
            f"<td class='col-tds'>{row['tds'] if row['tds'] is not None else ''}</td>"
            f"<td class='col-conf'>{row['confirmed']}</td>"
            f"<td class='col-ko'>{row['kickoff_label']}</td>"
            "</tr>"
        )
    feed_html = (
        feed_styles
        + "<div class='lineup-feed-terminal'>"
        + "<table class='lineup-feed-table'>"
        + header
        + "".join(body_rows)
        + "</table>"
        + "</div>"
    )
    st.markdown(feed_html, unsafe_allow_html=True)
else:
    st.caption("No projected lineup data is available yet.")

if status_changes:
    with st.expander("Lineup status changes (latest refresh)", expanded=False):
        st.caption(f"Last refresh: {status_refresh_label}")
        st.dataframe(pd.DataFrame(status_changes), hide_index=True, use_container_width=True)

st.divider()
st.subheader("Optimize your lineup for conditional swaps")
st.write(
    "Conditional swaps move an active player to the bench (or vice versa) when certain conditions are met. "
    "Because of that, the current starting XI is the foundation for every rule. "
    "If you want more options later—especially for early kickoffs—make sure your active spots start with those players now."
)

# Ensure we have a resolved Fantrax period early (used by optimized lineup apply)
if "swap_period_int" not in locals():
    swap_period_int = None
try:
    if swap_period_int is None and selected_period_id is not None:
        swap_period_int = int(selected_period_id)
except Exception:
    swap_period_int = None
if swap_period_int is None:
    try:
        swap_period_int = int(api.resolve_active_period(team_id))
    except Exception:
        swap_period_int = None

# ----------------------------------------------------------------------
# Optimized lineup (ProjFPts-driven) preview + apply
# ----------------------------------------------------------------------
st.markdown("**Suggested optimized XI (10 outfield + 1 GK if available)**")
st.caption(
    "GS uses confirmed lineup status when available (SofaScore then Fantrax); otherwise ProjGS."
)
st.markdown(
    """
    <style>
    button[data-testid="baseButton-secondary"]:disabled {
        border: 1px solid #0f5e19;
        color: #56ff76;
        background: #031403;
        opacity: 1;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Ensure projections are loaded for optimization
projections_map = _get_projections_map()

def _kickoff_or_max(info: Optional[PlayerLineupInfo]) -> datetime:
    ko = getattr(info, "kickoff", None) if info else None
    return ko or datetime.max.replace(tzinfo=timezone.utc)

def _status_to_gs(status: Optional[LineupStatus]) -> Optional[int]:
    if status == LineupStatus.STARTING:
        return 1
    if status in (LineupStatus.BENCH, LineupStatus.OUT, LineupStatus.DOUBTFUL):
        return 0
    return None


def _resolve_gs(
    info: Optional[PlayerLineupInfo],
    proj_gs: Optional[int],
    row: Optional[RosterRow],
) -> tuple[int, str]:
    gs = _status_to_gs(getattr(info, "ss_conf_status", None))
    if gs is not None:
        return gs, "Confirmed (SofaScore)"
    gs = _status_to_gs(getattr(info, "fx_conf_status", None))
    if gs is not None:
        return gs, "Confirmed (Fantrax)"
    gs = _status_to_gs(_fantrax_confirmed_status(row))
    if gs is not None:
        return gs, "Confirmed (Fantrax)"
    if proj_gs is not None:
        return int(proj_gs), "Projected (TDS)"
    return 0, "Unknown"


def _fantrax_confirmed_status(row: Optional[RosterRow]) -> Optional[LineupStatus]:
    if not row or not getattr(row, "_raw", None):
        return None
    scorer = row._raw.get("scorer") or {}
    icons = scorer.get("icons") or []
    for ic in icons:
        tooltip = (ic.get("tooltip") or "").lower()
        type_id = str(ic.get("typeId"))
        if type_id == "15" or "not starting" in tooltip:
            return LineupStatus.BENCH
    for ic in icons:
        tooltip = (ic.get("tooltip") or "").lower()
        type_id = str(ic.get("typeId"))
        if type_id == "12" or "starting in upcoming" in tooltip:
            return LineupStatus.STARTING
    return None

def _pick_optimized_lineup(
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    projections: dict,
) -> tuple[list[str], list[dict], list[dict], bool]:
    """
    Returns (active_ids, active_rows, reserve_rows, missing_gk_flag)
    Rows include display info for the UI.
    """
    players: list[tuple] = []
    now = datetime.now(timezone.utc)
    missing_confirmed: list[str] = []
    for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
        row = player_lookup.get(pid)
        if not row or not getattr(row, "player", None):
            continue
        info = lineup_info_by_player.get(pid)
        kickoff = getattr(info, "kickoff", None) if info else None
        proj_row = _player_projection_row(info, projections=projections, fallback_name=row.player.name)
        proj = None
        proj_gs = None
        if proj_row:
            try:
                proj = float(proj_row.get("ProjFPts"))
            except Exception:
                proj = None
            try:
                proj_gs = int(proj_row.get("ProjGS"))
            except Exception:
                proj_gs = None
        gs_val, gs_source = _resolve_gs(info, proj_gs, row)
        if kickoff and kickoff < now and not gs_source.startswith("Confirmed"):
            missing_confirmed.append(row.player.name)
        pos = _display_pos(row) or ""
        players.append(
            (
                pid,
                row.player.name,
                pos.upper(),
                proj if proj is not None else 0.0,
                gs_val,
                _kickoff_or_max(info),
                info,
                gs_source,
            )
        )

    gks = [p for p in players if p[2] == "G"]
    outs = [p for p in players if p[2] != "G"]

    def sort_key(p):
        # Prefer GS==1 first, then higher proj, then earlier kickoff
        return (-p[4], -p[3], p[5])

    gks_sorted = sorted(gks, key=sort_key)
    outs_sorted = sorted(outs, key=sort_key)

    active: list[tuple] = []
    missing_gk = False
    if gks_sorted:
        active.append(gks_sorted[0])
    else:
        missing_gk = True

    active.extend(outs_sorted[:10 if gks_sorted else 10])
    active_ids = [p[0] for p in active]
    active_rows = []
    for p in active:
        info = p[6]
        active_rows.append(
            {
                "Player": p[1],
                "Pos": p[2],
                "ProjFPts": p[3],
                "GS": p[4],
                "GS Source": p[7],
                "Kickoff": _format_kickoff(getattr(info, "kickoff", None)) if info else "-",
            }
        )

    reserve_rows = []
    for p in players:
        if p[0] in active_ids:
            continue
        info = p[6]
        reserve_rows.append(
            {
                "Player": p[1],
                "Pos": p[2],
                "ProjFPts": p[3],
                "GS": p[4],
                "GS Source": p[7],
                "Kickoff": _format_kickoff(getattr(info, "kickoff", None)) if info else "-",
            }
        )

    def _sort_rows(rows: list[dict]) -> list[dict]:
        order = {"G": 0, "D": 1, "M": 2, "F": 3}
        return sorted(rows, key=lambda r: (order.get(str(r.get("Pos")).upper(), 4), r.get("Player", "")))

    if missing_confirmed:
        st.warning(
            "Missing confirmed lineup status for completed kickoffs: "
            + ", ".join(sorted(set(missing_confirmed)))
        )

    return active_ids, _sort_rows(active_rows), _sort_rows(reserve_rows), missing_gk


opt_active_ids, opt_active_rows, opt_reserve_rows, opt_missing_gk = _pick_optimized_lineup(
    roster_view,
    lineup_info_by_player,
    projections_map,
)

if opt_missing_gk:
    st.warning("No goalkeeper available; selecting top 10 outfielders. Fantrax lineup may be invalid without a GK.")

opt_cols = st.columns(2)
opt_active_height = 35 * (max(len(opt_active_rows), 11) + 1)
with opt_cols[0]:
    st.markdown("**Optimized Actives**")
    st.dataframe(
        pd.DataFrame(opt_active_rows),
        use_container_width=True,
        hide_index=True,
        height=opt_active_height,
    )
with opt_cols[1]:
    st.markdown("**Optimized Reserves**")
    st.dataframe(pd.DataFrame(opt_reserve_rows), use_container_width=True, hide_index=True)

def _apply_optimized_lineup(
    *,
    subs_service: SubsService,
    roster: Roster,
    league_id: str,
    team_id: str,
    period_id: Optional[int],
    desired_actives: list[str],
) -> dict:
    """
    Build a field map with desired starters and apply via Fantrax confirm/execute.
    """
    try:
        field_map = subs_service.build_field_map(roster, desired_actives)
    except Exception as exc:
        return {"ok": False, "reason": f"field_map_error: {exc}", "confirm": {}}
    try:
        confirm = subs_service.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=team_id,
            roster_limit_period=int(period_id) if period_id is not None else None,
            field_map=field_map,
            apply_to_future=False,
            do_finalize=True,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"confirm_execute_error: {exc}", "confirm": {}}
    fantasy_response = confirm.get("fantasyResponse") if isinstance(confirm, dict) else {}
    illegal_msgs = fantasy_response.get("illegalRosterMsgs") or []
    ok = bool(confirm.get("ok", False)) and not illegal_msgs
    return {"ok": ok, "reason": "ok" if ok else "illegal", "confirm": confirm, "illegal_msgs": illegal_msgs}


apply_col1, apply_col2 = st.columns([1, 2])
with apply_col1:
    current_starters_set = set(roster_view.active_player_ids())
    desired_set = set(opt_active_ids)
    already_optimal = bool(desired_set) and current_starters_set == desired_set
    apply_disabled = already_optimal or not opt_active_ids
    apply_label = "Lineup already optimized" if already_optimal else "Apply optimized lineup to Fantrax"
    apply_type = "secondary" if already_optimal else "primary"
    if st.button(apply_label, type=apply_type, key="apply_optimized_lineup", disabled=apply_disabled):
        if swap_period_int is None:
            st.error("Cannot apply lineup: no valid Fantrax period selected.")
        elif not opt_active_ids:
            st.error("Cannot apply lineup: no eligible players found.")
        else:
            subs_service = SubsService(session=session)
            result = _apply_optimized_lineup(
                subs_service=subs_service,
                roster=roster,
                league_id=league_id,
                team_id=team_id,
                period_id=swap_period_int,
                desired_actives=opt_active_ids,
            )
            if result.get("ok"):
                st.success("Optimized lineup applied to Fantrax.")
                _safe_rerun()
            else:
                st.error(f"Failed to apply lineup: {result.get('reason')}")
                if result.get("illegal_msgs"):
                    st.warning(f"Illegal roster messages: {result.get('illegal_msgs')}")
if already_optimal:
    st.caption("Lineup already optimized based on current projections and confirmed status.")

# ----------------------------------------------------------------------
# Create Rule (main)
# ----------------------------------------------------------------------
st.divider()
st.subheader("Create Rule")
st.caption(
    "Active-first: fire when active is not starting. Reserve-first: fire when selected reserve is starting. "
    "Select an active or reserve to filter eligible counterparts. "
    "Backup priority follows click order and must be non-decreasing by KOS."
)

if st.session_state.pop("show_advanced_rule_dialog", False):
    st.session_state["show_saved_rule_notice_inline"] = True
if st.session_state.pop("show_saved_rule_notice_inline", False):
    with st.container(border=True):
        _show_saved_rule_notice_inline()

projections_map = _get_projections_map()

def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

def _row_projection(
    info: Optional[PlayerLineupInfo], *, fallback_name: str
) -> tuple[Optional[float], Optional[float]]:
    row = _player_projection_row(info, projections=projections_map, fallback_name=fallback_name)
    if not row:
        return None, None
    return _safe_float(row.get("ProjFPts")), _safe_float(row.get("ProjGS"))

def _is_backup_eligible(active_id: str, reserve_id: str, *, reserve_first: bool = False) -> bool:
    if not active_id or not reserve_id:
        return False
    if not selected_period_id:
        return False
    active_row = player_lookup.get(active_id)
    reserve_row = player_lookup.get(reserve_id)
    if not active_row:
        return False
    if hasattr(roster_view, "lock_flags"):
        active_flags = roster_view.lock_flags(
            active_id,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    else:
        active_flags = get_row_lock_flags(
            active_row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    if active_flags.get("fx_locked") or active_flags.get("visually_locked"):
        return False
    if not reserve_row:
        return False
    if hasattr(roster_view, "lock_flags"):
        reserve_flags = roster_view.lock_flags(
            reserve_id,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    else:
        reserve_flags = get_row_lock_flags(
            reserve_row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )
    if reserve_flags.get("fx_locked") or reserve_flags.get("visually_locked"):
        return False
    active_pos = _display_pos(active_row).upper()
    reserve_pos = _display_pos(reserve_row).upper()
    if active_pos in {"G", "GK"} and reserve_pos not in {"G", "GK"}:
        return False
    if reserve_first:
        active_info = lineup_info_by_player.get(active_id)
        reserve_info = lineup_info_by_player.get(reserve_id)
        if (
            active_info
            and reserve_info
            and active_info.kickoff
            and reserve_info.kickoff
            and active_info.kickoff < reserve_info.kickoff - timedelta(hours=1.25)
        ):
            return False
    if would_break_mandatory_slots(roster_view, active_id, reserve_id, min_gks=1):
        return False
    return can_swap_in_period(
        subs_service=subs_service,
        roster=roster,
        league_id=league_id,
        team_id=team_id,
        active_id=active_id,
        reserve_id=reserve_id,
        period_id=selected_period_id or "",
    )

adv_active_id = st.session_state.get("advanced_active_id")
adv_backup_order: list[str] = st.session_state.get("advanced_backup_order", []) or []
prev_adv_active_id = adv_active_id
prev_adv_backup_order = list(adv_backup_order)
adv_trigger_mode = st.session_state.get("advanced_trigger_mode")

active_ids = [str(pid) for pid in roster_view.active_player_ids()]
reserve_ids = [str(pid) for pid in roster_view.reserve_player_ids()]
adv_active_labels: dict[str, str] = {}

focus_reserve_id = None if adv_active_id else (adv_backup_order[0] if adv_backup_order else None)
reserve_first_filter = bool(focus_reserve_id)
eligible_active_ids = None
eligible_reserve_ids = None

if adv_active_id:
    eligible_reserve_ids = [pid for pid in reserve_ids if _is_backup_eligible(adv_active_id, pid)]
elif focus_reserve_id:
    eligible_active_ids = [
        pid
        for pid in active_ids
        if _is_backup_eligible(pid, focus_reserve_id, reserve_first=reserve_first_filter)
    ]

if eligible_active_ids is not None:
    active_ids = [pid for pid in active_ids if pid in eligible_active_ids]
if eligible_reserve_ids is not None:
    reserve_ids = [pid for pid in reserve_ids if pid in eligible_reserve_ids]

active_rows = []
for pid in active_ids:
    row = player_lookup.get(pid)
    if not row:
        continue
    info = lineup_info_by_player.get(pid)
    proj_fpts, proj_gs = _row_projection(info, fallback_name=row.player.name)
    team_name, opponent = _team_and_opponent_for_player(
        pid, lineup_info_by_player, player_lookup
    )
    active_rows.append(
        {
            "player_id": pid,
            "Select": pid == adv_active_id,
            "Player": row.player.name,
            "ProjFPts": proj_fpts,
            "Pos": _display_pos(row),
            "Team": team_name,
            "Opponent": opponent,
            "KOS": kos_map.get(pid),
            "Kickoff": _format_kickoff(getattr(info, "kickoff", None)) if info else "—",
        }
    )
    adv_active_labels[pid] = f"{row.player.name} ({_display_pos(row)})"

reserve_rows = []
for pid in reserve_ids:
    row = player_lookup.get(pid)
    if not row:
        continue
    info = lineup_info_by_player.get(pid)
    proj_fpts, proj_gs = _row_projection(info, fallback_name=row.player.name)
    team_name, opponent = _team_and_opponent_for_player(
        pid, lineup_info_by_player, player_lookup
    )
    reserve_rows.append(
        {
            "player_id": pid,
            "Select": pid in adv_backup_order,
            "Player": row.player.name,
            "ProjFPts": proj_fpts,
            "Pos": _display_pos(row),
            "Team": team_name,
            "Opponent": opponent,
            "KOS": kos_map.get(pid),
            "Kickoff": _format_kickoff(getattr(info, "kickoff", None)) if info else "—",
        }
    )

def _swap_rank_key(row: dict) -> tuple:
    proj = row.get("ProjFPts")
    proj_val = proj if isinstance(proj, (int, float)) else -1e9
    return (
        -proj_val,
        row.get("KOS") is None,
        row.get("KOS") or 0,
        row.get("Player") or "",
    )

def _format_rule_preview_line(
    active_name: str,
    reserve_name: str,
    trigger_mode: Optional[str],
) -> str:
    is_reserve_trigger = trigger_mode == "reserve"
    condition = (
        "Reserve player confirmed starting"
        if is_reserve_trigger
        else "Active player confirmed not starting"
    )
    return (
        "Active player "
        f"{active_name} to be replaced by Reserve player {reserve_name} "
        f"on condition {condition}"
    )


def _format_saved_rule_sentence(
    active_label: str,
    reserve_label: str,
    condition_value: Optional[str],
) -> str:
    if condition_value == SwapCondition.RESERVE_STARTING.value:
        condition = "Reserve player confirmed starting"
    else:
        condition = "Active player confirmed not starting"
    return (
        "Active player "
        f"{active_label} to be replaced by Reserve player {reserve_label} "
        f"on condition {condition}"
    )

active_rows.sort(key=_swap_rank_key)
reserve_rows.sort(key=_swap_rank_key)

col_active, col_reserve = st.columns(2)
with col_active:
    st.markdown("**Active XI**")
    # if not adv_active_id and not adv_backup_order:
    #     st.caption("Pick one active or reserve to filter the opposite list.")
    if not active_rows:
        st.info("No eligible active players available for the current selection.")
        edited_active = pd.DataFrame(columns=["Select"])
    else:
        active_df = pd.DataFrame(active_rows).set_index("player_id")
        edited_active = st.data_editor(
            active_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("Pick", help="Select the active player to replace."),
                "Player": st.column_config.TextColumn("Player", disabled=True),
                "ProjFPts": st.column_config.NumberColumn("ProjFPts", disabled=True),
                "Pos": st.column_config.TextColumn("Pos", disabled=True),
                "Team": st.column_config.TextColumn("Team", disabled=True),
                "Opponent": st.column_config.TextColumn("Opponent", disabled=True),
                "KOS": st.column_config.NumberColumn("KOS", disabled=True),
                "Kickoff": st.column_config.TextColumn("Kickoff", disabled=True),
            },
            key="advanced_active_editor",
        )

with col_reserve:
    st.markdown("**Reserves**")
    if not reserve_rows:
        st.info("No eligible reserves available for the current selection.")
        edited_reserve = pd.DataFrame(columns=["Select"])
    else:
        reserve_df = pd.DataFrame(reserve_rows).set_index("player_id")
        edited_reserve = st.data_editor(
            reserve_df,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Select": st.column_config.CheckboxColumn("Pick", help="Select backups in priority order."),
                "Player": st.column_config.TextColumn("Player", disabled=True),
                "ProjFPts": st.column_config.NumberColumn("ProjFPts", disabled=True),
                "Pos": st.column_config.TextColumn("Pos", disabled=True),
                "Team": st.column_config.TextColumn("Team", disabled=True),
                "Opponent": st.column_config.TextColumn("Opponent", disabled=True),
                "KOS": st.column_config.NumberColumn("KOS", disabled=True),
                "Kickoff": st.column_config.TextColumn("Kickoff", disabled=True),
            },
            key="advanced_reserve_editor",
        )

selected_active_ids = [
    str(pid)
    for pid, row in edited_active.iterrows()
    if row.get("Select")
]
new_active_id = selected_active_ids[0] if selected_active_ids else None
active_changed = new_active_id != adv_active_id
if active_changed:
    adv_active_id = new_active_id
    adv_backup_order = prev_adv_backup_order[:1] if prev_adv_backup_order else []

selected_reserve_ids = [
    str(pid)
    for pid, row in edited_reserve.iterrows()
    if row.get("Select")
]
ordered_selected_reserve_ids = [
    str(pid)
    for pid, row in edited_reserve.iterrows()
    if row.get("Select")
]

prev_set = set(adv_backup_order)
new_set = set(selected_reserve_ids)
removed = [pid for pid in adv_backup_order if pid not in new_set]
if removed:
    adv_backup_order = [pid for pid in adv_backup_order if pid in new_set]

added = [pid for pid in ordered_selected_reserve_ids if pid not in prev_set]
kos_blocked = False
for pid in added:
    if not adv_active_id and adv_backup_order:
        continue
    if adv_backup_order:
        last_kos = kos_map.get(adv_backup_order[-1])
        next_kos = kos_map.get(pid)
        if last_kos is not None and next_kos is not None and next_kos < last_kos:
            kos_blocked = True
            continue
    adv_backup_order.append(pid)

if kos_blocked:
    st.warning("Backup priority must be in KOS order. Selections with earlier KOS were ignored.")
if not adv_active_id and len(adv_backup_order) > 1:
    adv_backup_order = adv_backup_order[:1]

if active_changed:
    adv_trigger_mode = "reserve" if prev_adv_backup_order else "active"
elif not adv_active_id and selected_reserve_ids:
    adv_trigger_mode = "reserve"
elif adv_active_id and not adv_trigger_mode:
    adv_trigger_mode = "active"
elif not adv_active_id and not selected_reserve_ids:
    adv_trigger_mode = None

if adv_trigger_mode == "reserve" and len(adv_backup_order) > 1:
    adv_backup_order = adv_backup_order[:1]

st.session_state["advanced_active_id"] = adv_active_id
st.session_state["advanced_backup_order"] = adv_backup_order
st.session_state["advanced_trigger_mode"] = adv_trigger_mode
if adv_active_id != prev_adv_active_id or adv_backup_order != prev_adv_backup_order:
    _safe_rerun()

adv_period_id = None
adv_period_label = ""
if period_id_map:
    period_choices = list(period_id_map.keys())
    default_period_id = st.session_state.get("advanced_rule_period_id") or st.session_state.get("selected_gameweek_period_id")
    if not default_period_id or default_period_id not in period_id_map:
        default_period_id = period_choices[0]
    adv_period_id = st.selectbox(
        "Apply rule during period",
        options=period_choices,
        index=period_choices.index(default_period_id),
        format_func=lambda pid: period_id_map.get(pid, pid),
        key="advanced_rule_period_select",
    )
    adv_period_label = period_id_map.get(adv_period_id, "")
    st.session_state["advanced_rule_period_id"] = adv_period_id
else:
    st.error("No roster-change periods available. Visit the Overview page to refresh your session.")

adv_submit_disabled = not (
    user_id
    and adv_active_id
    and adv_backup_order
    and adv_period_id
)
if not state_writer_enabled:
    adv_submit_disabled = True
adv_submitted = st.button("Save Rule", disabled=adv_submit_disabled, type="primary", key="save_rule_adv_btn")

st.caption(
    "Test fire applies a live swap now (ignores conditions). Swap back after testing so the rule can still trigger."
)
test_fire_disabled = not (adv_active_id and adv_backup_order and adv_period_id)
test_fire = st.button(
    "Test fire swap now",
    disabled=(test_fire_disabled or not state_writer_enabled),
    type="secondary",
    key="test_fire_adv_btn",
)
if test_fire:
    if not adv_active_id or not adv_backup_order:
        st.error("Select an active player and at least one backup before testing.")
    else:
        test_reserve_id = adv_backup_order[0]
        test_period_id = adv_period_id
        try:
            test_period_int = int(test_period_id)
        except Exception:
            test_period_int = None
        if test_period_int is None:
            st.error("Cannot test fire: invalid period.")
        else:
            legal = can_swap_in_period(
                subs_service=subs_service,
                roster=roster,
                league_id=league_id,
                team_id=team_id,
                active_id=adv_active_id,
                reserve_id=test_reserve_id,
                period_id=str(test_period_id),
            )
            if not legal:
                st.error("Cannot test fire: swap is not legal right now.")
            else:
                result = subs_service.swap_players(
                    team_id=team_id,
                    out_player_id=adv_active_id,
                    in_player_id=test_reserve_id,
                    period=test_period_int,
                )
                if result.get("success"):
                    _record_immediate_swap_event(
                        action="test_fire",
                        out_player_id=adv_active_id,
                        in_player_id=test_reserve_id,
                        period=test_period_int,
                    )
                    st.success("Test swap executed in Fantrax.")
                    st.warning("Swap back manually so the rule can still trigger later.")
                    _safe_rerun()
                else:
                    st.error(
                        f"Test swap failed: {result.get('error') or result.get('message') or 'Unknown error'}"
                    )

if adv_submitted and not adv_submit_disabled:
    try:
        existing_rules = _load_rules_for_user_with_revision(str(user_id))
    except Exception:
        existing_rules = []
    already_exists = False
    for r in existing_rules:
        if not _is_manual_rule(r):
            continue
        if str(r.get("league_id")) != str(league_id) or str(r.get("team_id")) != str(team_id):
            continue
        if str(r.get("active_id")) != str(adv_active_id):
            continue
        if str(r.get("period")) != str(adv_period_id):
            continue
        if _normalized_rule_state(r.get("state")) != "fired":
            already_exists = True
            break
    if already_exists:
        st.warning("A manual rule already exists for this active player and period.")
    else:
        group_id = uuid.uuid4().hex
        active_row = player_lookup.get(adv_active_id)
        active_label = adv_active_labels.get(adv_active_id, adv_active_id)
        active_name = active_row.player.name if active_row else str(adv_active_id)
        active_row_pos = _display_pos(active_row) if active_row else ""
        if active_row_pos and "(" not in active_label:
            active_label = _format_player_label(active_label, active_row_pos)
        to_persist = []
        preview_lines = []
        for index, pid in enumerate(adv_backup_order):
            backup_row = player_lookup.get(pid)
            backup_name = backup_row.player.name if backup_row else pid  # type: ignore[union-attr]
            backup_pos = _display_pos(backup_row) if backup_row else ""
            in_label = _format_player_label(backup_name, backup_pos)
            preview_lines.append(
                _format_rule_preview_line(active_name, str(backup_name), adv_trigger_mode)
            )
            condition_val = (
                SwapCondition.RESERVE_STARTING.value
                if adv_trigger_mode == "reserve"
                else SwapCondition.NOT_STARTING.value
            )
            rec = {
                "active_id": adv_active_id,
                "reserve_id": pid,
                "out_label": active_label,
                "in_label": in_label,
                "priority": index + 1,
                "period": adv_period_id,
                "period_label": adv_period_label,
                "trigger": "confirmed_lineup",
                "condition": condition_val,
                "max_fires": 1,
                "league_id": league_id,
                "team_id": team_id,
                "user_id": str(user_id),
                "source": "manual",
                "source_type": 3,
                "group_id": group_id,
                "state": "active",
            }
            to_persist.append(rec)
        _apply_user_rule_ops(
            user_id=str(user_id),
            operations=[{"op": "upsert_rules", "items": to_persist}],
        )
        st.session_state["advanced_rule_preview_lines"] = preview_lines
        st.session_state["show_advanced_rule_dialog"] = True
        st.success("Rule saved successfully.")
        st.rerun()

preview_lines = st.session_state.get("advanced_rule_preview_lines")
if preview_lines:
    st.markdown("**Latest saved rule**")
    st.markdown("\n".join(f"{index + 1}. {line}" for index, line in enumerate(preview_lines)))

st.subheader("Make any final lineup tweaks")
# Expander
with st.expander("Make a single swap", expanded=False):
    st.caption("Swap one player for another immediately; this writes directly to Fantrax.")

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

    if st.button(
        "Execute swap now",
        type="primary",
        key="immediate_swap_button_top",
        disabled=not state_writer_enabled,
    ):
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
                        _record_immediate_swap_event(
                            action="immediate_swap",
                            out_player_id=swap_active,
                            in_player_id=swap_reserve,
                            period=swap_period_int,
                        )
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
# st.subheader("Current Lineup Snapshot")
# st.write(
#     "This snapshot reflects your current roster status and metadata. "
#     "It is the baseline used for conditional swap decisions and the handoff point for optimization."
# )
# latest_lineup_fetch = _latest_lineup_fetch_timestamp(Path(DEFAULT_LINEUPS_DIR))
# if latest_lineup_fetch:
#     st.caption(f"Predicted lineups refreshed on {latest_lineup_fetch.strftime('%Y-%m-%d %H:%M:%S %Z')}")
# else:
#     st.caption("Predicted lineups refresh time unavailable.")

# with st.expander("What do these columns mean?"):
#     st.markdown(
#         "- SS / FX: numeric code (1 starter, 0 bench, -1 out) from SofaScore/Fantrax signals.\n"
#         "- Kickoff: Effective kickoff used for ordering and locking heuristics.\n"
#         "- FX locked / markers: Fantrax disableLineupChange and visual markers that indicate player is locked and cannot be changed."
#     )

# active_display = []
# for pid in roster_view.active_player_ids():
#     row = player_lookup.get(pid)
#     if not row:
#         continue
#     info = lineup_info_by_player.get(pid)
#     team_name, opponent = _team_and_opponent_for_player(
#         pid, lineup_info_by_player, player_lookup
#     )
#     if hasattr(roster_view, "lock_flags"):
#         lock_flags = roster_view.lock_flags(
#             pid,
#             now=now,
#             lineup_info_by_player=lineup_info_by_player,
#         )
#     else:  # fallback for older RosterView without lock_flags
#         lock_flags = get_row_lock_flags(
#             row,
#             now=now,
#             lineup_info_by_player=lineup_info_by_player,
#         )
#     locked = lock_flags.get("fx_locked", False)
#     visually_locked = lock_flags.get("visually_locked", False)
#     effective_kickoff = None if visually_locked else (info.kickoff if info else None)
#     ss_effective = None
#     fx_effective = getattr(info, "fx_status", None) if info else None
#     if info:
#         if getattr(info, "ss_conf_status", None):
#             ss_effective = getattr(info, "ss_conf_status")
#         elif getattr(info, "ss_pred_status", None):
#             ss_effective = getattr(info, "ss_pred_status")
#         else:
#             ss_effective = getattr(info, "ss_status", None)
#     ss_code = _status_code(ss_effective)
#     fx_code = _status_code(fx_effective)
#     active_display.append(
#         {
#             "Player": row.player.name,
#             "Pos": _display_pos(row),
#             "Team": team_name,
#             "Opponent": opponent,
#             "SS": ss_code,
#             "FX": fx_code,
#             "Kickoff": _format_kickoff(effective_kickoff),
#             "FX locked": "Yes" if locked else "No",
#             "Kickoff passed": "Yes" if lock_flags.get("kickoff_passed") else "No",
#             "Finished marker": "Yes" if lock_flags.get("finished_marker") else "No",
#             "Visually locked": "Yes" if visually_locked else "No",
#         }
#     )

# reserve_display = []
# for pid in roster_view.reserve_player_ids():
#     row = player_lookup.get(pid)
#     if not row:
#         continue
#     info = lineup_info_by_player.get(pid)
#     team_name, opponent = _team_and_opponent_for_player(
#         pid, lineup_info_by_player, player_lookup
#     )
#     if hasattr(roster_view, "lock_flags"):
#         lock_flags = roster_view.lock_flags(
#             pid,
#             now=now,
#             lineup_info_by_player=lineup_info_by_player,
#         )
#     else:
#         lock_flags = get_row_lock_flags(
#             row,
#             now=now,
#             lineup_info_by_player=lineup_info_by_player,
#         )
#     locked = lock_flags.get("fx_locked", False)
#     visually_locked = lock_flags.get("visually_locked", False)
#     effective_kickoff = None if visually_locked else (info.kickoff if info else None)
#     ss_effective = None
#     fx_effective = getattr(info, "fx_status", None) if info else None
#     if info:
#         if getattr(info, "ss_conf_status", None):
#             ss_effective = getattr(info, "ss_conf_status")
#         elif getattr(info, "ss_pred_status", None):
#             ss_effective = getattr(info, "ss_pred_status")
#         else:
#             ss_effective = getattr(info, "ss_status", None)
#     ss_code = _status_code(ss_effective)
#     fx_code = _status_code(fx_effective)
#     reserve_display.append(
#         {
#             "Player": row.player.name,
#             "Pos": _display_pos(row),
#             "Team": team_name,
#             "Opponent": opponent,
#             "SS": ss_code,
#             "FX": fx_code,
#             "Kickoff": _format_kickoff(effective_kickoff),
#             "FX locked": "Yes" if locked else "No",
#             "Kickoff passed": "Yes" if lock_flags.get("kickoff_passed") else "No",
#             "Finished marker": "Yes" if lock_flags.get("finished_marker") else "No",
#             "Visually locked": "Yes" if visually_locked else "No",
#         }
#     )

# formation = (
#     f"Formation: GK {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'G')} "
#     f"/ DEF {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'D')} "
#     f"/ MID {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'M')} "
#     f"/ FWD {sum(1 for pid in roster_view.active_player_ids() if getattr(player_lookup.get(pid).pos, 'short_name', '').upper() == 'F')}"
# )
# st.caption(formation)

# active_df = pd.DataFrame(active_display)
# reserve_df = pd.DataFrame(reserve_display)

# st.caption("Active XI (codes: 1 starter, 0 bench, -1 out; confirmed=1 only when provider marks confirmed)")
# st.dataframe(
#     active_df,
#     use_container_width=True,
#     hide_index=True,
#     column_config={
#         "Team": st.column_config.Column(width="small"),
#         "SS": st.column_config.Column(width="small"),
#         "FX": st.column_config.Column(width="small"),
#         "Kickoff": st.column_config.Column(width="medium"),
#     },
# )
# st.caption("Reserves (codes: 1 starter, 0 bench, -1 out; confirmed=1 only when provider marks confirmed)")
# st.dataframe(
#     reserve_df,
#     use_container_width=True,
#     hide_index=True,
#     column_config={
#         "Team": st.column_config.Column(width="small"),
#         "SS": st.column_config.Column(width="small"),
#         "FX": st.column_config.Column(width="small"),
#         "Kickoff": st.column_config.Column(width="medium"),
#     },
# )

# with st.expander("🔍 Debug: Team/Opponent context"):
#     debug_rows = []
#     for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
#         row = player_lookup.get(pid)
#         if not row:
#             continue
#         info = lineup_info_by_player.get(pid)
#         if hasattr(roster_view, "lock_flags"):
#             lock_flags = roster_view.lock_flags(
#                 pid,
#                 now=now,
#                 lineup_info_by_player=lineup_info_by_player,
#             )
#         else:
#             lock_flags = get_row_lock_flags(
#                 row,
#                 now=now,
#                 lineup_info_by_player=lineup_info_by_player,
#             )
#         team_name, opponent = _team_and_opponent_for_player(
#             pid, lineup_info_by_player, player_lookup
#         )
#         debug_rows.append(
#             {
#                 "Player": row.player.name,
#                 "Team": team_name,
#                 "Opponent": opponent,
#                 "Effective status": _format_status(info.status) if info else "Unknown",
#                 "Kickoff": _fmt_debug_datetime(getattr(info, "kickoff", None)) if info else None,
#                 "FX locked": bool(lock_flags.get("fx_locked")),
#                 "event_id": getattr(info, "event_id", None) if info else None,
#                 "status_source": getattr(info, "status_source", None) if info else None,
#                 "team_name": getattr(info, "team_name", None) if info else None,
#                 "opponent_name": getattr(info, "opponent_name", None) if info else None,
#                 "is_home": getattr(info, "is_home", None) if info else None,
#                 "opponent_source": _opponent_source(info),
#             }
#         )
#     if debug_rows:
#         st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)
#     else:
#         st.caption("No lineup snapshot data available.")

# ----------------------------------------------------------------------
# Suggested Conditional Swaps (quick picks)
# ----------------------------------------------------------------------
st.subheader("Suggested Conditional Swaps")
st.caption(
    "Suggestions based on kickoff order, projections, and league legality. "
    "We prioritize same-KOS backups first, then later KOS, and rank by ProjFPts within each slot; "
    "Fantrax legality (including positional mins/maxes) is enforced via confirm checks."
)

suggestions: list[dict] = []
active_candidates = []
reserve_candidates = []
active_debug = []
reserve_debug = []
odds_map = st.session_state.get("ss_event_odds", {})
# If no odds loaded, attempt to fetch via API-Football using available event data
if not odds_map:
    event_idx = _build_event_index_from_lineups(lineup_info_by_player)
    season_guess = st.session_state.get("sofascore_season_id")
    fetched = _build_event_odds_map_from_api_football(event_idx, season=season_guess)
    if fetched:
        odds_map = fetched
        st.session_state["ss_event_odds"] = fetched

projections_map = _get_projections_map()

team_strength_map = st.session_state.get("team_strength_map")
if team_strength_map is None:
    team_strength_map = _load_team_strength_map()
    st.session_state["team_strength_map"] = team_strength_map

missing_projections_debug: list[dict] = []
status_changes: list[dict] = []

# Track status changes after refresh
status_changes: list[dict] = []
current_status_map: dict[str, tuple] = {}
for pid, info in lineup_info_by_player.items():
    if not info:
        continue
    key = str(pid)
    current_status_map[key] = (
        getattr(info, "status", None),
        getattr(info, "ss_status", None),
        getattr(info, "ss_pred_status", None),
        getattr(info, "ss_conf_status", None),
    )
    old = prev_status_map.get(key)
    if old and old != current_status_map[key]:
        status_changes.append(
            {
                "Player": player_lookup.get(pid).player.name if pid in player_lookup else pid,
                "From": old,
                "To": current_status_map[key],
                "Note": getattr(info, "note", None),
                "Source": getattr(info, "status_source", None),
            }
    )
st.session_state["prev_lineup_statuses"] = current_status_map


def _order_reserve_candidates_by_kos(
    candidates: list[dict],
    active_kos_index: Optional[int],
) -> list[dict]:
    """
    Order reserves with same-KOS first, then later KOS, using ProjFPts as tie-breaker.
    """
    def sort_key(item: dict) -> tuple:
        proj_val = item.get("proj_fpts")
        try:
            proj_val = float(proj_val)
        except Exception:
            proj_val = 0.0
        return (-proj_val, str(item.get("name") or ""))

    if active_kos_index is None:
        return sorted(candidates, key=sort_key)

    same_kos = [c for c in candidates if c.get("kos_index") == active_kos_index]
    later_kos = [
        c
        for c in candidates
        if c.get("kos_index") is not None and c.get("kos_index") > active_kos_index
    ]
    unknown_kos = [c for c in candidates if c.get("kos_index") is None]

    ordered: list[dict] = sorted(same_kos, key=sort_key)
    for kos in sorted({c.get("kos_index") for c in later_kos}):
        group = [c for c in later_kos if c.get("kos_index") == kos]
        ordered.extend(sorted(group, key=sort_key))
    ordered.extend(sorted(unknown_kos, key=sort_key))
    return ordered
# Build candidate pools only if we have a valid period (legality checks depend on it).
if swap_period_int is not None:
    for pid in roster_view.active_player_ids():
        row = player_lookup.get(pid)
        info = lineup_info_by_player.get(pid)
        if not row:
            continue
        if hasattr(roster_view, "lock_flags"):
            lock_flags = roster_view.lock_flags(pid, now=now, lineup_info_by_player=lineup_info_by_player)
        else:
            lock_flags = get_row_lock_flags(row, now=now, lineup_info_by_player=lineup_info_by_player)
        if lock_flags.get("fx_locked") or lock_flags.get("visually_locked"):
            active_debug.append(
                {"Player": row.player.name, "Reason": "locked", "Code": "-", "KO": "-"}
            )
            continue
        ko = info.kickoff if info else None
        if not ko:
            active_debug.append({"Player": row.player.name, "Reason": "no kickoff", "Code": "-", "KO": "-"})
            continue
        proj_row = _player_projection_row(
            info,
            projections=projections_map,
            fallback_name=row.player.name,
        )
        proj_fpts = None
        proj_gs = None
        if proj_row:
            try:
                proj_fpts = float(proj_row.get("ProjFPts"))
            except Exception:
                proj_fpts = None
            try:
                proj_gs = int(proj_row.get("ProjGS"))
            except Exception:
                proj_gs = None
        active_candidates.append(
            {
                "pid": pid,
                "name": row.player.name,
                "pos": _display_pos(row),
                "kickoff": ko,
                "info": info,
                "proj_fpts": proj_fpts,
                "proj_gs": proj_gs,
                "kos_index": kos_map.get(str(pid)),
            }
        )
        active_debug.append(
            {
                "Player": row.player.name,
                "Pos": _display_pos(row),
                "KO": _format_kickoff(ko),
                "Reason": "eligible",
            }
        )

    for pid in roster_view.reserve_player_ids():
        row = player_lookup.get(pid)
        info = lineup_info_by_player.get(pid)
        if not row:
            continue
        if hasattr(roster_view, "lock_flags"):
            lock_flags = roster_view.lock_flags(pid, now=now, lineup_info_by_player=lineup_info_by_player)
        else:
            lock_flags = get_row_lock_flags(row, now=now, lineup_info_by_player=lineup_info_by_player)
        if lock_flags.get("fx_locked") or lock_flags.get("visually_locked"):
            reserve_debug.append(
                {"Player": row.player.name, "Reason": "locked", "Code": "-", "KO": "-"}
            )
            continue
        ko = info.kickoff if info else None
        if not ko:
            reserve_debug.append({"Player": row.player.name, "Reason": "no kickoff", "Code": "-", "KO": "-"})
            continue
        has_projection = _has_projection_row(
            info,
            projections=projections_map,
            fallback_name=row.player.name,
        )
        if not has_projection:
            missing_projections_debug.append(
                {
                    "Player": row.player.name,
                    "Team": getattr(info, "team_name", None) if info else None,
                    "Key": _normalize_player_name(row.player.name),
                    "Usage": "excluded_from_auto_swaps",
                }
            )
            reserve_debug.append(
                {
                    "Player": row.player.name,
                    "Reason": "missing projection (excluded)",
                    "Code": "-",
                    "KO": _format_kickoff(ko),
                }
            )
            continue
        proj_row = _player_projection_row(
            info,
            projections=projections_map,
            fallback_name=row.player.name,
        )
        proj_fpts = None
        proj_gs = None
        if proj_row:
            try:
                proj_fpts = float(proj_row.get("ProjFPts"))
            except Exception:
                proj_fpts = None
            try:
                proj_gs = int(proj_row.get("ProjGS"))
            except Exception:
                proj_gs = None
        reserve_candidates.append(
            {
                "pid": pid,
                "name": row.player.name,
                "pos": _display_pos(row),
                "kickoff": ko,
                "info": info,
                "proj_fpts": proj_fpts,
                "proj_gs": proj_gs,
                "kos_index": kos_map.get(str(pid)),
            }
        )
        reserve_debug.append(
            {
                "Player": row.player.name,
                "Pos": _display_pos(row),
                "KO": _format_kickoff(ko),
                "Reason": "eligible",
            }
        )

# sort and build suggestions only if we have a valid period
if swap_period_int is not None:
    active_candidates.sort(key=lambda r: r["kickoff"])
    reserve_candidates.sort(key=lambda r: r["kickoff"], reverse=True)
    later_matches = 0
    same_kos_matches = 0
    legal_matches = 0

    max_backups = 3
    for bad in active_candidates:
        if not bad["kickoff"] or bad["kickoff"] <= now:
            continue
        active_kos_index = kos_map.get(str(bad["pid"]))
        eligible_reserves: list[dict] = []
        for good in reserve_candidates:
            if not good["kickoff"] or good["kickoff"] <= now:
                continue
            if good["kickoff"] < bad["kickoff"]:
                continue
            if would_break_mandatory_slots(
                roster_view,
                bad["pid"],
                good["pid"],
                min_gks=1,
            ):
                continue
            legal = can_swap_in_period(
                subs_service=subs_service,
                roster=roster,
                league_id=league_id,
                team_id=team_id,
                active_id=bad["pid"],
                reserve_id=good["pid"],
                period_id=swap_period_int,
            )
            if not legal:
                continue
            legal_matches += 1
            if good["kickoff"] > bad["kickoff"]:
                later_matches += 1
            elif good["kickoff"] == bad["kickoff"]:
                same_kos_matches += 1
            eligible_reserves.append(good)

        ordered_reserves = _order_reserve_candidates_by_kos(eligible_reserves, active_kos_index)
        for idx, good in enumerate(ordered_reserves[:max_backups]):
            good_proj = good.get("proj_fpts")
            good_proj_gs = good.get("proj_gs")
            try:
                score = float(good_proj or 0.0)
            except Exception:
                score = 0.0
            good_odds = _odds_score(good.get("info"), odds_map)
            good_strength = _team_strength(good.get("info"), team_strength_map)
            suggestions.append(
                {
                    "Out": _format_player_label(bad["name"], bad.get("pos")),
                    "In": _format_player_label(good["name"], good.get("pos")),
                    "active_id": bad["pid"],
                    "reserve_id": good["pid"],
                    "Active": bad["name"],
                    "Swap Type": _swap_type_label(bad["pid"], [good["pid"]], kos_map),
                    "Description": _swap_description(bad["name"], [good["name"]]),
                    "Priority": idx + 1,
                    "Odds": good_odds if good_odds is not None else "",
                    "Strength": good_strength if good_strength is not None else "",
                    "ProjFPts": good_proj if good_proj is not None else "",
                    "ProjGS": good_proj_gs if good_proj_gs is not None else "",
                    "Score": score,
                    "Out KO": _format_kickoff(bad["kickoff"]),
                    "In KO": _format_kickoff(good["kickoff"]),
                    "Out KO dt": bad["kickoff"],
                    "In KO dt": good["kickoff"],
                }
            )

if swap_period_int is None:
    st.caption("Cannot suggest swaps because no valid Fantrax period is selected.")
elif suggestions:
    sugg_df = pd.DataFrame(suggestions)
    if "Select" not in sugg_df.columns:
        sugg_df["Select"] = False
    if "Priority" not in sugg_df.columns:
        sugg_df["Priority"] = 1
    # Sort by kickoff window, then preserve per-active priority ordering.
    sort_by = ["Out KO dt", "Active", "Priority"]
    sort_order = [True, True, True]
    sugg_df = sugg_df.sort_values(by=sort_by, ascending=sort_order).reset_index(drop=True)
    # Styled read-only view with kickoff gradient
    def _kickoff_gradient(series: pd.Series) -> list[str]:
        vals = series.apply(lambda d: d.timestamp() if isinstance(d, datetime) else None)
        valid = vals.dropna()
        if valid.empty:
            return [""] * len(series)
        vmin, vmax = valid.min(), valid.max()
        if vmax <= vmin:
            vmax = vmin + 1.0
        colors = ["#00c853", "#00b0ff", "#ffeb3b", "#ff9800", "#d32f2f"]  # green → blue → yellow → orange → red
        steps = [vmin + i * (vmax - vmin) / (len(colors) - 1) for i in range(len(colors))]

        def pick(v: Optional[float]) -> str:
            if v is None:
                return ""
            for idx in range(len(steps) - 1):
                if steps[idx] <= v <= steps[idx + 1]:
                    return f"background-color: {colors[idx]}; color: white;"
            return f"background-color: {colors[-1]}; color: white;"

        return [pick(v) for v in vals]

    display_cols = [
        "Out",
        "In",
        "Priority",
        "Swap Type",
        "Description",
        "ProjFPts",
        "ProjGS",
        "Score",
        "Out KO",
        "In KO",
    ]
    view_df = sugg_df.copy()
    if "In KO dt" in view_df.columns:
        display_df = view_df[display_cols + ["In KO dt"]].copy()
        display_df["ProjFPts"] = pd.to_numeric(display_df["ProjFPts"], errors="coerce")
        view_df_styled = (
            display_df.style.apply(_kickoff_gradient, subset=["In KO dt"])
            .format({"ProjFPts": "{:.1f}"}, na_rep="")
            .hide(axis="columns", subset=["In KO dt"])
        )
        st.dataframe(view_df_styled, hide_index=True, use_container_width=True)
    else:
        display_df = view_df[display_cols].copy()
        display_df["ProjFPts"] = pd.to_numeric(display_df["ProjFPts"], errors="coerce")
        st.dataframe(
            display_df.style.format({"ProjFPts": "{:.1f}"}, na_rep=""),
            hide_index=True,
            use_container_width=True,
        )

    editor_cols = [
        "Out",
        "In",
        "Select",
        "Priority",
        "Swap Type",
        "Description",
        "ProjFPts",
        "ProjGS",
        "Score",
        "Out KO",
        "In KO",
        "active_id",
        "reserve_id",
        "Active",
    ]
    editor_df = sugg_df[[col for col in editor_cols if col in sugg_df.columns]].copy()
    if "ProjFPts" in editor_df.columns:
        editor_df["ProjFPts"] = pd.to_numeric(editor_df["ProjFPts"], errors="coerce")

    edited = st.data_editor(
        editor_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", help="Choose swaps to queue as conditional rules."),
            "Out": st.column_config.TextColumn("Out", width="medium"),
            "In": st.column_config.TextColumn("In", width="medium"),
            "active_id": st.column_config.TextColumn("active_id", disabled=True),
            "reserve_id": st.column_config.TextColumn("reserve_id", disabled=True),
            "Swap Type": st.column_config.TextColumn("Swap Type", disabled=True),
            "Description": st.column_config.TextColumn("Description", disabled=True),
            "Priority": st.column_config.NumberColumn(
                "Priority",
                min_value=1,
                max_value=max(1, len(sugg_df)),
                step=1,
                help="Lower numbers are tried first for the same active.",
            ),
            "ProjFPts": st.column_config.NumberColumn("ProjFPts", format="%.1f"),
        },
        key="suggested_swaps_editor",
    )
    chosen = edited[edited["Select"]]
    if st.button("Queue selected suggested swaps as conditional rules", type="primary"):
        if chosen.empty:
            st.info("No suggested swaps selected.")
        else:
            queued = st.session_state.get("queued_suggested_swaps", [])
            chosen_sorted = chosen.copy()
            chosen_sorted["Priority"] = pd.to_numeric(chosen_sorted["Priority"], errors="coerce").fillna(
                float("inf")
            )
            chosen_sorted = chosen_sorted.sort_values(by=["active_id", "Priority"])
            to_persist = []
            for _, row in chosen_sorted.iterrows():
                user_id = st.session_state.get("user_id")
                rec = {
                    "active_id": row["active_id"],
                    "reserve_id": row["reserve_id"],
                    "out_label": row["Out"],
                    "in_label": row["In"],
                    "priority": int(row["Priority"]) if pd.notna(row["Priority"]) else None,
                    "period": swap_period_int,
                    "source_strategy": strategy.value if hasattr(strategy, "value") else str(strategy),
                    "trigger": "confirmed_lineup",  # to be consumed by conditional engine
                    "score": row.get("Score"),
                    "proj_fpts": row.get("ProjFPts"),
                    "proj_gs": row.get("ProjGS"),
                    "max_fires": 1,
                    "league_id": league_id,
                    "team_id": team_id,
                    "user_id": str(user_id) if user_id else None,
                }
                queued.append(rec)
                to_persist.append(rec)
            st.session_state["queued_suggested_swaps"] = queued
            try:
                user_id = st.session_state.get("user_id")
                if user_id:
                    if state_writer_enabled:
                        _apply_user_rule_ops(
                            user_id=str(user_id),
                            operations=[{"op": "upsert_rules", "items": to_persist}],
                        )
                    else:
                        st.warning("Read-only mode: suggested swaps were queued in session but not persisted.")
                else:
                    append_rules(
                        to_persist,
                        source="suggested_swaps",
                        source_type=1,
                    )
            except Exception:
                st.warning("Queued locally, but failed to persist conditional rules.")
            st.success(f"Queued {len(chosen)} conditional swap(s) (based on {strategy.name}).")
else:
    st.caption(
        "No suggested swaps right now (need unlocked actives and reserves with "
        "same or later kickoffs; must also be legal)."
    )
    st.caption(
        f"Unlocked actives with kickoff: {len(active_candidates)} | "
        f"Unlocked reserves with kickoff: {len(reserve_candidates)} | "
        f"Later KO matches (pos-compatible): {later_matches if swap_period_int is not None else 0} | "
        f"Same KOS matches (confirmed-only): {same_kos_matches if swap_period_int is not None else 0} | "
        f"Legal matches (Fantrax): {legal_matches if swap_period_int is not None else 0}"
    )
    if active_debug or reserve_debug:
        with st.expander("Why no suggestions? (eligibility debug)"):
            if active_debug:
                st.markdown("**Actives**")
                st.dataframe(pd.DataFrame(active_debug), hide_index=True, use_container_width=True)
            if reserve_debug:
                st.markdown("**Reserves**")
                st.dataframe(pd.DataFrame(reserve_debug), hide_index=True, use_container_width=True)
if swap_period_int is not None and active_candidates and reserve_candidates:
    if st.checkbox("Show swap legality diagnostics", key="swap_legality_debug"):
        debug_rows = _build_swap_legality_debug(
            roster=roster,
            roster_view=roster_view,
            lineup_info_by_player=lineup_info_by_player,
            subs_service=subs_service,
            league_id=league_id,
            team_id=team_id,
            swap_period_int=swap_period_int,
            active_candidates=active_candidates,
            reserve_candidates=reserve_candidates,
        )
        st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)
if missing_projections_debug:
    st.warning(
        "Players missing projections are treated as likely unavailable for this gameweek and are excluded from auto swap candidates."
    )
    with st.expander("Projection coverage debug"):
        st.dataframe(
            pd.DataFrame(missing_projections_debug).drop_duplicates(),
            hide_index=True,
            use_container_width=True,
        )

# ----------------------------------------------------------------------
# Create Rule (simple)
# ----------------------------------------------------------------------
st.divider()
st.subheader("Create Rule (simple)")

projections_map = _get_projections_map()

all_rows = [
    row
    for row in roster.rows
    if getattr(row, "player", None)
]
active_rows = [
    row
    for row in all_rows
    if getattr(row, "pos_id", "0") != "0"
]

if not active_rows:
    st.warning("No active players detected on this roster.")
    st.stop()

active_labels = {
    str(row.player.id): f"{row.player.name} ({getattr(row.pos, 'short_name', '')})"
    for row in active_rows
}
roster_labels = {
    str(row.player.id): f"{row.player.name} ({_display_pos(row)})"
    for row in all_rows
}

show_claims_builder = st.session_state.get("show_claims_builder", True)
if not claims_allowed:
    show_claims_builder = False
action_options = {
    "Lineup swap (bench player)": RuleActionType.LINEUP_SWAP,
}
if show_claims_builder:
    action_options.update(
        {
            "FA add/drop (free agent)": RuleActionType.FA_CLAIM_DROP,
        }
    )
rule_action_label = st.radio(
    "Rule action",
    options=list(action_options.keys()),
    index=0,
    help="Choose whether this rule swaps to a bench player or submits a free-agent claim/drop.",
)
selected_action_type = action_options[rule_action_label]

if selected_action_type == FA_ACTION_ADD_ONLY:
    st.info(
        "Add-only rules require an open roster slot at execution time. "
        "If no slots are open, Fantrax will reject the claim."
    )

active_player_id: Optional[str] = None
drop_player_id: Optional[str] = None
override_never_drop = False
fa_simple_mode = FA_SIMPLE_MODE_CLAIM_BASED
fa_trigger_mode = FA_TRIGGER_MODE_FA_STARTING_ONLY

if selected_action_type == RuleActionType.LINEUP_SWAP:
    active_player_id = st.selectbox(
        "Active player to monitor",
        options=list(active_labels.keys()),
        format_func=lambda pid: active_labels.get(pid, pid),
        key="conditional_active_select",
    )

if selected_action_type == RuleActionType.FA_CLAIM_DROP:
    mode_options = {
        "Conditional claim (FA status drives trigger) (recommended)": FA_SIMPLE_MODE_CLAIM_BASED,
        "Conditional drop (rostered player status drives trigger)": FA_SIMPLE_MODE_DROP_BASED,
    }
    mode_label = st.selectbox(
        "Add/Drop mode",
        options=list(mode_options.keys()),
        index=0,
        help=(
            "Conditional claim: trigger when the FA target is starting, then choose a drop candidate compatible "
            "with that FA kickoff slot. Conditional drop: trigger when the rostered drop candidate is not starting, "
            "then submit the FA claim immediately."
        ),
        key="fa_simple_mode_select",
    )
    fa_simple_mode = mode_options[mode_label]
    fa_trigger_mode = (
        FA_TRIGGER_MODE_FA_STARTING_ONLY
        if fa_simple_mode == FA_SIMPLE_MODE_CLAIM_BASED
        else FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE
    )
    if fa_simple_mode == FA_SIMPLE_MODE_DROP_BASED:
        drop_player_id = st.selectbox(
            "Roster player to drop/monitor",
            options=list(roster_labels.keys()),
            format_func=lambda pid: roster_labels.get(pid, pid),
            key="conditional_drop_select",
        )

active_row = player_lookup.get(active_player_id) if active_player_id else None
active_lineup = lineup_info_by_player.get(active_player_id) if active_player_id else None
drop_row = player_lookup.get(drop_player_id) if drop_player_id else None
drop_lineup = lineup_info_by_player.get(drop_player_id) if drop_player_id else None
claims_acknowledged = False
claims_ack_checkbox = False

if active_row:
    status_text = _format_status(active_lineup.status) if active_lineup else "Unknown"
    kickoff_text = _format_kickoff(active_lineup.kickoff if active_lineup else None)
    st.markdown(
        f"**Selected**: {active_row.player.name} (`{active_player_id}`) — lineup status: "
        f"`{status_text}` (kickoff {kickoff_text})"
    )
if drop_row:
    status_text = _format_status(drop_lineup.status) if drop_lineup else "Unknown"
    kickoff_text = _format_kickoff(drop_lineup.kickoff if drop_lineup else None)
    st.markdown(
        f"**Drop candidate**: {drop_row.player.name} (`{drop_player_id}`) — lineup status: "
        f"`{status_text}` (kickoff {kickoff_text})"
    )
    if drop_player_id in never_drop_ids:
        override_never_drop = st.checkbox(
            "Override never-drop guard for this rule",
            value=False,
            key="override_never_drop_rule",
            help="Only enable if you are intentionally dropping a never-drop player.",
        )
        if not override_never_drop:
            st.warning("This player is in your never-drop list; saving is disabled until you override.")

if user_id and claims_allowed and selected_action_type in (
    RuleActionType.FA_CLAIM_DROP,
    FA_ACTION_ADD_ONLY,
    FA_ACTION_DROP_ONLY,
):
    claims_acknowledged = user_mgr.get_claims_ack(str(user_id), str(league_id))
    if claims_acknowledged:
        st.caption("Claim/drop rules acknowledged for this league.")
    else:
        claims_ack_checkbox = st.checkbox(
            "I understand these claim/drop rules submit live Fantrax transactions.",
            value=False,
            key="claims_ack_checkbox",
        )

period_id_map: Dict[str, str] = {}
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
fa_status_map: Dict[str, object] = {}

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
                pos = _display_pos(row)
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

claim_to_status_id: Optional[str] = None
claim_position_id: Optional[str] = None
post_claim_swap_out_id: Optional[str] = None
drop_is_active = bool(drop_row and getattr(drop_row, "pos_id", "0") != "0")
open_active_slots, open_reserve_slots = _open_roster_slots(roster)

if selected_action_type in (RuleActionType.FA_CLAIM_DROP, FA_ACTION_ADD_ONLY):
    st.markdown("**Free agent candidate (conditional claim target)**")
    drop_kickoff = drop_lineup.kickoff if drop_lineup else None
    if selected_action_type == RuleActionType.FA_CLAIM_DROP and fa_simple_mode == FA_SIMPLE_MODE_DROP_BASED and not drop_kickoff:
        st.caption(
            "Drop candidate kickoff is unknown; evaluating FA targets anyway and applying kickoff ordering only when available."
        )
    if selected_action_type == FA_ACTION_ADD_ONLY and not (open_active_slots or open_reserve_slots):
        st.info("No open roster slots available for add-only claims.")
    else:
        fa_candidate_rows: List[Dict[str, str]] = []
        fa_confirmed_rows: List[Dict[str, str]] = []
        with st.spinner("Evaluating eligible free agents..."):
            try:
                fa_status_map = fetch_fa_status_map(session=session, league_id=league_id, status_filter="FREE_AGENT")
                fa_pool = waivers_service.list_players_by_name(
                    limit=150,
                    status="FREE_AGENT",
                )
            except Exception as exc:
                st.error(f"Failed to load free agent pool: {exc}")
                fa_pool = []
                fa_status_map = {}

            for p in fa_pool:
                sid = str(p.get("id"))
                snapshot = fa_status_map.get(sid) if fa_status_map else None
                status_val = getattr(snapshot, "status", None) if snapshot else None
                status_label = _format_status(status_val)
                kickoff_dt = getattr(snapshot, "kickoff", None) if snapshot else None
                # Exclude players we can positively identify as already played.
                # If kickoff is missing, keep the player visible (we'll show kickoff as blank/unknown).
                if kickoff_dt and kickoff_dt <= now:
                    continue
                is_confirmed = False
                if status_val == LineupStatus.STARTING:
                    if (
                        selected_action_type == RuleActionType.FA_CLAIM_DROP
                        and fa_trigger_mode in (
                            FA_TRIGGER_MODE_DROP_AND_FA_STARTING,
                            FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE,
                        )
                        and drop_kickoff
                        and kickoff_dt
                        and kickoff_dt < drop_kickoff
                    ):
                        is_confirmed = False
                    else:
                        is_confirmed = True
                if selected_action_type == RuleActionType.FA_CLAIM_DROP:
                    if not drop_player_id:
                        continue
                    if drop_player_id in never_drop_ids and not override_never_drop:
                        continue
                    if not ConditionalSwapEngine._drop_would_keep_roster_legal(
                        roster_view,
                        drop_id=drop_player_id,
                        min_gks=1,
                    ):
                        continue
                    if (
                        fa_trigger_mode in (
                            FA_TRIGGER_MODE_DROP_AND_FA_STARTING,
                            FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE,
                        )
                        and drop_kickoff
                        and kickoff_dt
                        and kickoff_dt < drop_kickoff
                    ):
                        continue
                proj_row = _fa_projection_row(p.get("name"), p.get("team"), projections_map or {})
                proj_fpts = proj_row.get("ProjFPts") if proj_row else None
                proj_gs = proj_row.get("ProjGS") if proj_row else None
                fa_candidate_rows.append(
                    {
                        "id": sid,
                        "Name": p.get("name") or "",
                        "Team": p.get("team") or "",
                        "Position": p.get("position") or "",
                        "default_pos_id": p.get("default_pos_id"),
                        "Status": status_label,
                        "ProjFPts": proj_fpts,
                        "ProjGS": proj_gs,
                        "Kickoff (UTC)": _format_kickoff(kickoff_dt),
                        "Select": False,
                    }
                )
                if is_confirmed:
                    fa_confirmed_rows.append(fa_candidate_rows[-1])

        display_rows = fa_confirmed_rows or fa_candidate_rows
        if fa_candidate_rows and not fa_confirmed_rows:
            st.info("No confirmed starters right now; showing all available free agents.")

        if display_rows:
            fa_df = pd.DataFrame(display_rows)
            fa_df["ProjFPts"] = pd.to_numeric(fa_df["ProjFPts"], errors="coerce")
            fa_df["ProjGS"] = pd.to_numeric(fa_df["ProjGS"], errors="coerce")
            fa_df = fa_df.sort_values(
                by=["ProjFPts", "ProjGS"],
                ascending=[False, False],
            )
            edited_fa_df = st.data_editor(
                fa_df,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Select": st.column_config.CheckboxColumn("Use as FA target"),
                    "Name": st.column_config.TextColumn("Name", disabled=True),
                    "Team": st.column_config.TextColumn("Team", disabled=True),
                    "Position": st.column_config.TextColumn("Pos", disabled=True),
                    "Status": st.column_config.TextColumn("Status", disabled=True),
                    "ProjFPts": st.column_config.NumberColumn("ProjFPts", format="%.1f", disabled=True),
                    "ProjGS": st.column_config.NumberColumn("ProjGS", disabled=True),
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
            st.info("No free agents available for this league/period.")

    if selected_action_type == RuleActionType.FA_CLAIM_DROP and fa_simple_mode == FA_SIMPLE_MODE_CLAIM_BASED and fa_candidate:
        fa_snapshot = fa_status_map.get(str(fa_candidate["id"])) if fa_status_map else None
        fa_kickoff = getattr(fa_snapshot, "kickoff", None) if fa_snapshot else None
        st.markdown("**Roster player to drop (filtered by FA kickoff slot)**")

        eligible_drop_ids: List[str] = []
        for pid in roster_labels.keys():
            row = player_lookup.get(pid)
            if not row:
                continue
            if not roster_view.get_row(pid):
                continue
            info = lineup_info_by_player.get(pid)
            drop_kickoff = getattr(info, "kickoff", None) if info else None
            if fa_kickoff and drop_kickoff and drop_kickoff < fa_kickoff:
                continue
            if not ConditionalSwapEngine._drop_would_keep_roster_legal(
                roster_view,
                drop_id=pid,
                min_gks=1,
            ):
                continue
            eligible_drop_ids.append(pid)

        if not eligible_drop_ids:
            st.info("No eligible drop candidates available for the selected FA kickoff slot.")
        else:
            drop_player_id = st.selectbox(
                "Roster player to drop/monitor",
                options=eligible_drop_ids,
                format_func=lambda pid: roster_labels.get(pid, pid),
                key="conditional_drop_select_claim_mode",
            )
            drop_row = player_lookup.get(drop_player_id) if drop_player_id else None
            drop_lineup = lineup_info_by_player.get(drop_player_id) if drop_player_id else None
            if drop_row:
                status_text = _format_status(drop_lineup.status) if drop_lineup else "Unknown"
                kickoff_text = _format_kickoff(drop_lineup.kickoff if drop_lineup else None)
                st.markdown(
                    f"**Drop candidate**: {drop_row.player.name} (`{drop_player_id}`) — lineup status: "
                    f"`{status_text}` (kickoff {kickoff_text})"
                )
                if drop_player_id in never_drop_ids:
                    override_never_drop = st.checkbox(
                        "Override never-drop guard for this rule",
                        value=False,
                        key="override_never_drop_rule_claim_mode",
                        help="Only enable if you are intentionally dropping a never-drop player.",
                    )
                    if not override_never_drop:
                        st.warning("This player is in your never-drop list; saving is disabled until you override.")

    if fa_candidate:
        active_targets: list[tuple[str, str]] = []
        if drop_is_active:
            active_targets.append(("Active (replace drop slot)", "active_drop"))
        if open_active_slots:
            active_targets.append(("Active (open slot)", "active_open"))

        if active_targets:
            target_choice = st.selectbox(
                "Active placement",
                options=[label for label, _ in active_targets],
                index=0,
                key="claim_destination_select",
            )
            target_key = dict(active_targets).get(target_choice)
        else:
            target_key = None

        if target_key == "active_drop" and drop_row:
            claim_to_status_id = "1"
            claim_position_id = str(getattr(drop_row, "pos_id", "") or "")
        elif target_key == "active_open" and open_active_slots:
            slot_labels = [_slot_label(row) for row in open_active_slots]
            slot_choice = st.selectbox(
                "Active slot to fill",
                options=slot_labels,
                key="claim_active_slot_select",
            )
            slot_index = slot_labels.index(slot_choice)
            slot_row = open_active_slots[slot_index]
            claim_to_status_id = "1"
            claim_position_id = str(getattr(slot_row, "pos_id", "") or "")
        else:
            claim_to_status_id = "2"
            claim_position_id = str(fa_candidate.get("default_pos_id") or "")
            st.caption("No open active slots; FA will claim to reserve then swap into active.")
            st.markdown("**Active player to move to reserve after claim**")
            post_claim_swap_out_id = st.selectbox(
                "Active player to replace",
                options=list(active_labels.keys()),
                format_func=lambda pid: active_labels.get(pid, pid),
                key="post_claim_swap_out_select",
            )

if selected_action_type == RuleActionType.LINEUP_SWAP:
    submit_disabled = not (
        active_player_id
        and period_id
        and selected_backup_ids
        and active_lineup
        and active_lineup.kickoff
    )
elif selected_action_type == RuleActionType.FA_CLAIM_DROP:
    submit_disabled = not (
        drop_player_id
        and period_id
        and fa_candidate
        and claim_to_status_id
        and claim_position_id
    )
    if claim_to_status_id == "2" and not post_claim_swap_out_id:
        submit_disabled = True
    if drop_player_id in never_drop_ids and not override_never_drop:
        submit_disabled = True
    if claims_allowed and not (claims_acknowledged or claims_ack_checkbox):
        submit_disabled = True
elif selected_action_type == FA_ACTION_ADD_ONLY:
    submit_disabled = not (
        period_id
        and fa_candidate
        and claim_to_status_id
        and claim_position_id
    )
    if claim_to_status_id == "2" and not post_claim_swap_out_id:
        submit_disabled = True
    if claims_allowed and not (claims_acknowledged or claims_ack_checkbox):
        submit_disabled = True
elif selected_action_type == FA_ACTION_DROP_ONLY:
    submit_disabled = not (drop_player_id and period_id)
    if drop_player_id in never_drop_ids and not override_never_drop:
        submit_disabled = True
    if claims_allowed and not (claims_acknowledged or claims_ack_checkbox):
        submit_disabled = True
else:
    submit_disabled = True

if not user_id:
    submit_disabled = True
    st.info("Log in to save manual rules.")
if not state_writer_enabled:
    submit_disabled = True

submitted = st.button("Save Rule", disabled=submit_disabled, type="primary", key="save_rule_btn")

def _render_conflicting_rules(conflicts: List[Dict[str, Any]], *, heading: str) -> None:
    if not conflicts:
        return
    st.warning(heading)
    rows: List[Dict[str, Any]] = []
    for r in conflicts:
        action = str(r.get("action_type") or RuleActionType.LINEUP_SWAP.value)
        period_raw = str(r.get("period") or "")
        rows.append(
            {
                "Rule ID": str(r.get("rule_id") or ""),
                "Action": action,
                "State": _normalized_rule_state(r.get("state")),
                "Period": period_id_map.get(period_raw, period_raw),
                "Active/Drop ID": str(r.get("active_id") or ""),
                "Reserve/FA ID": str(r.get("reserve_id") or r.get("fa_add_scorer_id") or ""),
                "Trigger Mode": str(r.get("fa_trigger_mode") or ""),
                "Source": str(r.get("source") or ""),
            }
        )
    with st.expander("Conflicting existing rule(s)", expanded=True):
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

if submitted and not submit_disabled:
    try:
        if selected_action_type == RuleActionType.LINEUP_SWAP:
            if not user_id:
                st.warning("Log in to save manual lineup swap rules.")
                st.stop()
            try:
                existing_rules = _load_rules_for_user_with_revision(str(user_id))
            except Exception:
                existing_rules = []
            conflicts: List[Dict[str, Any]] = []
            for r in existing_rules:
                if not _is_manual_rule(r):
                    continue
                if str(r.get("league_id")) != str(league_id) or str(r.get("team_id")) != str(team_id):
                    continue
                if str(r.get("active_id")) != str(active_player_id):
                    continue
                if str(r.get("period")) != str(period_id):
                    continue
                if _normalized_rule_state(r.get("state")) != "fired":
                    conflicts.append(r)
            if conflicts:
                _render_conflicting_rules(
                    conflicts,
                    heading="A manual rule already exists for this active player and period.",
                )
                st.stop()
            group_id = uuid.uuid4().hex
            active_label = active_labels.get(active_player_id, active_player_id)
            active_row_pos = _display_pos(active_row) if active_row else ""
            if active_row_pos and "(" not in active_label:
                active_label = _format_player_label(active_label, active_row_pos)
            to_persist = []
            for index, pid in enumerate(selected_backup_ids):
                backup_row = player_lookup.get(pid)
                backup_name = backup_row.player.name if backup_row else pid  # type: ignore[union-attr]
                backup_pos = _display_pos(backup_row) if backup_row else ""
                in_label = _format_player_label(backup_name, backup_pos)
                rec = {
                    "active_id": active_player_id,
                    "reserve_id": pid,
                    "out_label": active_label,
                    "in_label": in_label,
                    "priority": index + 1,
                    "period": period_id,
                    "period_label": period_label,
                    "trigger": "confirmed_lineup",
                    "max_fires": 1,
                    "league_id": league_id,
                    "team_id": team_id,
                    "user_id": str(user_id),
                    "source": "manual",
                    "source_type": 3,
                    "group_id": group_id,
                    "state": "active",
                }
                to_persist.append(rec)
            _apply_user_rule_ops(
                user_id=str(user_id),
                operations=[{"op": "upsert_rules", "items": to_persist}],
            )
            st.success("Rule saved successfully.")
            st.rerun()
        else:
            if not user_id:
                st.warning("Log in to save manual rules.")
                st.stop()
            try:
                existing_rules = _load_rules_for_user_with_revision(str(user_id))
            except Exception:
                existing_rules = []
            action_key = selected_action_type.value if hasattr(selected_action_type, "value") else str(selected_action_type)
            drop_key = str(drop_player_id or "")
            fa_key = str(fa_candidate["id"]) if fa_candidate else ""
            conflicts: List[Dict[str, Any]] = []
            for r in existing_rules:
                if not _is_manual_rule(r):
                    continue
                if str(r.get("league_id")) != str(league_id) or str(r.get("team_id")) != str(team_id):
                    continue
                if str(r.get("period")) != str(period_id):
                    continue
                if str(r.get("action_type") or "") != action_key:
                    continue
                if action_key == FA_ACTION_DROP_ONLY and str(r.get("active_id") or "") == drop_key:
                    if _normalized_rule_state(r.get("state")) != "fired":
                        conflicts.append(r)
                if action_key == RuleActionType.FA_CLAIM_DROP.value:
                    existing_mode = str(r.get("fa_trigger_mode") or FA_TRIGGER_MODE_FA_STARTING_ONLY)
                    if (
                        str(r.get("active_id") or "") == drop_key
                        and str(r.get("fa_add_scorer_id") or "") == fa_key
                        and existing_mode == fa_trigger_mode
                    ):
                        if _normalized_rule_state(r.get("state")) != "fired":
                            conflicts.append(r)
                if action_key == FA_ACTION_ADD_ONLY and str(r.get("fa_add_scorer_id") or "") == fa_key:
                    if _normalized_rule_state(r.get("state")) != "fired":
                        conflicts.append(r)
            if conflicts:
                _render_conflicting_rules(
                    conflicts,
                    heading="A manual rule already exists for this selection and period.",
                )
                st.stop()

            rec = {
                "action_type": action_key,
                "active_id": drop_player_id if drop_player_id else None,
                "drop_label": roster_labels.get(drop_player_id, "") if drop_player_id else "",
                "fa_add_scorer_id": fa_candidate["id"] if fa_candidate else None,
                "fa_trigger_mode": fa_trigger_mode if action_key == RuleActionType.FA_CLAIM_DROP.value else None,
                "fa_add_position_id": claim_position_id,
                "fa_claim_to_status_id": claim_to_status_id,
                "fa_bid_amount": fa_bid_amount,
                "fa_add_display_name": fa_candidate["name"] if fa_candidate else None,
                "post_claim_swap_out_id": post_claim_swap_out_id,
                "period": period_id,
                "period_label": period_label,
                "trigger": "confirmed_lineup",
                "max_fires": 1,
                "league_id": league_id,
                "team_id": team_id,
                "user_id": str(user_id),
                "source": "manual",
                "source_type": 3,
                "state": "active",
                "override_never_drop": bool(override_never_drop),
            }
            _apply_user_rule_ops(
                user_id=str(user_id),
                operations=[{"op": "upsert_rules", "items": [rec]}],
            )
            if claims_allowed and not claims_acknowledged:
                user_mgr.set_claims_ack(
                    user_id=str(user_id),
                    league_id=str(league_id),
                    acknowledged=True,
                    team_id=str(team_id),
                )
            st.success("Rule saved successfully.")
            st.rerun()
    except ValueError as exc:
        st.warning(str(exc))
    except Exception as exc:  # pragma: no cover - runtime feedback
        logger.exception("Failed to save conditional rule")
        st.error(f"Failed to save rule: {exc}")


# ----------------------------------------------------------------------
# Existing rules (manual, per-user storage)
# ----------------------------------------------------------------------
manual_swap_rules: List[Dict[str, Any]] = []
manual_claim_rules: List[Dict[str, Any]] = []
legacy_fa_rules: List[ConditionalSwapRule] = []

if user_id:
    try:
        existing_rules = _load_rules_for_user_with_revision(str(user_id))
    except Exception:
        existing_rules = []

    legacy_rules = legacy_storage.load_rules_for_team(league_id, team_id)
    if legacy_rules:
        existing_origin = {
            str(r.get("origin_rule_id"))
            for r in existing_rules
            if r.get("origin_rule_id")
        }
        to_migrate = []
        for legacy in legacy_rules:
            if legacy.id in existing_origin:
                continue
            state = "disabled" if legacy.state == RuleState.DISABLED else "active"
            if legacy.action_type == RuleActionType.FA_CLAIM_DROP:
                drop_row = player_lookup.get(legacy.active_player_id)
                drop_label = ""
                if drop_row:
                    drop_label = _format_player_label(
                        drop_row.player.name,
                        _display_pos(drop_row),
                    )
                to_migrate.append(
                    {
                        "action_type": RuleActionType.FA_CLAIM_DROP.value,
                        "active_id": legacy.active_player_id,
                        "drop_label": drop_label,
                        "fa_add_scorer_id": legacy.fa_add_scorer_id,
                        "fa_add_position_id": legacy.fa_add_position_id,
                        "fa_claim_to_status_id": legacy.fa_claim_to_status_id,
                        "fa_bid_amount": legacy.fa_bid_amount,
                        "fa_add_display_name": legacy.fa_add_display_name,
                        "period": legacy.period_id,
                        "period_label": legacy.period_label,
                        "trigger": "confirmed_lineup",
                        "max_fires": legacy.max_fires_per_period,
                        "league_id": legacy.league_id,
                        "team_id": legacy.team_id,
                        "user_id": str(user_id),
                        "source": "manual",
                        "source_type": 3,
                        "origin_rule_id": legacy.id,
                        "state": state,
                    }
                )
                continue
            group_id = legacy.id
            for backup in legacy.sorted_backups():
                row = player_lookup.get(backup.reserve_player_id)
                backup_name = row.player.name if row else backup.reserve_player_id  # type: ignore[union-attr]
                backup_pos = _display_pos(row) if row else ""
                in_label = f"{backup_name} ({backup_pos})" if backup_pos else backup_name
                to_migrate.append(
                    {
                        "active_id": legacy.active_player_id,
                        "reserve_id": backup.reserve_player_id,
                        "out_label": "",
                        "in_label": in_label,
                        "priority": backup.priority,
                        "period": legacy.period_id,
                        "period_label": legacy.period_label,
                        "trigger": "confirmed_lineup",
                        "max_fires": legacy.max_fires_per_period,
                        "league_id": legacy.league_id,
                        "team_id": legacy.team_id,
                        "user_id": str(user_id),
                        "source": "manual",
                        "source_type": 3,
                        "group_id": group_id,
                        "origin_rule_id": legacy.id,
                        "state": state,
                    }
                )
        if to_migrate:
            if state_writer_enabled:
                _apply_user_rule_ops(
                    user_id=str(user_id),
                    operations=[{"op": "upsert_rules", "items": to_migrate}],
                )
            try:
                existing_rules = _load_rules_for_user_with_revision(str(user_id))
            except Exception:
                existing_rules = []

    for r in existing_rules:
        if not _is_manual_rule(r):
            continue
        if str(r.get("league_id")) != str(league_id) or str(r.get("team_id")) != str(team_id):
            continue
        action = str(r.get("action_type") or "")
        if action in {
            RuleActionType.FA_CLAIM_DROP.value,
            FA_ACTION_ADD_ONLY,
            FA_ACTION_DROP_ONLY,
        }:
            if claims_allowed:
                manual_claim_rules.append(r)
        else:
            manual_swap_rules.append(r)
else:
    legacy_rules = legacy_storage.load_rules_for_team(league_id, team_id)
    legacy_fa_rules = [r for r in legacy_rules if r.action_type == RuleActionType.FA_CLAIM_DROP]

if not manual_swap_rules and not manual_claim_rules and not legacy_fa_rules:
    st.info("No manual conditional rules configured yet.")
else:
    if manual_swap_rules:
        st.divider()
        st.subheader("Existing Conditional Rules")
        grouped: Dict[str, Dict[str, Any]] = {}
        for r in manual_swap_rules:
            group_id = str(r.get("group_id") or "")
            if not group_id:
                group_id = f"{r.get('period')}::{r.get('active_id')}"
            group = grouped.setdefault(
                group_id,
                {
                    "rules": [],
                    "active_id": r.get("active_id"),
                    "period": r.get("period"),
                    "period_label": r.get("period_label"),
                },
            )
            group["rules"].append(r)

        def _group_sort_key(item: tuple[str, Dict[str, Any]]) -> tuple:
            _gid, group = item
            period_val = group.get("period") or 0
            try:
                period_val = int(period_val)
            except Exception:
                period_val = 0
            active_val = group.get("active_id") or ""
            return (period_val, str(active_val))

        for group_id, group in sorted(grouped.items(), key=_group_sort_key):
            group_rules = sorted(
                group["rules"], key=lambda r: int(r.get("priority") or 999)
            )
            active_id = str(group.get("active_id") or "")
            active_row = player_lookup.get(active_id)
            active_name = (
                active_row.player.name if active_row else active_id  # type: ignore[union-attr]
            )
            backup_ids = [str(r.get("reserve_id")) for r in group_rules if r.get("reserve_id")]
            eligible_ids, ineligible_ids = _partition_backups(backup_ids, roster_view)
            eligible_names = []
            for bid in eligible_ids:
                row = player_lookup.get(bid)
                eligible_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            ineligible_names = []
            for bid in ineligible_ids:
                row = player_lookup.get(bid)
                ineligible_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            backups_chain = " > ".join(eligible_names) if eligible_names else "(none)"
            swap_type = _swap_type_label(active_id, eligible_ids, kos_map)
            rule_condition = None
            for r in group_rules:
                if r.get("condition"):
                    rule_condition = str(r.get("condition"))
                    break
            desc = _swap_description(active_name, eligible_names, rule_condition)
            st.markdown(f"**Swap type:** {swap_type}")
            st.markdown(f"**Description:** {desc}")
            rule_lines = []
            for r in group_rules:
                if not r.get("reserve_id"):
                    continue
                active_label = r.get("out_label") or active_name
                reserve_label = r.get("in_label") or r.get("reserve_id")
                rule_lines.append(
                    _format_saved_rule_sentence(
                        str(active_label),
                        str(reserve_label),
                        str(r.get("condition") or ""),
                    )
                )
            if rule_lines:
                st.markdown("**Rules:**")
                for line in rule_lines:
                    st.caption(line)
            st.caption(f"Backups (eligible now): {backups_chain}")
            if ineligible_names:
                st.caption(
                    "Backups ignored (not reserves right now): " + " > ".join(ineligible_names)
                )

            info_line = lineup_info_by_player.get(active_id)
            status_text = _format_binary_status(info_line.status) if info_line else "UNCONFIRMED"
            kickoff_text = _format_kickoff(info_line.kickoff if info_line else None)
            period_label = (
                group.get("period_label")
                or period_id_map.get(str(group.get("period")), str(group.get("period")))
            )
            states = {_normalized_rule_state(r.get("state")) for r in group_rules}
            if states == {"fired"}:
                state_text = "fired"
            elif "disabled" in states:
                state_text = "disabled"
            else:
                state_text = "active"
            st.caption(
                f"Period: {period_label} | Type: lineup_swap | Max fires: {group_rules[0].get('max_fires', 1)} | State: {state_text} "
                f"| Current lineup: {status_text} ({kickoff_text})"
            )

            action_cols = st.columns(2)
            if state_text != "fired":
                toggle_label = "Disable" if state_text == "active" else "Enable"
                if action_cols[0].button(
                    f"{toggle_label} Rule",
                    key=f"toggle_manual_{group_id}",
                    use_container_width=True,
                    disabled=not state_writer_enabled,
                ):
                    try:
                        selector: Dict[str, Any]
                        if group_rules and group_rules[0].get("group_id"):
                            selector = {"group_id": group_id}
                        else:
                            selector = {
                                "matcher": {
                                    "active_id": str(active_id),
                                    "period": str(group.get("period")),
                                    "league_id": str(league_id),
                                    "team_id": str(team_id),
                                }
                            }
                        _apply_user_rule_ops(
                            user_id=str(user_id),
                            operations=[
                                {
                                    "op": "patch_rules",
                                    "selector": selector,
                                    "patch": {
                                        "state": "disabled" if state_text == "active" else "active",
                                    },
                                }
                            ],
                        )
                        st.rerun()
                    except Exception:
                        st.warning("Failed to update rule state.")
            else:
                action_cols[0].write("")

            if action_cols[1].button(
                "Delete",
                key=f"delete_manual_{group_id}",
                use_container_width=True,
                disabled=not state_writer_enabled,
            ):
                try:
                    selector: Dict[str, Any]
                    if group_rules and group_rules[0].get("group_id"):
                        selector = {"group_id": group_id}
                    else:
                        selector = {
                            "matcher": {
                                "active_id": str(active_id),
                                "period": str(group.get("period")),
                                "league_id": str(league_id),
                                "team_id": str(team_id),
                            }
                        }
                    _apply_user_rule_ops(
                        user_id=str(user_id),
                        operations=[
                            {
                                "op": "delete_rules",
                                "selector": selector,
                            }
                        ],
                    )
                    st.rerun()
                except Exception:
                    st.warning("Failed to delete rule group.")

            st.caption(f"Rule group ID: {group_id}")
            st.markdown("---")

    if manual_claim_rules and claims_allowed:
        st.divider()
        st.subheader("Existing Claim/Drop Rules")
        manual_claim_rules = sorted(
            manual_claim_rules,
            key=lambda r: (
                int(r.get("period") or 0),
                str(r.get("active_id") or ""),
                str(r.get("fa_add_scorer_id") or ""),
            ),
        )
        for rule in manual_claim_rules:
            action = str(rule.get("action_type") or RuleActionType.FA_CLAIM_DROP.value)
            drop_id = str(rule.get("active_id") or "")
            drop_row = player_lookup.get(drop_id)
            drop_name = drop_row.player.name if drop_row else drop_id  # type: ignore[union-attr]
            fa_label = (
                rule.get("fa_add_display_name")
                or rule.get("fa_add_scorer_id")
                or "unknown FA"
            )
            post_swap_out = rule.get("post_claim_swap_out_id")
            period_label = (
                rule.get("period_label")
                or period_id_map.get(str(rule.get("period")), str(rule.get("period")))
            )
            state_text = _normalized_rule_state(rule.get("state"))
            if action == FA_ACTION_DROP_ONLY:
                st.markdown(
                    f"**Drop rule:** When **{drop_name}** is *not starting*, drop **{drop_name}** during **{period_label}**."
                )
            elif action == FA_ACTION_ADD_ONLY:
                st.markdown(
                    f"**Add rule:** When **{fa_label}** is *starting*, submit claim to add **{fa_label}** "
                    f"during **{period_label}**."
                )
            else:
                trigger_mode = str(rule.get("fa_trigger_mode") or FA_TRIGGER_MODE_FA_STARTING_ONLY)
                st.markdown(
                    (
                        f"**Add/Drop rule:** When **{fa_label}** is *starting*, submit claim to add **{fa_label}** "
                        f"and drop **{drop_name}** during **{period_label}**."
                        if trigger_mode == FA_TRIGGER_MODE_FA_STARTING_ONLY
                        else f"**Add/Drop rule:** When **{drop_name}** is *not starting*, submit claim to add **{fa_label}** "
                        f"and drop **{drop_name}** during **{period_label}**."
                        if trigger_mode == FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE
                        else f"**Add/Drop rule:** When **{drop_name}** is *not starting* and **{fa_label}** is *starting*, "
                        f"submit claim to add **{fa_label}** and drop **{drop_name}** during **{period_label}**."
                    )
                )
            if post_swap_out:
                swap_row = player_lookup.get(str(post_swap_out))
                swap_label = swap_row.player.name if swap_row else post_swap_out  # type: ignore[union-attr]
                st.caption(f"After claim, swap into active (send to reserve): {swap_label}")
            st.caption(
                f"Type: {action} | State: {state_text} | Max fires: {rule.get('max_fires', 1)}"
            )
            action_cols = st.columns(2)
            if state_text != "fired":
                toggle_label = "Disable" if state_text == "active" else "Enable"
                if action_cols[0].button(
                    f"{toggle_label} Rule",
                    key=f"toggle_manual_claim_{rule.get('rule_id')}",
                    use_container_width=True,
                    disabled=not state_writer_enabled,
                ):
                    try:
                        _apply_user_rule_ops(
                            user_id=str(user_id),
                            operations=[
                                {
                                    "op": "patch_rules",
                                    "selector": {"rule_ids": [str(rule.get("rule_id"))]},
                                    "patch": {
                                        "state": "disabled" if state_text == "active" else "active",
                                    },
                                }
                            ],
                        )
                        st.rerun()
                    except Exception:
                        st.warning("Failed to update rule state.")
            else:
                action_cols[0].write("")
            if action_cols[1].button(
                "Delete",
                key=f"delete_manual_claim_{rule.get('rule_id')}",
                use_container_width=True,
                disabled=not state_writer_enabled,
            ):
                try:
                    _apply_user_rule_ops(
                        user_id=str(user_id),
                        operations=[
                            {
                                "op": "delete_rules",
                                "selector": {"rule_ids": [str(rule.get("rule_id"))]},
                            }
                        ],
                    )
                    st.rerun()
                except Exception:
                    st.warning("Failed to delete rule.")
            st.caption(f"Rule ID: {rule.get('rule_id')}")
            st.markdown("---")
    elif manual_claim_rules and not claims_allowed:
        st.divider()
        st.subheader("Existing Claim/Drop Rules")
        st.caption("Claim/drop rules are locked to the test league during the pilot.")

    if legacy_fa_rules:
        st.divider()
        st.subheader("Legacy FA claim/drop rules")
        st.caption("Legacy FA claim rules are stored in legacy storage.")
        for rule in legacy_fa_rules:
            active_name = (
                player_lookup.get(rule.active_player_id).player.name  # type: ignore[union-attr]
                if player_lookup.get(rule.active_player_id)
                else rule.active_player_id
            )
            fa_label = (
                getattr(rule, "fa_add_display_name", None)
                or getattr(rule, "fa_add_scorer_id", None)
                or "unknown FA"
            )
            st.markdown(
                f"**FA claim/drop rule:** When **{active_name}** is *not starting* and **{fa_label}** is *starting*, "
                f"submit FA claim to add **{fa_label}** and drop **{active_name}** during **{rule.period_label}**."
            )
            st.caption(f"Rule ID: {rule.id}")
            st.markdown("---")

# ----------------------------------------------------------------------
# Auto-generated rules (runner-managed)
# ----------------------------------------------------------------------
st.divider()
st.subheader("Auto-generated lineup rules (read-only)")
if not user_id:
    st.info("Log in to view auto-generated rules for this league.")
else:
    auto_rules: List[Dict[str, Any]] = []
    try:
        raw_rules = _load_rules_for_user_with_revision(str(user_id))
    except Exception:
        raw_rules = []
    for rule in raw_rules:
        if str(rule.get("league_id")) != str(league_id) or str(rule.get("team_id")) != str(team_id):
            continue
        source = normalize_rule_source(str(rule.get("source") or ""))
        if source != SOURCE_AUTO_LINEUP_SWAPS:
            continue
        auto_rules.append(rule)

    if not auto_rules:
        st.caption(
            "Either: 1. You have not toggled the Auto lineup swaps toggle in the sidebar (most likely), or 2. No auto rules are available for this roster/period right now."
        )
    else:
        auto_rules = sorted(
            auto_rules,
            key=lambda r: (
                int(r.get("period") or 0),
                r.get("active_id") or "",
                int(r.get("priority") or 999),
            ),
        )
        grouped: dict[tuple[str, str], list[dict]] = {}
        for r in auto_rules:
            key = (str(r.get("period") or ""), str(r.get("active_id") or ""))
            grouped.setdefault(key, []).append(r)
        rows = []
        for (period, active_id), group in grouped.items():
            group = sorted(group, key=lambda r: int(r.get("priority") or 999))
            active_row = player_lookup.get(active_id)
            active_name = active_row.player.name if active_row else active_id  # type: ignore[union-attr]
            active_locked = False
            if active_row:
                active_lock_flags = get_row_lock_flags(
                    active_row,
                    now=now,
                    lineup_info_by_player=lineup_info_by_player,
                )
                active_locked = bool(active_lock_flags.get("visually_locked"))
            backup_ids = [str(r.get("reserve_id")) for r in group if r.get("reserve_id")]
            eligible_ids: List[str] = []
            ineligible_ids: List[str] = []
            locked_ids: List[str] = []
            for bid in backup_ids:
                if not roster_view.is_reserve(bid):
                    ineligible_ids.append(bid)
                    continue
                row = player_lookup.get(bid)
                if row:
                    lock_flags = get_row_lock_flags(
                        row,
                        now=now,
                        lineup_info_by_player=lineup_info_by_player,
                    )
                    if lock_flags.get("visually_locked"):
                        locked_ids.append(bid)
                        continue
                eligible_ids.append(bid)

            configured_names = []
            for bid in backup_ids:
                row = player_lookup.get(bid)
                configured_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            backup_names = []
            for bid in eligible_ids:
                row = player_lookup.get(bid)
                backup_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            ineligible_names = []
            for bid in ineligible_ids:
                row = player_lookup.get(bid)
                ineligible_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            locked_names = []
            for bid in locked_ids:
                row = player_lookup.get(bid)
                locked_names.append(row.player.name if row else bid)  # type: ignore[union-attr]
            if active_locked:
                desc = "Active locked; no eligible backups right now."
                backup_names = []
                configured_names = []
                eligible_ids = []
            elif backup_names:
                desc = _swap_description(active_name, backup_names)
            elif configured_names:
                desc = "No eligible backups right now."
            else:
                desc = _swap_description(active_name, backup_names)
            rows.append(
                {
                    "Active": active_name,
                    "Swap Type": _swap_type_label(active_id, eligible_ids, kos_map),
                    "Description": desc,
                    "Configured backups": " > ".join(configured_names) if configured_names else "(none)",
                    "Backups (eligible now)": " > ".join(backup_names) if backup_names else "",
                    "Ignored (not reserves)": " > ".join(ineligible_names) if ineligible_names else "",
                    "Ignored (locked/played)": " > ".join(locked_names) if locked_names else "",
                    "Period": period,
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption(
            "Auto rules are regenerated by the runner and cannot be edited here. "
            "Use the auto lineup swaps toggle to enable or disable them per league. "
            "If the backups changed, rerun the runner so the rules regenerate against the current roster."
        )
        st.caption(
            "Auto rules default behavior: if an active player is confirmed non-starter and no reserve is "
            "confirmed starter yet, the runner may fallback to the best unconfirmed reserve candidate."
        )
    if swap_period_int is not None and active_candidates and reserve_candidates:
        if st.checkbox("Show auto-rule legality diagnostics", key="auto_swap_legality_debug"):
            debug_rows = _build_swap_legality_debug(
                roster=roster,
                roster_view=roster_view,
                lineup_info_by_player=lineup_info_by_player,
                subs_service=subs_service,
                league_id=league_id,
                team_id=team_id,
                swap_period_int=swap_period_int,
                active_candidates=active_candidates,
                reserve_candidates=reserve_candidates,
            )
            st.dataframe(pd.DataFrame(debug_rows), hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Auto-generated claim/drop rules (read-only)")
    st.caption("Auto claims/drops are disabled during testing.")


# ----------------------------------------------------------------------
# Conditional swap system health (Fantrax reconciliation)
# ----------------------------------------------------------------------
st.divider()
with st.expander("Conditional Swap System Health", expanded=False):
    if not user_id:
        st.info("Log in to view conditional swap health for this team.")
    else:
        try:
            rules_for_user = _load_rules_for_user_with_revision(str(user_id))
        except Exception:
            rules_for_user = []

        app_fired_events_all = build_fired_conditional_events(
            rules_for_user,
            league_id=str(league_id),
            team_id=str(team_id),
            user_id=str(user_id),
        )
        logger.info(
            "[health] app fired conditional events: %s (league=%s team=%s user=%s)",
            len(app_fired_events_all),
            league_id,
            team_id,
            user_id,
        )

        try:
            fantrax_raw_rows = fetch_lineup_change_history(api, team_id=str(team_id), max_rows=200)
            fantrax_events_all = normalize_lineup_change_rows(fantrax_raw_rows)
        except Exception as exc:
            fantrax_events_all = []
            st.warning(f"Unable to load Fantrax lineup-change history: {exc}")
        logger.info(
            "[health] fantrax lineup history events: %s (league=%s team=%s)",
            len(fantrax_events_all),
            league_id,
            team_id,
        )

        def _period_token(value: Any) -> str:
            raw = str(value or "").strip()
            digits = "".join(ch for ch in raw if ch.isdigit())
            if not digits:
                return ""
            try:
                return str(int(digits))
            except Exception:
                return ""

        period_id_to_round_token: Dict[str, str] = {}
        for pid, label in period_id_map.items():
            round_token = resolve_round_from_period(
                selected_period_id=str(pid),
                selected_period_label=str(label),
                inferred_round=None,
            ) or _period_token(pid)
            if round_token:
                period_id_to_round_token[str(pid)] = str(round_token)

        last_gameweek_token = ""
        detected_token = _period_token(detected_period)
        selected_token = _period_token(selected_period_id)
        if detected_token:
            try:
                detected_int = int(detected_token)
                if detected_int > 1:
                    last_gameweek_token = str(detected_int - 1)
            except Exception:
                pass
        if not last_gameweek_token and selected_token:
            try:
                selected_int = int(selected_token)
                if selected_int > 1:
                    last_gameweek_token = str(selected_int - 1)
            except Exception:
                pass

        sorted_period_ids: List[str] = []
        for pid in period_id_map.keys():
            token = period_id_to_round_token.get(str(pid)) or _period_token(pid)
            if token:
                sorted_period_ids.append(token)
        sorted_period_ids = sorted(set(sorted_period_ids), key=lambda p: int(p))
        if not last_gameweek_token and len(sorted_period_ids) >= 2:
            last_gameweek_token = sorted_period_ids[-2]
        elif not last_gameweek_token and len(sorted_period_ids) == 1:
            last_gameweek_token = sorted_period_ids[0]

        period_window_options: List[str] = []
        period_window_map: Dict[str, str] = {}
        if last_gameweek_token:
            last_label = period_id_map.get(last_gameweek_token, f"Period {last_gameweek_token}")
            default_label = f"Last gameweek (default): {last_label}"
            period_window_options.append(default_label)
            period_window_map[default_label] = last_gameweek_token
        all_label = "All periods"
        period_window_options.append(all_label)
        period_window_map[all_label] = ""
        for pid in sorted(
            period_id_map.keys(),
            key=lambda p: int(period_id_to_round_token.get(str(p)) or _period_token(p) or 0),
            reverse=True,
        ):
            token = period_id_to_round_token.get(str(pid)) or _period_token(pid)
            if not token:
                continue
            if token == last_gameweek_token:
                continue
            label = period_id_map.get(pid, f"Period {pid}")
            option_label = f"{label}"
            period_window_options.append(option_label)
            period_window_map[option_label] = token

        selected_window_label = st.selectbox(
            "Health view window period",
            options=period_window_options if period_window_options else [all_label],
            index=0,
            key=f"conditional_health_period_window_{league_id}_{team_id}",
            help="Choose which gameweek period to evaluate in the conditional swap health view.",
        )
        selected_window_period = period_window_map.get(selected_window_label, "")
        if selected_window_period:
            def _event_round_token(event_period: Any) -> str:
                period_str = str(event_period or "").strip()
                if period_str in period_id_to_round_token:
                    return period_id_to_round_token[period_str]
                return _period_token(period_str)

            app_fired_events = [
                event
                for event in app_fired_events_all
                if _event_round_token(event.get("period")) == selected_window_period
            ]
            fantrax_events = [
                event
                for event in fantrax_events_all
                if _period_token(event.get("week_or_period")) == selected_window_period
            ]
        else:
            app_fired_events = app_fired_events_all
            fantrax_events = fantrax_events_all
        logger.info(
            "[health] window period=%s app_events=%s fantrax_events=%s (league=%s team=%s)",
            selected_window_period or "ALL",
            len(app_fired_events),
            len(fantrax_events),
            league_id,
            team_id,
        )

        matched_rows = match_events(
            app_events=app_fired_events,
            fantrax_events=fantrax_events,
            window_seconds=HEALTH_MATCH_WINDOW_SECONDS,
        )
        metrics = compute_health_metrics(matched_rows)
        logger.info(
            "[health] matched=%s unmatched=%s total=%s (league=%s team=%s)",
            metrics.get("matched"),
            metrics.get("unmatched"),
            metrics.get("total_fired"),
            league_id,
            team_id,
        )

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Fired conditional swaps", metrics.get("total_fired", 0))
        m2.metric("Matched in Fantrax", metrics.get("matched", 0))
        m3.metric("Unmatched", metrics.get("unmatched", 0))
        m4.metric("Success rate", f"{metrics.get('success_rate', 0.0)}%")
        st.caption(
            f"Match window: {HEALTH_MATCH_WINDOW_SECONDS}s | "
            f"Fantrax view: LINEUP_CHANGE | Team filter: {team_id} | "
            f"Window period: {selected_window_period or 'ALL'}"
        )

        if matched_rows:
            local_tz = ZoneInfo(HEALTH_TIMEZONE)
            details_rows: List[Dict[str, Any]] = []
            for row in matched_rows:
                app_time = row.get("fired_at_utc")
                app_time_local = (
                    app_time.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                    if isinstance(app_time, datetime)
                    else "—"
                )
                fan_time = row.get("fantrax_time_utc")
                fan_time_local = (
                    fan_time.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
                    if isinstance(fan_time, datetime)
                    else (row.get("fantrax_time_local") or "—")
                )
                out_id = str(row.get("active_id") or "")
                in_id = str(row.get("reserve_id") or "")
                out_row = player_lookup.get(out_id)
                in_row = player_lookup.get(in_id)
                out_name = out_row.player.name if out_row and getattr(out_row, "player", None) else out_id
                in_name = in_row.player.name if in_row and getattr(in_row, "player", None) else in_id
                details_rows.append(
                    {
                        "Status": str(row.get("health_status") or "unmatched").upper(),
                        "App fired (local)": app_time_local,
                        "Rule ID": row.get("rule_id") or "—",
                        "Out": out_name,
                        "In": in_name,
                        "Period": row.get("period") or "—",
                        "Fantrax tx time (local)": fan_time_local,
                        "Fantrax txSetId": row.get("matched_tx_set_id") or "—",
                        "Delta (sec)": row.get("delta_seconds") if row.get("delta_seconds") is not None else "—",
                    }
                )
            st.dataframe(pd.DataFrame(details_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No fired conditional lineup swap rules found for this team yet.")

        if metrics.get("unmatched", 0) > 0:
            st.warning(
                "Some app-fired swaps were not matched in Fantrax within 2 minutes. "
                "Possible causes: timing drift, delayed Fantrax posting, or non-swap outcomes."
            )

        with st.container(border=True):
            st.markdown("**Health Debug Datasets**")
            st.caption(
                "Compare what Fantrax transaction history returned vs what app-fired conditional rules returned "
                "for this selected period window."
            )
            st.caption("Log path: data/logs/conditional_swaps.log")

            def _dt_iso(val: Any) -> str:
                if isinstance(val, datetime):
                    try:
                        return val.astimezone(timezone.utc).isoformat()
                    except Exception:
                        return str(val)
                return str(val or "")

            app_debug_rows: List[Dict[str, Any]] = []
            for event in app_fired_events_all:
                app_debug_rows.append(
                    {
                        "window_included": "yes"
                        if event in app_fired_events
                        else "no",
                        "rule_id": event.get("rule_id") or "",
                        "period": event.get("period") or "",
                        "active_id": event.get("active_id") or "",
                        "reserve_id": event.get("reserve_id") or "",
                        "fired_at_utc": _dt_iso(event.get("fired_at_utc")),
                        "source": event.get("source") or "",
                        "event_origin": event.get("event_origin") or "",
                        "result": event.get("result") or "",
                    }
                )
            fantrax_debug_rows: List[Dict[str, Any]] = []
            for event in fantrax_events_all:
                moves = event.get("moves") or []
                player_ids = " | ".join(
                    f"{m.get('player_name') or m.get('player_id')}:{m.get('from_slot')}->{m.get('to_slot')}"
                    for m in moves
                )
                fantrax_debug_rows.append(
                    {
                        "window_included": "yes"
                        if event in fantrax_events
                        else "no",
                        "tx_set_id": event.get("tx_set_id") or "",
                        "week_or_period": event.get("week_or_period") or "",
                        "team_id": event.get("team_id") or "",
                        "executed": bool(event.get("executed")),
                        "date_local": event.get("date_local") or "",
                        "date_utc": _dt_iso(event.get("date_utc")),
                        "moves": player_ids,
                    }
                )

            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**App Fired Conditional Rules (source)**")
                if app_debug_rows:
                    st.dataframe(pd.DataFrame(app_debug_rows), hide_index=True, use_container_width=True)
                else:
                    st.caption("No app fired conditional events found.")
            with c2:
                st.markdown("**Fantrax Lineup Transactions (source)**")
                if fantrax_debug_rows:
                    st.dataframe(pd.DataFrame(fantrax_debug_rows), hide_index=True, use_container_width=True)
                else:
                    st.caption("No Fantrax lineup transaction events found.")

            st.markdown("**Match Output (what the component uses)**")
            if matched_rows:
                match_debug_rows = []
                for row in matched_rows:
                    match_debug_rows.append(
                        {
                            "health_status": row.get("health_status") or "",
                            "rule_id": row.get("rule_id") or "",
                            "period": row.get("period") or "",
                            "active_id": row.get("active_id") or "",
                            "reserve_id": row.get("reserve_id") or "",
                            "fired_at_utc": _dt_iso(row.get("fired_at_utc")),
                            "matched_tx_set_id": row.get("matched_tx_set_id") or "",
                            "fantrax_time_utc": _dt_iso(row.get("fantrax_time_utc")),
                            "delta_seconds": row.get("delta_seconds"),
                        }
                    )
                st.dataframe(pd.DataFrame(match_debug_rows), hide_index=True, use_container_width=True)
            else:
                st.caption("No matched/unmatched rows generated for current filters.")

            if st.button(
                "Log Health Debug Snapshot",
                key=f"log_health_debug_snapshot_{league_id}_{team_id}",
                type="secondary",
            ):
                snapshot = {
                    "league_id": str(league_id),
                    "team_id": str(team_id),
                    "user_id": str(user_id),
                    "selected_window_label": selected_window_label,
                    "selected_window_period": selected_window_period or "ALL",
                    "match_window_seconds": HEALTH_MATCH_WINDOW_SECONDS,
                    "app_fired_events_all": app_debug_rows,
                    "fantrax_events_all": fantrax_debug_rows,
                    "matched_rows": [
                        {
                            "health_status": row.get("health_status") or "",
                            "rule_id": row.get("rule_id") or "",
                            "period": row.get("period") or "",
                            "active_id": row.get("active_id") or "",
                            "reserve_id": row.get("reserve_id") or "",
                            "fired_at_utc": _dt_iso(row.get("fired_at_utc")),
                            "matched_tx_set_id": row.get("matched_tx_set_id") or "",
                            "fantrax_time_utc": _dt_iso(row.get("fantrax_time_utc")),
                            "delta_seconds": row.get("delta_seconds"),
                        }
                        for row in matched_rows
                    ],
                }
                snapshot_json = json.dumps(snapshot, separators=(",", ":"))
                logger.info("[health-debug] %s", snapshot_json)
                try:
                    _HEALTH_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with _HEALTH_DEBUG_PATH.open("a", encoding="utf-8") as f:
                        f.write(snapshot_json + "\n")
                    st.success(
                        "Health debug snapshot written to "
                        "data/logs/conditional_swaps_health_debug.jsonl"
                    )
                except Exception as exc:
                    st.warning(f"Failed to write dedicated health debug file: {exc}")


# ----------------------------------------------------------------------
# Immediate swaps (session only)
# ----------------------------------------------------------------------
with st.expander("Immediate Swaps (current session only)", expanded=False):
    events = st.session_state.get("conditional_immediate_swap_events")
    if not isinstance(events, list) or not events:
        st.caption("No immediate or test-fire swaps recorded in this session.")
    else:
        local_tz = ZoneInfo(HEALTH_TIMEZONE)
        rows: List[Dict[str, Any]] = []
        for event in reversed(events):
            out_id = str(event.get("out_player_id") or "")
            in_id = str(event.get("in_player_id") or "")
            out_row = player_lookup.get(out_id)
            in_row = player_lookup.get(in_id)
            out_name = out_row.player.name if out_row and getattr(out_row, "player", None) else out_id
            in_name = in_row.player.name if in_row and getattr(in_row, "player", None) else in_id
            fired_raw = str(event.get("fired_at_utc") or "")
            try:
                fired_dt = datetime.fromisoformat(fired_raw)
                if fired_dt.tzinfo is None:
                    fired_dt = fired_dt.replace(tzinfo=timezone.utc)
                fired_local = fired_dt.astimezone(local_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                fired_local = fired_raw or "—"
            rows.append(
                {
                    "Time (local)": fired_local,
                    "Action": event.get("action") or "immediate_swap",
                    "Out": out_name,
                    "In": in_name,
                    "Period": event.get("period") or "—",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
