"""
Lightweight loader for team strength artifacts built from Understat.

The JSON artifact is produced by scripts/build_team_strengths.py and is
expected at data/derived/team_strengths_pl.json by default.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

from fantraxapi.lineups.sofascore_bridge import TEAM_NAME_ALIASES

DEFAULT_STRENGTH_PATH = Path("data/derived/team_strengths_pl.json")


def _canonical_key(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    base = (str(name).strip().lower())
    if isinstance(TEAM_NAME_ALIASES, dict):
        base = TEAM_NAME_ALIASES.get(base, base)
    cleaned = "".join(ch for ch in base if ch.isalnum())
    return cleaned or None


def _timestamp_value(row: dict) -> str:
    return str(row.get("updated_at") or "")


@lru_cache(maxsize=4)
def load_strengths(path: Path = DEFAULT_STRENGTH_PATH) -> Dict[str, dict]:
    """
    Return mapping keyed by canonical team name and team_code (lowercased).
    Latest updated_at wins if duplicates exist.
    """
    try:
        payload = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    mapping: Dict[str, dict] = {}
    for row in payload if isinstance(payload, list) else []:
        key = _canonical_key(row.get("team")) or _canonical_key(row.get("name"))
        code = (row.get("team_code") or row.get("code") or "").strip().lower()
        for candidate in filter(None, [key, code]):
            existing = mapping.get(candidate)
            if not existing or _timestamp_value(row) > _timestamp_value(existing):
                mapping[candidate] = row
    return mapping


def strength_for_team(
    *,
    team_name: Optional[str],
    team_code: Optional[str] = None,
    season: Optional[int] = None,
    strengths: Optional[Dict[str, dict]] = None,
) -> Optional[float]:
    """
    Lookup strength for a team. If season provided, prefer season match;
    otherwise return the latest available record.
    """
    data = strengths or load_strengths()
    candidates = []
    for key in (
        _canonical_key(team_name),
        team_code.lower() if team_code else None,
    ):
        if key and key in data:
            candidates.append(data[key])

    if not candidates:
        return None

    if season is not None:
        for row in candidates:
            try:
                if int(row.get("season")) == int(season):
                    return float(row.get("strength"))
            except Exception:
                continue

    newest = max(candidates, key=_timestamp_value)
    try:
        return float(newest.get("strength"))
    except Exception:
        return None


__all__ = ["load_strengths", "strength_for_team", "DEFAULT_STRENGTH_PATH"]
