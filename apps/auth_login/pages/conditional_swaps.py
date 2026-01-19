"""
Conditional swaps page – define tiered backup rules per Fantrax period.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import pandas as pd
import streamlit as st
import yaml

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
    append_rules,
    append_rules_for_user,
    load_rules_for_user,
    rules_path_for_user,
    save_rules,
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

try:
    from utils.auth_helpers import load_requests_session_from_artifacts
except ImportError:  # pragma: no cover - fallback for older deployments
    load_requests_session_from_artifacts = None  # type: ignore
from utils.user_manager import UserManager

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
            "Auto lineup swaps (per league)",
            value=auto_swaps_enabled,
            help="Opt-in per league; automated conditional lineup swaps are disabled by default.",
            key="auto_rules_lineup_swaps_toggle",
        )
        auto_claims_toggle = st.toggle(
            "Auto claims/drops (per league)",
            value=auto_claims_enabled,
            help="Higher risk; automated claims/drops are disabled by default.",
            key="auto_rules_claims_toggle",
        )
    if auto_swaps_toggle != auto_swaps_enabled and hasattr(user_mgr, "set_auto_rules_enabled"):
        user_mgr.set_auto_rules_enabled(
            user_id=str(user_id),
            league_id=str(league_id),
            team_id=str(team_id),
            enabled=auto_swaps_toggle,
            feature="lineup_swaps",
        )
    if auto_claims_toggle != auto_claims_enabled and hasattr(user_mgr, "set_auto_rules_enabled"):
        user_mgr.set_auto_rules_enabled(
            user_id=str(user_id),
            league_id=str(league_id),
            team_id=str(team_id),
            enabled=auto_claims_toggle,
            feature="claims",
        )

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


def _swap_description(active_name: str, backup_names: List[str]) -> str:
    if not backup_names:
        return "No backups configured."
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
        return Path(env_path).expanduser().resolve()
    return SERVICE_ACCOUNT_DEFAULT_PATH


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
        return None

    key_path = _service_account_path()
    if not key_path.exists():
        logger.info("Service account key missing at %s; skipping sheet fetch", key_path)
        return None

    try:
        client = gspread.service_account(filename=str(key_path))
        sheet = client.open_by_url(PROJECTIONS_SHEET_URL)
        ws = sheet.worksheet(PROJECTIONS_SHEET_WORKSHEET) if PROJECTIONS_SHEET_WORKSHEET else sheet.sheet1
        df = _normalize_projection_columns(pd.DataFrame(ws.get_all_records()))
        if df.empty:
            raise ValueError("projections sheet returned no rows")
        logger.info("Loaded %s projection rows from Google Sheet", len(df))
        try:
            PROJECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(PROJECTIONS_PATH, index=False)
        except Exception as cache_exc:
            logger.info("Unable to cache projections to %s: %s", PROJECTIONS_PATH, cache_exc)
        return df
    except Exception as exc:
        logger.warning("Failed to load projections from Google Sheet: %s", exc)
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
        except Exception as exc:
            logger.warning("Unable to load projections from sheet or cache: %s", exc)
            st.warning("Projections unavailable (Google Sheet fetch failed and no cached file found).")
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
    event_keys_by_kickoff: dict[datetime, set[str]] = {}
    for ko in kickoffs:
        days.setdefault(ko.date(), []).append(ko)
    for info in lineup_info_by_player.values():
        if not info or not info.kickoff:
            continue
        kickoff = info.kickoff
        key = None
        if getattr(info, "event_id", None) is not None:
            key = f"event:{info.event_id}"
        else:
            team = getattr(info, "team_name", None)
            opp = getattr(info, "opponent_name", None)
            if team and opp:
                pair = "|".join(sorted([str(team), str(opp)]))
                key = f"teams:{pair}"
        if key is None:
            key = f"player:{getattr(info, 'fantrax_player_id', '')}"
        event_keys_by_kickoff.setdefault(kickoff, set()).add(key)
    gw_payload: dict[str, dict] = {
        gw_label: {
            "days": len(days),
            "kickoff_slots": len(kickoffs),
        }
    }
    for day_idx, day_key in enumerate(sorted(days.keys()), start=1):
        md_key = f"MD {day_idx}"
        day_entries: dict[str, dict] = {}
        for ko in sorted(days[day_key]):
            kos_idx = kickoff_to_index.get(ko)
            label = f"KOS {kos_idx}" if kos_idx else "KOS ?"
            count = len(event_keys_by_kickoff.get(ko, set()))
            day_entries[label] = {
                "kickoff": ko.strftime("%Y-%m-%d %H:%M UTC"),
                "count": count,
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
                        events.append({"event_id": event_id, "kickoff": kickoff})
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
                events.append({"event_id": event_id, "kickoff": kickoff})

        _collect(False)
        _collect(True)
        if cached_events:
            merged = {ev["event_id"]: ev for ev in cached_events}
            for ev in events:
                merged.setdefault(ev["event_id"], ev)
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
    event_ids_by_kickoff: dict[datetime, set[int]] = {}
    for ev in events:
        kickoff = ev.get("kickoff")
        if not kickoff:
            continue
        days.setdefault(kickoff.date(), []).append(kickoff)
        event_id = ev.get("event_id")
        if event_id is None:
            continue
        event_ids_by_kickoff.setdefault(kickoff, set()).add(int(event_id))
    gw_payload: dict[str, dict] = {
        gw_label: {
            "days": len(days),
            "kickoff_slots": len(kickoffs),
        }
    }
    for day_idx, day_key in enumerate(sorted(days.keys()), start=1):
        md_key = f"MD {day_idx}"
        day_entries: dict[str, dict] = {}
        for ko in sorted(days[day_key]):
            kos_idx = kickoff_to_index.get(ko)
            label = f"KOS {kos_idx}" if kos_idx else "KOS ?"
            count = len(event_ids_by_kickoff.get(ko, set()))
            day_entries[label] = {
                "kickoff": ko.strftime("%Y-%m-%d %H:%M UTC"),
                "count": count,
            }
        gw_payload[gw_label][md_key] = day_entries
    return gw_payload


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

with st.spinner("Loading lineup data..."):
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

    with st.expander("🔍 SofaScore live refresh debug"):
        st.write("Target event kickoffs (event_id -> kickoff UTC):")
        if event_kickoffs:
            st.write(
                {
                    eid: ko.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(ko, datetime) else str(ko)
                    for eid, ko in event_kickoffs.items()
                }
            )
        else:
            st.write("None")
        st.write(
            "Last ss_refresh_meta:",
            {k: v for k, v in ss_meta.items() if k != "_call_count"} if isinstance(ss_meta, dict) else ss_meta,
        )
        if last_err:
            st.write("Last live fetch error:", str(last_err))
        if last_debug:
            st.write("Last live refresh debug:", str(last_debug))

    with st.expander("🧩 Event ID debug"):
        event_rows = []
        for pid in roster_view.active_player_ids() + roster_view.reserve_player_ids():
            row = player_lookup.get(pid)
            name = getattr(getattr(row, "player", None), "name", str(pid))
            info = lineup_info_by_player.get(pid)
            event_rows.append(
                {
                    "Player": name,
                    "Team": getattr(info, "team_name", None) if info else None,
                    "Opponent": getattr(info, "opponent_name", None) if info else None,
                    # Event ID should be a string in the dataframe
                    "Event ID": str(getattr(info, "event_id", None)) if info else None,
                    "Kickoff (UTC)": _format_kickoff(getattr(info, "kickoff", None)) if info else "—",
                }
            )
        if event_rows:
            st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
        else:
            st.write("No event data available for this roster.")

kos_map, last_kos_index = _build_kos_index_map(lineup_info_by_player)

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


def _build_lineup_feed_rows(
    *,
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    player_lookup: Dict[str, RosterRow],
    projections: Optional[dict] = None,
) -> list[dict]:
    """
    Build a simple feed payload sorted by team code, then kickoff.
    """
    rows: list[dict] = []
    order_index = {pid: i for i, pid in enumerate(roster_view.active_player_ids())}
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
            if getattr(info, "ss_conf_status", None):
                ss_effective = getattr(info, "ss_conf_status")
                ss_confirmed = 1
            elif getattr(info, "ss_pred_status", None):
                ss_effective = getattr(info, "ss_pred_status")
            else:
                ss_effective = getattr(info, "ss_status", None)
        ss_code = _status_code(ss_effective)

        fx_effective = getattr(info, "fx_status", None) if info else None
        fx_code = _status_code(fx_effective)
        proj_gs = None
        if projections:
            proj_row = _player_projection_row(info, projections=projections, fallback_name=row.player.name)
            if proj_row:
                try:
                    proj_gs = int(proj_row.get("ProjGS"))
                except Exception:
                    proj_gs = None

        rows.append(
            {
                "team": team or "-",
                "player": row.player.name,
                "ss": ss_code,
                "fx": fx_code,
                "confirmed": ss_confirmed,
                "tds": proj_gs,
                "kickoff": kickoff,
                "kickoff_label": _format_kickoff(kickoff),
                "opponent": opponent,
                "order": order_index.get(pid, 999),
            }
        )
    rows.sort(key=lambda r: (r["team"], r["kickoff"] or datetime.max.replace(tzinfo=timezone.utc), r["order"]))
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
st.subheader("Lineup updates feed (terminal view)")
st.caption(
    "Projected lineups (SofaScore vs. Fantrax) grouped by EPL team. "
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

gw_num = inferred_round or selected_period_id
gw_label = f"GW {gw_num}" if gw_num else "GW"
schedule_events, schedule_source = _fetch_gameweek_events(inferred_round)
gw_meta = _build_gameweek_meta_from_events(schedule_events, gw_label)
if not gw_meta:
    gw_meta = _build_gameweek_meta(lineup_info_by_player, gw_label)
if gw_meta:
    st.markdown("**Gameweek overview**")
    st.code(json.dumps(gw_meta, indent=2), language="json")
    if schedule_events:
        tournament_id = st.session_state.get("sofascore_tournament_id", 17)
        expected_matches = 10 if int(tournament_id) == 17 else None
        source_label = schedule_source or "live"
        count_label = f"{len(schedule_events)} events"
        if expected_matches and len(schedule_events) != expected_matches:
            count_label += f" (expected {expected_matches})"
        st.caption(f"Schedule source: {source_label} | {count_label}")
    else:
        st.caption("Schedule fallback: derived from this roster's kickoffs only.")

projections_map = st.session_state.get("projections_map")
if projections_map is None:
    projections_map = _load_projections()
    st.session_state["projections_map"] = projections_map

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
    max-height: 320px;
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
.col-fx { width: 12%; }
.col-tds { width: 8%; }
.col-conf { width: 8%; }
.col-ko { width: 20%; }
</style>
"""

