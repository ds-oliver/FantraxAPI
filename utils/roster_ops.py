# utils/roster_ops.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from requests import Session
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster, RosterRow

# Reuse your existing helper so league/team mapping is consistent everywhere
from utils.auth_helpers import fetch_user_leagues

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeagueTeam:
    league_id: str
    team_id: str
    league_name: str
    team_name: str


class DropService:
    """
    A small, portable façade around fantraxapi for roster discovery and 'drop' actions.
    - Uses your authenticated requests.Session (cookies/headers already set).
    - Finds a user's leagues/teams, discovers where a player is rostered,
      and performs single-team or multi-team drops.
    """

    def __init__(self, session: Session):
        self.session = session

    # ---------- discovery ----------

    def list_user_leagues(self) -> List[LeagueTeam]:
        items = fetch_user_leagues(self.session)  # [{"leagueId","teamId","league","team"}, ...]
        return [
            LeagueTeam(
                league_id=i["leagueId"],
                team_id=i["teamId"],
                league_name=i.get("league", ""),
                team_name=i.get("team", ""),
            )
            for i in items
        ]

    def make_api(self, league_id: str) -> FantraxAPI:
        return FantraxAPI(league_id=league_id, session=self.session)

    def get_roster(self, league_id: str, team_id: str) -> Roster:
        return self.make_api(league_id).roster_info(team_id)

    def _roster_contains(self, roster: Roster, scorer_id: str) -> bool:
        for row in roster.rows:
            if row.player and row.player.id == scorer_id:
                return True
        return False

    def _normalize_drop_result(self, res) -> bool:
        """Best-effort interpretation of fantrax drop result."""
        # Exact booleans
        if isinstance(res, bool):
            return res
        # Strings like "OK", "Success"
        if isinstance(res, str):
            return res.strip().lower() in {"ok", "success", "true", "1"}
        # Numbers (e.g. transaction id); treat as success if any int/float returned
        if isinstance(res, (int, float)):
            return True
        # Dict payloads that may carry a status
        if isinstance(res, dict):
            for k in ("success", "ok", "wasSuccessful", "completed", "result", "status"):
                if k in res:
                    v = res[k]
                    if isinstance(v, bool): return v
                    if isinstance(v, str) and v.lower() in {"ok", "success", "true"}: return True
            # If there is an explicit pageError => failure; otherwise assume success-ish
            if res.get("pageError"):
                return False
            return True
        # Fallback: consider any non-empty object as truthy
        return bool(res)

    def _verify_drop_applied(self, league_id: str, team_id: str, scorer_id: str) -> bool:
        """Refetch roster and confirm the player is gone (with a tiny wait+retry)."""
        import time
        for _ in range(2):
            time.sleep(1.0)  # Fantrax can be eventually-consistent for a second or two
            roster_after = self.get_roster(league_id, team_id)
            if not self._roster_contains(roster_after, scorer_id):
                return True
        return False

    def find_player_locations(self, scorer_id: str) -> List[LeagueTeam]:
        """
        Return all (league, team) pairs where this player is currently rostered.
        """
        hits: List[LeagueTeam] = []
        for lt in self.list_user_leagues():
            try:
                roster = self.get_roster(lt.league_id, lt.team_id)
                if self._roster_contains(roster, scorer_id):
                    hits.append(lt)
            except Exception as e:
                log.warning("find_player_locations: failed %s/%s: %s", lt.league_id, lt.team_id, e)
        return hits

    # ---------- period helpers ----------

    def get_current_period(self, league_id: str) -> Optional[int]:
        """
        Best-effort fetch of current period (gameweek). Not required; fantraxapi
        can usually infer current if None is passed.
        """
        try:
            api = self.make_api(league_id)
            # fantraxapi often provides this:
            return api.drops.get_current_period()
        except Exception:
            return None

    # ---------- drop actions ----------

    def drop_player_single(
        self,
        *,
        league_id: str,
        team_id: str,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False,
    ) -> bool:
        api = self.make_api(league_id)
        use_period = period if period is not None else self.get_current_period(league_id)

        # Call the API
        raw = api.drops.drop_player(
            team_id=team_id,
            scorer_id=scorer_id,
            period=use_period,
            skip_validation=skip_validation,
        )

        ok = self._normalize_drop_result(raw)

        # Final authority = roster actually changed
        if self._verify_drop_applied(league_id, team_id, scorer_id):
            return True

        return ok


    def drop_player_everywhere(
        self,
        *,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False,
    ) -> Dict[str, Dict]:
        """
        Drop a player from every roster that has him.
        Returns a mapping: { "<team_id>": {"success": bool, "league_id": str, "team_name": str, "league_name": str, "error": str|None } }
        """
        results: Dict[str, Dict] = {}
        locations = self.find_player_locations(scorer_id)
        if not locations:
            return results  # empty = nowhere to drop

        for lt in locations:
            try:
                ok = self.drop_player_single(
                    league_id=lt.league_id,
                    team_id=lt.team_id,
                    scorer_id=scorer_id,
                    period=period,  # can be None; service will try to infer
                    skip_validation=skip_validation,
                )
                results[lt.team_id] = {
                    "success": bool(ok),
                    "error": None,
                    "league_id": lt.league_id,
                    "team_name": lt.team_name,
                    "league_name": lt.league_name,
                }
            except Exception as e:
                log.exception("drop_player_everywhere: failed for %s/%s", lt.league_id, lt.team_id)
                results[lt.team_id] = {
                    "success": False,
                    "error": str(e),
                    "league_id": lt.league_id,
                    "team_name": lt.team_name,
                    "league_name": lt.league_name,
                }
        return results



