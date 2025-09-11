# fantrax.py

import json
import logging
from typing import Optional, Union, List, Dict
from requests import Session
from json.decoder import JSONDecodeError
from requests.exceptions import RequestException

from fantraxapi.exceptions import FantraxException, Unauthorized
from fantraxapi.objs import (
	ScoringPeriod, Team, Standings, Trade, TradeBlock, Position,
	Transaction, Roster, Player
)
from fantraxapi.trades import TradesService
from fantraxapi.league import LeagueService
from fantraxapi.waivers import WaiversService
from fantraxapi.drops import DropsService

log = logging.getLogger(__name__)


class FantraxAPI:
	""" Main API wrapper for Fantrax private endpoints. """

	def __init__(self, league_id: str, session: Optional[Session] = None):
		self.league_id = league_id
		self._session = Session() if session is None else session
		self.session = self._session  # keep your alias

		self._teams: Optional[List[Team]] = None
		self._positions: Optional[Dict[str, Position]] = None
		# Feature services
		self.trades = TradesService(self._request, self)
		self.league = LeagueService(self._request, self)
		self.waivers = WaiversService(self._request, self)
		self.drops = DropsService(self)

		# NEW: make our session look like the SPA (tz + UI version)
		self._apply_client_hints()

	def _apply_client_hints(self) -> None:
		"""
		Give the session the same hints the web app uses.
		- X-TZ tells Fantrax our timezone
		- X-Fantrax-UI-Version populates payload["v"]
		"""
		# Default TZ if caller didn't set a header already
		if not self._session.headers.get("X-TZ"):
			self._session.headers["X-TZ"] = "America/Los_Angeles"

		# One-time probe to fetch UI version (shown as "up" on responses)
		if not self._session.headers.get("X-Fantrax-UI-Version"):
			payload = {"msgs": [{"method": "getAllLeagues", "data": {"view": "LEAGUES"}}], "uiv": 3}
			try:
				r = self._session.post(
					"https://www.fantrax.com/fxpa/req",
					json=payload,
					timeout=20,
					headers={
						"Accept": "application/json; charset=UTF-8",
						"X-Requested-With": "XMLHttpRequest",
					},
				)
				j = r.json()
				up = (j.get("data") or {}).get("up")
				if up:
					self._session.headers["X-Fantrax-UI-Version"] = str(up)
			except Exception:
				# If this fails, it's okay — calls will still work without "v"
				pass

	@property
	def teams(self) -> List[Team]:
		if self._teams is None:
			response = self._request("getFantasyTeams")
			self._teams = []
			for data in response["fantasyTeams"]:
				# Team(api, id, name, shortName, logoUrl256)
				t = Team(
					self,
					data["id"],
					data["name"],
					data.get("shortName", ""),
					data.get("logoUrl256", ""),
				)
				# Defensive aliases for snake_case
				if not hasattr(t, "short_name"):
					setattr(t, "short_name", getattr(t, "shortName", "") or "")
				if not hasattr(t, "logo_url"):
					setattr(t, "logo_url", data.get("logoUrl256", ""))
				self._teams.append(t)
		return self._teams

	def get_team_by_id(self, team_id: str) -> Team:
		for team in self.teams:
			if team.team_id == team_id:
				return team
		raise FantraxException(f"Team ID: {team_id} not found")

	def resolve_active_period(self, team_id: Optional[str] = None, *, use_confirm_probe: bool = True) -> int:
		"""
		Resolve rosterLimitPeriod robustly:
		A) getTeamRosterInfo → textArray.model.rosterAdjustmentInfo.rosterLimitPeriod
		B) getStandings(view='SCHEDULE') → currentPeriod
		C) DIRECT confirm probe via _request(..., confirm=True, rosterLimitPeriod=0) and read textArray.model.rosterAdjustmentInfo.rosterLimitPeriod
		Returns >0 else 1 (and records a trace for debugging).
		"""
		import logging
		logger = logging.getLogger(__name__)
		tid = team_id or self.teams[0].team_id
		trace = {}

		# --- A) roster_info ---
		try:
			data = self._request("getTeamRosterInfo", teamId=tid)
			model = ((data.get("textArray") or {}).get("model") or {})
			rai = (model.get("rosterAdjustmentInfo") or data.get("rosterAdjustmentInfo") or {})
			p = rai.get("rosterLimitPeriod")
			if p is None:
				for k in ("currentPeriod", "period", "rosterPeriod", "currentScoringPeriod"):
					if data.get(k) is not None:
						p = data.get(k); break
			if p is not None and int(str(p)) > 0:
				p_int = int(str(p)); trace["A"] = p_int
				logger.info("[period] source=A:roster_info -> %s", p_int)
				self._last_period_trace = trace
				return p_int
			trace["A"] = "missing"
		except Exception as e:
			trace["A"] = f"err:{e}"

		# --- B) schedule standings ---
		try:
			sched = self._request("getStandings", view="SCHEDULE")
			cand = (sched.get("currentPeriod")
					or (sched.get("scheduleInfo") or {}).get("currentPeriod")
					or (sched.get("fantasyResponse") or {}).get("currentPeriod"))
			if cand is not None and int(str(cand)) > 0:
				p_int = int(str(cand)); trace["B"] = p_int
				logger.info("[period] source=B:schedule -> %s", p_int)
				self._last_period_trace = trace
				return p_int
			trace["B"] = "missing"
		except Exception as e:
			trace["B"] = f"err:{e}"

		# --- C) DIRECT confirm probe (do NOT go through SubsService) ---
		if use_confirm_probe:
			try:
				roster = self.roster_info(tid)
				# Build fieldMap mirroring current roster
				fmap = {}
				for r in roster.rows:
					if not getattr(r, "player", None):
						continue
					pos_id = str(getattr(r, "pos_id", "0") or "0")
					st_id  = "1" if pos_id != "0" else "2"
					fmap[r.player.id] = {"posId": pos_id, "stId": st_id}

				pre = self._request("confirmOrExecuteTeamRosterChanges",
									rosterLimitPeriod=0,		  # let server choose (echo)
									fantasyTeamId=tid,
									daily=False,
									adminMode=False,
									confirm=True,
									applyToFuturePeriods=False,
									fieldMap=fmap)

				# Parse like your DevTools payload
				model = ((pre.get("textArray") or {}).get("model") or {})
				rai = (model.get("rosterAdjustmentInfo") or {})
				p = rai.get("rosterLimitPeriod")
				if p is not None and int(str(p)) > 0:
					p_int = int(str(p)); trace["C"] = p_int
					logger.info("[period] source=C:confirm-probe -> %s", p_int)
					self._last_period_trace = trace
					return p_int
				trace["C"] = "missing_model"
			except Exception as e:
				trace["C"] = f"err:{e}"

		logger.warning("[period] could not resolve; defaulting to 1 | trace=%s", trace)
		self._last_period_trace = trace
		return 1

	def _current_roster_limit_period(self) -> int:
		p = self.resolve_active_period()
		log.info(f"Resolved current rosterLimitPeriod={p}")
		return p

	# Back-compat alias (objs.Roster calls api.team(...))
	def team(self, team_id: str) -> Team:
		return self.get_team_by_id(team_id)

	def find_team_by_name(self, needle: str) -> Optional[Team]:
		q = (needle or "").strip().lower()
		if not q:
			return None
		for t in self.teams:
			name = getattr(t, "name", "") or ""
			short1 = getattr(t, "short_name", "") or ""
			short2 = getattr(t, "shortName", "") or ""
			if q in name.lower() or q in short1.lower() or q in short2.lower():
				return t
		return None

	@property
	def positions(self) -> Dict[str, Position]:
		if self._positions is None:
			ref = self._request("getRefObject", type="Position")
			self._positions = {k: Position(self, v) for k, v in ref["allObjs"].items()}
		return self._positions

	def _request(self, method: str, **data):
		"""
		Core FXPA request with browser-shaped payload:
		- root fields: uiv, refUrl, tz, v, dt/at/av
		- Accept + X-Requested-With headers
		- checks top-level and per-response pageError
		Always returns the first responses[0].data dict.
		"""
		url = f"https://www.fantrax.com/fxpa/req"
		# Build a realistic refUrl (the SPA sends a page URL). For lineup changes,
		# include the ;period anchor so the backend knows what you're viewing.
		ref_url = f"https://www.fantrax.com/fantasy/league/{self.league_id}"
		if method == "confirmOrExecuteTeamRosterChanges":
			period_hint = data.get("rosterLimitPeriod")
			ref_url = f"{ref_url}/team/roster" + (f";period={int(period_hint)}" if period_hint else "")

		payload = {
			"msgs": [{"method": method, "data": {**data, "leagueId": self.league_id}}],
			"uiv": 3,
			"refUrl": ref_url,
			"dt": 0, "at": 0, "av": "0.0",
		}

		# Inject client hints the SPA includes
		tz = self._session.headers.get("X-TZ")
		ui_ver = self._session.headers.get("X-Fantrax-UI-Version")
		if tz:
			payload["tz"] = tz
		if ui_ver:
			payload["v"] = ui_ver

		headers = {
			"Accept": "application/json; charset=UTF-8",
			"X-Requested-With": "XMLHttpRequest",
		}

		try:
			res = self._session.post(
				url,
				params={"leagueId": self.league_id},
				json=payload,
				timeout=30,
				headers=headers,
			)
		except Exception as e:
			raise FantraxException(f"Network error calling {method}: {e}")

		text = res.text or ""
		try:
			j = res.json()
		except Exception as e:
			low = text[:200].lower()
			if res.status_code in (401, 403) or "sign in" in low or "log in" in low:
				raise FantraxException(f"Auth/permission error calling {method}: HTTP {res.status_code}")

			if method == "getStandings":
				logging.getLogger("fantraxapi.fantrax").warning(
					"[_request] %s returned non-JSON (status=%s). Returning soft-null schedule data.",
					method, res.status_code
				)
				return {"currentPeriod": None}

			raise FantraxException(
				f"Failed to parse JSON for {method}: {e} | HTTP {res.status_code} | first200={text[:200]!r}"
			)

		if res.status_code >= 400:
			raise FantraxException(f"({res.status_code} [{res.reason}]) {j}")

		# Top-level pageError (your logs show UNEXPECTED_ERROR here)
		pe_top = j.get("pageError")
		if pe_top:
			raise FantraxException(f"PageError(top) calling {method}: {pe_top}")

		# Per-response pageError
		r0 = (j.get("responses") or [{}])[0]
		if r0.get("pageError"):
			raise FantraxException(f"PageError calling {method}: {r0['pageError']}")

		return r0.get("data") or {}

	# ---------- Higher-level helpers ----------
	def scoring_periods(self) -> Dict[int, ScoringPeriod]:
		periods = {}
		response = self._request("getStandings", view="SCHEDULE")
		self._teams = []
		for team_id, data in response["fantasyTeamInfo"].items():
			t = Team(self, team_id, data["name"], data.get("shortName", ""), data.get("logoUrl512", ""))
			if not hasattr(t, "short_name"):
				setattr(t, "short_name", getattr(t, "shortName", "") or "")
			self._teams.append(t)
		for period_data in response["tableList"]:
			period = ScoringPeriod(self, period_data)
			periods[period.week] = period
		return periods

	def standings(self, week: Optional[Union[int, str]] = None) -> Standings:
		if week is None:
			response = self._request("getStandings")
		else:
			response = self._request(
				"getStandings", period=week, timeframeType="BY_PERIOD", timeStartType="FROM_SEASON_START"
			)

		self._teams = []
		for team_id, data in response["fantasyTeamInfo"].items():
			t = Team(self, team_id, data["name"], data.get("shortName", ""), data.get("logoUrl512", ""))
			if not hasattr(t, "short_name"):
				setattr(t, "short_name", getattr(t, "shortName", "") or "")
			self._teams.append(t)
		return Standings(self, response["tableList"][0], week=week)

	def pending_trades(self) -> List[Trade]:
		return self.trades.list_pending()

	def trade_block(self):
		return self.trades.get_trade_block()

	def transactions(self, count=100) -> List[Transaction]:
		response = self._request("getTransactionDetailsHistory", maxResultsPerPage=str(count))
		transactions = []
		update = False
		for row in response["table"]["rows"]:
			if update:
				transaction.update(row)	 # noqa
				update = False
			else:
				transaction = Transaction(self, row)
			if transaction.count > 1 and not transaction.finalized:
				update = True
			else:
				transactions.append(transaction)
		return transactions

	def max_goalie_games_this_week(self) -> int:
		response = self._request("getTeamRosterInfo", teamId=self.teams[0].team_id, view="GAMES_PER_POS")
		for maxes in response["gamePlayedPerPosData"]["tableData"]:
			if maxes["pos"] == "NHL Team Goalies (TmG)":
				return int(maxes["max"])

	def playoffs(self) -> Dict[int, ScoringPeriod]:
		response = self._request("getStandings", view="PLAYOFFS")
		other_brackets = {}
		for tab in response["displayedLists"]["tabs"]:
			if tab["id"].startswith("."):
				other_brackets[tab["name"]] = tab["id"]

		playoff_periods = {}
		for obj in response["tableList"]:
			if obj["caption"] == "Standings":
				continue
			period = ScoringPeriod(self, obj)
			playoff_periods[period.week] = period

		for name, bracket_id in other_brackets.items():
			response = self._request("getStandings", view=bracket_id)
			for obj in response["tableList"]:
				if obj["caption"] == "Standings":
					continue
				playoff_periods[int(obj["caption"][17:])].add_matchups(obj)

		return playoff_periods

	def roster_info(self, team_id):
		return Roster(self, self._request("getTeamRosterInfo", teamId=team_id), team_id)
		
	def _extract_rows(self, stats_table):
		"""Helper method to extract rows from statsTable, handling both dict and list shapes."""
		rows = []
		if isinstance(stats_table, dict):
			rows = stats_table.get("rows") or stats_table.get("data") or []
		elif isinstance(stats_table, list):
			# The statsTable is a list of player objects, each with a 'scorer' key
			# Each item in the list is already a player row
			rows = stats_table
		return rows

	def get_all_players(self, debug: bool = False) -> List[Player]:
		"""
		Get all players available in Fantrax.
		
		Args:
			debug: If True, print debug information about the response
		
		Returns:
			List of Player objects
		"""
		# Get all players using the player stats endpoint (supports pagination)
		# Use maxResultsPerPage=500 to reduce pagination overhead
		response = self._request(
			"getPlayerStats",
			miscDisplayType="1",  # Standard display
			pageNumber="1",	 # First page
			statusOrTeamFilter="ALL",  # Get all players, not just available
			view="STATS",  # Include stats view
			positionOrGroup="ALL",	# Ensure all position groups are included
			maxResultsPerPage="500"	 # Reduce pagination overhead
		)
		
		if debug:
			print("\nAPI Response:")
			print(f"Keys in response: {list(response.keys())}")
			if "statsTable" in response:
				stats_table = response["statsTable"]
				print(f"statsTable type: {type(stats_table).__name__}")
				if isinstance(stats_table, dict):
					print(f"Keys in statsTable: {list(stats_table.keys())}")
					if "rows" in stats_table:
						print(f"Number of players (rows): {len(stats_table['rows'])}")
						if stats_table["rows"]:
							print(f"Sample player data keys: {list(stats_table['rows'][0].keys())}")
				elif isinstance(stats_table, list):
					print(f"statsTable list length: {len(stats_table)}")
					if stats_table and isinstance(stats_table[0], dict):
						print(f"First element keys: {list(stats_table[0].keys())}")
						first = stats_table[0]
						if "scorer" in first and isinstance(first["scorer"], dict):
							print(f"First.scorer keys: {list(first['scorer'].keys())}")
						if "cells" in first and isinstance(first["cells"], list):
							print(f"First.cells length: {len(first['cells'])}")
							# Show first few cell keys if they are dicts
							cell_keys = []
							for c in first["cells"][:3]:
								if isinstance(c, dict):
									cell_keys.append(list(c.keys()))
								else:
									cell_keys.append(type(c).__name__)
							for k in ("columns", "rows", "headers"):
								if k in stats_table:
									try:
										print(f"tableHeader.{k} length: {len(stats_table[k])}")
									except Exception:
										pass
				elif isinstance(stats_table, list):
					print(f"tableHeader length: {len(stats_table)}")
		
		# Extract player rows robustly from first page
		rows = self._extract_rows(response.get("statsTable"))
		
		# Fallback: try paginatedResultSet common shapes if statsTable is empty
		if not rows:
			prs = response.get("paginatedResultSet") or {}
			for key in ("results", "rows", "data", "items"):  # try common keys
				candidate = prs.get(key)
				if isinstance(candidate, list) and candidate:
					rows = candidate
					break

		if debug and rows:
			print(f"Total extracted rows (page 1): {len(rows)}")
			print(f"Sample row keys: {list(rows[0].keys())}")

		# If paginated, fetch remaining pages with proper row extraction
		prs = response.get("paginatedResultSet") or {}
		total_pages = 1
		try:
			total_pages = int(prs.get("totalNumPages") or 1)
		except Exception:
			total_pages = 1

		if total_pages > 1:
			log.info(f"Fetching {total_pages} total pages...")
			for page_num in range(2, total_pages + 1):
				page_resp = self._request(
					"getPlayerStats",
					miscDisplayType="1",
					pageNumber=str(page_num),
					statusOrTeamFilter="ALL",
					view="STATS",
					positionOrGroup="ALL",	# Ensure all position groups are included
					maxResultsPerPage="500"	 # Reduce pagination overhead
				)
				
				# Use the same row extraction logic for consistency
				page_rows = self._extract_rows(page_resp.get("statsTable"))
				
				# Fallback for paginatedResultSet if statsTable is empty
				if not page_rows:
					prs2 = page_resp.get("paginatedResultSet") or {}
					for key in ("results", "rows", "data", "items"):
						if isinstance(prs2.get(key), list) and prs2[key]:
							page_rows = prs2[key]
							break
				
				if page_rows:
					rows.extend(page_rows)
					log.info(f"Page {page_num}: +{len(page_rows)} rows (total: {len(rows)})")
				else:
					log.warning(f"Page {page_num}: No rows extracted")

		if debug and rows:
			print(f"Total extracted rows (all pages): {len(rows)}")

		# Build Player objects from all extracted rows
		players = []
		for player_data in rows:
			if isinstance(player_data, dict):
				# If rows are table cells shape, try to extract core player info from 'scorer'
				if "scorer" in player_data and isinstance(player_data["scorer"], dict):
					scorer = player_data["scorer"]
					pos_list = scorer.get("posShortNames") or scorer.get("pos") or []
					if isinstance(pos_list, str):
						pos_list = [pos_list]
					
					# Debug: Log the first few players to see what's happening
					if len(players) < 3:
						log.debug(f"Creating player from scorer data: {scorer.get('name')} - {scorer.get('teamShortName')}")
					
					player_dict = {
						"id": scorer.get("scorerId") or scorer.get("playerId") or scorer.get("id") or scorer.get("pid"),
						"name": scorer.get("name") or scorer.get("fullName") or scorer.get("playerName"),
						"firstName": scorer.get("firstName"),
						"lastName": scorer.get("lastName"),
						"proTeamAbbr": scorer.get("teamShortName") or scorer.get("proTeamAbbr") or scorer.get("team"),
						"position": (pos_list[0] if isinstance(pos_list, list) and pos_list else None),
						"eligiblePositions": pos_list if isinstance(pos_list, list) else [],
						"status": scorer.get("status") or scorer.get("statusId"),
						"injuryStatus": scorer.get("injuryStatus"),
					}
					
					# Debug: Log the created player_dict
					if len(players) < 3:
						log.debug(f"Created player_dict: {player_dict}")
					
					try:
						player_obj = Player(self, player_dict)
						players.append(player_obj)
						
						# Debug: Verify the Player object was created correctly
						if len(players) < 3:
							log.debug(f"Player object created: {player_obj.name} - {player_obj.team} - {player_obj.position}")
					except Exception as e:
						log.error(f"Failed to create Player object for {scorer.get('name')}: {e}")
						log.error(f"player_dict: {player_dict}")
				else:
					try:
						players.append(Player(self, player_data))
					except Exception as e:
						log.error(f"Failed to create Player object from raw data: {e}")
						log.error(f"player_data: {player_data}")
		
		# Sanity checks
		log.info(f"Total players: {len(players)}")
		log.info(f"Contains Moises Caicedo (05rb8)? {any(p.id=='05rb8' for p in players)}")
		
		return players

	# Lineup helpers
	def make_lineup_changes(self, team_id: str, changes: dict, apply_to_future_periods: bool = True) -> bool:
		log.info(f"Making lineup changes for team {team_id}")
		log.info("Requested changes:\n%s", json.dumps(changes, indent=2, ensure_ascii=False))

		# Build complete fieldMap from current roster (strings for posId/stId)
		roster = self.roster_info(team_id)
		field_map: Dict[str, Dict[str, str]] = {}
		for row in roster.rows:
			if not row.player:
				continue
			is_starter = str(row.pos_id) != "0"
			field_map[row.player.id] = {
				"posId": str(row.pos_id if is_starter else "0"),
				"stId": "1" if is_starter else "2",
			}

		# Apply requested changes (force to string)
		for pid, cfg in (changes or {}).items():
			if pid not in field_map:
				log.warning("Change requested for unknown player id %s", pid)
				continue
			if "posId" in cfg:
				field_map[pid]["posId"] = str(cfg["posId"])
			if "stId" in cfg:
				field_map[pid]["stId"] = str(cfg["stId"])

		# Choose seed period from schedule
		try:
			sched = self._request("getStandings", view="SCHEDULE")
			current_period = int(sched.get("currentPeriod") or 1)
		except Exception:
			current_period = 1
		log.info("Using seed rosterLimitPeriod=%s", current_period)

		# ---- CONFIRM (browser sends confirm=True) ----
		confirm_req = {
			"rosterLimitPeriod": current_period,
			"fantasyTeamId": team_id,
			"teamId": team_id,                # critical: send both ids
			"daily": False,
			"adminMode": False,
			"confirm": True,                  # confirm only on this step
			"applyToFuturePeriods": bool(apply_to_future_periods),
			"fieldMap": field_map,
		}
		confirm_resp = self._request("confirmOrExecuteTeamRosterChanges", **confirm_req)

		# Inspect model to pick execution period/applyToFuture
		model = ((confirm_resp.get("textArray") or {}).get("model") or {})
		log.debug("CONFIRM model: %s", model)

		change_allowed = bool(model.get("changeAllowed", True))
		deadline_passed = bool(model.get("playerPickDeadlinePassed"))
		rai = (model.get("rosterAdjustmentInfo") or {})
		first_illegal = model.get("firstIllegalRosterPeriod")
		if isinstance(first_illegal, str) and first_illegal.isdigit():
			first_illegal = int(first_illegal)

		if change_allowed and not deadline_passed:
			exec_period = int(rai.get("rosterLimitPeriod") or current_period)
			exec_apply_future = bool(apply_to_future_periods)
		elif first_illegal and int(first_illegal) > 0:
			exec_period = int(first_illegal)
			exec_apply_future = True
		else:
			exec_period = int(current_period + 1)  # schedule forward if deadline passed/unknown
			exec_apply_future = True

		log.info("Finalize using period=%s, applyToFuturePeriods=%s", exec_period, exec_apply_future)

		# ---- FINALIZE (browser does NOT send confirm=False; it omits 'confirm') ----
		finalize_req = {
			"rosterLimitPeriod": exec_period,
			"fantasyTeamId": team_id,
			"teamId": team_id,
			"daily": False,
			"adminMode": False,
			"applyToFuturePeriods": exec_apply_future,
			"fieldMap": field_map,
		}
		exec_resp = self._request("confirmOrExecuteTeamRosterChanges", **finalize_req)

		# Some successful finalizations are quiet; log what we can
		fr = (exec_resp or {}).get("fantasyResponse") or {}
		if fr.get("mainMsg"):
			log.info("Fantrax says: %s", fr["mainMsg"])
		illegal = ((fr.get("illegalRosterMsgs") or []))
		msg_type = (fr.get("msgType") or "").upper()
		ok = (not illegal) and (msg_type in ("", "SUCCESS", "CONFIRM", None))

		log.info("Lineup change result: %s", "SUCCESS" if ok else "FAILED")
		return bool(ok)


	def swap_players(self, team_id: str, player1_id: str, player2_id: str) -> bool:
		"""
		Swap starter/bench (or two like-position starters).
		We flip stId only and let the server place the player legally.
		"""
		roster = self.roster_info(team_id)
		p1_st = p2_st = None
		p1_pos = p2_pos = None

		for row in roster.rows:
			if not row.player:
				continue
			if row.player.id == player1_id:
				p1_st = "1" if str(row.pos_id) != "0" else "2"
				p1_pos = str(row.pos_id)
			elif row.player.id == player2_id:
				p2_st = "1" if str(row.pos_id) != "0" else "2"
				p2_pos = str(row.pos_id)

		if p1_st is None or p2_st is None:
			raise FantraxException("One or both players not found on roster")

		# Minimal, browser-like change set
		changes = {
			player1_id: {"stId": p2_st},
			player2_id: {"stId": p1_st},
		}

		log.info("Swap changes: %s", json.dumps(changes, indent=2))
		return self.make_lineup_changes(team_id, changes, apply_to_future_periods=True)

	def move_to_starters(self, team_id: str, player_ids: list) -> bool:
		changes = {pid: {"stId": "1"} for pid in player_ids}
		return self.make_lineup_changes(team_id, changes)

	def move_to_bench(self, team_id: str, player_ids: list) -> bool:
		changes = {pid: {"stId": "2", "posId": "0"} for pid in player_ids}
		return self.make_lineup_changes(team_id, changes)

