"""
Conditional swap rules and evaluation engine.

Implements the backend side of the "conditional active ↔ reserve swaps"
feature without altering the existing substitution logic.  Rules are stored
in JSON, evaluated with SofaScore lineup data, and executed by reusing the
current SubsService plumbing.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field, validator

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.objs import Roster, RosterRow
from fantraxapi.subs import SubsService

log = logging.getLogger(__name__)


def generate_rule_id() -> str:
    """Return a deterministic-looking identifier for a rule."""
    return uuid.uuid4().hex


class LineupStatus(str, Enum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    BENCH = "bench"
    OUT = "out"
    DOUBTFUL = "doubtful"


@dataclass
class PlayerLineupInfo:
    fantrax_player_id: str
    sofascore_player_id: Optional[int]
    # Effective view used by the engine/UI
    status: LineupStatus = LineupStatus.UNKNOWN
    kickoff: Optional[datetime] = None  # UTC
    status_source: Optional[str] = None  # "sofascore", "fantrax", or None

    # Source-specific views
    ss_status: Optional[LineupStatus] = None
    ss_kickoff: Optional[datetime] = None
    fx_status: Optional[LineupStatus] = None
    fx_kickoff: Optional[datetime] = None

    # Fixture metadata (display-oriented)
    event_id: Optional[int] = None
    team_name: Optional[str] = None
    opponent_name: Optional[str] = None
    is_home: Optional[bool] = None


class SwapCondition(str, Enum):
    NOT_STARTING = "not_starting"


class RuleState(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class BackupOption(BaseModel):
    reserve_player_id: str
    priority: int = Field(gt=0)

    class Config:
        frozen = True


class ConditionalSwapRule(BaseModel):
    id: str = Field(default_factory=generate_rule_id)
    league_id: str
    team_id: str

    active_player_id: str
    backups: List[BackupOption]

    condition: SwapCondition = SwapCondition.NOT_STARTING

    period_id: str
    period_label: str

    enforce_kickoff_order: bool = True
    max_fires_per_period: int = Field(default=1, ge=1)

    state: RuleState = RuleState.ACTIVE

    class Config:
        use_enum_values = True

    @validator("backups")
    def _validate_backups(cls, value: Sequence[BackupOption]) -> Sequence[BackupOption]:
        if not value:
            raise ValueError("At least one backup must be provided.")
        seen = set()
        for opt in value:
            key = opt.reserve_player_id
            if key == "":
                raise ValueError("Empty reserve player id.")
            if key in seen:
                raise ValueError("Backups must be unique per rule.")
            seen.add(key)
        return value

    def sorted_backups(self) -> List[BackupOption]:
        return sorted(self.backups, key=lambda opt: opt.priority)

    def primary_key(self) -> Tuple[str, str, str, str]:
        return (self.league_id, self.team_id, self.period_id, self.active_player_id)


class RosterView:
    """
    Thin utility around the Fantrax roster payload that exposes helpers needed
    by the engine.  It intentionally reuses the SubsService locking heuristics.
    """

    def __init__(self, roster: Roster):
        self.roster = roster
        self._row_by_player: Dict[str, RosterRow] = {
            r.player.id: r for r in roster.rows if getattr(r, "player", None)
        }

    def _row(self, player_id: str) -> Optional[RosterRow]:
        return self._row_by_player.get(player_id)

    def is_active(self, player_id: str) -> bool:
        row = self._row(player_id)
        return bool(row and getattr(row, "pos_id", "0") != "0")

    def is_reserve(self, player_id: str) -> bool:
        row = self._row(player_id)
        return bool(row and getattr(row, "pos_id", "0") == "0")

    def is_locked(
        self,
        player_id: str,
        *,
        now: Optional[datetime] = None,
        lineup_info_by_player: Optional[Dict[str, "PlayerLineupInfo"]] = None,
    ) -> bool:
        """
        Strict Fantrax lock check for rule execution (does not include kickoff-based heuristics).
        """
        row = self._row(player_id)
        if not row:
            return False
        return is_row_locked(
            row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )

    def lock_flags(
        self,
        player_id: str,
        *,
        now: Optional[datetime] = None,
        lineup_info_by_player: Optional[Dict[str, "PlayerLineupInfo"]] = None,
    ) -> Dict[str, bool]:
        row = self._row(player_id)
        if not row:
            return {
                "fx_locked": False,
                "kickoff_passed": False,
                "finished_marker": False,
                "visually_locked": False,
            }
        return get_row_lock_flags(
            row,
            now=now,
            lineup_info_by_player=lineup_info_by_player,
        )

    def get_row(self, player_id: str) -> Optional[RosterRow]:
        return self._row(player_id)

    def active_player_ids(self) -> List[str]:
        return [pid for pid, row in self._row_by_player.items() if getattr(row, "pos_id", "0") != "0"]

    def reserve_player_ids(self) -> List[str]:
        return [pid for pid, row in self._row_by_player.items() if getattr(row, "pos_id", "0") == "0"]


def is_row_locked(
    row: RosterRow,
    *,
    now: Optional[datetime] = None,  # kept for API compatibility; not used
    lineup_info_by_player: Optional[Dict[str, "PlayerLineupInfo"]] = None,  # also unused
) -> bool:
    """
    Determine if a roster row is locked and cannot be changed.

    For EPL conditional swaps we now define "locked" strictly as:
      - Fantrax scorer.disableLineupChange == true

    We intentionally ignore:
      - Finished-match markers in the cells (e.g. "... F")
      - SofaScore kickoff timestamps

    Those can still be used for *display* and debugging, but not to decide
    whether Fantrax will allow a lineup change.
    """
    raw = getattr(row, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}

    return bool(scorer.get("disableLineupChange") is True)


def get_row_lock_flags(
    row: RosterRow,
    *,
    now: Optional[datetime] = None,
    lineup_info_by_player: Optional[Dict[str, "PlayerLineupInfo"]] = None,
) -> Dict[str, bool]:
    """
    Return lock-related flags for UI/diagnostics.
      - fx_locked: Fantrax disableLineupChange
      - finished_marker: first cell contains finished marker
      - kickoff_passed: SofaScore kickoff exists and is in the past
      - visually_locked: aggregate flag for UI styling (any of the above)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    raw = getattr(row, "_raw", {}) or {}
    scorer = raw.get("scorer") or {}
    fx_locked = bool(scorer.get("disableLineupChange") is True)

    finished_marker = False
    cells = raw.get("cells") or []
    if cells:
        content = (cells[0].get("content") or "").upper()
        if " F" in content or content.endswith(" F"):
            finished_marker = True

    kickoff_passed = False
    player = getattr(row, "player", None)
    player_id = str(getattr(player, "id", "")) if player else None
    if player_id and lineup_info_by_player is not None:
        info = lineup_info_by_player.get(player_id)
        if info and info.kickoff and now >= info.kickoff:
            kickoff_passed = True

    visually_locked = fx_locked or finished_marker or kickoff_passed

    log.debug(
        "[conditional-swaps] lock flags for %s: fx_locked=%s kickoff_passed=%s finished_marker=%s",
        player_id,
        fx_locked,
        kickoff_passed,
        finished_marker,
    )

    return {
        "fx_locked": fx_locked,
        "kickoff_passed": kickoff_passed,
        "finished_marker": finished_marker,
        "visually_locked": visually_locked,
    }


