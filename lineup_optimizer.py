#!/usr/bin/env python3
"""
Script to automatically optimize lineups based on confirmed starting players.
Checks Fantrax starting players page and adjusts lineups accordingly.
"""

import os
import time
import json
import pickle
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from configparser import ConfigParser
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass
from requests import Session
from fantraxapi import FantraxAPI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('lineup_optimizer.log'),
        logging.StreamHandler()
    ]
)

@dataclass
class Formation:
    gk: int
    def_: int
    mid: int
    fwd: int

    def is_legal(self) -> bool:
        """Check if formation meets requirements:
        - Exactly 11 players total
        - 1 GK
        - Between 3-5 DEF
        - Between 2-5 MID
        - Between 1-3 FWD
        """
        total_players = self.gk + self.def_ + self.mid + self.fwd
        return (
            total_players == 11 and  # Must have exactly 11 players
            self.gk == 1 and
            3 <= self.def_ <= 5 and
            2 <= self.mid <= 5 and
            1 <= self.fwd <= 3
        )

    def __str__(self) -> str:
        return f"{self.gk}-{self.def_}-{self.mid}-{self.fwd}"

@dataclass
class PlayerStatus:
    """Represents a player's current status for lineup decisions"""
    is_starting: bool
    game_time: Optional[datetime]
    is_locked: bool
    opponent: Optional[str]

