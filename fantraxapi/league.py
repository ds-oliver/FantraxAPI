from typing import List

from .exceptions import FantraxException
from .objs import Roster


class LeagueService:
    """Feature module for league-wide data (rosters, budgets, etc.)."""

    def __init__(self, request_callable, api):
        self._request = request_callable
        self._api = api

    def list_rosters(self) -> List[Roster]:
        """Return the current roster for every team in the league."""
        rosters: List[Roster] = []
        for team in self._api.teams:
            rosters.append(self._api.roster_info(team.team_id))
        return rosters

    def get_roster(self, team_id: str) -> Roster:
        """Return the roster for a specific team."""
        return self._api.roster_info(team_id)

    def faab_budgets(self):
        """Return FAAB budgets for all teams.

        NOTE: Endpoint discovery required. Placeholder for future implementation.
        """
        raise FantraxException("FAAB budgets retrieval not implemented yet. Pending endpoint discovery.")


