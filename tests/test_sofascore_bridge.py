import json
from datetime import datetime, timezone
from pathlib import Path

from fantraxapi.lineups.sofascore_bridge import (
    _build_sofascore_index,
    _collect_lineup_event_ids,
    _collect_player_lineup_context,
)


def _write_lineup_json(path: Path, event_id: int, player_id: int) -> None:
    payload = {
        "event_id": event_id,
        "confirmed": True,
        "home": {
            "formation": "4-3-3",
            "starters": [
                {
                    "id": player_id,
                    "name": "Test Player",
                    "position": "F",
                    "team_id": 30,
                    "substitute": False,
                    "captain": False,
                }
            ],
            "subs": [],
            "missing": [],
        },
        "away": {
            "formation": "4-4-2",
            "starters": [
                {
                    "id": 999999,
                    "name": "Opponent",
                    "position": "F",
                    "team_id": 60,
                    "substitute": False,
                    "captain": False,
                }
            ],
            "subs": [],
            "missing": [],
        },
        "fetched_at_utc": "2026-01-01 00:00:00+0000",
    }
    path.write_text(json.dumps(payload))


class _DummyPlayer:
    def __init__(self, player_id: str, team_name: str):
        self.id = player_id
        self.team_name = team_name
        self.team_short_name = team_name
        self.next_kickoff = None


class _DummyRow:
    def __init__(self, player):
        self.player = player
        self._raw = {"scorer": {"disableLineupChange": False}}


class _DummyMappingManager:
    def get_by_fantrax_id(self, fantrax_id: str):
        class M:
            sofascore_id = 1400106

        return M()


def test_collect_lineup_event_ids(tmp_path):
    lineups_dir = tmp_path / "lineups"
    lineups_dir.mkdir()
    path = lineups_dir / "15129445.json"
    _write_lineup_json(path, event_id=15129445, player_id=1400106)
    event_ids = _collect_lineup_event_ids(lineups_dir)
    assert 15129445 in event_ids


def test_player_context_fallback_uses_snapshot(tmp_path):
    lineups_dir = tmp_path / "lineups"
    lineups_dir.mkdir()
    event_id = 15129445
    player_id = 1400106
    _write_lineup_json(lineups_dir / f"{event_id}.json", event_id=event_id, player_id=player_id)

    event_index = _build_sofascore_index(
        lineups_dir=lineups_dir,
        schedule_map={},
        allowed_event_ids={event_id},
    )
    schedule_by_team = {}
    event_team_map = {}
    allowed_event_ids = {event_id}

    row = _DummyRow(_DummyPlayer("06c6g", "Brighton & Hove Albion"))
    collected = _collect_player_lineup_context(
        row,
        mapping_manager=_DummyMappingManager(),
        event_index=event_index,
        schedule_by_team=schedule_by_team,
        event_team_map=event_team_map,
        allowed_event_ids=allowed_event_ids,
        now=datetime.now(timezone.utc),
    )
    assert collected is not None
    info, ctx = collected
    snapshot_event_id = ctx.get("event_id")
    assert snapshot_event_id == event_id
    assert info.status != None
