"""Pure planning for company work-item completion and gate rework.

The normal worker-completion path and the durable owner-gate consumer both
use these functions.  Keeping the routing decision free of Store/runtime
state prevents an answered gate from growing a second, subtly different
completion state machine.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opc.core.models import DelegationWorkItem, Phase, Task
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemGatePolicy,
)
from opc.layer2_organization.work_item_identity import (
    canonical_work_item_turn_type_for_kind,
    gate_rework_payload,
    is_delivery_turn,
    is_manager_reviewable_turn,
    projection_id_for_task,
    rework_projection_id_for_gate,
    target_projection_ids_for_decision,
    turn_type_for_task,
    work_item_identity_payload_for_task,
)


_DELEGATION_OUTPUT_TURN_TYPES = frozenset({"dispatch", "intake", "plan"})
_ROUTING_METADATA_FROM_WORK_ITEM = (
    "user_visible",
    "authoritative_output",
    "review_owner_kind",
    "requires_user_feedback",
    "feedback_scope",
)

COMPANY_REWORK_OUTPUT_METADATA_KEYS = (
    "completion_report",
    "work_item_summary",
    "work_item_summary_for_downstream",
    "work_item_artifact_index",
    "verification_status",
    "verification_evidence",
    "verification",
    "structured_review_verdict",
    "delivery_package",
    "downstream_assignments",
    "artifacts",
    "automated_verification_results",
    "final_feedback_evaluation",
    "feedback_followup_message",
    "gate_harness_status",
    "gate_harness_constraints",
    "gate_harness_pending_decision",
    "gate_harness_decision",
    "gate_harness_evidence",
)


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _is_final_human_acceptance(metadata: Mapping[str, Any]) -> bool:
    if _flag(metadata.get("attention_work_item", False)):
        return False
    if not _flag(metadata.get("authoritative_output", False)):
        return False
    if not _flag(metadata.get("user_visible", False)):
        return False
    if str(metadata.get("feedback_scope", "") or "").strip().lower() != "final":
        return False
    return bool(
        is_delivery_turn(metadata)
        or str(metadata.get("review_owner_kind", "") or "").strip().lower()
        == "human"
    )


@dataclass(frozen=True)
class CompanyWorkItemDoneRoutingPlan:
    """Canonical durable route after one worker output has been accepted."""

    target_phase: Phase
    task_metadata: dict[str, Any]
    work_item_metadata_updates: dict[str, Any]
    manager_turn_context: dict[str, str]
    review_owner_role_id: str
    review_owner_seat_id: str
    is_attention_work_item: bool


def plan_company_work_item_done_routing(
    task: Task,
    work_item: DelegationWorkItem,
    *,
    summary: str = "",
) -> CompanyWorkItemDoneRoutingPlan:
    """Return the unique Task/WorkItem routing plan for accepted output.

    Only durable Task and WorkItem fields participate.  In particular no
    process-local org lookup is used, so a normal completion and a gate reply
    recompute the same answer inside different controller generations.
    """

    item_metadata = dict(work_item.metadata or {})
    task_metadata = copy.deepcopy(dict(task.metadata or {}))
    for key in _ROUTING_METADATA_FROM_WORK_ITEM:
        if key not in task_metadata and key in item_metadata:
            task_metadata[key] = copy.deepcopy(item_metadata[key])

    raw_work_kind = turn_type_for_task(
        task,
        fallback=str(task_metadata.get("work_kind", "") or "execute"),
    )
    work_kind = canonical_work_item_turn_type_for_kind(raw_work_kind, fallback="")
    work_item_id = str(work_item.work_item_id or "").strip()
    attention_id = str(task_metadata.get("attention_work_item_id", "") or "").strip()
    is_attention = bool(
        _flag(task_metadata.get("attention_work_item", False))
        or _flag(item_metadata.get("attention_work_item", False))
        or (attention_id and attention_id == work_item_id)
    )
    manager_reviewable = is_manager_reviewable_turn(work_kind) if work_kind else True
    is_delivery = bool(
        is_delivery_turn(task_metadata)
        or str(task_metadata.get("review_owner_kind", "") or "").strip().lower()
        == "human"
    )
    manager_context: dict[str, str] = {}
    if (
        not manager_reviewable
        and not is_attention
        and not is_delivery
        and work_kind in _DELEGATION_OUTPUT_TURN_TYPES
    ):
        board_mutated = _flag(
            task_metadata.get("manager_board_mutation_performed", False)
        )
        execution_choice = str(
            task_metadata.get("manager_execution_choice", "") or ""
        ).strip().lower()
        if execution_choice == "delegated":
            board_mutated = True
        justification = str(
            task_metadata.get("manager_no_delegation_justification", "") or ""
        ).strip()
        unresolved = str(
            task_metadata.get("manager_dispatch_guard_unresolved", "") or ""
        ).strip()
        manager_context = {
            "outcome": "delegated" if board_mutated else "self_produced",
            "source": (
                "manager_decision"
                if execution_choice in {"delegated", "direct"}
                else "board_mutation"
                if board_mutated
                else "justified"
                if justification
                else "dispatch_guard_exhausted"
                if unresolved
                else "no_board_mutation"
            ),
        }
        note = justification or unresolved
        if note:
            manager_context["note"] = note
        manager_role = str(
            task_metadata.get("manager_role_id")
            or work_item.manager_role_id
            or ""
        ).strip()
        if not board_mutated and manager_role and manager_role != "owner":
            manager_reviewable = True
        elif not board_mutated and manager_role in {"", "owner"}:
            # A top-seat manager that completes the company outcome directly
            # has no organizational manager to review it.  Treat that output
            # as the authoritative delivery and route it to the owner instead
            # of silently auto-approving an unreviewed result.
            task_metadata.update(
                {
                    "authoritative_output": True,
                    "user_visible": True,
                    "feedback_scope": "final",
                    "review_owner_kind": "human",
                    "requires_user_feedback": True,
                }
            )
            is_delivery = True

    final_human_acceptance = bool(
        str(task_metadata.get("execution_mode", "") or "").strip()
        == "company_mode"
        and _flag(task_metadata.get("requires_user_feedback", False))
        and _is_final_human_acceptance(task_metadata)
    )
    if is_attention:
        target_phase = Phase.APPROVED
    elif is_delivery:
        target_phase = (
            Phase.AWAITING_HUMAN
            if final_human_acceptance
            else Phase.APPROVED
        )
    elif not manager_reviewable:
        target_phase = Phase.APPROVED
    else:
        target_phase = Phase.AWAITING_MANAGER_REVIEW

    review_owner_role_id = str(
        task_metadata.get("manager_role_id")
        or work_item.manager_role_id
        or ""
    ).strip()
    review_owner_seat_id = str(
        task_metadata.get("manager_seat_id")
        or work_item.manager_seat_id
        or ""
    ).strip()
    if target_phase == Phase.AWAITING_MANAGER_REVIEW and not review_owner_role_id:
        target_phase = Phase.APPROVED

    metadata_updates: dict[str, Any] = {
        **work_item_identity_payload_for_task(task),
        "adaptive": copy.deepcopy(dict(task_metadata.get("adaptive", {}) or {})),
    }
    if is_attention:
        metadata_updates["attention_work_item_outcome"] = "completed"
    if target_phase in {Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN}:
        metadata_updates["review_owner_role_id"] = review_owner_role_id
        metadata_updates["review_owner_seat_id"] = review_owner_seat_id
        if str(summary or "").strip():
            metadata_updates["completion_report"] = str(summary or "").strip()
    return CompanyWorkItemDoneRoutingPlan(
        target_phase=target_phase,
        task_metadata=task_metadata,
        work_item_metadata_updates=metadata_updates,
        manager_turn_context=manager_context,
        review_owner_role_id=review_owner_role_id,
        review_owner_seat_id=review_owner_seat_id,
        is_attention_work_item=is_attention,
    )


def company_gate_rework_target_projection_ids(
    gate: WorkItemGatePolicy,
    *,
    pending_harness_decision: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return the exact ordered target set encoded by the gate source."""

    gate_metadata = dict(gate.metadata or {})
    if str(gate_metadata.get("source", "") or "").strip() == "gate_harness":
        return target_projection_ids_for_decision(
            dict(pending_harness_decision or {})
        )
    target = rework_projection_id_for_gate(gate)
    return [target] if target else []


