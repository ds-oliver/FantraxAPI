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
from datetime import datetime
from pathlib import Path
from configparser import ConfigParser
from typing import Dict, List, Set
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

class LineupOptimizer:
    def __init__(self, league_id: str, team_id: str, cookie_path: str):
        self.league_id = league_id
        self.team_id = team_id
        self.cookie_path = cookie_path
        self.session = self._init_session()
        self.api = FantraxAPI(league_id, session=self.session)
        self.starting_players: Set[str] = set()  # Set of player IDs who are starting
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

    def get_starting_players(self) -> Set[str]:
        """Fetch and parse the starting players page"""
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
            
            # Parse response and extract starting player IDs
            starting_ids = set()
            if "statsTable" in data["responses"][0]["data"]:
                for player in data["responses"][0]["data"]["statsTable"]:
                    if player.get("starter"):  # Need to confirm actual field name
                        starting_ids.add(player["scorerId"])
            
            self.starting_players = starting_ids
            self.last_check_time = datetime.now()
            
            logging.info(f"Found {len(starting_ids)} starting players")
            return starting_ids
            
        except Exception as e:
            logging.error(f"Error fetching starting players: {e}")
            return set()

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

    def is_valid_swap(self, starter, bench, current_formation: Formation) -> bool:
        """Check if swapping these players maintains a legal formation
        
        Args:
            starter: Player to move to bench
            bench: Player to move to starting lineup
            current_formation: Current formation before swap
        
        Returns:
            bool: True if swap maintains a legal 11-player formation
        """
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

        # Check if new formation is legal (maintains 11 players and position limits)
        is_legal = new_formation.is_legal()
        if not is_legal:
            logging.debug(f"Invalid formation after swap: {new_formation} (must have exactly 11 players)")
        return is_legal

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

            # Get latest starting players
            starting_players = self.get_starting_players()
            
            # Track needed swaps
            swaps = []
            
            # Check starters who aren't in starting lineup
            for starter in roster.get_starters():
                if starter.player and starter.player.id not in starting_players:
                    # Find eligible bench replacement
                    for bench in roster.get_bench_players():
                        if (bench.player and 
                            bench.player.id in starting_players and
                            self.is_valid_swap(starter, bench, current_formation)):
                            swaps.append((starter, bench))
                            break

            # Execute swaps
            for starter, bench in swaps:
                try:
                    logging.info(f"Swapping {starter.player.name} with {bench.player.name}")
                    success = self.api.swap_players(self.team_id, starter.player.id, bench.player.id)
                    if success:
                        logging.info("Swap successful")
                    else:
                        logging.error("Swap failed")
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
