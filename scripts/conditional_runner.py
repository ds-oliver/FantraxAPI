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
from fantraxapi.lineups.lineup_resolver import resolve_lineup_info
from fantraxapi.lineups.fantrax_lineup_bridge import global_status_path_for_user
from fantraxapi.lineups.sofascore_bridge import infer_current_gameweek
from fantraxapi.subs import SubsService
from fantraxapi.player_mapping import PlayerMappingManager
from fantraxapi.objs import RosterRow

from utils.conditional_rule_store import (
    DEFAULT_RULES_PATH,
    load_rules,
    load_rules_with_locks,
    load_rules_for_user,
    load_rules_for_user_with_locks,
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
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [runner] %(message)s",
)
logger = logging.getLogger(__name__)
PROJECTIONS_PATH = Path("data/derived/projections.parquet")
AUTO_MAX_BACKUPS = 3
CONFIRM_WINDOW_MINUTES = 60
LOCK_DIR = Path("data/locks/conditional_runner")
LOCK_DIR.mkdir(parents=True, exist_ok=True)
LOCK_TTL_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _format_datetime_for_user(dt: Optional[datetime], tz_name: str) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(tz_name or "UTC")
    except Exception:
        zone = timezone.utc
    try:
        return dt.astimezone(zone).isoformat()
    except Exception:
        return dt.isoformat()


