"""
Headless conditional swap runner.

Reads rules from data/conditional_rules.json (or per-user rules under
data/conditional_rules/<user_id>.json), refreshes lineup info, and executes
eligible swaps via Fantrax SubsService.

Usage:
  python scripts/conditional_runner.py \
    --league-id <LEAGUE> --team-id <TEAM> [--period <PERIOD>] [--dry-run]
  python scripts/conditional_runner.py --user-id <USER_ID>
  python scripts/conditional_runner.py --all-users [--force-trigger]

Notes:
- Auth/session rebuild uses utils.auth_helpers.load_requests_session_from_artifacts
  with auth_artifacts.json by default; override with --auth-artifacts.
- Per-user runs use cookies stored in data/auth/ via utils.user_manager.
- Rules are marked as fired in their respective rules file (state=fired, fired_at).
- Triggers supported: "confirmed_lineup" (fires when active is not STARTING).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.lineups.conditional_swaps import (
    LineupStatus,
    RosterView,
    can_swap_in_period,
    get_available_periods,
)
from fantraxapi.lineups.lineup_resolver import LineupSourceStrategy, resolve_lineup_info
from fantraxapi.lineups.fantrax_lineup_bridge import (
    fetch_fa_status_map,
    global_status_path_for_user,
)
from fantraxapi.lineups.sofascore_bridge import infer_current_gameweek
from fantraxapi.subs import SubsService
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.objs import RosterRow
from fantraxapi.waivers import WaiversService

from utils.conditional_rule_store import (
    AUTO_RULE_PRUNE_FIRED_FROM_RULES,
    DEFAULT_RULES_PATH,
    SOURCE_AUTO_LINEUP_SWAPS,
    append_execution_event_for_user,
    is_conditional_state_writer,
    load_rules,
    load_rules_with_locks,
    load_rules_for_user,
    load_rules_for_user_with_locks,
    normalize_rule_source,
    rules_path_for_user,
    save_rules,
)
from utils.user_manager import UserManager

try:
    from utils.auth_helpers import load_requests_session_from_artifacts
except Exception:
    load_requests_session_from_artifacts = None  # type: ignore

LOG_PATH = Path("data/logs/conditional_runner.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_TIMEZONE = "America/Los_Angeles"
LOG_MAX_BYTES = 2_000_000  # 2 MB
LOG_BACKUP_COUNT = 5

def _log_time_converter(*_args):
    return datetime.now(ZoneInfo(LOG_TIMEZONE)).timetuple()

logging.Formatter.converter = _log_time_converter
_root = logging.getLogger()
_fmt = logging.Formatter("%(asctime)s %(levelname)s [runner] %(message)s")
if not any(
    isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == str(LOG_PATH)
    for h in _root.handlers
):
    _fh = RotatingFileHandler(LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    _fh.setFormatter(_fmt)
    _root.addHandler(_fh)
_root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)
PROJECTIONS_PATH = Path("data/derived/projections.parquet")
AUTO_MAX_BACKUPS = 3
AUTO_MAX_FA_CANDIDATES = 3
FA_POOL_LIMIT = 200
FA_TRIGGER_MODE_FA_STARTING_ONLY = "fa_starting_only"
FA_TRIGGER_MODE_DROP_AND_FA_STARTING = "drop_not_starting_and_fa_starting"
FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE = "drop_not_starting_then_claim_immediate"
ENABLE_AUTO_CLAIMS = False
CLAIMS_TEST_LEAGUE_ID = "0z7r5871mc1yqc0s"
CONFIRM_WINDOW_MINUTES = 60
LOCK_DIR = Path("data/locks/conditional_runner")
LOCK_DIR.mkdir(parents=True, exist_ok=True)
LOCK_TTL_SECONDS = 300
STATE_WRITER_ENABLED = is_conditional_state_writer()


def _record_execution_event(
    *,
    user_id: Optional[str],
    league_id: Any,
    team_id: Any,
    period: Any,
    rule: Dict[str, Any],
    result: str,
    reason: str = "",
    fantrax_tx_set_id: Optional[str] = None,
    event_type: str = "execution",
) -> None:
    if not user_id:
        return
    try:
        append_execution_event_for_user(
            str(user_id),
            {
                "event_type": event_type,
                "occurred_at_utc": _now().isoformat(),
                "league_id": str(league_id or ""),
                "team_id": str(team_id or ""),
                "period": str(period or ""),
                "rule_id": str(rule.get("rule_id") or ""),
                "group_id": str(rule.get("group_id") or ""),
                "source": normalize_rule_source(str(rule.get("source") or "")),
                "action_type": str(rule.get("action_type") or "lineup_swap"),
                "active_id": str(rule.get("active_id") or ""),
                "reserve_id": str(rule.get("reserve_id") or ""),
                "result": str(result or ""),
                "reason": str(reason or ""),
                "fantrax_tx_set_id": fantrax_tx_set_id,
                "rule_snapshot": dict(rule),
                "fired_at": str(rule.get("fired_at") or ""),
            },
        )
    except Exception as exc:
        logger.info(
            "Execution journal append failed (user=%s league=%s team=%s rule=%s): %s",
            user_id,
            league_id,
            team_id,
            rule.get("rule_id"),
            exc,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _format_datetime_for_user(dt: Optional[datetime], tz_name: str) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name or LOG_TIMEZONE)
    except Exception:
        zone = timezone.utc
    try:
        return dt.astimezone(zone).isoformat()
    except Exception:
        return dt.isoformat()


def _resolve_user_timezone(user_mgr: Optional[UserManager], user_id: Optional[str]) -> str:
    if not user_mgr or not user_id:
        return LOG_TIMEZONE
    try:
        tz = user_mgr.get_timezone(str(user_id))
    except Exception:
        tz = None
    return tz or LOG_TIMEZONE


def _sanitize_lock_segment(value: Optional[Any]) -> str:
    if value is None:
        return "none"
    raw = str(value)
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw)
    return safe or "none"


def _lock_file_path(league_id: str, team_id: str, period_id: Optional[int]) -> Path:
    file_name = f"{_sanitize_lock_segment(league_id)}_{_sanitize_lock_segment(team_id)}_{_sanitize_lock_segment(period_id)}.lock"
    return LOCK_DIR / file_name


def _is_lock_stale(lock_path: Path, now: datetime) -> bool:
    try:
        payload = json.loads(lock_path.read_text())
        locked_at = payload.get("locked_at")
        if not locked_at:
            return False
        ts = datetime.fromisoformat(locked_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() > LOCK_TTL_SECONDS
    except Exception:
        return True


def _try_acquire_run_lock(
    *,
    league_id: str,
    team_id: str,
    period_id: Optional[int],
) -> tuple[bool, Optional[Path]]:
    lock_path = _lock_file_path(league_id, team_id, period_id)
    now = _now()
    if lock_path.exists():
        if not _is_lock_stale(lock_path, now):
            return False, None
        try:
            lock_path.unlink()
        except Exception:
            pass
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "league_id": league_id,
                    "team_id": team_id,
                    "period": str(period_id) if period_id is not None else "",
                    "locked_at": now.isoformat(),
                    "pid": os.getpid(),
                }
            )
        )
        return True, lock_path
    except FileExistsError:
        return False, None


def _release_run_lock(path: Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


@contextmanager
def _run_lock(
    *,
    league_id: str,
    team_id: str,
    period_id: Optional[int],
) -> tuple[bool, Optional[Path]]:
    acquired, path = _try_acquire_run_lock(
        league_id=league_id,
        team_id=team_id,
        period_id=period_id,
    )
    try:
        yield (acquired, path)
    finally:
        if acquired and path:
            _release_run_lock(path)


def _rule_action_type(rule: Dict[str, Any]) -> str:
    return str(rule.get("action_type") or "").lower()

def _fa_trigger_mode(rule: Dict[str, Any]) -> str:
    raw = str(rule.get("fa_trigger_mode") or "").strip().lower()
    if raw == FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE:
        return FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE
    if raw == FA_TRIGGER_MODE_DROP_AND_FA_STARTING:
        return FA_TRIGGER_MODE_DROP_AND_FA_STARTING
    return FA_TRIGGER_MODE_FA_STARTING_ONLY


def _is_fa_action(rule: Dict[str, Any]) -> bool:
    return _rule_action_type(rule) in {"fa_claim_drop", "fa_add_only", "drop_only"}


def _eligible(rule: Dict[str, Any]) -> bool:
    """
    Determine if a rule is eligible to be executed.
    """
    state = str(rule.get("state") or "").lower()
    if state == "fired":
        return False
    if state in {"disabled", "inactive", "off"}:
        return False
    fired_count = int(rule.get("fired_count") or 0)
    max_fires = int(rule.get("max_fires") or 1)
    if fired_count >= max_fires:
        return False
    if not rule.get("active_id") or not rule.get("reserve_id"):
        return False
    return True


def _eligible_fa_rule(rule: Dict[str, Any]) -> bool:
    """
    Determine if a FA claim/drop rule is eligible to be executed.
    """
    if not _is_fa_action(rule):
        return False
    state = str(rule.get("state") or "").lower()
    if state == "fired":
        return False
    if state in {"disabled", "inactive", "off"}:
        return False
    fired_count = int(rule.get("fired_count") or 0)
    max_fires = int(rule.get("max_fires") or 1)
    if fired_count >= max_fires:
        return False
    action_type = _rule_action_type(rule)
    add_id = rule.get("fa_add_scorer_id") or rule.get("fa_add_id")
    drop_id = rule.get("drop_player_id") or rule.get("active_id")
    if action_type == "drop_only":
        return bool(drop_id)
    if action_type == "fa_add_only":
        return bool(add_id)
    return bool(add_id and drop_id)


def _normalize_player_name(name: Optional[str]) -> Optional[str]:
    """
    Normalize a player name to a consistent format.
    """
    if not name:
        return None
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return "".join(ch for ch in normalized.lower() if ch.isalnum())


def _canonical_team_code(raw: Optional[str]) -> str:
    """
    Convert a team code to a canonical format.
    """
    if raw is None:
        return ""
    base = "".join(ch for ch in str(raw).lower() if ch.isalnum())
    return base.upper() if base else ""


def _match_period_from_round(
    round_hint: Optional[str],
    period_id_map: Dict[str, str],
) -> Optional[str]:
    """
    Match a SofaScore round (gameweek) to a Fantrax period id using period labels.
    Mirrors the UI selection logic for period selection in the app.
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


def _resolve_period_like_ui(
    *,
    api: FantraxAPI,
    league_id: str,
    team_id: str,
    session: Any,
    preferred_period: Optional[int],
) -> Tuple[Optional[int], Optional[str]]:
    periods: List[Dict[str, str]] = []
    try:
        periods = get_available_periods(league_id=league_id, team_id=team_id, session=session)
    except Exception as exc:
        logger.info("Failed to load roster-change periods for league=%s team=%s: %s", league_id, team_id, exc)
        return None, None

    period_id_map = {str(opt["id"]): opt["label"] for opt in periods}
    period_choices = list(period_id_map.keys())
    if not period_choices:
        return None, None

    if preferred_period is not None and str(preferred_period) in period_id_map:
        chosen = str(preferred_period)
        return int(chosen), period_id_map.get(chosen, "")

    inferred_round = infer_current_gameweek()
    inferred_match = _match_period_from_round(inferred_round, period_id_map)
    if inferred_match and inferred_match in period_id_map:
        return int(inferred_match), period_id_map.get(inferred_match, "")

    detected_period = None
    try:
        detected_period = str(api.resolve_active_period(team_id))
    except Exception:
        detected_period = None

    if detected_period and detected_period in period_id_map:
        return int(detected_period), period_id_map.get(detected_period, "")

    try:
        return int(period_choices[0]), period_id_map.get(period_choices[0], "")
    except Exception:
        return None, None


