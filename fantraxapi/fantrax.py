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

logger = logging.getLogger(__name__)


class FantraxAPI:
	""" Main API wrapper for Fantrax private endpoints. """

	def __init__(self, league_id: str, session: Optional[Session] = None):
		self.league_id = league_id
		self._session = Session() if session is None else session
		self._teams: Optional[List[Team]] = None
		self._positions: Optional[Dict[str, Position]] = None
		# Feature services
		self.trades = TradesService(self._request, self)
		self.league = LeagueService(self._request, self)
		self.waivers = WaiversService(self._request, self)
		self.drops = DropsService(self)

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

	def _current_roster_limit_period(self) -> int:
		"""
		Fetch the current scoring period/week ID from Fantrax.
		This is required for lineup changes; using an old one causes 'locked' errors.
		"""
		resp = self._request("getStandings", view="SCHEDULE")
		period_id = int(resp.get("currentPeriod", 0))
		logger.info(f"Resolved current rosterLimitPeriod={period_id}")
		return period_id


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

	def _request(self, method, **kwargs):
		"""Low-level request helper. Returns the inner .responses[0].data."""
		data = {"leagueId": self.league_id}
		data.update(kwargs)
		json_data = {"msgs": [{"method": method, "data": data}]}
		logger.debug("Request JSON:\n%s", json.dumps(json_data, indent=2, ensure_ascii=False))

		try:
			response = self._session.post(
				"https://www.fantrax.com/fxpa/req",
				params={"leagueId": self.league_id},
				json=json_data
			)
			response_json = response.json()
		except (RequestException, JSONDecodeError) as e:
			raise FantraxException(f"Failed to Connect to {method}: {e}\nData: {data}")

		# Log just a preview of the response (first 500 chars)
		preview = json.dumps(response_json, indent=2, ensure_ascii=False)
		if len(preview) > 500:
			preview = preview[:250] + "\n...[truncated]...\n" + preview[-250:]
		logger.debug("Response (%s [%s]):\n%s", 
					response.status_code, 
					response.reason, 
					preview)

		if response.status_code >= 400:
			raise FantraxException(f"({response.status_code} [{response.reason}]) {response_json}")

		if "pageError" in response_json:
			pe = response_json["pageError"]
			if "code" in pe and pe["code"] == "WARNING_NOT_LOGGED_IN":
				raise Unauthorized("Unauthorized: Not Logged in")
			raise FantraxException(f"Error: {response_json}")

		return response_json["responses"][0]["data"]

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
			logger.info(f"Fetching {total_pages} total pages...")
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
					logger.info(f"Page {page_num}: +{len(page_rows)} rows (total: {len(rows)})")
				else:
					logger.warning(f"Page {page_num}: No rows extracted")

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
						logger.debug(f"Creating player from scorer data: {scorer.get('name')} - {scorer.get('teamShortName')}")
					
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
						logger.debug(f"Created player_dict: {player_dict}")
					
					try:
						player_obj = Player(self, player_dict)
						players.append(player_obj)
						
						# Debug: Verify the Player object was created correctly
						if len(players) < 3:
							logger.debug(f"Player object created: {player_obj.name} - {player_obj.team} - {player_obj.position}")
					except Exception as e:
						logger.error(f"Failed to create Player object for {scorer.get('name')}: {e}")
						logger.error(f"player_dict: {player_dict}")
				else:
					try:
						players.append(Player(self, player_data))
					except Exception as e:
						logger.error(f"Failed to create Player object from raw data: {e}")
						logger.error(f"player_data: {player_data}")
		
		# Sanity checks
		logger.info(f"Total players: {len(players)}")
		logger.info(f"Contains Moises Caicedo (05rb8)? {any(p.id=='05rb8' for p in players)}")
		
		return players

	# Lineup helpers
	def make_lineup_changes(self, team_id: str, changes: dict, apply_to_future_periods: bool = True) -> bool:
		logger.info(f"Making lineup changes for team {team_id}")
		logger.info("Requested changes:\n%s", json.dumps(changes, indent=2, ensure_ascii=False))

		# Build current fieldMap from raw roster data
		roster = self.roster_info(team_id)
		current_field_map = {}

		logger.info("Building fieldMap from _raw roster data...")
		for row in roster.rows:
			if not row.player:
				continue
			raw = getattr(row, "_raw", {}) or {}
			pos_id = str(raw.get("posId", row.pos_id))
			st_id = str(raw.get("stId", "2" if pos_id == "0" else "1"))

			current_field_map[row.player.id] = {"posId": pos_id, "stId": st_id}
			logger.info(f"  {row.player.name} ({row.player.id}) -> posId={pos_id}, stId={st_id}")

		# Apply requested changes
		logger.info("Applying requested changes:")
		for pid, new_cfg in changes.items():
			if pid in current_field_map:
				old_cfg = current_field_map[pid].copy()
				current_field_map[pid].update(new_cfg)
				logger.info(f"  {pid}: {old_cfg} -> {current_field_map[pid]}")
			else:
				logger.warning(f"  Change requested for unknown player id {pid}")

		# Always fetch the current scoring period dynamically
		period_id = self._current_roster_limit_period()

		confirm_data = {
			"rosterLimitPeriod": period_id,
			"fantasyTeamId": team_id,
			"daily": False,
			"adminMode": False,
			"confirm": True,
			"applyToFuturePeriods": apply_to_future_periods,
			"fieldMap": current_field_map,
		}

		logger.info("Sending confirmation request...")
		try:
			confirm_resp = self._request("confirmOrExecuteTeamRosterChanges", **confirm_data)
			preview = json.dumps(confirm_resp, indent=2, ensure_ascii=False)
			if len(preview) > 500:
				preview = preview[:250] + "\n...[truncated]...\n" + preview[-250:]
			logger.debug("Confirmation response:\n%s", preview)
		except FantraxException as e:
			logger.error(f"Confirmation request failed: {e}")
			raise

		execute_data = dict(confirm_data)
		execute_data["confirm"] = False
		logger.info("Sending execution request...")
		try:
			exec_resp = self._request("confirmOrExecuteTeamRosterChanges", **execute_data)
			preview = json.dumps(exec_resp, indent=2, ensure_ascii=False)
			if len(preview) > 500:
				preview = preview[:250] + "\n...[truncated]...\n" + preview[-250:]
			logger.debug("Execution response:\n%s", preview)
		except FantraxException as e:
			logger.error(f"Execution request failed: {e}")
			raise

		# Inspect fantasyResponse
		fr = (exec_resp or {}).get("fantasyResponse", {}) or {}
		msg_type = (fr.get("msgType") or "").upper()
		illegal = fr.get("illegalRosterMsgs") or []
		change_allowed = ((fr.get("textArray") or {}).get("model") or {}).get("changeAllowed", True)

		logger.info(f"Response details: msgType={msg_type}, changeAllowed={change_allowed}, illegal={illegal}")
		if fr.get("mainMsg"):
			logger.info(f"  Main message: {fr['mainMsg']}")

		ok = (msg_type in ("", "SUCCESS", None)) and change_allowed and not illegal
		logger.info(f"Lineup change result: {'SUCCESS' if ok else 'FAILED'}")
		return bool(ok)


	def swap_players(self, team_id: str, player1_id: str, player2_id: str) -> bool:
		logger.info(f"Attempting to swap players: {player1_id} <-> {player2_id} for team {team_id}")
		roster = self.roster_info(team_id)

		p1_row = p2_row = None
		for row in roster.rows:
			if not row.player:
				continue
			if row.player.id == player1_id:
				p1_row = row
			elif row.player.id == player2_id:
				p2_row = row

		if not p1_row or not p2_row:
			logger.error("One or both players not found on roster")
			raise FantraxException("One or both players not found on roster")

		# Decide swap
		changes = {}
		if p1_row.pos_id != "0" and p2_row.pos_id == "0":
			# player1 starter, player2 bench
			changes[player1_id] = {"stId": "2", "posId": "0"}
			changes[player2_id] = {"stId": "1", "posId": p1_row.pos_id}
		elif p1_row.pos_id == "0" and p2_row.pos_id != "0":
			# player1 bench, player2 starter
			changes[player2_id] = {"stId": "2", "posId": "0"}
			changes[player1_id] = {"stId": "1", "posId": p2_row.pos_id}
		else:
			# both bench or both starters, just swap stId/posId
			changes[player1_id] = {"stId": ("1" if p2_row.pos_id != "0" else "2"), "posId": p2_row.pos_id}
			changes[player2_id] = {"stId": ("1" if p1_row.pos_id != "0" else "2"), "posId": p1_row.pos_id}

		logger.info(f"Swap changes: {json.dumps(changes, indent=2)}")
		return self.make_lineup_changes(team_id, changes)

	def move_to_starters(self, team_id: str, player_ids: list) -> bool:
		changes = {pid: {"stId": "1"} for pid in player_ids}
		return self.make_lineup_changes(team_id, changes)

	def move_to_bench(self, team_id: str, player_ids: list) -> bool:
		changes = {pid: {"stId": "2", "posId": "0"} for pid in player_ids}
		return self.make_lineup_changes(team_id, changes)