class RuleStorage:
    """
    Simple JSON-backed storage for conditional swap rules.
    The file contains a list of objects compatible with ConditionalSwapRule.
    """

    def __init__(self, storage_path: Optional[Path] = None):
        if storage_path is None:
            storage_path = Path("data/lineups/conditional_swap_rules.json")
        self.storage_path = storage_path
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_all(self) -> List[dict]:
        if not self.storage_path.exists():
            return []
        try:
            return json.loads(self.storage_path.read_text())
        except Exception:
            log.exception("[conditional-swaps] Failed reading rule storage")
            return []

    def _write_all(self, data: List[dict]) -> None:
        self.storage_path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def load_rules_for_team(self, league_id: str, team_id: str) -> List[ConditionalSwapRule]:
        all_rules = self._load_all()
        rules = []
        for raw in all_rules:
            try:
                rule = ConditionalSwapRule.parse_obj(raw)
            except Exception:
                log.exception("[conditional-swaps] Invalid rule encountered; skipping")
                continue
            if rule.league_id == league_id and rule.team_id == team_id:
                rules.append(rule)
        return rules

    def _replace_rules(self, new_rules: Iterable[ConditionalSwapRule]) -> None:
        payload = [rule.dict() for rule in new_rules]
        self._write_all(payload)

    def save_rule(self, rule: ConditionalSwapRule) -> None:
        rules = self._load_all()
        existing = [
            ConditionalSwapRule.parse_obj(raw)
            for raw in rules
            if isinstance(raw, dict)
        ]
        if any(r.primary_key() == rule.primary_key() for r in existing):
            raise ValueError(
                "A rule already exists for this league/team/period/active player."
            )
        existing.append(rule)
        self._replace_rules(existing)

    def disable_rule(self, rule_id: str) -> None:
        self._update_rule_state(rule_id, RuleState.DISABLED)

    def enable_rule(self, rule_id: str) -> None:
        self._update_rule_state(rule_id, RuleState.ACTIVE)

    def _update_rule_state(self, rule_id: str, state: RuleState) -> None:
        updated = []
        changed = False
        for raw in self._load_all():
            try:
                rule = ConditionalSwapRule.parse_obj(raw)
            except Exception:
                continue
            if rule.id == rule_id:
                changed = True
                rule = rule.copy(update={"state": state.value})
            updated.append(rule)
        if changed:
            self._replace_rules(updated)

    def delete_rule(self, rule_id: str) -> None:
        remaining: List[ConditionalSwapRule] = []
        for raw in self._load_all():
            try:
                rule = ConditionalSwapRule.parse_obj(raw)
            except Exception:
                continue
            if rule.id != rule_id:
                remaining.append(rule)
        self._replace_rules(remaining)


