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
                - pendingClaims: List of pending claims with details
        """
        # Get basic claim info
        info_response = self._request("getTeamRosterInfo", teamId=team_id, view="CLAIMS")
        info = {
            "numPendingClaims": info_response.get("numPendingClaims", 0),
            "claimTypes": info_response.get("claimTypes", {}),
            "claimGroupsEnabled": info_response.get("miscData", {}).get("claimGroupsEnabled", False),
            "showBidColumn": info_response.get("miscData", {}).get("showBidColumn", False)
        }
        
        # Get detailed pending claims via roster view
        claims_response = self._request("getTeamRosterInfo", teamId=team_id, view="PENDING_CLAIMS")
        pending_claims = []

        if "tables" in claims_response:
            for table in claims_response["tables"]:
                if "txSets" in table:
                    for claim in table["txSets"]:
                        pending_claims.append({
                            "id": claim.get("txSetId"),
                            "type": table.get("claimType"),
                            "process_date": table.get("processDate"),
                            "process_date_raw": table.get("processDateRaw"),
                            "submitted_date": claim.get("dateSubmitted"),
                            "submitted_date_raw": claim.get("dateSubmittedRaw"),
                            "bid_amount": claim.get("bid", 0),
                            "priority": claim.get("priority"),
                            "group": claim.get("group"),
                            "claim_player": ({
                                "id": claim["claimScorer"].get("scorerId"),
                                "name": claim["claimScorer"].get("name"),
                                "position": claim["claimScorer"].get("posShortNames"),
                                "team": claim["claimScorer"].get("teamName"),
                                "to_position": claim.get("toPositionName"),
                                "to_status": claim.get("toStatusName")
                            } if "claimScorer" in claim else None),
                            "drop_player": ({
                                "id": claim["dropOrMoveScorer"].get("scorerId"),
                                "name": claim["dropOrMoveScorer"].get("name"),
                                "position": claim["dropOrMoveScorer"].get("posShortNames"),
                                "team": claim["dropOrMoveScorer"].get("teamName"),
                                "from_position": claim.get("fromPositionName"),
                                "from_status": claim.get("fromStatusName")
                            } if "dropOrMoveScorer" in claim else None)
                        })

        info["pendingClaims"] = pending_claims
        # Prefer computed count if available
        info["numPendingClaims"] = len(pending_claims)
        return info


