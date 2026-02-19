"""
Lightweight persistence for conditional swap rules.

Rules are stored as JSON at data/conditional_rules.json by default.
This module is intentionally simple to avoid external dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, List, Dict, Any, Optional, Tuple
import uuid
import time

DEFAULT_RULES_PATH = Path("data/conditional_rules.json")
DEFAULT_RULES_DIR = Path("data/conditional_rules")
DEFAULT_JOURNAL_DIR = Path("data/conditional_rules_journal")
LOCK_CLEANUP_TTL_DAYS = 14
SOURCE_AUTO_LINEUP_SWAPS = "auto_lineup_swaps"
LEGACY_AUTO_SOURCE_ALIASES = {"auto", SOURCE_AUTO_LINEUP_SWAPS}
AUTO_RULE_PRUNE_FIRED_FROM_RULES = (
    str(os.getenv("AUTO_RULE_PRUNE_FIRED_FROM_RULES", "false")).strip().lower() == "true"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_rule_source(source: str) -> str:
    norm = str(source or "").strip().lower()
    if norm in LEGACY_AUTO_SOURCE_ALIASES:
        return SOURCE_AUTO_LINEUP_SWAPS
    return norm or str(source or "")


def _normalize_rule_record(rule: Dict[str, Any]) -> Dict[str, Any]:
    rec = dict(rule)
    if "source" in rec:
        rec["source"] = normalize_rule_source(str(rec.get("source") or ""))
    return rec


def _normalize_rules(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_normalize_rule_record(rule) for rule in (rules or []) if isinstance(rule, dict)]


def _default_writer_env() -> str:
    env = str(os.getenv("CONDITIONAL_WRITER_ENV", "")).strip().lower()
    if env in {"vps", "local"}:
        return env
    return "vps" if Path("/opt/FantraxAPI").exists() else "local"


def _default_state_role() -> str:
    explicit = str(os.getenv("CONDITIONAL_STATE_ROLE", "")).strip().lower()
    if explicit in {"writer", "reader"}:
        return explicit
    return "writer" if _default_writer_env() == "vps" else "reader"


def conditional_state_role() -> str:
    return _default_state_role()


def is_conditional_state_writer() -> bool:
    return conditional_state_role() == "writer"


def _base_meta() -> Dict[str, Any]:
    return {
        "revision": 0,
        "writer_env": _default_writer_env() or "unknown",
        "updated_at": _now_iso(),
    }


def _load_rules_payload(path: Path) -> Dict[str, Any]:
    """
    Load rules payload from disk.
    Supports legacy list format or dict format with player_locks/meta.
    """
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {"meta": _base_meta(), "rules": [], "player_locks": {}}
    except Exception:
        return {"meta": _base_meta(), "rules": [], "player_locks": {}}

    if isinstance(payload, dict):
        rules = _normalize_rules(payload.get("rules") or [])
        player_locks = payload.get("player_locks") or {}
        meta = payload.get("meta") or {}
        if not isinstance(rules, list):
            rules = []
        if not isinstance(player_locks, dict):
            player_locks = {}
        if not isinstance(meta, dict):
            meta = {}
        revision = meta.get("revision")
        try:
            revision = int(revision)
        except Exception:
            revision = 0
        normalized_meta = {
            "revision": max(revision, 0),
            "writer_env": str(meta.get("writer_env") or _default_writer_env() or "unknown"),
            "updated_at": str(meta.get("updated_at") or _now_iso()),
        }
        cleaned = _cleanup_player_locks(rules, player_locks)
        return {"meta": normalized_meta, "rules": rules, "player_locks": cleaned}

    if isinstance(payload, list):
        rules = _normalize_rules(payload)
        cleaned = _cleanup_player_locks(rules, {})
        return {"meta": _base_meta(), "rules": rules, "player_locks": cleaned}

    return {"meta": _base_meta(), "rules": [], "player_locks": {}}


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


def _journal_path_for_user(user_id: str) -> Path:
    return DEFAULT_JOURNAL_DIR / f"{user_id}.jsonl"


def rules_path_for_user(user_id: str) -> Path:
    return _rules_path_for_user(user_id)


def journal_path_for_user(user_id: str) -> Path:
    return _journal_path_for_user(user_id)


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


def load_rules_with_meta(path: Path = DEFAULT_RULES_PATH) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    payload = _load_rules_payload(path)
    return (
        list(payload.get("rules") or []),
        dict(payload.get("player_locks") or {}),
        dict(payload.get("meta") or {}),
    )


def load_rules_for_user(user_id: str) -> List[Dict[str, Any]]:
    return load_rules(_rules_path_for_user(user_id))


def load_rules_for_user_with_locks(user_id: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    return load_rules_with_locks(_rules_path_for_user(user_id))


def load_rules_for_user_state(user_id: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    return load_rules_with_meta(_rules_path_for_user(user_id))


def _write_payload(
    *,
    path: Path,
    rules: List[Dict[str, Any]],
    player_locks: Dict[str, Any],
    meta: Dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_rules = _normalize_rules(rules)
    cleaned_locks = _cleanup_player_locks(cleaned_rules, player_locks)
    normalized_meta = {
        "revision": int(meta.get("revision") or 0),
        "writer_env": str(meta.get("writer_env") or "unknown"),
        "updated_at": str(meta.get("updated_at") or _now_iso()),
    }
    data = {"meta": normalized_meta, "rules": cleaned_rules, "player_locks": cleaned_locks}
    path.write_text(json.dumps(data, indent=2))


def save_rules(
    rules: List[Dict[str, Any]],
    *,
    path: Path = DEFAULT_RULES_PATH,
    player_locks: Optional[Dict[str, Any]] = None,
    actor_env: str = "unknown",
) -> None:
    with _file_lock(path):
        payload = _load_rules_payload(path)
        if player_locks is None:
            player_locks = payload.get("player_locks") or {}
        meta = dict(payload.get("meta") or _base_meta())
        meta["revision"] = int(meta.get("revision") or 0) + 1
        meta["updated_at"] = _now_iso()
        meta["writer_env"] = actor_env if actor_env and actor_env != "unknown" else str(meta.get("writer_env") or _default_writer_env())
        _write_payload(
            path=path,
            rules=list(rules),
            player_locks=dict(player_locks or {}),
            meta=meta,
        )


def _source_type_for(source: str) -> int:
    norm = normalize_rule_source(source)
    if norm in {"suggested_swaps"}:
        return 1
    if norm in {SOURCE_AUTO_LINEUP_SWAPS}:
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
        meta = dict(payload.get("meta") or _base_meta())
        ts = _now_iso()
        resolved_source_type = source_type if source_type is not None else _source_type_for(source)
        enriched = []
        for r in rules:
            rec = dict(r)
            rec.setdefault("rule_id", uuid.uuid4().hex)
            rec.setdefault("created_at", ts)
            rec.setdefault("source", normalize_rule_source(source))
            rec.setdefault("source_type", resolved_source_type)
            rec.setdefault("state", "pending")
            rec.setdefault("fired_count", 0)
            rec.setdefault("max_fires", default_max_fires)
            enriched.append(rec)
        final_rules = existing + enriched
        meta["revision"] = int(meta.get("revision") or 0) + 1
        meta["updated_at"] = _now_iso()
        meta["writer_env"] = str(meta.get("writer_env") or _default_writer_env())
        _write_payload(
            path=path,
            rules=final_rules,
            player_locks=player_locks,
            meta=meta,
        )


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


def _apply_upsert_rules(
    *,
    base_rules: List[Dict[str, Any]],
    items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    updated = list(base_rules)
    index_by_rule_id = {
        str(r.get("rule_id")): idx
        for idx, r in enumerate(updated)
        if r.get("rule_id")
    }
    changed = 0
    for item in items:
        rec = _normalize_rule_record(dict(item))
        rid = str(rec.get("rule_id") or "")
        if rid and rid in index_by_rule_id:
            idx = index_by_rule_id[rid]
            if updated[idx] != rec:
                updated[idx] = rec
                changed += 1
            continue
        updated.append(rec)
        if rid:
            index_by_rule_id[rid] = len(updated) - 1
        changed += 1
    return updated, changed


def _rule_matches_selector(rule: Dict[str, Any], selector: Dict[str, Any]) -> bool:
    rule_ids = selector.get("rule_ids")
    if isinstance(rule_ids, list) and rule_ids:
        return str(rule.get("rule_id") or "") in {str(rid) for rid in rule_ids}
    group_id = selector.get("group_id")
    if group_id is not None and str(group_id) != "":
        return str(rule.get("group_id") or "") == str(group_id)
    matcher = selector.get("matcher")
    if isinstance(matcher, dict) and matcher:
        for key, value in matcher.items():
            if str(rule.get(key) or "") != str(value):
                return False
        return True
    return False


def _apply_patch_rules(
    *,
    base_rules: List[Dict[str, Any]],
    selector: Dict[str, Any],
    patch: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], int]:
    updated = []
    changed = 0
    for rule in base_rules:
        if _rule_matches_selector(rule, selector):
            next_rule = dict(rule)
            next_rule.update(patch or {})
            next_rule = _normalize_rule_record(next_rule)
            if next_rule != rule:
                changed += 1
            updated.append(next_rule)
        else:
            updated.append(rule)
    return updated, changed


def _apply_delete_rules(
    *,
    base_rules: List[Dict[str, Any]],
    selector: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], int]:
    retained = []
    deleted = 0
    for rule in base_rules:
        if _rule_matches_selector(rule, selector):
            deleted += 1
            continue
        retained.append(rule)
    return retained, deleted


def apply_rule_operations_for_user(
    user_id: str,
    operations: List[Dict[str, Any]],
    *,
    expected_revision: Optional[int] = None,
    actor_env: str = "unknown",
) -> Dict[str, Any]:
    path = _rules_path_for_user(user_id)
    with _file_lock(path):
        payload = _load_rules_payload(path)
        rules = list(payload.get("rules") or [])
        player_locks = dict(payload.get("player_locks") or {})
        meta = dict(payload.get("meta") or _base_meta())
        revision_before = int(meta.get("revision") or 0)
        conflict = False
        conflict_reason = ""

        if expected_revision is not None and int(expected_revision) != revision_before:
            # Optimistic versioning with deterministic rebase: apply operations over latest state.
            # Mark conflict only when selector-based operations target no records.
            pass

        total_changes = 0
        for op in operations or []:
            kind = str(op.get("op") or "").strip().lower()
            if kind == "upsert_rules":
                rules, changed = _apply_upsert_rules(
                    base_rules=rules,
                    items=list(op.get("items") or []),
                )
                total_changes += changed
                continue
            if kind == "patch_rules":
                selector = dict(op.get("selector") or {})
                patch = dict(op.get("patch") or {})
                rules, changed = _apply_patch_rules(
                    base_rules=rules,
                    selector=selector,
                    patch=patch,
                )
                if changed == 0 and expected_revision is not None and int(expected_revision) != revision_before:
                    conflict = True
                    conflict_reason = "patch_target_missing_after_rebase"
                total_changes += changed
                continue
            if kind == "delete_rules":
                selector = dict(op.get("selector") or {})
                rules, deleted = _apply_delete_rules(
                    base_rules=rules,
                    selector=selector,
                )
                if deleted == 0 and expected_revision is not None and int(expected_revision) != revision_before:
                    conflict = True
                    conflict_reason = "delete_target_missing_after_rebase"
                total_changes += deleted
                continue

        meta["revision"] = revision_before + 1
        meta["updated_at"] = _now_iso()
        meta["writer_env"] = actor_env if actor_env and actor_env != "unknown" else str(meta.get("writer_env") or _default_writer_env())
        _write_payload(path=path, rules=rules, player_locks=player_locks, meta=meta)
        return {
            "ok": True,
            "conflict": conflict,
            "conflict_reason": conflict_reason,
            "revision_before": revision_before,
            "revision_after": int(meta.get("revision") or 0),
            "changed_count": total_changes,
            "rules_count": len(rules),
        }


def _event_dedupe_key(event: Dict[str, Any]) -> str:
    bits = [
        str(event.get("user_id") or ""),
        str(event.get("rule_id") or ""),
        str(event.get("fired_at") or event.get("occurred_at_utc") or ""),
        str(event.get("active_id") or ""),
        str(event.get("reserve_id") or ""),
        str(event.get("result") or ""),
    ]
    return hashlib.sha256("|".join(bits).encode("utf-8")).hexdigest()


def append_execution_event_for_user(user_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    path = _journal_path_for_user(user_id)
    with _file_lock(path):
        existing = set()
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except Exception:
                        continue
                    key = str(parsed.get("idempotency_key") or "")
                    if key:
                        existing.add(key)
            except Exception:
                existing = set()

        rec = dict(event or {})
        rec["user_id"] = str(rec.get("user_id") or user_id)
        rec.setdefault("event_id", uuid.uuid4().hex)
        rec.setdefault("occurred_at_utc", _now_iso())
        rec["source"] = normalize_rule_source(str(rec.get("source") or ""))
        rec.setdefault("runner_host", socket.gethostname())
        rec.setdefault("runner_pid", os.getpid())
        rec["idempotency_key"] = str(rec.get("idempotency_key") or _event_dedupe_key(rec))
        if rec["idempotency_key"] in existing:
            return {"ok": True, "deduped": True, "idempotency_key": rec["idempotency_key"]}

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        return {"ok": True, "deduped": False, "idempotency_key": rec["idempotency_key"]}


def load_execution_events_for_user(
    user_id: str,
    *,
    league_id: Optional[str] = None,
    team_id: Optional[str] = None,
    period: Optional[str] = None,
    action_type: Optional[str] = None,
    result: Optional[str] = None,
    max_rows: int = 2000,
) -> List[Dict[str, Any]]:
    path = _journal_path_for_user(user_id)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if league_id is not None and str(rec.get("league_id") or "") != str(league_id):
            continue
        if team_id is not None and str(rec.get("team_id") or "") != str(team_id):
            continue
        if period is not None and str(rec.get("period") or "") != str(period):
            continue
        if action_type is not None and str(rec.get("action_type") or "") != str(action_type):
            continue
        if result is not None and str(rec.get("result") or "") != str(result):
            continue
        rec["source"] = normalize_rule_source(str(rec.get("source") or ""))
        out.append(rec)
        if len(out) >= max_rows:
            break
    out.sort(
        key=lambda r: str(r.get("occurred_at_utc") or ""),
        reverse=True,
    )
    return out
