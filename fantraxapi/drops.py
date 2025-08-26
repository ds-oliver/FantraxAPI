"""Service for handling player drops in Fantrax."""

# fantraxapi/drops.py

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .exceptions import FantraxException

logger = logging.getLogger(__name__)


class DropsService:
    """Service for handling player drops in Fantrax."""

    def __init__(self, api):
        """Initialize the drops service.
        
        Args:
            api: The FantraxAPI instance
        """
        self._api = api

    def _finalize_tx_set(self, tx_set_id: str) -> dict | None:
        """
        Finalize/execute a pending transaction set. Fantrax sometimes responds
        with another confirmation cycle. This will loop through all verbs and
        keep finalizing until no more 'confirm: True' responses are present.

        Args:
            tx_set_id: The transaction set ID to finalize

        Returns:
            dict | None: Final API response once no further confirmation required
        """
        verbs = ("executeTransactionSet", "finalizeTransactionSet", "executeTransactions")
        safety_counter = 0
        response = None

        while True:
            safety_counter += 1
            if safety_counter > 5:
                raise FantraxException(
                    f"Too many finalize cycles for transaction set {tx_set_id}"
                )

            last_err = None
            for method in verbs:
                try:
                    logger.info(f"Finalizing tx_set {tx_set_id} with method {method}")
                    resp = self._api._request(
                        method,
                        transactionSetId=tx_set_id,
                        acceptWarnings=True  # allow illegal roster warnings
                    )
                    logger.info(f"Finalize response ({method}): {resp}")

                    # Bail on explicit error
                    if isinstance(resp, dict) and resp.get("pageError"):
                        raise FantraxException(
                            f"Finalize failed with pageError: {resp['pageError']}"
                        )

                    response = resp

                    # If no confirm required, we're done
                    if not (
                        isinstance(resp, dict)
                        and resp.get("txResponses")
                        and any(r.get("confirm") for r in resp["txResponses"])
                    ):
                        return response or {}
                    else:
                        logger.info(
                            "Transaction still requires confirmation, retrying finalize loop..."
                        )
                        # break inner loop, retry outer while
                        break
                except Exception as e:
                    last_err = e
                    logger.warning(f"Finalize method {method} failed: {e}")
                    continue

            if last_err and response is None:
                raise FantraxException(
                    f"Could not finalize transaction set {tx_set_id}: {last_err}"
                )

    def get_current_period(self) -> int:
        """Get the current gameweek/period.
        
        Returns:
            int: The current gameweek number
        
        Raises:
            FantraxException: If unable to determine current period
        """
        # Get current period from standings data
        response = self._api._request("getStandings")
        
        # Try to get period from table list
        if "tableList" in response and response["tableList"]:
            table = response["tableList"][0]
            if "caption" in table:
                try:
                    # Caption format is typically "Week N" or similar
                    return int(table["caption"].split()[-1])
                except (IndexError, ValueError):
                    pass
                    
        # Fallback: Try to get from scoring periods
        try:
            periods = self._api.scoring_periods()
            if periods:
                # Get the latest period that has started
                current_time = datetime.now(timezone.utc)
                current_period = None
                for period in periods.values():
                    # TODO: Add logic to check period start/end dates when available
                    current_period = period.week
                if current_period is not None:
                    return current_period
        except Exception as e:
            logger.warning(f"Failed to get period from scoring periods: {e}")
            
        raise FantraxException("Unable to determine current period")

    def validate_drop(self, team_id: str, scorer_id: str, period: Optional[int] = None) -> bool:
        """Validate if a player can be dropped.
        
        Args:
            team_id: The ID of the team dropping the player
            scorer_id: The ID of the player to drop
            period: Optional period/gameweek number. If not provided, uses current period.
            
        Returns:
            bool: True if the drop is valid
            
        Raises:
            FantraxException: If the drop is invalid
        """
        if period is None:
            period = self.get_current_period()

        # Check if player is on the team's roster
        roster = self._api.roster_info(team_id)
        player_found = False
        for row in roster.rows:
            if row.player and row.player.id == scorer_id:
                player_found = True
                break

        if not player_found:
            raise FantraxException(f"Player {scorer_id} not found on team {team_id}")

        # Get pending transactions to check for conflicts
        pending_txns = self._api._request(
            "getPendingTransactions",
            txType="CLAIM",
            teamId=team_id,
            numOnly=False
        )
        
        # Check if player is involved in any pending transactions
        if "transactions" in pending_txns:
            for txn in pending_txns["transactions"]:
                if txn.get("scorerId") == scorer_id:
                    raise FantraxException(f"Player {scorer_id} is involved in a pending transaction")
        
        # Verify the period is current or future
        if period < self.get_current_period():
            raise FantraxException("Cannot make changes to past periods")
            
        return True

    def drop_player(
        self,
        team_id: str,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False,
        league_id: Optional[str] = None,
    ) -> bool:
        """Drop a player from a team's roster.

        Matches the browser flow:
        1) getClaimDropConfirmInfo (to read dropPeriod / message)
        2) createClaimDrop (server immediately returns EXECUTED for a scheduled drop)
        3) optional roster reload (browser does this purely to refresh UI)

        Notes:
        - `confirm: true` in txResponses is *not* a signal to "finalize"; it's a UI hint.
        - We treat `code == "EXECUTED"` as terminal success.
        - To detect a scheduled drop, prefer the preview's `dropPeriod`. As a fallback, parse it from detailMessages.
        - Don't compare to a global "current period"; compare to the league's displayed period from getTeamRosterInfo
        or simply trust the "effective Gameweek ..." message.
        """

        import re
        import time
        logger = getattr(self, "logger", None) or __import__("logging").getLogger(__name__)

        # Best-effort league id
        if league_id is None:
            league_id = getattr(self._api, "league_id", None) or getattr(self._api, "leagueId", None)

        def _safe_displayed_period(_league_id: Optional[str]) -> Optional[int]:
            if not _league_id:
                return None
            try:
                resp = self._api._request("getTeamRosterInfo", leagueId=_league_id, reload="0")
                # Support both shapes: {"data": {...}} or {"responses":[{"data":{...}}]}
                data = (resp.get("responses") or [{}])[0].get("data") if isinstance(resp, dict) and "responses" in resp else resp.get("data", resp)
                return (data or {}).get("displayedSelections", {}).get("displayedPeriod")
            except Exception:
                return None

        def _extract_gw_from_text(text: str) -> Optional[int]:
            if not text:
                return None
            m = re.search(r"Gameweek\s+(\d+)", text)
            return int(m.group(1)) if m else None

        # If caller didn’t provide a reference period, prefer the league’s displayed period over a global current period
        if period is None:
            period = _safe_displayed_period(league_id)
            if period is None:
                try:
                    period = self.get_current_period()
                except Exception:
                    period = None

        if not skip_validation:
            self.validate_drop(team_id, scorer_id, period)

        # Optional preflight like the site does
        try:
            self._api._request(
                "getScorerDetails",
                scorers=[{"scorerId": scorer_id}],
                teamId=team_id
            )
        except Exception:
            pass

        confirm_data = {
            "transactionSets": [{
                "transactions": [{
                    "type": "DROP",
                    "scorerId": scorer_id,
                    "teamId": team_id
                }]
            }]
        }

        # 1) Preview → grab dropPeriod/message
        logger.info(f"Requesting drop confirmation for player {scorer_id}")
        preview = self._api._request("getClaimDropConfirmInfo", **confirm_data)
        logger.info(f"Drop confirmation response: {preview}")

        drop_period = None
        eff_msg = None
        try:
            cr = (preview.get("confirmResponses") or [])[0]
            drop_period = cr.get("dropPeriod")
            eff_msg = cr.get("dropEffectiveDateMsg")
        except Exception:
            pass

        # 2) Execute drop (this is terminal; browser doesn't call any finalize verbs)
        logger.info("Submitting createClaimDrop")
        response = self._api._request("createClaimDrop", **confirm_data)
        logger.info(f"createClaimDrop response: {response}")

        if not response:
            raise FantraxException("No response received from drop request")
        if isinstance(response, dict) and response.get("pageError"):
            raise FantraxException(f"Drop failed: {response['pageError']}")

        # Unwrap txResponses
        tx_list = []
        try:
            # Support both {"data":{"txResponses":[...]}} and {"txResponses":[...]}
            if "txResponses" in response:
                tx_list = response.get("txResponses") or []
            else:
                tx_list = (response.get("responses") or [{}])[0].get("data", {}).get("txResponses") or []
        except Exception:
            pass

        if not tx_list:
            # Some leagues may return empty txResponses but set status elsewhere; be conservative:
            logger.warning("No txResponses found; assuming drop was accepted (site usually returns EXECUTED here).")
            return True

        tx = tx_list[0]
        code = (tx or {}).get("code", "").upper()
        generic_msg = (tx or {}).get("genericMessage") or ""
        detail_msgs = " ".join((tx or {}).get("detailMessages") or [])
        status_display = ((tx or {}).get("transactionSet") or {}).get("statusDisplay")

        # Fallback: extract GW from detail text if preview missed it
        if drop_period is None:
            drop_period = _extract_gw_from_text(detail_msgs) or _extract_gw_from_text(generic_msg) or _extract_gw_from_text(eff_msg or "")

        # Terminal success (what the browser shows)
        if code == "EXECUTED" or (status_display and status_display.upper() == "EXECUTED"):
            # Scheduled vs immediate
            displayed = _safe_displayed_period(league_id)
            # Prefer comparing against league's displayed period; if unavailable, compare against provided `period`
            scheduled = False
            if drop_period is not None:
                if displayed is not None:
                    scheduled = (drop_period != displayed)
                elif period is not None:
                    scheduled = (drop_period != period)

            if scheduled:
                logger.info(f"Drop accepted and scheduled for GW{drop_period}. {eff_msg or detail_msgs or generic_msg}")
                return True

            # Immediate (same period) — we can try a quick verification like the site’s UI reload,
            # but don’t fail if caching delays keep the player visible.
            time.sleep(1.5)
            try:
                roster = self._api.roster_info(team_id)
                still_there = False
                for row in getattr(roster, "rows", []):
                    if getattr(row, "player", None) and getattr(row.player, "id", None) == scorer_id:
                        still_there = True
                        break
                if still_there:
                    logger.warning("Player still on roster right after EXECUTED; treating as eventual consistency and returning success.")
                else:
                    logger.info("Success: Player removed from roster")
            except Exception as e:
                logger.warning(f"Roster reload after EXECUTED failed ({e}); assuming success.")
            return True

        # Some leagues send WARNING + “effective Gameweek …” but still accept/schedule it.
        if code == "WARNING" and ("effective Gameweek" in detail_msgs or "effective Gameweek" in (eff_msg or "")):
            logger.info(f"Drop accepted with warnings and scheduled for GW{drop_period}. {eff_msg or detail_msgs or generic_msg}")
            return True

        # If we get here, treat as failure and surface the messages.
        raise FantraxException(
            f"Drop failed: code={code or 'UNKNOWN'} "
            f"{(eff_msg or '')} {(detail_msgs or generic_msg or '')}".strip()
        )

    def drop_player_from_all_teams(
        self,
        scorer_id: str,
        period: Optional[int] = None,
        skip_validation: bool = False
    ) -> Dict[str, Dict[str, any]]:
        """Drop a player from all teams that have them rostered.
        
        Args:
            scorer_id: The ID of the player to drop
            period: Optional period/gameweek number. If not provided, uses current period.
            skip_validation: If True, skips validation checks
            
        Returns:
            Dict[str, Dict[str, any]]: Map of team_id to result info containing:
                - success: bool indicating if drop was successful
                - error: error message if drop failed
                - league_id: ID of the league the team is in
                - team_name: name of the team
        """
        results = {}
        
        # Get player details first
        try:
            player_details = self._api._request(
                "getScorerDetails",
                scorers=[{"scorerId": scorer_id}],
                teamId=list(self._api.teams)[0].team_id if self._api.teams else None
            )
            player_name = player_details.get("scorers", [{}])[0].get("name", scorer_id)
            logger.info(f"Attempting to drop player {player_name} (ID: {scorer_id}) from all teams")
        except Exception as e:
            logger.warning(f"Failed to get player details: {e}")
            player_name = scorer_id
        
        # Get current period if not provided
        if period is None:
            try:
                period = self.get_current_period()
                logger.info(f"Using current period: {period}")
            except Exception as e:
                logger.error(f"Failed to get current period: {e}")
                return {}
        
        # Get all teams from all leagues
        for league in self._api.leagues:
            logger.info(f"Checking league: {league.league_id}")
            for team in league.teams:
                result = {
                    "success": False,
                    "error": None,
                    "league_id": league.league_id,
                    "team_name": team.name
                }
                
                try:
                    # Check if player is on this team's roster
                    roster = self._api.roster_info(team.team_id)
                    player_found = False
                    
                    for row in roster.rows:
                        if row.player and row.player.id == scorer_id:
                            player_found = True
                            logger.info(f"Found {player_name} on team {team.name} ({team.team_id})")
                            
                            try:
                                # Attempt to drop
                                self.drop_player(
                                    team.team_id,
                                    scorer_id,
                                    period=period,
                                    skip_validation=skip_validation
                                )
                                result["success"] = True
                                logger.info(f"Successfully dropped {player_name} from {team.name}")
                            except Exception as e:
                                error_msg = str(e)
                                result["error"] = error_msg
                                logger.error(f"Failed to drop {player_name} from {team.name}: {error_msg}")
                            break
                            
                    if not player_found:
                        logger.debug(f"Player {player_name} not found on team {team.name}")
                        continue
                        
                except Exception as e:
                    error_msg = f"Error checking roster for team {team.team_id}: {e}"
                    result["error"] = error_msg
                    logger.error(error_msg)
                
                results[team.team_id] = result
                    
        # Log summary
        successful_drops = sum(1 for r in results.values() if r["success"])
        failed_drops = sum(1 for r in results.values() if not r["success"] and r.get("error"))
        logger.info(f"Drop summary for {player_name}:")
        logger.info(f"- Successful drops: {successful_drops}")
        logger.info(f"- Failed drops: {failed_drops}")
        
        return results
