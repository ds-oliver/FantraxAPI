"""
Combine SofaScore + Fantrax lineup signals into a unified PlayerLineupInfo map.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

import requests

from fantraxapi.lineups.conditional_swaps import LineupStatus, PlayerLineupInfo
from fantraxapi.lineups.fantrax_lineup_bridge import (
    DEFAULT_GLOBAL_STATUS_PATH,
    build_lineup_info_by_player_fantrax,
    load_global_status_snapshot,
)
from fantraxapi.lineups.sofascore_bridge import build_lineup_info_by_player
from fantraxapi.objs import Roster
from fantraxapi.player_mapping import PlayerMappingManager

logger = logging.getLogger(__name__)


class LineupSourceStrategy(str, Enum):
    SOFASCORE_PRIMARY = "sofascore_primary"
    FANTRAX_PRIMARY = "fantrax_primary"


def resolve_lineup_info(
    roster: Roster,
    *,
    session: requests.Session,
    league_id: str,
    period: Optional[int],
    strategy: LineupSourceStrategy,
    mapping_manager: Optional[PlayerMappingManager] = None,
    round_hint: Optional[str] = None,
    global_status_path: Optional[Path] = None,
) -> Dict[str, PlayerLineupInfo]:
    mapping_manager = mapping_manager or PlayerMappingManager()
    try:
        sofa_map = build_lineup_info_by_player(
            roster,
            mapping_manager=mapping_manager,
            round_hint=round_hint,
        )
    except Exception as exc:
        logger.warning("SofaScore lineup fetch failed: %s", exc)
        sofa_map = {}

    # Optional global Fantrax snapshot (icon/typeId based) cached on disk.
    try:
        snapshot_path = global_status_path or DEFAULT_GLOBAL_STATUS_PATH
        global_fx_map = load_global_status_snapshot(snapshot_path)
    except Exception as exc:
        logger.warning("Fantrax global snapshot load failed: %s", exc)
        global_fx_map = {}

    try:
        fantrax_map = build_lineup_info_by_player_fantrax(
            roster,
            session=session,
            league_id=league_id,
            period=period,
        )
    except Exception as exc:
        logger.warning("Fantrax lineup fetch failed: %s", exc)
        fantrax_map = {}

    merged: Dict[str, PlayerLineupInfo] = {}
    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue
        pid = str(player.id)
        sofa_info = sofa_map.get(pid)
        fantrax_info = fantrax_map.get(pid)
        global_fx = global_fx_map.get(pid)

        base = sofa_info or fantrax_info or PlayerLineupInfo(
            fantrax_player_id=pid,
            sofascore_player_id=getattr(sofa_info, "sofascore_player_id", None),
        )

        # Apply global fantrax snapshot first if available
        if global_fx:
            if getattr(base, "fx_status", None) in (None, LineupStatus.UNKNOWN):
                base.fx_status = global_fx.status
            if getattr(base, "fx_kickoff", None) is None and global_fx.kickoff:
                base.fx_kickoff = global_fx.kickoff
            if getattr(base, "team_name", None) is None and global_fx.team_name:
                base.team_name = global_fx.team_name
            if getattr(base, "opponent_name", None) is None and global_fx.opponent_name:
                base.opponent_name = global_fx.opponent_name
            if getattr(base, "is_home", None) is None and global_fx.is_home is not None:
                base.is_home = global_fx.is_home

        # carry fx fields
        base.fx_status = (
            getattr(base, "fx_status", None)
            or (getattr(fantrax_info, "fx_status", None) if fantrax_info else None)
            or (getattr(fantrax_info, "status", None) if fantrax_info else None)
        )
        base.fx_kickoff = (
            getattr(base, "fx_kickoff", None)
            or (getattr(fantrax_info, "fx_kickoff", None) if fantrax_info else None)
            or (getattr(fantrax_info, "kickoff", None) if fantrax_info else None)
        )

        # carry display metadata with SofaScore priority
        if getattr(base, "event_id", None) is None and getattr(sofa_info, "event_id", None) is not None:
            base.event_id = sofa_info.event_id
        if getattr(base, "team_name", None) is None and getattr(sofa_info, "team_name", None):
            base.team_name = sofa_info.team_name
        if getattr(base, "opponent_name", None) is None and getattr(sofa_info, "opponent_name", None):
            base.opponent_name = sofa_info.opponent_name
        if getattr(base, "is_home", None) is None and getattr(sofa_info, "is_home", None) is not None:
            base.is_home = sofa_info.is_home

        if getattr(base, "event_id", None) is None and getattr(fantrax_info, "event_id", None) is not None:
            base.event_id = fantrax_info.event_id
        if getattr(base, "team_name", None) is None and getattr(fantrax_info, "team_name", None):
            base.team_name = fantrax_info.team_name
        if getattr(base, "opponent_name", None) is None and getattr(fantrax_info, "opponent_name", None):
            base.opponent_name = fantrax_info.opponent_name
        if getattr(base, "is_home", None) is None and getattr(fantrax_info, "is_home", None) is not None:
            base.is_home = fantrax_info.is_home

        # overlay sofascore fields
        base.sofascore_player_id = (
            sofa_info.sofascore_player_id if sofa_info else base.sofascore_player_id
        )
        base.ss_status = (
            getattr(base, "ss_status", None)
            or (getattr(sofa_info, "ss_status", None) if sofa_info else None)
            or (getattr(sofa_info, "status", None) if sofa_info else None)
        )
        base.ss_kickoff = (
            getattr(base, "ss_kickoff", None)
            or (getattr(sofa_info, "ss_kickoff", None) if sofa_info else None)
            or (getattr(sofa_info, "kickoff", None) if sofa_info else None)
        )

        # choose effective
        if strategy == LineupSourceStrategy.FANTRAX_PRIMARY:
            effective_status = base.fx_status or base.ss_status or LineupStatus.UNKNOWN
            status_source = "fantrax" if base.fx_status and base.fx_status != LineupStatus.UNKNOWN else (
                "sofascore" if base.ss_status and base.ss_status != LineupStatus.UNKNOWN else None
            )
        else:
            effective_status = base.ss_status or base.fx_status or LineupStatus.UNKNOWN
            status_source = "sofascore" if base.ss_status and base.ss_status != LineupStatus.UNKNOWN else (
                "fantrax" if base.fx_status and base.fx_status != LineupStatus.UNKNOWN else None
            )

        effective_kickoff = base.ss_kickoff or base.fx_kickoff

        base.status = effective_status or LineupStatus.UNKNOWN
        base.kickoff = effective_kickoff or base.kickoff
        base.status_source = status_source

        merged[pid] = base

    return merged


def _choose_status_primary_sofa(
    sofa_info: Optional[PlayerLineupInfo],
    fantrax_info: Optional[PlayerLineupInfo],
) -> LineupStatus:
    if sofa_info and sofa_info.status != LineupStatus.UNKNOWN:
        return sofa_info.status
    if fantrax_info and fantrax_info.status != LineupStatus.UNKNOWN:
        return fantrax_info.status
    return LineupStatus.UNKNOWN


def _choose_status_primary_fantrax(
    sofa_info: Optional[PlayerLineupInfo],
    fantrax_info: Optional[PlayerLineupInfo],
) -> LineupStatus:
    if fantrax_info and fantrax_info.status != LineupStatus.UNKNOWN:
        return fantrax_info.status
    if sofa_info and sofa_info.status != LineupStatus.UNKNOWN:
        return sofa_info.status
    return LineupStatus.UNKNOWN


__all__ = ["LineupSourceStrategy", "resolve_lineup_info"]
