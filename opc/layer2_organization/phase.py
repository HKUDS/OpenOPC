"""Unified phase model for delegation work items.

A `Phase` is the single authoritative state of a delegation work item. It
replaces the previous mixture of `status` + 5 metadata sub-state fields
(activation_state / lifecycle_state / review_state / manager_release_state /
review_execution_state) which were tangled and could disagree with each other.

Design:
- One enum value per concrete situation a card can be in.
- Pure-function projections derive everything else (kanban column, owner,
  TaskStatus, runnability, verdict).
- A static transition table is enforced at every write.
- A **phase-transition hook** mechanism lets other layers (task.status,
  role_session.status, dispatcher wake signal, ...) subscribe to phase
  changes and synchronise themselves in one place. This eliminates the
  bug class where code forgot to update a dependent layer after
  transitioning a phase, leaving task/session state desynchronised from
  work-item phase.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional

from loguru import logger

from opc.core.models import Phase, TaskStatus
from opc.layer2_organization.work_item_identity import work_item_turn_type_from_metadata

__all__ = [
    "Phase",
    "TODO_PHASES",
    "IN_PROGRESS_PHASES",
    "IN_REVIEW_PHASES",
    "DONE_PHASES",
    "TERMINAL_PHASES",
    "RUNNABLE_PHASES",
    "WAITING_EXTERNAL_PHASES",
    "ALLOWED_TRANSITIONS",
    "InvalidPhaseTransition",
    "validate_transition",
    "kanban_column",
    "is_runnable",
    "is_terminal",
    "is_waiting_external",
    "effective_owner",
    "verdict",
    "task_status_for_phase",
    "phase_for_task_status",
    "coerce_phase",
    "is_review_execution_work_item_metadata",
    "is_runtime_auxiliary_work_item",
    "should_hide_work_item_from_company_kanban",
    "is_stale_claim_releasable",
    "is_resumable_after_claim_release",
    "is_orphaned",
    "is_dispatchable",
    "PhaseTransitionHook",
    "register_phase_transition_hook",
    "clear_phase_transition_hooks",
    "on_phase_transition",
]


# ── Phase transition hook mechanism (D2) ─────────────────────────────────
#
# Other layers (task.status sync, role_session.status sync, dispatcher wake)
# subscribe by calling register_phase_transition_hook. The store fires
# on_phase_transition after every successful work-item write. Each hook
# is invoked with (previous_phase, target_phase, item, store=...). One
# hook raising never prevents the others from firing nor prevents the
# write itself from succeeding; the goal is "best-effort cross-layer
# convergence", not transactional consistency. (The work-item write IS
# the source of truth — hooks merely propagate it.)

PhaseTransitionHook = Callable[..., Awaitable[None]]

_PHASE_TRANSITION_HOOKS: list[PhaseTransitionHook] = []


def register_phase_transition_hook(hook: PhaseTransitionHook) -> None:
    """Register a hook to fire after every work-item phase write.

    Hook signature: ``async def hook(previous: Phase | None, target: Phase,
    item: DelegationWorkItem, *, store: OPCStore) -> None``.

    Hooks are invoked in registration order. Re-registering the same
    callable is a no-op (idempotent — important for module-level
    registration that may run twice under reload/import re-entry).
    """
    if hook not in _PHASE_TRANSITION_HOOKS:
        _PHASE_TRANSITION_HOOKS.append(hook)


def clear_phase_transition_hooks() -> None:
    """Remove all registered hooks. Test-only helper."""
    _PHASE_TRANSITION_HOOKS.clear()


async def on_phase_transition(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """Fire all registered phase-transition hooks.

    Called by the store after a successful work-item write commits. Each
    hook gets the same arguments; failures in one hook are logged but do
    not affect other hooks or the upstream write.
    """
    for hook in list(_PHASE_TRANSITION_HOOKS):
        try:
            await hook(previous, target, item, store=store)
        except Exception:
            logger.opt(exception=True).warning(
                f"phase transition hook {getattr(hook, '__name__', repr(hook))} "
                f"failed for {previous} -> {target} on work_item "
                f"{getattr(item, 'work_item_id', '?')}"
            )


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def is_review_execution_work_item_metadata(metadata: Mapping[str, Any] | None) -> bool:
    """True when the work item is a hidden auxiliary review card."""
    data = dict(metadata or {})
    if bool(data.get("review_execution_work_item", False)):
        return True
    work_kind = _normalized_text(data.get("work_kind") or work_item_turn_type_from_metadata(data, fallback=""))
    return work_kind == "review" and bool(str(data.get("review_target_work_item_id", "") or "").strip())


def is_report_execution_work_item_metadata(metadata: Mapping[str, Any] | None) -> bool:
    """True when the work item is the hidden auxiliary report-generation card.

    Spawned by ``_apply_done_transition`` after a worker finishes its execute
    turn but before review begins, so the worker can produce a structured
    handoff report on its own session before the reviewer sees anything.
    """
    data = dict(metadata or {})
    if bool(data.get("report_execution_work_item", False)):
        return True
    work_kind = _normalized_text(data.get("work_kind") or work_item_turn_type_from_metadata(data, fallback=""))
    return work_kind == "report" and bool(str(data.get("report_target_work_item_id", "") or "").strip())


def is_runtime_auxiliary_work_item(value_or_metadata: Any) -> bool:
    """True for attention, report, and review runtime helper cards.

    Accepts either a work-item-like object, a serialized work item containing
    a ``metadata`` mapping, or the metadata mapping itself.  Keeping this
    identity check below the company runtime gives lifecycle and board code a
    single dependency-free definition of a non-business child.
    """
    if isinstance(value_or_metadata, Mapping):
        nested = value_or_metadata.get("metadata")
        metadata = nested if isinstance(nested, Mapping) else value_or_metadata
    else:
        metadata = getattr(value_or_metadata, "metadata", None)
    data = dict(metadata or {}) if isinstance(metadata, Mapping) else {}
    return (
        bool(data.get("attention_work_item", False))
        or is_report_execution_work_item_metadata(data)
        or is_review_execution_work_item_metadata(data)
    )


def should_hide_work_item_from_company_kanban(metadata: Mapping[str, Any] | None) -> bool:
    """True when the kanban UI should not display this work item."""
    data = dict(metadata or {})
    return bool(data.get("hidden_from_company_kanban", False))


# ── Set views over phases ────────────────────────────────────────────────

TODO_PHASES: frozenset[Phase] = frozenset({
    Phase.QUEUED,
    Phase.READY,
    Phase.READY_FOR_REWORK,
    Phase.WAITING_DEPENDENCIES,
})

IN_PROGRESS_PHASES: frozenset[Phase] = frozenset({
    Phase.RUNNING,
    Phase.WAITING_FOR_PEER,
    Phase.WAITING_FOR_CHILDREN,
    Phase.PAUSED,
    Phase.NEEDS_ATTENTION,
})

IN_REVIEW_PHASES: frozenset[Phase] = frozenset({
    Phase.AWAITING_MANAGER_REVIEW,
    Phase.AWAITING_HUMAN,
})

DONE_PHASES: frozenset[Phase] = frozenset({
    Phase.APPROVED,
    Phase.FAILED,
    Phase.CANCELLED,
})

TERMINAL_PHASES: frozenset[Phase] = DONE_PHASES

RUNNABLE_PHASES: frozenset[Phase] = frozenset({
    Phase.READY,
    Phase.READY_FOR_REWORK,
})

WAITING_EXTERNAL_PHASES: frozenset[Phase] = frozenset({
    Phase.WAITING_FOR_PEER,
    Phase.WAITING_FOR_CHILDREN,
    Phase.WAITING_DEPENDENCIES,
    Phase.NEEDS_ATTENTION,
})


# ── Allowed transitions (state machine) ──────────────────────────────────

# Two universal exits every non-terminal phase must have:
#   - FAILED:   abort-on-error path. Any exception in the agent runtime
#               must be able to land here from any sub-state.
#   - CANCELLED: user/system cancel path. Same reason.
# These are baked into the table below; the invariant tests in
# test_phase_state_machine_invariants.py guarantee they stay there.
_UNIVERSAL_EXITS: frozenset[Phase] = frozenset({Phase.FAILED, Phase.CANCELLED})

# Transitions that release a stale claim back to the dispatcher queue,
# enabling crash-recovery (Bug C). Whenever an in-flight card's runtime
# session dies, the sweeper transitions the card back to READY (or
# READY_FOR_REWORK if appropriate), which the dispatcher then re-claims.
# This must be wired into every in-flight phase.
_RECOVERY_EXITS: frozenset[Phase] = frozenset({Phase.READY})


ALLOWED_TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    # todo
    Phase.QUEUED: frozenset({
        Phase.READY,
        Phase.WAITING_DEPENDENCIES,
    }) | _UNIVERSAL_EXITS,
    Phase.WAITING_DEPENDENCIES: frozenset({
        Phase.READY,
    }) | _UNIVERSAL_EXITS,
    Phase.READY: frozenset({
        Phase.RUNNING,
        Phase.WAITING_DEPENDENCIES,
    }) | _UNIVERSAL_EXITS,
    Phase.READY_FOR_REWORK: frozenset({
        Phase.RUNNING,
    }) | _UNIVERSAL_EXITS,

    # in_progress
    #   RUNNING → APPROVED is the self-completion path used by review
    #   work items (which do not themselves need external review). It is
    #   also a legal escape for any future "auto-approved" work kind.
    Phase.RUNNING: frozenset({
        Phase.WAITING_FOR_PEER,
        Phase.WAITING_FOR_CHILDREN,
        Phase.PAUSED,
        Phase.NEEDS_ATTENTION,
        Phase.AWAITING_MANAGER_REVIEW,
        Phase.AWAITING_HUMAN,
        Phase.APPROVED,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,
    Phase.WAITING_FOR_PEER: frozenset({
        Phase.RUNNING,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,
    Phase.WAITING_FOR_CHILDREN: frozenset({
        Phase.RUNNING,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,
    Phase.PAUSED: frozenset({
        Phase.RUNNING,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,
    Phase.NEEDS_ATTENTION: frozenset({
        Phase.RUNNING,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,

    # in_review
    Phase.AWAITING_MANAGER_REVIEW: frozenset({
        Phase.APPROVED,
        Phase.READY_FOR_REWORK,
        # Escalation path: when the manager-review/rework loop exceeds its
        # retry budget, the runtime escalates the card to a human decider
        # instead of bouncing it back to the worker yet again.
        Phase.AWAITING_HUMAN,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,
    Phase.AWAITING_HUMAN: frozenset({
        Phase.APPROVED,
        Phase.READY_FOR_REWORK,
    }) | _UNIVERSAL_EXITS | _RECOVERY_EXITS,

    # terminal — no outgoing edges
    Phase.APPROVED: frozenset(),
    Phase.FAILED: frozenset(),
    Phase.CANCELLED: frozenset(),
}


class InvalidPhaseTransition(ValueError):
    """Raised when a write attempts a transition not in ALLOWED_TRANSITIONS."""


def validate_transition(previous: Phase | None, target: Phase) -> None:
    """Raise InvalidPhaseTransition when target is not reachable from previous.

    Initial creation (previous is None) and idempotent writes (previous == target)
    are always allowed.
    """
    if previous is None or previous == target:
        return
    allowed = ALLOWED_TRANSITIONS.get(previous, frozenset())
    if target not in allowed:
        raise InvalidPhaseTransition(
            f"invalid phase transition: {previous.value} -> {target.value}"
        )


# ── Pure-function projections ────────────────────────────────────────────

_PHASE_TO_COLUMN: dict[Phase, str] = {
    **{p: "todo" for p in TODO_PHASES},
    **{p: "in_progress" for p in IN_PROGRESS_PHASES},
    **{p: "in_review" for p in IN_REVIEW_PHASES},
    **{p: "done" for p in DONE_PHASES},
}


def kanban_column(phase: Phase) -> str:
    """Project Phase to one of the four kanban columns."""
    return _PHASE_TO_COLUMN[phase]


def is_runnable(phase: Phase) -> bool:
    """Whether the dispatcher should claim+spawn this card on its next tick."""
    return phase in RUNNABLE_PHASES


# A process restart invalidates every persisted runtime claim, including claims
# on passive review states.  Releasing a dead claim and allowing the original
# worker to execute again are intentionally separate decisions: review parents
# must lose their dead claim but remain passive while their report/review
# auxiliaries resume.
_STALE_CLAIM_RELEASABLE_PHASES: frozenset[Phase] = (
    IN_PROGRESS_PHASES | IN_REVIEW_PHASES
)
_RESUMABLE_AFTER_CLAIM_RELEASE_PHASES: frozenset[Phase] = IN_PROGRESS_PHASES


def is_stale_claim_releasable(phase: Phase) -> bool:
    """Whether startup recovery may clear a dead runtime claim.

    This includes passive review/human-wait states so a crashed resident
    session cannot retain ownership forever.  It does *not* imply that the
    original work item may be dispatched again.
    """
    return phase in _STALE_CLAIM_RELEASABLE_PHASES


def is_resumable_after_claim_release(phase: Phase) -> bool:
    """Whether the original worker may resume after its claim is released.

    Active execution phases can be re-picked after a crash. Passive
    ``AWAITING_*`` parents cannot: their report/review chain (or human) owns
    forward progress.
    """
    return phase in _RESUMABLE_AFTER_CLAIM_RELEASE_PHASES


def is_orphaned(item: Any) -> bool:
    """A work item is orphaned when its phase says 'in flight' but no
    runtime session currently holds a claim on it.

    Typical scenario: the process that owned an execution claim died. On
    startup the stale-claim sweeper clears the claim metadata; this function
    then lets the dispatcher re-pick active execution, but never a passive
    review parent.
    """
    if not is_resumable_after_claim_release(item.phase):
        return False
    claim = str(getattr(item, "claimed_by_role_runtime_session_id", "") or "").strip()
    return not claim


# ── Attempt ledger ───────────────────────────────────────────────────────
#
# Every durable claim opens an "attempt" on the work item (stamped by the
# claim CAS in store.claim_delegation_work_item_if_dispatchable) and every
# turn-boundary transition through work_item_transition.transition_work_item
# settles it with an outcome. The dispatcher refuses to start attempt N+1
# for cards whose ledger shows a run of consecutive crashed / interrupted
# attempts — this is the structural brake that makes "a crash path forgot
# (or failed) to write FAILED" converge instead of re-dispatching forever
# (issue #10: restart → resume → deterministic crash → re-dispatch loop).

ATTEMPT_CRASH_STREAK_LIMIT = 3
ATTEMPT_INTERRUPTED_STREAK_LIMIT = 5


def _attempt_ledger_int(metadata: Mapping[str, Any], key: str) -> int:
    try:
        return int(metadata.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def attempt_ledger_dispatch_block_reason(metadata: Mapping[str, Any] | None) -> str:
    """Non-empty human-readable reason when the attempt ledger forbids
    dispatching this card again; empty string when dispatch is allowed."""
    if not isinstance(metadata, Mapping):
        return ""
    crash_streak = _attempt_ledger_int(metadata, "attempt_crash_streak")
    if crash_streak >= ATTEMPT_CRASH_STREAK_LIMIT:
        return (
            f"attempt ledger: {crash_streak} consecutive crashed attempts "
            f"(limit {ATTEMPT_CRASH_STREAK_LIMIT})"
        )
    interrupted_streak = _attempt_ledger_int(metadata, "attempt_interrupted_streak")
    if interrupted_streak >= ATTEMPT_INTERRUPTED_STREAK_LIMIT:
        return (
            f"attempt ledger: {interrupted_streak} consecutive interrupted attempts "
            f"(limit {ATTEMPT_INTERRUPTED_STREAK_LIMIT})"
        )
    return ""


def has_open_attempt(metadata: Mapping[str, Any] | None) -> bool:
    """True when a claim opened an attempt that was never settled."""
    if not isinstance(metadata, Mapping):
        return False
    if _attempt_ledger_int(metadata, "attempt_seq") <= 0:
        return False
    return not bool(metadata.get("attempt_settled", True))


def is_dispatchable(item: Any) -> bool:
    """Combined check used by the dispatcher: pick this card on next tick?

    True for fresh-runnable phases (READY/READY_FOR_REWORK) and for
    orphaned in-flight cards (RUNNING / WAITING_FOR_* / PAUSED / etc.
    whose previous claim died and was swept).

    Fix 5 PR3: a work item stamped with ``metadata.queued_behind_session``
    is waiting behind another task on its role's serial queue — the
    dispatcher must skip it until the session that holds the queue
    dequeues it (``clear_session_focus_on_terminal_hook`` does the
    dequeue on terminal transitions and drops the stamp). Without this
    check the dispatcher would claim both the focused item and the queued
    item into the same session on the same tick, defeating the queue.
    """
    metadata = getattr(item, "metadata", {}) or {}
    if isinstance(metadata, dict) and str(metadata.get("queued_behind_session", "") or "").strip():
        return False
    if isinstance(metadata, dict) and str(metadata.get("dispatch_hold", "") or "").strip():
        return False
    if attempt_ledger_dispatch_block_reason(metadata):
        return False
    return is_runnable(item.phase) or is_orphaned(item)


def is_terminal(phase: Phase) -> bool:
    return phase in TERMINAL_PHASES


def is_waiting_external(phase: Phase) -> bool:
    """True when the card is suspended waiting for an external event/actor."""
    return phase in WAITING_EXTERNAL_PHASES


def effective_owner(phase: Phase, item: Any) -> tuple[str, str]:
    """Return (role_id, seat_id) of the actor currently responsible for the card.

    In `in_review` phases the owner swaps from worker to manager so the card
    appears in the reviewer's swimlane. In all other phases the worker stays
    as owner. `item` may be a DelegationWorkItem or a mapping with the same
    field names.
    """
    def _field(name: str) -> str:
        if isinstance(item, Mapping):
            return str(item.get(name, "") or "").strip()
        return str(getattr(item, name, "") or "").strip()

    if phase in IN_REVIEW_PHASES:
        return _field("manager_role_id") or _field("role_id"), \
               _field("manager_seat_id") or _field("seat_id")
    return _field("role_id"), _field("seat_id")


_PHASE_TO_VERDICT: dict[Phase, str] = {
    Phase.APPROVED: "approve",
    Phase.READY_FOR_REWORK: "rework",
    Phase.FAILED: "fail",
    Phase.CANCELLED: "cancel",
}


def verdict(phase: Phase) -> Optional[str]:
    """Return the verdict label for terminal/rework phases, else None."""
    return _PHASE_TO_VERDICT.get(phase)


# Company-mode boundary note:
# DelegationWorkItem.phase is the business state source of truth. Task.status is
# only the runtime/job projection needed by existing execution, session, and
# tool infrastructure.
_PHASE_TO_TASK_STATUS: dict[Phase, TaskStatus] = {
    # todo column
    Phase.QUEUED: TaskStatus.PENDING,
    Phase.READY: TaskStatus.PENDING,
    Phase.READY_FOR_REWORK: TaskStatus.PENDING,
    Phase.WAITING_DEPENDENCIES: TaskStatus.BLOCKED,
    # in_progress column
    Phase.RUNNING: TaskStatus.RUNNING,
    Phase.WAITING_FOR_PEER: TaskStatus.AWAITING_PEER,
    Phase.WAITING_FOR_CHILDREN: TaskStatus.BLOCKED,
    Phase.PAUSED: TaskStatus.BLOCKED,
    Phase.NEEDS_ATTENTION: TaskStatus.BLOCKED,
    # in_review column
    Phase.AWAITING_MANAGER_REVIEW: TaskStatus.AWAITING_MANAGER_REVIEW,
    Phase.AWAITING_HUMAN: TaskStatus.AWAITING_HUMAN,
    # done column
    Phase.APPROVED: TaskStatus.DONE,
    Phase.FAILED: TaskStatus.FAILED,
    Phase.CANCELLED: TaskStatus.CANCELLED,
}


def task_status_for_phase(phase: Phase) -> TaskStatus:
    """Project Phase to the corresponding runtime TaskStatus.

    Used when syncing a work item's phase back to its associated tasks row.
    """
    return _PHASE_TO_TASK_STATUS[phase]


# ── TaskStatus → Phase reverse mapping (for runtime → work-item sync) ────

def phase_for_task_status(
    status: TaskStatus | str,
    *,
    has_pending_children: bool = False,
) -> Phase:
    """Map a runtime TaskStatus to a Phase.

    `has_pending_children` lets the caller distinguish two BLOCKED sub-cases:
    BLOCKED-with-children-pending → WAITING_FOR_CHILDREN, otherwise PAUSED.
    All other TaskStatus values map 1-to-1.
    """
    s = TaskStatus(status) if not isinstance(status, TaskStatus) else status
    if s == TaskStatus.RUNNING:
        return Phase.RUNNING
    if s == TaskStatus.AWAITING_PEER:
        return Phase.WAITING_FOR_PEER
    if s == TaskStatus.BLOCKED:
        return Phase.WAITING_FOR_CHILDREN if has_pending_children else Phase.PAUSED
    if s in (TaskStatus.AWAITING_MANAGER_REVIEW, TaskStatus.AWAITING_REVIEW):
        return Phase.AWAITING_MANAGER_REVIEW
    if s == TaskStatus.AWAITING_HUMAN:
        return Phase.AWAITING_HUMAN
    if s == TaskStatus.DONE:
        return Phase.APPROVED
    if s == TaskStatus.FAILED:
        return Phase.FAILED
    if s == TaskStatus.CANCELLED:
        return Phase.CANCELLED
    # PENDING / IDLE → READY (caller can override to QUEUED if not released)
    return Phase.READY


# ── Convenience parsers ─────────────────────────────────────────────────

def coerce_phase(value: Any) -> Phase:
    """Parse a phase value from a string/Phase, raising ValueError on garbage."""
    if isinstance(value, Phase):
        return value
    if isinstance(value, str):
        try:
            return Phase(value.strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown phase value: {value!r}") from exc
    raise TypeError(f"phase must be Phase or str, got {type(value).__name__}")