if feed_rows:
    header = """
    <tr>
        <th class='col-team'>Team</th>
        <th class='col-player'>Player vs Opp</th>
        <th class='col-ss'>SofaScore</th>
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
        st.dataframe(pd.DataFrame(status_changes), hide_index=True, use_container_width=True)

st.subheader("Make lineup adjustments before defining conditional rules")
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

# Ensure projections are loaded for optimization
projections_map = st.session_state.get("projections_map")
if projections_map is None:
    projections_map = _load_projections()
    st.session_state["projections_map"] = projections_map

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
with opt_cols[0]:
    st.markdown("**Optimized Actives**")
    st.dataframe(pd.DataFrame(opt_active_rows), use_container_width=True, hide_index=True)
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
    apply_label = "Apply optimized lineup to Fantrax" + (" (already applied)" if already_optimal else "")
    if st.button(apply_label, type="primary", key="apply_optimized_lineup", disabled=apply_disabled):
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

st.subheader("Make a single swap")

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

# _dump_debug_for_player(swap_active)

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
        "- SS / FX: numeric code (1 starter, 0 bench, -1 out) from SofaScore/Fantrax signals.\n"
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
    ss_effective = None
    fx_effective = getattr(info, "fx_status", None) if info else None
    if info:
        if getattr(info, "ss_conf_status", None):
            ss_effective = getattr(info, "ss_conf_status")
        elif getattr(info, "ss_pred_status", None):
            ss_effective = getattr(info, "ss_pred_status")
        else:
            ss_effective = getattr(info, "ss_status", None)
    ss_code = _status_code(ss_effective)
    fx_code = _status_code(fx_effective)
    active_display.append(
        {
            "Player": row.player.name,
            "Pos": _display_pos(row),
            "Team": team_name,
            "Opponent": opponent,
            "SS": ss_code,
            "FX": fx_code,
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
    ss_effective = None
    fx_effective = getattr(info, "fx_status", None) if info else None
    if info:
        if getattr(info, "ss_conf_status", None):
            ss_effective = getattr(info, "ss_conf_status")
        elif getattr(info, "ss_pred_status", None):
            ss_effective = getattr(info, "ss_pred_status")
        else:
            ss_effective = getattr(info, "ss_status", None)
    ss_code = _status_code(ss_effective)
    fx_code = _status_code(fx_effective)
    reserve_display.append(
        {
            "Player": row.player.name,
            "Pos": _display_pos(row),
            "Team": team_name,
            "Opponent": opponent,
            "SS": ss_code,
            "FX": fx_code,
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

st.caption("Active XI (codes: 1 starter, 0 bench, -1 out; confirmed=1 only when provider marks confirmed)")
st.dataframe(
    active_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Team": st.column_config.Column(width="small"),
        "SS": st.column_config.Column(width="small"),
        "FX": st.column_config.Column(width="small"),
        "Kickoff": st.column_config.Column(width="medium"),
    },
)
st.caption("Reserves (codes: 1 starter, 0 bench, -1 out; confirmed=1 only when provider marks confirmed)")
st.dataframe(
    reserve_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Team": st.column_config.Column(width="small"),
        "SS": st.column_config.Column(width="small"),
        "FX": st.column_config.Column(width="small"),
        "Kickoff": st.column_config.Column(width="medium"),
    },
)

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

projections_map = st.session_state.get("projections_map")
if projections_map is None:
    projections_map = _load_projections()
    st.session_state["projections_map"] = projections_map

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
        else:
            missing_projections_debug.append(
                {
                    "Player": row.player.name,
                    "Team": getattr(info, "team_name", None) if info else None,
                    "Key": _normalize_player_name(row.player.name),
                }
            )
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
        "Priority",
        "Swap Type",
        "Description",
        "Out",
        "In",
        "ProjFPts",
        "ProjGS",
        "Score",
        "Out KO",
        "In KO",
    ]
    view_df = sugg_df.copy()
    if "In KO dt" in view_df.columns:
        display_df = view_df[display_cols + ["In KO dt"]].copy()
        view_df_styled = display_df.style.apply(_kickoff_gradient, subset=["In KO dt"]).hide(
            axis="columns", subset=["In KO dt"]
        )
        st.dataframe(view_df_styled, hide_index=True, use_container_width=True)
    else:
        st.dataframe(view_df[display_cols], hide_index=True, use_container_width=True)

    edited = st.data_editor(
        sugg_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", help="Choose swaps to queue as conditional rules."),
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
                    append_rules_for_user(
                        str(user_id),
                        to_persist,
                        source="suggested_swaps",
                        source_type=1,
                    )
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
    with st.expander("Projection coverage debug"):
        st.dataframe(
            pd.DataFrame(missing_projections_debug).drop_duplicates(),
            hide_index=True,
            use_container_width=True,
        )

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

if selected_action_type == RuleActionType.LINEUP_SWAP and not user_id:
    submit_disabled = True
    st.info("Log in to save manual lineup swap rules.")

submitted = st.button("Save Rule", disabled=submit_disabled, type="primary", key="save_rule_btn")

if submitted and not submit_disabled:
    try:
        if selected_action_type == RuleActionType.LINEUP_SWAP:
            if not user_id:
                st.warning("Log in to save manual lineup swap rules.")
                st.stop()
            try:
                existing_rules = load_rules_for_user(str(user_id))
            except Exception:
                existing_rules = []
            already_exists = False
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
                    already_exists = True
                    break
            if already_exists:
                st.warning("A manual rule already exists for this active player and period.")
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
            append_rules_for_user(
                str(user_id),
                to_persist,
                source="manual",
                source_type=3,
                default_max_fires=1,
            )
            st.success("Rule saved successfully.")
            st.rerun()
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
            legacy_storage.save_rule(rule)
            st.success("Rule saved successfully (legacy FA rule).")
            st.rerun()
    except ValueError as exc:
        st.warning(str(exc))
    except Exception as exc:  # pragma: no cover - runtime feedback
        logger.exception("Failed to save conditional rule")
        st.error(f"Failed to save rule: {exc}")


# ----------------------------------------------------------------------
# Existing rules (manual, per-user storage)
# ----------------------------------------------------------------------
manual_rules: List[Dict[str, Any]] = []
legacy_fa_rules: List[ConditionalSwapRule] = []

if user_id:
    try:
        existing_rules = load_rules_for_user(str(user_id))
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
            if legacy.action_type == RuleActionType.FA_CLAIM_DROP:
                legacy_fa_rules.append(legacy)
                continue
            if legacy.id in existing_origin:
                continue
            state = "disabled" if legacy.state == RuleState.DISABLED else "active"
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
            append_rules_for_user(
                str(user_id),
                to_migrate,
                source="manual",
                source_type=3,
                default_max_fires=1,
            )
            try:
                existing_rules = load_rules_for_user(str(user_id))
            except Exception:
                existing_rules = []

    manual_rules = [
        r
        for r in existing_rules
        if _is_manual_rule(r)
        and str(r.get("league_id")) == str(league_id)
        and str(r.get("team_id")) == str(team_id)
    ]
else:
    legacy_rules = legacy_storage.load_rules_for_team(league_id, team_id)
    legacy_fa_rules = [r for r in legacy_rules if r.action_type == RuleActionType.FA_CLAIM_DROP]

if not manual_rules and not legacy_fa_rules:
    st.info("No manual conditional rules configured yet.")
else:
    if manual_rules:
        st.divider()
        st.subheader("Existing Conditional Rules")
        grouped: Dict[str, Dict[str, Any]] = {}
        for r in manual_rules:
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
            desc = _swap_description(active_name, eligible_names)
            st.markdown(f"**Swap type:** {swap_type}")
            st.markdown(f"**Description:** {desc}")
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
                ):
                    try:
                        path = rules_path_for_user(str(user_id))
                        updated_rules = load_rules_for_user(str(user_id))
                        for r in updated_rules:
                            if not _is_manual_rule(r):
                                continue
                            in_group = False
                            if r.get("group_id"):
                                in_group = str(r.get("group_id")) == group_id
                            else:
                                in_group = (
                                    str(r.get("active_id")) == active_id
                                    and str(r.get("period")) == str(group.get("period"))
                                )
                            if in_group:
                                r["state"] = "disabled" if state_text == "active" else "active"
                        save_rules(updated_rules, path=path)
                        st.rerun()
                    except Exception:
                        st.warning("Failed to update rule state.")
            else:
                action_cols[0].write("")

            if action_cols[1].button(
                "Delete",
                key=f"delete_manual_{group_id}",
                use_container_width=True,
            ):
                try:
                    path = rules_path_for_user(str(user_id))
                    updated_rules = load_rules_for_user(str(user_id))
                    retained = []
                    for r in updated_rules:
                        if not _is_manual_rule(r):
                            retained.append(r)
                            continue
                        in_group = False
                        if r.get("group_id"):
                            in_group = str(r.get("group_id")) == group_id
                        else:
                            in_group = (
                                str(r.get("active_id")) == active_id
                                and str(r.get("period")) == str(group.get("period"))
                            )
                        if not in_group:
                            retained.append(r)
                    save_rules(retained, path=path)
                    st.rerun()
                except Exception:
                    st.warning("Failed to delete rule group.")

            st.caption(f"Rule group ID: {group_id}")
            st.markdown("---")

    if legacy_fa_rules:
        st.divider()
        st.subheader("Legacy FA claim/drop rules")
        st.caption("FA claim rules are stored in legacy storage and are not executed by the runner yet.")
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
        raw_rules = load_rules_for_user(str(user_id))
    except Exception:
        raw_rules = []
    for rule in raw_rules:
        if str(rule.get("league_id")) != str(league_id) or str(rule.get("team_id")) != str(team_id):
            continue
        source = str(rule.get("source") or "")
        if source not in {"auto_lineup_swaps", "auto"}:
            continue
        auto_rules.append(rule)

    if not auto_rules:
        st.caption(
            "No auto rules are available for this roster/period right now. "
            "This usually means there are no legal backup swaps given the current lineup."
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
