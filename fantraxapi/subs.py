# fantraxapi/subs.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from requests import Session
from fantraxapi import FantraxAPI
from fantraxapi.objs import Roster, RosterRow

log = logging.getLogger(__name__)

# Stable mapping observed in FXPA payloads
_ID_TO_CODE = {
    701: "F",  # Forward
    702: "M",  # Midfielder
    703: "D",  # Defender
    704: "G",  # Goalkeeper
}

# Map {'F','M','D','G'} -> {701,702,703,704} (numeric posIds)
_CODE_TO_ID = {v: k for k, v in _ID_TO_CODE.items()}  # ints

# player_id (scorerId) -> {'G','D','M','F'}
_ELIG_CACHE: Dict[str, Set[str]] = {}


@dataclass(frozen=True)
class Formation:
    gk: int
    d: int
    m: int
    f: int

    def is_legal(self) -> bool:
        total = self.gk + self.d + self.m + self.f
        return (
            total == 11
            and self.gk == 1
            and 3 <= self.d <= 5
            and 2 <= self.m <= 5
            and 1 <= self.f <= 3
        )


class SubsService:
    """
    Low-level lineup (substitutions) service.
    - Reuses an authenticated requests.Session (no cookie bootstrap here).
    - Helpers to list starters/bench, swap, preflight, plan & set a full XI.
    """

    def __init__(self, session: Session, league_id: str = None):
        self.session = session
        self.league_id = league_id

    # -------- core plumbing --------
    def _api(self, league_id: str) -> FantraxAPI:
        return FantraxAPI(league_id=league_id, session=self.session)

    def get_roster(self, league_id: str, team_id: str) -> Roster:
        return self._api(league_id).roster_info(team_id)

    def list_starters(self, league_id: str, team_id: str) -> List[RosterRow]:
        return self.get_roster(league_id, team_id).get_starters()

    def list_bench(self, league_id: str, team_id: str) -> List[RosterRow]:
        return self.get_roster(league_id, team_id).get_bench_players()

    # -------- small helpers --------
    @staticmethod
    def _find_row_by_id(roster: Roster, player_id: str) -> Optional[RosterRow]:
        for r in roster.rows:
            if getattr(r, "player", None) and r.player.id == player_id:
                return r
        return None

    @staticmethod
    def _row_locked(row: Optional[RosterRow]) -> bool:
        if not row:
            return False
        raw = getattr(row, "_raw", {}) or {}

        # direct boolean flags we’ve seen
        flags = [
            raw.get("isLocked"),
            raw.get("locked"),
            raw.get("lineupLocked"),
            raw.get("lineupChangeLocked"),
        ]
        if any(bool(x) for x in flags if x is not None):
            return True

        # Some payloads expose the inverse
        if raw.get("lineupAdjustmentAllowed") is False:
            return True

        # heuristic: any boolean key containing 'lock' that is True
        try:
            for k, v in raw.items():
                if isinstance(k, str) and "lock" in k.lower() and isinstance(v, bool) and v:
                    return True
        except Exception:
            pass

        # textual hints in cells/tooltips
        for c in (raw.get("cells") or []):
            if isinstance(c, dict):
                txt = (c.get("toolTip") or c.get("tooltip") or c.get("content") or "")
                if isinstance(txt, str) and "lock" in txt.lower():
                    return True
        return False

    @staticmethod
    def _normalize_result(res: Any) -> bool:
        if isinstance(res, bool):
            return res
        if res is None:
            return True
        if isinstance(res, dict) and not res:
            return True
        if isinstance(res, (int, float)):
            return True
        if isinstance(res, str):
            return res.strip().lower() in {"ok", "true", "success", "1"}
        if isinstance(res, dict):
            if res.get("pageError"):
                return False
            for k in ("success", "ok", "wasSuccessful", "completed", "status", "result"):
                if k in res:
                    v = res[k]
                    if isinstance(v, bool):
                        return v
                    if isinstance(v, str) and v.lower() in {"ok", "true", "success"}:
                        return True
            return True
        return bool(res)

    # ---------- Position helpers ----------
    @staticmethod
    def _pos_of_row(row: RosterRow, overrides: dict | None = None) -> str:
        pid = getattr(getattr(row, "player", None), "id", None)
        if overrides and pid in overrides:
            return overrides[pid]
        sn = (getattr(getattr(row, "pos", None), "short_name", "") or "").upper()
        if sn in {"G", "D", "M", "F"} and getattr(row, "pos_id", None) != "0":
            return sn
        elig = SubsService.eligible_positions_of_row(row)
        if len(elig) == 1:
            return next(iter(elig))
        return next(iter(elig)) if elig else "?"

    @staticmethod
    def _normalize_pos_token(tok: str) -> str:
        if not tok:
            return ""
        t = str(tok).strip().upper()
        if t in {"G", "GK", "GKP", "GOALKEEPER"}:
            return "G"
        if t in {"D", "DEF", "DEFENDER", "WB"}:
            return "D"
        if t in {"M", "MID", "MIDFIELDER", "CM", "DM", "AM", "W"}:
            return "M"
        if t in {"F", "FW", "FWD", "STRIKER", "ST"}:
            return "F"
        return ""

    @classmethod
    def _extract_pos_codes_from_value(cls, v) -> set[str]:
        out: set[str] = set()
        if v is None:
            return out
        if isinstance(v, (list, tuple, set)):
            for item in v:
                if isinstance(item, (list, tuple, set, dict)):
                    out |= cls._extract_pos_codes_from_value(item)
                else:
                    out.add(cls._normalize_pos_token(str(item)))
            return {x for x in out if x}
        if isinstance(v, dict):
            for key in ("position", "pos", "shortName", "short_name", "display", "abbr"):
                if key in v:
                    out |= cls._extract_pos_codes_from_value(v.get(key))
            return {x for x in out if x}
        s = str(v)
        for tok in [
            x
            for d in [",", "/", "|", " "]
            for x in s.replace("/", " / ").replace("|", " | ").split(d)
        ]:
            code = cls._normalize_pos_token(tok)
            if code:
                out.add(code)
        return {x for x in out if x}

    @staticmethod
    def eligible_positions_of_row(row) -> set[str]:
        """
        Best-effort {'G','D','M','F'} for *any* row, starter or bench.
        Tries (in order): starter slot, raw row hints, cache, player attrs.
        """
        codes: Set[str] = set()
        pid = getattr(getattr(row, "player", None), "id", None)

        # Starter slot is authoritative if not a bench slot
        sn = (getattr(getattr(row, "pos", None), "short_name", "") or "").upper()
        if sn in {"G", "D", "M", "F"} and getattr(row, "pos_id", None) != "0":
            codes.add(sn)

        # Raw fields often carry posShortNames/defaultPosId even on bench
        raw = getattr(row, "_raw", {}) or {}
        codes |= SubsService._map_slot_ids_to_codes(
            raw.get("defaultPosId")
            or raw.get("posId")
            or raw.get("posIds")
            or raw.get("posIdsNoFlex"),
            hint=raw.get("posShortNames") or raw.get("posShortName"),
        )

        # Cache from earlier warmers / stats page lookups
        if pid and pid in _ELIG_CACHE:
            codes |= set(_ELIG_CACHE[pid])

        # Player object fallbacks
        pl = getattr(row, "player", None)
        if pl:
            for attr in (
                "position_short",
                "primary_position",
                "default_position",
                "pos_short",
                "display_position",
            ):
                v = (getattr(pl, attr, "") or "").upper()
                if v[:1] in {"G", "D", "M", "F"}:
                    codes.add(v[:1])
            poss = (
                getattr(pl, "positions", None)
                or getattr(pl, "eligible_positions", None)
                or []
            )
            if isinstance(poss, (list, set, tuple)):
                for val in poss:
                    vv = str(val).upper()[:1]
                    if vv in {"G", "D", "M", "F"}:
                        codes.add(vv)

        return {c for c in codes if c in {"G", "D", "M", "F"}}

    # ---- cache helpers -------------------------------------------------
    @staticmethod
    def _map_slot_ids_to_codes(ids, hint: Optional[str] = None) -> set[str]:
        """
        Map Fantrax numeric pos ids (701/702/703/704) and/or a hint string (e.g. 'M/F')
        to a set like {'M','F'}. We now *union* ids with hint (no early-return).
        """
        out: Set[str] = set()
        if hint:
            for tok in (
                str(hint)
                .replace("/", " / ")
                .replace("|", " | ")
                .replace(",", " , ")
                .split()
            ):
                code = SubsService._normalize_pos_token(tok)
                if code:
                    out.add(code)
        if not ids:
            return out
        if isinstance(ids, (str, int, float)):
            ids = [ids]
        for x in ids:
            try:
                c = _ID_TO_CODE.get(int(x))  # expects 701/702/703/704
                if c:
                    out.add(c)
            except Exception:
                pass
        return out

    @staticmethod
    def warm_from_swap_response(payload: Any) -> None:
        try:
            sMap = payload["responses"][0]["data"]["fantasyResponse"]["scorerMap"]
        except Exception:
            return
        for pid, info in sMap.items():
            codes = set()
            if "posShortNames" in info:
                codes |= SubsService._map_slot_ids_to_codes(None, hint=info["posShortNames"])
            for k in ("posIds", "posIdsNoFlex", "defaultPosId"):
                if k in info:
                    codes |= SubsService._map_slot_ids_to_codes(
                        info[k], hint=info.get("posShortNames")
                    )
            if codes:
                _ELIG_CACHE[pid] = codes

    @staticmethod
    def warm_from_fxpa_request(payload: Any) -> None:
        try:
            msgs = payload.get("msgs") or []
            for m in msgs:
                if m.get("method") == "confirmOrExecuteTeamRosterChanges":
                    field_map = (m.get("data") or {}).get("fieldMap") or {}
                    for pid, meta in field_map.items():
                        ids = meta.get("posId")
                        codes = SubsService._map_slot_ids_to_codes(ids)
                        if codes:
                            _ELIG_CACHE[pid] = codes
        except Exception:
            pass

    @staticmethod
    def prime_player_position(
        player_id: str, *, pos_ids=None, pos_short: Optional[str] = None, default_pos_id=None
    ) -> None:
        codes = SubsService._map_slot_ids_to_codes(
            pos_ids or default_pos_id, hint=pos_short
        )
        if codes:
            _ELIG_CACHE[player_id] = codes

    @staticmethod
    def warm_from_player_stats_response(payload: Any) -> None:
        try:
            items = payload["responses"][0]["data"]["statsTable"]
        except Exception:
            return
        for row in (items or []):
            sc = (row or {}).get("scorer") or {}
            pid = sc.get("scorerId")
            codes = SubsService._map_slot_ids_to_codes(
                sc.get("defaultPosId") or sc.get("posIds") or sc.get("posIdsNoFlex"),
                hint=sc.get("posShortNames"),
            )
            if pid and codes:
                _ELIG_CACHE[pid] = codes

    def _fetch_and_cache_pos_from_stats(
        self, league_id: str, *, player_id: str, search_name: str
    ) -> bool:
        url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
        body = {
            "msgs": [
                {
                    "method": "getPlayerStats",
                    "data": {
                        "statusOrTeamFilter": "ALL",
                        "pageNumber": "1",
                        "searchName": search_name,
                    },
                }
            ],
            "uiv": 3,
            "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/players",
            "dt": 0,
            "at": 0,
            "av": "0.0",
        }
        try:
            j = self.session.post(url, json=body, timeout=20).json()
            rows = j["responses"][0]["data"]["statsTable"]
        except Exception:
            return False
        for r in rows or []:
            sc = (r or {}).get("scorer") or {}
            if sc.get("scorerId") == player_id:
                codes = SubsService._map_slot_ids_to_codes(
                    sc.get("defaultPosId") or sc.get("posIds") or sc.get("posIdsNoFlex"),
                    hint=sc.get("posShortNames"),
                )
                if codes:
                    _ELIG_CACHE[player_id] = codes
                    return True
        return False

    def _ensure_codes_for_selection(
        self, league_id: str, roster: Roster, player_ids: List[str]
    ) -> None:
        rmap = self._row_map(roster)
        for pid in player_ids:
            if pid in _ELIG_CACHE:
                continue
            row = rmap.get(pid)
            if not row or not getattr(row, "player", None):
                continue
            raw = getattr(row, "_raw", {}) or {}
            codes = SubsService._map_slot_ids_to_codes(
                raw.get("defaultPosId")
                or raw.get("posId")
                or raw.get("posIds")
                or raw.get("posIdsNoFlex"),
                hint=raw.get("posShortNames") or raw.get("posShortName"),
            )
            if codes:
                _ELIG_CACHE[pid] = codes
                continue
            name = row.player.name or (getattr(row.player, "url_name", "") or "").replace(
                "-", " "
            )
            if name:
                self._fetch_and_cache_pos_from_stats(
                    league_id, player_id=pid, search_name=name
                )

    def _ensure_codes_for_roster(self, league_id: str, roster: Roster) -> None:
        ids = [r.player.id for r in roster.rows if getattr(r, "player", None)]
        self._ensure_codes_for_selection(league_id, roster, ids)

    def warm_codes_for_roster(self, league_id: str, roster: Roster) -> None:
        ids = [r.player.id for r in roster.rows if getattr(r, "player", None)]
        self._ensure_codes_for_selection(league_id, roster, ids)

    # ---------- counts & maps ----------
    def _pos_counts_for_rows(
        self, rows: List[RosterRow], overrides: dict | None = None
    ) -> Formation:
        g = d = m = f = 0
        for r in rows:
            if not getattr(r, "player", None):
                continue
            p = self._pos_of_row(r, overrides)
            if p == "G":
                g += 1
            elif p == "D":
                d += 1
            elif p == "M":
                m += 1
            elif p == "F":
                f += 1
        return Formation(g, d, m, f)

    def _current_starter_ids(self, roster: Roster) -> List[str]:
        return [
            r.player.id
            for r in roster.get_starters()
            if getattr(r, "player", None)
        ]

    def _row_map(self, roster: Roster) -> Dict[str, RosterRow]:
        return {r.player.id: r for r in roster.rows if getattr(r, "player", None)}

    # -------- preflight (swap) --------
    def preflight_swap(
        self, *, league_id: str, team_id: str, starter_player_id: str, bench_player_id: str
    ) -> Dict[str, Any]:
        warnings, errors = [], []
        try:
            room = self.get_roster(league_id, team_id)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch roster: {e}")
        starter = self._find_row_by_id(room, starter_player_id)
        bench = self._find_row_by_id(room, bench_player_id)
        if not starter:
            errors.append("Starter player not found on roster.")
        if not bench:
            errors.append("Bench player not found on roster.")
        if errors:
            return {"ok": False, "warnings": warnings, "errors": errors}
        if getattr(starter, "pos_id", None) == "0":
            warnings.append("Selected 'starter' appears to be on the bench; will auto-correct.")
        if getattr(bench, "pos_id", None) != "0":
            warnings.append("Selected 'bench' appears to be a starter; will auto-correct.")
        if self._row_locked(starter):
            warnings.append("Starter appears locked (swap may defer or be rejected).")
        if self._row_locked(bench):
            warnings.append("Bench player appears locked (swap may defer or be rejected).")
        return {"ok": True, "warnings": warnings, "errors": errors}

    # --------- period helpers (new) ----------
    def _probe_eligible_period(
        self,
        *,
        league_id: str,
        team_id: str,
        fmap: Dict[str, Dict[str, str]],
        start_period: int,
        window: int = 6,
    ) -> Optional[int]:
        """
        Try a short window of CONFIRM calls to find the first period where
        changeAllowed=True and playerPickDeadlinePassed=False. If all probed
        periods are past deadline, return earliest candidate+1 to schedule into.
        """
        best_schedule: Optional[int] = None

        for off in range(max(1, window)):
            p = max(1, int(start_period)) + off

            payload = {
                "msgs": [{
                    "method": "confirmOrExecuteTeamRosterChanges",
                    "data": {
                        "leagueId": league_id,
                        "rosterLimitPeriod": int(p),
                        "fantasyTeamId": team_id,
                        "teamId": team_id,
                        "daily": False,
                        "adminMode": False,
                        "applyToFuturePeriods": False,
                        "fieldMap": fmap,
                        "confirm": True,  # CONFIRM probe (no execute)
                    }
                }],
                "uiv": 3,
                "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster",
                "dt": 0, "at": 0, "av": "0.0",
            }

            j = self._post_fxpa(league_id, payload)
            resp0 = ((j.get("responses") or [{}])[0]) or {}
            data0 = (resp0.get("data") or {})

            # Extract model regardless of nesting style
            model = {}
            if isinstance(data0.get("model"), dict):
                model = data0["model"]
            elif isinstance(data0.get("textArray"), dict) and isinstance(data0["textArray"].get("model"), dict):
                model = data0["textArray"]["model"]
            elif isinstance((data0.get("fantasyResponse") or {}).get("model"), dict):
                model = (data0["fantasyResponse"]["model"])

            change_allowed = bool(model.get("changeAllowed", True))
            deadline_passed = bool(model.get("playerPickDeadlinePassed"))

            if change_allowed and not deadline_passed:
                return p
            if deadline_passed and best_schedule is None:
                best_schedule = p + 1

        return best_schedule

    # -------- action (swap) --------
    def swap_cross_position(
        self,
        team_id: str,
        out_player_id: str,
        in_player_id: str,
        period: int | None = None,
    ) -> dict:
        """
        Cross-position swap: build target XI and execute via fieldMap.
        Uses preflight validation but executes in a single operation (not sequential swaps).

        Returns:
            dict with keys: success (bool), message (str), error (str)
        """
        if not self.league_id:
            raise ValueError("league_id is required")

        api = self._api(self.league_id)
        roster = api.roster_info(team_id)
        current_starters = self._current_starter_ids(roster)

        # Basic sanity checks
        if out_player_id not in current_starters:
            return {
                "success": False,
                "message": "",
                "error": "OUT player must be an active starter for a cross-position swap.",
            }
        if in_player_id in current_starters:
            return {
                "success": False,
                "message": "",
                "error": "IN player is already a starter.",
            }

        # Build target XI: current starters, minus OUT, plus IN
        desired = [pid for pid in current_starters if pid != out_player_id]
        desired.append(in_player_id)

        # De-dupe while preserving order
        seen: set[str] = set()
        desired_starter_ids = [
            x for x in desired if not (x in seen or seen.add(x))
        ]

        log.info(f"[swap:cross-pos] Building target XI: current={len(current_starters)}, desired={len(desired_starter_ids)}")

        # Preflight validation: check formation legality
        pre = self.preflight_set_lineup_by_ids(
            league_id=self.league_id,
            team_id=team_id,
            desired_starter_ids=desired_starter_ids,
            ensure_unlocked=True,
            pos_overrides=None,
        )

        if not pre.get("ok"):
            # Collapse planner errors into a single message
            errs = pre.get("errors") or []
            msg = "; ".join(str(e) for e in errs if e) or "Formation or eligibility invalid for cross-position swap."
            log.error(f"[swap:cross-pos] Preflight failed: {msg}")
            return {
                "success": False,
                "message": "",
                "error": msg,
            }

        formation_str = pre.get("desired_formation", "?-?-?-?")
        log.info(f"[swap:cross-pos] Preflight OK. Desired formation: {formation_str}")

        # Build fieldMap for the target XI (not sequential swaps, just final state)
        field_map = self.build_field_map(roster, desired_starter_ids, pos_overrides=None)
        
        # Determine period and check for deadline
        if period is not None and period > 0:
            roster_period = int(period)
            log.info(f"[swap:cross-pos] Using explicit period: {roster_period}")
        else:
            roster_period, _ = self._sniff_period_and_deadline_from_roster(roster)
            log.info(f"[swap:cross-pos] Using detected period: {roster_period}")

        # Execute with CONFIRM → ACK → FINALIZE flow (like simple swap does)
        log.info(f"[swap:cross-pos] Executing fieldMap with {len(field_map)} players, starters={len(desired_starter_ids)}")
        
        # Step 1: Initial CONFIRM to check for warnings/deadline
        confirm_result = self.confirm_or_execute_lineup(
            league_id=self.league_id,
            fantasy_team_id=team_id,
            roster_limit_period=roster_period,
            field_map=field_map,
            apply_to_future=False,
            do_finalize=False,  # CONFIRM phase
        )
        
        # Check model flags from CONFIRM
        model = confirm_result.get("model", {})
        fantasy_resp = confirm_result.get("fantasyResponse", {})
        msg_type = fantasy_resp.get("msgType") or confirm_result.get("msgType", "")
        show_confirm = fantasy_resp.get("showConfirmWindow", False)
        pick_deadline_passed = model.get("playerPickDeadlinePassed", False)
        first_illegal = model.get("firstIllegalRosterPeriod")
        
        log.info(f"[swap:cross-pos] Confirm phase: msgType={msg_type}, showConfirm={show_confirm}, deadlinePassed={pick_deadline_passed}, firstIllegal={first_illegal}")
        
        # If deadline passed or needs future period, adjust
        apply_to_future = bool(pick_deadline_passed)
        try:
            if first_illegal and int(first_illegal) > 0:
                roster_period = int(first_illegal)
                apply_to_future = True
                log.info(f"[swap:cross-pos] Adjusted to firstIllegalRosterPeriod={roster_period}, apply_to_future=True")
        except (ValueError, TypeError):
            pass
        
        # Step 2: If WARNING or CONFIRM needed, acknowledge it
        if msg_type in ("WARNING", "CONFIRM") or show_confirm:
            log.info(f"[swap:cross-pos] Acknowledging {msg_type} with second CONFIRM")
            confirm_result = self.confirm_or_execute_lineup(
                league_id=self.league_id,
                fantasy_team_id=team_id,
                roster_limit_period=roster_period,
                field_map=field_map,
                apply_to_future=apply_to_future,
                do_finalize=False,  # Still CONFIRM
            )
        
        # Step 3: Final EXECUTE/FINALIZE
        log.info(f"[swap:cross-pos] Finalizing with period={roster_period}, apply_to_future={apply_to_future}")
        result = self.confirm_or_execute_lineup(
            league_id=self.league_id,
            fantasy_team_id=team_id,
            roster_limit_period=roster_period,
            field_map=field_map,
            apply_to_future=apply_to_future,
            do_finalize=True,  # FINALIZE
        )

        # Check final result
        fantasy_resp = result.get("fantasyResponse", {})
        final_model = result.get("model", {})
        main_msg = fantasy_resp.get("mainMsg") or result.get("mainMsg") or ""
        msg_type = fantasy_resp.get("msgType") or result.get("msgType", "")
        lineup_changes = fantasy_resp.get("lineupChanges", [])
        errors = fantasy_resp.get("illegalRosterMsgs") or []
        page_error = result.get("pageError")
        change_allowed = final_model.get("changeAllowed")
        
        log.info(f"[swap:cross-pos] Final result: msgType={msg_type}, mainMsg={main_msg}, lineupChanges={len(lineup_changes)}, errors={len(errors)}, changeAllowed={change_allowed}")
        
        # Success detection:
        # When scheduling for future period (apply_to_future=True), Fantrax returns:
        # - msgType=CONFIRM (not SUCCESS)
        # - lineupChanges=0 (because it's scheduled, not immediately visible)
        # - changeAllowed=True
        # - No errors or error messages
        has_immediate_changes = len(lineup_changes) > 0
        no_errors = page_error is None and not errors
        has_error_msg = main_msg and ("sorry" in main_msg.lower() or "not eligible" in main_msg.lower() or "cannot" in main_msg.lower() or "illegal" in main_msg.lower())
        
        # Success if either:
        # 1. Has immediate lineupChanges (current period change)
        # 2. No errors AND (changeAllowed=True or msgType=CONFIRM) when apply_to_future=True (scheduled change)
        is_scheduled_success = apply_to_future and no_errors and not has_error_msg and (change_allowed or msg_type == "CONFIRM")
        
        if not no_errors or has_error_msg:
            # Definite failure - has errors or error messages
            if errors:
                msg = "; ".join(str(e) for e in errors)
            elif main_msg:
                msg = main_msg
            elif page_error:
                msg = str(page_error)
            else:
                msg = "Fantrax rejected the cross-position swap"
            
            log.error(f"[swap:cross-pos] Execute failed: {msg}")
            return {
                "success": False,
                "message": "",
                "error": msg,
            }
        
        if not has_immediate_changes and not is_scheduled_success:
            # No changes and not a valid scheduled change
            msg = f"No lineup changes detected (msgType={msg_type}, changeAllowed={change_allowed})"
            log.error(f"[swap:cross-pos] Execute failed: {msg}")
            return {
                "success": False,
                "message": "",
                "error": msg,
            }

        # Build success message based on whether it's scheduled or immediate
        if apply_to_future:
            success_msg = f"Cross-position swap scheduled successfully for period {roster_period}. New formation: {formation_str}."
            log.info(f"[swap:cross-pos] Execute succeeded (scheduled). Formation: {formation_str}, period: {roster_period}")
        else:
            success_msg = f"Cross-position swap completed successfully. New formation: {formation_str}."
            log.info(f"[swap:cross-pos] Execute succeeded (immediate). Formation: {formation_str}")
        
        return {
            "success": True,
            "message": success_msg,
            "error": "",
        }

    def swap_players(self, team_id: str, out_player_id: str, in_player_id: str, period: int = None) -> dict:
        """
        Swap two players on a roster.
        - Same-position (G↔G, D↔D, M↔M, F↔F): use simple slot swap (existing logic).
        - Cross-position (D↔M, M↔F, etc.): delegate to full XI planner via swap_cross_position().

        Returns:
            dict with keys: success (bool), message (str), error (str)
        """
        try:
            if not self.league_id:
                raise ValueError("league_id is required")

            log.info(f"[swap] Starting swap: out={out_player_id}, in={in_player_id}, explicit_period={period}")

            # First, get positions to decide which path to take
            roster = self.get_roster(self.league_id, team_id)
            out_row = self._find_row_by_id(roster, out_player_id)
            in_row = self._find_row_by_id(roster, in_player_id)

            if not out_row or not in_row:
                missing = []
                if not out_row:
                    missing.append("OUT player not on roster")
                if not in_row:
                    missing.append("IN player not on roster")
                return {
                    "success": False,
                    "message": "",
                    "error": "; ".join(missing),
                }

            out_bucket = self._pos_of_row(out_row, overrides=None)
            in_bucket = self._pos_of_row(in_row, overrides=None)

            log.info(f"[swap] Position buckets: OUT={out_bucket}, IN={in_bucket}")

            same_bucket = (
                out_bucket in {"G", "D", "M", "F"} and
                in_bucket in {"G", "D", "M", "F"} and
                out_bucket == in_bucket
            )

            known_both = (
                out_bucket in {"G", "D", "M", "F"} and
                in_bucket in {"G", "D", "M", "F"}
            )

            # --- Goalkeeper special rule: GK can only swap with GK ---
            if known_both:
                if (out_bucket == "G" and in_bucket != "G") or (in_bucket == "G" and out_bucket != "G"):
                    out_name = getattr(out_row.player, "name", "Unknown")
                    in_name = getattr(in_row.player, "name", "Unknown")
                    log.error(f"[swap] Rejected GK↔outfield swap: {out_name} ({out_bucket}) ↔ {in_name} ({in_bucket})")
                    return {
                        "success": False,
                        "message": "",
                        "error": (
                            f"Cannot swap goalkeeper with outfield player. "
                            f"Formations must have exactly 1 goalkeeper. "
                            f"Please swap {out_name} ({out_bucket}) with another {out_bucket}, "
                            f"or swap {in_name} ({in_bucket}) with another {in_bucket}."
                        ),
                    }

            # --- Cross-position path ---
            if known_both and not same_bucket:
                log.info("[swap] Detected cross-position swap, delegating to swap_cross_position()")
                return self.swap_cross_position(
                    team_id=team_id,
                    out_player_id=out_player_id,
                    in_player_id=in_player_id,
                    period=period,
                )

            # --- Unknown positions: fail with a clear message ---
            if not known_both:
                return {
                    "success": False,
                    "message": "",
                    "error": (
                        "Cannot determine one or both players' eligible positions. "
                        "Refresh your roster in Fantrax and try again."
                    ),
                }

            # --- Same-position: keep existing simple swap behavior ---
            log.info(f"[swap] Same-position swap detected ({out_bucket}), using simple swap logic")
            confirm_resp = self._confirm_swap(team_id, out_player_id, in_player_id, explicit_period=period)
            log.info(f"[swap] Confirm response: {confirm_resp}")

            execute_resp = self._execute_swap(team_id, out_player_id, in_player_id, explicit_period=period)
            log.info(f"[swap] Execute response: {execute_resp}")

            fantasy_resp = execute_resp.get("fantasyResponse", {})
            main_msg = fantasy_resp.get("mainMsg") or execute_resp.get("mainMsg") or ""
            msg_type = fantasy_resp.get("msgType") or execute_resp.get("msgType", "")
            lineup_changes = fantasy_resp.get("lineupChanges", [])

            has_error_message = (
                "Sorry, you cannot perform that action" in main_msg
                or "not eligible" in main_msg
            )
            needs_correction = (msg_type == "CONFIRM")

            if has_error_message or needs_correction:
                log.error(
                    f"[swap] Execute failed - msgType={msg_type}, "
                    f"lineupChanges={len(lineup_changes)}, error: {main_msg}"
                )
                return {
                    "success": False,
                    "message": "",
                    "error": main_msg or "Swap failed - please check player eligibility and formation rules",
                }

            if not lineup_changes:
                log.warning(f"[swap] No lineupChanges returned. msgType={msg_type}, mainMsg={main_msg}")
                if not main_msg or "success" in main_msg.lower():
                    log.info("[swap] Execute succeeded (no changes listed, but no error)")
                else:
                    return {
                        "success": False,
                        "message": "",
                        "error": main_msg or "No lineup changes applied",
                    }

            log.info(f"[swap] Execute succeeded - {len(lineup_changes)} lineup changes applied")
            return {
                "success": True,
                "message": "Swap completed successfully!",
                "error": "",
            }

        except Exception as e:
            log.exception("[swap] Exception during swap")
            return {
                "success": False,
                "message": "",
                "error": f"Error during swap: {str(e)}",
            }

    def _build_swap_field_map(self, team_id: str, out_id: str, in_id: str) -> dict:
        """
        Build FULL fieldMap for roster with two players swapped.
        
        Fantrax requires:
        1. ALL roster players in the map (not just the two being swapped)
        2. Keys are player IDs (strings like "04tm0")
        3. Values are {"posId": "703", "stId": "1"} with STRING values
        4. stId: "1" = starter, "2" = bench/reserve
        """
        roster = self.get_roster(self.league_id, team_id)
        
        # Build full map first (current state)
        field_map = {}
        out_row = None
        in_row = None
        
        for row in roster.rows:
            if not getattr(row, "player", None) or not row.player.id:
                continue
                
            player_id = row.player.id
            pos_id = str(row.pos_id) if row.pos_id else "0"
            
            # Determine stId: "1" for starters (pos_id != 0), "2" for bench
            if pos_id == "0":
                st_id = "2"
            else:
                st_id = "1"
            
            field_map[player_id] = {
                "posId": pos_id,
                "stId": st_id
            }
            
            # Track the two players we're swapping
            if player_id == out_id:
                out_row = row
            elif player_id == in_id:
                in_row = row
        
        if not out_row or not in_row:
            raise ValueError(f"Could not find both players on roster: out_id={out_id}, in_id={in_id}")
        
        # Now perform the swap: exchange stId values
        out_pos_id = field_map[out_id]["posId"]
        in_pos_id = field_map[in_id]["posId"]
        
        # Swap: OUT player goes to bench (stId="2"), IN player takes OUT's position (stId="1")
        field_map[out_id] = {
            "posId": out_pos_id,  # Keep original position (for when they come back)
            "stId": "2"           # Move to bench
        }
        field_map[in_id] = {
            "posId": out_pos_id,  # Take OUT player's position slot
            "stId": "1"           # Move to starting lineup
        }
        
        log.info(f"[swap] Built full fieldMap with {len(field_map)} players")
        log.info(f"[swap] Swap: OUT={out_row.player.name} (ID={out_id}, pos_id={out_pos_id}) → bench, IN={in_row.player.name} (ID={in_id}, pos_id={in_pos_id}) → lineup(pos_id={out_pos_id})")
        log.debug(f"[swap] Full fieldMap sample (first 3): {dict(list(field_map.items())[:3])}")
        
        return field_map

    def _confirm_swap(self, team_id: str, out_id: str, in_id: str, explicit_period: int = None) -> dict:
        if not self.league_id:
            raise ValueError("league_id is required")
        field_map = self._build_swap_field_map(team_id, out_id, in_id)
        roster = self.get_roster(self.league_id, team_id)
        out_row = self._find_row_by_id(roster, out_id)
        in_row = self._find_row_by_id(roster, in_id)
        locked_any = self._row_locked(out_row) or self._row_locked(in_row)

        # Use explicit period if provided, otherwise let server echo
        period_to_use = int(explicit_period) if explicit_period else 0
        log.info(f"[swap] Confirm call using period={period_to_use}, locked={locked_any}")

        return self.confirm_or_execute_lineup(
            league_id=self.league_id,
            fantasy_team_id=team_id,
            roster_limit_period=period_to_use,
            field_map=field_map,
            apply_to_future=bool(locked_any),
            do_finalize=False,
        )

    def _execute_swap(self, team_id: str, out_id: str, in_id: str, explicit_period: int = None) -> dict:
        """
        Enhanced execute:
        - Preflight with period=0 to read model flags.
        - Choose a concrete period (server echo, or currentPeriod, or probed).
        - If FIN says locked/no-change and we weren't scheduling, retry as scheduled and advance periods.
        - If explicit_period is provided, use it directly instead of auto-detection.
        """
        if not self.league_id:
            raise ValueError("league_id is required")

        league_id = self.league_id
        field_map = self._build_swap_field_map(team_id, out_id, in_id)

        # --- Preflight (read model flags, possible period echo)
        pre = self.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=team_id,
            roster_limit_period=0,          # probe/echo
            field_map=field_map,
            apply_to_future=False,
            do_finalize=False,
        )

        fr = pre.get("fantasyResponse") or {}
        model = pre.get("model") or {}

        # Extract period & flags
        rai = (model.get("rosterAdjustmentInfo") or {})
        try:
            server_period = int(rai.get("rosterLimitPeriod")) if rai.get("rosterLimitPeriod") is not None else None
        except Exception:
            server_period = None

        change_allowed = model.get("changeAllowed")
        deadline_passed = bool(model.get("playerPickDeadlinePassed"))
        first_illegal = None
        try:
            first_illegal = int(model.get("firstIllegalRosterPeriod")) if model.get("firstIllegalRosterPeriod") is not None else None
        except Exception:
            first_illegal = None

        pre_main = (fr.get("mainMsg") or "").lower()
        pre_says_locked = ("locked" in pre_main) or (change_allowed is False) or deadline_passed
        
        # NEW: If the UI would show a confirm window, ACK it with a second CONFIRM
        if fr.get("showConfirmWindow") or fr.get("msgType") == "WARNING":
            pre = self.confirm_or_execute_lineup(
                league_id=league_id,
                fantasy_team_id=team_id,
                roster_limit_period=int(server_period or 0),
                field_map=field_map,
                apply_to_future=True,    # clicking “OK” in UI implies future=True when locked
                do_finalize=False,
            )
            fr = pre.get("fantasyResponse") or {}
            model = pre.get("model") or {}
            rai = (model.get("rosterAdjustmentInfo") or {})
            try:
                server_period = int(rai.get("rosterLimitPeriod")) if rai.get("rosterLimitPeriod") is not None else server_period
            except Exception:
                pass
            change_allowed = model.get("changeAllowed")
            deadline_passed = bool(model.get("playerPickDeadlinePassed"))

        # Also derive a robust currentPeriod via fxpa
        fxpa_current = None
        try:
            fxpa_current = int(self._current_period_via_fxpa(league_id) or 0)
        except Exception:
            fxpa_current = None

        # Optional: peek from roster too (can carry displayedPeriod/deadline)
        try:
            roster_now = self.get_roster(league_id, team_id)
            rp, dl = self._sniff_period_and_deadline_from_roster(roster_now)
            roster_period = int(rp) if rp else None
            roster_deadline = bool(dl)
        except Exception:
            roster_period = None
            roster_deadline = None

        # Decide if we must schedule
        # Decide if we must schedule
        apply_to_future = bool(deadline_passed or (change_allowed is False) or pre_says_locked)

        # If user explicitly set period, use it directly
        if explicit_period is not None and explicit_period > 0:
            fin_period = int(explicit_period)
            log.info("[lineup] Using explicit user-provided period: %s", fin_period)
        # Choose period: prefer server echo; then resolved current; then roster; then +1
        elif apply_to_future:
            if isinstance(first_illegal, int) and first_illegal > 0:
                fin_period = first_illegal
            else:
                seed = (self._current_period_via_fxpa(league_id) or 0) + 1
                probed = self._probe_eligible_period(
                    league_id=league_id, team_id=team_id, fmap=field_map,
                    start_period=max(seed, (server_period or 0) + 1), window=6
                )
                fin_period = int(probed or max(seed, (server_period or 1) + 1))
        else:
            fin_period = int(server_period or self._current_period_via_fxpa(league_id) or roster_period or 1)


        log.info("[lineup] choose period: server=%s fxpa=%s roster=%s firstIllegal=%s -> chosen=%s apply_to_future=%s",
                server_period, fxpa_current, roster_period, first_illegal, fin_period, apply_to_future)

        # --- Finalize attempt
        fin = self.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=team_id,
            roster_limit_period=int(fin_period or 0),
            field_map=field_map,
            apply_to_future=apply_to_future,
            do_finalize=True,
        )

        # If locked/no-op and we weren't scheduling, retry as scheduled and advance a few periods
        fin_fr = fin.get("fantasyResponse") or {}
        fin_msg = (fin_fr.get("mainMsg") or fin.get("mainMsg") or "").lower()
        if (not bool(fin.get("ok"))) and (("locked" in fin_msg) or ("no changes detected" in fin_msg)) and (not apply_to_future):
            # schedule next
            if isinstance(first_illegal, int) and first_illegal > 0:
                sched_period = int(first_illegal)
            else:
                seed = (fxpa_current or 0) + 1 if fxpa_current else ( (roster_period or 0) + 1 if roster_period else 1 )
                probed = self._probe_eligible_period(
                    league_id=league_id,
                    team_id=team_id,
                    fmap=field_map,
                    start_period=seed,
                    window=6
                )
                sched_period = int(probed or seed)

            log.info("[lineup] retry as scheduled: period=%s (apply_to_future=True)", sched_period)
            fin = self.confirm_or_execute_lineup(
                league_id=league_id,
                fantasy_team_id=team_id,
                roster_limit_period=sched_period,
                field_map=field_map,
                apply_to_future=True,
                do_finalize=True,
            )

            # --------- NEW: advance if still blocked/ambiguous ---------
            fin_fr_retry = fin.get("fantasyResponse") or {}
            fin_model_retry = fin.get("model") or {}
            fin_msg_type_retry = (fin_fr_retry.get("msgType") or fin.get("msgType"))
            fin_msg_retry = (fin_fr_retry.get("mainMsg") or fin.get("mainMsg") or "").lower()

            lockedish = (
                ("locked" in fin_msg_retry)
                or bool(fin_model_retry.get("playerPickDeadlinePassed"))
                or (fin_model_retry.get("changeAllowed") is False)
                or (fin_msg_type_retry in (None, ""))
            )

            if (not bool(fin.get("ok"))) and lockedish:
                base = int(sched_period or ((fxpa_current or 0) + 1))
                for extra in range(1, 6):
                    try_p = base + extra
                    log.info("[lineup] advance retry -> period=%s (scheduled)", try_p)
                    fin2 = self.confirm_or_execute_lineup(
                        league_id=league_id,
                        fantasy_team_id=team_id,
                        roster_limit_period=try_p,
                        field_map=field_map,
                        apply_to_future=True,
                        do_finalize=True,
                    )
                    if bool(fin2.get("ok")):
                        fin = fin2
                        break

                    fr2 = fin2.get("fantasyResponse") or {}
                    model2 = fin2.get("model") or {}
                    msg2 = (fr2.get("mainMsg") or fin2.get("mainMsg") or "").lower()
                    msg_type2 = (fr2.get("msgType") or fin2.get("msgType"))
                    still_lockedish = (
                        ("locked" in msg2)
                        or bool(model2.get("playerPickDeadlinePassed"))
                        or (model2.get("changeAllowed") is False)
                        or (msg_type2 in (None, ""))
                    )
                    fin = fin2
                    if not still_lockedish:
                        break
            # -----------------------------------------------------------

        return fin

    # ---------- Validation (full XI) ----------
    def set_lineup_by_ids(
        self,
        *,
        league_id: str,
        team_id: str,
        desired_starter_ids: List[str],
        best_effort: bool = True,
        verify_each: bool = True,
        pos_overrides: Optional[Dict[str, str]] = None,
        server_confirm: bool = True,           # kept for signature compatibility (not used)
        apply_to_future: bool = False,
        roster_limit_period: Optional[int] = None,
        fantasy_team_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Apply XI as sequential server-confirm swaps (bring IN, then bench OUT).
        - Uses current period from roster (displayedSelections.displayedPeriod).
        - If deadline has passed, sets applyToFuturePeriods=True automatically.
        - On confirm WARNING, re-confirms to acknowledge, then executes.
        - If model.firstIllegalRosterPeriod is provided, auto-retargets that period.

        Returns a summary dict with 'ok', 'warnings', 'errors', 'results', etc.
        """
        # --- Plan swaps locally
        pre = self.preflight_set_lineup_by_ids(
            league_id=league_id,
            team_id=team_id,
            desired_starter_ids=desired_starter_ids,
            ensure_unlocked=True,
            pos_overrides=pos_overrides,
        )
        if not pre["ok"] and not best_effort:
            return pre | {"results": [], "warnings": pre.get("warnings", []), "errors": pre.get("errors", [])}

        api = self._api(league_id)
        ftid = fantasy_team_id or team_id

        # Determine period + future flag from roster (unless caller forces period)
        if roster_limit_period is not None:
            period = int(roster_limit_period)
            apply_to_future = bool(apply_to_future)
        else:
            roster_for_period = api.roster_info(team_id)
            period, deadline_passed = self._sniff_period_and_deadline_from_roster(roster_for_period)
            apply_to_future = bool(apply_to_future or deadline_passed)

        import time, random

        results: List[Dict[str, Any]] = []
        warnings: List[str] = []
        errors: List[str] = []

        def _confirm_ack_then_execute(fmap, per, future) -> tuple[Dict[str, Any], Dict[str, Any], int, bool]:
            """
            Confirm (ack WARNING if needed) then execute.
            May adjust period/future based on model flags (firstIllegalRosterPeriod).
            Returns: (pre_response, final_response, period_used, future_used)
            """
            nonlocal period, apply_to_future

            # Initial confirm
            pre_resp = self.confirm_or_execute_lineup(
                league_id=league_id, fantasy_team_id=ftid,
                roster_limit_period=per, field_map=fmap,
                apply_to_future=future, do_finalize=False,
            )

            # If WARNING (12 starters etc.), acknowledge with a 2nd confirm
            fr0 = pre_resp.get("fantasyResponse") or {}
            if (fr0.get("msgType") == "WARNING") or fr0.get("showConfirmWindow"):
                pre_resp = self.confirm_or_execute_lineup(
                    league_id=league_id, fantasy_team_id=ftid,
                    roster_limit_period=per, field_map=fmap,
                    apply_to_future=future, do_finalize=False,
                )

            # If not allowed for this period, retarget to firstIllegalRosterPeriod and flip future
            model0 = (pre_resp.get("model") or {})
            change_allowed = bool(model0.get("changeAllowed", True))
            pick_deadline_passed = bool(model0.get("playerPickDeadlinePassed"))
            first_illegal_period = model0.get("firstIllegalRosterPeriod")

            per_used = per
            future_used = future

            if (not change_allowed or pick_deadline_passed) and first_illegal_period is not None:
                try:
                    per_used = int(first_illegal_period)
                    future_used = True
                    # Re-confirm (ack if needed) for the new period
                    pre_resp = self.confirm_or_execute_lineup(
                        league_id=league_id, fantasy_team_id=ftid,
                        roster_limit_period=per_used, field_map=fmap,
                        apply_to_future=future_used, do_finalize=False,
                    )
                    fr1 = pre_resp.get("fantasyResponse") or {}
                    if (fr1.get("msgType") == "WARNING") or fr1.get("showConfirmWindow"):
                        pre_resp = self.confirm_or_execute_lineup(
                            league_id=league_id, fantasy_team_id=ftid,
                            roster_limit_period=per_used, field_map=fmap,
                            apply_to_future=future_used, do_finalize=False,
                        )
                except Exception:
                    pass

            fin_resp = self.confirm_or_execute_lineup(
                league_id=league_id, fantasy_team_id=ftid,
                roster_limit_period=per_used, field_map=fmap,
                apply_to_future=future_used, do_finalize=True,
            )
            return pre_resp, fin_resp, per_used, future_used

        # --- Per-swap loop
        for (out_id, in_id) in pre.get("plan", []):
            try:
                # Fresh snapshot
                roster_now = api.roster_info(team_id)
                current_starters = set(
                    r.player.id for r in roster_now.get_starters() if getattr(r, "player", None)
                )

                # -------- Phase A: Promote 'in' (12 actives expected) --------
                starters_A = set(current_starters)
                starters_A.add(in_id)
                fmap_A = self.build_field_map(roster_now, list(starters_A), pos_overrides)

                pre_A, fin_A, period, apply_to_future = _confirm_ack_then_execute(fmap_A, period, apply_to_future)

                ok_A = bool(fin_A.get("ok"))
                if not ok_A:
                    err_A: List[str] = []
                    if fin_A.get("mainMsg"):
                        err_A.append(str(fin_A["mainMsg"]))
                    for m in (fin_A.get("illegalMsgs") or []):
                        err_A.append(str(m))
                    if not err_A:
                        err_A.append("Phase A (promote) failed.")
                    results.append({
                        "out": out_id, "in": in_id, "phase": "A",
                        "ok": False, "verified": None,
                        "precheck": pre_A, "finalize": fin_A,
                        "error": "; ".join(err_A),
                    })
                    errors.extend(err_A)
                    if not best_effort:
                        break
                    # Skip Phase B; continue with next swap
                    time.sleep(0.5)
                    continue

                # -------- Phase B: Demote 'out' (back to 11) --------
                time.sleep(0.35 + random.random() * 0.3)
                roster_mid = api.roster_info(team_id)
                current_after_A = set(
                    r.player.id for r in roster_mid.get_starters() if getattr(r, "player", None)
                )
                starters_B = set(current_after_A)
                if out_id in starters_B:
                    starters_B.remove(out_id)
                starters_B.add(in_id)  # ensure 'in' remains a starter
                fmap_B = self.build_field_map(roster_mid, list(starters_B), pos_overrides)

                pre_B, fin_B, period, apply_to_future = _confirm_ack_then_execute(fmap_B, period, apply_to_future)

                ok_B = bool(fin_B.get("ok"))
                verified = None

                if ok_B and verify_each:
                    try:
                        time.sleep(0.35)
                        roster_after = api.roster_info(team_id)
                        cur_ids = set(
                            r.player.id for r in roster_after.get_starters() if getattr(r, "player", None)
                        )
                        verified = (in_id in cur_ids) and (out_id not in cur_ids)
                        if not verified:
                            ok_B = False
                    except Exception as ve:
                        warnings.append(f"Swap applied but verify failed: {ve}")

                err_B: List[str] = []
                if not ok_B:
                    if fin_B.get("mainMsg"):
                        err_B.append(str(fin_B["mainMsg"]))
                    for m in (fin_B.get("illegalMsgs") or []):
                        err_B.append(str(m))
                    if verified is False and not err_B:
                        err_B.append("Finalize reported OK but roster did not reflect the swap.")

                results.append({
                    "out": out_id, "in": in_id, "phase": "A+B",
                    "ok": ok_B, "verified": verified,
                    "precheck": {"A": pre_A, "B": pre_B},
                    "finalize": {"A": fin_A, "B": fin_B},
                    "error": ("; ".join(err_B) if (not ok_B and err_B) else None),
                })

                if not ok_B and not best_effort:
                    break

                time.sleep(0.45 + random.random() * 0.35)

            except Exception as e:
                results.append({
                    "out": out_id, "in": in_id, "phase": "exception",
                    "ok": False, "verified": None,
                    "precheck": None, "finalize": None,
                    "error": str(e),
                })
                errors.append(str(e))
                if not best_effort:
                    break

        # --- Final summary/snapshot
        try:
            final_roster = api.roster_info(team_id)
            selected_rows = [
                r for r in final_roster.rows
                if getattr(r, "player", None) and r.player.id in set(desired_starter_ids)
            ]
            desired_counts = self._pos_counts_for_rows(selected_rows, pos_overrides)
            final_starters = [
                r.player.id for r in final_roster.get_starters() if getattr(r, "player", None)
            ]
            formation_str = f"{desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f}"
        except Exception as e:
            final_starters = []
            formation_str = "0-0-0-0"
            warnings.append(f"Final roster fetch failed: {e}")

        all_ok = bool(results) and all(r.get("ok") for r in results if isinstance(r, dict))
        errs = [r["error"] for r in results if isinstance(r, dict) and r.get("error")]

        return {
            "ok": all_ok,
            "warnings": warnings,
            "errors": [e for e in errs if e],
            "plan": pre.get("plan", []),
            "plan_human": pre.get("plan_human", []),
            "current_starters": final_starters,
            "desired_starters": desired_starter_ids,
            "desired_formation": formation_str,
            "results": results,
        }

    def preflight_set_lineup_by_ids(
        self,
        *,
        league_id: str,
        team_id: str,
        desired_starter_ids: List[str],
        ensure_unlocked: bool = True,
        pos_overrides: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        warnings: List[str] = []
        errors: List[str] = []

        seen = set()
        desired_starter_ids = [
            x for x in desired_starter_ids if not (x in seen or seen.add(x))
        ]

        roster = self.get_roster(league_id, team_id)
        row_map = self._row_map(roster)

        if len(desired_starter_ids) != 11:
            errors.append(f"Exactly 11 starters required; got {len(desired_starter_ids)}.")
        not_on_roster = [pid for pid in desired_starter_ids if pid not in row_map]
        if not_on_roster:
            errors.append(f"{len(not_on_roster)} selected not on roster.")
        if errors:
            return {
                "ok": False,
                "warnings": warnings,
                "errors": errors,
                "plan": [],
                "plan_human": [],
                "current_starters": [],
                "desired_starters": desired_starter_ids,
            }

        self._ensure_codes_for_selection(league_id, roster, desired_starter_ids)

        selected_rows = [row_map[pid] for pid in desired_starter_ids]
        desired_counts = self._pos_counts_for_rows(selected_rows, pos_overrides)

        def _legal_relaxed(c: Formation) -> bool:
            total = c.gk + c.d + c.m + c.f
            return total == 11 and 0 <= c.gk <= 1 and 3 <= c.d <= 5 and 2 <= c.m <= 5 and 1 <= c.f <= 3

        if not _legal_relaxed(desired_counts):
            errors.append(
                f"Invalid formation {desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f} "
                "(needs GK 0–1, D 3–5, M 2–5, F 1–3; 11 total)."
            )
        if desired_counts.gk > 1:
            errors.append("You can have at most 1 GK.")

        if errors:
            return {
                "ok": False,
                "warnings": warnings,
                "errors": errors,
                "plan": [],
                "plan_human": [],
                "current_starters": [],
                "desired_starters": desired_starter_ids,
                "desired_formation": f"{desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f}",
            }

        plan = self._plan_swaps(
            roster,
            desired_starter_ids,
            ensure_unlocked=ensure_unlocked,
            warnings=warnings,
            errors=errors,
            pos_overrides=pos_overrides,
        )
        current_ids = self._current_starter_ids(roster)
        return {
            "ok": not errors,
            "warnings": warnings,
            "errors": errors,
            "plan": plan,
            "plan_human": self._plan_to_human(roster, plan),
            "current_starters": current_ids,
            "desired_starters": desired_starter_ids,
            "desired_formation": f"{desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f}",
        }

    def _plan_to_human(self, roster: Roster, plan: List[tuple]) -> List[Dict[str, str]]:
        row_map = self._row_map(roster)
        out = []
        for out_id, in_id in plan:
            o = row_map.get(out_id)
            i = row_map.get(in_id)
            on = getattr(getattr(o, "player", None), "name", "?")
            inn = getattr(getattr(i, "player", None), "name", "?")
            os = (getattr(getattr(o, "pos", None), "short_name", "") or "BN")
            is_ = (getattr(getattr(i, "pos", None), "short_name", "") or "BN")
            out.append(
                {
                    "out_id": out_id,
                    "out_name": on,
                    "out_slot": os,
                    "in_id": in_id,
                    "in_name": inn,
                    "in_slot": is_,
                }
            )
        return out

    # ---------- Planning ----------
    def _plan_swaps(
        self,
        roster: Roster,
        desired_starter_ids: List[str],
        *,
        ensure_unlocked: bool,
        warnings: List[str],
        errors: List[str],
        pos_overrides: Optional[Dict[str, str]] = None,
    ) -> List[tuple]:
        row_map = self._row_map(roster)
        current_starters = set(self._current_starter_ids(roster))
        desired_set = set(desired_starter_ids)
        to_add = [pid for pid in desired_starter_ids if pid not in current_starters]
        to_remove = [pid for pid in current_starters if pid not in desired_set]
        if not to_add and not to_remove:
            return []

        def _movable(pid: str) -> bool:
            return True if not ensure_unlocked else not self._row_locked(row_map.get(pid))

        def _pos(pid: str) -> str:
            return self._pos_of_row(row_map[pid], pos_overrides)

        add_by_pos = {"G": [], "D": [], "M": [], "F": []}
        rem_by_pos = {"G": [], "D": [], "M": [], "F": []}
        for pid in to_add:
            add_by_pos[_pos(pid)].append(pid)
        for pid in to_remove:
            rem_by_pos[_pos(pid)].append(pid)

        plan: List[tuple] = []

        # (1) same-position swaps
        for p in ("G", "D", "M", "F"):
            while add_by_pos[p] and rem_by_pos[p]:
                inn = add_by_pos[p].pop()
                out = rem_by_pos[p].pop()
                if not (_movable(inn) and _movable(out)):
                    warnings.append(
                        f"Skipped swap {row_map.get(out).player.name} ↔ {row_map.get(inn).player.name} due to lock."
                    )
                    continue
                plan.append((out, inn))

        def _surplus(c: Formation, t: Formation) -> List[str]:
            return [p for p, diff in (("D", c.d - t.d), ("M", c.m - t.m), ("F", c.f - t.f)) if diff > 0]

        def _deficit(c: Formation, t: Formation) -> List[str]:
            return [p for p, diff in (("D", t.d - c.d), ("M", t.m - c.m), ("F", t.f - c.f)) if diff > 0]

        cur = self._pos_counts_for_rows([row_map[pid] for pid in current_starters], pos_overrides)
        target = self._pos_counts_for_rows([row_map[pid] for pid in desired_starter_ids], pos_overrides)

        # GK balancing (works if target.gk == 0 or 1)
        if cur.gk != target.gk:
            if cur.gk > target.gk and rem_by_pos["G"]:
                def_pos = _deficit(cur, target)
                picked_in = next((add_by_pos[p].pop() for p in def_pos if add_by_pos[p]), None)
                if picked_in:
                    out = rem_by_pos["G"].pop()
                    if _movable(picked_in) and _movable(out):
                        plan.append((out, picked_in))
            elif cur.gk < target.gk and add_by_pos["G"] and (rem_by_pos["D"] or rem_by_pos["M"] or rem_by_pos["F"]):
                surplus = _surplus(cur, target) or ["D", "M", "F"]
                picked_out = next((rem_by_pos[p].pop() for p in surplus if rem_by_pos[p]), None)
                if picked_out:
                    inn = add_by_pos["G"].pop()
                    if _movable(inn) and _movable(picked_out):
                        plan.append((picked_out, inn))

        # Outfield balancing loop
        safety = 100
        while safety > 0 and (cur.d != target.d or cur.m != target.m or cur.f != target.f):
            safety -= 1
            sur = _surplus(cur, target)
            defc = _deficit(cur, target)
            if not sur or not defc:
                break
            took = False
            for p_out in sur:
                if not rem_by_pos[p_out]:
                    continue
                for p_in in defc:
                    if not add_by_pos[p_in]:
                        continue
                    out = rem_by_pos[p_out][-1]
                    inn = add_by_pos[p_in][-1]
                    if not (_movable(out) and _movable(inn)):
                        rem_by_pos[p_out].pop()
                        add_by_pos[p_in].pop()
                        continue
                    rem_by_pos[p_out].pop()
                    add_by_pos[p_in].pop()
                    plan.append((out, inn))
                    took = True
                    break
                if took:
                    break

        # (3) cleanup
        for p in ("D", "M", "F", "G"):
            while add_by_pos[p] and rem_by_pos[p]:
                inn = add_by_pos[p].pop()
                out = rem_by_pos[p].pop()
                if not (_movable(inn) and _movable(out)):
                    warnings.append(
                        f"Skipped final same-pos swap due to locks: {row_map.get(out).player.name} ↔ {row_map.get(inn).player.name}"
                    )
                    continue
                plan.append((out, inn))

        return plan

    def apply_lineup_fieldmap(
        self,
        *,
        league_id: str,
        team_id: str,
        desired_starter_ids: List[str],
        pos_overrides: Optional[Dict[str, str]] = None,
        accept_warnings: bool = True,
    ) -> Dict[str, Any]:
        api_url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
        roster = self.get_roster(league_id, team_id)
        row_map = self._row_map(roster)

        code_to_id = {"F": 701, "M": 702, "D": 703, "G": 704}
        field_map: Dict[str, Dict[str, int]] = {}

        for pid in desired_starter_ids:
            code = pos_overrides.get(pid) if pos_overrides and pid in pos_overrides else self._pos_of_row(row_map[pid])
            pos_id = code_to_id.get(code)
            if pos_id is None:
                raise RuntimeError(f"Cannot determine posId for {pid} ({code})")
            field_map[pid] = {"posId": pos_id}

        for r in roster.rows:
            if not getattr(r, "player", None):
                continue
            pid = r.player.id
            if pid in field_map:
                continue
            field_map[pid] = {"posId": 0}

        msg = {
            "method": "confirmOrExecuteTeamRosterChanges",
            "data": {
                "teamId": team_id,
                "fieldMap": field_map,
                "acceptWarnings": bool(accept_warnings),
                "action": "EXECUTE",
            },
        }
        body = {
            "msgs": [msg],
            "uiv": 3,
            "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/lineup",
            "dt": 0,
            "at": 0,
            "av": "0.0",
        }

        log.info(f"[subs] fieldMap size={len(field_map)} starters={len(desired_starter_ids)}")
        resp = self.session.post(api_url, json=body, timeout=30)
        try:
            j = resp.json()
        except Exception:
            j = {"http_status": resp.status_code, "text": resp.text[:800]}

        ok = False
        page_error = None
        try:
            if isinstance(j, dict) and j.get("responses"):
                r0 = j["responses"][0].get("data") or {}
                page_error = r0.get("pageError")
                tx = (r0.get("txResponses") or [None])[0] or {}
                code = (tx.get("code") or "").lower()
                ok = (not page_error) and code.startswith("ok")
        except Exception:
            pass

        log.info(f"[subs] fieldMap apply ok={ok} page_error={page_error} raw={j}")
        return {"ok": bool(ok), "raw": j, "page_error": page_error}

    # ---------- Execute (full XI) ----------
    def get_current_period(self, league_id: str) -> Optional[int]:
        try:
            return self._api(league_id).drops.get_current_period()
        except Exception:
            return None

    def build_field_map(
        self,
        roster,
        desired_starter_ids: list[str],
        pos_overrides: Optional[Dict[str, str]] = None
    ) -> Dict[str, Dict[str, str]]:
        """
        Build the FULL fieldMap required by Fantrax confirm/execute.

        Returns:
            { scorerId: {"posId": "701|702|703|704", "stId": "1|2"} } for *every* player on the roster.
        """
        pos_overrides = pos_overrides or {}
        want = set(desired_starter_ids)
        fmap: Dict[str, Dict[str, str]] = {}

        for r in roster.rows:
            if not getattr(r, "player", None):
                continue

            pid = r.player.id

            # Determine the bucket code 'G'/'D'/'M'/'F'
            code = pos_overrides.get(pid)
            if not code:
                code = self._pos_of_row(r, pos_overrides)

            # Map to numeric posId; fall back to eligibility if needed
            pos_id = _CODE_TO_ID.get(code)
            if not pos_id:
                elig = self.eligible_positions_of_row(r)
                code2 = next(iter(elig)) if elig else None
                pos_id = _CODE_TO_ID.get(code2 or "")

            if not pos_id:
                # Final fallback (rare). Skip row rather than send junk.
                # The server will preserve existing state for skipped rows,
                # but we strive to send a complete map whenever possible.
                continue

            if pid in want:
                fmap[pid] = {"posId": str(pos_id), "stId": "1"}
            else:
                fmap[pid] = {"posId": "0", "stId": "2"}

        return fmap

    def _post_fxpa(self, league_id: str, body: dict) -> dict:
        """
        Low-level POST to /fxpa/req with:
        - Accept + X-Requested-With headers like the browser
        - Root-level client hints 'tz' and 'v' (UI version) if available
        - Robust JSON parse with graceful fallback
        """
        # Inject browser-like root fields if present on the session
        tz = getattr(self, "session", None) and self.session.headers.get("X-TZ")
        ui_ver = getattr(self, "session", None) and self.session.headers.get("X-Fantrax-UI-Version")

        payload = dict(body) if isinstance(body, dict) else {"msgs": body}
        if tz and "tz" not in payload:
            payload["tz"] = tz
        if ui_ver and "v" not in payload:
            payload["v"] = ui_ver

        # Always include uiv/refUrl scaffolding to resemble UI calls
        payload.setdefault("uiv", 3)
        payload.setdefault("refUrl", f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster")
        payload.setdefault("dt", 0)
        payload.setdefault("at", 0)
        payload.setdefault("av", "0.0")

        # Headers like the SPA does
        headers = {
            "Accept": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            res = self.session.post(
                "https://www.fantrax.com/fxpa/req",
                params={"leagueId": league_id},
                json=payload,
                timeout=25,
                headers=headers,
            )
            try:
                j = res.json()
            except Exception:
                # Graceful fallback on HTML or error pages
                j = {"http_status": res.status_code, "text": (res.text or "")[:1000]}
            return j
        except Exception as e:
            return {"http_error": str(e)}

    def _current_period_via_fxpa(self, league_id: str) -> int:
        body = {
            "msgs": [{"method": "getStandings", "data": {"leagueId": league_id, "view": "SCHEDULE"}}],
            "uiv": 3,
            "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/standings",
            "dt": 0,
            "at": 0,
            "av": "0.0",
        }
        j = self._post_fxpa(league_id, body)
        resp0 = (j.get("responses") or [{}])[0]
        data = (resp0.get("data") or {})
        # Parse from several possible locations, treating period=1 as a special case
        cands = []
        # Primary: direct currentPeriod
        try:
            p = int(data.get("currentPeriod"))
            if p > 1:  # Ignore period=1 as it's often a fallback
                return p
            if p == 1:
                cands.append(p)
        except Exception:
            pass

        # Secondary: scheduleInfo.currentPeriod
        try:
            p = int(((data.get("scheduleInfo") or {}).get("currentPeriod")))
            if p > 1:
                return p
            if p == 1:
                cands.append(p)
        except Exception:
            pass

        # Tertiary: fantasyResponse.currentPeriod
        try:
            p = int(((resp0.get("fantasyResponse") or {}).get("currentPeriod")))
            if p > 1:
                return p
            if p == 1:
                cands.append(p)
        except Exception:
            pass

        # Fallback: tableList[0].caption like "Week 4"
        try:
            cap = str(((data.get("tableList") or [{}])[0] or {}).get("caption") or "").strip()
            parts = cap.split()
            if parts and parts[-1].isdigit():
                p = int(parts[-1])
                if p > 1:
                    return p
                if p == 1:
                    cands.append(p)
        except Exception:
            pass

        # If we only found period=1 candidates, treat as unresolved (0)
        # This will trigger the direct SCHEDULE fallback in the app
        return 0 if all(p == 1 for p in cands) else (cands[0] if cands else 0)

    def _normalize_field_map(self, field_map: dict) -> dict:
        submit = {}
        for pid, meta in (field_map or {}).items():
            if not meta:
                continue
            pos = meta.get("posId")
            st = meta.get("stId")
            if pos is None or st is None:
                continue
            submit[str(pid)] = {"posId": int(pos), "stId": "1" if str(st) == "1" else "2"}
        return submit

    def confirm_or_execute_lineup(
        self,
        *,
        league_id: str,
        fantasy_team_id: str,
        roster_limit_period: int,
        field_map: dict,
        apply_to_future: bool,
        do_finalize: bool,
    ) -> dict:
        """
        One-shot CONFIRM or FINALIZE for lineup changes.

        do_finalize=False  -> CONFIRM (server returns model + CONFIRM window shape)
        do_finalize=True   -> EXECUTE/FINALIZE (server applies the change)

        Returns a flattened dict with keys:
        - ok: bool (True if no pageError)
        - fantasyResponse: dict
        - model: dict
        - mainMsg: str | None
        - msgType: str | None
        - pageError: dict | None
        - targetPeriod: int | None
        """
        data = {
            "leagueId": league_id,  # keep explicit for strict pods
            "rosterLimitPeriod": int(roster_limit_period),
            "fantasyTeamId": fantasy_team_id,
            "teamId": fantasy_team_id,
            "daily": False,
            "adminMode": False,
            "applyToFuturePeriods": bool(apply_to_future),
            "fieldMap": field_map,
        }
        if not do_finalize:
            data["confirm"] = True  # CONFIRM phase (browser sends this on first click)

        body = {"msgs": [{"method": "confirmOrExecuteTeamRosterChanges", "data": data}]}
        # NEW: add a page-ish refUrl mirroring the SPA (with ;period=...)
        body["refUrl"] = f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster;period={int(roster_limit_period or 0)}"

        j = self._post_fxpa(league_id, body)

        # Flatten typical shapes
        resp0 = ((j or {}).get("responses") or [{}])[0]
        data0 = (resp0.get("data") or {})
        fr = (data0.get("fantasyResponse") or {})
        ta = (data0.get("textArray") or {})
        model = (ta.get("model") or j.get("model") or {})
        page_error = j.get("pageError") or resp0.get("pageError") or None

        # Warm position eligibility cache opportunistically
        try:
            self.warm_from_fxpa_request(body)
            self.warm_from_swap_response(j)
        except Exception:
            pass

        # Signal for callers
        ok = page_error is None  # treat absence of pageError as success indicator

        out = {
            "ok": bool(ok),
            "fantasyResponse": fr,
            "model": model,
            "mainMsg": fr.get("mainMsg") or j.get("mainMsg"),
            "msgType": fr.get("msgType"),
            "pageError": page_error,
            "targetPeriod": (
                j.get("targetPeriod")
                or ((model.get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod"))
            ),
        }

        log.info(
            "[lineup] finalize=%s type=%s confirmWindow=%s illegal=%s",
            do_finalize,
            out["msgType"],
            fr.get("showConfirmWindow"),
            len(fr.get("illegalRosterMsgs") or []),
        )
        log.info(
            "[lineup] changeAllowed=%s firstIllegalPeriod=%s pickDeadlinePassed=%s",
            model.get("changeAllowed") if isinstance(model, dict) else None,
            (model.get("firstIllegalRosterPeriod") if isinstance(model, dict) else None),
            (model.get("playerPickDeadlinePassed") if isinstance(model, dict) else None),
        )
        return out

    def _sniff_period_and_deadline_from_roster(self, roster) -> tuple[int, bool]:
        """
        Find the current displayed period + pick-deadline flag from the roster payload.
        Falls back safely if missing or partially present.

        Returns:
            (period:int, deadline_passed:bool)
        """
        period = None
        deadline = None

        for r in getattr(roster, "rows", []):
            raw = getattr(r, "_raw", {}) or {}
            ctx = raw.get("context") or raw

            disp = (ctx.get("displayedSelections") or {})
            if period is None:
                try:
                    period = int(disp.get("displayedPeriod"))
                except Exception:
                    pass

            model = (ctx.get("model") or {})
            if deadline is None:
                try:
                    deadline = bool(model.get("playerPickDeadlinePassed"))
                except Exception:
                    pass

            if period is not None and deadline is not None:
                break

        if period is None:
            # Safe fallback if roster doesn't carry the flag
            try:
                period = int(self.get_current_period(self.league_id) or 3)
            except Exception:
                period = 3

        return int(period), bool(deadline)


# Optional convenience for UI code:
def eligible_positions_of_row(row) -> set[str]:
    return SubsService.eligible_positions_of_row(row)
