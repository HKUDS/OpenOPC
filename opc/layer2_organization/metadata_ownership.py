"""Company-mode metadata ownership rules.

This module is the executable owner matrix for WorkItem/runtime Task
metadata. In company mode, DelegationWorkItem owns business/collaboration
state; runtime Task owns execution/session/audit state. Task may carry a
small execution-copy envelope, but those copies are not scheduling facts.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from opc.core.models import DelegationWorkItem, Task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task


class MetadataOwner(str, Enum):
    WORK_ITEM = "work_item"
    RUNTIME_TASK = "runtime_task"
    EXECUTION_COPY = "execution_copy"


@dataclass(frozen=True)
class MetadataFieldSpec:
    key: str
    owner: MetadataOwner
    allowed_locations: tuple[str, ...] = ("work_item",)
    legacy_fallback: bool = False
    migration_policy: str = ""
    description: str = ""

    @property
    def allows_task_execution_copy(self) -> bool:
        return "task_execution_copy" in self.allowed_locations

    @property
    def allows_task_legacy_read(self) -> bool:
        return "task_legacy_read" in self.allowed_locations or self.legacy_fallback


@dataclass(frozen=True)
class MetadataOwnershipIssue:
    code: str
    severity: str
    key: str
    owner: str
    work_item_id: str = ""
    runtime_task_id: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataOwnershipMigrationChange:
    work_item_id: str
    runtime_task_id: str
    updates: dict[str, Any]
    conflicts: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataOwnershipMigrationReport:
    dry_run: bool
    scanned_work_items: int
    changed_work_items: int
    changes: tuple[MetadataOwnershipMigrationChange, ...]
    issues: tuple[MetadataOwnershipIssue, ...] = ()


def _spec(
    key: str,
    owner: MetadataOwner,
    *,
    allowed_locations: Iterable[str] | None = None,
    legacy_fallback: bool = False,
    migration_policy: str = "",
    description: str = "",
) -> MetadataFieldSpec:
    if allowed_locations is None:
        allowed_locations = ("work_item",) if owner == MetadataOwner.WORK_ITEM else ("task",)
    return MetadataFieldSpec(
        key=key,
        owner=owner,
        allowed_locations=tuple(allowed_locations),
        legacy_fallback=legacy_fallback,
        migration_policy=migration_policy,
        description=description,
    )


_WORK_ITEM_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec("work_kind", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("current_turn_mode", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("dependency_work_item_ids", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("waiting_on_work_item_ids", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("delegated_children_pending", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("handoff_context", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("context_preview", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("prompt_contract", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("review_target_prompt_contract", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("report_target_prompt_contract", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("prompt_contract_blocker", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("manager_mutation_revision", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_action", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_reason", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_at", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_by_role_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_by_seat_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("manager_mutation_user_input", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("latest_user_directive", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("progress_log", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_legacy_read"), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("work_item_role_name", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("employee_assignment", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("employee_prompt_context", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("employee_delta_context", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("completion_report", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("deliverable_summary", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("work_item_summary", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("work_item_summary_for_downstream", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("work_item_artifact_index", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("verification_status", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("verification_evidence", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("verification", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("structured_review_verdict", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("review_owner_role_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_owner_seat_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_attempt", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_attempt_count", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_target_work_item_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_source_report_work_item_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("review_resolution", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("review_resolution_state", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("review_resolution_applied_work_item_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("review_target_worker_task_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_target_worker_role_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_target_worker_seat_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_completion_report", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_target_title", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_target_description", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_evidence", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("rework_feedback", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_feedback_version", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_rework_count", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_retry_hint", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_retry_of_attempt", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("review_retry_reason", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_attempt", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_attempt_count", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_work_item_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_worker_task_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_worker_role_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_worker_seat_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_title", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_target_description", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_source_summary", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_source_result_content", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("report_source_evidence", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("follow_up_actions", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("follow_up_action", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("follow_up_reason", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("follow_up_dedupe_key", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("synthesis_turn_started", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("synthesis_ready_at", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("synthesis_source_work_item_ids", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("synthesis_reports_to_role_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("synthesis_reports_to_seat_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), legacy_fallback=True, migration_policy="work_item_wins"),
    _spec("delivery_package", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("downstream_assignments", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("open_questions", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("assumptions", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("decisions", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("risks", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), legacy_fallback=True, migration_policy="backfill_if_missing"),
    _spec("self_evolution_work_item", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_root", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_checkpoint_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_human_action", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_human_feedback", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_delivery_task_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_delivery_projection_id", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_delivery_summary", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_patch_max_retries", MetadataOwner.WORK_ITEM, allowed_locations=("work_item", "task_execution_copy"), migration_policy="work_item_wins"),
    _spec("self_evolution_recorded", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("self_evolution_patch", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("self_evolution_completed_at", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("self_evolution_error", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    # Attempt ledger (dispatch-attempt accounting; see phase.py). Opened by the
    # claim CAS, settled by transition_work_item — the dispatcher's structural
    # brake against crash/interrupted re-dispatch loops.
    _spec("attempt_seq", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_settled", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_outcome", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_started_at", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_settled_at", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_crash_streak", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_interrupted_streak", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
    _spec("attempt_ledger_block_reason", MetadataOwner.WORK_ITEM, allowed_locations=("work_item",), migration_policy="work_item_wins"),
)

_RUNTIME_TASK_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec("runtime_v2", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_verification", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_verification_evidence", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_verification_status", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("member_session_state", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("external_resume_session_id", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("external_resume_agent_type", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("working_memory", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("interrupted_recovery", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("last_stop_reason", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("peer_wait", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("comms_cross_role_history", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("comms_last_blocked_reactivation_at", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("comms_last_blocked_reactivation_key", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_control_state", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("automated_verification_results", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_session_team_instance_id", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_session_team_id", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
    _spec("runtime_session_seat_id", MetadataOwner.RUNTIME_TASK, allowed_locations=("task",), migration_policy="task_only"),
)

_EXECUTION_COPY_FIELDS: tuple[MetadataFieldSpec, ...] = (
    _spec("mode", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("execution_mode", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("execution_model", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("runtime_model", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("original_message", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("company_profile", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_playbook", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("runtime_topology", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("organization_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("org_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_run_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_cell_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_team_instance_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_team_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_seat_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("delegation_role_session_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("work_item_role_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("seat_manager_role_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("manager_role_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("manager_seat_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("managed_team_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("seat_contact_role_ids", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("allowed_delegate_role_ids", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("force_native_execution", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("preferred_external_agent", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("selected_execution_agent", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("execution_agent_locked", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("selected_execution_agent_source", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("work_item_execution_strategy", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("adaptive", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("execution_task_ids", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("parent_session_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("work_item_batch_id", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("target_output_dir", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("output_root", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("workspace_root", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("comms_workspace_root", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("comms_root", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("user_visible", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("authoritative_output", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("review_owner_kind", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("requires_user_feedback", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("feedback_scope", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("review_task", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("review_execution_work_item", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("report_execution_work_item", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
    _spec("skip_work_item_sync", MetadataOwner.EXECUTION_COPY, allowed_locations=("task_execution_copy",), migration_policy="copy_from_runtime_context"),
)

METADATA_FIELD_SPECS: dict[str, MetadataFieldSpec] = {
    spec.key: spec
    for spec in (
        *_WORK_ITEM_FIELDS,
        *_RUNTIME_TASK_FIELDS,
        *_EXECUTION_COPY_FIELDS,
    )
}

WORK_ITEM_OWNED_KEYS: frozenset[str] = frozenset(
    key for key, spec in METADATA_FIELD_SPECS.items() if spec.owner == MetadataOwner.WORK_ITEM
)
RUNTIME_TASK_OWNED_KEYS: frozenset[str] = frozenset(
    key for key, spec in METADATA_FIELD_SPECS.items() if spec.owner == MetadataOwner.RUNTIME_TASK
)
EXECUTION_COPY_KEYS: frozenset[str] = frozenset(
    key
    for key, spec in METADATA_FIELD_SPECS.items()
    if spec.owner == MetadataOwner.EXECUTION_COPY or spec.allows_task_execution_copy
)
LEGACY_READONLY_KEYS: frozenset[str] = frozenset()


def metadata_owner_for_key(key: str) -> MetadataOwner | None:
    spec = METADATA_FIELD_SPECS.get(str(key or "").strip())
    return spec.owner if spec is not None else None


def metadata_spec_for_key(key: str) -> MetadataFieldSpec | None:
    return METADATA_FIELD_SPECS.get(str(key or "").strip())


def is_work_item_owned_key(key: str) -> bool:
    return metadata_owner_for_key(key) == MetadataOwner.WORK_ITEM


def is_runtime_task_owned_key(key: str) -> bool:
    return metadata_owner_for_key(key) == MetadataOwner.RUNTIME_TASK


def is_execution_copy_key(key: str) -> bool:
    return str(key or "").strip() in EXECUTION_COPY_KEYS


def supports_legacy_task_fallback(key: str) -> bool:
    spec = metadata_spec_for_key(key)
    return bool(spec and spec.legacy_fallback)


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def copy_work_item_execution_metadata(
    work_item: DelegationWorkItem | None,
    *,
    keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return WorkItem-owned values allowed on Task as execution copies."""
    if work_item is None:
        return {}
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    selected = list(keys) if keys is not None else sorted(EXECUTION_COPY_KEYS)
    copied: dict[str, Any] = {}
    for key in selected:
        spec = metadata_spec_for_key(key)
        if spec is None or not spec.allows_task_execution_copy:
            continue
        if key not in metadata:
            continue
        value = metadata.get(key)
        if not _has_value(value):
            continue
        copied[key] = copy.deepcopy(value)
    return copied


