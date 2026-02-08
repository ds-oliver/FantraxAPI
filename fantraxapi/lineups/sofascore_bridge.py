"""
Helpers to bridge SofaScore lineup data with Fantrax rosters.

Provides a utility to build the `fantrax_player_id -> PlayerLineupInfo`
mapping required by the conditional swap UI/engine without duplicating
the parsing logic inside the Streamlit layer.
"""

from __future__ import annotations

import csv
import json
import logging
import yaml
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

from fantraxapi.lineups.conditional_swaps import LineupStatus, PlayerLineupInfo
from fantraxapi.objs import Roster, RosterRow
from fantraxapi.player_mapping import PlayerMappingManager

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]
_LOG_PATH = REPO_ROOT / "data" / "logs" / "conditional_swaps.log"
LOG_TIMEZONE = "America/Los_Angeles"

def _ensure_logger_handler(log: logging.Logger, log_path: Path, *, level: int, tag: str) -> None:
    """
    Attach a file handler when possible. If the log path is not writable (common on VPS when run
    under a non-root user), fall back to stderr instead of crashing at import time.
    """
    if any(getattr(h, "baseFilename", None) == str(log_path) for h in log.handlers):
        return

    formatter = logging.Formatter(f"%(asctime)s %(levelname)s [{tag}] %(message)s")
    formatter.converter = _log_time_converter

    handler: logging.Handler
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path)
    except Exception:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.setLevel(level)

def _log_time_converter(*_args):
    return datetime.now(ZoneInfo(LOG_TIMEZONE)).timetuple()

