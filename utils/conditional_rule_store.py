"""
Lightweight persistence for conditional swap rules.

Rules are stored as JSON at data/conditional_rules.json by default.
This module is intentionally simple to avoid external dependencies.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional
import uuid
import time

DEFAULT_RULES_PATH = Path("data/conditional_rules.json")
DEFAULT_RULES_DIR = Path("data/conditional_rules")
LOCK_CLEANUP_TTL_DAYS = 14


def _load_rules_payload(path: Path) -> Dict[str, Any]:
    """
    Load rules payload from disk.
    Supports legacy list format or dict format with player_locks.
    """
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {"rules": [], "player_locks": {}}
    except Exception:
        return {"rules": [], "player_locks": {}}

    if isinstance(payload, dict):
        rules = payload.get("rules") or []
        player_locks = payload.get("player_locks") or {}
        if not isinstance(rules, list):
            rules = []
        if not isinstance(player_locks, dict):
            player_locks = {}
        cleaned = _cleanup_player_locks(rules, player_locks)
        return {"rules": rules, "player_locks": cleaned}

    if isinstance(payload, list):
        cleaned = _cleanup_player_locks(payload, {})
        return {"rules": payload, "player_locks": cleaned}

    return {"rules": [], "player_locks": {}}


def _lock_bucket_key_from_rule(rule: Dict[str, Any]) -> str:
    league_id = str(rule.get("league_id") or "")
    team_id = str(rule.get("team_id") or "")
    period = str(rule.get("period") or "")
    return f"{league_id}:{team_id}:{period}"


def _cleanup_player_locks(
    rules: List[Dict[str, Any]],
    player_locks: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(player_locks, dict):
        return {}
    keyset = {
        _lock_bucket_key_from_rule(rule)
        for rule in rules
        if rule.get("league_id") is not None and rule.get("team_id") is not None
    }
    if not keyset:
        return {}
    now = datetime.now(timezone.utc)
    cleaned: Dict[str, Any] = {}
    for key, bucket in player_locks.items():
        if key not in keyset:
            continue
        if not isinstance(bucket, dict):
            continue
        next_bucket: Dict[str, Any] = {}
        for player_id, entry in bucket.items():
            if not isinstance(entry, dict):
                continue
            locked_at = entry.get("locked_at")
            if locked_at:
                try:
                    ts = datetime.fromisoformat(locked_at)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    ts = None
                if ts and (now - ts) > timedelta(days=LOCK_CLEANUP_TTL_DAYS):
                    continue
            next_bucket[player_id] = entry
        if next_bucket:
            cleaned[key] = next_bucket
    return cleaned


def _rules_path_for_user(user_id: str) -> Path:
    return DEFAULT_RULES_DIR / f"{user_id}.json"


def rules_path_for_user(user_id: str) -> Path:
    return _rules_path_for_user(user_id)


@contextmanager
def _file_lock(path: Path, timeout_seconds: float = 5.0):
    lock_path = path.with_suffix(path.suffix + ".lock")
    start = time.time()
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("x")
            break
        except FileExistsError:
            if time.time() - start > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for lock {lock_path}")
            time.sleep(0.05)
    try:
        lock_file.write(str(time.time()))
        lock_file.flush()
        yield
    finally:
        try:
            lock_file.close()
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def load_rules(path: Path = DEFAULT_RULES_PATH) -> List[Dict[str, Any]]:
    payload = _load_rules_payload(path)
    return list(payload.get("rules") or [])


def load_rules_with_locks(path: Path = DEFAULT_RULES_PATH) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    payload = _load_rules_payload(path)
    return list(payload.get("rules") or []), dict(payload.get("player_locks") or {})


def load_rules_for_user(user_id: str) -> List[Dict[str, Any]]:
    return load_rules(_rules_path_for_user(user_id))


def load_rules_for_user_with_locks(user_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    return load_rules_with_locks(_rules_path_for_user(user_id))


def save_rules(
    rules: List[Dict[str, Any]],
    *,
    path: Path = DEFAULT_RULES_PATH,
    player_locks: Optional[Dict[str, Any]] = None,
) -> None:
    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if player_locks is None:
            payload = _load_rules_payload(path)
            player_locks = payload.get("player_locks") or {}
        cleaned_locks = _cleanup_player_locks(rules, player_locks)
        data = {"rules": list(rules), "player_locks": cleaned_locks}
        path.write_text(json.dumps(data, indent=2))


def _source_type_for(source: str) -> int:
    norm = (source or "").lower()
    if norm in {"suggested_swaps"}:
        return 1
    if norm in {"auto", "auto_lineup_swaps"}:
        return 2
    if norm in {"manual"}:
        return 3
    return 0


def append_rules(
    rules: Iterable[Dict[str, Any]],
    *,
    path: Path = DEFAULT_RULES_PATH,
    source: str = "suggested_swaps",
    source_type: Optional[int] = None,
    default_max_fires: int = 1,
) -> None:
    with _file_lock(path):
        payload = _load_rules_payload(path)
        existing = payload.get("rules") or []
        player_locks = payload.get("player_locks") or {}
        ts = datetime.now(timezone.utc).isoformat()
        resolved_source_type = source_type if source_type is not None else _source_type_for(source)
        enriched = []
        for r in rules:
            rec = dict(r)
            rec.setdefault("rule_id", uuid.uuid4().hex)
            rec.setdefault("created_at", ts)
            rec.setdefault("source", source)
            rec.setdefault("source_type", resolved_source_type)
            rec.setdefault("state", "pending")
            rec.setdefault("fired_count", 0)
            rec.setdefault("max_fires", default_max_fires)
            enriched.append(rec)
        path.parent.mkdir(parents=True, exist_ok=True)
        final_rules = existing + enriched
        cleaned_locks = _cleanup_player_locks(final_rules, player_locks)
        data = {"rules": final_rules, "player_locks": cleaned_locks}
        path.write_text(json.dumps(data, indent=2))


def append_rules_for_user(
    user_id: str,
    rules: Iterable[Dict[str, Any]],
    *,
    source: str = "suggested_swaps",
    source_type: Optional[int] = None,
    default_max_fires: int = 1,
) -> None:
    append_rules(
        rules,
        path=_rules_path_for_user(user_id),
        source=source,
        source_type=source_type,
        default_max_fires=default_max_fires,
    )