class LineupOptimizer:
    def __init__(self, league_id: str, team_id: str, cookie_path: str):
        self.league_id = league_id
        self.team_id = team_id
        self.cookie_path = cookie_path
        self.session = self._init_session()
        self.api = FantraxAPI(league_id, session=self.session)
        self.player_statuses: Dict[str, PlayerStatus] = {}  # player_id -> PlayerStatus
        self.last_check_time = None

    def _init_session(self) -> Session:
        """Initialize session with cookies"""
        session = Session()
        try:
            with open(self.cookie_path, "rb") as f:
                for cookie in pickle.load(f):
                    session.cookies.set(cookie["name"], cookie["value"])
            logging.info("Cookie session loaded successfully")
            return session
        except Exception as e:
            logging.error(f"Failed to load cookies: {e}")
            raise

    def update_player_statuses(self) -> None:
        """Fetch and parse the starting players page to update player statuses"""
        try:
            # Make request to get starting players
            response = self.session.get(
                "https://www.fantrax.com/fxpa/req",
                params={
                    "leagueId": self.league_id,
                    "statusOrTeamFilter": "ALL",
                    "miscDisplayType": "10",  # Starting players
                    "pageNumber": "1"
                }
            )
            data = response.json()
            
            # Get current time in UTC
            now = datetime.now(timezone.utc)
            self.last_check_time = now
            
            # Clear existing statuses
            self.player_statuses.clear()
            
            # Parse response and update player statuses
            if "statsTable" in data["responses"][0]["data"]:
                for player in data["responses"][0]["data"]["statsTable"]:
                    player_id = player["scorerId"]
                    
                    # Parse game time if available
                    game_time = None
                    opponent = None
                    if "opponent" in player:
                        # Example: "vs MUN<br/>4:00PM" or similar
                        opp_info = player["opponent"].split("<br/>")
                        if len(opp_info) == 2:
                            opponent = opp_info[0]
                            try:
                                # Convert game time string to datetime
                                time_str = opp_info[1]
                                game_time = datetime.strptime(f"{now.date()} {time_str}", "%Y-%m-%d %I:%M%p")
                                game_time = game_time.replace(tzinfo=timezone.utc)
                            except ValueError:
                                logging.warning(f"Could not parse game time: {opp_info[1]}")
                    
                    # Determine if player is locked
                    is_locked = False
                    if game_time:
                        # Lock players 1 minute before game time
                        if now >= (game_time - timedelta(minutes=1)):
                            is_locked = True
                    
                    self.player_statuses[player_id] = PlayerStatus(
                        is_starting=bool(player.get("starter")),  # Confirm actual field name
                        game_time=game_time,
                        is_locked=is_locked,
                        opponent=opponent
                    )
            
            starting_count = sum(1 for status in self.player_statuses.values() if status.is_starting)
            logging.info(f"Found {starting_count} starting players")
            
        except Exception as e:
            logging.error(f"Error updating player statuses: {e}")
            
    def get_player_status(self, player_id: str) -> Optional[PlayerStatus]:
        """Get status for a specific player"""
        return self.player_statuses.get(player_id)

    def get_current_formation(self, roster) -> Formation:
        """Calculate current formation from roster"""
        gk = def_ = mid = fwd = 0
        for row in roster.get_starters():
            pos = row.pos.short_name
            if pos == "G":
                gk += 1
            elif pos == "D":
                def_ += 1
            elif pos == "M":
                mid += 1
            elif pos == "F":
                fwd += 1
        return Formation(gk, def_, mid, fwd)

    def get_position_counts(self, roster_rows) -> Dict[str, int]:
        """Get counts of each position from a list of roster rows"""
        counts = {"G": 0, "D": 0, "M": 0, "F": 0}
        for row in roster_rows:
            if row.player:  # Only count rows with actual players
                pos = row.pos.short_name
                if pos in counts:
                    counts[pos] += 1
        return counts

    def can_swap_players(self, starter, bench, current_formation: Formation) -> Tuple[bool, str]:
        """Check if two players can be swapped based on formation and game status
        
        Args:
            starter: Player to move to bench
            bench: Player to move to starting lineup
            current_formation: Current formation before swap
        
        Returns:
            Tuple[bool, str]: (can_swap, reason)
            - can_swap: True if players can be swapped
            - reason: Explanation if swap is not allowed
        """
        # Check if starter is locked
        starter_status = self.get_player_status(starter.player.id)
        if starter_status and starter_status.is_locked:
            return False, f"Cannot move {starter.player.name} to bench - player is locked (game in progress)"

        # Check if bench player is locked
        bench_status = self.get_player_status(bench.player.id)
        if bench_status and bench_status.is_locked:
            return False, f"Cannot move {bench.player.name} to starting lineup - player is locked (game in progress)"
            
        # Create new formation after swap
        new_formation = Formation(
            current_formation.gk,
            current_formation.def_,
            current_formation.mid,
            current_formation.fwd
        )
        
        # Adjust counts based on positions
        if starter.pos.short_name == "G":
            new_formation.gk -= 1
        elif starter.pos.short_name == "D":
            new_formation.def_ -= 1
        elif starter.pos.short_name == "M":
            new_formation.mid -= 1
        elif starter.pos.short_name == "F":
            new_formation.fwd -= 1
            
        if bench.pos.short_name == "G":
            new_formation.gk += 1
        elif bench.pos.short_name == "D":
            new_formation.def_ += 1
        elif bench.pos.short_name == "M":
            new_formation.mid += 1
        elif bench.pos.short_name == "F":
            new_formation.fwd += 1

        # Check if new formation is legal
        if not new_formation.is_legal():
            return False, f"Invalid formation after swap: {new_formation} (must have exactly 11 players with valid position counts)"
            
        return True, "Swap is valid"

    def optimize_lineup(self):
        """Main optimization logic"""
        try:
            # Get current roster
            roster = self.api.roster_info(self.team_id)
            current_formation = self.get_current_formation(roster)
            logging.info(f"Current formation: {current_formation}")

            # Validate current formation has exactly 11 players
            if not current_formation.is_legal():
                logging.error(f"Current formation {current_formation} is invalid! Must have exactly 11 players with valid position counts.")
                return

            # Update player statuses
            self.update_player_statuses()
            
            # Track needed swaps
            swaps = []
            
            # Check starters who aren't in starting lineup
            for starter in roster.get_starters():
                if not starter.player:
                    continue
                    
                starter_status = self.get_player_status(starter.player.id)
                if not starter_status or not starter_status.is_starting:
                    # Find eligible bench replacement
                    for bench in roster.get_bench_players():
                        if not bench.player:
                            continue
                            
                        bench_status = self.get_player_status(bench.player.id)
                        if bench_status and bench_status.is_starting:
                            # Check if swap is valid
                            can_swap, reason = self.can_swap_players(starter, bench, current_formation)
                            if can_swap:
                                swaps.append((starter, bench))
                                logging.info(f"Planning swap: {starter.player.name} ({starter.pos.short_name}) -> bench, "
                                          f"{bench.player.name} ({bench.pos.short_name}) -> starting")
                                break
                            else:
                                logging.debug(f"Invalid swap ({starter.player.name} <-> {bench.player.name}): {reason}")

            if not swaps:
                logging.info("No valid swaps found")
                return

            # Execute swaps
            for starter, bench in swaps:
                try:
                    starter_status = self.get_player_status(starter.player.id)
                    bench_status = self.get_player_status(bench.player.id)
                    
                    logging.info(f"Swapping {starter.player.name} (vs {starter_status.opponent if starter_status else 'Unknown'}) "
                               f"with {bench.player.name} (vs {bench_status.opponent if bench_status else 'Unknown'})")
                    
                    success = self.api.swap_players(self.team_id, starter.player.id, bench.player.id)
                    if success:
                        logging.info("✅ Swap successful")
                    else:
                        logging.error("❌ Swap failed")
                except Exception as e:
                    logging.error(f"Error making swap: {e}")

        except Exception as e:
            logging.error(f"Error in optimize_lineup: {e}")

def load_config():
    """Load configuration from config.ini"""
    config = ConfigParser()
    config_path = Path("config.ini")
    
    if not config_path.exists():
        raise FileNotFoundError("config.ini not found!")
    
    config.read(config_path)
    if "fantrax" not in config:
        raise KeyError("config.ini must have a [fantrax] section!")
        
    return {
        "league_id": config["fantrax"]["league_id"],
        "team_id": config["fantrax"]["team_id"],
        "cookie_path": config["fantrax"]["cookie_path"]
    }

def main():
    # Load configuration
    config = load_config()
    
    # Initialize optimizer
    optimizer = LineupOptimizer(
        league_id=config["league_id"],
        team_id=config["team_id"],
        cookie_path=config["cookie_path"]
    )
    
    # Run continuous optimization loop
    check_interval = 300  # 5 minutes
    while True:
        try:
            logging.info("Running lineup optimization...")
            optimizer.optimize_lineup()
            
            logging.info(f"Sleeping for {check_interval} seconds...")
            time.sleep(check_interval)
            
        except KeyboardInterrupt:
            logging.info("Optimization stopped by user")
            break
        except Exception as e:
            logging.error(f"Error in main loop: {e}")
            time.sleep(60)  # Wait a minute before retrying

if __name__ == "__main__":
    main()
