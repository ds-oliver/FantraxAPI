"""
Bridge Fantrax FXPA player listings into the LineupStatus model.

This module fetches the "Players" table via /fxpa/req, inspects the icons
Fantrax renders (typeId 12 == starting, 32 == expected, etc.), and produces
fantrax_player_id -> LineupStatus mappings that can be merged with SofaScore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

from fantraxapi.lineups.conditional_swaps import LineupStatus, PlayerLineupInfo
from fantraxapi.objs import Roster, RosterRow
from fantraxapi.lineups.sofascore_bridge import _kickoff_from_fantrax_row, _team_code

logger = logging.getLogger(__name__)
FXPA_URL = "https://www.fantrax.com/fxpa/req"


def _fxpa_post(
    session: requests.Session,
    league_id: str,
    *,
    payload: dict,
    timeout: int = 30,
) -> dict:
    headers = {
        "Accept": "application/json; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        res = session.post(
            FXPA_URL,
            params={"leagueId": league_id},
            json=payload,
            timeout=timeout,
            headers=headers,
        )
        res.raise_for_status()
        return res.json()
    except Exception as exc:
        logger.error("[fantrax-lineup] FXPA call failed (%s): %s", payload, exc)
        raise


def fetch_fantrax_player_status_snapshot(
    session: requests.Session,
    league_id: str,
    *,
    misc_display_type: str = "10",
    status_filter: str = "ALL",
    max_results: int = 500,
    period: Optional[int] = None,
) -> dict:
    """
    Fetch the raw FXPA payload for the Players table using a miscDisplayType.
    misc_display_type:
        1 -> Standard listing
        10 -> Starting (default)
    """
    base_data = {
        "view": "STATS",
        "positionOrGroup": "ALL",
        "statusOrTeamFilter": status_filter,
        "displayedStatusOrTeam": status_filter,
        "miscDisplayType": misc_display_type,
        "displayedMiscDisplayType": misc_display_type,
        "pageNumber": "1",
        "maxResultsPerPage": str(max_results),
        "leagueId": league_id,
    }
    if period is not None:
        base_data["periodId"] = str(period)

    payload = {
        "msgs": [
            {
                "method": "getPlayerStats",
                "data": base_data,
            }
        ],
        "uiv": 3,
        "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/players",
        "dt": 0,
        "at": 0,
        "av": "0.0",
    }

    tz = session.headers.get("X-TZ")
    if tz:
        payload["tz"] = tz
    ui_ver = session.headers.get("X-Fantrax-UI-Version")
    if ui_ver:
        payload["v"] = ui_ver

    return _fxpa_post(session, league_id, payload=payload)


def _status_from_icons(scorer: dict) -> LineupStatus:
    # Explicit server-side lock usually means the match is over / slot cannot change
    if scorer.get("disableLineupChange") is True:
        return LineupStatus.OUT

    icons = scorer.get("icons") or []
    icons = icons or []
    for ic in icons:
        tooltip = (ic.get("tooltip") or "").lower()
        type_id = str(ic.get("typeId"))
        if type_id == "12" or "starting in upcoming" in tooltip:
            return LineupStatus.STARTING
    for ic in icons:
        tooltip = (ic.get("tooltip") or "").lower()
        type_id = str(ic.get("typeId"))
        if type_id == "32" or "expected to play" in tooltip:
            return LineupStatus.STARTING
    return LineupStatus.UNKNOWN


def _derive_fx_status_and_kickoff(
    row: RosterRow,
    *,
    now: Optional[datetime] = None,
) -> tuple[LineupStatus, Optional[datetime]]:
    if now is None:
        now = datetime.now(timezone.utc)

    raw = getattr(row, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}
    cells = raw.get("cells") or []
    icons = scorer.get("icons") or []
    upcoming = scorer.get("upcomingEventStatusId")
    kickoff = _kickoff_from_fantrax_row(row)
    if kickoff and kickoff < now:
        kickoff = None

    locked_flag = bool(scorer.get("disableLineupChange") is True)
    finished_flag = False
    if cells:
        content = (cells[0].get("content") or "").upper()
        if " F" in content or content.endswith(" F"):
            finished_flag = True

    if locked_flag and finished_flag:
        return LineupStatus.OUT, kickoff

    has_starting_icon = any(str(icon.get("typeId")) == "12" for icon in icons)
    has_expected_icon = any(str(icon.get("typeId")) == "32" for icon in icons)

    if has_starting_icon:
        return LineupStatus.STARTING, kickoff
    if has_expected_icon:
        return LineupStatus.STARTING, kickoff

    if str(upcoming) == "2":
        return LineupStatus.STARTING, kickoff

    return LineupStatus.UNKNOWN, kickoff


@dataclass(frozen=True)
class FantraxPlayerStatus:
    fantrax_player_id: str
    status: LineupStatus
    icons: list[dict]
    event_id: Optional[str] = None


def parse_fantrax_player_statuses(payload: dict) -> dict[str, FantraxPlayerStatus]:
    responses = payload.get("responses") or []
    first = responses[0] if responses else {}
    data = first.get("data") or {}
    stats_table = data.get("statsTable") or []
    results: Dict[str, FantraxPlayerStatus] = {}

    for row in stats_table:
        scorer = row.get("scorer") or {}
        fantrax_id = scorer.get("scorerId")
        if not fantrax_id:
            continue
        icons = scorer.get("icons") or []
        status = _status_from_icons(scorer)

        event_id = None
        cells = row.get("cells") or []
        if len(cells) >= 3 and isinstance(cells[2], dict):
            event_id = cells[2].get("eventId")

        results[str(fantrax_id)] = FantraxPlayerStatus(
            fantrax_player_id=str(fantrax_id),
            status=status,
            icons=icons,
            event_id=event_id,
        )

    return results


def build_lineup_info_by_player_fantrax(
    roster: Roster,
    *,
    session: requests.Session,
    league_id: str,
    period: Optional[int] = None,
    misc_display_type: str = "10",
) -> Dict[str, PlayerLineupInfo]:
    mapping: Dict[str, PlayerLineupInfo] = {}
    unknown_logged = 0
    now = datetime.now(timezone.utc)

    for row in roster.rows:
        player = getattr(row, "player", None)
        if not player or not getattr(player, "id", None):
            continue
        fantrax_id = str(player.id)

        fx_status, fx_kickoff = _derive_fx_status_and_kickoff(row, now=now)

        if fx_status == LineupStatus.UNKNOWN and unknown_logged < 5:
            scorer = getattr(row, "_raw", {}).get("scorer", {}) if getattr(row, "_raw", None) else {}
            icons = scorer.get("icons") or []
            logger.info(
                "[fantrax-status] UNKNOWN for %s icons=%s upcomingEventStatusId=%s disableLineupChange=%s",
                getattr(player, "name", fantrax_id),
                [ic.get("typeId") for ic in icons],
                scorer.get("upcomingEventStatusId"),
                scorer.get("disableLineupChange"),
            )
            unknown_logged += 1

        pli = mapping.get(fantrax_id)
        if pli is None:
            pli = PlayerLineupInfo(
                fantrax_player_id=fantrax_id,
                sofascore_player_id=None,
            )
            mapping[fantrax_id] = pli

        pli.fx_status = fx_status
        pli.fx_kickoff = fx_kickoff
        # Only populate display fixture metadata if SofaScore hasn't already done so
        scorer = getattr(row, "_raw", {}) or {}
        scorer_block = scorer.get("scorer") or {}
        team_val_raw = scorer_block.get("teamShortName") or scorer_block.get("teamName")
        opp_val_raw = (
            scorer_block.get("nextOpponentShortName")
            or scorer_block.get("nextOpponent")
            or scorer_block.get("nextOpponentName")
        )
        team_val = _team_code(team_val_raw) or team_val_raw
        opp_val = _team_code(opp_val_raw) or opp_val_raw
        is_home_val: Optional[bool] = None
        if opp_val and "nextOpponentIsAway" in scorer_block:
            is_home_val = not bool(scorer_block.get("nextOpponentIsAway"))

        filled_from_fantrax = False
        if team_val and pli.team_name is None:
            pli.team_name = team_val
            filled_from_fantrax = True
        if opp_val and pli.opponent_name is None:
            pli.opponent_name = opp_val
            filled_from_fantrax = True
        if pli.is_home is None and is_home_val is not None:
            pli.is_home = is_home_val
            filled_from_fantrax = True

        if filled_from_fantrax:
            logger.info(
                "[fantrax-status] FX fallback team/opponent for %s: %s vs %s (is_home=%s) "
                "| short_names? team=%s opp=%s",
                fantrax_id,
                pli.team_name,
                pli.opponent_name,
                pli.is_home,
                team_val,
                opp_val,
            )

    return mapping


def debug_fx_lineup_context(roster: Roster, fantrax_player_id: str) -> Optional[dict]:
    row_match: Optional[RosterRow] = None
    for r in roster.rows:
        p = getattr(r, "player", None)
        if p and str(getattr(p, "id", "")) == str(fantrax_player_id):
            row_match = r
            break
    if row_match is None:
        return None

    now = datetime.now(timezone.utc)
    fx_status, fx_kickoff = _derive_fx_status_and_kickoff(row_match, now=now)

    raw = getattr(row_match, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}
    cells = raw.get("cells") or []

    return {
        "fantrax_player_id": fantrax_player_id,
        "upcomingEventStatusId": scorer.get("upcomingEventStatusId"),
        "disableLineupChange": scorer.get("disableLineupChange"),
        "icons": scorer.get("icons"),
        "first_cell_content": cells[0].get("content") if cells else None,
        "fx_status": fx_status.value,
        "fx_kickoff": fx_kickoff.isoformat() if fx_kickoff else None,
    }


def debug_fantrax_player_status(payload: dict, fantrax_player_id: str) -> Optional[dict]:
    statuses = parse_fantrax_player_statuses(payload)
    status = statuses.get(fantrax_player_id)
    if not status:
        return None
    return {
        "fantrax_player_id": status.fantrax_player_id,
        "status": status.status.value,
        "event_id": status.event_id,
        "icons": status.icons,
    }


__all__ = [
    "FantraxPlayerStatus",
    "build_lineup_info_by_player_fantrax",
    "debug_fantrax_player_status",
    "debug_fx_lineup_context",
    "fetch_fantrax_player_status_snapshot",
    "parse_fantrax_player_statuses",
]
