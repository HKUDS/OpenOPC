"""Stateless turn-mode classifier for role-instance dispatch.

A role's session can be called into action for several different kinds
of turn on the SAME work item. ``infer_turn_mode`` looks at the work
item's state (phase + metadata) and the queue entry type to return
one of five canonical modes. The prompt / context assembly branches
on this value so each mode gets the right context block injected.

    EXECUTE    — do the work yourself (leaf role, no children)
    DELEGATE   — break into subtasks (manager role, no children yet)
    REVIEW     — evaluate a subordinate's deliverable and emit a verdict
    INTEGRATE  — parent resumes after all children APPROVED; produce the
                 rolled-up deliverable for upstream review
    REWORK     — reviewer rejected your prior turn; address the feedback
    REPORT     — worker DONE; resume the same session under a dedicated
                 prompt to produce a structured handoff for the reviewer

The classifier is pure: given the same work item + queue entry kind,
it always returns the same mode. It does **not** load the store; all
state must be present on the work item (phase + metadata). This is a
deliberate tradeoff so the mode can be recomputed cheaply at any
point in the dispatcher or context-assembly path.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from opc.core.models import Phase


MANAGER_DISPATCH_TURN_METADATA_KEYS: tuple[str, ...] = (
    "manager_board_mutation_performed",
    "manager_board_modified_work_item_ids",
    "manager_board_deleted_work_item_ids",
    "manager_no_delegation_justification",
    "no_delegation_justification",
    "manager_dispatch_guard_unresolved",
)

GATE_HARNESS_REWORK_METADATA_KEYS: tuple[str, ...] = (
    "gate_harness_rework_feedback",
    "gate_harness_rework_count",
    "gate_harness_rework_request",
)


def reset_manager_dispatch_turn_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return mutable metadata with prior manager-turn outcomes removed.

    These keys describe what happened in one agent turn.  They must not be
    carried into a retry, rework turn, or user follow-up; durable board state
    (dependencies and child mutation revisions) is intentionally untouched.
    """
    result = dict(metadata or {})
    for key in MANAGER_DISPATCH_TURN_METADATA_KEYS:
        result.pop(key, None)
    return result


class TurnMode(str, Enum):
    EXECUTE = "execute"
    DELEGATE = "delegate"
    REVIEW = "review"
    INTEGRATE = "integrate"
    REWORK = "rework"
    REPORT = "report"


