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

from pydantic.v1 import BaseModel, Field, validator

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


class RuleActionType(str, Enum):
    LINEUP_SWAP = "lineup_swap"
    FA_CLAIM_DROP = "fa_claim_drop"


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
    ss_pred_status: Optional[LineupStatus] = None
    ss_conf_status: Optional[LineupStatus] = None
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


class RuleActionType(str, Enum):
    LINEUP_SWAP = "lineup_swap"
    FA_CLAIM_DROP = "fa_claim_drop"


class BackupOption(BaseModel):
    reserve_player_id: str
    priority: int = Field(gt=0)

    class Config:
        frozen = True


class ConditionalSwapRule(BaseModel):
    id: str = Field(default_factory=generate_rule_id)
    league_id: str
    team_id: str

    active_player_id: str  # monitored roster player
    backups: List[BackupOption]  # used only for lineup swaps

    condition: SwapCondition = SwapCondition.NOT_STARTING

    period_id: str
    period_label: str

    enforce_kickoff_order: bool = True
    max_fires_per_period: int = Field(default=1, ge=1)

    state: RuleState = RuleState.ACTIVE
    action_type: RuleActionType = RuleActionType.LINEUP_SWAP

    # FA claim/drop config (for action_type == FA_CLAIM_DROP)
    fa_add_scorer_id: Optional[str] = None
    fa_add_position_id: Optional[str] = None
    fa_claim_to_status_id: str = "2"
    fa_bid_amount: float = 0.0
    fa_add_display_name: Optional[str] = None

    class Config:
        use_enum_values = True

    @validator("backups", pre=True, always=True)
    def _validate_backups(cls, value: Sequence[BackupOption], values) -> Sequence[BackupOption]:
        action_type = values.get("action_type", RuleActionType.LINEUP_SWAP)
        if action_type == RuleActionType.LINEUP_SWAP:
            if not value:
                raise ValueError("At least one backup must be provided for lineup swap rules.")
            seen = set()
            for opt in value:
                key = opt.reserve_player_id
                if key == "":
                    raise ValueError("Empty reserve player id.")
                if key in seen:
                    raise ValueError("Backups must be unique per rule.")
                seen.add(key)
            return value
        # FA rules ignore backups
        return []

    @validator("fa_add_scorer_id", "fa_add_position_id", always=True)
    def _validate_fa_fields(cls, v, values, field):
        action_type = values.get("action_type", RuleActionType.LINEUP_SWAP)
        if action_type == RuleActionType.FA_CLAIM_DROP:
            if field.name == "fa_add_scorer_id" and not v:
                raise ValueError("fa_add_scorer_id is required for FA_CLAIM_DROP rules.")
            if field.name == "fa_add_position_id" and not v:
                raise ValueError("fa_add_position_id is required for FA_CLAIM_DROP rules.")
        return v

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
        return [
            pid
            for pid, row in self._row_by_player.items()
            if getattr(row, "pos_id", "0") != "0"
        ]

    def reserve_player_ids(self) -> List[str]:
        return [
            pid
            for pid, row in self._row_by_player.items()
            if getattr(row, "pos_id", "0") == "0"
        ]


def _canonical_pos_from_row(row: RosterRow) -> str:
    """
    Return a normalized position code for lineup legality checks.

    - Starters: use row.pos.short_name directly.
    - Reserves ("RES"): derive the player's underlying position from player/raw.
    """
    pos_obj = getattr(row, "pos", None)
    raw_short = (getattr(pos_obj, "short_name", None) or "").strip().upper()
    if raw_short and raw_short not in {"RES", "R", "BENCH"}:
        code = raw_short
    else:
        player = getattr(row, "player", None)
        raw = getattr(row, "_raw", {}) or {}
        scorer = raw.get("scorer") or {}
        candidates = [
            getattr(player, "position_short", None) if player is not None else None,
            getattr(player, "positionShort", None) if player is not None else None,
            getattr(player, "position", None) if player is not None else None,
            scorer.get("positionShort"),
            scorer.get("position"),
            raw.get("positionShort"),
            raw.get("position"),
        ]
        code = ""
        for c in candidates:
            if c:
                code = str(c).strip().upper()
                if code:
                    break

    if not code:
        return ""

    if code.startswith("GK") or code.startswith("G"):
        return "G"
    if code.startswith("D"):
        return "D"
    if code.startswith("M"):
        return "M"
    if code.startswith("F"):
        return "F"
    return code