def _normalize_projection_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize projection column names to consistent formats.
    """
    rename_map = {
        "projfpts": "ProjFPts",
        "projgpts": "ProjGPts",
        "%rost": "Ros%",
        "rost%": "Ros%",
        "projmins": "ProjMins",
    }
    return df.rename(columns={col: rename_map.get(col.lower(), col) for col in df.columns})


def _load_projections_map(path: Path = PROJECTIONS_PATH) -> Dict[Tuple[str, str], dict]:
    """
    Load a mapping of (player_name, team_code) -> projection row.
    """
    if not path.exists():
        logger.info("Projections file missing at %s; skipping auto rule generation", path)
        return {}
    try:
        df = _normalize_projection_columns(pd.read_parquet(path))
    except Exception as exc:
        logger.error("Failed to read projections at %s: %s", path, exc)
        return {}
    proj_map: Dict[Tuple[str, str], dict] = {}
    for _, row in df.iterrows():
        name_key = _normalize_player_name(row.get("Player"))
        team_key = _canonical_team_code(row.get("Team"))
        if name_key:
            proj_map[(name_key, team_key)] = row.to_dict()
            proj_map[(name_key, "")] = row.to_dict()
    return proj_map


def _update_never_drop_auto(
    *,
    user_mgr: Optional[UserManager],
    user_id: Optional[str],
    league_id: str,
    team_id: str,
    roster: Any,
    waivers_service: WaiversService,
) -> set[str]:
    if not user_mgr or not user_id:
        return set()
    if str(league_id) != CLAIMS_TEST_LEAGUE_ID:
        return set(user_mgr.get_never_drop(str(user_id), str(league_id)) or [])
    try:
        top_players = waivers_service.list_top_players_by_fpts(limit=50, status="ALL")
    except Exception as exc:
        logger.info("Failed to refresh never-drop list: %s", exc)
        return set(user_mgr.get_never_drop(str(user_id), str(league_id)) or [])
    top_ids = {str(p.get("id")) for p in top_players if p.get("id")}
    auto_ids: list[str] = []
    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue
        pid = str(player.id)
        if pid in top_ids:
            auto_ids.append(pid)
    state = user_mgr.get_never_drop_state(str(user_id), str(league_id))
    manual_add = list(state.get("manual_add") or [])
    manual_remove = list(state.get("manual_remove") or [])
    stored_auto = set(state.get("auto") or [])
    auto_set = set(auto_ids)
    if stored_auto != auto_set:
        user_mgr.set_never_drop_state(
            user_id=str(user_id),
            league_id=str(league_id),
            auto_ids=auto_ids,
            manual_add=manual_add,
            manual_remove=manual_remove,
            team_id=str(team_id),
        )
    return (auto_set - set(manual_remove)) | set(manual_add)


def _build_kos_index_map(
    lineup_info_by_player: Dict[str, Any],
) -> Tuple[Dict[str, int], Optional[datetime], Optional[datetime]]:
    """
    Build a mapping of player id to KOS index, and determine the first and last KOS times.
    """
    kickoff_times = sorted({info.kickoff for info in lineup_info_by_player.values() if info and info.kickoff})
    kickoff_to_index = {ko: idx + 1 for idx, ko in enumerate(kickoff_times)}
    kos_map: Dict[str, int] = {}
    for pid, info in lineup_info_by_player.items():
        if info and info.kickoff in kickoff_to_index:
            kos_map[str(pid)] = kickoff_to_index[info.kickoff]
    first_kos = kickoff_times[0] if kickoff_times else None
    last_kos = kickoff_times[-1] if kickoff_times else None
    return kos_map, first_kos, last_kos


def _player_label(roster_view: RosterView, player_id: str) -> str:
    """
    Get a human-readable label for a player.
    """
    row = roster_view.get_row(str(player_id))
    if row and getattr(row, "player", None) and getattr(row.player, "name", None):
        return f"{row.player.name} ({player_id})"
    return str(player_id)


def _status_kind(info: Optional[Any]) -> str:
    """
    Determine the status kind of a player.
    """
    if not info or info.status is None:
        return "unconfirmed"
    if info.status == LineupStatus.STARTING:
        return "starting"
    if info.status in (LineupStatus.OUT, LineupStatus.BENCH, LineupStatus.DOUBTFUL):
        return "not_starting"
    return "unconfirmed"

def _confirmed_status(info: Optional[Any]) -> Optional[LineupStatus]:
    """
    Return a confirmed lineup status when available.

    NOTE: For automation we want the `confirmed_lineup` trigger to be able to run
    on the VPS without requiring synced SofaScore lineup snapshots. Therefore we
    treat Fantrax confirmed flags as a first-class confirmed source (primary),
    and use SofaScore confirmed status as a fallback when Fantrax doesn't have a
    signal.
    """
    if not info:
        return None

    # Prefer Fantrax "confirmed" flags (icons / FXPA status list).
    fx_status = getattr(info, "fx_conf_status", None) or getattr(info, "fx_status", None)
    if fx_status is not None:
        if isinstance(fx_status, LineupStatus):
            if fx_status != LineupStatus.UNKNOWN:
                return fx_status
        else:
            try:
                norm = LineupStatus(str(fx_status).lower())
                if norm != LineupStatus.UNKNOWN:
                    return norm
            except Exception:
                pass

    # Fall back to SofaScore confirmed status when Fantrax doesn't have a signal.
    ss_conf = getattr(info, "ss_conf_status", None)
    if ss_conf is None:
        return None
    if isinstance(ss_conf, LineupStatus):
        return ss_conf if ss_conf != LineupStatus.UNKNOWN else None
    try:
        norm = LineupStatus(str(ss_conf).lower())
        return norm if norm != LineupStatus.UNKNOWN else None
    except Exception:
        return None


def _confirmed_status_kind(info: Optional[Any]) -> str:
    """
    Determine the status kind using confirmed lineups only.
    """
    status = _confirmed_status(info)
    if status is None:
        return "unconfirmed"
    if isinstance(status, LineupStatus):
        if status == LineupStatus.STARTING:
            return "starting"
        if status in (LineupStatus.OUT, LineupStatus.BENCH, LineupStatus.DOUBTFUL):
            return "not_starting"
        return "unconfirmed"
    val = str(status).lower()
    if val == LineupStatus.STARTING.value:
        return "starting"
    if val in {
        LineupStatus.OUT.value,
        LineupStatus.BENCH.value,
        LineupStatus.DOUBTFUL.value,
    }:
        return "not_starting"
    return "unconfirmed"


def _lock_bucket_key(league_id: str, team_id: str, period_id: Optional[int]) -> str:
    period_key = str(period_id) if period_id is not None else ""
    return f"{league_id}:{team_id}:{period_key}"


def _lock_bucket(
    player_locks: Dict[str, Any],
    *,
    league_id: str,
    team_id: str,
    period_id: Optional[int],
) -> Dict[str, Any]:
    key = _lock_bucket_key(league_id, team_id, period_id)
    bucket = player_locks.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        player_locks[key] = bucket
    return bucket


def _is_player_locked(bucket: Dict[str, Any], player_id: str) -> bool:
    entry = bucket.get(str(player_id))
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("locked", True))


def _lock_player(
    bucket: Dict[str, Any],
    *,
    player_id: str,
    reason: str,
    rule_id: Optional[str],
    locked_at: Optional[str] = None,
) -> None:
    bucket[str(player_id)] = {
        "locked": True,
        "reason": reason,
        "rule_id": rule_id,
        "locked_at": locked_at or _now().isoformat(),
    }


def _summarize_candidates(candidates: List[Dict[str, Any]], user_timezone: str = "UTC") -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for c in candidates:
        kickoff = c.get("kickoff")
        kickoff_local = _format_datetime_for_user(kickoff, user_timezone)
        summary.append(
            {
                "reserve_id": c.get("reserve_id"),
                "status": c.get("status"),
                "candidate_classification": c.get("candidate_classification"),
                "proj_gs": c.get("proj_gs"),
                "proj_fpts": c.get("proj_fpts"),
                "kos_index": c.get("kos_index"),
                "locked": c.get("locked"),
                "lock_bypass": c.get("lock_bypass"),
                "kickoff": kickoff_local,
                "kickoff_local": kickoff_local,
            }
        )
    return summary


def _log_trace(
    trace_enabled: bool,
    *,
    logger: logging.Logger,
    meta: Dict[str, Any],
    ready: bool,
    skip_reason: Optional[str] = None,
    candidates: Optional[List[Dict[str, Any]]] = None,
    chosen: Optional[Dict[str, Any]] = None,
    lock_bypass: Optional[bool] = None,
    result: Optional[str] = None,
    lineup_diff: Optional[Dict[str, Any]] = None,
) -> None:
    if not trace_enabled:
        return
    payload = dict(meta)
    payload["ready"] = ready
    if skip_reason:
        payload["skip_reason"] = skip_reason
    if candidates is not None:
        payload["candidates"] = candidates
    if chosen is not None:
        payload["chosen"] = chosen
    if lock_bypass is not None:
        payload["lock_bypass"] = lock_bypass
    if result:
        payload["result"] = result
    if lineup_diff is not None:
        payload["lineup_diff"] = lineup_diff
    logger.info("Trace rule eval: %s", json.dumps(payload))


def _rule_priority_key(rule: Dict[str, Any]) -> tuple:
    try:
        priority = int(rule.get("priority") or 999)
    except Exception:
        priority = 999
    created_at = str(rule.get("created_at") or "")
    rule_id = str(rule.get("rule_id") or "")
    return (priority, created_at, rule_id)


@dataclass(frozen=True)
class ActionCandidate:
    action_family: str
    source: str
    rule_id: str
    rule_ref: Dict[str, Any]
    kos_index: Optional[int]
    kickoff: Optional[datetime]
    tie_rank: int
    priority_key: tuple


def _kickoff_to_kos_index(
    kickoff: Optional[datetime],
    kos_index_map: Dict[str, int],
    lineup_info_by_player: Dict[str, Any],
) -> Optional[int]:
    if kickoff is None:
        return None
    best_kos: Optional[int] = None
    best_delta: Optional[float] = None
    for pid, info in lineup_info_by_player.items():
        if not info:
            continue
        player_kickoff = getattr(info, "kickoff", None)
        kos_idx = kos_index_map.get(str(pid))
        if player_kickoff is None or kos_idx is None:
            continue
        try:
            delta = abs((player_kickoff - kickoff).total_seconds())
        except Exception:
            continue
        if delta > 300:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best_kos = int(kos_idx)
    return best_kos


def _fa_action_kickoff(
    rule: Dict[str, Any],
    lineup_info_by_player: Dict[str, Any],
    fa_status_map: Dict[str, Any],
) -> Optional[datetime]:
    action_type = _rule_action_type(rule)
    drop_id = str(rule.get("drop_player_id") or rule.get("active_id") or "")
    add_id = str(rule.get("fa_add_scorer_id") or rule.get("fa_add_id") or "")
    drop_info = lineup_info_by_player.get(drop_id) if drop_id else None
    drop_kickoff = getattr(drop_info, "kickoff", None) if drop_info else None
    fa_snapshot = fa_status_map.get(add_id) if add_id else None
    add_kickoff = getattr(fa_snapshot, "kickoff", None) if fa_snapshot else None
    if action_type == "drop_only":
        return drop_kickoff
    if action_type == "fa_add_only":
        return add_kickoff
    kickoffs = [dt for dt in (drop_kickoff, add_kickoff) if dt is not None]
    return min(kickoffs) if kickoffs else None


def _action_precedence_key(candidate: ActionCandidate) -> tuple:
    # Lower wins: known earlier KOS first, then earlier kickoff, then family/source tie-breaks.
    kos_key = candidate.kos_index if candidate.kos_index is not None else 999
    kickoff_key = candidate.kickoff or datetime.max.replace(tzinfo=timezone.utc)
    return (
        kos_key,
        kickoff_key,
        candidate.tie_rank,
        candidate.priority_key,
    )


def _best_pending_swap_candidate(
    rules: List[Dict[str, Any]],
    *,
    league_id: str,
    team_id: str,
    chosen_period: Optional[int],
    kos_index_map: Dict[str, int],
    lineup_info_by_player: Dict[str, Any],
    user_mgr: Optional[UserManager] = None,
    user_id: Optional[str] = None,
) -> Optional[ActionCandidate]:
    best: Optional[ActionCandidate] = None
    for rule in rules:
        if not _eligible(rule):
            continue
        if _is_fa_action(rule):
            continue
        if str(rule.get("league_id")) != str(league_id) or str(rule.get("team_id")) != str(team_id):
            continue
        rule_period = rule.get("period")
        if rule_period is not None and chosen_period is not None and str(rule_period) != str(chosen_period):
            continue
        active_id = str(rule.get("active_id") or "")
        if not active_id:
            continue
        info = lineup_info_by_player.get(active_id)
        kickoff = getattr(info, "kickoff", None) if info else None
        kos_index = kos_index_map.get(active_id)
        source = normalize_rule_source(str(rule.get("source") or "manual"))
        if (
            source == SOURCE_AUTO_LINEUP_SWAPS
            and user_id
            and user_mgr
            and not user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
        ):
            continue
        # Tie rank: claim/drop beats swap elsewhere (0). Here we rank swaps and prefer manual over auto.
        tie_rank = 2 if source == SOURCE_AUTO_LINEUP_SWAPS else 1
        candidate = ActionCandidate(
            action_family="lineup_swap",
            source=source,
            rule_id=str(rule.get("rule_id") or ""),
            rule_ref=rule,
            kos_index=kos_index,
            kickoff=kickoff,
            tie_rank=tie_rank,
            priority_key=_rule_priority_key(rule),
        )
        if best is None or _action_precedence_key(candidate) < _action_precedence_key(best):
            best = candidate
    return best


def _order_auto_unconfirmed_fallback_candidates(
    candidates: List[Dict[str, Any]],
    *,
    active_kos_index: Optional[int],
) -> List[Dict[str, Any]]:
    """
    Rank auto fallback reserves by:
    1) KOS ordering relative to active
    2) projected starts (ProjGS)
    3) projected fantasy points (ProjFPts)
    4) deterministic reserve id tie-breaker
    """

    def _kos_rank(kos_val: Optional[int]) -> int:
        if kos_val is None:
            return 999
        if active_kos_index is None:
            return int(kos_val)
        if kos_val == active_kos_index:
            return 0
        if kos_val > active_kos_index:
            return int(kos_val - active_kos_index)
        return 100 + int(active_kos_index - kos_val)

    def _sort_key(c: Dict[str, Any]) -> tuple:
        try:
            proj_gs = int(c.get("proj_gs") or 0)
        except Exception:
            proj_gs = 0
        try:
            proj_fpts = float(c.get("proj_fpts") or 0.0)
        except Exception:
            proj_fpts = 0.0
        return (
            _kos_rank(c.get("kos_index")),
            -proj_gs,
            -proj_fpts,
            str(c.get("reserve_id") or ""),
        )

    return sorted(candidates, key=_sort_key)


def _apply_inverse_rule_guard(
    rules: List[Dict[str, Any]],
    *,
    league_id: str,
    team_id: str,
) -> bool:
    """
    Detect inverse rules (A->B and B->A) within the same league/team/period.
    Disable the lower-priority rule deterministically.
    """
    updated = False
    index: Dict[tuple, Dict[str, Any]] = {}
    for rule in rules:
        if str(rule.get("league_id")) != str(league_id):
            continue
        if str(rule.get("team_id")) != str(team_id):
            continue
        state = str(rule.get("state") or "").lower()
        if state in {"disabled", "inactive", "off"}:
            continue
        active_id = str(rule.get("active_id") or "")
        reserve_id = str(rule.get("reserve_id") or "")
        if not active_id or not reserve_id:
            continue
        period_id = str(rule.get("period") or "")
        key = (period_id, active_id, reserve_id)
        inv_key = (period_id, reserve_id, active_id)
        other = index.get(inv_key)
        if other is None:
            index[key] = rule
            continue
        # Decide winner by priority, then created_at, then rule_id.
        if _rule_priority_key(rule) < _rule_priority_key(other):
            winner = rule
            loser = other
            index[inv_key] = rule
        else:
            winner = other
            loser = rule
        if str(loser.get("state") or "").lower() != "disabled":
            loser["state"] = "disabled"
            loser["disabled_at"] = _now().isoformat()
            loser["disabled_reason"] = "inverse_rule_conflict"
            updated = True
            logger.info(
                "Rule %s disabled: inverse conflict with %s",
                loser.get("rule_id"),
                winner.get("rule_id"),
            )
    return updated

def _in_confirmation_window(kickoff: Optional[datetime], now: datetime) -> bool:
    """
    Determine if a player is in the confirmation window.
    """
    if not kickoff:
        return False
    window_start = kickoff - timedelta(minutes=CONFIRM_WINDOW_MINUTES)
    return window_start <= now <= kickoff


def _pos_short(row: RosterRow) -> str:
    """
    Get the short position code for a player.
    """
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


def _format_player_label(name: str, pos: str) -> str:
    """
    Format a player label with position.
    """
    pos_clean = (pos or "").strip()
    return f"{name} ({pos_clean})" if pos_clean else name


def _player_team_code(row: RosterRow) -> str:
    """
    Get the team code for a player.
    """
    player = getattr(row, "player", None)
    for attr in ("team_short", "teamShort", "team", "team_name", "teamName"):
        if hasattr(player, attr):
            val = getattr(player, attr, None)
            if val:
                return _canonical_team_code(val)
    return ""


def _projection_for_row(row: RosterRow, projections: Dict[Tuple[str, str], dict]) -> Tuple[float, int]:
    """
    Get the projection for a player.
    """
    player = getattr(row, "player", None)
    name_key = _normalize_player_name(getattr(player, "name", None))
    if not name_key:
        return 0.0, 0
    team_code = _player_team_code(row)
    proj_row = projections.get((name_key, team_code)) or projections.get((name_key, ""))
    if not proj_row:
        return 0.0, 0
    proj = 0.0
    proj_gs = 0
    try:
        proj = float(proj_row.get("ProjFPts"))
    except Exception:
        proj = 0.0
    try:
        proj_gs = int(proj_row.get("ProjGS"))
    except Exception:
        proj_gs = 0
    return proj, proj_gs


def _has_projection_for_row(row: Optional[RosterRow], projections: Dict[Tuple[str, str], dict]) -> bool:
    """
    Return True only when a concrete projection row exists for this roster player.
    """
    if row is None:
        return False
    player = getattr(row, "player", None)
    name_key = _normalize_player_name(getattr(player, "name", None))
    if not name_key:
        return False
    team_code = _player_team_code(row)
    proj_row = projections.get((name_key, team_code)) or projections.get((name_key, ""))
    return proj_row is not None


def _projection_for_name_and_team(
    name: Optional[str],
    team: Optional[str],
    projections: Dict[Tuple[str, str], dict],
) -> Tuple[float, int]:
    """
    Get the projection for a free agent candidate by name/team.
    """
    name_key = _normalize_player_name(name)
    if not name_key:
        return 0.0, 0
    team_code = _canonical_team_code(team)
    proj_row = projections.get((name_key, team_code)) or projections.get((name_key, ""))
    if not proj_row:
        return 0.0, 0
    proj = 0.0
    proj_gs = 0
    try:
        proj = float(proj_row.get("ProjFPts"))
    except Exception:
        proj = 0.0
    try:
        proj_gs = int(proj_row.get("ProjGS"))
    except Exception:
        proj_gs = 0
    return proj, proj_gs


def _fa_position_matches_slot(fa_pos: Optional[str], slot_pos: str) -> bool:
    """
    Check if an FA candidate's position list can fill the target slot.
    """
    if not slot_pos:
        return True
    raw = str(fa_pos or "").upper()
    if not raw:
        return True
    tokens = {tok.strip() for tok in raw.replace("/", ",").split(",") if tok.strip()}
    return slot_pos.upper() in tokens if tokens else True


def _drop_would_keep_roster_legal(roster_view: RosterView, drop_id: str, *, min_gks: int = 1) -> bool:
    """
    Quick local invariant: do not drop to zero goalkeepers.
    """
    gk_count = 0
    for row in roster_view.roster.rows:
        if not getattr(row, "player", None):
            continue
        pid = str(row.player.id)
        if pid == drop_id:
            continue
        pos = _pos_short(row).upper()
        if pos == "G":
            gk_count += 1
    return gk_count >= min_gks


def _open_roster_slots(roster: Any) -> tuple[list[RosterRow], list[RosterRow]]:
    """
    Return (open_active_slots, open_reserve_slots) from the roster payload.
    """
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


def _sort_players_by_projection(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort players by projection.
    """
    return sorted(
        players,
        key=lambda p: (
            -p["proj_fpts"],
            -(p.get("kos_index") or -1),
        ),
    )


