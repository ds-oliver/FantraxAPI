# utils/roster_ops.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    
    def _find_row(self, roster: Roster, scorer_id: str) -> Optional[RosterRow]:
        for r in roster.rows:
            if r.player and r.player.id == scorer_id:
                return r
        return None

    def _infer_drop_status_from_row(self, row: RosterRow, league_id: str) -> Dict[str, Any]:
        """Heuristic: infer lock/droppable/effective period from roster row raw fields."""
        raw = getattr(row, "_raw", {}) or {}
        cells = raw.get("cells") or []

        # 1) Obvious flags if Fantrax sends them
        locked_flags = (
            raw.get("isLocked"),
            raw.get("locked"),
            raw.get("lineupLocked"),
        )
        locked = any(bool(x) for x in locked_flags if x is not None)

        can_drop = raw.get("canDrop")
        if can_drop is not None:
            can_drop = bool(can_drop)
        else:
            # If we don't have an explicit canDrop, treat "not locked" as droppable for UI,
            # the actual drop() call will still validate.
            can_drop = not locked

        # 2) Fallback: scan cells/tooltips/labels for 'locked' text
        if not locked:
            for c in cells:
                if not isinstance(c, dict):
                    continue
                txt = (c.get("toolTip") or c.get("tooltip") or c.get("content") or "")
                if isinstance(txt, str) and "lock" in txt.lower():
                    locked = True
                    if can_drop is None:
                        can_drop = False
                    break

        # 3) Reason text if available
        reason = raw.get("lockedReason") or raw.get("status") or None

        # 4) Current period and when a drop would take effect
        current = self.get_current_period(league_id)
        effective = current if can_drop else (current + 1 if current is not None else None)

        return {
            "locked": bool(locked),
            "can_drop_now": bool(can_drop),
            "reason": reason,
            "current_period": current,
            "effective_period": effective,
        }

    def get_player_drop_status(
        self,
        *,
        league_id: str,
        team_id: str,
        scorer_id: str
    ) -> Dict[str, Any]:
        """Return a structured status blob for UI/logic."""
        roster = self.get_roster(league_id, team_id)
        row = self._find_row(roster, scorer_id)
        if not row:
            # Not on this roster
            current = self.get_current_period(league_id)
            return {
                "locked": False,
                "can_drop_now": False,
                "reason": "Player not on roster",
                "current_period": current,
                "effective_period": None,
            }
        return self._infer_drop_status_from_row(row, league_id)

    def _normalize_drop_result(self, res) -> bool:
        # Explicit True/False
        if isinstance(res, bool):
            return res

        # Treat None / {} as "accepted" (observed successful drops with empty body)
        if res is None:
            return True
        if isinstance(res, dict) and not res:
            return True

        # Strings like "OK", "Success"
        if isinstance(res, str):
            return res.strip().lower() in {"ok", "success", "true", "1"}

        # Numbers (e.g. transaction id)
        if isinstance(res, (int, float)):
            return True

        if isinstance(res, dict):
            for k in ("success", "ok", "wasSuccessful", "completed", "result", "status"):
                if k in res:
                    v = res[k]
                    if isinstance(v, bool): return v
                    if isinstance(v, str) and v.lower() in {"ok", "success", "true"}: return True
            if res.get("pageError"):
                return False
            return True

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
        skip_validation: bool = False,
    ) -> bool:
        log.info(f"Attempting to drop player {scorer_id} from team {team_id} in league {league_id}")
        
        # Get initial roster state for logging
        try:
            initial_roster = self.get_roster(league_id, team_id)
            player_row = self._find_row(initial_roster, scorer_id)
            if player_row and player_row.player:
                log.info(f"Found player to drop: {player_row.player.name} ({scorer_id})")
                status = self._infer_drop_status_from_row(player_row, league_id)
                log.info(f"Player status: locked={status['locked']}, can_drop_now={status['can_drop_now']}, " 
                        f"current_period={status['current_period']}, effective_period={status['effective_period']}")
            else:
                log.warning(f"Player {scorer_id} not found on initial roster check")
        except Exception as e:
            log.warning(f"Failed to get initial roster state: {e}")

        api = self.make_api(league_id)

        # Don't pass period; let Fantrax infer (immediate if unlocked, next GW if locked)
        try:
            raw = api.drops.drop_player(
                team_id=team_id,
                scorer_id=scorer_id,
                period=None,
                skip_validation=skip_validation,
            )
            log.info(f"Drop API response: {raw}")
            result = self._normalize_drop_result(raw)
            log.info(f"Normalized drop result: {result}")
            
            # Verify the drop if successful
            if result:
                verified = self._verify_drop_applied(league_id, team_id, scorer_id)
                log.info(f"Drop verification result: {verified}")
                if not verified:
                    log.warning("Drop appeared successful but player still on roster")
            
            return result
        except Exception as e:
            log.exception(f"Drop API call failed: {e}")
            raise


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
                    # period=period,  # can be None; service will try to infer
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