def _format_log_dt(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(ZoneInfo(LOG_TIMEZONE)).isoformat()
    except Exception:
        return dt.isoformat()
_ensure_logger_handler(logger, _LOG_PATH, level=logging.DEBUG, tag="sofascore_bridge")

DEFAULT_LINEUPS_DIR = REPO_ROOT / "data" / "sofascore" / "lineups"
DEFAULT_SCHEDULE_PATHS = (
    REPO_ROOT / "data" / "sofascore" / "schedules" / "17_76986_upcoming.csv",
    REPO_ROOT / "data" / "sofascore" / "schedules" / "17_76986_last.csv",
)
CLUB_TEAM_MAPPINGS_PATH = REPO_ROOT / "config" / "club_team_mappings.yaml"
TEAM_MAPPINGS_PATH = REPO_ROOT / "config" / "team_mappings.yaml"

# Canonicalization of team names between Fantrax and SofaScore schedules.
# Keys: lower-cased Fantrax teamName/teamShortName or other variants.
# Values: lower-cased canonical name as it appears in SofaScore schedule CSV.
TEAM_NAME_ALIASES: dict[str, str] = {
    "arsenal": "arsenal",
    "aston villa": "aston villa",
    "afc bournemouth": "bournemouth",
    "bournemouth": "bournemouth",
    "brentford": "brentford",
    "burnley": "burnley",
    "chelsea": "chelsea",
    "crystal palace": "crystal palace",
    "everton": "everton",
    "fulham": "fulham",
    "liverpool": "liverpool",
    "sunderland": "sunderland",
    "brighton": "brighton & hove albion",
    "brighton & hove": "brighton & hove albion",
    "brighton & hove albion": "brighton & hove albion",
    "brighton and hove albion": "brighton & hove albion",
    "nottingham f.": "nottingham forest",
    "wolves": "wolverhampton wanderers",
    "wolverhampton": "wolverhampton wanderers",
    "wolverhampton wanderers": "wolverhampton wanderers",
    "wolves fc": "wolverhampton wanderers",
    "wolverhampton wanderers fc": "wolverhampton wanderers",
    "tottenham": "tottenham hotspur",
    "spurs": "tottenham hotspur",
    "tottenham hotspur": "tottenham hotspur",
    "west ham": "west ham united",
    "west ham united": "west ham united",
    "west ham utd": "west ham united",
    "man city": "manchester city",
    "manchester city": "manchester city",
    "man united": "manchester united",
    "man utd": "manchester united",
    "manchester united": "manchester united",
    "newcastle": "newcastle united",
    "newcastle united": "newcastle united",
    "nottingham forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "leeds": "leeds united",
    "leeds utd": "leeds united",
    "leeds united": "leeds united",
    "sun": "sunderland",
    # Fantrax short codes that differ from SofaScore canonical codes.
    "ar": "arsenal",
    "brf": "brentford",
    "fu": "fulham",
    "not": "nottingham forest",
    "wh": "west ham united",
}

TEAM_SHORTCODES: dict[str, str] = {
    "arsenal": "ARS",
    "aston villa": "AVL",
    "afc bournemouth": "BOU",
    "bournemouth": "BOU",
    "brentford": "BRE",
    "burnley": "BUR",
    "chelsea": "CHE",
    "crystal palace": "CRY",
    "everton": "EVE",
    "fulham": "FUL",
    "liverpool": "LIV",
    "sunderland": "SUN",
    "brighton": "BHA",
    "brighton & hove": "BHA",
    "brighton & hove albion": "BHA",
    "brighton and hove albion": "BHA",
    "nottingham f.": "NOT",
    "wolves": "WOL",
    "wolverhampton": "WOL",
    "wolverhampton wanderers": "WOL",
    "wolves fc": "WOL",
    "wolverhampton wanderers fc": "WOL",
    "tottenham": "TOT",
    "spurs": "TOT",
    "tottenham hotspur": "TOT",
    "west ham": "WHU",
    "west ham united": "WHU",
    "west ham utd": "WHU",
    "man city": "MCI",
    "manchester city": "MCI",
    "man united": "MUN",
    "man utd": "MUN",
    "manchester united": "MUN",
    "newcastle": "NEW",
    "newcastle united": "NEW",
    "nottingham forest": "NFO",
    "nottm forest": "NFO",
    "leeds": "LEE",
    "leeds utd": "LEE",
    "leeds united": "LEE",
}
TEAM_CODE_ALIASES: dict[str, str] = {code.lower(): name for name, code in TEAM_SHORTCODES.items()}

_TEAM_ALIAS_CACHE: Optional[dict[str, str]] = None


def _load_team_aliases() -> dict[str, str]:
    """
    Load team name/code aliases from config mappings to canonical SofaScore names.
    """
    global _TEAM_ALIAS_CACHE
    if _TEAM_ALIAS_CACHE is not None:
        return _TEAM_ALIAS_CACHE
    aliases: dict[str, str] = {}

    def _normalize_key(val: Optional[str]) -> Optional[str]:
        if not val:
            return None
        cleaned = "".join(ch for ch in str(val).lower() if ch.isalnum() or ch in (" ", "&"))
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    try:
        with TEAM_MAPPINGS_PATH.open() as fh:
            code_cfg = yaml.safe_load(fh) or {}
        for std, data in code_cfg.items():
            canonical = _normalize_key(std)
            if not canonical:
                continue
            variations = data.get("variations", []) if isinstance(data, dict) else []
            for entry in [std, *variations]:
                key = _normalize_key(entry)
                if key:
                    aliases[key] = canonical
    except Exception:
        pass

    try:
        with CLUB_TEAM_MAPPINGS_PATH.open() as fh:
            club_cfg = yaml.safe_load(fh) or {}
        for std, data in club_cfg.items():
            if not isinstance(data, dict):
                continue
            canonical = _normalize_key(data.get("long_name") or std)
            if not canonical:
                continue
            keys = [std, data.get("long_name"), data.get("short_name")]
            keys += data.get("long_name_variations", []) or []
            keys += data.get("short_name_variations", []) or []
            keys += data.get("nicknames", []) or []
            for entry in keys:
                key = _normalize_key(entry)
                if key:
                    aliases[key] = canonical
    except Exception:
        pass

    _TEAM_ALIAS_CACHE = aliases
    return aliases


def _normalize_team_name(raw: str) -> str:
    """
    Normalize a team name to the canonical key used in schedule_by_team.
    """
    key = (raw or "").strip().lower()
    if key.startswith("@"):
        key = key[1:].strip()
    if key.startswith("vs "):
        key = key[3:].strip()
    if not key:
        return key
    alias_map = _load_team_aliases()
    mapped = TEAM_NAME_ALIASES.get(key)
    if mapped:
        return mapped
    mapped = TEAM_CODE_ALIASES.get(key) or alias_map.get(key) or key
    mapped = TEAM_CODE_ALIASES.get(mapped, mapped)
    return TEAM_NAME_ALIASES.get(mapped, mapped)


def _team_code(raw: Optional[str]) -> Optional[str]:
    """
    Return a 3-letter team code for display.
    Prefers known aliases; falls back to short strings when provided.
    """
    if raw is None:
        return None
    cleaned = str(raw).strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    if not cleaned:
        return None
    canonical = _normalize_team_name(cleaned)
    if canonical in TEAM_SHORTCODES:
        return TEAM_SHORTCODES[canonical]
    if len(cleaned) <= 4 and cleaned.replace(" ", "").isalpha():
        return cleaned.upper()
    if canonical.replace(" ", "") and len(canonical) >= 3:
        return canonical[:3].upper()
    return None

@dataclass
class _SofaPlayerSnapshot:
    sofascore_id: int
    role: str
    reason: Optional[int]
    kickoff: Optional[datetime]
    event_id: Optional[int]
    confirmed: bool
    source_path: Path
    fetched_at: Optional[datetime]


def _parse_kickoff(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    sanitized = value.replace("+0000", "+00:00")
    try:
        return datetime.fromisoformat(sanitized)
    except Exception:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S%z")  # type: ignore[arg-type]
        except Exception:
            logger.debug("[sofascore-bridge] Unable to parse kickoff %s", value)
            return None


def _load_schedule_map(
    paths: Iterable[Path],
    round_filter: Optional[str] = None,
) -> tuple[
    Dict[int, datetime],
    Dict[str, tuple[int, datetime]],
    Dict[int, tuple[str, str]],
]:
    mapping: Dict[int, datetime] = {}
    team_index: Dict[str, tuple[int, datetime]] = {}
    event_team_map: Dict[int, tuple[str, str]] = {}
    now = datetime.now(timezone.utc)
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    round_val = (row.get("round") or row.get("Round") or "").strip()
                    if round_filter and round_val and round_val != round_filter:
                        continue
                    event_raw = row.get("event_id")
                    kickoff_raw = row.get("kickoff_utc")
                    if not event_raw:
                        continue
                    kickoff = _parse_kickoff(kickoff_raw)
                    if not kickoff:
                        continue
                    if not round_filter and kickoff < now:
                        continue
                    try:
                        ev_id = int(event_raw)
                    except ValueError:
                        continue
                    mapping[ev_id] = kickoff
                    home_team_raw = row.get("home_team") or ""
                    away_team_raw = row.get("away_team") or ""
                    event_team_map[ev_id] = (home_team_raw, away_team_raw)
                    for key in ("home_team", "away_team"):
                        tname = row.get(key)
                        if not tname:
                            continue
                        k = _normalize_team_name(str(tname))
                        existing = team_index.get(k)
                        if not existing or kickoff < existing[1]:
                            team_index[k] = (ev_id, kickoff)
        except Exception as exc:
            logger.warning("[sofascore-bridge] Failed reading %s: %s", path, exc)
    return mapping, team_index, event_team_map


def _collect_lineup_event_ids(lineups_dir: Path) -> Set[int]:
    event_ids: Set[int] = set()
    if not lineups_dir.exists():
        return event_ids
    for path in sorted(lineups_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        event_id = payload.get("event_id")
        if isinstance(event_id, (int, float)):
            try:
                event_ids.add(int(event_id))
            except Exception:
                continue
    return event_ids


def _missing_reason_to_status(reason: Optional[int]) -> LineupStatus:
    if reason == 1:
        return LineupStatus.DOUBTFUL
    if reason == 2:
        return LineupStatus.OUT
    return LineupStatus.OUT


def _map_snapshot_to_status(
    *,
    confirmed: bool,
    role: Optional[str],
    reason: Optional[int],
) -> LineupStatus:
    role_normalized = (role or "").strip().lower()
    if confirmed:
        if role_normalized == "starters":
            return LineupStatus.STARTING
        if role_normalized == "subs":
            return LineupStatus.BENCH
        if role_normalized == "missing":
            return _missing_reason_to_status(reason)
        return LineupStatus.UNKNOWN
    # predicted / not confirmed
    if role_normalized == "starters":
        return LineupStatus.STARTING
    if role_normalized == "subs":
        return LineupStatus.BENCH
    if role_normalized == "missing":
        return _missing_reason_to_status(reason)
    return LineupStatus.UNKNOWN


def _reason_label(reason: Optional[int]) -> Optional[str]:
    if reason is None:
        return None
    # SofaScore does not expose full reason text here; keep a descriptive code label.
    return f"SofaScore missing reason code={reason}"


def _prefer_snapshot_by_kickoff(
    existing: Optional[_SofaPlayerSnapshot],
    candidate: _SofaPlayerSnapshot,
    *,
    now: datetime,
) -> _SofaPlayerSnapshot:
    if existing is None:
        return candidate

    cand_kickoff = candidate.kickoff
    exist_kickoff = existing.kickoff
    cand_future = bool(cand_kickoff and cand_kickoff >= now)
    exist_future = bool(exist_kickoff and exist_kickoff >= now)

    if cand_future and not exist_future:
        return candidate
    if exist_future and not cand_future:
        return existing

    if cand_future and exist_future:
        if exist_kickoff and cand_kickoff:
            return candidate if cand_kickoff < exist_kickoff else existing
        return candidate

    # both past or both missing kickoff -> keep the one with the latest kickoff if available
    if not cand_kickoff:
        return existing
    if not exist_kickoff:
        return candidate
    return candidate if cand_kickoff > exist_kickoff else existing


def _prefer_snapshot(
    existing: Optional[_SofaPlayerSnapshot],
    candidate: _SofaPlayerSnapshot,
    *,
    now: datetime,
) -> _SofaPlayerSnapshot:
    if existing is None:
        return candidate
    if candidate.confirmed and not existing.confirmed:
        return candidate
    if existing.confirmed and not candidate.confirmed:
        return existing
    cand_fetch = candidate.fetched_at or datetime.min.replace(tzinfo=timezone.utc)
    exist_fetch = existing.fetched_at or datetime.min.replace(tzinfo=timezone.utc)
    if cand_fetch > exist_fetch:
        return candidate
    if cand_fetch < exist_fetch:
        return existing
    return _prefer_snapshot_by_kickoff(existing, candidate, now=now)


def _kickoff_from_fantrax_row(row: RosterRow) -> Optional[datetime]:
    player = getattr(row, "player", None)
    if not player:
        return None
    raw_val = getattr(player, "next_kickoff", None)
    if raw_val in (None, "", 0):
        scorer_raw = getattr(row, "_raw", {}) or {}
        scorer = scorer_raw.get("scorer") or {}
        cells = scorer_raw.get("cells") or []
        is_locked_flag = scorer.get("disableLineupChange") is True
        is_finished = False
        if cells:
            c0 = (cells[0].get("content") or "").upper()
            if " F" in c0 or c0.endswith(" F"):
                is_finished = True

        if not (is_locked_flag or is_finished):
            logged_flag = getattr(player, "_logged_kickoff_missing", False)
            if not logged_flag:
                setattr(player, "_logged_kickoff_missing", True)
                meta = {}
                for key in ("nextKickoff", "nextEventId", "nextOpponent", "nextOpponentIsAway"):
                    if isinstance(scorer, dict) and key in scorer:
                        meta[key] = scorer.get(key)
                    elif key in scorer_raw:
                        meta[key] = scorer_raw.get(key)
                logger.debug(
                    "Missing Fantrax nextKickoff for %s | extracted_meta=%s",
                    getattr(player, "name", player.id),
                    meta or list(scorer_raw.keys()),
                )
        return None
    try:
        val = int(raw_val)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(raw_val))
        except Exception:
            return None
    if val > 1_000_000_000_000:
        val = val / 1000.0
    return datetime.fromtimestamp(val, tz=timezone.utc)


def _build_sofascore_index(
    *,
    lineups_dir: Path,
    schedule_map: Dict[int, datetime],
    allowed_event_ids: set[int],
) -> Dict[int, Dict[int, _SofaPlayerSnapshot]]:
    events: Dict[int, Dict[int, _SofaPlayerSnapshot]] = {}
    if not lineups_dir.exists():
        logger.debug("[sofascore-bridge] Lineups directory %s missing", lineups_dir)
        return events

    json_paths = sorted(lineups_dir.glob("*.json"))
    now = datetime.now(timezone.utc)

    for path in json_paths:
        try:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.debug("[sofascore-bridge] Unable to parse %s: %s", path, exc)
            continue

        confirmed = bool(payload.get("confirmed"))
        fetched_at = _parse_kickoff(payload.get("fetched_at_utc"))
        try:
            event_id = int(payload.get("event_id"))
        except Exception:
            event_id = None

        if event_id is None or event_id not in allowed_event_ids:
            continue

        kickoff = schedule_map.get(event_id)
        event_snapshots = events.setdefault(event_id, {})

        for side_key in ("home", "away"):
            block = payload.get(side_key) or {}
            for role_key in ("starters", "subs", "missing"):
                for player in block.get(role_key, []) or []:
                    pid = player.get("id")
                    if pid is None:
                        continue
                    try:
                        pid_int = int(pid)
                    except (TypeError, ValueError):
                        continue
                    reason_raw = player.get("reason") if role_key == "missing" else None
                    try:
                        reason = int(reason_raw) if reason_raw is not None else None
                    except (TypeError, ValueError):
                        reason = None
                    snapshot = _SofaPlayerSnapshot(
                        sofascore_id=pid_int,
                        role=role_key,
                        reason=reason,
                        kickoff=kickoff,
                        event_id=event_id,
                        confirmed=confirmed,
                        source_path=path,
                        fetched_at=fetched_at,
                    )
                    current = event_snapshots.get(pid_int)
                    event_snapshots[pid_int] = _prefer_snapshot(current, snapshot, now=now)

    return events


def _collect_player_lineup_context(
    row: RosterRow,
    *,
    mapping_manager: PlayerMappingManager,
    event_index: Dict[int, Dict[int, _SofaPlayerSnapshot]],
    schedule_by_team: Dict[str, tuple[int, datetime]],
    event_team_map: Dict[int, tuple[str, str]],
    allowed_event_ids: set[int],
    now: datetime,
) -> Optional[tuple[PlayerLineupInfo, Dict[str, object]]]:
    player = getattr(row, "player", None)
    if not player or not getattr(player, "id", None):
        return None

    fantrax_id: str = str(player.id)
    mapping = mapping_manager.get_by_fantrax_id(fantrax_id)
    sofascore_id = mapping.sofascore_id if mapping else None

    fantrax_kickoff = _kickoff_from_fantrax_row(row)
    if fantrax_kickoff and fantrax_kickoff < now:
        fantrax_kickoff = None

    scorer = getattr(row, "_raw", {}) or {}
    scorer_block = scorer.get("scorer") or {}

    raw_team_name = (
        getattr(player, "team_name", None)
        or getattr(player, "team_short_name", None)
        or scorer_block.get("teamName")
        or scorer_block.get("teamShortName")
        or ""
    )
    if not raw_team_name:
        scorer_raw = getattr(row, "_raw", {}) or {}
        scorer = scorer_raw.get("scorer") or {}
        raw_team_name = scorer.get("teamShortName") or scorer.get("teamName") or ""
    logger.debug("[lineup-resolve] raw_team_name=%r scorer=%r", raw_team_name, scorer_block)
    team_name = _normalize_team_name(raw_team_name)

    schedule_hit: Optional[tuple[int, datetime]] = None
    schedule_event_id: Optional[int] = None
    schedule_kickoff: Optional[datetime] = None
    team_display_raw: Optional[str] = None
    opponent_display_raw: Optional[str] = None
    is_home: Optional[bool] = None
    if team_name and team_name in schedule_by_team:
        schedule_hit = schedule_by_team[team_name]
        schedule_event_id, kickoff_candidate = schedule_hit
        schedule_kickoff = kickoff_candidate
        if schedule_event_id in event_team_map:
            home_raw, away_raw = event_team_map[schedule_event_id]
            home_norm = _normalize_team_name(home_raw)
            away_norm = _normalize_team_name(away_raw)
            if team_name == home_norm:
                is_home = True
                team_display_raw = _team_code(home_raw) or home_raw
                opponent_display_raw = _team_code(away_raw) or away_raw
            elif team_name == away_norm:
                is_home = False
                team_display_raw = _team_code(away_raw) or away_raw
                opponent_display_raw = _team_code(home_raw) or home_raw
            logger.info(
                "[lineup-resolver] schedule team/opponent for %s: %s vs %s (is_home=%s event_id=%s)",
                fantrax_id,
                team_display_raw or team_name,
                opponent_display_raw,
                is_home,
                schedule_event_id,
            )
    elif fantrax_kickoff:
        # No schedule entry; keep kickoff for debugging only
        schedule_kickoff = None

    snapshot: Optional[_SofaPlayerSnapshot] = None
    if schedule_event_id is not None and sofascore_id is not None:
        snapshot = (event_index.get(schedule_event_id) or {}).get(int(sofascore_id))

    if snapshot is None and sofascore_id is not None and schedule_event_id is None:
        fallback_event_id: Optional[int] = None
        fallback_snapshot: Optional[_SofaPlayerSnapshot] = None
        for eid, snaps in event_index.items():
            cand = snaps.get(int(sofascore_id))
            if not cand:
                continue
            if fallback_snapshot is None:
                fallback_event_id = eid
                fallback_snapshot = cand
                continue
            if cand.kickoff and fallback_snapshot.kickoff:
                if cand.kickoff > fallback_snapshot.kickoff:
                    fallback_event_id = eid
                    fallback_snapshot = cand
        if fallback_snapshot:
            snapshot = fallback_snapshot
            schedule_event_id = fallback_event_id
            schedule_kickoff = fallback_snapshot.kickoff
            if schedule_event_id and schedule_kickoff:
                schedule_hit = (schedule_event_id, schedule_kickoff)
            logger.info(
                "[lineup-resolver] fallback snapshot match for %s event=%s",
                fantrax_id,
                schedule_event_id,
            )

    already_played_current_round = bool(
        snapshot and snapshot.kickoff and snapshot.kickoff <= now
    )

    kickoff: Optional[datetime] = None
    kickoff_source: Optional[str] = None
    if already_played_current_round:
        kickoff = snapshot.kickoff
        kickoff_source = "snapshot_past"
    elif fantrax_kickoff and fantrax_kickoff > now:
        kickoff = fantrax_kickoff
        kickoff_source = "fantrax"
    elif schedule_kickoff:
        kickoff = schedule_kickoff
        kickoff_source = "schedule"
    elif snapshot and snapshot.kickoff and snapshot.kickoff > now:
        kickoff = snapshot.kickoff
        kickoff_source = "snapshot"

    info = PlayerLineupInfo(
        fantrax_player_id=fantrax_id,
        sofascore_player_id=sofascore_id,
        status=LineupStatus.UNKNOWN,
        kickoff=kickoff,
        status_source="sofascore",
        note=_reason_label(snapshot.reason) if snapshot else None,
        ss_status=LineupStatus.UNKNOWN,
        ss_pred_status=None,
        ss_conf_status=None,
        ss_kickoff=kickoff,
        event_id=schedule_event_id,
        team_name=team_display_raw,
        opponent_name=opponent_display_raw,
        is_home=is_home,
    )
    if info.event_id is not None:
        logger.debug(
            "[lineup-resolver] SS team/opponent for %s: %s vs %s (is_home=%s event_id=%s)",
            fantrax_id,
            info.team_name,
            info.opponent_name,
            info.is_home,
            info.event_id,
        )

    # Fallback team/opponent from Fantrax scorer if schedule did not populate
    if info.event_id is None and (not info.team_name or not info.opponent_name):
        scorer_full = getattr(row, "_raw", {}) or {}
        scorer_block = scorer_full.get("scorer") or {}
        team_fallback_raw = scorer_block.get("teamShortName") or scorer_block.get("teamName")
        opp_fallback_raw = (
            scorer_block.get("nextOpponentShortName")
            or scorer_block.get("nextOpponent")
            or scorer_block.get("nextOpponentName")
        )
        team_fallback = _team_code(team_fallback_raw) or team_fallback_raw
        opp_fallback = _team_code(opp_fallback_raw) or opp_fallback_raw
        is_home_fallback: Optional[bool] = None
        if opp_fallback_raw and "nextOpponentIsAway" in scorer_block:
            is_home_fallback = not bool(scorer_block.get("nextOpponentIsAway"))
        if team_fallback and not info.team_name:
            info.team_name = team_fallback
        if opp_fallback and not info.opponent_name:
            info.opponent_name = opp_fallback
        if info.is_home is None and is_home_fallback is not None:
            info.is_home = is_home_fallback
        if team_fallback or opp_fallback:
            logger.info(
                "[lineup-resolver] FX fallback team/opponent for %s: %s vs %s (is_home=%s)",
                fantrax_id,
                info.team_name,
                info.opponent_name,
                info.is_home,
            )
    if info.kickoff is not None and not isinstance(info.kickoff, datetime):
        raise TypeError(
            f"PlayerLineupInfo.kickoff must be datetime or None "
            f"(got {type(info.kickoff)} for player={fantrax_id}, source={kickoff_source})"
        )

    if kickoff and snapshot:
        mapped_status = _map_snapshot_to_status(
            confirmed=snapshot.confirmed,
            role=snapshot.role,
            reason=snapshot.reason,
        )
        if snapshot.confirmed:
            info.ss_conf_status = mapped_status
        else:
            info.ss_pred_status = mapped_status
        info.ss_status = mapped_status
        info.status = mapped_status
    elif not kickoff:
        info.status = LineupStatus.UNKNOWN
        info.ss_status = LineupStatus.UNKNOWN

    if info.status != LineupStatus.UNKNOWN and not kickoff:
        logger.warning(
            "[lineup] Non-UNKNOWN status without kickoff | player=%s event=%s team=%s snapshot=%s",
            fantrax_id,
            getattr(snapshot, "event_id", None),
            team_name,
            getattr(snapshot, "source_path", None),
        )

    debug_ctx: Dict[str, object] = {
        "fantrax_player_id": fantrax_id,
        "sofascore_player_id": sofascore_id,
        "snapshot": snapshot,
        "fantrax_kickoff": fantrax_kickoff,
        "kickoff": kickoff,
        "schedule_hit": schedule_hit,
        "team_name_raw": raw_team_name,
        "team_name": team_name,
        "status": info.status,
        "event_id": schedule_event_id,
        "kickoff_source": kickoff_source,
        "allowed_event_ids": allowed_event_ids,
    }

    return info, debug_ctx


def debug_player_lineup_context(
    roster: Roster,
    fantrax_player_id: str,
    *,
    mapping_manager: Optional[PlayerMappingManager] = None,
    lineups_dir: Path | str = DEFAULT_LINEUPS_DIR,
    schedule_paths: Optional[Iterable[Path | str]] = None,
    round_hint: Optional[str] = None,
) -> Optional[Dict[str, object]]:
    """
    Build a verbose debug context for a single Fantrax player.
    Includes SofaScore snapshot metadata and kickoff derivation details.
    """
    mapping_manager = mapping_manager or PlayerMappingManager()
    lineups_dir = Path(lineups_dir)
    schedule_iterable = (
        [Path(p) for p in schedule_paths] if schedule_paths else DEFAULT_SCHEDULE_PATHS
    )
    schedule_map, schedule_by_team, event_team_map = _load_schedule_map(
        schedule_iterable, round_filter=round_hint
    )
    lineup_event_ids = _collect_lineup_event_ids(lineups_dir)
    allowed_event_ids = {event_id for event_id, _ in schedule_by_team.values()} | lineup_event_ids
    event_index = _build_sofascore_index(
        lineups_dir=lineups_dir,
        schedule_map=schedule_map,
        allowed_event_ids=allowed_event_ids,
    )
    event_confirmed_map = {
        event_id: any(snapshot.confirmed for snapshot in snapshots.values())
        for event_id, snapshots in event_index.items()
    }

    target_row = None
    for row in roster.rows:
        player = getattr(row, "player", None)
        if player and str(getattr(player, "id", "")) == str(fantrax_player_id):
            target_row = row
            break

    if target_row is None:
        return None

    now = datetime.now(timezone.utc)
    collected = _collect_player_lineup_context(
        target_row,
        mapping_manager=mapping_manager,
        event_index=event_index,
        schedule_by_team=schedule_by_team,
        event_team_map=event_team_map,
        allowed_event_ids=allowed_event_ids,
        now=now,
    )
    if not collected:
        return None

    _, debug_ctx = collected
    snapshot = debug_ctx.get("snapshot")
    schedule_hit = debug_ctx.get("schedule_hit")
    if isinstance(schedule_hit, tuple):
        schedule_event_id, schedule_kickoff = schedule_hit
    else:
        schedule_event_id, schedule_kickoff = None, None
    lineup_status = debug_ctx.get("status")
    kickoff = debug_ctx.get("kickoff")
    snapshot_event_id = getattr(snapshot, "event_id", None)

    if isinstance(lineup_status, LineupStatus) and lineup_status is not LineupStatus.UNKNOWN:
        if not kickoff or kickoff <= now:
            logger.warning(
                "[lineup-debug] Non-future kickoff for player %s status=%s kickoff=%s event=%s",
                fantrax_player_id,
                lineup_status,
                _format_log_dt(kickoff),
                snapshot_event_id,
            )
        if snapshot_event_id is not None and snapshot_event_id not in allowed_event_ids:
            logger.warning(
                "[lineup-debug] Snapshot event %s not allowed for player %s (allowed=%s)",
                snapshot_event_id,
                fantrax_player_id,
                sorted(allowed_event_ids),
            )

    return {
        "fantrax_player_id": debug_ctx.get("fantrax_player_id"),
        "sofascore_player_id": debug_ctx.get("sofascore_player_id"),
        "team_name_raw": debug_ctx.get("team_name_raw"),
        "team_name_normalized": debug_ctx.get("team_name"),
        "snapshot_event_id": snapshot_event_id,
        "snapshot_confirmed": getattr(snapshot, "confirmed", None),
        "snapshot_role": getattr(snapshot, "role", None) or "none",
        "lineup_status": lineup_status,
        "kickoff": kickoff,
        "fantrax_kickoff": debug_ctx.get("fantrax_kickoff"),
        "schedule_event_id": schedule_event_id,
        "schedule_kickoff": schedule_kickoff,
        "snapshot_source": getattr(snapshot, "source_path", None),
    }


def build_lineup_info_by_player(
    roster: Roster,
    *,
    mapping_manager: Optional[PlayerMappingManager] = None,
    lineups_dir: Path | str = DEFAULT_LINEUPS_DIR,
    schedule_paths: Optional[Iterable[Path | str]] = None,
    round_hint: Optional[str] = None,
    primary_source: str = "sofascore",
) -> Dict[str, PlayerLineupInfo]:
    """
    Build fantrax_player_id -> PlayerLineupInfo mapping for a roster.

    Prefers confirmed SofaScore lineups when available; otherwise the most
    recent predicted lineup is used before falling back to UNKNOWN.
    """
    mapping_manager = mapping_manager or PlayerMappingManager()
    lineups_dir = Path(lineups_dir)
    schedule_iterable = (
        [Path(p) for p in schedule_paths] if schedule_paths else DEFAULT_SCHEDULE_PATHS
    )
    schedule_map, schedule_by_team, event_team_map = _load_schedule_map(
        schedule_iterable,
        round_filter=round_hint,
    )
    lineup_event_ids = _collect_lineup_event_ids(lineups_dir)
    allowed_event_ids = {event_id for event_id, _ in schedule_by_team.values()} | lineup_event_ids
    event_index = _build_sofascore_index(
        lineups_dir=lineups_dir,
        schedule_map=schedule_map,
        allowed_event_ids=allowed_event_ids,
    )
    event_confirmed_map = {
        event_id: any(snapshot.confirmed for snapshot in snapshots.values())
        for event_id, snapshots in event_index.items()
    }

    info: Dict[str, PlayerLineupInfo] = {}
    now = datetime.now(timezone.utc)
    stats = {
        "scheduled_event": 0,
        "missing_schedule": 0,
        "no_kickoff": 0,
        "total": 0,
    }

    for row in roster.rows:
        collected = _collect_player_lineup_context(
            row,
            mapping_manager=mapping_manager,
            event_index=event_index,
            schedule_by_team=schedule_by_team,
            event_team_map=event_team_map,
            allowed_event_ids=allowed_event_ids,
            now=now,
        )
        if not collected:
            continue
        player_info, debug_ctx = collected
        event_id = player_info.event_id
        team_norm = debug_ctx.get("team_name")
        if isinstance(team_norm, str):
            team_norm = team_norm.strip().lower()

        schedule_kickoff: Optional[datetime] = None
        if event_id is not None:
            schedule_kickoff = schedule_map.get(event_id)
            if schedule_kickoff and not player_info.kickoff:
                player_info.kickoff = schedule_kickoff
            if schedule_kickoff and getattr(player_info, "ss_kickoff", None) is None:
                player_info.ss_kickoff = schedule_kickoff

            event_teams = event_team_map.get(event_id)
            if event_teams and team_norm:
                home_raw, away_raw = event_teams
                home_norm = _normalize_team_name(home_raw)
                away_norm = _normalize_team_name(away_raw)
                if team_norm == home_norm:
                    player_info.team_name = _team_code(home_raw) or home_raw
                    player_info.opponent_name = _team_code(away_raw) or away_raw
                    if player_info.is_home is None:
                        player_info.is_home = True
                elif team_norm == away_norm:
                    player_info.team_name = _team_code(away_raw) or away_raw
                    player_info.opponent_name = _team_code(home_raw) or home_raw
                    if player_info.is_home is None:
                        player_info.is_home = False

        player_info.ss_status = (
            player_info.ss_conf_status
            or player_info.ss_pred_status
            or player_info.ss_status
        )
        if player_info.status == LineupStatus.UNKNOWN and player_info.ss_status:
            player_info.status = player_info.ss_status
        player_info.ss_kickoff = player_info.kickoff
        fantrax_id = player_info.fantrax_player_id
        snapshot = debug_ctx.get("snapshot")
        kickoff = player_info.kickoff
        team_name = debug_ctx.get("team_name")
        event_id = debug_ctx.get("event_id")
        status = player_info.status

        if event_id:
            stats["scheduled_event"] += 1
        else:
            stats["missing_schedule"] += 1

        if not kickoff:
            stats["no_kickoff"] += 1
            if stats["no_kickoff"] <= 5:
                logger.info(
                    "No kickoff for %s (team=%s, schedule_event=%s, snapshot=%s)",
                    getattr(getattr(row, "player", None), "name", fantrax_id),
                    team_name,
                    event_id,
                    snapshot.source_path.name if snapshot else None,
                )
        if status == LineupStatus.UNKNOWN and stats["no_kickoff"] <= 5:
            logger.info(
                "[unknown-status] %s (team=%s, sofascore_id=%s, schedule_event=%s, snapshot=%s, kickoff=%s)",
                getattr(getattr(row, "player", None), "name", fantrax_id),
                team_name,
                getattr(snapshot, "sofascore_id", None) if snapshot else None,
                event_id,
                getattr(snapshot, "source_path", None),
                _format_log_dt(kickoff),
            )
        if event_id is not None and schedule_kickoff:
            # If we have a scheduled event and still no SofaScore status (not in starters/subs/missing),
            # treat as bench rather than unknown so downstream displays stay consistent.
            if player_info.ss_status == LineupStatus.UNKNOWN:
                player_info.ss_status = LineupStatus.BENCH
                if player_info.status == LineupStatus.UNKNOWN:
                    player_info.status = LineupStatus.BENCH

            if event_confirmed_map.get(event_id):
                if player_info.ss_conf_status is None:
                    if player_info.ss_pred_status is not None:
                        player_info.ss_status = LineupStatus.BENCH
                        if player_info.status == player_info.ss_pred_status:
                            player_info.status = LineupStatus.BENCH
                    if player_info.ss_status is None or player_info.ss_status == LineupStatus.UNKNOWN:
                        player_info.ss_status = LineupStatus.BENCH
                    player_info.ss_conf_status = player_info.ss_status

        info[fantrax_id] = player_info
        stats["total"] += 1

    logger.info(
        "lineup-info built for %s players (with_schedule=%s, missing_schedule=%s, missing_kickoff=%s)",
        stats["total"],
        stats["scheduled_event"],
        stats["missing_schedule"],
        stats["no_kickoff"],
    )

    return info


def infer_current_gameweek(
    schedule_paths: Optional[Iterable[Path | str]] = None,
) -> Optional[str]:
    """
    Infer the active gameweek (round) based on the upcoming SofaScore schedule.

    Returns the round as a string, or None if it cannot be determined.
    """
    schedule_iterable = (
        [Path(p) for p in schedule_paths] if schedule_paths else DEFAULT_SCHEDULE_PATHS
    )
    now = datetime.now(timezone.utc)
    chosen_round: Optional[str] = None
    chosen_kickoff: Optional[datetime] = None

    for path in schedule_iterable:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    kickoff = _parse_kickoff(row.get("kickoff_utc"))
                    if not kickoff or kickoff < now:
                        continue
                    round_val = row.get("round") or row.get("Round")
                    if not round_val:
                        continue
                    round_str = str(round_val).strip()
                    if not round_str:
                        continue
                    if chosen_kickoff is None or kickoff < chosen_kickoff:
                        chosen_round = round_str
                        chosen_kickoff = kickoff
        except Exception as exc:
            logger.warning("[sofascore-bridge] Failed to parse schedule %s: %s", path, exc)

    return chosen_round


__all__ = [
    "build_lineup_info_by_player",
    "debug_player_lineup_context",
    "infer_current_gameweek",
]
