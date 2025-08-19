import os, sys, time, unittest
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fantraxapi import FantraxAPI

"""
import logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler())
"""

load_dotenv()

league_id = os.environ["LEAGUE_ID"]
local = os.environ["LOCAL"] == "True"
py_version = f"{sys.version_info.major}.{sys.version_info.minor}"

team_names = [
    "Bunch of Yahoos",
    "Carry Me Ovi",
    "Dude Where’s Makar?",
    "Former Ice Dancers",
    "Girouxsalem",
    "High and Draisaitl",
    "Kashyyyk Wookies 🏴‍☠️",
    "Momma Ain't Raise No Bitch",
    "Rantanen With The Devil",
    "Sonk squad",
    "Tage Against The Machine",
    "Tease McBulge"
]

class APITests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.api = FantraxAPI(league_id)

    def test_teams(self):
        for team in self.api.teams:
            self.assertIn(team.name, team_names)
            
    def test_lineup_changes(self):
        # Get a team's roster
        team = self.api.teams[0]
        roster = self.api.roster_info(team.team_id)
        
        # Find a player on reserve and one active
        reserve_player = None
        active_player = None
        for row in roster.rows:
            if row.player:
                if row.pos.short_name == "Res" and not reserve_player:
                    reserve_player = row.player
                elif row.pos.short_name != "Res" and not active_player:
                    active_player = row.player
                if reserve_player and active_player:
                    break
                    
        if reserve_player and active_player:
            # Test single moves
            response = roster.move_to_active(reserve_player.id)
            self.assertEqual(response.get("fantasyResponse", {}).get("msgType"), "CONFIRM")
            
            response = roster.move_to_reserve(active_player.id)
            self.assertEqual(response.get("fantasyResponse", {}).get("msgType"), "CONFIRM")
            
            # Test batch moves
            moves = [
                {"player_id": reserve_player.id, "move_type": "RESERVE_TO_ACTIVE"},
                {"player_id": active_player.id, "move_type": "ACTIVE_TO_RESERVE"}
            ]
            response = roster.batch_moves(moves)
            self.assertEqual(response.get("fantasyResponse", {}).get("msgType"), "CONFIRM")