def _order_reserve_candidates_by_kos(
    candidates: List[Dict[str, Any]],
    active_kos_index: Optional[int],
) -> List[Dict[str, Any]]:
    """
    Order reserves with same-KOS first, then later KOS, using ProjFPts as tie-breaker.
    """
    def sort_key(item: Dict[str, Any]) -> Tuple[float, str]:
        """
        Sort key for reserve candidates.
        """
        try:
            proj_val = float(item.get("proj_fpts") or 0.0)
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

    ordered: List[Dict[str, Any]] = sorted(same_kos, key=sort_key)
    for kos in sorted({c.get("kos_index") for c in later_kos}):
        group = [c for c in later_kos if c.get("kos_index") == kos]
        ordered.extend(sorted(group, key=sort_key))
    ordered.extend(sorted(unknown_kos, key=sort_key))
    return ordered


def _generate_auto_swap_rules(
    *,
    roster: Any,
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, Any],
    kos_index_map: Dict[str, int],
    subs_service: SubsService,
    league_id: str,
    team_id: str,
    period_id: Optional[int],
    projections: Dict[Tuple[str, str], dict],
    max_backups: int = AUTO_MAX_BACKUPS,
) -> List[Dict[str, Any]]:
    """
    Generate auto swap rules.
    """
    id_to_row: Dict[str, RosterRow] = {}
    for row in roster.rows:
        player = getattr(row, "player", None)
        if player and getattr(player, "id", None) is not None:
            id_to_row[str(player.id)] = row

    active_ids = [str(pid) for pid in roster_view.active_player_ids()]
    reserve_ids = [str(pid) for pid in roster_view.reserve_player_ids()]

    reserves: List[Dict[str, Any]] = []
    for pid in reserve_ids:
        row = id_to_row.get(str(pid))
        if not row:
            continue
        if not _has_projection_for_row(row, projections):
            logger.info(
                "Auto swap reserve excluded (missing projection): %s",
                getattr(getattr(row, "player", None), "name", pid),
            )
            continue
        info = lineup_info_by_player.get(str(pid))
        proj, proj_gs = _projection_for_row(row, projections)
        reserves.append(
            {
                "pid": pid,
                "name": getattr(row.player, "name", str(pid)),
                "pos": _pos_short(row).upper(),
                "proj_fpts": proj,
                "proj_gs": proj_gs,
                "kos_index": kos_index_map.get(str(pid)),
                "kickoff": getattr(info, "kickoff", None) if info else None,
            }
        )

    rules: List[Dict[str, Any]] = []

    for active_id in active_ids:
        row = id_to_row.get(str(active_id))
        if not row:
            continue
        active_info = lineup_info_by_player.get(str(active_id))
        active_kickoff = getattr(active_info, "kickoff", None) if active_info else None
        active_kos_index = kos_index_map.get(str(active_id))
        if not active_kickoff:
            continue
        backups = 0
        eligible_reserves: List[Dict[str, Any]] = []
        for cand in reserves:
            if not cand.get("kickoff"):
                continue
            if active_kickoff and cand["kickoff"] < active_kickoff:
                continue
            legal = True
            if period_id is not None:
                legal = can_swap_in_period(
                    subs_service=subs_service,
                    roster=roster,
                    league_id=league_id,
                    team_id=team_id,
                    active_id=str(active_id),
                    reserve_id=str(cand["pid"]),
                    period_id=period_id,
                )
            if not legal:
                continue
            eligible_reserves.append(cand)
        ordered_reserves = _order_reserve_candidates_by_kos(eligible_reserves, active_kos_index)
        for cand in ordered_reserves:
            if backups >= max_backups:
                break
            rules.append(
                {
                    "active_id": str(active_id),
                    "reserve_id": str(cand["pid"]),
                    "out_label": _format_player_label(
                        getattr(row.player, "name", active_id),
                        _pos_short(row),
                    ),
                    "in_label": _format_player_label(cand["name"], cand["pos"]),
                    "priority": backups + 1,
                    "period": period_id,
                    "source_strategy": "projection",
                    "trigger": "confirmed_lineup",
                    "score": cand["proj_fpts"],
                    "proj_fpts": cand["proj_fpts"],
                    "proj_gs": cand["proj_gs"],
                    "max_fires": 1,
                    "league_id": league_id,
                    "team_id": team_id,
                    "source": SOURCE_AUTO_LINEUP_SWAPS,
                }
            )
            backups += 1
    return rules


