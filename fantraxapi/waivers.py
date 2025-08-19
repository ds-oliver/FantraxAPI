from typing import Optional, Dict, Any

from .exceptions import FantraxException


class WaiversService:
    """Feature module for waiver/claim operations."""

    def __init__(self, request_callable, api):
        self._request = request_callable
        self._api = api

    def submit_claim(
        self,
        *,
        team_id: str,
        claim_scorer_id: str,
        bid_amount: float = 0.0,
        drop_scorer_id: Optional[str] = None,
        to_position_id: Optional[str] = None,
        to_status_id: str = "2",  # 1=Active, 2=Reserve/Bench
        group: Optional[int] = None,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Submit a waiver claim with optional drop and FAAB bid.

        NOTE: Fantrax uses private endpoints; this method tries a sequence of
        likely method names. If your league uses a different method, pass it via
        config or update the list below.
        """

        # Build a transactions-like payload consistent with examples
        transactions = []

        # Claim transaction (CLAIM)
        claim_tx: Dict[str, Any] = {
            "type": {"code": "CLAIM", "name": "Claim"},
            "scorerId": claim_scorer_id,
            "toStatusId": to_status_id,
        }
        if to_position_id:
            claim_tx["toPositionId"] = to_position_id
        if priority is not None:
            claim_tx["priority"] = priority
        if group is not None:
            claim_tx["group"] = group
        if bid_amount:
            claim_tx["bid"] = float(bid_amount)
        transactions.append(claim_tx)

        # Optional drop paired with the claim (DROP)
        if drop_scorer_id:
            drop_tx: Dict[str, Any] = {
                "type": {"code": "DROP", "name": "Drop"},
                "scorerId": drop_scorer_id,
                "sourceTeamId": team_id,
            }
            transactions.append(drop_tx)

        # Common data properties seen in example responses
        base_data = {
            "fantasyTeamId": team_id,
        }

        # Try a sequence of likely private methods
        candidate_methods = [
            # Known trade pattern uses submitTrade; for claims these are educated guesses
            "submitPendingClaimsDrops",
            "savePendingClaimsDrops",
            "submitClaims",
            "submitClaim",
            "submitWaiverClaim",
            # Generic fallbacks sometimes seen in Fantrax flows
            "submitTransactionSet",
            "submitTransactions",
        ]

        last_error: Optional[Exception] = None
        for method in candidate_methods:
            try:
                # Different methods may expect different shapes; try the most common first
                # 1) transactions list
                return self._request(method, transactions=transactions, **base_data)
            except FantraxException as e:
                last_error = e
                # Try alternative shapes only for generic fallbacks
                if method in ("submitTransactionSet", "submitTransactions"):
                    try:
                        tx_set = {
                            "typeCode": "CLAIM",
                            "transactions": transactions,
                            "creatorTeamId": team_id,
                        }
                        return self._request(method, transactionSet=tx_set, **base_data)
                    except FantraxException as e2:
                        last_error = e2
                        continue
                continue

        # If all attempts failed, surface a helpful error
        raise FantraxException(
            f"Failed to submit claim after trying {len(candidate_methods)} methods. "
            f"Last error: {last_error}"
        )


