"""
Fantrax lineup synchronization functionality.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import logging

from ..fantrax import Fantrax
from .models import LineupRecord, LineupStatus, PlayerRecord

logger = logging.getLogger(__name__)

class LineupChange:
    """Represents a single lineup change to be made."""
    def __init__(
        self,
        player_in: PlayerRecord,
        player_out: Optional[PlayerRecord] = None,
        reason: str = ""
    ):
        self.player_in = player_in
        self.player_out = player_out
        self.reason = reason
        self.executed = False
        self.error: Optional[str] = None

    @property
    def is_swap(self) -> bool:
        """Whether this is a swap (both in and out) vs just a position change."""
        return bool(self.player_out)

    def __str__(self) -> str:
        if self.is_swap:
            return f"Swap {self.player_out.fantrax_name} -> {self.player_in.fantrax_name}"
        return f"Move {self.player_in.fantrax_name} to starting lineup"

class LineupSynchronizer:
    """
    Handles synchronization of lineups between SofaScore and Fantrax.
    
    This class:
    1. Determines what changes need to be made
    2. Validates changes against lineup rules
    3. Executes changes via Fantrax API
    4. Tracks results and handles errors
    """
    
    def __init__(
        self,
        fantrax: Fantrax,
        dry_run: bool = False,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize synchronizer.
        
        Args:
            fantrax: Fantrax API instance
            dry_run: If True, don't actually make changes
            logger: Optional logger instance
        """
        self.fantrax = fantrax
        self.dry_run = dry_run
        self.logger = logger or logging.getLogger(__name__)
        self.changes: List[LineupChange] = []
        self.errors: List[str] = []

    def determine_changes(self, lineup: LineupRecord) -> List[LineupChange]:
        """
        Determine what changes need to be made to match the given lineup.
        
        Args:
            lineup: Lineup record to sync with
            
        Returns:
            List of LineupChange instances
        """
        changes = []
        
        # Process both teams
        for team in [lineup.home_team, lineup.away_team]:
            # Get current Fantrax lineup for team
            try:
                current = self.fantrax.get_team_lineup(team.team_id)
            except Exception as e:
                self.logger.error(f"Failed to get Fantrax lineup for {team.team_name}: {e}")
                continue
                
            # Map current starters/subs
            current_starters = {p["id"] for p in current["starters"]}
            current_subs = {p["id"] for p in current["subs"]}
            
            # Check each player
            for player in team.players:
                if not player.fantrax_id:
                    self.logger.warning(f"No Fantrax ID for {player.player_name}")
                    continue
                    
                # Determine if change needed
                current_starting = player.fantrax_id in current_starters
                should_start = not player.is_sub
                
                if current_starting != should_start:
                    # Need to make a change
                    if should_start:
                        # Find sub to swap out
                        for p in team.players:
                            if (p.fantrax_id and 
                                p.fantrax_id in current_starters and 
                                p.is_sub):
                                changes.append(LineupChange(
                                    player_in=player,
                                    player_out=p,
                                    reason="Move to starting lineup"
                                ))
                                break
                    else:
                        # Find starter to swap in
                        for p in team.players:
                            if (p.fantrax_id and 
                                p.fantrax_id in current_subs and 
                                not p.is_sub):
                                changes.append(LineupChange(
                                    player_in=p,
                                    player_out=player,
                                    reason="Move to bench"
                                ))
                                break
                                
        return changes

    def validate_changes(self, changes: List[LineupChange]) -> bool:
        """
        Validate proposed changes against lineup rules.
        
        Args:
            changes: List of proposed changes
            
        Returns:
            True if all changes are valid
        """
        # TODO: Implement lineup validation rules
        return True

    def execute_changes(self, changes: List[LineupChange]) -> bool:
        """
        Execute the proposed changes.
        
        Args:
            changes: List of changes to make
            
        Returns:
            True if all changes were successful
        """
        success = True
        
        for change in changes:
            try:
                if self.dry_run:
                    self.logger.info(f"[DRY RUN] Would execute: {change}")
                    change.executed = True
                    continue
                    
                if change.is_swap:
                    # Execute swap
                    self.fantrax.swap_players(
                        player_in_id=change.player_in.fantrax_id,
                        player_out_id=change.player_out.fantrax_id
                    )
                else:
                    # Just move to starting lineup
                    self.fantrax.move_to_lineup(
                        player_id=change.player_in.fantrax_id
                    )
                    
                change.executed = True
                self.logger.info(f"Executed: {change}")
                
            except Exception as e:
                change.error = str(e)
                self.logger.error(f"Failed to execute {change}: {e}")
                success = False
                
        return success

    def sync_lineup(self, lineup: LineupRecord) -> bool:
        """
        Synchronize Fantrax lineups with given lineup record.
        
        Args:
            lineup: Lineup to sync with
            
        Returns:
            True if sync was successful
        """
        # Only sync confirmed lineups
        if not lineup.is_confirmed:
            self.logger.warning("Cannot sync unconfirmed lineup")
            return False
            
        # Determine needed changes
        changes = self.determine_changes(lineup)
        if not changes:
            self.logger.info("No changes needed")
            return True
            
        # Validate changes
        if not self.validate_changes(changes):
            self.logger.error("Invalid changes detected")
            return False
            
        # Execute changes
        success = self.execute_changes(changes)
        
        # Store changes for reference
        self.changes.extend(changes)
        
        return success
