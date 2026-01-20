import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict

import pytest

from fantraxapi.lineups.conditional_swaps import LineupStatus, PlayerLineupInfo
from utils.conditional_rule_store import load_rules_with_locks


def _load_runner_module():
    module_name = "scripts.conditional_runner"
    if module_name in sys.modules:
        return sys.modules[module_name]
    runtime_path = Path(__file__).resolve().parents[1] / "scripts" / "conditional_runner.py"
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore
    return module


runner = _load_runner_module()


class _DummyPos:
    def __init__(self, short_name: str = "F"):
        self.short_name = short_name


class _DummyPlayer:
    def __init__(self, player_id: str):
        self.id = player_id
        self.name = f"Player {player_id}"


class _DummyRow:
    def __init__(self, player_id: str, is_active: bool):
        self.player = _DummyPlayer(player_id)
        self.pos_id = "1" if is_active else "0"
        self.status_id = "1" if is_active else "0"
        self._raw = {"scorer": {"disableLineupChange": False}}
        self.pos = _DummyPos()


class _DummyRoster:
    def __init__(self, rows):
        self.rows = rows

    def get_starters(self):
        return [row for row in self.rows if row.pos_id != "0" and getattr(row, "player", None)]


def _make_lineup_info(player_id: str, confirmed_status: LineupStatus) -> PlayerLineupInfo:
    return PlayerLineupInfo(
        fantrax_player_id=player_id,
        sofascore_player_id=None,
        status=confirmed_status,
        ss_conf_status=confirmed_status,
    )


def _patch_runner_environment(
    monkeypatch: pytest.MonkeyPatch,
    roster: _DummyRoster,
    lineup_info: Dict[str, PlayerLineupInfo],
):
    class DummyFantraxAPI:
        def __init__(self, league_id, session):
            self.league_id = league_id

        def roster_info(self, team_id, period=None):
            return roster

    class DummyPlayerMappingManager:
        pass

    class DummySubsService:
        swap_calls: list[tuple[str, str, str, int | None]] = []

        def __init__(self, session, league_id):
            self.session = session
            self.league_id = league_id

        def _sniff_period_and_deadline_from_roster(self, roster_obj):
            return (1, None)

        def swap_players(self, team_id, out_player_id, in_player_id, period=None):
            DummySubsService.swap_calls.append((team_id, out_player_id, in_player_id, period))
            return {"success": True}

    def stub_resolve_lineup_info(*args, **kwargs):
        return {pid: info for pid, info in lineup_info.items()}

    monkeypatch.setattr(runner, "FantraxAPI", DummyFantraxAPI)
    monkeypatch.setattr(runner, "SubsService", DummySubsService)
    monkeypatch.setattr(runner, "PlayerMappingManager", DummyPlayerMappingManager)
    monkeypatch.setattr(runner, "global_status_path_for_user", lambda user_id: Path("data/.runner_dummy"))
    monkeypatch.setattr(runner, "_build_session_from_artifacts", lambda path: object())
    monkeypatch.setattr(runner, "infer_current_gameweek", lambda: "")
    monkeypatch.setattr(runner, "resolve_lineup_info", stub_resolve_lineup_info)
    monkeypatch.setattr(runner, "can_swap_in_period", lambda *args, **kwargs: True)
    return DummySubsService


def _build_rule(rule_id: str, active_id: str, reserve_id: str, created_at: str, priority: int = 1) -> Dict[str, str]:
    return {
        "rule_id": rule_id,
        "league_id": "L1",
        "team_id": "T1",
        "period": 1,
        "active_id": active_id,
        "reserve_id": reserve_id,
        "priority": priority,
        "created_at": created_at,
        "state": "pending",
        "fired_count": 0,
        "max_fires": 1,
    }


def _write_rules(payload_path: Path, rules, player_locks=None):
    payload = {"rules": rules, "player_locks": player_locks or {}}
    payload_path.write_text(json.dumps(payload))


def _runner_argv(rules_path: Path) -> list[str]:
    return [
        "conditional_runner.py",
        "--rules-path",
        str(rules_path),
        "--league-id",
        "L1",
        "--team-id",
        "T1",
        "--period",
        "1",
        "--force-trigger",
    ]


def test_inverse_rules_disable_deterministically():
    rules = [
        _build_rule("rule-a", "P1", "P2", created_at="2026-01-01T00:00:00Z"),
        _build_rule("rule-b", "P2", "P1", created_at="2026-01-02T00:00:00Z"),
    ]
    updated = runner._apply_inverse_rule_guard(rules, league_id="L1", team_id="T1")
    assert updated is True
    losers = [r for r in rules if str(r.get("state")) == "disabled"]
    assert len(losers) == 1
    assert losers[0]["rule_id"] == "rule-b"
    assert losers[0]["disabled_reason"] == "inverse_rule_conflict"


