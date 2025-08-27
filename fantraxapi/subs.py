# /Users/hogan/FantraxAPI/fantraxapi/subs.py
from __future__ import annotations

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
_CODE_TO_ID = {v: k for k, v in _ID_TO_CODE.items()}	   # ints

# player_id (scorerId) -> {'G','D','M','F'}
_ELIG_CACHE: Dict[str, Set[str]] = {}

# One canonical Formation model (top-level, used everywhere)
@dataclass(frozen=True)
class Formation:
	gk: int
	d: int
	m: int
	f: int

	def is_legal(self) -> bool:
		# Exactly 11, 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD
		total = self.gk + self.d + self.m + self.f
		return (
			total == 11 and
			self.gk == 1 and
			3 <= self.d <= 5 and
			2 <= self.m <= 5 and
			1 <= self.f <= 3
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
		flags = [raw.get("isLocked"), raw.get("locked"), raw.get("lineupLocked")]
		if any(bool(x) for x in flags if x is not None):
			return True
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
		if sn in {"G","D","M","F"} and getattr(row, "pos_id", None) != "0":
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
		if t in {"G", "GK", "GKP", "GOALKEEPER"}: return "G"
		if t in {"D", "DEF", "DEFENDER", "WB"}:	 return "D"
		if t in {"M", "MID", "MIDFIELDER", "CM", "DM", "AM", "W"}: return "M"
		if t in {"F", "FW", "FWD", "STRIKER", "ST"}: return "F"
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
		for tok in [x for d in [",", "/", "|", " "] for x in s.replace("/", " / ").replace("|", " | ").split(d)]:
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
		if sn in {"G","D","M","F"} and getattr(row, "pos_id", None) != "0":
			codes.add(sn)

		# Raw fields often carry posShortNames/defaultPosId even on bench
		raw = getattr(row, "_raw", {}) or {}
		codes |= SubsService._map_slot_ids_to_codes(
			raw.get("defaultPosId") or raw.get("posId") or raw.get("posIds") or raw.get("posIdsNoFlex"),
			hint=raw.get("posShortNames") or raw.get("posShortName")
		)

		# Cache from earlier warmers / stats page lookups
		if pid and pid in _ELIG_CACHE:
			codes |= set(_ELIG_CACHE[pid])

		# Player object fallbacks
		pl = getattr(row, "player", None)
		if pl:
			for attr in ("position_short", "primary_position", "default_position", "pos_short", "display_position"):
				v = (getattr(pl, attr, "") or "").upper()
				if v[:1] in {"G","D","M","F"}:
					codes.add(v[:1])
			poss = (getattr(pl, "positions", None) or getattr(pl, "eligible_positions", None) or [])
			if isinstance(poss, (list, set, tuple)):
				for val in poss:
					vv = str(val).upper()[:1]
					if vv in {"G","D","M","F"}:
						codes.add(vv)

		return {c for c in codes if c in {"G","D","M","F"}}
	# ---- cache helpers -------------------------------------------------
	@staticmethod
	def _map_slot_ids_to_codes(ids, hint: Optional[str] = None) -> set[str]:
		"""
		Map Fantrax numeric pos ids (701/702/703/704) and/or a hint string (e.g. 'M/F')
		to a set like {'M','F'}. We now *union* ids with hint (no early-return).
		"""
		out: Set[str] = set()
		if hint:
			for tok in str(hint).replace("/", " / ").replace("|", " | ").replace(",", " , ").split():
				code = SubsService._normalize_pos_token(tok)
				if code:
					out.add(code)
		if not ids:
			return out
		if isinstance(ids, (str, int, float)):
			ids = [ids]
		for x in ids:
			try:
				c = _ID_TO_CODE.get(int(x))	 # expects {701:'F',702:'M',703:'D',704:'G'}
				if c:
					out.add(c)
			except Exception:
				pass
		return out

	@staticmethod
	def warm_from_swap_response(payload: Any) -> None:
		"""
		Takes the swap/lineup response you posted earlier (with fantasyResponse.scorerMap)
		and caches eligibilities.
		"""
		try:
			sMap = payload["responses"][0]["data"]["fantasyResponse"]["scorerMap"]
		except Exception:
			return
		for pid, info in sMap.items():
			codes = set()
			# posShortNames: e.g., 'F' or 'M/F'
			if "posShortNames" in info:
				codes |= SubsService._map_slot_ids_to_codes(None, hint=info["posShortNames"])
			# posIds / posIdsNoFlex / defaultPosId
			for k in ("posIds", "posIdsNoFlex", "defaultPosId"):
				if k in info:
					codes |= SubsService._map_slot_ids_to_codes(info[k], hint=info.get("posShortNames"))
			if codes:
				_ELIG_CACHE[pid] = codes

	@staticmethod
	def warm_from_fxpa_request(payload: Any) -> None:
		"""
		Accepts the exact request JSON you pasted for confirmOrExecuteTeamRosterChanges.
		It learns player -> posId from data.fieldMap and caches codes.
		"""
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
	def prime_player_position(player_id: str, *, pos_ids=None, pos_short: Optional[str]=None, default_pos_id=None) -> None:
		"""
		Manual priming hook if you fetch a player page elsewhere.
		"""
		codes = SubsService._map_slot_ids_to_codes(pos_ids or default_pos_id, hint=pos_short)
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
				
	def _fetch_and_cache_pos_from_stats(self, league_id: str, *, player_id: str, search_name: str) -> bool:
		url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
		body = {
			"msgs": [{"method": "getPlayerStats", "data": {
				"statusOrTeamFilter": "ALL",
				"pageNumber": "1",
				"searchName": search_name,
			}}],"uiv": 3,"refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/players","dt": 0,"at": 0,"av": "0.0"
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
	def _ensure_codes_for_selection(self, league_id: str, roster: Roster, player_ids: List[str]) -> None:
		"""
		Ensure every selected player has eligibility cached (bench included).
		"""
		rmap = self._row_map(roster)
		for pid in player_ids:
			if pid in _ELIG_CACHE:
				continue
			row = rmap.get(pid)
			if not row or not getattr(row, "player", None):
				continue
			raw = getattr(row, "_raw", {}) or {}
			codes = SubsService._map_slot_ids_to_codes(
				raw.get("defaultPosId") or raw.get("posId") or raw.get("posIds") or raw.get("posIdsNoFlex"),
				hint=raw.get("posShortNames") or raw.get("posShortName"),
			)
			if codes:
				_ELIG_CACHE[pid] = codes
				continue
			name = row.player.name or (getattr(row.player, "url_name", "") or "").replace("-", " ")
			if name:
				self._fetch_and_cache_pos_from_stats(league_id, player_id=pid, search_name=name)
				
	def _ensure_codes_for_roster(self, league_id: str, roster: Roster) -> None:
		"""
		Warm eligibilities for *all* rows so dropdowns can include bench players.
		"""
		ids = [r.player.id for r in roster.rows if getattr(r, "player", None)]
		self._ensure_codes_for_selection(league_id, roster, ids)

	def warm_codes_for_roster(self, league_id: str, roster: Roster) -> None:
		"""
		Warm eligibilities for *all* rows so dropdowns can include bench players.
		"""
		ids = [r.player.id for r in roster.rows if getattr(r, "player", None)]
		self._ensure_codes_for_selection(league_id, roster, ids)

	# ---------- counts & maps ----------
	def _pos_counts_for_ids(self, roster: Roster, player_ids: List[str]) -> Formation:
		g = d = m = f = 0
		idset = set(player_ids)
		for r in roster.rows:
			if getattr(r, "player", None) and r.player.id in idset and r.pos_id != "0":
				p = self._pos_of_row(r)
				if p == "G": g += 1
				elif p == "D": d += 1
				elif p == "M": m += 1
				elif p == "F": f += 1
		return Formation(g, d, m, f)

	def _pos_counts_for_rows(self, rows: List[RosterRow], overrides: dict | None = None) -> Formation:
		g = d = m = f = 0
		for r in rows:
			if not getattr(r, "player", None):
				continue
			p = self._pos_of_row(r, overrides)
			if p == "G": g += 1
			elif p == "D": d += 1
			elif p == "M": m += 1
			elif p == "F": f += 1
		return Formation(g, d, m, f)
	
	def _current_starter_ids(self, roster: Roster) -> List[str]:
		return [r.player.id for r in roster.get_starters() if getattr(r, "player", None)]

	def _bench_ids(self, roster: Roster) -> List[str]:
		return [r.player.id for r in roster.get_bench_players() if getattr(r, "player", None)]

	def _row_map(self, roster: Roster) -> Dict[str, RosterRow]:
		return {r.player.id: r for r in roster.rows if getattr(r, "player", None)}

	# -------- preflight (swap) --------
	def preflight_swap(self, *, league_id: str, team_id: str, starter_player_id: str, bench_player_id: str) -> Dict[str, Any]:
		warnings, errors = [], []
		try:
			room = self.get_roster(league_id, team_id)
		except Exception as e:
			raise RuntimeError(f"Failed to fetch roster: {e}")
		starter = self._find_row_by_id(room, starter_player_id)
		bench = self._find_row_by_id(room, bench_player_id)
		if not starter: errors.append("Starter player not found on roster.")
		if not bench: errors.append("Bench player not found on roster.")
		if errors: return {"ok": False, "warnings": warnings, "errors": errors}
		if getattr(starter, "pos_id", None) == "0": warnings.append("Selected 'starter' appears to be on the bench; will auto-correct.")
		if getattr(bench, "pos_id", None) != "0": warnings.append("Selected 'bench' appears to be a starter; will auto-correct.")
		if self._row_locked(starter): warnings.append("Starter appears locked (swap may defer or be rejected).")
		if self._row_locked(bench): warnings.append("Bench player appears locked (swap may defer or be rejected).")
		return {"ok": True, "warnings": warnings, "errors": errors}

	# -------- action (swap) --------
	def swap_players(self, team_id: str, out_player_id: str, in_player_id: str) -> bool:
		"""Simple 1-for-1 player swap."""
		try:
			log.info(f"[swap] Starting swap: out={out_player_id}, in={in_player_id}")
			
			# Step 1: Confirm swap
			confirm_resp = self._confirm_swap(team_id, out_player_id, in_player_id)
			log.info(f"[swap] Confirm response: {confirm_resp}")
			if not confirm_resp.get('ok'):
				log.error(f"[swap] Confirm failed: {confirm_resp}")
				return False
				
			# Step 2: Execute swap
			execute_resp = self._execute_swap(team_id, out_player_id, in_player_id)
			log.info(f"[swap] Execute response: {execute_resp}")
			success = execute_resp.get('ok', False)
			if not success:
				log.error(f"[swap] Execute failed: {execute_resp}")
			return success
		except Exception as e:
			log.exception("[swap] Exception during swap")
			return False

	def _build_swap_field_map(self, team_id: str, out_id: str, in_id: str) -> dict:
		"""Build field map for a swap operation."""
		# Get current roster to get position IDs
		roster = self.get_roster(self.league_id, team_id)
		
		# Find the rows for both players
		out_row = None
		in_row = None
		for row in roster.rows:
			if getattr(row, "player", None):
				if row.player.id == out_id:
					out_row = row
				elif row.player.id == in_id:
					in_row = row
		
		if not out_row or not in_row:
			raise ValueError("Could not find both players on roster")
		
		# Keep the same posId but swap stId
		return {
			out_id: {"posId": int(out_row.pos_id), "stId": "2"},	# Move to bench
			in_id: {"posId": int(out_row.pos_id), "stId": "1"}	# Move to active with out_row's position
		}

	def _confirm_swap(self, team_id: str, out_id: str, in_id: str) -> dict:
		"""Confirm a single player swap."""
		if not self.league_id:
			raise ValueError("league_id is required")
		
		field_map = self._build_swap_field_map(team_id, out_id, in_id)
		return self.confirm_or_execute_lineup(
			league_id=self.league_id,
			fantasy_team_id=team_id,
			roster_limit_period=0,  # Current period
			apply_to_future=False,
			do_finalize=False,  # Confirm mode
			field_map=field_map
		)

	def _execute_swap(self, team_id: str, out_id: str, in_id: str) -> dict:
		"""Execute a confirmed player swap."""
		if not self.league_id:
			raise ValueError("league_id is required")
		
		field_map = self._build_swap_field_map(team_id, out_id, in_id)
		return self.confirm_or_execute_lineup(
			league_id=self.league_id,
			fantasy_team_id=team_id,
			roster_limit_period=0,  # Current period
			apply_to_future=False,
			do_finalize=True,  # Execute mode
			field_map=field_map
		)

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
		server_confirm: bool = True,
		apply_to_future: bool = False,
		roster_limit_period: Optional[int] = None,
		fantasy_team_id: Optional[str] = None,
	) -> Dict[str, Any]:
		"""
		Apply XI as sequential server-confirm swaps (no legacy swap_players fallback).
		For each planned (out_id, in_id):
			1) Promote 'in' to starters (temporary 12 actives) → preflight(confirm=True) → execute(confirm omitted)
			2) Demote 'out' to bench (back to 11)	   → preflight(confirm=True) → execute(confirm omitted)
		Auto-retarget earliest legal period if playerPickDeadlinePassed/changeAllowed=False.
		Verify after each swap by refetching roster.
		"""
		# Plan changes locally
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

		def _apply_two_step(field_map, future, *, do_finalize: bool):
			return self.confirm_or_execute_lineup(
				league_id=league_id,
				fantasy_team_id=ftid,
				roster_limit_period=0,	 # <-- always 0
				field_map=field_map,
				apply_to_future=future,
				do_finalize=do_finalize,
			)

		import time, random
		results: List[Dict[str, Any]] = []
		warnings: List[str] = []
		errors: List[str] = []

		for (out_id, in_id) in pre["plan"]:
			try:
				# Fresh snapshot before each swap
				roster_now = api.roster_info(team_id)
				current_starters = set(self._current_starter_ids(roster_now))

				# ---- Phase A: Promote 'in' (12 actives) ----
				starters_A = set(current_starters)
				starters_A.add(in_id)
				fmap_A = self.build_field_map(roster_now, list(starters_A), pos_overrides)

				pre_A  = _apply_two_step(fmap_A, apply_to_future, do_finalize=False)

				# If deadline passed / not allowed, retarget future period
				model_A = pre_A.get("model") or {}
				change_allowed = bool(model_A.get("changeAllowed", True))
				pick_deadline_passed = bool(model_A.get("playerPickDeadlinePassed"))
				first_illegal_period = model_A.get("firstIllegalRosterPeriod")

				auto_future_A = False

				fin_A  = _apply_two_step(fmap_A, apply_to_future, do_finalize=True)

				ok_A = bool(fin_A.get("ok"))

				err_A: List[str] = []
				if not ok_A:
					if fin_A.get("mainMsg"): err_A.append(str(fin_A["mainMsg"]))
					for m in (fin_A.get("illegalMsgs") or []): err_A.append(str(m))
					if not err_A: err_A.append("Phase A (promote) failed.")
					results.append({"out": out_id, "in": in_id, "phase": "A", "ok": False, "verified": None, "precheck": pre_A, "finalize": fin_A, "error": "; ".join(err_A)})
					errors.extend(err_A)
					if not best_effort:
						break
					# Skip Phase B if A failed; continue to next swap
					time.sleep(0.6 + random.random() * 0.4)
					continue

				# ---- Phase B: Demote 'out' (back to 11) ----
				# Refresh snapshot to build an accurate full fieldMap
				time.sleep(0.3)
				roster_mid = api.roster_info(team_id)
				current_after_A = set(self._current_starter_ids(roster_mid))
				starters_B = set(current_after_A)
				if out_id in starters_B:
					starters_B.remove(out_id)
				# Ensure 'in' remains starter
				starters_B.add(in_id)

				fmap_B = self.build_field_map(roster_mid, list(starters_B), pos_overrides)

				pre_B  = _apply_two_step(fmap_B, apply_to_future, do_finalize=False)

				model_B = (pre_B.get("model") or {})
				change_allowed_B = bool(model_B.get("changeAllowed", True))
				pick_deadline_passed_B = bool(model_B.get("playerPickDeadlinePassed"))
				first_illegal_period_B = model_B.get("firstIllegalRosterPeriod")

				auto_future_B = False

				fin_B  = _apply_two_step(fmap_B, apply_to_future, do_finalize=True)

				ok_B = bool(fin_B.get("ok"))

				err_B: List[str] = []
				if not ok_B:
					if fin_B.get("mainMsg"): err_B.append(str(fin_B["mainMsg"]))
					for m in (fin_B.get("illegalMsgs") or []): err_B.append(str(m))
					if not err_B: err_B.append("Phase B (demote) failed.")

				# Verify final state of this swap
				verified = None
				if ok_B and verify_each:
					try:
						time.sleep(0.4)
						roster_after = api.roster_info(team_id)
						cur_ids = set(self._current_starter_ids(roster_after))
						verified = (in_id in cur_ids) and (out_id not in cur_ids)
						if not verified:
							ok_B = False
							err_B.append("Finalize reported OK but roster did not reflect the swap.")
					except Exception as ve:
						warnings.append(f"Swap applied but verify failed: {ve}")

				server_period = ((pre_B.get("model") or {}).get("rosterAdjustmentInfo") or {}).get("rosterLimitPeriod")
				if server_period is not None:
					warnings.append(f"Server scheduled change to period {server_period}.")  # info only

				results.append({
					"out": out_id,
					"in": in_id,
					"phase": "A+B",
					"ok": ok_B,
					"verified": verified,
					"precheck": {"A": pre_A, "B": pre_B},
					"finalize": {"A": fin_A, "B": fin_B},
					"error": ("; ".join(err_B) if (not ok_B and err_B) else None),
				})

				if not ok_B and not best_effort:
					break

				# polite throttle
				time.sleep(0.6 + random.random() * 0.4)

			except Exception as e:
				results.append({"out": out_id, "in": in_id, "phase": "exception", "ok": False, "verified": None, "precheck": None, "finalize": None, "error": str(e)})
				errors.append(str(e))
				if not best_effort:
					break

		# Final summary
		try:
			final_roster = api.roster_info(team_id)
			selected_rows = [r for r in final_roster.rows if getattr(r, "player", None) and r.player.id in set(desired_starter_ids)]
			desired_counts = self._pos_counts_for_rows(selected_rows, pos_overrides)
			final_starters = self._current_starter_ids(final_roster)
		except Exception as e:
			final_starters = []
			desired_counts = Formation(0, 0, 0, 0)
			warnings.append(f"Final roster fetch failed: {e}")

		all_ok = bool(results) and all(r["ok"] for r in results if isinstance(r, dict))
		errs = [r["error"] for r in results if isinstance(r, dict) and r.get("error")]

		return {
			"ok": all_ok,
			"warnings": warnings,
			"errors": [e for e in errs if e],
			"plan": pre.get("plan", []),
			"plan_human": pre.get("plan_human", []),
			"current_starters": final_starters,
			"desired_starters": desired_starter_ids,
			"desired_formation": f"{desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f}",
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
		desired_starter_ids = [x for x in desired_starter_ids if not (x in seen or seen.add(x))]

		roster = self.get_roster(league_id, team_id)
		row_map = self._row_map(roster)

		if len(desired_starter_ids) != 11:
			errors.append(f"Exactly 11 starters required; got {len(desired_starter_ids)}.")
		not_on_roster = [pid for pid in desired_starter_ids if pid not in row_map]
		if not_on_roster:
			errors.append(f"{len(not_on_roster)} selected not on roster.")
		if errors:
			return {"ok": False, "warnings": warnings, "errors": errors, "plan": [], "plan_human": [], "current_starters": [], "desired_starters": desired_starter_ids}

		self._ensure_codes_for_selection(league_id, roster, desired_starter_ids)

		selected_rows = [row_map[pid] for pid in desired_starter_ids]
		desired_counts = self._pos_counts_for_rows(selected_rows, pos_overrides)

		def _legal_relaxed(c: Formation) -> bool:
			total = c.gk + c.d + c.m + c.f
			return (total == 11 and 0 <= c.gk <= 1 and 3 <= c.d <= 5 and 2 <= c.m <= 5 and 1 <= c.f <= 3)

		if not _legal_relaxed(desired_counts):
			errors.append(
				f"Invalid formation {desired_counts.gk}-{desired_counts.d}-{desired_counts.m}-{desired_counts.f} "
				"(needs GK 0–1, D 3–5, M 2–5, F 1–3; 11 total)."
			)

		if desired_counts.gk > 1:
			errors.append("You can have at most 1 GK.")

		if errors:
			return {
				"ok": False, "warnings": warnings, "errors": errors, "plan": [], "plan_human": [],
				"current_starters": [], "desired_starters": desired_starter_ids,
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
			o = row_map.get(out_id); i = row_map.get(in_id)
			on = getattr(getattr(o, "player", None), "name", "?")
			inn = getattr(getattr(i, "player", None), "name", "?")
			os = (getattr(getattr(o, "pos", None), "short_name", "") or "BN")
			is_ = (getattr(getattr(i, "pos", None), "short_name", "") or "BN")
			out.append({
				"out_id": out_id, "out_name": on, "out_slot": os,
				"in_id": in_id, "in_name": inn, "in_slot": is_,
			})
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

		cur = self._pos_counts_for_rows([row_map[pid] for pid in current_starters], pos_overrides)
		target = self._pos_counts_for_rows([row_map[pid] for pid in desired_starter_ids], pos_overrides)

		def _pos(pid: str) -> str:
			return self._pos_of_row(row_map[pid], pos_overrides)

		add_by_pos = {"G": [], "D": [], "M": [], "F": []}
		rem_by_pos = {"G": [], "D": [], "M": [], "F": []}
		for pid in to_add: add_by_pos[_pos(pid)].append(pid)
		for pid in to_remove: rem_by_pos[_pos(pid)].append(pid)

		plan: List[tuple] = []

		# (1) same-position swaps
		for p in ("G", "D", "M", "F"):
			while add_by_pos[p] and rem_by_pos[p]:
				inn = add_by_pos[p].pop()
				out = rem_by_pos[p].pop()
				if not (_movable(inn) and _movable(out)):
					warnings.append(f"Skipped swap {row_map.get(out).player.name} ↔ {row_map.get(inn).player.name} due to lock.")
					continue
				plan.append((out, inn))

		def _surplus(c: Formation, t: Formation) -> List[str]:
			return ([p for p, diff in (("D", c.d - t.d), ("M", c.m - t.m), ("F", c.f - t.f)) if diff > 0])

		def _deficit(c: Formation, t: Formation) -> List[str]:
			return ([p for p, diff in (("D", t.d - c.d), ("M", t.m - c.m), ("F", t.f - c.f)) if diff > 0])

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
					rem_by_pos[p_out].pop(); add_by_pos[p_in].pop()
					plan.append((out, inn))
					took = True
					break
				if took:
					break

		# (3) cleanup
		for p in ("D", "M", "F", "G"):
			while add_by_pos[p] and rem_by_pos[p]:
				inn = add_by_pos[p].pop(); out = rem_by_pos[p].pop()
				if not (_movable(inn) and _movable(out)):
					warnings.append(f"Skipped final same-pos swap due to locks: {row_map.get(out).player.name} ↔ {row_map.get(inn).player.name}")
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
		"""
		Apply XI using Fantrax 'confirmOrExecuteTeamRosterChanges' with fieldMap.
		pos_overrides: {scorerId: 'G'|'D'|'M'|'F'} for chosen bucket; others on roster go to bench (0).
		"""
		api_url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
		roster = self.get_roster(league_id, team_id)
		row_map = self._row_map(roster)

		# Build fieldMap scorerId -> posId (0 bench, 701 F, 702 M, 703 D, 704 G)
		code_to_id = {"F": 701, "M": 702, "D": 703, "G": 704}
		want = set(desired_starter_ids)
		field_map: Dict[str, Dict[str, int]] = {}

		# starters with chosen buckets
		for pid in desired_starter_ids:
			code = None
			if pos_overrides and pid in pos_overrides:
				code = pos_overrides[pid]
			else:
				# derive from eligibility / current slot
				code = self._pos_of_row(row_map[pid])
			pos_id = code_to_id.get(code, None)
			if pos_id is None:
				raise RuntimeError(f"Cannot determine posId for {pid} ({code})")
			field_map[pid] = {"posId": pos_id}

		# everyone else on bench
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
				"action": "EXECUTE"
			}
		}
		body = {"msgs": [msg], "uiv": 3, "refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/lineup", "dt": 0, "at": 0, "av": "0.0"}

		log.info(f"[subs] fieldMap size={len(field_map)} starters={len(desired_starter_ids)}")
		resp = self.session.post(api_url, json=body, timeout=30)
		try:
			j = resp.json()
		except Exception:
			j = {"http_status": resp.status_code, "text": resp.text[:800]}

		# Try to pick out confirm/tx responses
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
		"""
		Server-reported current 'period' (aka gameweek). Safe fallback if not present in roster payloads.
		"""
		try:
			return self._api(league_id).drops.get_current_period()
		except Exception:
			return None

	def build_field_map(self, roster: Roster, desired_starter_ids: List[str], pos_overrides: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, int | str]]:
		"""
		Build the FULL fieldMap required by Fantrax confirm/execute.
		Keys: scorerId -> {"posId":701|702|703|704, "stId":"1"|"2"}
		- Starters (in desired_starter_ids) get stId="1", everyone else "2".
		- posId derived from current/effective G/D/M/F (honoring overrides when provided).
		"""
		pos_overrides = pos_overrides or {}
		want = set(desired_starter_ids)
		fmap: Dict[str, Dict[str, int | str]] = {}

		for r in roster.rows:
			if not getattr(r, "player", None):
				continue
			pid = r.player.id
			# Figure the code for this row
			code = pos_overrides.get(pid) if pid in pos_overrides else self._pos_of_row(r, pos_overrides)
			pos_id = _CODE_TO_ID.get(code)
			if not pos_id:
				elig = SubsService.eligible_positions_of_row(r)
				code2 = next(iter(elig)) if elig else None
				pos_id = _CODE_TO_ID.get(code2 or "")
			if not pos_id:
				# If unresolved, skip; server will keep current state for this row
				continue
			is_starter = pid in want
			if is_starter:
				# starter: real slot id (701/702/703/704)
				fmap[pid] = {"posId": int(pos_id), "stId": "1"}
			else:
				# bench: ALWAYS 0
				fmap[pid] = {"posId": 0, "stId": "2"}
		return fmap

	def _post_fxpa(self, league_id: str, body: dict) -> dict:
		url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
		try:
			res = self.session.post(url, json=body, timeout=30)
			return res.json()
		except Exception as e:
			return {"error": str(e)}

	def confirm_or_execute_lineup(
		self,
		*,
		league_id: str,
		fantasy_team_id: str,
		roster_limit_period: int,
		field_map: Dict[str, Dict[str, int]],
		apply_to_future: bool,
		do_finalize: bool,
	) -> Dict[str, Any]:
		# --- request payload ---
		data_common = {
			"rosterLimitPeriod": int(roster_limit_period),
			"fantasyTeamId": fantasy_team_id,
			"teamId": fantasy_team_id,
			"daily": False,
			"adminMode": False,
			"applyToFuturePeriods": bool(apply_to_future),
			"fieldMap": field_map,
		}
		if not do_finalize:
			data = dict(data_common)
			data["confirm"] = True
			data["action"] = "CONFIRM"	   # <-- important
		else:
			data = dict(data_common)
			data["acceptWarnings"] = True
			data["action"] = "EXECUTE"	   # <-- important

		body = {
			"msgs": [{"method": "confirmOrExecuteTeamRosterChanges", "data": data}],
			"uiv": 3,
			"refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster",
			"dt": 0, "at": 0, "av": "0.0",
		}

		j = self._post_fxpa(league_id, body)

		# --- parse both shapes: fantasyResponse + txResponses ---
		resp0 = (j.get("responses") or [{}])[0] if isinstance(j, dict) else {}
		data_blob = resp0.get("data") or {}
		page_error = resp0.get("pageError") or data_blob.get("pageError")

		fr = data_blob.get("fantasyResponse") or {}
		ta = data_blob.get("textArray") or {}
		model = (ta.get("model") or {}) if isinstance(ta, dict) else {}

		tx = (data_blob.get("txResponses") or [])
		tx_ok = False
		tx_code = None
		tx_msg  = None
		if tx:
			t0 = tx[0] or {}
			tx_code = (t0.get("code") or "").upper()
			status  = (t0.get("status") or "").upper()
			tx_msg  = t0.get("message")
			# e.g. OK_SUCCESS / SUCCEEDED / SUCCESS / OK
			tx_ok = tx_code.startswith("OK") or status in {"SUCCEEDED", "SUCCESS", "OK"}

		msg_type = fr.get("msgType")
		show_confirm = bool(fr.get("showConfirmWindow"))
		illegal = list(fr.get("illegalRosterMsgs") or [])
		main_msg = fr.get("mainMsg") or tx_msg

		# Success rules:
		# - PRE: CONFIRM/WARNING/SUCCESS OR tx_ok
		# - FIN: SUCCESS/CONFIRM/WARNING OR tx_ok
		if do_finalize:
			ok_flag = (msg_type in ("SUCCESS", "CONFIRM", "WARNING")) or tx_ok
		else:
			ok_flag = (msg_type in ("CONFIRM", "WARNING", "SUCCESS")) or tx_ok

		if page_error:
			ok_flag = False

		log.info(
			"[lineup] finalize=%s type=%s confirmWindow=%s illegal=%s",
			do_finalize, msg_type, show_confirm, illegal or None
		)
		log.info(
			"[lineup] changeAllowed=%s firstIllegalPeriod=%s pickDeadlinePassed=%s",
			model.get("changeAllowed"),
			model.get("firstIllegalRosterPeriod"),
			model.get("playerPickDeadlinePassed"),
		)

		# Helpful dump if FR missing and no txResponses
		if msg_type is None and not tx:
			log.info("[lineup] raw response (truncated): %s", str(j)[:800])

		return {
			"ok": bool(ok_flag),
			"fantasyResponse": fr,
			"model": model,
			"illegalMsgs": illegal,
			"mainMsg": main_msg,
			"raw": j,
		}

	
# Optional convenience for UI code:
def eligible_positions_of_row(row) -> set[str]:
	return SubsService.eligible_positions_of_row(row)