class LineupService:
    """
    Helpers for lineup discovery and swaps that reuse the caller's authenticated
    requests.Session (Streamlit/app-managed). No cookie/bootstrap logic here.

    Public methods are non-interactive and safe to call from the Streamlit app.
    """

    def __init__(self, session: Session):
        self.session = session

    # ---------- core accessors ----------

    def make_api(self, league_id: str) -> FantraxAPI:
        return FantraxAPI(league_id=league_id, session=self.session)

    def get_roster(self, league_id: str, team_id: str) -> Roster:
        return self.make_api(league_id).roster_info(team_id)

    def list_starters(self, league_id: str, team_id: str) -> List[RosterRow]:
        roster = self.get_roster(league_id, team_id)
        return roster.get_starters()

    def list_bench(self, league_id: str, team_id: str) -> List[RosterRow]:
        roster = self.get_roster(league_id, team_id)
        return roster.get_bench_players()

    # ---------- lookup helpers ----------

    def _find_row_by_player_id(self, roster: Roster, player_id: str) -> Optional[RosterRow]:
        for r in roster.rows:
            if r.player and r.player.id == player_id:
                return r
        return None

    def _find_row_by_player_name(self, roster: Roster, player_name: str) -> Optional[RosterRow]:
        try:
            return roster.get_player_by_name(player_name)
        except Exception:
            return None

    # ---------- swap actions ----------

    def _normalize_swap_result(self, res: Any) -> bool:
        # Mirror the tolerant handling used for drops
        if isinstance(res, bool):
            return res
        if res is None:
            return True
        if isinstance(res, dict) and not res:
            return True
        if isinstance(res, str):
            return res.strip().lower() in {"ok", "success", "true", "1"}
        if isinstance(res, (int, float)):
            return True
        if isinstance(res, dict):
            for k in ("success", "ok", "wasSuccessful", "completed", "result", "status"):
                if k in res:
                    v = res[k]
                    if isinstance(v, bool):
                        return v
                    if isinstance(v, str) and v.lower() in {"ok", "success", "true"}:
                        return True
            if res.get("pageError"):
                return False
            return True
        return bool(res)

    def swap_players_by_ids(
        self,
        *,
        league_id: str,
        team_id: str,
        starter_player_id: str,
        bench_player_id: str,
    ) -> bool:
        """
        Swap a starter out for a bench player in. This is a thin wrapper around
        FantraxAPI.swap_players(...) that performs minimal local validation and
        normalizes the response to a bool.
        """
        log.info(f"Attempting to swap players in league={league_id}, team={team_id}: "
                f"starter={starter_player_id} ↔ bench={bench_player_id}")
        
        api = self.make_api(league_id)

        # Minimal validation to improve error messages before hitting the API
        try:
            roster = api.roster_info(team_id)
            log.info("Successfully fetched initial roster state")
        except Exception as e:
            log.error("swap_players_by_ids: failed to fetch roster %s/%s: %s", league_id, team_id, e)
            raise

        starter_row = self._find_row_by_player_id(roster, starter_player_id)
        bench_row = self._find_row_by_player_id(roster, bench_player_id)

        # Log detailed player state
        if starter_row and starter_row.player:
            log.info(f"Starter found: {starter_row.player.name} ({starter_player_id})")
            log.info(f"Starter position: {getattr(starter_row, 'pos_id', 'unknown')}, "
                    f"raw data: {getattr(starter_row, '_raw', {})}")
        else:
            log.error(f"Starter player {starter_player_id} not found on roster")
            raise ValueError("Starter player not found on roster")

        if bench_row and bench_row.player:
            log.info(f"Bench player found: {bench_row.player.name} ({bench_player_id})")
            log.info(f"Bench position: {getattr(bench_row, 'pos_id', 'unknown')}, "
                    f"raw data: {getattr(bench_row, '_raw', {})}")
        else:
            log.error(f"Bench player {bench_player_id} not found on roster")
            raise ValueError("Bench player not found on roster")

        # If roles were passed in the wrong order, try to infer and swap
        if getattr(starter_row, "pos_id", None) == "0" and getattr(bench_row, "pos_id", None) != "0":
            log.info("Players appear to be in wrong order, swapping roles")
            starter_row, bench_row = bench_row, starter_row
            starter_player_id, bench_player_id = bench_player_id, starter_player_id

        # Let the API enforce formation/lock rules. We only provide clearer messages.
        try:
            log.info("Calling Fantrax API to perform swap")
            raw = api.swap_players(team_id, starter_player_id, bench_player_id)
            log.info(f"Swap API response: {raw}")
            
            ok = self._normalize_swap_result(raw)
            log.info(f"Normalized swap result: {ok}")
            
            # Verify the swap if successful
            if ok:
                try:
                    roster_after = api.roster_info(team_id)
                    starter_after = self._find_row_by_player_id(roster_after, starter_player_id)
                    bench_after = self._find_row_by_player_id(roster_after, bench_player_id)
                    
                    if starter_after and bench_after:
                        log.info(f"Post-swap positions: {starter_player_id}={getattr(starter_after, 'pos_id', 'unknown')}, "
                                f"{bench_player_id}={getattr(bench_after, 'pos_id', 'unknown')}")
                    else:
                        log.warning("Could not verify final positions after swap")
                except Exception as e:
                    log.warning(f"Failed to verify post-swap state: {e}")
            
            return bool(ok)
        except Exception as e:
            log.exception(f"Swap API call failed: {e}")
            raise

    def swap_players_by_names(
        self,
        *,
        league_id: str,
        team_id: str,
        starter_player_name: str,
        bench_player_name: str,
    ) -> bool:
        """
        Convenience wrapper that resolves player names on the current roster and
        performs the swap. If the provided names are reversed (bench/starter),
        the method will auto-correct based on roster rows.
        """
        roster = self.get_roster(league_id, team_id)

        starter_row = self._find_row_by_player_name(roster, starter_player_name)
        bench_row = self._find_row_by_player_name(roster, bench_player_name)
        if starter_row is None:
            raise ValueError(f"Player not found: {starter_player_name}")
        if bench_row is None:
            raise ValueError(f"Player not found: {bench_player_name}")

        # If both are starters or both are bench, still attempt the swap and
        # let the API decide. If obviously reversed, flip them locally.
        if getattr(starter_row, "pos_id", None) == "0" and getattr(bench_row, "pos_id", None) != "0":
            starter_row, bench_row = bench_row, starter_row

        return self.swap_players_by_ids(
            league_id=league_id,
            team_id=team_id,
            starter_player_id=starter_row.player.id,
            bench_player_id=bench_row.player.id,
        )