@dataclass
class RuleEvaluationResult:
    rule: ConditionalSwapRule
    should_fire: bool
    chosen_backup_id: Optional[str] = None
    reason: Optional[str] = None


class FireTracker:
    """Tracks how many times a rule has fired per period."""

    def __init__(self):
        self._counts: Dict[Tuple[str, str], int] = {}

    def get(self, rule: ConditionalSwapRule) -> int:
        key = (rule.id, str(rule.period_id))
        return self._counts.get(key, 0)

    def increment(self, rule: ConditionalSwapRule) -> None:
        key = (rule.id, str(rule.period_id))
        self._counts[key] = self.get(rule) + 1


class ConditionalSwapEngine:
    """
    Evaluates tiered backup rules and determines whether any should trigger.
    """

    def __init__(self, subs_service: SubsService, fire_tracker: Optional[FireTracker] = None):
        self.subs_service = subs_service
        self.fire_tracker = fire_tracker or FireTracker()

    def evaluate_rules(
        self,
        *,
        rules: Sequence[ConditionalSwapRule],
        roster_view: RosterView,
        lineup_info_by_player: Dict[str, PlayerLineupInfo],
        now: datetime,
        current_period_id: str,
    ) -> List[RuleEvaluationResult]:
        results: List[RuleEvaluationResult] = []
        for rule in rules:
            result = self._evaluate_single_rule(
                rule=rule,
                roster_view=roster_view,
                lineup_info_by_player=lineup_info_by_player,
                now=now,
                current_period_id=current_period_id,
            )
            results.append(result)
        return results

    def _evaluate_single_rule(
        self,
        *,
        rule: ConditionalSwapRule,
        roster_view: RosterView,
        lineup_info_by_player: Dict[str, PlayerLineupInfo],
        now: datetime,
        current_period_id: str,
    ) -> RuleEvaluationResult:
        if rule.state != RuleState.ACTIVE:
            return RuleEvaluationResult(rule, False, reason="rule_disabled")

        if rule.period_id != str(current_period_id):
            return RuleEvaluationResult(rule, False, reason="period_mismatch")

        if self.fire_tracker.get(rule) >= rule.max_fires_per_period:
            return RuleEvaluationResult(rule, False, reason="fire_limit_reached")

        if not roster_view.is_active(rule.active_player_id):
            return RuleEvaluationResult(rule, False, reason="active_not_active")

        if roster_view.is_locked(rule.active_player_id):
            return RuleEvaluationResult(rule, False, reason="active_locked")

        info_active = lineup_info_by_player.get(rule.active_player_id)
        if not info_active or info_active.status == LineupStatus.UNKNOWN:
            return RuleEvaluationResult(rule, False, reason="status_unknown")

        if info_active.status == LineupStatus.STARTING:
            return RuleEvaluationResult(rule, False, reason="still_starting")

        if not info_active.kickoff:
            return RuleEvaluationResult(rule, False, reason="missing_active_kickoff")

        if now >= info_active.kickoff:
            return RuleEvaluationResult(rule, False, reason="active_kickoff_passed")

        chosen_backup: Optional[str] = None
        for backup in rule.sorted_backups():
            candidate_id = backup.reserve_player_id

            if not roster_view.is_reserve(candidate_id):
                continue
            if roster_view.is_locked(candidate_id):
                continue

            info_candidate = lineup_info_by_player.get(candidate_id)
            if not info_candidate or info_candidate.status != LineupStatus.STARTING:
                continue
            if not info_candidate.kickoff:
                continue
            if now >= info_candidate.kickoff:
                continue
            if (
                rule.enforce_kickoff_order
                and info_candidate.kickoff < info_active.kickoff
            ):
                continue

            if not can_swap_in_period(
                subs_service=self.subs_service,
                roster=roster_view.roster,
                league_id=rule.league_id,
                team_id=rule.team_id,
                active_id=rule.active_player_id,
                reserve_id=candidate_id,
                period_id=rule.period_id,
            ):
                continue

            chosen_backup = candidate_id
            break

        if not chosen_backup:
            return RuleEvaluationResult(rule, False, reason="no_valid_backup")

        return RuleEvaluationResult(rule, True, chosen_backup_id=chosen_backup)

    def mark_rule_fired(self, rule: ConditionalSwapRule) -> None:
        self.fire_tracker.increment(rule)