def company_gate_rework_affected_projection_ids(
    gate: WorkItemGatePolicy,
    *,
    pending_harness_decision: Mapping[str, Any] | None = None,
    plan: CompanyWorkItemRuntimePlan | None = None,
) -> list[str]:
    """Return the canonical rework closure for one durable gate decision.

    Ordinary work-item gates reopen the review source and the policy's exact
    target.  Gate-harness decisions additionally invalidate every transitive
    downstream consumer of each explicit target, matching the live controller
    path.  The caller still adds the review source itself when it is not part
    of this projection closure.
    """

    targets = company_gate_rework_target_projection_ids(
        gate,
        pending_harness_decision=pending_harness_decision,
    )
    if (
        str(dict(gate.metadata or {}).get("source", "") or "").strip()
        != "gate_harness"
        or plan is None
    ):
        return targets
    return [
        projection_id
        for projection_id, _explicit_target, _upstream_target in (
            company_gate_harness_rework_entries(
                gate,
                pending_harness_decision=pending_harness_decision,
                plan=plan,
            )
        )
    ]


def company_gate_harness_rework_entries(
    gate: WorkItemGatePolicy,
    *,
    pending_harness_decision: Mapping[str, Any] | None = None,
    plan: CompanyWorkItemRuntimePlan | None = None,
) -> list[tuple[str, bool, str]]:
    """Return ``(projection, explicit_target, upstream_target)`` in order."""

    targets = company_gate_rework_target_projection_ids(
        gate,
        pending_harness_decision=pending_harness_decision,
    )
    if not targets:
        return []
    if plan is None:
        return [(target, True, target) for target in targets]
    dependents: dict[str, list[str]] = {}
    for projection in list(plan.projections or []):
        projection_id = str(projection.projection_id or "").strip()
        for dependency_id in list(projection.dependency_projection_ids or []):
            dependency = str(dependency_id or "").strip()
            if dependency and projection_id:
                dependents.setdefault(dependency, []).append(projection_id)
    explicit = set(targets)
    seen: set[str] = set()
    entries: list[tuple[str, bool, str]] = []
    for target in targets:
        queue = [target]
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            entries.append((current, current == target, target))
            for dependent in dependents.get(current, []):
                if dependent in explicit and dependent != target:
                    continue
                queue.append(dependent)
    return entries