def _resolve_user_timezone(user_mgr: Optional[UserManager], user_id: Optional[str]) -> str:
    if not user_mgr or not user_id:
        return "UTC"
    try:
        tz = user_mgr.get_timezone(str(user_id))
    except Exception:
        tz = None
    return tz or "UTC"


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
    Return a confirmed lineup status when available (SofaScore confirmed lineups).
    Falls back to Fantrax status only when no SofaScore mapping exists.
    """
    if not info:
        return None
    conf = getattr(info, "ss_conf_status", None)
    if conf is not None:
        if isinstance(conf, LineupStatus):
            return conf if conf != LineupStatus.UNKNOWN else None
        try:
            norm = LineupStatus(str(conf).lower())
            return norm if norm != LineupStatus.UNKNOWN else None
        except Exception:
            return None
    if getattr(info, "sofascore_player_id", None):
        return None
    fx_status = getattr(info, "fx_status", None) or getattr(info, "status", None)
    if fx_status is None:
        return None
    if isinstance(fx_status, LineupStatus):
        return fx_status if fx_status != LineupStatus.UNKNOWN else None
    try:
        norm = LineupStatus(str(fx_status).lower())
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
        summary.append(
            {
                "reserve_id": c.get("reserve_id"),
                "status": c.get("status"),
                "proj_fpts": c.get("proj_fpts"),
                "kos_index": c.get("kos_index"),
                "locked": c.get("locked"),
                "lock_bypass": c.get("lock_bypass"),
                "kickoff": kickoff.isoformat() if kickoff else None,
                "kickoff_local": _format_datetime_for_user(kickoff, user_timezone),
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
                    "source": "auto",
                }
            )
            backups += 1
    return rules


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
            str(r.get("source")) == "auto"
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
        r.setdefault("source", "auto_lineup_swaps")
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
            pending = [r for r in rules if _eligible(r)]
            auto_combos = _auto_enabled_combos(user_mgr, user_id, "lineup_swaps")
            if not pending and not auto_combos:
                continue
            session = _build_session_from_user(user_mgr, user_id)
            if session is None:
                continue
            runs.append((user_id, rules_path, rules, player_locks, session, user_mgr))
        return runs

    rules_path = Path(args.rules_path)
    rules, player_locks = load_rules_with_locks(rules_path)
    pending = [r for r in rules if _eligible(r)]
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
    parser.add_argument(
        "--force-trigger",
        action="store_true",
        help="Skip lineup-status trigger checks (treat all rules as eligible).",
    )
    parser.add_argument("--trace", action="store_true", help="Log detailed per-rule trace information.")
    args = parser.parse_args()

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
        pending = [r for r in rules if _eligible(r)]
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

        if not combos:
            logger.error("No league/team context provided or found in rules (user=%s).", user_id)
            continue

        updated = False
        updated_locks = False
        for league_id, team_id in combos:
            api = FantraxAPI(league_id=league_id, session=session)
            subs_service = SubsService(session=session, league_id=league_id)
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
                        strategy=None,
                        mapping_manager=mapping_manager,
                        round_hint=round_hint,
                        global_status_path=global_status_path_for_user(user_id),
                    )
                except Exception as exc:
                    logger.error("Failed to resolve lineup info for league=%s team=%s: %s", league_id, team_id, exc)
                    continue

                kos_index_map, _first_kos, last_kos = _build_kos_index_map(lineup_info_by_player)

                if (
                    user_id
                    and user_mgr
                    and user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
                    and projections
                ):
                    roster_view = RosterView(roster)
                    had_auto_rules = any(
                        str(r.get("source")) == "auto"
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

                if _apply_inverse_rule_guard(rules, league_id=league_id, team_id=team_id):
                    updated = True

                base_do_not_move: set[str] = set()
                late_kos_policy = "trust"
                if user_id and user_mgr and hasattr(user_mgr, "get_do_not_move"):
                    base_do_not_move = set(user_mgr.get_do_not_move(str(user_id), str(league_id)) or [])
                if user_id and user_mgr and hasattr(user_mgr, "get_late_kos_policy"):
                    late_kos_policy = user_mgr.get_late_kos_policy(str(user_id), str(league_id)) or "trust"

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
                    do_not_move = set(base_do_not_move)

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

                    last_kos_index = max(kos_index_map.values(), default=None)
                    late_cover_active_id: Optional[str] = None
                    if late_kos_policy == "cover" and last_kos_index is not None:
                        late_candidates = []
                        for active in active_candidates:
                            if active.get("kos_index") != last_kos_index:
                                continue
                            active_id = active["active_id"]
                            if active_id in do_not_move:
                                continue
                            active_rules = rules_by_active.get(active_id) or []
                            has_same_kos_backup = any(
                                kos_index_map.get(str(r.get("reserve_id"))) == last_kos_index for r in active_rules
                            )
                            if has_same_kos_backup:
                                continue
                            late_candidates.append(active)
                        if late_candidates:
                            late_cover_active_id = min(
                                late_candidates, key=lambda a: (a.get("proj_fpts") or 0.0)
                            ).get("active_id")

                    swap_executed = False
                    for active in active_candidates:
                        active_id = active["active_id"]
                        active_rules = rules_by_active.get(active_id)
                        if not active_rules:
                            continue
                        active_rules = sorted(active_rules, key=lambda r: r.get("priority") or 999)
                        if (
                            user_id
                            and user_mgr
                            and str(active_rules[0].get("source")) == "auto_lineup_swaps"
                            and not user_mgr.is_auto_rules_enabled(str(user_id), str(league_id), "lineup_swaps")
                        ):
                            logger.info(
                                "Rule %s skipped: auto lineup swaps disabled for league=%s user=%s",
                                active_rules[0].get("rule_id"),
                                league_id,
                                user_id,
                            )
                            continue
                        is_auto_rule = str(active_rules[0].get("source")) == "auto_lineup_swaps"
                        if is_auto_rule:
                            period_id = auto_period_id
                        else:
                            period_id = active_rules[0].get("period")
                            if period_id is None:
                                period_id = args.period or roster_period
                        try:
                            period_id = int(period_id) if period_id is not None else None
                        except Exception:
                            period_id = None
                        if period_id is None:
                            logger.info(
                                "Rule %s proceeding without explicit period; deferring to Fantrax.",
                                active_rules[0].get("rule_id"),
                            )
                        trigger = active_rules[0].get("trigger") or "confirmed_lineup"

                        active_info = lineup_info_by_player.get(active_id)
                        status_kind_fn = _status_kind
                        if not args.force_trigger and trigger == "confirmed_lineup":
                            status_kind_fn = _confirmed_status_kind
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
                            "rule_id": active_rules[0].get("rule_id"),
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

                        lock_bucket = _lock_bucket(
                            player_locks,
                            league_id=league_id,
                            team_id=team_id,
                            period_id=period_id,
                        )
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
                                active_rules[0].get("rule_id"),
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
                                active_rules[0].get("rule_id"),
                                active_status,
                                _player_label(roster_view, active_id),
                            )
                            continue

                        if active_id in do_not_move and active_status != "not_starting":
                            _log_trace(
                                args.trace,
                                logger=logger,
                                meta=trace_meta,
                                ready=False,
                                skip_reason="do_not_move",
                            )
                            logger.info("Rule %s skipped: do-not-move active.", active_rules[0].get("rule_id"))
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
                                        active_rules[0].get("rule_id"),
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
                                        active_rules[0].get("rule_id"),
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
                                    active_rules[0].get("rule_id"),
                                )
                                continue
                        # TODO: additional triggers (e.g., kickoff_passed, injury_flag) can be added here.

                        if roster_view.is_locked(active_id, now=now, lineup_info_by_player=lineup_info_by_player):
                            for r in active_rules:
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
                                active_rules[0].get("rule_id"),
                                _player_label(roster_view, active_id),
                            )
                            continue
                        reserve_candidates = []
                        for r in active_rules:
                            reserve_id = str(r.get("reserve_id"))
                            if reserve_id in used_reserves:
                                continue
                            reserve_info = lineup_info_by_player.get(reserve_id)
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
                            if proj_val is None:
                                row = id_to_row.get(reserve_id)
                                proj_val, _proj_gs = _projection_for_row(row, projections) if row else (0.0, 0)
                            try:
                                proj_val = float(proj_val or 0.0)
                            except Exception:
                                proj_val = 0.0
                            reserve_candidates.append(
                                {
                                    "rule": r,
                                    "reserve_id": reserve_id,
                                    "status": status_kind_fn(reserve_info),
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
                                    logger.info(
                                        "Rule %s skipped: no confirmed reserve starters for %s.",
                                        active_rules[0].get("rule_id"),
                                        _player_label(roster_view, active_id),
                                    )
                                    continue
                                preferred = reserve_candidates

                        else:  # unconfirmed
                            if late_kos_policy == "cover" and active_id == late_cover_active_id and active_kickoff:
                                earlier_confirmed = [
                                    c
                                    for c in reserve_candidates
                                    if c["status"] == "starting" and c.get("kickoff") and c["kickoff"] < active_kickoff
                                ]
                                if earlier_confirmed:
                                    earlier_confirmed.sort(key=lambda c: c["proj_fpts"], reverse=True)
                                    preferred = earlier_confirmed

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
            save_rules(rules, path=rules_path, player_locks=player_locks)


if __name__ == "__main__":
    main()