def would_break_mandatory_slots(
    roster_view: RosterView,
    active_id: str,
    reserve_id: str,
    *,
    min_gks: int = 1,
) -> bool:
    """
    Return True if swapping active_id -> reserve_id would violate hard roster invariants (e.g., 0 active GKs).
    Uses the player's underlying position, not 'RES', for bench rows.
    """
    starters = roster_view.active_player_ids()
    if active_id not in starters:
        return True

    new_starters = [pid for pid in starters if pid != active_id]
    new_starters.append(reserve_id)

    gk_count = 0
    for pid in new_starters:
        row = roster_view.get_row(pid)
        if not row:
            continue
        pos = _canonical_pos_from_row(row)
        if pos == "G":
            gk_count += 1

    if gk_count < min_gks:
        return True
    return False


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

    log.info(
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
    reason: Optional[str] = None

    swap_out_id: Optional[str] = None
    swap_in_id: Optional[str] = None
    add_player_id: Optional[str] = None
    drop_player_id: Optional[str] = None


def execute_rules(
    *,
    rules: Sequence[ConditionalSwapRule],
    roster_view: RosterView,
    lineup_info_by_player: Dict[str, PlayerLineupInfo],
    now: datetime,
    current_period_id: str,
    subs_service: SubsService,
    waivers_service,
    engine: "ConditionalSwapEngine",
    session=None,
    league_id: Optional[str] = None,
    fa_status_map: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Evaluate and execute rules, dispatching to lineup swaps or FA claims.
    """
    results = engine.evaluate_rules(
        rules=rules,
        roster_view=roster_view,
        lineup_info_by_player=lineup_info_by_player,
        now=now,
        current_period_id=current_period_id,
        fa_status_map=fa_status_map,
    )

    for res in results:
        rule = res.rule
        if not res.should_fire:
            continue

        if rule.action_type == RuleActionType.LINEUP_SWAP:
            if not (res.swap_out_id and res.swap_in_id):
                log.warning("[conditional-swaps] swap rule=%s missing swap ids", rule.id)
                continue
            try:
                subs_service.swap_players(
                    team_id=rule.team_id,
                    out_player_id=res.swap_out_id,
                    in_player_id=res.swap_in_id,
                    period=int(current_period_id),
                )
                engine.mark_rule_fired(rule)
                log.info(
                    "[conditional-swaps] Executed lineup swap rule=%s out=%s in=%s",
                    rule.id,
                    res.swap_out_id,
                    res.swap_in_id,
                )
            except Exception:
                log.exception("[conditional-swaps] Failed to execute lineup swap rule=%s", rule.id)
        elif rule.action_type == RuleActionType.FA_CLAIM_DROP:
            if not waivers_service:
                log.warning("[conditional-swaps] waivers service missing; cannot submit FA claim for rule=%s", rule.id)
                continue
            if not (res.add_player_id and res.drop_player_id):
                log.warning("[conditional-swaps] FA rule=%s missing add/drop ids", rule.id)
                continue
            if session and league_id:
                if not _fa_is_starting(session=session, league_id=league_id, scorer_id=res.add_player_id):
                    log.info(
                        "[conditional-swaps] FA rule=%s skipped; add target %s not starting",
                        rule.id,
                        res.add_player_id,
                    )
                    continue
            try:
                resp = waivers_service.submit_claim(
                    team_id=rule.team_id,
                    claim_scorer_id=res.add_player_id,
                    bid_amount=rule.fa_bid_amount,
                    drop_scorer_id=res.drop_player_id,
                    to_position_id=rule.fa_add_position_id,
                    to_status_id=rule.fa_claim_to_status_id,
                )
                fantasy_error = resp.get("error") or resp.get("errorMsg")
                if fantasy_error:
                    log.warning(
                        "[conditional-swaps] FA claim rejected rule=%s add=%s drop=%s error=%s",
                        rule.id,
                        res.add_player_id,
                        res.drop_player_id,
                        fantasy_error,
                    )
                else:
                    engine.mark_rule_fired(rule)
                    log.info(
                        "[conditional-swaps] Submitted FA claim rule=%s add=%s drop=%s",
                        rule.id,
                        res.add_player_id,
                        res.drop_player_id,
                    )
            except Exception:
                log.exception("[conditional-swaps] Failed to submit FA claim rule=%s", rule.id)


def _fa_is_starting(*, session, league_id: str, scorer_id: str) -> bool:
    try:
        from fantraxapi.lineups.fantrax_lineup_bridge import (
            fetch_fantrax_player_status_snapshot,
            parse_fantrax_player_statuses,
        )
        payload = fetch_fantrax_player_status_snapshot(
            session=session,
            league_id=league_id,
            status_filter="ALL_AVAILABLE",
            misc_display_type="10",
            max_results=500,
        )
        statuses = parse_fantrax_player_statuses(payload)
        status_obj = statuses.get(str(scorer_id))
        return bool(status_obj and status_obj.status == LineupStatus.STARTING)
    except Exception:
        log.exception("[conditional-swaps] Unable to determine FA lineup status for scorer_id=%s", scorer_id)
        return False


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


def choose_backup_for_rule_preview(
    rule: "ConditionalSwapRule",
    roster_view: "RosterView",
    lineup_info_by_player: Dict[str, "PlayerLineupInfo"],
    *,
    now: Optional[datetime] = None,
) -> Tuple[Optional[str], str]:
    """
    For a given rule, pick the backup that *would* be used if the active player
    were NOT STARTING, following the same ordering and constraints as the engine.

    Returns (backup_player_id_or_None, reason_string).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    info_active = lineup_info_by_player.get(rule.active_player_id)
    if not info_active:
        return None, "missing_active_info"

    if not info_active.kickoff:
        return None, "missing_active_kickoff"

    for backup in rule.sorted_backups():
        candidate_id = backup.reserve_player_id

        if not roster_view.is_reserve(candidate_id):
            continue
        if roster_view.is_locked(candidate_id):
            continue

        info_candidate = lineup_info_by_player.get(candidate_id)
        if not info_candidate:
            continue
        if info_candidate.status != LineupStatus.STARTING:
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
        if would_break_mandatory_slots(
            roster_view,
            rule.active_player_id,
            candidate_id,
            min_gks=1,
        ):
            continue

        return candidate_id, "ok"

    return None, "no_valid_backup"


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
        fa_status_map: Optional[Dict[str, Any]] = None,
    ) -> List[RuleEvaluationResult]:
        results: List[RuleEvaluationResult] = []
        for rule in rules:
            result = self._evaluate_single_rule(
                rule=rule,
                roster_view=roster_view,
                lineup_info_by_player=lineup_info_by_player,
                now=now,
                current_period_id=current_period_id,
                fa_status_map=fa_status_map,
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
        fa_status_map: Optional[Dict[str, Any]],
    ) -> RuleEvaluationResult:
        if rule.state != RuleState.ACTIVE:
            return RuleEvaluationResult(rule, False, reason="rule_disabled")

        if rule.period_id != str(current_period_id):
            return RuleEvaluationResult(rule, False, reason="period_mismatch")

        if self.fire_tracker.get(rule) >= rule.max_fires_per_period:
            return RuleEvaluationResult(rule, False, reason="fire_limit_reached")

        if rule.action_type == RuleActionType.FA_CLAIM_DROP:
            return self._evaluate_fa_claim_rule(
                rule=rule,
                roster_view=roster_view,
                lineup_info_by_player=lineup_info_by_player,
                now=now,
                current_period_id=current_period_id,
                fa_status_map=fa_status_map,
            )

        # default: lineup swap
        return self._evaluate_lineup_swap_rule(
            rule=rule,
            roster_view=roster_view,
            lineup_info_by_player=lineup_info_by_player,
            now=now,
            current_period_id=current_period_id,
        )

    def _evaluate_lineup_swap_rule(
        self,
        *,
        rule: ConditionalSwapRule,
        roster_view: RosterView,
        lineup_info_by_player: Dict[str, PlayerLineupInfo],
        now: datetime,
        current_period_id: str,
    ) -> RuleEvaluationResult:
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
            if would_break_mandatory_slots(
                roster_view,
                rule.active_player_id,
                candidate_id,
                min_gks=1,
                ):
                    continue

            result = can_swap_in_period(
                subs_service=self.subs_service,
                roster=roster_view.roster,
                league_id=rule.league_id,
                team_id=rule.team_id,
                active_id=rule.active_player_id,
                reserve_id=candidate_id,
                period_id=rule.period_id,
            )
            if not result:
                continue

            chosen_backup = candidate_id
            break

        if not chosen_backup:
            return RuleEvaluationResult(rule, False, reason="no_valid_backup")

        return RuleEvaluationResult(
            rule=rule,
            should_fire=True,
            swap_out_id=rule.active_player_id,
            swap_in_id=chosen_backup,
        )

    def _evaluate_fa_claim_rule(
        self,
        *,
        rule: ConditionalSwapRule,
        roster_view: RosterView,
        lineup_info_by_player: Dict[str, PlayerLineupInfo],
        now: datetime,
        current_period_id: str,
        fa_status_map: Optional[Dict[str, Any]],
    ) -> RuleEvaluationResult:
        drop_id = rule.active_player_id
        add_id = rule.fa_add_scorer_id

        if not add_id:
            return RuleEvaluationResult(rule, False, reason="missing_fa_add")

        if not roster_view.get_row(drop_id):
            return RuleEvaluationResult(rule, False, reason="drop_not_on_roster")
        if roster_view.is_locked(drop_id, now=now, lineup_info_by_player=lineup_info_by_player):
            return RuleEvaluationResult(rule, False, reason="drop_locked")

        info_drop = lineup_info_by_player.get(drop_id)
        if not info_drop or info_drop.status == LineupStatus.UNKNOWN:
            return RuleEvaluationResult(rule, False, reason="drop_status_unknown")
        if info_drop.status == LineupStatus.STARTING:
            return RuleEvaluationResult(rule, False, reason="drop_still_starting")
        if info_drop.kickoff and now >= info_drop.kickoff:
            return RuleEvaluationResult(rule, False, reason="drop_kickoff_passed")

        fa_snapshot = fa_status_map.get(add_id) if fa_status_map else None
        if not fa_snapshot:
            return RuleEvaluationResult(rule, False, reason="fa_status_unknown")
        status_val = getattr(fa_snapshot, "status", None)
        kickoff = getattr(fa_snapshot, "kickoff", None)
        if isinstance(status_val, LineupStatus):
            is_starting = status_val == LineupStatus.STARTING
        else:
            is_starting = str(status_val).lower() == LineupStatus.STARTING.value
        if not is_starting:
            return RuleEvaluationResult(rule, False, reason="fa_not_starting")
        if kickoff and now >= kickoff:
            return RuleEvaluationResult(rule, False, reason="fa_kickoff_passed")
        if kickoff and info_drop.kickoff and kickoff < info_drop.kickoff:
            return RuleEvaluationResult(rule, False, reason="fa_kickoff_before_active")

        if not self._drop_would_keep_roster_legal(roster_view, drop_id):
            return RuleEvaluationResult(rule, False, reason="drop_illegal_minimums")

        return RuleEvaluationResult(
            rule=rule,
            should_fire=True,
            add_player_id=add_id,
            drop_player_id=drop_id,
        )

    @staticmethod
    def _drop_would_keep_roster_legal(roster_view: RosterView, drop_id: str, *, min_gks: int = 1) -> bool:
        """
        Quick local invariant: do not drop to zero goalkeepers.
        """
        gk_count = 0
        for row in roster_view.roster.rows:
            if not getattr(row, "player", None):
                continue
            pid = str(row.player.id)
            if pid == drop_id:
                continue
            pos = _canonical_pos_from_row(row)
            if pos == "G":
                gk_count += 1
        return gk_count >= min_gks

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


def test_swap_in_period(
    *,
    subs_service: SubsService,
    roster: Roster,
    league_id: str,
    team_id: str,
    active_id: str,
    reserve_id: str,
    period_id: str | int,
    enforce_invariants: bool = True,
) -> dict:
    """
    Dry-run a swap for a specific period. Does not finalize the lineup.
    """
    try:
        period_int = int(str(period_id))
    except (TypeError, ValueError):
        return {
            "ok": False,
            "reason": "invalid_period",
            "illegal_msgs": [],
            "confirm": {},
        }

    starters = [
        r.player.id for r in roster.get_starters() if getattr(r, "player", None)
    ]

    if active_id not in starters:
        return {
            "ok": False,
            "reason": "active_not_starter",
            "illegal_msgs": [],
            "confirm": {},
        }

    if reserve_id in starters:
        return {
            "ok": False,
            "reason": "reserve_already_starter",
            "illegal_msgs": [],
            "confirm": {},
        }

    roster_view = RosterView(roster)
    if enforce_invariants and would_break_mandatory_slots(
        roster_view,
        active_id=active_id,
        reserve_id=reserve_id,
    ):
        return {
            "ok": False,
            "reason": "would_break_mandatory_slots",
            "illegal_msgs": [
                "Swap would violate mandatory position constraints (e.g. 0 GKs)."
            ],
            "confirm": {},
        }

    desired = [pid for pid in starters if pid != active_id]
    desired.append(reserve_id)

    try:
        field_map = subs_service.build_field_map(roster, desired)
    except Exception:
        log.exception("[conditional-swaps] Failed to build field map for test")
        return {
            "ok": False,
            "reason": "field_map_error",
            "illegal_msgs": ["Failed to build field map for test."],
            "confirm": {},
        }

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
        log.exception("[conditional-swaps] Confirm request failed during test")
        return {
            "ok": False,
            "reason": "confirm_request_failed",
            "illegal_msgs": ["Fantrax confirm request failed."],
            "confirm": {},
        }

    fantasy_response = confirm.get("fantasyResponse") or {}
    illegal_msgs = fantasy_response.get("illegalRosterMsgs") or []
    ok = bool(confirm.get("ok", False)) and not illegal_msgs

    return {
        "ok": ok,
        "reason": "ok" if ok else "illegal",
        "illegal_msgs": illegal_msgs,
        "confirm": confirm,
    }


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
    Uses the confirm_or_execute_lineup confirm phase to check legality.
    """
    result = test_swap_in_period(
        subs_service=subs_service,
        roster=roster,
        league_id=league_id,
        team_id=team_id,
        active_id=active_id,
        reserve_id=reserve_id,
        period_id=period_id,
        enforce_invariants=True,
    )
    return bool(result.get("ok"))


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
    "RuleActionType",
    "RuleStorage",
    "SwapCondition",
    "can_swap_in_period",
    "choose_backup_for_rule_preview",
    "execute_rules",
    "generate_rule_id",
    "get_available_periods",
    "get_row_lock_flags",
    "_canonical_pos_from_row",
    "_fa_is_starting",
    "test_swap_in_period",
    "would_break_mandatory_slots",
]
