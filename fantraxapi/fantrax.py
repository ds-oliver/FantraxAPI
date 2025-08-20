import logging
from typing import Optional, Union, List, Dict
from requests import Session
from json.decoder import JSONDecodeError
from requests.exceptions import RequestException

from fantraxapi.exceptions import FantraxException, Unauthorized
from fantraxapi.objs import (
    ScoringPeriod, Team, Standings, Trade, TradeBlock, Position,
    Transaction, Roster
)
from fantraxapi.trades import TradesService
from fantraxapi.league import LeagueService
from fantraxapi.waivers import WaiversService

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
        logger.debug(f"Request JSON: {json_data}")

        try:
            response = self._session.post(
                "https://www.fantrax.com/fxpa/req",
                params={"leagueId": self.league_id},
                json=json_data
            )
            response_json = response.json()
        except (RequestException, JSONDecodeError) as e:
            raise FantraxException(f"Failed to Connect to {method}: {e}\nData: {data}")

        logger.debug(f"Response ({response.status_code} [{response.reason}]) {response_json}")

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
                transaction.update(row)  # noqa
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

    # Lineup helpers
    def make_lineup_changes(self, team_id: str, changes: dict, apply_to_future_periods: bool = True) -> bool:
        roster = self.roster_info(team_id)
        current_field_map = {}
        for row in roster.rows:
            if row.player:
                current_field_map[row.player.id] = {
                    "posId": row.pos_id,
                    "stId": "1" if row.pos_id != "0" else "2"  # 1=starter, 2=bench
                }
        for player_id, new_config in changes.items():
            if player_id in current_field_map:
                current_field_map[player_id].update(new_config)

        confirm_data = {
            "rosterLimitPeriod": 2,
            "fantasyTeamId": team_id,
            "daily": False,
            "adminMode": False,
            "confirm": True,
            "applyToFuturePeriods": apply_to_future_periods,
            "fieldMap": current_field_map
        }
        try:
            self._request("confirmOrExecuteTeamRosterChanges", **confirm_data)
        except FantraxException as e:
            raise FantraxException(f"Failed to confirm lineup changes: {e}")

        execute_data = confirm_data.copy()
        execute_data["confirm"] = False
        try:
            self._request("confirmOrExecuteTeamRosterChanges", **execute_data)
        except FantraxException as e:
            raise FantraxException(f"Failed to execute lineup changes: {e}")
        return True

    def swap_players(self, team_id: str, player1_id: str, player2_id: str) -> bool:
        roster = self.roster_info(team_id)
        player1_status = None
        player2_status = None
        for row in roster.rows:
            if row.player:
                if row.player.id == player1_id:
                    player1_status = "1" if row.pos_id != "0" else "2"
                elif row.player.id == player2_id:
                    player2_status = "1" if row.pos_id != "0" else "2"
        if player1_status is None or player2_status is None:
            raise FantraxException("One or both players not found on roster")
        changes = {
            player1_id: {"stId": player2_status},
            player2_id: {"stId": player1_status}
        }
        return self.make_lineup_changes(team_id, changes)

    def move_to_starters(self, team_id: str, player_ids: list) -> bool:
        changes = {player_id: {"stId": "1"} for player_id in player_ids}
        return self.make_lineup_changes(team_id, changes)

    def move_to_bench(self, team_id: str, player_ids: list) -> bool:
        changes = {player_id: {"stId": "2"} for player_id in player_ids}
        return self.make_lineup_changes(team_id, changes)
