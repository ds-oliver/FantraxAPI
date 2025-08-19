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
        """Return FAAB budgets for all teams by checking each team's claim page.
        
        Returns:
            Dict[str, dict]: Map of team_id to budget info containing:
                - value: float - Raw budget amount
                - display: str - Formatted budget (e.g. "$100.00")
                - tradeable: bool - Whether budget can be traded
        """
        budgets = {}
        for team in self._api.teams:
            # Get the claim/waiver page for this team to see their budget
            response = self._request("getTeamRosterInfo", teamId=team.team_id, view="CLAIMS")
            if "miscData" in response and "transactionSalaryBudgetInfo" in response["miscData"]:
                for budget in response["miscData"]["transactionSalaryBudgetInfo"]:
                    if budget["key"] == "claimBudget":
                        budgets[team.team_id] = {
                            "value": float(budget["value"]),
                            "display": budget["display"],
                            "tradeable": budget.get("tradeable", False)
                        }
                        break
        return budgets

    def get_claim_info(self, team_id: str) -> dict:
        """Get claim/waiver info for a team including pending claims.
        
        Args:
            team_id: The team ID to check
            
        Returns:
            dict: Claim info including:
                - numPendingClaims: Number of pending claims
                - claimTypes: Available claim types (FREE_FOR_ALL, BIDDING)
                - claimGroupsEnabled: Whether claim groups are enabled
                - showBidColumn: Whether FAAB bidding is enabled
        """
        response = self._request("getTeamRosterInfo", teamId=team_id, view="CLAIMS")
        info = {
            "numPendingClaims": response.get("numPendingClaims", 0),
            "claimTypes": response.get("claimTypes", {}),
            "claimGroupsEnabled": response.get("miscData", {}).get("claimGroupsEnabled", False),
            "showBidColumn": response.get("miscData", {}).get("showBidColumn", False)
        }
        return info


