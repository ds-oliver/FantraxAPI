# fantraxapi/drops.py
"""
Service for handling player drops in Fantrax (soft preflight; never blocks on lock/period).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from .exceptions import FantraxException

logger = logging.getLogger(__name__)


class Drops:
    """Attach as api.drops. Soft preflight + message-forwarding behavior."""

    def __init__(self, api):
        self._api = api

    # -------- helpers --------

    def get_current_period(self) -> int:
        """
        Best-effort current scoring period. If this fails upstream, callers can
        choose to ignore it (we never block purely on this).
        """
        # Try standings caption (e.g., "Week 38")
        try:
            resp = self._api._request("getStandings")
            if isinstance(resp, dict) and (resp.get("tableList") or []):
                caption = (resp["tableList"][0] or {}).get("caption", "")
                parts = caption.strip().split()
                if parts and parts[-1].isdigit():
                    return int(parts[-1])
        except Exception as e:
            logger.debug(f"get_current_period via getStandings failed: {e}")

        # Fallback: scoring periods (take the highest available week number)
        try:
            periods = self._api.scoring_periods()
            if periods:
                latest = None
                for p in periods.values():
                    w = getattr(p, "week", None)
                    if isinstance(w, int):
                        latest = max(latest or 0, w)
                if latest is not None:
                    return latest
        except Exception as e:
            logger.debug(f"get_current_period via scoring_periods failed: {e}")

        raise FantraxException("Unable to determine current period")

    # -------- preflight / validation --------

    def preflight_drop(
        self,
        team_id: str,
        scorer_id: str,
        period: Optional[int] = None,
    ) -> Tuple[List[str], List[str]]:
        """
        Soft checks ONLY. Returns (errors, warnings). Do NOT raise for locks/period.
        """
        errors: List[str] = []
        warnings: List[str] = []

        if not team_id or not scorer_id:
            errors.append("Missing team_id or scorer_id")
            return errors, warnings

        # Is the player on this roster?
        try:
            roster = self._api.roster_info(team_id)
            on_roster = any(getattr(r, "player", None) and r.player.id == scorer_id for r in roster.rows)
            if not on_roster:
                errors.append("Player not on this roster")
        except Exception as e:
            warnings.append(f"Could not verify roster locally: {e}")

        # Period heuristic → warning only
        if period is not None:
            try:
                current = self.get_current_period()
                if current is not None and period < current:
                    warnings.append(
                        "Requested period is in the past; Fantrax may schedule the change next gameweek."
                    )
            except Exception:
                pass

        # Example hard error hook (disabled by default):
        # if not self._league_allows_claims_drops():
        #     errors.append("Claims/Drops are disabled for this league")

        return errors, warnings

    def validate_drop(self, team_id: str, scorer_id: str, period: Optional[int] = None) -> Dict[str, List[str]]:
        """
        Compatibility shim for older callers that expect 'validate_drop' to exist.
        Never raises for locks/period; only raises for guaranteed errors (e.g., not on roster).
        """
        errors, warnings = self.preflight_drop(team_id, scorer_id, period)
        if errors:
            raise FantraxException("; ".join(errors))
        return {"warnings": warnings}

    # -------- action --------

    def drop_player(
        self,
        team_id: str,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False,
        return_details: bool = True,
    ):
        """
        Create a DROP transaction. We let the server decide timing (immediate vs scheduled).
        Returns a dict with 'ok', 'scheduled', 'drop_period', 'effective_msg', 'messages', 'raw'
        when return_details=True; otherwise returns True/False.
        """

        # Try to pass a sensible period (never block if missing).
        if period is None:
            try:
                period = self.get_current_period()
            except Exception:
                period = None

        preflight_warnings: List[str] = []
        if not skip_validation:
            errors, warnings = self.preflight_drop(team_id, scorer_id, period)
            preflight_warnings = warnings or []
            if errors:
                # Only true, guaranteed failures should stop here.
                raise FantraxException("; ".join(errors))

        # Optional: fetch details (no-op if Fantrax ignores)
        try:
            self._api._request("getScorerDetails", scorers=[{"scorerId": scorer_id}], teamId=team_id)
        except Exception as e:
            logger.debug(f"getScorerDetails prefetch failed: {e}")

        confirm_data = {
            "transactionSets": [{
                "transactions": [{
                    "type": "DROP",
                    "scorerId": scorer_id,
                    "teamId": team_id
                }]
            }]
        }

        # 1) Preview: see dropPeriod / human message
        try:
            preview = self._api._request("getClaimDropConfirmInfo", **confirm_data)
        except Exception as e:
            logger.debug(f"Preview failed (continuing): {e}")
            preview = {}

        drop_period = None
        eff_msg = None
        try:
            cr = (preview.get("confirmResponses") or [])[0]
            drop_period = cr.get("dropPeriod")
            eff_msg = cr.get("dropEffectiveDateMsg")
        except Exception:
            pass

        # 2) Execute and accept warnings so Fantrax doesn’t block
        response = self._api._request("createClaimDrop", acceptWarnings=True, **confirm_data)
        if not response:
            raise FantraxException("No response received from drop request")
        if isinstance(response, dict) and response.get("pageError"):
            raise FantraxException(f"Drop failed: {response['pageError']}")

        # 3) Parse messages/flags
        tx_code = None
        detail_messages: List[str] = []
        try:
            txr = (response.get("txResponses") or [])[0]
            tx_code = txr.get("code")
            detail_messages = txr.get("detailMessages") or []
        except Exception:
            pass

        # 4) Decide immediate vs scheduled
        scheduled = False
        try:
            current = self.get_current_period()
        except Exception:
            current = None
        if drop_period and current and drop_period > current:
            scheduled = True

        # 5) Optional quick sanity check for immediate drops (never flips to failure)
        if not scheduled:
            try:
                import time
                time.sleep(1.0)
                roster = self._api.roster_info(team_id)
                _ = any(getattr(r, "player", None) and r.player.id == scorer_id for r in roster.rows)
            except Exception:
                pass

        result = {
            "ok": True,
            "code": tx_code,
            "scheduled": scheduled,
            "drop_period": drop_period,
            "effective_msg": eff_msg,
            "messages": (preflight_warnings or []) + (detail_messages or []),
            "raw": response,
        }
        return result if return_details else True


# Optional alias if some code imports DropsService
DropsService = Drops
__all__ = ["Drops", "DropsService"]