def test_runner_prevents_flip_flop_after_first_swap(tmp_path, monkeypatch):
    active_id = "active-1"
    reserve_id = "reserve-1"
    rules = [_build_rule("rule-flip", active_id, reserve_id, created_at="2026-01-01T00:00:00Z")]
    rules_path = tmp_path / "rules_flip.json"
    _write_rules(rules_path, rules)
    roster = _DummyRoster([_DummyRow(active_id, True), _DummyRow(reserve_id, False)])
    lineup_info = {
        active_id: _make_lineup_info(active_id, LineupStatus.BENCH),
        reserve_id: _make_lineup_info(reserve_id, LineupStatus.STARTING),
    }
    DummySubsService = _patch_runner_environment(monkeypatch, roster, lineup_info)
    DummySubsService.swap_calls.clear()
    monkeypatch.setattr(sys, "argv", _runner_argv(rules_path))
    for _ in range(3):
        runner.main()
    assert len(DummySubsService.swap_calls) == 1
    saved = json.loads(rules_path.read_text())
    lock_key = "L1:T1:1"
    assert lock_key in saved["player_locks"]
    assert saved["player_locks"][lock_key].get(active_id)
    assert saved["player_locks"][lock_key].get(reserve_id)


def test_safety_override_bypasses_locked_reserves(tmp_path, monkeypatch):
    active_id = "active-2"
    reserve_id = "reserve-2"
    rules = [_build_rule("rule-safety", active_id, reserve_id, created_at="2026-01-01T00:00:00Z")]
    lock_bucket = {
        "L1:T1:1": {
            reserve_id: {"locked": True, "rule_id": "legacy", "reason": "rule_swap"},
        }
    }
    rules_path = tmp_path / "rules_safety.json"
    _write_rules(rules_path, rules, player_locks=lock_bucket)
    roster = _DummyRoster([_DummyRow(active_id, True), _DummyRow(reserve_id, False)])
    lineup_info = {
        active_id: _make_lineup_info(active_id, LineupStatus.BENCH),
        reserve_id: _make_lineup_info(reserve_id, LineupStatus.STARTING),
    }
    DummySubsService = _patch_runner_environment(monkeypatch, roster, lineup_info)
    DummySubsService.swap_calls.clear()
    monkeypatch.setattr(sys, "argv", _runner_argv(rules_path))
    runner.main()
    assert len(DummySubsService.swap_calls) == 1


def test_rule_marked_satisfied_noop_without_swap(tmp_path, monkeypatch):
    active_id = "active-3"
    reserve_id = "reserve-3"
    rules = [_build_rule("rule-noop", active_id, reserve_id, created_at="2026-01-01T00:00:00Z")]
    rules_path = tmp_path / "rules_noop.json"
    _write_rules(rules_path, rules)
    roster = _DummyRoster([_DummyRow(active_id, False), _DummyRow(reserve_id, True)])
    lineup_info = {
        active_id: _make_lineup_info(active_id, LineupStatus.BENCH),
        reserve_id: _make_lineup_info(reserve_id, LineupStatus.STARTING),
    }
    DummySubsService = _patch_runner_environment(monkeypatch, roster, lineup_info)
    DummySubsService.swap_calls.clear()
    monkeypatch.setattr(sys, "argv", _runner_argv(rules_path))
    runner.main()
    assert not DummySubsService.swap_calls
    rules_after = json.loads(rules_path.read_text())["rules"]
    updated = next(r for r in rules_after if r["rule_id"] == "rule-noop")
    assert updated["result"] == "satisfied_noop"
    assert updated["state"] == "fired"


def test_load_rules_with_locks_accepts_list_payload(tmp_path):
    payload_rules = [
        _build_rule("rule-list-1", "A", "B", created_at="2026-01-01T00:00:00Z"),
        _build_rule("rule-list-2", "B", "C", created_at="2026-01-02T00:00:00Z"),
    ]
    path = tmp_path / "legacy_rules.json"
    path.write_text(json.dumps(payload_rules))
    loaded_rules, locks = load_rules_with_locks(path)
    assert locks == {}
    assert len(loaded_rules) == len(payload_rules)
    assert {r["rule_id"] for r in loaded_rules} == {"rule-list-1", "rule-list-2"}


def test_load_rules_prunes_stale_locks(tmp_path):
    rules = [_build_rule("rule-store", "A1", "B1", created_at="2026-01-01T00:00:00Z")]
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    payload = {
        "rules": rules,
        "player_locks": {
            "L1:T1:1": {
                "A1": {"locked": True, "rule_id": "rule-store", "locked_at": stale_ts}
            },
            "X:Y:1": {"X": {"locked": True, "rule_id": "rule-other", "locked_at": None}},
        },
    }
    path = tmp_path / "rules_prune.json"
    path.write_text(json.dumps(payload))
    _, locks = load_rules_with_locks(path)
    assert locks == {}
