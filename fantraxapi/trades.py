from typing import List

from .exceptions import FantraxException
from .objs import Trade, TradeBlock


class TradesService:
    """Feature module for trade-related operations.

    This service is intentionally thin and uses the core client's request
    function and models. Keep methods additive to avoid breaking changes.
    """

    def __init__(self, request_callable, api):
        # request_callable must have signature: (method: str, **kwargs) -> dict
        self._request = request_callable
        self._api = api

    def list_pending(self) -> List[Trade]:
        """Return all currently pending trades in the league."""
        response = self._request("getPendingTransactions")
        trades: List[Trade] = []
        if "tradeInfoList" in response:
            for trade_data in response["tradeInfoList"]:
                trades.append(Trade(self._api, trade_data))
        return trades

    def get_trade_block(self) -> List[TradeBlock]:
        """Return league trade blocks for all teams (requires auth)."""
        data = self._request("getTradeBlocks")
        return [
            TradeBlock(self._api, block)
            for block in data.get("tradeBlocks", [])
            if len(block) > 2
        ]

    def propose_trade(
        self,
        *,
        from_team_id: str,
        to_team_id: str,
        player_ids_to_give: list[str] | None = None,
        player_ids_to_receive: list[str] | None = None,
        faab_to_give: float | None = None,
        faab_to_receive: float | None = None,
        conditional_drops: dict[str, str] | None = None,
    ) -> dict:
        """Propose a trade between teams.

        Args:
            from_team_id: Your team's ID (the proposer)
            to_team_id: The other team's ID
            player_ids_to_give: List of your player IDs to give
            player_ids_to_receive: List of their player IDs you want
            faab_to_give: Amount of FAAB to give (if any)
            faab_to_receive: Amount of FAAB to receive (if any)
            conditional_drops: Dict mapping player_id to drop_player_id for roster space

        Returns:
            dict: Trade response with transaction IDs and status

        Example:
            # Trade your player "abc123" for their player "def456"
            api.trades.propose_trade(
                from_team_id="your_team_id",
                to_team_id="their_team_id",
                player_ids_to_give=["abc123"],
                player_ids_to_receive=["def456"]
            )

            # Trade 5 FAAB for their player "def456"
            api.trades.propose_trade(
                from_team_id="your_team_id",
                to_team_id="their_team_id",
                faab_to_give=5.0,
                player_ids_to_receive=["def456"]
            )
        """
        transactions = []

        # Add your outgoing players
        if player_ids_to_give:
            for player_id in player_ids_to_give:
                transactions.append({
                    "destinationTeamId": to_team_id,
                    "sourceTeamId": from_team_id,
                    "scorerId": player_id,
                    "type": {"code": "TRADE", "name": "Trade"}
                })

        # Add their players you want
        if player_ids_to_receive:
            for player_id in player_ids_to_receive:
                transactions.append({
                    "destinationTeamId": from_team_id,
                    "sourceTeamId": to_team_id,
                    "scorerId": player_id,
                    "type": {"code": "TRADE", "name": "Trade"}
                })

        # Add FAAB if included
        if faab_to_give:
            transactions.append({
                "destinationTeamId": to_team_id,
                "sourceTeamId": from_team_id,
                "scorerId": f"BA_{faab_to_give}",
                "type": {"code": "TRADE", "name": "Trade"}
            })
        if faab_to_receive:
            transactions.append({
                "destinationTeamId": from_team_id,
                "sourceTeamId": to_team_id,
                "scorerId": f"BA_{faab_to_receive}",
                "type": {"code": "TRADE", "name": "Trade"}
            })

        if not transactions:
            raise FantraxException("Trade must include at least one asset (players or FAAB)")

        # Add conditional drops if specified
        if conditional_drops:
            for incoming_id, drop_id in conditional_drops.items():
                transactions.append({
                    "type": {"code": "DROP", "name": "Drop"},
                    "scorerId": drop_id,
                    "sourceTeamId": from_team_id if incoming_id in (player_ids_to_receive or []) else to_team_id,
                    "conditional": True,
                    "conditionalOnScorerId": incoming_id
                })

        # Submit the trade
        response = self._request("submitTrade", transactions=transactions)
        return response

    def cancel_trade(self, trade_id: str) -> dict:
        """Cancel a pending trade you proposed.

        Args:
            trade_id: The ID of the trade to cancel
        """
        return self._request("cancelTrade", txSetId=trade_id)

    def edit_trade(self, trade_id: str) -> dict:
        """Get trade details for editing.

        Args:
            trade_id: The ID of the trade to edit
        """
        return self._request("editTrade", txSetId=trade_id)

    def get_trade_details(self, trade_id: str) -> dict:
        """Get full details of a trade including status and roster impacts.

        Args:
            trade_id: The ID of the trade to check
        """
        response = self._request("getPendingTransactions", txSetId=trade_id)
        if "tradeInfoList" in response:
            for trade in response["tradeInfoList"]:
                if trade["txSetId"] == trade_id:
                    return trade
        raise FantraxException(f"Trade {trade_id} not found")