def build_work_item_owner_execution_copy(work_item: DelegationWorkItem | None) -> dict[str, Any]:
    """Build the runtime Task owner envelope from the WorkItem facts."""
    if work_item is None:
        return {}
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    payload = {
        "delegation_run_id": str(getattr(work_item, "run_id", "") or "").strip(),
        "delegation_cell_id": str(getattr(work_item, "cell_id", "") or "").strip(),
        "delegation_team_instance_id": str(getattr(work_item, "team_instance_id", "") or "").strip(),
        "delegation_team_id": str(
            getattr(work_item, "team_id", "")
            or metadata.get("team_id", "")
            or getattr(work_item, "cell_id", "")
            or ""
        ).strip(),
        "delegation_seat_id": str(
            getattr(work_item, "seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip(),
        "delegation_role_session_id": str(
            getattr(work_item, "role_runtime_session_id", "")
            or metadata.get("assigned_role_runtime_id", "")
            or ""
        ).strip(),
        "work_item_role_id": str(getattr(work_item, "role_id", "") or metadata.get("role_id", "") or "").strip(),
        "work_kind": str(metadata.get("work_kind", "") or getattr(work_item, "kind", "") or "").strip().lower(),
        "manager_role_id": str(getattr(work_item, "manager_role_id", "") or metadata.get("manager_role_id", "") or "").strip(),
        "manager_seat_id": str(getattr(work_item, "manager_seat_id", "") or metadata.get("manager_seat_id", "") or "").strip(),
        "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
    }
    return {
        key: copy.deepcopy(value)
        for key, value in payload.items()
        if key in EXECUTION_COPY_KEYS and _has_value(value)
    }


def strip_disallowed_work_item_metadata_from_runtime_task(task: Task) -> list[str]:
    """Remove WorkItem-owned fields that are not valid Task execution copies."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    removed: list[str] = []
    for key in sorted(WORK_ITEM_OWNED_KEYS):
        if key in EXECUTION_COPY_KEYS:
            continue
        if key in metadata:
            metadata.pop(key, None)
            removed.append(key)
    if removed:
        task.metadata = metadata
    return removed


def filter_work_item_owned_metadata(updates: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in dict(updates or {}).items()
        if is_work_item_owned_key(str(key)) and _has_value(value)
    }


def filter_runtime_task_owned_metadata(updates: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(value)
        for key, value in dict(updates or {}).items()
        if is_runtime_task_owned_key(str(key)) and _has_value(value)
    }


def validate_metadata_ownership(
    work_item: DelegationWorkItem | None,
    task: Task | None,
) -> list[MetadataOwnershipIssue]:
    """Read-only diagnostics for WorkItem/runtime Task metadata drift."""
    if work_item is None and task is None:
        return []
    item_metadata = dict(getattr(work_item, "metadata", {}) or {}) if work_item is not None else {}
    task_metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip() or linked_work_item_id_for_task(task)
    runtime_task_id = str(getattr(task, "id", "") or "").strip()
    issues: list[MetadataOwnershipIssue] = []

    for key in sorted(WORK_ITEM_OWNED_KEYS):
        if key not in task_metadata:
            continue
        task_value = task_metadata.get(key)
        if not _has_value(task_value):
            continue
        work_item_has_value = key in item_metadata and _has_value(item_metadata.get(key))
        if not work_item_has_value:
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_violation",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.WORK_ITEM.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"WorkItem-owned metadata `{key}` is present on runtime Task but missing on WorkItem.",
                )
            )
            continue
        if item_metadata.get(key) != task_value and not is_execution_copy_key(key):
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_conflict",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.WORK_ITEM.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"WorkItem-owned metadata `{key}` differs between WorkItem and runtime Task; WorkItem wins.",
                    details={"work_item_value": item_metadata.get(key), "task_value": task_value},
                )
            )

    for key in sorted(RUNTIME_TASK_OWNED_KEYS):
        if key in item_metadata and _has_value(item_metadata.get(key)):
            issues.append(
                MetadataOwnershipIssue(
                    code="metadata_ownership_violation",
                    severity="warning",
                    key=key,
                    owner=MetadataOwner.RUNTIME_TASK.value,
                    work_item_id=work_item_id,
                    runtime_task_id=runtime_task_id,
                    message=f"Runtime Task-owned metadata `{key}` should not be stored on WorkItem.",
                )
            )
    return issues


async def update_work_item_owned_metadata(
    store: Any,
    work_item_id: str,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist only WorkItem-owned metadata updates through the store."""
    filtered = filter_work_item_owned_metadata(updates)
    if not filtered or store is None or not hasattr(store, "update_delegation_work_item"):
        return {}
    await store.update_delegation_work_item(work_item_id, metadata_updates=filtered)
    return filtered


async def append_work_item_progress(
    store: Any,
    work_item_id: str,
    message: str,
    *,
    limit: int = 20,
    dedupe: bool = False,
) -> list[Any]:
    """Append user-visible progress to the WorkItem-owned progress log."""
    wid = str(work_item_id or "").strip()
    note = str(message or "").strip()
    if not wid or not note or store is None or not hasattr(store, "get_delegation_work_item"):
        return []
    try:
        work_item = await store.get_delegation_work_item(wid)
    except Exception:
        return []
    if work_item is None:
        return []
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    progress = list(metadata.get("progress_log", []) or [])
    if not dedupe or note not in progress:
        progress.append(note)
    progress = progress[-max(1, int(limit or 20)) :]
    await update_work_item_owned_metadata(store, wid, {"progress_log": progress})
    return progress


async def sync_work_item_current_turn_mode(
    store: Any,
    work_item_id: str,
    current_turn_mode: str,
) -> bool:
    """Persist the WorkItem-owned current turn mode from runtime evaluation."""
    wid = str(work_item_id or "").strip()
    mode = str(current_turn_mode or "").strip()
    if not wid or not mode:
        return False
    return bool(await update_work_item_owned_metadata(store, wid, {"current_turn_mode": mode}))


def update_runtime_task_owned_metadata(task: Task, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Apply only runtime Task-owned metadata updates to an in-memory Task."""
    filtered = filter_runtime_task_owned_metadata(updates)
    if not filtered:
        return {}
    task.metadata = {**dict(task.metadata or {}), **filtered}
    return filtered


async def migrate_work_item_owned_metadata_from_linked_tasks(
    store: Any,
    *,
    run_id: str | None = None,
    work_item_ids: Iterable[str] | None = None,
    dry_run: bool = True,
) -> MetadataOwnershipMigrationReport:
    """Explicit maintenance backfill from legacy Task metadata to WorkItem.

    This is intentionally not called from hot read paths. WorkItem wins on
    conflicts; only missing WorkItem-owned keys are backfilled.
    """
    if store is None:
        return MetadataOwnershipMigrationReport(True, 0, 0, ())

    items: list[DelegationWorkItem] = []
    if work_item_ids is not None:
        getter = getattr(store, "get_delegation_work_item", None)
        if callable(getter):
            for raw_id in work_item_ids:
                wid = str(raw_id or "").strip()
                if not wid:
                    continue
                item = await getter(wid)
                if item is not None:
                    items.append(item)
    elif run_id:
        lister = getattr(store, "list_delegation_work_items", None)
        if callable(lister):
            items = list(await lister(str(run_id).strip()))

    get_runtime_task = getattr(store, "get_runtime_task_for_work_item", None)
    changes: list[MetadataOwnershipMigrationChange] = []
    issues: list[MetadataOwnershipIssue] = []
    for item in items:
        wid = str(getattr(item, "work_item_id", "") or "").strip()
        if not wid or not callable(get_runtime_task):
            continue
        task = await get_runtime_task(wid)
        if task is None:
            continue
        item_metadata = dict(item.metadata or {})
        task_metadata = dict(task.metadata or {})
        updates: dict[str, Any] = {}
        conflicts: dict[str, dict[str, Any]] = {}
        for key in sorted(WORK_ITEM_OWNED_KEYS):
            if key not in task_metadata or not _has_value(task_metadata.get(key)):
                continue
            if key not in item_metadata or not _has_value(item_metadata.get(key)):
                spec = metadata_spec_for_key(key)
                if spec is not None and spec.legacy_fallback:
                    updates[key] = copy.deepcopy(task_metadata.get(key))
                continue
            if item_metadata.get(key) != task_metadata.get(key):
                conflicts[key] = {
                    "work_item_value": copy.deepcopy(item_metadata.get(key)),
                    "task_value": copy.deepcopy(task_metadata.get(key)),
                }
        if conflicts:
            for key in sorted(conflicts):
                issues.append(
                    MetadataOwnershipIssue(
                        code="metadata_ownership_conflict",
                        severity="warning",
                        key=key,
                        owner=MetadataOwner.WORK_ITEM.value,
                        work_item_id=wid,
                        runtime_task_id=str(getattr(task, "id", "") or "").strip(),
                        message=f"WorkItem-owned metadata `{key}` conflicts during migration; WorkItem wins.",
                        details=conflicts[key],
                    )
                )
        if not updates:
            continue
        changes.append(
            MetadataOwnershipMigrationChange(
                work_item_id=wid,
                runtime_task_id=str(getattr(task, "id", "") or "").strip(),
                updates=updates,
                conflicts=conflicts,
            )
        )
        if not dry_run and hasattr(store, "update_delegation_work_item"):
            await store.update_delegation_work_item(wid, metadata_updates=updates)

    return MetadataOwnershipMigrationReport(
        dry_run=bool(dry_run),
        scanned_work_items=len(items),
        changed_work_items=len(changes),
        changes=tuple(changes),
        issues=tuple(issues),
    )
