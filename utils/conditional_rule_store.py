"""
Lightweight persistence for conditional swap rules.

Rules are stored as JSON at data/conditional_rules.json by default.
This module is intentionally simple to avoid external dependencies.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional
import uuid
import time

DEFAULT_RULES_PATH = Path("data/conditional_rules.json")
DEFAULT_RULES_DIR = Path("data/conditional_rules")


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
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except Exception:
        return []


def load_rules_for_user(user_id: str) -> List[Dict[str, Any]]:
    return load_rules(_rules_path_for_user(user_id))


def save_rules(rules: List[Dict[str, Any]], *, path: Path = DEFAULT_RULES_PATH) -> None:
    with _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rules, indent=2))


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
        existing = load_rules(path)
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
        path.write_text(json.dumps(existing + enriched, indent=2))


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
