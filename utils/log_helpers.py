# utils/log_helpers.py
from __future__ import annotations
import json, hashlib
from typing import Dict, Set, Tuple

def fmap_digest(fmap: Dict[str, Dict]) -> str:
    """Stable 8-char hash for a fieldMap (posId,int / stId,'1'|'2')."""
    try:
        norm = {
            str(k): {"posId": int(v.get("posId")), "stId": "1" if str(v.get("stId")) == "1" else "2"}
            for k, v in (fmap or {}).items()
            if isinstance(v, dict) and v.get("posId") is not None and v.get("stId") is not None
        }
        s = json.dumps(norm, sort_keys=True, separators=(",", ":"))
        return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return "NA"

def fmap_counts(fmap: Dict[str, Dict]) -> Tuple[int, int, int]:
    """Return (total, starters, bench) counts from a fieldMap."""
    total = len(fmap or {})
    starters = sum(1 for v in (fmap or {}).values() if str(v.get("stId")) == "1")
    return total, starters, total - starters

def summarize_diff(current_starters: Set[str], desired_starters: Set[str]) -> Dict[str, list]:
    """Tiny diff of starters → which IDs would bench/start (capped to 6 for log hygiene)."""
    to_bench = sorted(list(current_starters - desired_starters))[:6]
    to_start = sorted(list(desired_starters - current_starters))[:6]
    return {"to_bench": to_bench, "to_start": to_start}

def fmap_delta(before_fmap: Dict[str, Dict], after_fmap: Dict[str, Dict], focus_ids: Set[str]) -> Dict[str, Dict]:
    """Compare fieldMaps for specific player IDs to detect actual changes."""
    changes = {}
    for pid in focus_ids:
        a = before_fmap.get(pid)
        b = after_fmap.get(pid)
        if a != b:
            changes[pid] = {"from": a, "to": b}
    return changes
