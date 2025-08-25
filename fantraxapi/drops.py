"""Service for handling player drops in Fantrax."""
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
        skip_validation: bool = False
    ) -> bool:
        """Drop a player from a team's roster.
        
        Args:
            team_id: The ID of the team dropping the player
            scorer_id: The ID of the player to drop
            period: Optional period/gameweek number. If not provided, uses current period.
            skip_validation: If True, skips validation checks
            
        Returns:
            bool: True if the drop was successful
            
        Raises:
            FantraxException: If the drop fails
        """
        if period is None:
            period = self.get_current_period()

        if not skip_validation:
            self.validate_drop(team_id, scorer_id, period)

        # Get player details
        player_details = self._api._request(
            "getScorerDetails",
            scorers=[{"scorerId": scorer_id}],
            teamId=team_id
        )

        # Confirm drop
        confirm_data = {
            "transactionSets": [{
                "transactions": [{
                    "type": "DROP",
                    "scorerId": scorer_id,
                    "teamId": team_id
                }]
            }]
        }
        
        try:
            # Log initial roster state
            initial_roster = self._api.roster_info(team_id)
            logger.info(f"Initial roster state before drop - team {team_id}:")
            for row in initial_roster.rows:
                if row.player:
                    raw_data = getattr(row, '_raw', {})
                    logger.info(f"  Player: {row.player.name} ({row.player.id})")
                    logger.info(f"  Position: {getattr(row.pos, 'short_name', 'unknown')}")
                    logger.info(f"  Status: {raw_data.get('statusId', 'unknown')}")
                    logger.info(f"  Lock state: isLocked={raw_data.get('isLocked')}, "
                              f"locked={raw_data.get('locked')}, lineupLocked={raw_data.get('lineupLocked')}")
                    if raw_data.get('lockedReason'):
                        logger.info(f"  Lock reason: {raw_data.get('lockedReason')}")

            # First get confirmation info
            logger.info(f"Requesting drop confirmation for player {scorer_id}")
            confirm_response = self._api._request("getClaimDropConfirmInfo", **confirm_data)
            logger.info(f"Drop confirmation response: {confirm_response}")
            
            # Then execute the initial drop request
            logger.info(f"Executing initial drop request for player {scorer_id}")
            response = self._api._request("createClaimDrop", **confirm_data)
            logger.info(f"Initial drop execution response: {response}")
            
            # Check for warnings that need confirmation
            if response and "txResponses" in response:
                for tx_response in response["txResponses"]:
                    if tx_response.get("confirm") and tx_response.get("transactionId"):
                        logger.info(f"Drop requires confirmation. Transaction ID: {tx_response['transactionId']}")
                        logger.info(f"Warning messages: {tx_response.get('detailMessages', [])}")
                        
                        # Send confirmation request
                        confirm_tx_data = {
                            "transactionId": tx_response["transactionId"],
                            "confirm": True
                        }
                        logger.info("Sending drop confirmation request")
                        confirm_response = self._api._request("confirmTransaction", **confirm_tx_data)
                        logger.info(f"Drop confirmation response: {confirm_response}")
                        
                        # Check confirmation response
                        if not confirm_response:
                            logger.error("Drop confirmation returned empty response")
                            raise FantraxException("No response received from drop confirmation")
                        if "error" in confirm_response:
                            logger.error(f"Drop confirmation contains error: {confirm_response['error']}")
                            raise FantraxException(f"Drop confirmation failed: {confirm_response['error']}")
                        
                        # Original response is no longer relevant, use confirmation response
                        response = confirm_response
            
            # Verify the drop was successful by checking the response
            if not response:
                logger.error("Drop request returned empty response")
                raise FantraxException("No response received from drop request")
                
            # Check for error messages in response
            if "error" in response:
                logger.error(f"Drop response contains error: {response['error']}")
                raise FantraxException(f"Drop failed: {response['error']}")
                
            # Add delay before verification
            import time
            logger.info("Waiting 2 seconds before verifying roster update...")
            time.sleep(2)
                
            # Verify player is no longer on roster
            logger.info(f"Verifying roster state after drop - team {team_id}")
            roster = self._api.roster_info(team_id)
            player_found = False
            for row in roster.rows:
                if row.player and row.player.id == scorer_id:
                    player_found = True
                    raw_data = getattr(row, '_raw', {})
                    logger.error(f"Player still on roster after drop:")
                    logger.error(f"  Player: {row.player.name} ({row.player.id})")
                    logger.error(f"  Position: {getattr(row.pos, 'short_name', 'unknown')}")
                    logger.error(f"  Status: {raw_data.get('statusId', 'unknown')}")
                    logger.error(f"  Lock state: isLocked={raw_data.get('isLocked')}, "
                               f"locked={raw_data.get('locked')}, lineupLocked={raw_data.get('lineupLocked')}")
                    if raw_data.get('lockedReason'):
                        logger.error(f"  Lock reason: {raw_data.get('lockedReason')}")
                    raise FantraxException("Player still on roster after drop attempt")
            
            if not player_found:
                logger.info("Success: Player no longer found on roster")
            return True
            
        except Exception as e:
            raise FantraxException(f"Failed to drop player: {e}")

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
