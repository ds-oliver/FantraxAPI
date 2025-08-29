# utils/roster_ops.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from requests import Session
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster, RosterRow

from utils.auth_helpers import fetch_user_leagues
from fantraxapi.subs import SubsService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeagueTeam:
    league_id: str
    team_id: str
    league_name: str
    team_name: str


class DropService:
    def __init__(self, session: Session):
        self.session = session

    # ---------- discovery ----------
    def list_user_leagues(self) -> List[LeagueTeam]:
        items = fetch_user_leagues(self.session)
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
        raw = getattr(row, "_raw", {}) or {}
        cells = raw.get("cells") or []

        locked_flags = (
            raw.get("isLocked"),
            raw.get("locked"),
            raw.get("lineupLocked"),
            raw.get("lineupChangeLocked"),
        )
        locked = any(bool(x) for x in locked_flags if x is not None)
        if raw.get("lineupAdjustmentAllowed") is False:
            locked = True

        can_drop = raw.get("canDrop")
        if can_drop is not None:
            can_drop = bool(can_drop)
        else:
            can_drop = not locked

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

        reason = raw.get("lockedReason") or raw.get("status") or None
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
        self, *, league_id: str, team_id: str, scorer_id: str
    ) -> Dict[str, Any]:
        roster = self.get_roster(league_id, team_id)
        row = self._find_row(roster, scorer_id)
        if not row:
            current = self.get_current_period(league_id)
            return {
                "locked": False,
                "can_drop_now": False,
                "reason": "Player not on roster",
                "current_period": current,
                "effective_period": None,
            }
        return self._infer_drop_status_from_row(row, league_id)

    def _verify_drop_applied(self, league_id: str, team_id: str, scorer_id: str) -> bool:
        import time

        for _ in range(2):
            time.sleep(1.0)
            roster_after = self.get_roster(league_id, team_id)
            if not self._roster_contains(roster_after, scorer_id):
                return True
        return False

    def get_current_period(self, league_id: str) -> Optional[int]:
        try:
            api = self.make_api(league_id)
            return api.drops.get_current_period()
        except Exception:
            return None

    def drop_player_single(
        self,
        *,
        league_id: str,
        team_id: str,
        scorer_id: str,
        skip_validation: bool = False,
    ) -> Dict[str, Any]:
        api = self.make_api(league_id)
        try:
            raw = api.drops.drop_player(
                team_id=team_id,
                scorer_id=scorer_id,
                period=None,
                skip_validation=skip_validation,
                return_details=True,
            )
            log.debug("[drop] raw response (trunc): %s", str(raw)[:600])
        except Exception as e:
            log.exception("[drop] API call failed")
            result = {
                "ok": False,
                "scheduled": False,
                "drop_period": None,
                "effective_msg": None,
                "messages": [str(e)],
                "verified": None,
                "raw": None,
            }
            log.info(
                "[drop] team=%s player=%s ok=%s scheduled=%s dropP=%s verified=%s msg=%s",
                team_id,
                scorer_id,
                result["ok"],
                result["scheduled"],
                result["drop_period"],
                result["verified"],
                (result["effective_msg"] or "")[:120],
            )
            return result

        def _normalize(res) -> bool:
            if isinstance(res, bool):
                return res
            if res is None:
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
            if isinstance(res, str):
                return res.strip().lower() in {"ok", "success", "true", "1"}
            if isinstance(res, (int, float)):
                return True
            return bool(res)

        ok = _normalize(raw)
        if not isinstance(raw, dict):
            raw = {"ok": bool(ok), "raw": raw, "messages": []}
        else:
            raw.setdefault("ok", bool(ok))

        verified = None
        if raw.get("ok") and not raw.get("scheduled"):
            try:
                verified = self._verify_drop_applied(league_id, team_id, scorer_id)
            except Exception:
                verified = None
            raw["verified"] = verified

        log.info(
            "[drop] team=%s player=%s ok=%s scheduled=%s dropP=%s verified=%s msg=%s",
            team_id,
            scorer_id,
            raw.get("ok"),
            raw.get("scheduled"),
            raw.get("drop_period"),
            raw.get("verified"),
            (raw.get("effective_msg") or "")[:120],
        )
        return raw

    def drop_player_everywhere(
        self,
        *,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False,
    ) -> Dict[str, Dict]:
        results: Dict[str, Dict] = {}
        locations = self.find_player_locations(scorer_id)
        if not locations:
            return results

        for lt in locations:
            try:
                ok = self.drop_player_single(
                    league_id=lt.league_id,
                    team_id=lt.team_id,
                    scorer_id=scorer_id,
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
                log.exception(
                    "drop_player_everywhere: failed for %s/%s",
                    lt.league_id,
                    lt.team_id,
                )
                results[lt.team_id] = {
                    "success": False,
                    "error": str(e),
                    "league_id": lt.league_id,
                    "team_name": lt.team_name,
                    "league_name": lt.league_name,
                }
        return results

    def find_player_locations(self, scorer_id: str) -> List[LeagueTeam]:
        hits: List[LeagueTeam] = []
        for lt in self.list_user_leagues():
            try:
                roster = self.get_roster(lt.league_id, lt.team_id)
                if self._roster_contains(roster, scorer_id):
                    hits.append(lt)
            except Exception as e:
                log.warning(
                    "find_player_locations: failed %s/%s: %s",
                    lt.league_id,
                    lt.team_id,
                    e,
                )
        return hits


class LineupService:
    def __init__(self, session: Session, league_id: str = None):
        self.session = session
        self.league_id = league_id
        self._svc = SubsService(session, league_id=league_id)

    # ----- simple accessors -----
    def get_roster(self, league_id: str, team_id: str) -> Roster:
        return self._svc.get_roster(league_id, team_id)

    def list_starters(self, league_id: str, team_id: str) -> List[RosterRow]:
        return self._svc.list_starters(league_id, team_id)

    def list_bench(self, league_id: str, team_id: str) -> List[RosterRow]:
        return self._svc.list_bench(league_id, team_id)

    def warm_codes_for_roster(self, league_id: str, roster: Roster) -> None:
        self._svc.warm_codes_for_roster(league_id, roster)

    def apply_changes(
        self,
        league_id: str,
        team_id: str,
        changes: List[Tuple[str, str]],
        pos_overrides: dict[str, str] | None = None,
    ) -> bool:
        """
        Apply swaps using full fieldMap with confirm/execute.
        - Decide scheduling based on PRE model flags.
        - If FIN says "locked", auto-retry by scheduling for the next eligible period.
        """
        pos_overrides = pos_overrides or {}

        for idx, (out_id, in_id) in enumerate(changes, 1):
            roster_now = self.get_roster(league_id, team_id)
            current_starters = [
                r.player.id for r in roster_now.get_starters() if getattr(r, "player", None)
            ]
            cur_set = set(current_starters)
            if out_id not in cur_set and in_id in cur_set:
                log.info(
                    "[lineup] %d/%d already satisfied (out=%s in=%s)",
                    idx,
                    len(changes),
                    out_id,
                    in_id,
                )
                continue

            desired_set = set(current_starters)
            if out_id in desired_set:
                desired_set.remove(out_id)
            desired_set.add(in_id)
            desired_list = list(desired_set)

            fmap = self._svc.build_field_map(roster_now, desired_list, pos_overrides)
            if log.isEnabledFor(logging.DEBUG):
                try:
                    preview = {k: fmap[k] for k in (list(fmap)[:6])}
                    log.debug("[lineup] fmap preview (trunc): %s", str(preview)[:300])
                except Exception:
                    pass

            # PRE (try as "now")
            pre = self._svc.confirm_or_execute_lineup(
                league_id=league_id,
                fantasy_team_id=team_id,
                roster_limit_period=0,
                field_map=fmap,
                apply_to_future=False,
                do_finalize=False,
            )

            # If the PRE model says we can't change now, schedule it
            main_msg_pre = (pre.get("mainMsg") or "").lower()
            model = pre.get("model") or {}
            change_allowed = bool(model.get("changeAllowed", True))
            deadline_passed = bool(model.get("playerPickDeadlinePassed"))
            pre_says_locked = ("locked" in main_msg_pre) or (not change_allowed) or deadline_passed

            # Use server-echoed period when applying now; otherwise we'll schedule (period=0)
            server_period = ((model.get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod"))
            period_for_fin = 0 if pre_says_locked else (int(server_period) if server_period is not None else 0)

            # FIN (first attempt)
            fin = self._svc.confirm_or_execute_lineup(
                league_id=league_id,
                fantasy_team_id=team_id,
                roster_limit_period=period_for_fin,
                field_map=fmap,
                apply_to_future=bool(pre_says_locked),
                do_finalize=True,
            )

            ok = bool(fin.get("ok"))
            main_msg_fin = (fin.get("mainMsg") or "").lower()

            # If FIN says "locked" and we weren't already scheduling, retry as scheduled
            if (not ok) and ("locked" in main_msg_fin) and (not pre_says_locked):
                log.info("[lineup] retrying as scheduled change due to FIN lock message")
                fin = self._svc.confirm_or_execute_lineup(
                    league_id=league_id,
                    fantasy_team_id=team_id,
                    roster_limit_period=0,  # let server schedule next eligible
                    field_map=fmap,
                    apply_to_future=True,
                    do_finalize=True,
                )
                ok = bool(fin.get("ok"))

            log.info(
                "[lineup] %d/%d out=%s in=%s ok=%s tgtP=%s type=%s illegal=%s",
                idx,
                len(changes),
                out_id,
                in_id,
                ok,
                (((fin.get("model") or {}).get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")),
                ((fin.get("fantasyResponse") or {}).get("msgType") or "NONE"),
                len(fin.get("illegalMsgs") or []),
            )

            if not ok:
                return False

            # Quick verify (best-effort; scheduled changes won't reflect immediately)
            try:
                after = self.get_roster(league_id, team_id)
                ids = {r.player.id for r in after.get_starters() if getattr(r, "player", None)}
                if (in_id not in ids) and not pre_says_locked:
                    # Only treat as failure if it was meant to apply now
                    log.warning("[lineup] verification failed for out=%s in=%s", out_id, in_id)
                    return False
            except Exception:
                pass

        log.info("[lineup] applied %d/%d changes successfully", len(changes), len(changes))
        return True

    # ---- fast swap helper (unchanged) ----
    def _try_direct_swap(
        self,
        league_id: str,
        team_id: str,
        out_id: str,
        in_id: str,
        *,
        retries: int = 3,
        sleep_s: float = 0.8,
        optimistic_on_ok: bool = True,
    ) -> dict:
        from time import sleep

        api = FantraxAPI(league_id=league_id, session=self.session)

        roster = api.roster_info(team_id)
        row_map = {r.player.id: r for r in roster.rows if getattr(r, "player", None)}
        if out_id not in row_map or in_id not in row_map:
            log.info("[swap-fast] players not both on roster; skip fast path")
            return {"ok": False, "verified": False, "reason": "not_on_roster"}

        is_out_starter = getattr(row_map[out_id], "pos_id", None) != "0"
        is_in_starter = getattr(row_map[in_id], "pos_id", None) != "0"
        if not is_out_starter:
            log.info("[swap-fast] 'out' is not a starter; skip fast path")
            return {"ok": False, "verified": False, "reason": "out_not_starter"}
        if is_in_starter:
            log.info("[swap-fast] 'in' already a starter; nothing to do")
            return {"ok": True, "verified": True, "reason": None}

        out_pos = SubsService._pos_of_row(row_map[out_id])
        in_elig = SubsService.eligible_positions_of_row(row_map[in_id])
        if out_pos not in in_elig:
            log.info("[swap-fast] bench player not eligible for %s; skip fast path", out_pos)
            return {"ok": False, "verified": False, "reason": "not_eligible"}

        try:
            ok = api.swap_players(team_id, out_id, in_id)
            log.info("[swap-fast] api.swap_players -> %s", ok)
        except Exception as e:
            log.info("[swap-fast] api.swap_players raised %s; falling back", e)
            return {"ok": False, "verified": False, "reason": "exception"}

        verified = False
        if ok:
            for i in range(max(1, retries)):
                try:
                    after = api.roster_info(team_id)
                    starters = {
                        r.player.id for r in after.get_starters() if getattr(r, "player", None)
                    }
                    verified = (in_id in starters) and (out_id not in starters)
                    log.info("[swap-fast] verify attempt %d/%d -> %s", i + 1, retries, verified)
                    if verified:
                        break
                except Exception as ve:
                    log.info("[swap-fast] verify attempt %d/%d failed: %s", i + 1, retries, ve)
                sleep(sleep_s)

        if ok and not verified and optimistic_on_ok:
            log.info("[swap-fast] ok=True verify=False (optimistic success)")
            return {"ok": True, "verified": False, "reason": "optimistic"}

        log.info("[swap-fast] verify=%s", verified)
        return {"ok": bool(ok and verified), "verified": bool(verified), "reason": None if ok else "api_false"}

    # ----- bulk XI helpers -----
    def preflight_set_lineup_by_ids(
        self,
        *,
        league_id: str,
        team_id: str,
        desired_starter_ids: List[str],
        pos_overrides: dict | None = None,
    ) -> Dict[str, Any]:
        return self._svc.preflight_set_lineup_by_ids(
            league_id=league_id,
            team_id=team_id,
            desired_starter_ids=desired_starter_ids,
            ensure_unlocked=True,
            pos_overrides=pos_overrides,
        )

    def set_lineup_by_ids(
        self,
        *,
        league_id: str,
        team_id: str,
        desired_starter_ids: List[str],
        best_effort: bool = True,
        verify_each: bool = False,
        pos_overrides: dict | None = None,
        server_confirm: bool = False,
        apply_to_future: bool = False,
        roster_limit_period: Optional[int] = None,
        fantasy_team_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._svc.set_lineup_by_ids(
            league_id=league_id,
            team_id=team_id,
            desired_starter_ids=desired_starter_ids,
            best_effort=best_effort,
            verify_each=verify_each,
            pos_overrides=pos_overrides,
            server_confirm=server_confirm,
            apply_to_future=apply_to_future,
            roster_limit_period=roster_limit_period,
            fantasy_team_id=fantasy_team_id,
        )

    def set_lineup_by_names(self, *, league_id: str, team_id: str, names: List[str], **kwargs) -> Dict[str, Any]:
        roster = self.get_roster(league_id, team_id)
        want_ids = []
        for n in names:
            row = roster.get_player_by_name(n)
            if not row or not getattr(row, "player", None):
                raise ValueError(f"Player not found on roster: {n}")
            want_ids.append(row.player.id)
        return self.set_lineup_by_ids(
            league_id=league_id, team_id=team_id, desired_starter_ids=want_ids, **kwargs
        )
