# fantraxapi/subs.py
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote

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

	# -------- action (swap) --------
	def swap_players(self, team_id: str, out_player_id: str, in_player_id: str) -> bool:
		try:
			log.info(f"[swap] Starting swap: out={out_player_id}, in={in_player_id}")
			confirm_resp = self._confirm_swap(team_id, out_player_id, in_player_id)
			log.info(f"[swap] Confirm response: {confirm_resp}")
			if not confirm_resp.get("ok"):
				log.error(f"[swap] Confirm failed: {confirm_resp}")
				return False
			execute_resp = self._execute_swap(team_id, out_player_id, in_player_id)
			log.info(f"[swap] Execute response: {execute_resp}")
			success = execute_resp.get("ok", False)
			if not success:
				log.error(f"[swap] Execute failed: {execute_resp}")
			return success
		except Exception:
			log.exception("[swap] Exception during swap")
			return False

	def _build_swap_field_map(self, team_id: str, out_id: str, in_id: str) -> dict:
		roster = self.get_roster(self.league_id, team_id)
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
		return {
			out_id: {"posId": int(out_row.pos_id), "stId": "2"},
			in_id: {"posId": int(out_row.pos_id), "stId": "1"},
		}

	def _confirm_swap(self, team_id: str, out_id: str, in_id: str) -> dict:
		if not self.league_id:
			raise ValueError("league_id is required")
		field_map = self._build_swap_field_map(team_id, out_id, in_id)
		roster = self.get_roster(self.league_id, team_id)
		out_row = self._find_row_by_id(roster, out_id)
		in_row = self._find_row_by_id(roster, in_id)
		locked_any = self._row_locked(out_row) or self._row_locked(in_row)

		return self.confirm_or_execute_lineup(
			league_id=self.league_id,
			fantasy_team_id=team_id,
			roster_limit_period=0,
			field_map=field_map,
			apply_to_future=bool(locked_any),
			do_finalize=False,
		)

	def _execute_swap(self, team_id: str, out_id: str, in_id: str) -> dict:
		if not self.league_id:
			raise ValueError("league_id is required")
		field_map = self._build_swap_field_map(team_id, out_id, in_id)
		roster = self.get_roster(self.league_id, team_id)
		out_row = self._find_row_by_id(roster, out_id)
		in_row = self._find_row_by_id(roster, in_id)
		locked_any = self._row_locked(out_row) or self._row_locked(in_row)

		return self.confirm_or_execute_lineup(
			league_id=self.league_id,
			fantasy_team_id=team_id,
			roster_limit_period=0,
			field_map=field_map,
			apply_to_future=bool(locked_any),
			do_finalize=True,
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

			st_id = "1" if pid in want else "2"
			fmap[pid] = {"posId": str(int(pos_id)), "stId": str(st_id)}

		return fmap


	def _post_fxpa(self, league_id: str, body: dict) -> dict:
		url = f"https://www.fantrax.com/fxpa/req?leagueId={league_id}"
		api_log = logging.getLogger("auth_api")
		api_log.debug("[fxpa] request: url=%s body=%s", url, json.dumps(body, ensure_ascii=False)[:4000])
		try:
			res = self.session.post(url, json=body, timeout=30)
			j = res.json()
			api_log.debug("[fxpa] response: %s", json.dumps(j, ensure_ascii=False)[:4000])
			return j
		except Exception as e:
			api_log.error("[fxpa] error: %s", str(e))
			return {"error": str(e)}

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
		try:
			return int(data.get("currentPeriod") or 0)
		except Exception:
			return 0

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
		field_map: Dict[str, Dict[str, str]],
		apply_to_future: bool,
		do_finalize: bool,
	) -> Dict[str, Any]:
		"""
		Fantrax two-step flow for lineup changes:
		- do_finalize=False → preflight (send confirm=True)
		- do_finalize=True  → execute (omit 'confirm' key; same fieldMap)

		Robustly parses alternate response shapes and treats execute msgType=CONFIRM as success.
		"""

		data_common = {
			"rosterLimitPeriod": int(roster_limit_period),
			"fantasyTeamId": fantasy_team_id,
			"teamId": fantasy_team_id,
			"daily": False,
			"adminMode": False,
			"applyToFuturePeriods": bool(apply_to_future),
			"fieldMap": field_map,  # FULL map: all players with string posId and stId "1"/"2"
		}

		if not do_finalize:
			data = dict(data_common)
			data["confirm"] = True
		else:
			# Execute step: same payload, *no* 'confirm' key
			data = data_common

		body = {
			"msgs": [{"method": "confirmOrExecuteTeamRosterChanges", "data": data}],
			"uiv": 3,
			"refUrl": f"https://www.fantrax.com/fantasy/league/{league_id}/team/roster",
			"dt": 0,
			"at": 0,
			"av": "0.0",
		}

		j = self._post_fxpa(league_id, body)

		# --- Parse alternate shapes safely ---
		resp0 = (j.get("responses") or [{}])[0] if isinstance(j, dict) else {}
		data_blob = resp0.get("data") or {}

		# fantasyResponse can be at responses[0] or responses[0].data
		fr = (resp0.get("fantasyResponse") or data_blob.get("fantasyResponse") or {}) if isinstance(resp0, dict) else {}

		# model flags wander; look in several places
		ta = data_blob.get("textArray") or {}
		model = (ta.get("model") or data_blob.get("model") or fr.get("model") or {}) if isinstance(data_blob, dict) else {}

		msg_type = fr.get("msgType") or data_blob.get("msgType") or resp0.get("msgType")
		change_allowed = fr.get("changeAllowed")
		illegal = list(fr.get("illegalRosterMsgs") or []) or list(model.get("illegalRosterMsgs") or [])

		top_page_error = (j or {}).get("pageError") or resp0.get("pageError") or data_blob.get("pageError")

		# --- Success rules ---
		if not do_finalize:
			ok_flag = (msg_type in ("CONFIRM", "WARNING", "SUCCESS")) or (change_allowed is True)
		else:
			# Execute often returns msgType="CONFIRM" with empty lineupChanges → success
			ok_flag = (msg_type in ("CONFIRM", "SUCCESS")) or (change_allowed is True and not illegal)

		if top_page_error:
			ok_flag = False

		# Useful diagnostics
		try:
			log.info(
				"[lineup] finalize=%s type=%s confirmWindow=%s illegal=%s",
				do_finalize, msg_type, fr.get("showConfirmWindow"), illegal or None
			)
			log.info(
				"[lineup] changeAllowed=%s firstIllegalPeriod=%s pickDeadlinePassed=%s",
				(change_allowed if change_allowed is not None else model.get("changeAllowed")),
				model.get("firstIllegalRosterPeriod"),
				model.get("playerPickDeadlinePassed"),
			)
		except Exception:
			pass

		return {
			"ok": bool(ok_flag),
			"fantasyResponse": fr,
			"model": model,
			"illegalMsgs": illegal,
			"mainMsg": fr.get("mainMsg") or data_blob.get("mainMsg"),
			"raw": j,
		}
	
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
