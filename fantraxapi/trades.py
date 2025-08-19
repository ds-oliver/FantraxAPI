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

    def propose_trade(self, *, from_team_id: str, to_team_id: str, player_moves: list, draft_pick_moves: list | None = None):
        """Propose a trade between teams.

        NOTE: Endpoint and payload format for submitting trades is not yet
        implemented. This method is a placeholder for future work once the
        private endpoint schema is confirmed.
        """
        raise FantraxException("Trade submission is not implemented yet. Pending endpoint discovery.")