def build_company_gate_harness_rework_record(
    *,
    source_task: Task,
    target_task: Task,
    decision: Mapping[str, Any],
    rework_round: int,
    requested_at: datetime | str,
) -> dict[str, Any]:
    """Build the canonical per-target gate-harness rework record."""

    requested = (
        requested_at.isoformat()
        if isinstance(requested_at, datetime)
        else str(requested_at or "").strip()
    )
    target_projection_id = projection_id_for_task(target_task)
    return {
        "source_projection_id": projection_id_for_task(source_task),
        "source_work_item_title": source_task.title,
        **gate_rework_payload(target_projection_id=target_projection_id),
        "target_work_item_title": target_task.title,
        "feedback": str(decision.get("summary", "") or "").strip(),
        "blockers": list(decision.get("blockers", []) or []),
        "blocker_types": list(decision.get("blocker_types", []) or []),
        "constraints": list(decision.get("constraints", []) or []),
        "rework_round": int(rework_round),
        "requested_at": requested,
    }


def build_company_gate_rework_record(
    *,
    review_task: Task,
    gate: WorkItemGatePolicy,
    reviewer_feedback: str,
    rework_round: int,
    requested_at: datetime | str,
) -> dict[str, Any]:
    """Build the canonical feedback record persisted on source and targets."""

    reviewer_role = str(
        gate.reviewer_role
        or review_task.assigned_to
        or review_task.metadata.get("work_item_role_id", "")
        or ""
    ).strip()
    target_ids = company_gate_rework_target_projection_ids(
        gate,
        pending_harness_decision=dict(
            review_task.metadata.get("gate_harness_pending_decision", {}) or {}
        ),
    )
    target_projection_id = target_ids[0] if target_ids else ""
    timestamp = (
        requested_at.isoformat()
        if isinstance(requested_at, datetime)
        else str(requested_at or "").strip()
    )
    return {
        "review_task_id": review_task.id,
        **gate_rework_payload(
            review_projection_id=projection_id_for_task(review_task),
            target_projection_id=target_projection_id,
            rework_projection_id=target_projection_id,
        ),
        "target_projection_ids": list(target_ids),
        "review_work_item_title": review_task.title,
        "reviewer_role": reviewer_role,
        "feedback": str(reviewer_feedback or "").strip(),
        "gate_instructions": gate.instructions,
        "rework_round": int(rework_round),
        "requested_at": timestamp,
    }
