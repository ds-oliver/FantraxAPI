"""
Lineup status management functionality.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import LineupRecord, LineupStatus

# Constants
CONFIRMATION_WINDOW_MINUTES = 75  # 1h15m before kickoff
FINAL_WINDOW_MINUTES = 5  # 5m after kickoff

def determine_lineup_status(
    lineup: LineupRecord,
    current_time: Optional[datetime] = None
) -> LineupStatus:
    """
    Determine the appropriate status for a lineup based on timing and confirmation.
    
    Args:
        lineup: Lineup record to check
        current_time: Current time (defaults to now)
    
    Returns:
        Appropriate LineupStatus value
    """
    if not current_time:
        current_time = datetime.now(timezone.utc)
        
    # Calculate time windows
    time_to_kickoff = lineup.kickoff_utc - current_time
    confirmation_window = timedelta(minutes=CONFIRMATION_WINDOW_MINUTES)
    final_window = timedelta(minutes=FINAL_WINDOW_MINUTES)
    
    # Match has started
    if current_time > lineup.kickoff_utc + final_window:
        return LineupStatus.FINAL
        
    # Within confirmation window
    if time_to_kickoff <= confirmation_window:
        # Check if lineup is actually confirmed
        if getattr(lineup, 'is_confirmed', False):
            return LineupStatus.CONFIRMED
        return LineupStatus.PENDING_CONFIRMATION
        
    # Outside confirmation window
    return LineupStatus.PRELIMINARY

def validate_lineup_status(lineup: LineupRecord) -> bool:
    """
    Validate that a lineup's current status is appropriate.
    
    Args:
        lineup: Lineup record to validate
    
    Returns:
        True if status is valid, False otherwise
    """
    expected_status = determine_lineup_status(lineup)
    
    # Status is valid if it matches expected or is FINAL/INVALID
    return (
        lineup.status == expected_status or
        lineup.status in (LineupStatus.FINAL, LineupStatus.INVALID)
    )

def update_lineup_status(
    lineup: LineupRecord,
    current_time: Optional[datetime] = None
) -> bool:
    """
    Update a lineup's status based on current timing.
    
    Args:
        lineup: Lineup record to update
        current_time: Current time (defaults to now)
    
    Returns:
        True if status was changed, False otherwise
    """
    new_status = determine_lineup_status(lineup, current_time)
    
    if new_status != lineup.status:
        lineup.update_status(new_status)
        return True
        
    return False