def _as_phase(value: Any) -> Phase | None:
    if isinstance(value, Phase):
        return value
    if isinstance(value, str):
        try:
            return Phase(value.strip().lower())
        except Exception:
            return None
    return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def safe_rework_count(value: Any) -> int:
    """Return one non-negative rework count without trusting durable JSON."""

    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def authoritative_gate_harness_rework_metadata(
    *,
    task_metadata: Mapping[str, Any] | None,
    work_item_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Select the sole authority for one deterministic rework attempt.

    Controller-mode gate state is Task-owned.  Presence of *any* Task gate
    field therefore selects the Task group as a whole, including explicit
    empty/zero values that clear stale WorkItem projections.  A WorkItem is a
    compatibility fallback only when the Task has no gate fields at all.
    """

    task = _as_mapping(task_metadata)
    work_item = _as_mapping(work_item_metadata)
    if any(key in task for key in GATE_HARNESS_REWORK_METADATA_KEYS):
        return {
            key: task[key]
            for key in GATE_HARNESS_REWORK_METADATA_KEYS
            if key in task
        }
    return {
        key: work_item[key]
        for key in GATE_HARNESS_REWORK_METADATA_KEYS
        if key in work_item
    }


def has_gate_harness_rework(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether normalized gate metadata represents a rework turn."""

    normalized = _as_mapping(metadata)
    feedback = str(
        normalized.get("gate_harness_rework_feedback", "") or ""
    ).strip()
    request = _as_mapping(normalized.get("gate_harness_rework_request", {}))
    count = safe_rework_count(normalized.get("gate_harness_rework_count", 0))
    return bool(feedback or request or count > 0)


def infer_turn_mode(
    work_item: Any,
    *,
    is_review_entry: bool = False,
    supplemental_metadata: Mapping[str, Any] | None = None,
) -> TurnMode:
    """Classify the turn the agent is about to run.

    ``work_item`` is the DelegationWorkItem (or any object exposing
    the same ``phase`` / ``kind`` / ``metadata`` attributes).
    ``is_review_entry`` should be True when the dispatcher popped a
    ``review-work-item::`` queue entry — those are always reviews,
    even if the underlying work_item metadata is ambiguous.
    ``supplemental_metadata`` is a narrow Task-side projection used for
    controller-owned gate-harness rework fields that are not mirrored onto
    the claimed WorkItem.
    """
    metadata = _as_mapping(getattr(work_item, "metadata", None))
    # Deterministic gate rework is recorded on the durable Task because the
    # Task owns the validator/result envelope.  The dispatcher claims the
    # linked WorkItem (and flips it to RUNNING) before prompt assembly, while
    # the WorkItem itself may therefore have neither a rework phase nor these
    # Task-owned fields.  Accept the narrow supplemental projection so the
    # stateless classifier can still recover the actual turn semantics without
    # letting unrelated Task metadata override WorkItem identity or priority.
    # Task presence is authoritative even when its current value is empty/zero;
    # otherwise a stale WorkItem projection can resurrect an older rework.
    gate_metadata = authoritative_gate_harness_rework_metadata(
        task_metadata=supplemental_metadata,
        work_item_metadata=metadata,
    )
    kind = str(getattr(work_item, "kind", "") or "").strip().lower()
    phase = _as_phase(getattr(work_item, "phase", None))

    # Priority 0: report turn. The hidden auxiliary card spawned after
    # a worker DONE so the same session can produce a structured
    # handoff before the reviewer is invoked. Detected purely from the
    # work item's metadata flag or kind.
    if (
        bool(metadata.get("report_execution_work_item", False))
        or kind == "report"
    ):
        return TurnMode.REPORT

    # Priority 1: review turn. Either the queue entry tag says so,
    # the work item is explicitly marked as the hidden review card,
    # or kind == "review".
    if (
        is_review_entry
        or bool(metadata.get("review_execution_work_item", False))
        or kind == "review"
    ):
        return TurnMode.REVIEW

    # Priority 2: rework. Phase READY_FOR_REWORK is the canonical
    # signal, but the dispatcher flips the work item to RUNNING
    # before the prompt is built — by the time the agent runs we
    # may only see RUNNING. Fall back to the metadata trail the
    # reviewer leaves: ``rework_feedback`` is set on rejection and
    # cleared on approval, and ``review_rework_count`` increments
    # on each rejection.  Deterministic gate rework uses the parallel
    # ``gate_harness_rework_*`` trail.  Any of those signals means the
    # previous output must be corrected, so render this as REWORK.
    if phase == Phase.READY_FOR_REWORK:
        return TurnMode.REWORK
    rework_feedback = str(metadata.get("rework_feedback", "") or "").strip()
    rework_count = safe_rework_count(metadata.get("review_rework_count", 0))
    if rework_feedback or rework_count > 0 or has_gate_harness_rework(gate_metadata):
        return TurnMode.REWORK

    # Priority 3: integrate. The parent has dependency_work_item_ids
    # (it delegated previously) AND is currently runnable (RUNNING /
    # READY). That can only mean children have completed and the
    # parent is being dispatched for its integration turn. The
    # metadata.frontier == "resumed" flag is also set by the wake
    # edge when present.
    dependency_ids = [
        str(x).strip()
        for x in list(metadata.get("dependency_work_item_ids", []) or [])
        if str(x).strip()
    ]
    frontier = str(metadata.get("frontier", "") or "").strip().lower()
    if dependency_ids and (
        phase in {Phase.RUNNING, Phase.READY} or frontier == "resumed"
    ):
        return TurnMode.INTEGRATE

    # Priority 4: delegate. Manager role with nothing spawned yet.
    allowed_delegate_role_ids = [
        str(x).strip()
        for x in list(metadata.get("allowed_delegate_role_ids", []) or [])
        if str(x).strip()
    ]
    if allowed_delegate_role_ids and not dependency_ids:
        return TurnMode.DELEGATE

    # Default: worker executing their own work item.
    return TurnMode.EXECUTE