def _generate_auto_claim_rules(
    *,
    roster: Any,
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, Any],
    kos_index_map: Dict[str, int],
    league_id: str,
    team_id: str,
    period_id: Optional[int],
    projections: Dict[Tuple[str, str], dict],
    waivers_service: WaiversService,
    fa_status_map: Dict[str, Any],
    never_drop_ids: set[str],
    max_candidates: int = AUTO_MAX_FA_CANDIDATES,
) -> List[Dict[str, Any]]:
    """
    Generate auto FA claim/drop rules for active players.
    """
    id_to_row: Dict[str, RosterRow] = {}
    for row in roster.rows:
        player = getattr(row, "player", None)
        if player and getattr(player, "id", None) is not None:
            id_to_row[str(player.id)] = row

    try:
        # Only consider true free agents (exclude waiver-wire candidates).
        fa_pool = waivers_service.list_players_by_name(
            limit=FA_POOL_LIMIT,
            status="FREE_AGENT",
        )
    except Exception as exc:
        logger.info("Auto claims: failed to load FA pool: %s", exc)
        return []

    fa_candidates: List[Dict[str, Any]] = []
    now = _now()
    for p in fa_pool:
        sid = str(p.get("id") or "")
        if not sid:
            continue
        snapshot = fa_status_map.get(sid)
        if not snapshot:
            continue
        status_val = getattr(snapshot, "status", None)
        is_starting = status_val == LineupStatus.STARTING or str(status_val).lower() == LineupStatus.STARTING.value
        if not is_starting:
            continue
        kickoff = getattr(snapshot, "kickoff", None)
        # Exclude players we can positively identify as already played.
        # If kickoff is missing, keep the player eligible (we may still be able to use them,
        # but kickoff-based ordering/guardrails will be weaker).
        if kickoff and kickoff <= now:
            continue
        proj_fpts, proj_gs = _projection_for_name_and_team(
            p.get("name"),
            p.get("team"),
            projections,
        )
        fa_candidates.append(
            {
                "id": sid,
                "name": p.get("name") or "",
                "team": p.get("team") or "",
                "position": p.get("position") or "",
                "default_pos_id": p.get("default_pos_id"),
                "kickoff": kickoff,
                "proj_fpts": proj_fpts,
                "proj_gs": proj_gs,
            }
        )

    if not fa_candidates:
        return []

    rules: List[Dict[str, Any]] = []
    for active_id in roster_view.active_player_ids():
        drop_id = str(active_id)
        if drop_id in never_drop_ids:
            continue
        if roster_view.is_locked(drop_id, now=now, lineup_info_by_player=lineup_info_by_player):
            continue
        row = id_to_row.get(drop_id)
        if not row:
            continue
        active_info = lineup_info_by_player.get(drop_id)
        active_kickoff = getattr(active_info, "kickoff", None) if active_info else None
        if not active_kickoff:
            continue
        slot_pos = _pos_short(row).upper()
        slot_pos_id = str(getattr(row, "pos_id", "") or "")
        eligible = []
        for cand in fa_candidates:
            if slot_pos and not _fa_position_matches_slot(cand.get("position"), slot_pos):
                continue
            if cand.get("kickoff") and active_kickoff and cand["kickoff"] < active_kickoff:
                continue
            eligible.append(cand)
        eligible.sort(
            key=lambda c: (
                -(c.get("proj_fpts") or 0.0),
                -(c.get("proj_gs") or 0),
                str(c.get("name") or ""),
            )
        )
        for idx, cand in enumerate(eligible[:max_candidates]):
            rules.append(
                {
                    "action_type": "fa_claim_drop",
                    "active_id": drop_id,
                    "fa_add_scorer_id": cand["id"],
                    "fa_trigger_mode": FA_TRIGGER_MODE_FA_STARTING_ONLY,
                    "fa_add_position_id": slot_pos_id or str(cand.get("default_pos_id") or ""),
                    "fa_claim_to_status_id": "1",
                    "fa_bid_amount": 0.0,
                    "fa_add_display_name": cand["name"],
                    "priority": idx + 1,
                    "period": period_id,
                    "trigger": "confirmed_lineup",
                    "proj_fpts": cand.get("proj_fpts"),
                    "proj_gs": cand.get("proj_gs"),
                    "league_id": league_id,
                    "team_id": team_id,
                    "source": "auto_claims",
                }
            )
    return rules


