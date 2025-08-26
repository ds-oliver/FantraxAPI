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

from fantraxapi.subs import SubsService

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
			time.sleep(1.0)	 # Fantrax can be eventually-consistent for a second or two
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
	) -> Dict[str, Any]:
		"""
		Perform a single-team drop. Returns a dict:
		{
		ok: bool,
		scheduled: bool,
		drop_period: Optional[int],
		effective_msg: Optional[str],
		messages: List[str],
		verified: Optional[bool],	# present for immediate drops
		raw: Any
		}
		"""
		log.info(
			"Attempting to drop player %s from team %s in league %s",
			scorer_id, team_id, league_id
		)

		# Pre-drop logging (best-effort)
		try:
			initial_roster = self.get_roster(league_id, team_id)
			player_row = self._find_row(initial_roster, scorer_id)
			if player_row and player_row.player:
				st_inf = self._infer_drop_status_from_row(player_row, league_id)
				log.info(
					"Found player to drop: %s (%s) | locked=%s, can_drop_now=%s, curr=%s, eff=%s",
					player_row.player.name, scorer_id,
					st_inf["locked"], st_inf["can_drop_now"],
					st_inf["current_period"], st_inf["effective_period"]
				)
			else:
				log.warning("Player %s not found on initial roster check", scorer_id)
		except Exception as e:
			log.warning("Failed to get initial roster state: %s", e)

		api = self.make_api(league_id)

		try:
			raw = api.drops.drop_player(
				team_id=team_id,
				scorer_id=scorer_id,
				period=None,				 # let server decide timing
				skip_validation=skip_validation,
				return_details=True,		 # get messages/effective GW back
			)
			log.info("Drop API response: %s", raw)

			# Normalize/ensure 'ok' is present
			ok = self._normalize_drop_result(raw)
			if isinstance(raw, dict):
				raw.setdefault("ok", bool(ok))
			else:
				raw = {"ok": bool(ok), "raw": raw, "messages": []}

			# For immediate drops, attempt a quick verification
			verified = None
			if raw.get("ok") and not raw.get("scheduled"):
				try:
					verified = self._verify_drop_applied(league_id, team_id, scorer_id)
					log.info("Drop verification result: %s", verified)
				except Exception as e:
					log.warning("Drop verification failed: %s", e)
				raw["verified"] = verified

			if raw.get("ok") and raw.get("scheduled"):
				log.info(
					"Drop scheduled by server (drop_period=%s). Message: %s",
					raw.get("drop_period"), raw.get("effective_msg")
				)

			return raw

		except Exception as e:
			log.exception("Drop API call failed: %s", e)
			# Surface as a structured result so UI can show error text consistently
			return {
				"ok": False,
				"scheduled": False,
				"drop_period": None,
				"effective_msg": None,
				"messages": [str(e)],
				"verified": None,
				"raw": None,
			}


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
			return results	# empty = nowhere to drop

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
	Thin façade that delegates to fantraxapi.subs.SubsService.
	Keeps Session/auth ownership in the app layer.
	"""

	def __init__(self, session: Session):
		self.session = session
		self._svc = SubsService(session)

	# Optional: small helper so UI never needs _svc
	def get_roster(self, league_id: str, team_id: str) -> Roster:
		return self._svc.get_roster(league_id, team_id)
	
	def warm_codes_for_roster(self, league_id: str, roster: Roster) -> None:
		self._svc.warm_codes_for_roster(league_id, roster)

	# ----- simple accessors -----
	def get_roster(self, league_id: str, team_id: str) -> Roster:
		return self._svc.get_roster(league_id, team_id)

	def list_starters(self, league_id: str, team_id: str) -> List[RosterRow]:
		return self._svc.list_starters(league_id, team_id)

	def list_bench(self, league_id: str, team_id: str) -> List[RosterRow]:
		return self._svc.list_bench(league_id, team_id)

	# ----- single swap helpers -----
	def preflight_swap(
		self, *, league_id: str, team_id: str, starter_player_id: str, bench_player_id: str
	) -> Dict[str, Any]:
		return self._svc.preflight_swap(
			league_id=league_id,
			team_id=team_id,
			starter_player_id=starter_player_id,
			bench_player_id=bench_player_id,
		)

	def swap_players_by_ids(
		self, *, league_id: str, team_id: str, starter_player_id: str, bench_player_id: str, verify: bool = True
	) -> Dict[str, Any]:
		return self._svc.swap_players_by_ids(
			league_id=league_id,
			team_id=team_id,
			starter_player_id=starter_player_id,
			bench_player_id=bench_player_id,
			verify=verify,
		)

	def swap_players_by_names(
		self, *, league_id: str, team_id: str, starter_player_name: str, bench_player_name: str, verify: bool = True
	) -> Dict[str, Any]:
		roster = self.get_roster(league_id, team_id)

		def _by_name(name: str) -> str:
			row = roster.get_player_by_name(name)
			if not row or not getattr(row, "player", None):
				raise ValueError(f"Player not found: {name}")
			return row.player.id

		starter_id = _by_name(starter_player_name)
		bench_id = _by_name(bench_player_name)

		return self.swap_players_by_ids(
			league_id=league_id,
			team_id=team_id,
			starter_player_id=starter_id,
			bench_player_id=bench_id,
			verify=verify,
		)

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
	) -> Dict[str, Any]:
		return self._svc.set_lineup_by_ids(
			league_id=league_id,
			team_id=team_id,
			desired_starter_ids=desired_starter_ids,
			best_effort=best_effort,
			verify_each=verify_each,
			pos_overrides=pos_overrides,
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