def get_available_periods(league_id: str, team_id: str, session=None) -> List[Dict[str, str]]:
    """
    Return the list of roster change periods available in a league.
    Each entry contains {"id": str, "label": str}.
    """
    try:
        api = FantraxAPI(league_id=league_id, session=session)
        _ = api.roster_info(team_id)  # Warm team cache & validate access
        periods = api.scoring_periods()
    except Exception:
        log.exception("[conditional-swaps] Failed to load scoring periods")
        return []

    items: List[Dict[str, str]] = []
    for week, period in sorted(periods.items(), key=lambda kv: kv[0]):
        data = getattr(period, "data", {}) or {}
        period_id = str(data.get("periodId") or data.get("period") or week)
        caption = str(data.get("caption") or f"Period {week}")
        subtitle = data.get("subtitle") or data.get("subTitle") or ""
        label = f"{caption} ({subtitle})" if subtitle else caption
        items.append({"id": period_id, "label": label})
    return items


def can_swap_in_period(
    *,
    subs_service: SubsService,
    roster: Roster,
    league_id: str,
    team_id: str,
    active_id: str,
    reserve_id: str,
    period_id: str | int,
) -> bool:
    """
    Validate whether a given active->reserve swap would be legal for a specific period.
    Uses the existing confirm_or_execute_lineup confirm phase to check legality.
    """
    try:
        period_int = int(str(period_id))
    except (TypeError, ValueError):
        return False

    starters = [
        r.player.id for r in roster.get_starters() if getattr(r, "player", None)
    ]
    if active_id not in starters:
        return False
    if reserve_id in starters:
        return False

    desired = [pid for pid in starters if pid != active_id]
    desired.append(reserve_id)

    try:
        field_map = subs_service.build_field_map(roster, desired)
    except Exception:
        log.exception("[conditional-swaps] Failed to build field map for validation")
        return False

    try:
        confirm = subs_service.confirm_or_execute_lineup(
            league_id=league_id,
            fantasy_team_id=team_id,
            roster_limit_period=period_int,
            field_map=field_map,
            apply_to_future=False,
            do_finalize=False,
        )
    except Exception:
        log.exception("[conditional-swaps] Confirm request failed during validation")
        return False

    fantasy_response = confirm.get("fantasyResponse") or {}
    illegal_msgs = fantasy_response.get("illegalRosterMsgs") or []
    if illegal_msgs:
        return False
    if not confirm.get("ok", False):
        return False
    return True


__all__ = [
    "BackupOption",
    "ConditionalSwapEngine",
    "ConditionalSwapRule",
    "FireTracker",
    "is_row_locked",
    "LineupStatus",
    "PlayerLineupInfo",
    "RosterView",
    "RuleEvaluationResult",
    "RuleState",
    "RuleStorage",
    "SwapCondition",
    "can_swap_in_period",
    "generate_rule_id",
    "get_available_periods",
    "get_row_lock_flags",
]