def _merge_auto_claim_rules(
    rules: List[Dict[str, Any]],
    *,
    league_id: str,
    team_id: str,
    new_auto_rules: List[Dict[str, Any]],
    user_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Merge auto claim rules with existing rules.
    """
    filtered = [
        r
        for r in rules
        if not (
            str(r.get("source")) == "auto_claims"
            and str(r.get("league_id")) == str(league_id)
            and str(r.get("team_id")) == str(team_id)
        )
    ]
    ts = datetime.now(timezone.utc).isoformat()
    for r in new_auto_rules:
        r.setdefault("rule_id", uuid.uuid4().hex)
        r.setdefault("created_at", ts)
        r.setdefault("state", "pending")
        r.setdefault("fired_count", 0)
        r.setdefault("source", "auto_claims")
        r.setdefault("source_type", 2)
        if user_id:
            r["user_id"] = user_id
    return filtered + new_auto_rules


def _merge_auto_rules(
    rules: List[Dict[str, Any]],
    *,
    league_id: str,
    team_id: str,
    new_auto_rules: List[Dict[str, Any]],
    user_id: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Merge auto swap rules with existing rules.
    """
    filtered = [
        r
        for r in rules
        if not (
            normalize_rule_source(str(r.get("source") or "")) == SOURCE_AUTO_LINEUP_SWAPS
            and str(r.get("league_id")) == str(league_id)
            and str(r.get("team_id")) == str(team_id)
            and (
                AUTO_RULE_PRUNE_FIRED_FROM_RULES
                or str(r.get("state") or "").lower() not in {"fired", "executed", "satisfied_noop"}
            )
        )
    ]
    ts = datetime.now(timezone.utc).isoformat()
    for r in new_auto_rules:
        r.setdefault("rule_id", uuid.uuid4().hex)
        r.setdefault("created_at", ts)
        r.setdefault("state", "pending")
        r.setdefault("fired_count", 0)
        r.setdefault("source", SOURCE_AUTO_LINEUP_SWAPS)
        r.setdefault("source_type", 2)
        if user_id:
            r["user_id"] = user_id
    return filtered + new_auto_rules

def _build_session_from_artifacts(artifacts_path: Path) -> Optional[Any]:
    """
    Build a requests session from auth artifacts.
    """
    if not artifacts_path.exists():
        logger.error("Auth artifacts not found: %s", artifacts_path)
        return None
    try:
        import json as _json
        from utils.auth_helpers import load_requests_session_from_cookie_list

        artifacts = _json.loads(artifacts_path.read_text())
        if isinstance(artifacts, list):
            return load_requests_session_from_cookie_list(artifacts)
        if load_requests_session_from_artifacts and isinstance(artifacts, dict):
            return load_requests_session_from_artifacts(artifacts)
        raise ValueError("Unsupported auth artifacts format (expected list or dict).")
    except Exception as exc:
        logger.error("Failed to rebuild session from %s: %s", artifacts_path, exc)
        return None


def _build_session_from_user(user_mgr: UserManager, user_id: str) -> Optional[Any]:
    """
    Build a requests session from user cookies.
    """
    artifacts = user_mgr.load_user_cookies(user_id)
    if not artifacts:
        logger.info("No auth artifacts for user %s", user_id)
        return None
    try:
        from utils.auth_helpers import load_requests_session_from_cookie_list

        if isinstance(artifacts, list):
            return load_requests_session_from_cookie_list(artifacts)
        if load_requests_session_from_artifacts and isinstance(artifacts, dict):
            return load_requests_session_from_artifacts(artifacts)
        raise ValueError("Unsupported auth artifacts format (expected list or dict).")
    except Exception as exc:
        logger.error("Failed to rebuild session for user %s: %s", user_id, exc)
        return None


def _auto_enabled_combos(user_mgr: UserManager, user_id: str, feature: str) -> List[Tuple[str, str]]:
    """
    Get auto enabled combos for a user.
    """
    user = user_mgr.get_user_by_id(user_id) or {}
    auto_rules = user.get("auto_rules") or {}
    combos = []
    for league_id, pref in auto_rules.items():
        if not pref:
            continue
        enabled = user_mgr.is_auto_rules_enabled(user_id, league_id, feature)
        if not enabled:
            continue
        team_id = pref.get("team_id")
        if league_id and team_id:
            combos.append((str(league_id), str(team_id)))
    return combos


def _collect_runs(args) -> List[Tuple[Optional[str], Path, List[Dict[str, Any]], Any, Optional[UserManager]]]:
    """
    Collect runs for a given set of arguments.
    """
    runs: List[Tuple[Optional[str], Path, List[Dict[str, Any]], Any, Optional[UserManager]]] = []

    if args.all_users or args.user_id:
        user_mgr = UserManager()
        users = []
        if args.user_id:
            user = user_mgr.get_user_by_id(args.user_id)
            if not user:
                logger.error("Unknown user_id=%s", args.user_id)
                return []
            users = [user]
        else:
            users = user_mgr.list_all_users()

        for user in users:
            user_id = user.get("user_id")
            if not user_id:
                continue
            rules_path = rules_path_for_user(user_id)
            rules, player_locks = load_rules_for_user_with_locks(user_id)
            pending = [r for r in rules if _eligible(r) or _eligible_fa_rule(r)]
            auto_combos = _auto_enabled_combos(user_mgr, user_id, "lineup_swaps")
            auto_claim_combos = _auto_enabled_combos(user_mgr, user_id, "claims")
            if not pending and not auto_combos and not auto_claim_combos:
                continue
            session = _build_session_from_user(user_mgr, user_id)
            if session is None:
                continue
            runs.append((user_id, rules_path, rules, player_locks, session, user_mgr))
        return runs

    rules_path = Path(args.rules_path)
    rules, player_locks = load_rules_with_locks(rules_path)
    pending = [r for r in rules if _eligible(r) or _eligible_fa_rule(r)]
    session = _build_session_from_artifacts(Path(args.auth_artifacts))
    if session is None:
        return []
    runs.append((None, rules_path, rules, player_locks, session, None))
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Run conditional swaps headlessly.")
    parser.add_argument("--league-id", required=False, help="Fantrax league id (optional; will derive from rules if absent)")
    parser.add_argument("--team-id", required=False, help="Fantrax team id (optional; will derive from rules if absent)")
    parser.add_argument("--period", type=int, default=None, help="Fantrax scoring period id")
    parser.add_argument("--auth-artifacts", default="auth_artifacts.json", help="Path to auth artifacts")
    parser.add_argument("--rules-path", default=str(DEFAULT_RULES_PATH), help="Rules JSON path")
    parser.add_argument("--user-id", help="Run rules for a single user_id (uses data/auth/ cookies + data/conditional_rules)")
    parser.add_argument("--all-users", action="store_true", help="Run rules for all users with stored cookies")
    parser.add_argument("--dry-run", action="store_true", help="Do not execute swaps; log only")
    parser.add_argument("--kos", type=int, default=None, help="Limit rule execution to a single KOS index (e.g., 1).")
    parser.add_argument(
        "--force-trigger",
        action="store_true",
        help="Skip lineup-status trigger checks (treat all rules as eligible).",
    )
    parser.add_argument(
        "--simulate-lineups",
        action="store_true",
        help="Simulate lineup confirmations: treat actives as not starting and reserves as starting.",
    )
    parser.add_argument("--trace", action="store_true", help="Log detailed per-rule trace information.")
    args = parser.parse_args()
    if not STATE_WRITER_ENABLED and not args.dry_run:
        logger.info(
            "Conditional state role is reader; forcing --dry-run for this runner process."
        )
        args.dry_run = True

    runs = _collect_runs(args)
    if not runs:
        logger.info("No eligible rules to process.")
        return

    projections = _load_projections_map()

    for user_id, rules_path, rules, player_locks, session, user_mgr in runs:
        user_timezone = _resolve_user_timezone(user_mgr, user_id)
        """
        Process runs for a given user.
        """
        pending = [r for r in rules if _eligible(r) or _eligible_fa_rule(r)]
        combos = set()
        if args.league_id and args.team_id:
            combos.add((args.league_id, args.team_id))
        else:
            for r in pending:
                lid = r.get("league_id")
                tid = r.get("team_id")
                if lid and tid:
                    combos.add((str(lid), str(tid)))
            if user_id and user_mgr:
                for lid, tid in _auto_enabled_combos(user_mgr, str(user_id), "lineup_swaps"):
                    combos.add((lid, tid))
                for lid, tid in _auto_enabled_combos(user_mgr, str(user_id), "claims"):
                    combos.add((lid, tid))

        if not combos:
            logger.error("No league/team context provided or found in rules (user=%s).", user_id)
            continue

        updated = False
        updated_locks = False
        for league_id, team_id in combos:
            api = FantraxAPI(league_id=league_id, session=session)
            subs_service = SubsService(session=session, league_id=league_id)
            waivers_service = api.waivers
            drops_service = api.drops
            try:
                roster = api.roster_info(team_id)
            except Exception as exc:
                logger.error("Failed to load roster for league=%s team=%s: %s", league_id, team_id, exc)
                continue

            roster_period = None
            try:
                roster_period, _deadline = subs_service._sniff_period_and_deadline_from_roster(roster)
                roster_period = int(roster_period) if roster_period else None
            except Exception:
                roster_period = None

            preferred_period = None
            if user_id and user_mgr and hasattr(user_mgr, "get_preferred_period"):
                try:
                    pref_val = user_mgr.get_preferred_period(str(user_id), str(league_id))
                except Exception:
                    pref_val = None
                if pref_val:
                    try:
                        preferred_period = int(pref_val)
                    except Exception:
                        preferred_period = None

            chosen_period = None
            chosen_label = ""
            if args.period is not None:
                try:
                    chosen_period = int(args.period)
                except Exception:
                    chosen_period = None
            if chosen_period is None:
                chosen_period, chosen_label = _resolve_period_like_ui(
                    api=api,
                    league_id=league_id,
                    team_id=team_id,
                    session=session,
                    preferred_period=preferred_period,
                )
            if chosen_period is None:
                chosen_period = roster_period

            if chosen_period is not None and roster_period != chosen_period:
                try:
                    roster = api.roster_info(team_id, period=int(chosen_period))
                    roster_period, _deadline = subs_service._sniff_period_and_deadline_from_roster(roster)
                    roster_period = int(roster_period) if roster_period else roster_period
                except Exception as exc:
                    logger.info(
                        "Failed to load roster for period %s (league=%s team=%s): %s",
                        chosen_period,
                        league_id,
                        team_id,
                        exc,
                    )

            with _run_lock(
                league_id=league_id,
                team_id=team_id,
                period_id=chosen_period,
            ) as (lock_acquired, _):
                if not lock_acquired:
                    logger.info(
                        "Skipping league=%s team=%s period=%s: runner lock held.",
                        league_id,
                        team_id,
                        chosen_period,
                    )
                    continue
                auto_period_id = chosen_period
                lineup_period = chosen_period
                logger.info(
                    "[period] runner choose period=%s label=%s preferred=%s roster=%s",
                    chosen_period,
                    chosen_label,
                    preferred_period,
                    roster_period,
                )

                mapping_manager = PlayerMappingManager()
                try:
                    round_hint = infer_current_gameweek()
                    lineup_info_by_player = resolve_lineup_info(
                        roster,
                        session=session,
                        league_id=league_id,
                        period=lineup_period,
                        strategy=LineupSourceStrategy.FANTRAX_PRIMARY,
                        mapping_manager=mapping_manager,
                        round_hint=round_hint,
                        global_status_path=global_status_path_for_user(user_id),
                    )
                except Exception as exc:
                    logger.error("Failed to resolve lineup info for league=%s team=%s: %s", league_id, team_id, exc)
                    continue

                kos_index_map, _first_kos, last_kos = _build_kos_index_map(lineup_info_by_player)

                never_drop_ids: set[str] = _update_never_drop_auto(
                    user_mgr=user_mgr,
                    user_id=user_id,
                    league_id=str(league_id),
                    team_id=str(team_id),
                    roster=roster,
                    waivers_service=waivers_service,
                )

                if (
                    user_id
                    and user_mgr
                    and user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
                    and projections
                ):
                    roster_view = RosterView(roster)
                    had_auto_rules = any(
                        normalize_rule_source(str(r.get("source") or "")) == SOURCE_AUTO_LINEUP_SWAPS
                        and str(r.get("league_id")) == str(league_id)
                        and str(r.get("team_id")) == str(team_id)
                        for r in rules
                    )
                    auto_rules = _generate_auto_swap_rules(
                        roster=roster,
                        roster_view=roster_view,
                        lineup_info_by_player=lineup_info_by_player,
                        kos_index_map=kos_index_map,
                        subs_service=subs_service,
                        league_id=league_id,
                        team_id=team_id,
                        period_id=auto_period_id,
                        projections=projections,
                    )
                    rules = _merge_auto_rules(
                        rules,
                        league_id=league_id,
                        team_id=team_id,
                        new_auto_rules=auto_rules,
                        user_id=str(user_id) if user_id else None,
                    )
                    if auto_rules or had_auto_rules:
                        updated = True
                    logger.info(
                        "Generated %s auto lineup swap rules for league=%s team=%s user=%s",
                        len(auto_rules),
                        league_id,
                        team_id,
                        user_id,
                    )

                fa_status_map: Dict[str, Any] = {}
                if (
                    ENABLE_AUTO_CLAIMS
                    and user_id
                    and user_mgr
                    and user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "claims")
                    and projections
                    and str(league_id) == CLAIMS_TEST_LEAGUE_ID
                ):
                    try:
                        fa_status_map = fetch_fa_status_map(session=session, league_id=league_id)
                    except Exception as exc:
                        logger.info("Auto claims: failed to load FA statuses: %s", exc)
                        fa_status_map = {}
                    if fa_status_map:
                        roster_view = RosterView(roster)
                        had_auto_rules = any(
                            str(r.get("source")) == "auto_claims"
                            and str(r.get("league_id")) == str(league_id)
                            and str(r.get("team_id")) == str(team_id)
                            for r in rules
                        )
                        auto_claim_rules = _generate_auto_claim_rules(
                            roster=roster,
                            roster_view=roster_view,
                            lineup_info_by_player=lineup_info_by_player,
                            kos_index_map=kos_index_map,
                            league_id=league_id,
                            team_id=team_id,
                            period_id=auto_period_id,
                            projections=projections,
                            waivers_service=waivers_service,
                            fa_status_map=fa_status_map,
                            never_drop_ids=never_drop_ids,
                        )
                        rules = _merge_auto_claim_rules(
                            rules,
                            league_id=league_id,
                            team_id=team_id,
                            new_auto_rules=auto_claim_rules,
                            user_id=str(user_id) if user_id else None,
                        )
                        if auto_claim_rules or had_auto_rules:
                            updated = True
                        logger.info(
                            "Generated %s auto claim rules for league=%s team=%s user=%s",
                            len(auto_claim_rules),
                            league_id,
                            team_id,
                            user_id,
                        )

                if _apply_inverse_rule_guard(rules, league_id=league_id, team_id=team_id):
                    updated = True

                logger.info(
                    "Deprecated conditional settings ignored at runtime: late_kos_policy, do_not_move (league=%s team=%s).",
                    league_id,
                    team_id,
                )

                fa_action_executed = False
                fa_pending = [
                    r
                    for r in rules
                    if _eligible_fa_rule(r)
                    and str(r.get("league_id")) == str(league_id)
                    and str(r.get("team_id")) == str(team_id)
                ]
                if fa_pending and str(league_id) != CLAIMS_TEST_LEAGUE_ID:
                    logger.info(
                        "Skipping FA rules for league=%s; claims locked to test league.",
                        league_id,
                    )
                    fa_pending = []
                if fa_pending and (not user_id or not user_mgr or not user_mgr.get_claims_ack(str(user_id), str(league_id))):
                    logger.info(
                        "Skipping FA rules for league=%s; user has not acknowledged claims.",
                        league_id,
                    )
                    fa_pending = []
                best_swap_candidate = _best_pending_swap_candidate(
                    rules,
                    league_id=str(league_id),
                    team_id=str(team_id),
                    chosen_period=chosen_period,
                    kos_index_map=kos_index_map,
                    lineup_info_by_player=lineup_info_by_player,
                    user_mgr=user_mgr,
                    user_id=user_id,
                )
                fa_deferred_to_swap = False
                if fa_pending:
                    if not fa_status_map:
                        try:
                            fa_status_map = fetch_fa_status_map(session=session, league_id=league_id)
                        except Exception as exc:
                            logger.info("FA rules: failed to load FA statuses: %s", exc)
                            fa_status_map = {}
                    roster_view = RosterView(roster)
                    now = _now()
                    open_active_slots, open_reserve_slots = _open_roster_slots(roster)

                    grouped_fa: Dict[str, List[Dict[str, Any]]] = {}
                    for r in fa_pending:
                        drop_id = str(r.get("drop_player_id") or r.get("active_id") or "")
                        key = drop_id or str(r.get("rule_id") or "")
                        grouped_fa.setdefault(key, []).append(r)

                    def _group_priority(item: tuple[str, List[Dict[str, Any]]]) -> tuple:
                        _key, ruleset = item
                        if not ruleset:
                            return (999, "", "")
                        return _rule_priority_key(sorted(ruleset, key=_rule_priority_key)[0])

                    for _group_key, group_rules in sorted(grouped_fa.items(), key=_group_priority):
                        group_rules = sorted(group_rules, key=_rule_priority_key)
                        for rule in group_rules:
                            if (
                                user_id
                                and user_mgr
                                and str(rule.get("source")) == "auto_claims"
                                and not user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "claims")
                            ):
                                continue
                            rule_period = rule.get("period")
                            if rule_period is not None and chosen_period is not None:
                                if str(rule_period) != str(chosen_period):
                                    continue
                            action_type = _rule_action_type(rule)
                            fa_mode = _fa_trigger_mode(rule)
                            drop_id = str(rule.get("drop_player_id") or rule.get("active_id") or "")
                            add_id = str(rule.get("fa_add_scorer_id") or rule.get("fa_add_id") or "")
                            override_never = bool(rule.get("override_never_drop"))
                            if drop_id and drop_id in never_drop_ids and not override_never:
                                logger.info(
                                    "FA rule %s skipped: drop in never-drop list (%s)",
                                    rule.get("rule_id"),
                                    drop_id,
                                )
                                continue

                            info_drop = lineup_info_by_player.get(drop_id) if drop_id else None
                            drop_kickoff = getattr(info_drop, "kickoff", None) if info_drop else None

                            fa_kickoff_candidate = _fa_action_kickoff(rule, lineup_info_by_player, fa_status_map)
                            fa_kos_candidate = _kickoff_to_kos_index(
                                fa_kickoff_candidate, kos_index_map, lineup_info_by_player
                            )
                            fa_candidate = ActionCandidate(
                                action_family="claim_drop",
                                source=str(rule.get("source") or "manual"),
                                rule_id=str(rule.get("rule_id") or ""),
                                rule_ref=rule,
                                kos_index=fa_kos_candidate,
                                kickoff=fa_kickoff_candidate,
                                tie_rank=0,
                                priority_key=_rule_priority_key(rule),
                            )
                            if best_swap_candidate is not None:
                                fa_key = _action_precedence_key(fa_candidate)
                                swap_key = _action_precedence_key(best_swap_candidate)
                                if fa_key > swap_key:
                                    logger.info(
                                        "FA rule %s deferred to swap rule %s by precedence "
                                        "(fa_kos=%s fa_kickoff=%s swap_kos=%s swap_kickoff=%s).",
                                        rule.get("rule_id"),
                                        best_swap_candidate.rule_id or "(no-id)",
                                        fa_candidate.kos_index,
                                        fa_candidate.kickoff,
                                        best_swap_candidate.kos_index,
                                        best_swap_candidate.kickoff,
                                    )
                                    fa_deferred_to_swap = True
                                    break

                            if action_type == "drop_only":
                                if not drop_id:
                                    continue
                                if not roster_view.get_row(drop_id):
                                    continue
                                if roster_view.is_locked(drop_id, now=now, lineup_info_by_player=lineup_info_by_player):
                                    continue
                                if not _drop_would_keep_roster_legal(roster_view, drop_id):
                                    continue
                                if not args.force_trigger:
                                    drop_status = _confirmed_status_kind(info_drop)
                                    if drop_status != "not_starting":
                                        continue
                                    if drop_kickoff and now >= drop_kickoff:
                                        continue
                                if args.dry_run:
                                    logger.info("FA rule %s DRY RUN: would drop %s", rule.get("rule_id"), drop_id)
                                    fa_action_executed = True
                                    break
                                try:
                                    drops_service.drop_player(
                                        team_id=team_id,
                                        scorer_id=drop_id,
                                        period=int(rule_period) if rule_period is not None else None,
                                    )
                                    rule["state"] = "fired"
                                    rule["fired_at"] = _now().isoformat()
                                    rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                    rule["result"] = "drop_executed"
                                    _record_execution_event(
                                        user_id=user_id,
                                        league_id=league_id,
                                        team_id=team_id,
                                        period=rule_period,
                                        rule=rule,
                                        result="drop_executed",
                                        reason="drop_only_triggered",
                                    )
                                    updated = True
                                    fa_action_executed = True
                                    logger.info(
                                        "FA rule %s executed: dropped %s",
                                        rule.get("rule_id"),
                                        drop_id,
                                    )
                                except Exception as exc:
                                    logger.info("FA rule %s drop failed: %s", rule.get("rule_id"), exc)
                                break

                            if action_type == "fa_add_only":
                                if not add_id:
                                    continue
                                if not open_active_slots and not open_reserve_slots:
                                    continue
                            else:
                                if not drop_id or not add_id:
                                    continue
                                if not roster_view.get_row(drop_id):
                                    continue
                                if roster_view.is_locked(drop_id, now=now, lineup_info_by_player=lineup_info_by_player):
                                    continue
                                if not _drop_would_keep_roster_legal(roster_view, drop_id):
                                    continue
                                if not args.force_trigger:
                                    if drop_kickoff and now >= drop_kickoff:
                                        continue

                            fa_snapshot = fa_status_map.get(add_id) if fa_status_map else None
                            fa_kickoff = getattr(fa_snapshot, "kickoff", None) if fa_snapshot else None

                            if not args.force_trigger:
                                if action_type == "fa_claim_drop" and fa_mode == FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE:
                                    drop_status = _confirmed_status_kind(info_drop)
                                    if drop_status != "not_starting":
                                        continue
                                    if fa_kickoff and now >= fa_kickoff:
                                        rule["state"] = "fired"
                                        rule["fired_at"] = _now().isoformat()
                                        rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                        rule["result"] = "claim_failed_kickoff_passed"
                                        _record_execution_event(
                                            user_id=user_id,
                                            league_id=league_id,
                                            team_id=team_id,
                                            period=rule_period,
                                            rule=rule,
                                            result="claim_failed_kickoff_passed",
                                            reason="fa_kickoff_passed_before_claim",
                                        )
                                        updated = True
                                        fa_action_executed = True
                                        logger.info(
                                            "FA rule %s marked fired: FA kickoff already passed for add=%s",
                                            rule.get("rule_id"),
                                            add_id,
                                        )
                                        break
                                else:
                                    if not fa_snapshot:
                                        continue
                                    status_val = getattr(fa_snapshot, "status", None)
                                    is_starting = (
                                        status_val == LineupStatus.STARTING
                                        or str(status_val).lower() == LineupStatus.STARTING.value
                                    )
                                    if not is_starting:
                                        continue
                                    if fa_kickoff and now >= fa_kickoff:
                                        continue
                                if action_type == "fa_claim_drop" and fa_mode == FA_TRIGGER_MODE_DROP_AND_FA_STARTING:
                                    drop_status = _confirmed_status_kind(info_drop)
                                    if drop_status != "not_starting":
                                        continue
                                    # In strict mode, the FA kickoff must not be earlier than the drop-player kickoff.
                                    # Otherwise the FA lock can pass before we know the drop player's confirmed status.
                                    if fa_kickoff and drop_kickoff and fa_kickoff < drop_kickoff:
                                        continue

                            claim_to_status = str(rule.get("fa_claim_to_status_id") or "2")
                            claim_pos_id = str(rule.get("fa_add_position_id") or "").strip() or None
                            post_swap_out = str(rule.get("post_claim_swap_out_id") or "").strip() or None

                            if claim_to_status == "1" and not claim_pos_id:
                                logger.info(
                                    "FA rule %s skipped: missing active position id",
                                    rule.get("rule_id"),
                                )
                                continue
                            if action_type == "fa_add_only" and claim_to_status == "1" and not open_active_slots:
                                continue
                            if claim_to_status == "2" and not post_swap_out:
                                if action_type == "fa_claim_drop":
                                    # Reserve-drop claim/drop rules intentionally keep the FA on reserve.
                                    logger.info(
                                        "FA rule %s claim-to-reserve without post-claim swap target; proceeding (drop=%s add=%s).",
                                        rule.get("rule_id"),
                                        drop_id or "none",
                                        add_id or "none",
                                    )
                                else:
                                    logger.info(
                                        "FA rule %s skipped: missing post-claim swap target",
                                        rule.get("rule_id"),
                                    )
                                    continue

                            if post_swap_out:
                                if not roster_view.get_row(post_swap_out):
                                    continue
                                if roster_view.is_locked(post_swap_out, now=now, lineup_info_by_player=lineup_info_by_player):
                                    continue
                                period_id = rule_period if rule_period is not None else chosen_period
                                try:
                                    period_int = int(period_id) if period_id is not None else None
                                except Exception:
                                    period_int = None
                                if period_int is not None:
                                    legal = can_swap_in_period(
                                        subs_service=subs_service,
                                        roster=roster,
                                        league_id=league_id,
                                        team_id=team_id,
                                        active_id=post_swap_out,
                                        reserve_id=add_id,
                                        period_id=period_int,
                                    )
                                    if not legal:
                                        continue

                            if args.dry_run:
                                logger.info(
                                    "FA rule %s DRY RUN: would claim %s drop=%s",
                                    rule.get("rule_id"),
                                    add_id,
                                    drop_id or "none",
                                )
                                fa_action_executed = True
                                break

                            try:
                                resp = waivers_service.submit_claim(
                                    team_id=team_id,
                                    claim_scorer_id=add_id,
                                    bid_amount=float(rule.get("fa_bid_amount") or 0.0),
                                    drop_scorer_id=drop_id or None,
                                    to_position_id=claim_pos_id,
                                    to_status_id=claim_to_status,
                                )
                                error_msg = None
                                if isinstance(resp, dict):
                                    error_msg = resp.get("error") or resp.get("errorMsg") or resp.get("pageError")
                                if error_msg:
                                    logger.info(
                                        "FA rule %s claim rejected: %s",
                                        rule.get("rule_id"),
                                        error_msg,
                                    )
                                    if action_type == "fa_claim_drop" and fa_mode == FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE:
                                        rule["state"] = "fired"
                                        rule["fired_at"] = _now().isoformat()
                                        rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                        rule["result"] = "claim_rejected"
                                        _record_execution_event(
                                            user_id=user_id,
                                            league_id=league_id,
                                            team_id=team_id,
                                            period=rule_period,
                                            rule=rule,
                                            result="claim_rejected",
                                            reason=str(error_msg),
                                        )
                                        updated = True
                                        fa_action_executed = True
                                        break
                                    continue
                                rule["state"] = "fired"
                                rule["fired_at"] = _now().isoformat()
                                rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                rule["result"] = "claim_submitted"
                                _record_execution_event(
                                    user_id=user_id,
                                    league_id=league_id,
                                    team_id=team_id,
                                    period=rule_period,
                                    rule=rule,
                                    result="claim_submitted",
                                    reason="fa_claim_submitted",
                                )
                                updated = True
                                fa_action_executed = True
                                logger.info(
                                    "FA rule %s submitted claim add=%s drop=%s",
                                    rule.get("rule_id"),
                                    add_id,
                                    drop_id or "none",
                                )
                                if post_swap_out:
                                    try:
                                        refreshed = api.roster_info(team_id)
                                        if any(
                                            getattr(r, "player", None)
                                            and str(r.player.id) == add_id
                                            for r in refreshed.rows
                                        ):
                                            subs_service.swap_players(
                                                team_id=team_id,
                                                out_player_id=post_swap_out,
                                                in_player_id=add_id,
                                                period=int(rule_period) if rule_period is not None else None,
                                            )
                                            logger.info(
                                                "FA rule %s moved claim to active via swap (out=%s in=%s)",
                                                rule.get("rule_id"),
                                                post_swap_out,
                                                add_id,
                                            )
                                        roster = refreshed
                                    except Exception as exc:
                                        logger.info("FA rule %s post-claim swap failed: %s", rule.get("rule_id"), exc)
                            except Exception as exc:
                                logger.info("FA rule %s claim failed: %s", rule.get("rule_id"), exc)
                                if action_type == "fa_claim_drop" and fa_mode == FA_TRIGGER_MODE_DROP_THEN_CLAIM_IMMEDIATE:
                                    rule["state"] = "fired"
                                    rule["fired_at"] = _now().isoformat()
                                    rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                    rule["result"] = "claim_failed_exception"
                                    _record_execution_event(
                                        user_id=user_id,
                                        league_id=league_id,
                                        team_id=team_id,
                                        period=rule_period,
                                        rule=rule,
                                        result="claim_failed_exception",
                                        reason=str(exc),
                                    )
                                    updated = True
                                    fa_action_executed = True
                            break

                        if fa_action_executed:
                            break
                        if fa_deferred_to_swap:
                            break

                if fa_action_executed:
                    if updated or updated_locks:
                        save_rules(
                            rules,
                            path=rules_path,
                            player_locks=player_locks,
                            actor_env=str(os.getenv("CONDITIONAL_WRITER_ENV", "unknown")),
                        )
                    continue
                if fa_deferred_to_swap:
                    logger.info(
                        "FA execution skipped for league=%s team=%s in this pass; lineup swaps win precedence.",
                        league_id,
                        team_id,
                    )

                iteration = 0
                max_iterations = max(
                    1,
                    len(
                        [
                            r
                            for r in rules
                            if _eligible(r)
                            and str(r.get("league_id")) == str(league_id)
                            and str(r.get("team_id")) == str(team_id)
                        ]
                    ),
                )

                swapped_in_this_run: set[str] = set()
                while True:
                    iteration += 1
                    if iteration > max_iterations:
                        logger.warning(
                            "Stopping rule evaluation after %s iterations (league=%s team=%s).",
                            max_iterations,
                            league_id,
                            team_id,
                        )
                        break

                    roster_view = RosterView(roster)
                    now = _now()
                    id_to_row: Dict[str, RosterRow] = {}
                    for row in roster.rows:
                        player = getattr(row, "player", None)
                        if player and getattr(player, "id", None) is not None:
                            id_to_row[str(player.id)] = row

                    for r in rules:
                        if not _eligible(r):
                            continue
                        if str(r.get("league_id")) != str(league_id):
                            continue
                        if str(r.get("team_id")) != str(team_id):
                            continue
                        active_id = str(r.get("active_id") or "")
                        reserve_id = str(r.get("reserve_id") or "")
                        if not active_id or not reserve_id:
                            continue
                        if not roster_view.is_active(reserve_id):
                            continue
                        if roster_view.is_active(active_id):
                            continue
                        rule_period = r.get("period")
                        if rule_period is not None and chosen_period is not None:
                            if str(rule_period) != str(chosen_period):
                                continue
                        r["state"] = "fired"
                        r["fired_at"] = _now().isoformat()
                        r["fired_count"] = int(r.get("fired_count") or 0) + 1
                        r["result"] = "satisfied_noop"
                        _record_execution_event(
                            user_id=user_id,
                            league_id=league_id,
                            team_id=team_id,
                            period=rule_period,
                            rule=r,
                            result="satisfied_noop",
                            reason="already_swapped_before_runner",
                        )
                        updated = True
                        logger.info(
                            "Rule %s satisfied (noop): %s active=%s reserve=%s",
                            r.get("rule_id"),
                            r.get("trigger") or "confirmed_lineup",
                            active_id,
                            reserve_id,
                        )

                    active_candidates = []
                    for aid in roster_view.active_player_ids():
                        aid_str = str(aid)
                        info = lineup_info_by_player.get(aid_str)
                        row = id_to_row.get(aid_str)
                        proj_fpts, _proj_gs = _projection_for_row(row, projections) if row else (0.0, 0)
                        kos_index = kos_index_map.get(aid_str)
                        active_candidates.append(
                            {
                                "active_id": aid_str,
                                "proj_fpts": proj_fpts,
                                "kos_index": kos_index,
                                "kickoff": getattr(info, "kickoff", None) if info else None,
                            }
                        )

                    active_candidates.sort(
                        key=lambda a: (
                            a.get("kos_index") or 999,
                            -(a.get("proj_fpts") or 0.0),
                        )
                    )

                    used_reserves: set[str] = set()

                    team_pending = [
                        r
                        for r in rules
                        if _eligible(r)
                        and str(r.get("league_id")) == str(league_id)
                        and str(r.get("team_id")) == str(team_id)
                    ]
                    if not team_pending:
                        break

                    rules_by_active: Dict[str, List[Dict[str, Any]]] = {}
                    for r in team_pending:
                        aid = str(r.get("active_id"))
                        rules_by_active.setdefault(aid, []).append(r)

                    swap_executed = False
                    for active in active_candidates:
                        active_id = active["active_id"]
                        active_rules_all = rules_by_active.get(active_id)
                        if not active_rules_all:
                            continue
                        active_rules_sorted = sorted(active_rules_all, key=lambda r: r.get("priority") or 999)
                        reserve_first_rules = [
                            r
                            for r in active_rules_sorted
                            if str(r.get("condition") or "") == "reserve_starting"
                        ]
                        active_first_rules = [
                            r
                            for r in active_rules_sorted
                            if str(r.get("condition") or "") != "reserve_starting"
                        ]
                        base_rule = active_rules_sorted[0]
                        head_rule = active_first_rules[0] if active_first_rules else base_rule
                        if (
                            user_id
                            and user_mgr
                            and normalize_rule_source(str(base_rule.get("source") or "")) == SOURCE_AUTO_LINEUP_SWAPS
                            and not user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
                        ):
                            logger.info(
                                "Rule %s skipped: auto lineup swaps disabled for league=%s user=%s",
                                base_rule.get("rule_id"),
                                league_id,
                                user_id,
                            )
                            continue
                        is_auto_rule = normalize_rule_source(str(base_rule.get("source") or "")) == SOURCE_AUTO_LINEUP_SWAPS
                        if is_auto_rule:
                            period_id = auto_period_id
                        else:
                            period_id = base_rule.get("period")
                            if period_id is None:
                                period_id = args.period or roster_period
                        try:
                            period_id = int(period_id) if period_id is not None else None
                        except Exception:
                            period_id = None
                        if period_id is None:
                            logger.info(
                                "Rule %s proceeding without explicit period; deferring to Fantrax.",
                                base_rule.get("rule_id"),
                            )
                        trigger = head_rule.get("trigger") or "confirmed_lineup"

                        active_info = lineup_info_by_player.get(active_id)
                        status_kind_fn = _status_kind
                        if not args.force_trigger and trigger == "confirmed_lineup":
                            status_kind_fn = _confirmed_status_kind

                        def _sim_status_kind(player_id: str) -> str:
                            if roster_view.is_reserve(player_id):
                                return "starting"
                            if roster_view.is_active(player_id):
                                return "not_starting"
                            return "unconfirmed"

                        if args.simulate_lineups:
                            active_status = _sim_status_kind(active_id)
                            active_confirmed_status = active_status
                        else:
                            active_status = status_kind_fn(active_info)
                            active_confirmed_status = _confirmed_status_kind(active_info)
                        active_kos_index = kos_index_map.get(active_id)
                        active_kickoff = getattr(active_info, "kickoff", None) if active_info else None
                        active_proj_fpts = next(
                            (a.get("proj_fpts") for a in active_candidates if a.get("active_id") == active_id), 0.0
                        )

                        if args.force_trigger:
                            active_status = "not_starting"

                        lineup_active_before = sorted(str(pid) for pid in roster_view.active_player_ids())
                        lineup_reserve_before = sorted(str(pid) for pid in roster_view.reserve_player_ids())
                        trace_meta = {
                            "league_id": league_id,
                            "team_id": team_id,
                            "period_id": period_id,
                            "rule_id": head_rule.get("rule_id"),
                            "active_id": active_id,
                            "trigger": trigger,
                            "active_status": active_status,
                            "active_confirmed_status": active_confirmed_status,
                            "kos_index": active_kos_index,
                            "active_proj_fpts": active_proj_fpts,
                            "lineup_active_before": lineup_active_before,
                            "lineup_reserve_before": lineup_reserve_before,
                            "now_local": _format_datetime_for_user(now, user_timezone),
                            "user_timezone": user_timezone,
                        }

                        if args.kos is not None and active_kos_index != args.kos:
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=False,
                                skip_reason="kos_filter",
                            )
                            logger.info(
                                "Rule %s skipped: KOS filter (wanted=%s got=%s).",
                                head_rule.get("rule_id"),
                                args.kos,
                                active_kos_index,
                            )
                            continue

                        lock_bucket = _lock_bucket(
                            player_locks,
                            league_id=league_id,
                            team_id=team_id,
                            period_id=period_id,
                        )

                        if reserve_first_rules:
                            reserve_executed = False
                            for r in reserve_first_rules:
                                reserve_id = str(r.get("reserve_id"))
                                if reserve_id in used_reserves:
                                    continue
                                if roster_view.is_locked(
                                    active_id,
                                    now=now,
                                    lineup_info_by_player=lineup_info_by_player,
                                ):
                                    logger.info(
                                        "Rule %s skipped: active locked (%s).",
                                        r.get("rule_id"),
                                        _player_label(roster_view, active_id),
                                    )
                                    continue
                                if active_kickoff and now >= active_kickoff:
                                    logger.info(
                                        "Rule %s skipped: active kickoff passed (%s).",
                                        r.get("rule_id"),
                                        _player_label(roster_view, active_id),
                                    )
                                    continue
                                reserve_info = lineup_info_by_player.get(reserve_id)
                                reserve_trigger = r.get("trigger") or "confirmed_lineup"
                                reserve_status_fn = _status_kind
                                if not args.force_trigger and reserve_trigger == "confirmed_lineup":
                                    reserve_status_fn = _confirmed_status_kind
                                if args.simulate_lineups:
                                    reserve_status = _sim_status_kind(reserve_id)
                                else:
                                    reserve_status = reserve_status_fn(reserve_info)
                                if reserve_status != "starting":
                                    continue
                                reserve_kickoff = getattr(reserve_info, "kickoff", None) if reserve_info else None
                                if reserve_kickoff and now >= reserve_kickoff:
                                    continue
                                if (
                                    active_kickoff
                                    and reserve_kickoff
                                    and active_kickoff < reserve_kickoff - timedelta(hours=1.25)
                                ):
                                    continue
                                if roster_view.is_locked(
                                    reserve_id,
                                    now=now,
                                    lineup_info_by_player=lineup_info_by_player,
                                ):
                                    continue
                                legal = True
                                if period_id is not None:
                                    legal = can_swap_in_period(
                                        subs_service=subs_service,
                                        roster=roster,
                                        league_id=league_id,
                                        team_id=team_id,
                                        active_id=active_id,
                                        reserve_id=reserve_id,
                                        period_id=period_id,
                                    )
                                if not legal:
                                    continue
                                if args.dry_run:
                                    logger.info(
                                        "Rule %s DRY RUN ok: would swap %s -> %s (period %s)",
                                        r.get("rule_id"),
                                        _player_label(roster_view, active_id),
                                        _player_label(roster_view, reserve_id),
                                        period_id,
                                    )
                                    reserve_executed = True
                                    used_reserves.add(reserve_id)
                                    break
                                try:
                                    result = subs_service.swap_players(
                                        team_id=team_id,
                                        out_player_id=active_id,
                                        in_player_id=reserve_id,
                                        period=int(period_id) if period_id is not None else None,
                                    )
                                    if result and result.get("success"):
                                        logger.info(
                                            "Rule %s fired: swapped %s -> %s (period %s)",
                                            r.get("rule_id"),
                                            _player_label(roster_view, active_id),
                                            _player_label(roster_view, reserve_id),
                                            period_id,
                                        )
                                        reserve_executed = True
                                        used_reserves.add(reserve_id)
                                        break
                                except Exception:
                                    logger.exception("Rule %s failed during reserve-starting swap", r.get("rule_id"))
                            if reserve_executed:
                                swap_executed = True
                                continue

                        active_rules = active_first_rules
                        if not active_rules:
                            continue
                        if _is_player_locked(lock_bucket, active_id) and active_confirmed_status != "not_starting":
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=False,
                                skip_reason="active_locked",
                            )
                            logger.info(
                                "Rule %s skipped: active locked (%s).",
                                head_rule.get("rule_id"),
                                _player_label(roster_view, active_id),
                            )
                            continue

                        if active_id in swapped_in_this_run and active_status != "not_starting":
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=False,
                                skip_reason="swapped_this_run",
                            )
                            logger.info(
                                "Rule %s skipped: active swapped in this run and is %s (%s).",
                                head_rule.get("rule_id"),
                                active_status,
                                _player_label(roster_view, active_id),
                            )
                            continue

                        if not args.force_trigger:
                            if trigger == "confirmed_lineup":
                                if active_status == "unconfirmed":
                                    _log_trace(
                                        args.trace,
                                        logger=logger,
                                        meta=trace_meta,
                                        ready=False,
                                        skip_reason="active_unconfirmed",
                                    )
                                    logger.info(
                                        "Rule %s skipped: active lineup not confirmed (%s).",
                                        head_rule.get("rule_id"),
                                        _player_label(roster_view, active_id),
                                    )
                                    continue
                                if active_status == "starting":
                                    _log_trace(
                                        args.trace,
                                        logger=logger,
                                        meta=trace_meta,
                                        ready=False,
                                        skip_reason="active_confirmed_starter",
                                    )
                                    logger.info(
                                        "Rule %s skipped: active confirmed starter (%s).",
                                        head_rule.get("rule_id"),
                                        _player_label(roster_view, active_id),
                                    )
                                    continue
                            elif active_status == "unconfirmed" and not _in_confirmation_window(active_kickoff, now):
                                _log_trace(
                                    args.trace,
                                    logger=logger,
                                    meta=trace_meta,
                                    ready=False,
                                    skip_reason="active_unconfirmed_window",
                                )
                                logger.info(
                                    "Rule %s skipped: active unconfirmed outside window.",
                                    head_rule.get("rule_id"),
                                )
                                continue
                        # TODO: additional triggers (e.g., kickoff_passed, injury_flag) can be added here.

                        if roster_view.is_locked(active_id, now=now, lineup_info_by_player=lineup_info_by_player):
                            for r in active_rules_sorted:
                                r["state"] = "disabled"
                                r["disabled_at"] = _now().isoformat()
                                r["disabled_reason"] = "active_locked"
                            updated = True
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=False,
                                skip_reason="fantrax_active_locked",
                            )
                            logger.info(
                                "Rule %s disabled: active locked (%s).",
                                head_rule.get("rule_id"),
                                _player_label(roster_view, active_id),
                            )
                            continue
                        reserve_candidates = []
                        for r in active_rules:
                            reserve_id = str(r.get("reserve_id"))
                            if reserve_id in used_reserves:
                                continue
                            reserve_info = lineup_info_by_player.get(reserve_id)
                            if args.simulate_lineups:
                                reserve_confirmed_status = _sim_status_kind(reserve_id)
                            else:
                                reserve_confirmed_status = _confirmed_status_kind(reserve_info)
                            locked = _is_player_locked(lock_bucket, reserve_id)
                            lock_bypass = False
                            if locked:
                                if active_confirmed_status == "not_starting" and reserve_confirmed_status == "starting":
                                    lock_bypass = True
                                    logger.info(
                                        "Rule %s allowing locked reserve due to safety override (%s).",
                                        r.get("rule_id"),
                                        _player_label(roster_view, reserve_id),
                                    )
                                else:
                                    logger.info(
                                        "Rule %s skipped: reserve locked (%s).",
                                        r.get("rule_id"),
                                        _player_label(roster_view, reserve_id),
                                    )
                                    continue
                            proj_val = r.get("proj_fpts")
                            proj_gs_val = r.get("proj_gs")
                            row = id_to_row.get(reserve_id)
                            if not _has_projection_for_row(row, projections):
                                logger.info(
                                    "Rule %s skipped: reserve missing projection (%s).",
                                    r.get("rule_id"),
                                    _player_label(roster_view, reserve_id),
                                )
                                continue
                            if proj_val is None:
                                proj_val, fallback_proj_gs = _projection_for_row(row, projections) if row else (0.0, 0)
                                if proj_gs_val is None:
                                    proj_gs_val = fallback_proj_gs
                            elif proj_gs_val is None:
                                _p, fallback_proj_gs = _projection_for_row(row, projections) if row else (0.0, 0)
                                proj_gs_val = fallback_proj_gs
                            try:
                                proj_val = float(proj_val or 0.0)
                            except Exception:
                                proj_val = 0.0
                            try:
                                proj_gs_val = int(proj_gs_val or 0)
                            except Exception:
                                proj_gs_val = 0
                            candidate_status = (
                                _sim_status_kind(reserve_id)
                                if args.simulate_lineups
                                else status_kind_fn(reserve_info)
                            )
                            reserve_candidates.append(
                                {
                                    "rule": r,
                                    "reserve_id": reserve_id,
                                    "status": candidate_status,
                                    "candidate_classification": (
                                        "confirmed_starter" if candidate_status == "starting" else "unconfirmed_or_not_starting"
                                    ),
                                    "proj_gs": proj_gs_val,
                                    "proj_fpts": proj_val,
                                    "kos_index": kos_index_map.get(reserve_id),
                                    "kickoff": getattr(reserve_info, "kickoff", None) if reserve_info else None,
                                    "locked": locked,
                                    "lock_bypass": lock_bypass,
                                }
                            )

                        preferred: List[Dict[str, Any]] = []

                        if active_status == "starting":
                            same_kos = [
                                c
                                for c in reserve_candidates
                                if c["status"] == "starting"
                                and active_kos_index is not None
                                and c.get("kos_index") == active_kos_index
                                and c.get("proj_fpts") > (active_proj_fpts or 0.0)
                            ]
                            if same_kos:
                                same_kos.sort(key=lambda c: c["proj_fpts"], reverse=True)
                                preferred = same_kos

                        elif active_status == "not_starting":
                            confirmed = [c for c in reserve_candidates if c["status"] == "starting"]
                            if confirmed:
                                confirmed.sort(key=lambda c: c["proj_fpts"], reverse=True)
                                preferred = confirmed
                            else:
                                if not args.force_trigger and trigger == "confirmed_lineup":
                                    if is_auto_rule:
                                        preferred = _order_auto_unconfirmed_fallback_candidates(
                                            reserve_candidates,
                                            active_kos_index=active_kos_index,
                                        )
                                        logger.info(
                                            "Rule %s fallback_unconfirmed_reserve: active confirmed non-starter (%s); "
                                            "confirmed=0 fallback_candidates=%s",
                                            active_rules[0].get("rule_id"),
                                            _player_label(roster_view, active_id),
                                            len(preferred),
                                        )
                                        if args.trace:
                                            trace_meta["fallback_reason"] = "fallback_unconfirmed_reserve"
                                            trace_meta["fallback_candidate_count"] = len(preferred)
                                    else:
                                        logger.info(
                                            "Rule %s skipped: no confirmed reserve starters for %s.",
                                            active_rules[0].get("rule_id"),
                                            _player_label(roster_view, active_id),
                                        )
                                        _log_trace(
                                            args.trace,
                                            logger=logger,
                                            meta=trace_meta,
                                            ready=False,
                                            skip_reason="no_confirmed_reserve_starters",
                                        )
                                        continue
                                else:
                                    preferred = reserve_candidates

                        trace_candidates = _summarize_candidates(preferred, user_timezone)
                        trace_logged = False
                        executed = False
                        for candidate in preferred:
                            rule = candidate["rule"]
                            reserve_id = candidate["reserve_id"]
                            if roster_view.is_locked(reserve_id, now=now, lineup_info_by_player=lineup_info_by_player):
                                logger.info(
                                    "Rule %s skipped: reserve locked (%s).",
                                    rule.get("rule_id"),
                                    _player_label(roster_view, reserve_id),
                                )
                                continue

                            legal = True
                            if period_id is not None:
                                legal = can_swap_in_period(
                                    subs_service=subs_service,
                                    roster=roster,
                                    league_id=league_id,
                                    team_id=team_id,
                                    active_id=active_id,
                                    reserve_id=reserve_id,
                                    period_id=period_id,
                                )
                            if not legal:
                                logger.info(
                                    "Rule %s skipped: illegal swap for period %s (%s -> %s).",
                                    rule.get("rule_id"),
                                    period_id,
                                    _player_label(roster_view, active_id),
                                    _player_label(roster_view, reserve_id),
                                )
                                continue

                            if args.dry_run:
                                logger.info(
                                    "Rule %s DRY RUN ok: would swap %s -> %s (period %s)",
                                    rule.get("rule_id"),
                                    _player_label(roster_view, active_id),
                                    _player_label(roster_view, reserve_id),
                                    period_id,
                                )
                                executed = True
                                used_reserves.add(reserve_id)
                                break

                            try:
                                result = subs_service.swap_players(
                                    team_id=team_id,
                                    out_player_id=active_id,
                                    in_player_id=reserve_id,
                                    period=int(period_id) if period_id is not None else None,
                                )
                                if result.get("success"):
                                    rule["state"] = "fired"
                                    rule["fired_at"] = _now().isoformat()
                                    rule["fired_count"] = int(rule.get("fired_count") or 0) + 1
                                    rule["result"] = "executed"
                                    _record_execution_event(
                                        user_id=user_id,
                                        league_id=league_id,
                                        team_id=team_id,
                                        period=period_id,
                                        rule=rule,
                                        result="executed",
                                        reason="lineup_swap_executed",
                                    )
                                    updated = True
                                    executed = True
                                    swap_executed = True
                                    _lock_player(
                                        lock_bucket,
                                        player_id=active_id,
                                        reason="rule_swap",
                                        rule_id=rule.get("rule_id"),
                                    )
                                    _lock_player(
                                        lock_bucket,
                                        player_id=reserve_id,
                                        reason="rule_swap",
                                        rule_id=rule.get("rule_id"),
                                    )
                                    updated_locks = True
                                    used_reserves.add(reserve_id)
                                    swapped_in_this_run.add(reserve_id)
                                    logger.info(
                                        "Rule %s executed: %s -> %s (period %s)",
                                        rule.get("rule_id"),
                                        active_id,
                                        reserve_id,
                                        period_id,
                                    )
                                    _log_trace(
                                        args.trace,
                                        logger=logger,
                                        meta=trace_meta,
                                        ready=True,
                                        candidates=trace_candidates,
                                        chosen={
                                            "reserve_id": reserve_id,
                                            "candidate_classification": candidate.get("candidate_classification"),
                                            "proj_gs": candidate.get("proj_gs"),
                                            "proj_fpts": candidate.get("proj_fpts"),
                                            "lock_bypass": candidate.get("lock_bypass"),
                                        },
                                        result="executed",
                                        lineup_diff={"out": active_id, "in": reserve_id},
                                        lock_bypass=candidate.get("lock_bypass"),
                                    )
                                    trace_logged = True
                                    break
                                else:
                                    logger.info("Rule %s failed execute: %s", rule.get("rule_id"), result)
                            except Exception as exc:
                                logger.error("Rule %s execution error: %s", rule.get("rule_id"), exc)

                        if swap_executed:
                            break
                        if not trace_logged and args.trace:
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=True,
                                candidates=trace_candidates,
                                result="no_swap",
                            )

                        if not swap_executed:
                            break
                    try:
                        roster = api.roster_info(team_id)
                    except Exception as exc:
                        logger.error("Failed to refresh roster after swap for league=%s team=%s: %s", league_id, team_id, exc)
                        break
                    try:
                        roster_period, _deadline = subs_service._sniff_period_and_deadline_from_roster(roster)
                        roster_period = int(roster_period) if roster_period else roster_period
                    except Exception:
                        roster_period = roster_period
                    try:
                        round_hint = infer_current_gameweek()
                        lineup_info_by_player = resolve_lineup_info(
                            roster,
                            session=session,
                            league_id=league_id,
                            period=lineup_period,
                            strategy=None,
                            mapping_manager=mapping_manager,
                            round_hint=round_hint,
                            global_status_path=global_status_path_for_user(user_id),
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to refresh lineup info after swap for league=%s team=%s: %s",
                            league_id,
                            team_id,
                            exc,
                        )
                        break
                    kos_index_map, _first_kos, last_kos = _build_kos_index_map(lineup_info_by_player)

        if updated or updated_locks:
            save_rules(
                rules,
                path=rules_path,
                player_locks=player_locks,
                actor_env=str(os.getenv("CONDITIONAL_WRITER_ENV", "unknown")),
            )


if __name__ == "__main__":
    main()
