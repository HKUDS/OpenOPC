"""Company work-item planning and execution runtime."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.config import DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS, DEFAULT_ORGANIZATION_ID
from opc.core.models import (
    AdaptiveRoleProfile,
    AdaptiveSignalSpec,
    AdaptiveWorkItemProfile,
    ApprovalAction,
    ArtifactContract,
    CompanyMemberSession,
    CoordinationSpec,
    DataAcquisitionReport,
    DelegationEvent,
    DelegationWorkItem,
    EnvironmentManifest,
    Phase,
    RouterDecision,
    StructuredReviewVerdict,
    Task,
    TaskResult,
    TaskStatus,
    WorkItemExecutionStrategy,
    WorkspaceManifest,
    normalize_role_runtime_status,
)
from opc.core.worker_envelope import classify_worker_message, worker_message_is_actionable
from opc.layer2_organization.company_runtime import CompanyRuntime, canonical_role_session_id
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    TODO_PHASES,
    InvalidPhaseTransition,
    attempt_ledger_dispatch_block_reason,
    has_open_attempt,
    is_dispatchable,
    is_orphaned,
    is_report_execution_work_item_metadata,
    is_review_execution_work_item_metadata,
    is_runtime_auxiliary_work_item,
    is_runnable,
    kanban_column,
    phase_for_task_status,
    should_hide_work_item_from_company_kanban,
    task_status_for_phase,
)
# Import for side effect: registering the phase-transition hooks. The
# serial queue reconciler is also called explicitly from the dispatcher
# tick before runnable work is claimed.
from opc.layer2_organization import phase_hooks  # noqa: F401
from opc.layer2_organization.collaboration_service import CollaborationContext
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata
from opc.layer2_organization.data_acquisition_policy import (
    DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH,
    default_download_manifest_path,
    default_execution_record_path,
    default_source_candidates_path,
    has_downloaded_binary_asset,
    requires_binary_asset_acquisition,
)
from opc.layer2_organization.gate_harness import GateHarness, GateHarnessDecision
from opc.layer2_organization.metadata_ownership import (
    append_work_item_progress,
    build_work_item_owner_execution_copy,
    copy_work_item_execution_metadata,
    strip_disallowed_work_item_metadata_from_runtime_task,
    update_runtime_task_owned_metadata,
    update_work_item_owned_metadata,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.prompt_contract import (
    has_prompt_contract,
    make_prompt_contract,
    prompt_contract_from_work_item,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    deserialize_company_work_item_plan,
    serialize_company_work_item_plan,
)
from opc.layer2_organization.recruiter import (
    normalize_recruitment_agent_choice,
    resolve_effective_execution_agent,
)
from opc.layer2_organization.seat_executor import SeatExecutor
from opc.layer2_organization.work_item_transition import (
    DEPENDENCY_CLASS_DEFAULT,
    compute_doomed_work_item_ids,
    has_pending_settlement_release,
    normalize_dependency_work_item_ids,
    refresh_dependents_for_run,
    settle_open_attempt_as_interrupted,
    settled_failure_dependency_ids,
    transition_work_item,
    transition_work_item_from_task,
)
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_TURN_TYPE_KEY,
    canonical_work_item_turn_type_for_kind,
    gate_rework_payload,
    is_delivery_turn,
    is_manager_reviewable_turn,
    mark_projected_work_item_task,
    mark_gate_rework_projection,
    mark_work_item_projection,
    projection_id_for_task,
    projection_id_for_work_item,
    rework_projection_id_for_gate,
    target_projection_id_for_decision,
    target_projection_ids_for_decision,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
    task_by_linked_work_item_id,
)
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
    work_item_runtime_version,
)
from opc.layer2_organization.work_item_runtime_invariants import (
    WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
    WorkItemRuntimeInvariantIssue,
    diagnose_work_item_runtime_projections,
    validate_work_item_runtime_projection,
)
from opc.layer4_tools.output_budget import clip_text
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry


# Maximum consecutive idle dispatcher ticks (5s each) tolerated while every
# active task waits on a human but at least one waiter has no pending
# checkpoint on record yet (e.g. a park write racing this snapshot).  Once
# exhausted the turn exits with a parked summary instead of spinning forever.
_HUMAN_WAIT_MAX_STALL_TICKS = 24

# Matches the manager dispatch guard's escape line while tolerating the
# markdown decoration models routinely wrap protocol tokens in — bold
# (`**NO_DELEGATION_JUSTIFICATION**:`), headings, quotes, list markers,
# `_`/`-`/space separator variants, and fullwidth colons. A bare
# ``startswith("NO_DELEGATION_JUSTIFICATION:")`` here cost project 4444 its
# CTO card: the justification was written but bold-wrapped, went unparsed,
# and the guard drove the work item to FAILED.
_NO_DELEGATION_JUSTIFICATION_LINE = re.compile(
    r"^\s*(?:>+\s*)*"                     # blockquote markers
    r"(?:[-*+•]\s+|\d+[.)]\s+)?"          # list markers
    r"[\s#*_`~\"']*"                       # heading/bold/quote decoration
    r"NO[_\s-]?DELEGATION[_\s-]?JUSTIFICATION"
    r"[\s*_`~\"']*"                        # decoration between token and colon
    r"[:：]\s*(?P<reason>.*?)\s*$",
    re.IGNORECASE,
)


def review_work_item_id_for_attempt(worker_work_item_id: str, attempt: int) -> str:
    """Compute a per-attempt review work-item ID for a given worker.

    Each AWAITING_MANAGER_REVIEW entry gets a fresh attempt; previous
    attempts remain as immutable history. Module-level so the invariant
    test suite can import it without depending on the surrounding class.
    """
    wid = str(worker_work_item_id or "").strip()
    if not wid:
        raise ValueError("worker_work_item_id is required")
    n = int(attempt)
    if n < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return f"review::{wid}::v{n}"


def report_work_item_id_for_attempt(worker_work_item_id: str, attempt: int) -> str:
    """Compute a per-attempt report work-item ID for a given worker.

    Each worker DONE attempt spawns a fresh hidden report card so the
    worker writes a handoff report on its own session before the
    reviewer is invoked. Mirrors review_work_item_id_for_attempt.
    """
    wid = str(worker_work_item_id or "").strip()
    if not wid:
        raise ValueError("worker_work_item_id is required")
    n = int(attempt)
    if n < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return f"report::{wid}::v{n}"


# Default cap for how many times a worker can be sent back for review rework
# on the same delegated work item. Once reached, the runtime stops cycling the
# worker and marks the item done/approved with audit metadata.
# Overridable per work item via metadata.max_review_reworks.
DEFAULT_MAX_REVIEW_REWORKS = 5

# Pre-delivery rework is a final-delivery safety valve, not an unbounded
# execution loop. Infrastructure or assessor-format failures must not keep
# restarting the same delivery forever.
DEFAULT_MAX_PRE_DELIVERY_REWORKS = 3

# Cap on how many times the reviewer can produce an unparseable verdict
# before the runtime stops retrying the reviewer and closes the review without
# blaming the worker. Distinct from review_rework_count: this counts
# reviewer-side output failures (no extractable approve/reject label), not
# honest-but-rejected work.
MAX_VERDICT_PARSE_RETRIES = 2

_REVIEW_VERDICT_PARSE_RETRY_HINT = (
    "\n\n[REVIEW RETRY — Your previous verdict could not be parsed. The "
    "runtime needs an explicit approve/reject decision to drive the next "
    "step. Please end your turn with EXACTLY ONE JSON object on its own "
    "line in one of these shapes:\n\n"
    "  Approve:\n"
    '    {"review_verdict":"approve","summary":"<why this meets the bar>"}\n\n'
    "  Reject:\n"
    '    {"review_verdict":"reject","summary":"<why>",\n'
    '     "blocking_issues":["<specific change needed>"],\n'
    '     "followups":["<non-blocking improvement>"]}\n\n'
    "Without a parseable label this work item cannot make forward "
    "progress. After this retry the runtime will escalate to a human "
    "reviewer if the verdict is still unparseable.]"
)


EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT = """\
You are the top-level company executive performing the final delivery readiness check.

Return strict JSON:
{
  "deliverable": true,
  "summary": "short explanation",
  "rework_targets": [
    {
      "target_projection_id": "exact work item projection id",
      "work_item_projection_id": "same exact work item projection id",
      "role_id": "assigned role id",
      "feedback": "specific rework instructions for that work item"
    }
  ]
}

Rules:
- The user should only receive owner-facing delivery when the runtime is genuinely ready.
- If the delivery package contains unresolved open issues, failed/blocked work items, rejected reviews, or other blockers that make the work not ready, set `deliverable=false`.
- Use the provided role/work-item assignment map so the executive clearly knows who owns each part.
- Target the exact work item that should continue working inside its existing session history.
- Use only projection ids that appear in the provided work_item_tasks data.
- `summary` and each `feedback` must be concise and actionable.
- Return JSON only.
"""

# Backwards-compatible alias for existing tests, metadata, and checkpoints.
# The runtime concept is the top-level executive / final decider, not literally
# a CEO role in every organization.
CEO_PRE_DELIVERY_ASSESSMENT_PROMPT = EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT

_MAX_GATE_REVIEW_FEEDBACK_CHARS = 6000
_DEFAULT_CONTRACT_REWORK_MAX_RETRIES = 2
_WORKSPACE_BOOTSTRAP_PROJECTION_ID = "workspace_bootstrap"
_DATA_ACQUISITION_PROJECTION_ID = "data_acquisition"
_DEFAULT_WORKSPACE_LAYOUT = ("inputs", "deliverables", "work", ".openopc/manifests")
_DEFAULT_DATA_ACQUISITION_REPORT_PATH = "deliverables/data_acquisition_report.json"
_DEFAULT_DATA_ACQUISITION_LOG_PATH = "deliverables/data_acquisition_log.json"
_REVIEW_WAITING_STATUSES = {
    TaskStatus.AWAITING_MANAGER_REVIEW,
    TaskStatus.AWAITING_HUMAN,
    TaskStatus.AWAITING_REVIEW,
}

_COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS = (
    "dispatch_hold",
    "company_runtime_stop_state",
    "company_runtime_stop_intent_id",
    "company_runtime_stop_marked_at",
    "company_runtime_suspend_checkpoint_type",
    "company_runtime_suspended_at",
)
_STALE_REWORK_TASK_METADATA_KEYS = (
    "rework_feedback",
    "review_feedback_version",
    "review_rework_count",
    "review_retry_hint",
    "review_retry_of_attempt",
    "review_retry_reason",
)
_WAITING_TASK_STATUSES = {
    *_REVIEW_WAITING_STATUSES,
    TaskStatus.AWAITING_PEER,
}
_DEFAULT_DATA_ACQUISITION_EXECUTION_RECORD_PATH = DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH
_CANONICAL_COORDINATION_SIGNALS = (
    "scope_locked",
    "inputs_ready",
    "env_ready",
    "implementation_ready",
    "qa_ready",
    "delivery_ready",
)


def _fallback_comms_root(target_output_dir: str | None) -> str | None:
    """Heuristic comms-root used when the engine did not pass one in.

    The comms tree is OPC-internal collaboration state, NOT a project
    deliverable, so it must NOT live inside ``target_output_dir`` (which
    is the project's actual output folder, e.g. ``yitian2003``). The
    parent directory is almost always the user's general workspace
    root and is the right place to host ``.opc-comms/<project>/...``.
    """
    if not target_output_dir:
        return None
    try:
        parent = str(Path(target_output_dir).expanduser().resolve().parent)
        if parent and parent not in {"/", "."}:
            return parent
    except Exception:
        pass
    return target_output_dir


def serialize_company_work_item_runtime_plan(plan: CompanyWorkItemRuntimePlan | None) -> dict[str, Any]:
    return serialize_company_work_item_plan(_coerce_company_work_item_runtime_plan(plan))


def deserialize_company_work_item_runtime_plan(data: dict[str, Any] | None) -> CompanyWorkItemRuntimePlan:
    return deserialize_company_work_item_plan(data)


_SERIALIZED_PLAN_MARKER_KEYS = ("projections", "seeds", "root_projection_id", "runtime_model")


def is_serialized_company_work_item_runtime_plan(data: Any) -> bool:
    """Whether ``data`` is a run-level serialized CompanyWorkItemRuntimePlan.

    Work-item tasks overload plan-adjacent metadata keys: ``work_item_runtime_plan``
    holds a per-item assignment spec (``projection_id``/``turn_type``/``summary``/...),
    not the run-level plan. Feeding that shape to ``from_dict`` silently yields an
    empty plan, which downstream misreads as a legacy (non multi-team-org) run and
    refuses to resume the session.
    """
    if not isinstance(data, dict) or not data:
        return False
    if "projection_id" in data:
        return False
    return any(key in data for key in _SERIALIZED_PLAN_MARKER_KEYS)


def serialized_company_plan_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the first metadata value that is a real serialized run-level plan."""
    source = dict(metadata or {})
    for key in ("company_work_item_plan", "work_item_runtime_plan"):
        candidate = source.get(key)
        if is_serialized_company_work_item_runtime_plan(candidate):
            return candidate
    return None


def _coerce_company_work_item_runtime_plan(plan: Any) -> CompanyWorkItemRuntimePlan | None:
    """Accept projection-plan-like test doubles without consuming obsolete plan fields."""
    if plan is None or isinstance(plan, CompanyWorkItemRuntimePlan):
        return plan
    projections: list[WorkItemProjectionSpec] = []
    for raw_projection in list(getattr(plan, "projections", []) or []):
        projection_id = str(
            getattr(raw_projection, "projection_id", "")
            or ""
        ).strip()
        if not projection_id:
            continue
        raw_gate = getattr(raw_projection, "gate_policy", None)
        gate_policy = None
        if raw_gate is not None:
            raw_gate_type = getattr(raw_gate, "gate_type", "review")
            gate_policy = WorkItemGatePolicy(
                gate_type=str(getattr(raw_gate_type, "value", raw_gate_type) or "review"),
                instructions=str(getattr(raw_gate, "instructions", "") or ""),
                reviewer_role=getattr(raw_gate, "reviewer_role", None),
                requires_human=bool(getattr(raw_gate, "requires_human", False)),
                on_reject=str(getattr(raw_gate, "on_reject", "") or "halt"),
                rework_projection_id=str(
                    getattr(raw_gate, "rework_projection_id", "")
                    or ""
                ).strip() or None,
                max_retries=int(getattr(raw_gate, "max_retries", 1) or 1),
                metadata=dict(getattr(raw_gate, "metadata", {}) or {}),
            )
        raw_strategy = getattr(raw_projection, "execution_strategy", "auto")
        projections.append(
            WorkItemProjectionSpec(
                projection_id=projection_id,
                turn_type=str(
                    getattr(raw_projection, "turn_type", "")
                    or "execute"
                ).strip().lower() or "execute",
                role_id=str(getattr(raw_projection, "role_id", "") or "").strip(),
                title=str(getattr(raw_projection, "title", "") or projection_id).strip(),
                summary=str(getattr(raw_projection, "summary", "") or "").strip(),
                dependency_projection_ids=[
                    str(item).strip()
                    for item in list(getattr(raw_projection, "dependency_projection_ids", []) or [])
                    if str(item).strip()
                ],
                execution_strategy=str(getattr(raw_strategy, "value", raw_strategy) or "auto"),
                preferred_external_agent=getattr(raw_projection, "preferred_external_agent", None),
                parallel_group=getattr(raw_projection, "parallel_group", None),
                gate_policy=gate_policy,
                metadata=dict(getattr(raw_projection, "metadata", {}) or {}),
            )
        )
    return CompanyWorkItemRuntimePlan(
        profile=str(getattr(plan, "profile", "") or "corporate").strip() or "corporate",
        root_projection_id=str(getattr(plan, "root_projection_id", "") or "").strip(),
        projections=projections,
        metadata=dict(getattr(plan, "metadata", {}) or {}),
    )


@dataclass
class CompanyRuntimeSpec:
    """Lightweight pre-runtime spec for company-mode recruitment/bootstrap."""

    profile: str = "corporate"
    original_request: str = ""
    runtime_model: str = "multi_team_org"
    work_item_driven: bool = True
    staffing_overrides: dict[str, str] = field(default_factory=dict)
    role_agent_overrides: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkItemOutputBundle:
    """Separated runtime audit and WorkItem-owned output metadata."""

    work_item_updates: dict[str, Any] = field(default_factory=dict)
    runtime_audit_updates: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True)
class CompanyExecutorDriverOwnership:
    """One registry attempt covering a complete company scheduler run."""

    registry: ActiveTaskRunRegistry
    project_id: str
    task_id: str
    attempt_token: str

    def bind(self):
        return self.registry.bind_driver_attempt(self.attempt_token)

    def release(self) -> bool:
        return self.registry.unregister(
            self.project_id,
            self.task_id,
            self.attempt_token,
        )


def serialize_company_runtime_spec(spec: CompanyRuntimeSpec | None) -> dict[str, Any]:
    if spec is None:
        return {}
    return {
        "profile": spec.profile,
        "original_request": spec.original_request,
        "runtime_model": spec.runtime_model,
        "work_item_driven": bool(spec.work_item_driven),
        "staffing_overrides": dict(spec.staffing_overrides or {}),
        "role_agent_overrides": dict(spec.role_agent_overrides or {}),
        "metadata": dict(spec.metadata or {}),
    }


def deserialize_company_runtime_spec(data: dict[str, Any] | None) -> CompanyRuntimeSpec:
    payload = dict(data or {})
    metadata = dict(payload.get("metadata", {}) or {})
    original_request = str(
        payload.get("original_request")
        or metadata.get("original_request")
        or ""
    )
    runtime_model = str(payload.get("runtime_model") or metadata.get("runtime_model") or "multi_team_org")
    work_item_driven = bool(payload.get("work_item_driven", metadata.get("work_item_driven", True)))
    metadata.setdefault("original_request", original_request)
    metadata.setdefault("runtime_model", runtime_model)
    metadata.setdefault("work_item_driven", work_item_driven)
    return CompanyRuntimeSpec(
        profile=str(payload.get("profile") or metadata.get("company_profile") or "corporate"),
        original_request=original_request,
        runtime_model=runtime_model,
        work_item_driven=work_item_driven,
        staffing_overrides={
            str(role_id).strip(): str(employee_id).strip()
            for role_id, employee_id in dict(payload.get("staffing_overrides", {}) or {}).items()
            if str(role_id).strip() and str(employee_id).strip()
        },
        role_agent_overrides={
            str(role_id).strip(): str(agent_name).strip()
            for role_id, agent_name in dict(payload.get("role_agent_overrides", {}) or {}).items()
            if str(role_id).strip() and str(agent_name).strip()
        },
        metadata=metadata,
    )


class CompanyRuntimeWorkItemHelper:
    """Builds runtime coordination defaults for company work items."""

    def __init__(self, org_engine: OrgEngine, llm: Any | None = None) -> None:
        self.org_engine = org_engine
        self.llm = llm

    @staticmethod
    def _dedupe_lines(items: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for raw in items:
            item = str(raw or "").strip()
            if not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _coerce_projection_assignment(
        self,
        data: dict[str, Any] | None,
        *,
        projection: WorkItemProjectionSpec,
        global_intent_summary: str,
    ) -> dict[str, Any]:
        fallback = self._default_projection_assignment(projection, global_intent_summary=global_intent_summary)
        data = dict(data or {})
        your_responsibility = str(
            data.get("your_responsibility") or fallback["your_responsibility"]
        ).strip() or fallback["your_responsibility"]
        out_of_scope = self._normalize_assignment_lines(
            data.get("out_of_scope"),
            fallback["out_of_scope"],
        )
        inputs = self._normalize_assignment_lines(
            data.get("inputs"),
            fallback["inputs"],
        )
        deliverables = self._normalize_assignment_lines(
            data.get("deliverables"),
            fallback["deliverables"],
        )
        acceptance = self._normalize_assignment_lines(
            data.get("acceptance_criteria"),
            fallback["acceptance_criteria"],
        )
        return {
            "projection_id": projection.projection_id,
            **work_item_identity_payload(projection_id=projection.projection_id, turn_type=""),
            "global_intent_summary": global_intent_summary,
            "your_responsibility": your_responsibility,
            "out_of_scope": out_of_scope,
            "inputs": inputs,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
        }

    def _default_projection_assignment(
        self,
        projection: WorkItemProjectionSpec,
        *,
        global_intent_summary: str,
    ) -> dict[str, Any]:
        dependency_labels = [
            dep.replace("_", " ").strip().title()
            for dep in projection.dependency_projection_ids
        ]
        inputs = ["Use the global intent summary as the mission baseline."]
        if dependency_labels:
            inputs.extend(
                f"Rely on the completed handoff/results from `{label}`."
                for label in dependency_labels
            )
        else:
            inputs.append("You may proceed without waiting for upstream work-item handoffs.")
        deliverables = self._infer_work_item_deliverables(projection)
        acceptance = list(projection.metadata.get("acceptance_criteria", [])) or self._infer_work_item_acceptance(projection, deliverables)
        out_of_scope = [
            "Do not redo work that belongs to upstream completed work items.",
            "Do not take ownership of deliverables assigned to other work items.",
        ]
        if projection.dependency_projection_ids:
            out_of_scope.append("Do not overwrite dependency conclusions unless a gate or handoff explicitly requests rework.")
        return {
            "projection_id": projection.projection_id,
            **work_item_identity_payload(projection_id=projection.projection_id, turn_type=""),
            "global_intent_summary": global_intent_summary,
            "your_responsibility": (
                f"Own the `{projection.title}` work item for role `{projection.role_id}`. "
                f"{projection.summary.strip() or 'Complete the work defined for this projected work item.'}"
            ),
            "out_of_scope": out_of_scope,
            "inputs": inputs,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
        }

    def _infer_work_item_deliverables(self, projection_spec: WorkItemProjectionSpec) -> list[str]:
        lowered = f"{projection_spec.projection_id} {projection_spec.title} {projection_spec.summary}".lower()
        if _WORKSPACE_BOOTSTRAP_PROJECTION_ID in lowered or "workspace bootstrap" in lowered:
            return ["A structured workspace_manifest plus the prepared shared workspace layout."]
        if _DATA_ACQUISITION_PROJECTION_ID in lowered or "data acquisition" in lowered:
            return [
                "A data acquisition execution record showing what sources were attempted and what assets were prepared.",
                "A structured data_acquisition_report describing final self-audited input readiness for downstream execution.",
            ]
        if any(token in lowered for token in ("intake", "triage", "framing the company mission")):
            return [
                f"Write the project intake brief to `deliverables/{projection_spec.projection_id}.md` with these sections: "
                f"## Mission Summary, ## Scope, ## Out of Scope, ## Initial Risks & Unknowns, "
                f"## C-suite Routing (which executives own which slice), ## Acceptance Criteria for Final Delivery.",
                "Surface the same brief in your final task result so downstream planning work items and reviewers can read it directly.",
            ]
        if any(token in lowered for token in ("plan", "planning", "approach", "architecture")):
            return [
                f"Write the planning brief to `deliverables/{projection_spec.projection_id}.md` with these sections: "
                f"## Goals (what success looks like for this work item's slice), ## Key Decisions, "
                f"## Approach / Sequencing, ## Assumptions, ## Risks & Mitigations, "
                f"## Downstream Handoff Notes (what executors need from this plan).",
                "Surface the same plan in your final task result so downstream work items and reviewers can read it directly.",
            ]
        if any(token in lowered for token in ("review", "audit", "qa", "test", "validation")):
            return ["A review outcome with findings, validation notes, and a clear pass/fail recommendation."]
        if any(token in lowered for token in ("delivery", "aggregate", "final")):
            return ["A concise final delivery that aggregates relevant upstream results for the user."]
        if any(token in lowered for token in ("content", "documentation", "presentation", "design")):
            return ["The work-item-specific content or artifact requested by the runtime objective."]
        return ["A concrete output that fulfills this work-item objective and can be handed off downstream."]

    def _infer_work_item_acceptance(self, projection_spec: WorkItemProjectionSpec, deliverables: list[str]) -> list[str]:
        lowered = f"{projection_spec.projection_id} {projection_spec.title} {projection_spec.summary}".lower()
        if _WORKSPACE_BOOTSTRAP_PROJECTION_ID in lowered or "workspace bootstrap" in lowered:
            return [
                "The target workspace root exists and is writable for downstream work items.",
                "The reserved directories exist: inputs/, deliverables/, work/, and .openopc/manifests/.",
                "A workspace_manifest is recorded with the root path and reserved directory mapping.",
            ]
        if _DATA_ACQUISITION_PROJECTION_ID in lowered or "data acquisition" in lowered:
            return [
                "The work item attempts real acquisition or preparation of missing critical inputs before concluding they are blocked.",
                "A data_acquisition_report is recorded after the work item self-audits the prepared inputs.",
                "The report status is one of ready, already_present, not_required, partial, or missing_critical.",
                "Blocking statuses require explicit acquisition attempt evidence, prepared assets, or documented blockers.",
                "Required inputs, present inputs, missing inputs, attempted sources, prepared assets, and provenance summary are all explicit.",
            ]
        if any(token in lowered for token in (
            "intake", "triage", "framing the company mission", "plan", "planning", "approach", "architecture",
        )):
            return [
                f"The planning brief file `deliverables/{projection_spec.projection_id}.md` exists in the shared workspace and contains every required section.",
                "The brief stays within this work item's role boundary (this work item plans its own slice; it does not redo upstream work or absorb other work items' deliverables).",
                "Downstream work items can act on the brief without re-reading the original user request.",
            ]
        criteria = [
            "The output stays within this work item's responsibility boundary.",
            "The output is usable by downstream work items without re-reading the full user request.",
        ]
        if projection_spec.dependency_projection_ids:
            criteria.append("Upstream handoffs or dependency results are incorporated rather than duplicated.")
        return criteria

    def _normalize_assignment_lines(self, value: Any, fallback: list[str]) -> list[str]:
        if isinstance(value, str):
            lines = [line.strip(" -") for line in value.splitlines() if line.strip()]
        elif isinstance(value, list):
            lines = [str(item).strip() for item in value if str(item).strip()]
        else:
            lines = []
        return lines or list(fallback)

    def _fallback_global_intent_summary(self, original_message: str) -> str:
        compact = " ".join(str(original_message or "").split())
        if not compact:
            return "Complete the requested runtime while keeping each work item tightly scoped."
        # No character truncation: this fallback is the user's original
        # mission and downstream work items depend on it for routing.
        # Truncating it (the previous behavior chopped at 217 chars and
        # appended "...") silently destroyed the mission baseline for
        # any prompt that hit the fallback path.
        return compact

    def _build_work_item_description(
        self,
        assignment: dict[str, Any],
    ) -> str:
        parts = [
            f"## Global Intent Summary\n{assignment['global_intent_summary']}",
            f"## Your Responsibility\n{assignment['your_responsibility']}",
            "## Out of Scope\n" + "\n".join(f"- {item}" for item in assignment["out_of_scope"]),
            "## Inputs\n" + "\n".join(f"- {item}" for item in assignment["inputs"]),
            "## Deliverables\n" + "\n".join(f"- {item}" for item in assignment["deliverables"]),
            "## Acceptance Criteria\n" + "\n".join(f"- {item}" for item in assignment["acceptance_criteria"]),
        ]
        return "\n\n".join(parts)

    def _infer_work_item_turn_type(self, projection: WorkItemProjectionSpec) -> str:
        delegation_kind = str((projection.metadata or {}).get("delegation_turn_kind", "") or "").strip().lower()
        if delegation_kind == "intake":
            return "intake"
        if delegation_kind == "delegate":
            return "dispatch"
        if delegation_kind == "synthesize":
            return "aggregate"
        if delegation_kind == "deliver":
            return "deliver"
        if delegation_kind == "execute":
            return "execute"
        projection_id = str(projection.projection_id or "").strip().lower()
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            return "setup"
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            return "execute"
        lowered = " ".join(
            part for part in (
                str(projection.projection_id or "").strip().lower(),
                str(projection.title or "").strip().lower(),
                str(projection.summary or "").strip().lower(),
                str(projection.role_id or "").strip().lower(),
            )
            if part
        )
        if any(token in lowered for token in (
            "setup", "provision", "environment", "env_setup", "env_provision",
            "install dependencies", "install tools", "configure environment",
            "toolchain", "runtime setup", "dependency install",
            "workspace bootstrap", "workspace scaffold", "workspace setup", "bootstrap workspace",
        )):
            return "setup"
        if any(token in lowered for token in ("intake", "triage", "classify", "frame the company mission")):
            return "intake"
        if any(token in lowered for token in ("review", "approval", "approve", "audit", "qa", "validation", "acceptance")):
            return "review"
        if any(token in lowered for token in ("dispatch", "coordination", "coordinate", "routing", "assignment")):
            return "dispatch"
        if any(token in lowered for token in ("plan", "planning", "architecture", "approach", "execution plan")):
            return "plan"
        if any(token in lowered for token in ("execute", "execution", "implement", "implementation", "develop", "backend api", "code artifacts", "code implementation")):
            return "execute"
        if any(token in lowered for token in ("aggregate", "aggregation", "synthesize", "synthesis")):
            return "aggregate"
        if any(token in lowered for token in ("delivery", "deliver", "final return", "final delivery", "return the outcome")):
            return "deliver"
        return "execute"

    def _infer_work_item_orchestration_profile(
        self,
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> str:
        strategy = (
            projection_spec.execution_strategy.value
            if hasattr(projection_spec.execution_strategy, "value")
            else str(projection_spec.execution_strategy or "auto")
        )
        if work_item_turn_type == "setup":
            return "company_setup_provision"
        if work_item_turn_type == "review":
            return "company_review_fresh_eyes"
        if work_item_turn_type in {"plan", "intake"}:
            return "company_plan_read_heavy"
        if work_item_turn_type in {"dispatch", "aggregate", "deliver"}:
            return f"company_{work_item_turn_type}_coordinator"
        if strategy == WorkItemExecutionStrategy.AUTO.value:
            return "company_execute_native_first"
        if strategy == WorkItemExecutionStrategy.NATIVE.value:
            return "company_execute_native"
        if strategy == WorkItemExecutionStrategy.EXTERNAL.value:
            return "company_execute_external"
        if strategy == WorkItemExecutionStrategy.MIXED.value:
            return "company_execute_mixed"
        return "company_execute_native_first"

    def _work_item_verification_required(
        self,
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> bool:
        explicit = projection_spec.metadata.get("verification_required")
        if isinstance(explicit, bool):
            return explicit
        if work_item_turn_type not in {"execute"}:
            return False
        lowered = f"{projection_spec.title} {projection_spec.summary}".lower()
        if any(token in lowered for token in ("documentation", "presentation", "content strategy")):
            return False
        return True

    def _build_work_item_runtime_plan(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        runtime_policy: dict[str, Any],
    ) -> dict[str, Any]:
        communication_policy = dict(runtime_policy.get("communication", {}))
        collaboration: list[str] = []
        if projection_spec.dependency_projection_ids:
            collaboration.append("Start from dependency handoffs instead of re-solving upstream work.")
        if work_item_turn_type == "execute":
            collaboration.append("Consume only designated workspace and input paths from upstream readiness artifacts.")
            collaboration.append("Do not fabricate critical missing inputs or downgrade missing inputs into a done state.")
            if str(projection_spec.projection_id).strip() == _DATA_ACQUISITION_PROJECTION_ID:
                collaboration.append(
                    "Use web_search/web_fetch or browser tools first to discover and verify candidate external sources before writing custom network scripts."
                )
                collaboration.append(
                    "Use shell_exec only after concrete source URLs are identified and you need deterministic preparation or downloads inside the workspace."
                )
                collaboration.append("Attempt real acquisition and preparation before declaring a blocking input status.")
                collaboration.append("Record attempted sources, prepared assets, and blockers in the final data acquisition artifacts.")
        data_acquisition_extensions: dict[str, Any] = {}
        if str(projection_spec.projection_id).strip() == _DATA_ACQUISITION_PROJECTION_ID:
            data_acquisition_extensions = {
                "execution_sequence": [
                    "Discover: use web_search/web_fetch or browser tools to identify candidate sources.",
                    "Verify: keep only sources you can justify as official or acceptable for the task.",
                    "Prepare inputs: use standard CLI tools through shell_exec to download or normalize inputs inside the workspace.",
                    "Report: publish source_candidates, download_manifest, and the final readiness report.",
                ],
                "media_mode_triggers": [
                    "Enable media mode when the request or required inputs mention video, trailer, footage, clip, 素材, 片段, audio, music, subtitle, srt, bilibili, youtube, mp4, or wav.",
                ],
                "media_mode_rules": [
                    "Search-result pages, HTML snapshots, and URL lists never count as acquired binary assets.",
                    "Binary media must be prepared inside the workspace or the status must remain partial/missing_critical.",
                    "Prefer standard CLI tools such as yt-dlp, curl, wget, aria2c, and ffmpeg.",
                    "Do not use inline Python or ad hoc urllib scripts as the primary acquisition path.",
                    "Parse raw HTML into work/source_candidates.json before inspecting it further.",
                ],
                "download_priority": [
                    "Discover/verify: web_search, web_fetch, browser_*",
                    "Download and prepare: yt-dlp, curl, wget, aria2c",
                    "Normalize/probe: ffmpeg",
                ],
            }
        default_mode = str(communication_policy.get("default_mode", "") or "").strip()
        if default_mode == "dm":
            collaboration.append("Use direct messages for targeted clarifications.")
        elif default_mode == "broadcast":
            collaboration.append("Broadcast only when an issue affects multiple downstream roles.")
        if communication_policy.get("meeting_required_for"):
            collaboration.append("Escalate to a meeting only for true cross-role decisions or conflicts.")
        if work_item_turn_type in {"execute", "review"}:
            collaboration.append(
                "You may run a work-item swarm: the durable owner stays accountable while elastic worker slots claim shared microtasks."
            )
        if not collaboration:
            collaboration.append("Prefer asynchronous handoffs and annotations over blocking coordination.")
        return {
            "projection_id": projection_spec.projection_id,
            **work_item_identity_payload(projection_id=projection_spec.projection_id, turn_type=work_item_turn_type),
            "turn_type": work_item_turn_type,
            "summary": assignment["your_responsibility"],
            "inputs": list(assignment["inputs"]),
            "deliverables": list(assignment["deliverables"]),
            "acceptance_criteria": list(assignment["acceptance_criteria"]),
            "out_of_scope": list(assignment["out_of_scope"]),
            "collaboration_expectations": collaboration,
            "verification_required": self._work_item_verification_required(
                projection_spec,
                work_item_turn_type=work_item_turn_type,
            ),
            **data_acquisition_extensions,
        }

    def _downstream_consumers(
        self,
        plan: CompanyWorkItemRuntimePlan,
        projection_spec: WorkItemProjectionSpec,
    ) -> list[str]:
        consumers: list[str] = []
        for candidate in plan.projections:
            if projection_spec.projection_id not in list(candidate.dependency_projection_ids):
                continue
            role_id = str(candidate.role_id or "").strip()
            if role_id and role_id not in consumers:
                consumers.append(role_id)
        return consumers

    @staticmethod
    def _work_item_requires_writable_scope(
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> bool:
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            return True
        return work_item_turn_type in {"execute", "setup"}

    def _build_ownership_contract(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        target_output_dir: str | None,
        downstream_consumers: list[str],
    ) -> ArtifactContract:
        write_scope = "read_only"
        if self._work_item_requires_writable_scope(projection_spec, work_item_turn_type=work_item_turn_type):
            write_scope = str(target_output_dir or "assigned_workspace").strip() or "assigned_workspace"
        expected_artifacts = [
            str(item).strip()
            for item in list(assignment.get("deliverables", []) or [])
            if str(item).strip()
        ]
        allowed_targets: list[str] = []
        get_allowed_contact_roles = getattr(self.org_engine, "get_allowed_contact_roles", None)
        if callable(get_allowed_contact_roles):
            allowed_targets = list(get_allowed_contact_roles(projection_spec.role_id))
        return ArtifactContract(
            summary=str(assignment.get("your_responsibility", "") or projection_spec.summary or "").strip(),
            write_scope=write_scope,
            expected_artifacts=expected_artifacts,
            downstream_consumer=list(downstream_consumers),
            allowed_collaboration_targets=allowed_targets,
        )

    @staticmethod
    def _coordination_policy(runtime_policy: dict[str, Any]) -> dict[str, Any]:
        return dict(runtime_policy.get("coordination", {}) or {})

    @staticmethod
    def _role_text(agent: Any | None, employee_assignment: dict[str, Any] | None = None) -> str:
        def _collect_values(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                text = value.strip()
                return [text] if text else []
            if isinstance(value, (int, float, bool)):
                return [str(value)]
            if isinstance(value, dict):
                values: list[str] = []
                for nested in value.values():
                    values.extend(_collect_values(nested))
                return values
            if isinstance(value, (list, tuple, set)):
                values: list[str] = []
                for nested in value:
                    values.extend(_collect_values(nested))
                return values
            text = str(value).strip()
            return [text] if text else []

        parts = [
            str(getattr(agent, "name", "") or "").strip(),
            str(getattr(agent, "responsibility", "") or "").strip(),
            " ".join(str(item).strip() for item in list(getattr(agent, "can_spawn", []) or []) if str(item).strip()),
            " ".join(_collect_values(dict(getattr(agent, "runtime_policy", {}) or {}))),
            " ".join(_collect_values(dict(employee_assignment or {}))),
        ]
        return " ".join(part for part in parts if part).lower()

    def _infer_role_profile(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        employee_assignment: dict[str, Any] | None = None,
    ) -> AdaptiveRoleProfile:
        agent = self.org_engine.get_agent(projection_spec.role_id)
        role_text = self._role_text(agent, employee_assignment)
        facets: list[str] = []
        evidence: list[str] = []
        if list(getattr(agent, "can_spawn", []) or []) or str(getattr(agent, "reports_to", "") or "").strip() == "owner":
            facets.append("coordination")
            evidence.append("role_can_spawn_or_reports_to_owner")
        if any(token in role_text for token in ("review", "qa", "quality assurance", "test", "validation", "audit", "compliance", "verification")):
            facets.append("review")
            evidence.append("review_tokens")
        if any(token in role_text for token in ("environment", "toolchain", "setup", "provision", "dependency", "runtime")):
            facets.extend(["setup", "provider"])
            evidence.append("setup_tokens")
        if any(token in role_text for token in ("acquisition", "source", "input", "preparation", "download", "provenance")):
            facets.extend(["acquisition", "provider"])
            evidence.append("acquisition_tokens")
        if any(token in role_text for token in ("engineer", "implement", "implementation", "develop", "code", "technical")):
            facets.append("technical_execution")
            evidence.append("technical_tokens")
        if any(token in role_text for token in ("design", "ux", "content", "copy", "presentation", "documentation")):
            facets.append("creative_execution")
            evidence.append("creative_tokens")
        if str(getattr(agent, "reports_to", "") or "").strip() == "owner":
            facets.append("decision_maker")
            evidence.append("top_level_role")
        if not facets:
            facets.append("generalist")
            evidence.append("fallback_generalist")
        facets = list(dict.fromkeys(facets))
        authority_scope: list[str] = []
        if "decision_maker" in facets:
            authority_scope.extend(["deliver", "approve", "direct"])
        if "coordination" in facets:
            authority_scope.extend(["delegate", "synthesize", "gate"])
        if "review" in facets:
            authority_scope.extend(["review", "verify"])
        if "provider" in facets:
            authority_scope.extend(["prepare"])
        if "technical_execution" in facets or "creative_execution" in facets or "generalist" in facets:
            authority_scope.append("execute")
        label = "generalist"
        if "decision_maker" in facets:
            label = "decision_maker"
        elif "review" in facets and "coordination" in facets:
            label = "review_coordinator"
        elif "coordination" in facets:
            label = "coordinator"
        elif "review" in facets:
            label = "reviewer"
        elif "provider" in facets:
            label = "provider"
        elif "technical_execution" in facets:
            label = "technical_executor"
        elif "creative_execution" in facets:
            label = "creative_executor"
        execution_bias = "balanced"
        if "provider" in facets or "review" in facets:
            execution_bias = "serial_preferred"
        elif "technical_execution" in facets or "creative_execution" in facets:
            execution_bias = "parallel_friendly"
        review_bias = "none"
        if "review" in facets:
            review_bias = "strict"
        elif "coordination" in facets:
            review_bias = "managerial"
        collaboration_style = "async"
        if "coordination" in facets:
            collaboration_style = "manager_driven"
        confidence = 0.55
        if "fallback_generalist" not in evidence:
            confidence = 0.72
        if "decision_maker" in facets:
            confidence = 0.85
        return AdaptiveRoleProfile(
            label=label,
            facets=facets,
            authority_scope=list(dict.fromkeys(authority_scope)),
            execution_bias=execution_bias,
            review_bias=review_bias,
            collaboration_style=collaboration_style,
            confidence=confidence,
            evidence=evidence,
        )

    def _coordination_turn_kind(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        work_item_turn_type: str,
        role_profile: AdaptiveRoleProfile,
    ) -> str:
        explicit = str((projection_spec.metadata or {}).get("turn_kind", "") or "").strip().lower()
        if explicit:
            return explicit
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id.endswith("__prepare"):
            return "prepare"
        if projection_id.endswith("__verify"):
            return "verify"
        if work_item_turn_type == "intake":
            return "plan"
        if work_item_turn_type == "dispatch":
            return "dispatch"
        if work_item_turn_type == "plan":
            return "plan"
        if work_item_turn_type == "setup":
            return "acquire" if "acquisition" in role_profile.facets else "setup"
        if work_item_turn_type == "review":
            return "verify"
        if work_item_turn_type == "aggregate":
            return "synthesize"
        if work_item_turn_type == "deliver":
            return "deliver"
        if "review" in role_profile.facets:
            return "verify"
        if "provider" in role_profile.facets:
            return "setup"
        return "execute"

    def _coordination_signal_owner(
        self,
        *,
        signal_name: str,
        projection_spec: WorkItemProjectionSpec,
    ) -> str:
        if signal_name == "delivery_ready":
            final_decider = getattr(self.org_engine, "get_final_decider_role_id", None)
            if callable(final_decider):
                return str(final_decider(strict=False) or projection_spec.role_id).strip()
            return projection_spec.role_id
        agents = list(getattr(self.org_engine, "list_agents", lambda: [])() or [])
        signal_tokens = {
            "env_ready": ("environment", "setup", "provision", "toolchain", "dependency"),
            "inputs_ready": ("acquisition", "source", "input", "download", "provenance"),
            "implementation_ready": ("technical", "engineer", "implement", "development", "architecture"),
            "qa_ready": ("review", "qa", "quality assurance", "validation", "audit", "compliance"),
        }.get(signal_name, ())
        for agent in agents:
            role_text = self._role_text(agent)
            if any(token in role_text for token in signal_tokens):
                return str(getattr(agent, "role_id", "") or "").strip()
        manager_role_id = str((projection_spec.metadata or {}).get("manager_role_id", "") or "").strip()
        return manager_role_id or projection_spec.role_id

    def _coordination_required_signals(
        self,
        *,
        turn_kind: str,
        role_profile: AdaptiveRoleProfile,
    ) -> list[str]:
        signals: list[str] = []
        if turn_kind in {"plan", "prepare", "setup", "acquire", "execute", "synthesize", "integration"}:
            signals.append("scope_locked")
        if turn_kind == "execute" and "provider" not in role_profile.facets:
            signals.extend(["env_ready", "inputs_ready"])
        if turn_kind == "verify":
            signals.extend(["implementation_ready", "env_ready", "inputs_ready"])
        if turn_kind == "deliver":
            signals.extend(["qa_ready", "delivery_ready"])
        return list(dict.fromkeys(signal for signal in signals if signal))

    @staticmethod
    def _coordination_emitted_signals(
        *,
        turn_kind: str,
        role_profile: AdaptiveRoleProfile,
    ) -> list[str]:
        if turn_kind == "setup":
            return ["env_ready"]
        if turn_kind == "acquire":
            return ["inputs_ready"]
        if turn_kind == "execute" and "provider" not in role_profile.facets and "review" not in role_profile.facets:
            return ["implementation_ready"]
        if turn_kind == "verify":
            return ["qa_ready"]
        if turn_kind == "deliver":
            return ["delivery_ready"]
        return []

    def _build_coordination_spec(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        runtime_policy: dict[str, Any],
        employee_assignment: dict[str, Any] | None = None,
    ) -> CoordinationSpec:
        coordination_policy = self._coordination_policy(runtime_policy)
        role_profile = self._infer_role_profile(projection_spec=projection_spec, employee_assignment=employee_assignment)
        turn_kind = self._coordination_turn_kind(
            projection_spec=projection_spec,
            work_item_turn_type=work_item_turn_type,
            role_profile=role_profile,
        )
        strict_gate_turn_kinds = {
            str(item).strip().lower()
            for item in list(coordination_policy.get("strict_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        mixed_gate_turn_kinds = {
            str(item).strip().lower()
            for item in list(coordination_policy.get("mixed_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        gate_profile = str((projection_spec.metadata or {}).get("gate_profile", "") or "").strip().lower()
        dependency_class = "hard"
        if turn_kind in mixed_gate_turn_kinds:
            dependency_class = "soft"
        if gate_profile in {"readiness", "contract", "delivery"} or turn_kind in strict_gate_turn_kinds:
            dependency_class = "hard"
        required_artifacts = [
            item for item in list(assignment.get("inputs", []) or [])
            if any(token in str(item) for token in ("/", ".md", ".json", ".yml", ".yaml", ".txt"))
        ]
        writes: list[str] = []
        if work_item_turn_type in {"setup", "execute", "review", "deliver", "aggregate"}:
            writes.append("assigned_workspace")
        reads = [str(item).strip() for item in list(assignment.get("inputs", []) or []) if str(item).strip()]
        signals = [
            AdaptiveSignalSpec(
                name=signal_name,
                owner_role_id=self._coordination_signal_owner(signal_name=signal_name, projection_spec=projection_spec),
                required=True,
                strict=turn_kind in strict_gate_turn_kinds or gate_profile in {"readiness", "contract", "delivery"},
            )
            for signal_name in self._coordination_required_signals(turn_kind=turn_kind, role_profile=role_profile)
        ]
        work_item_profile = AdaptiveWorkItemProfile(
            turn_kind=turn_kind,
            dependency_class=dependency_class,
            blocked_by_projection_ids=list(dict.fromkeys(str(item).strip() for item in list(projection_spec.dependency_projection_ids) if str(item).strip())),
            blocked_by_signals=[signal.name for signal in signals],
            required_artifacts=list(dict.fromkeys(required_artifacts)),
            reads=list(dict.fromkeys(reads)),
            writes=list(dict.fromkeys(writes)),
            gate_owner_role_id=self._coordination_signal_owner(signal_name="qa_ready" if turn_kind == "verify" else "delivery_ready" if turn_kind == "deliver" else "scope_locked", projection_spec=projection_spec),
            soft_release_allowed=turn_kind in mixed_gate_turn_kinds and bool(coordination_policy.get("allow_manager_release_for_mixed_only", True)),
            confidence=0.8 if str((projection_spec.metadata or {}).get("turn_kind", "") or "").strip() else 0.68,
        )
        confidence = min(role_profile.confidence, work_item_profile.confidence) if role_profile.confidence and work_item_profile.confidence else max(role_profile.confidence, work_item_profile.confidence)
        return CoordinationSpec(
            version=1,
            inference_mode=str(coordination_policy.get("inference_mode", "llm_primary") or "llm_primary"),
            fallback_mode=str(coordination_policy.get("fallback_mode", "conservative") or "conservative"),
            role_profile=role_profile,
            work_item_profile=work_item_profile,
            signals=signals,
            emitted_signals=self._coordination_emitted_signals(turn_kind=turn_kind, role_profile=role_profile),
            normalized_state="planned",
            notes=[],
            confidence=confidence,
            evidence=[
                f"work_item_turn_type:{work_item_turn_type}",
                f"dependency_count:{len(projection_spec.dependency_projection_ids)}",
                *list(role_profile.evidence),
            ],
        )

    @staticmethod
    def _coordination_spec_dict(spec: CoordinationSpec) -> dict[str, Any]:
        return asdict(spec)

    def _lint_work_item_assignment(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        responsibility = str(assignment.get("your_responsibility", "") or "").strip().lower()
        if not responsibility:
            issues.append("Missing responsibility summary for the work-item assignment.")
        out_of_scope = {
            str(item).strip().lower()
            for item in list(assignment.get("out_of_scope", []) or [])
            if str(item).strip()
        }
        deliverables = [
            str(item).strip()
            for item in list(assignment.get("deliverables", []) or [])
            if str(item).strip()
        ]
        acceptance_criteria = [
            str(item).strip()
            for item in list(assignment.get("acceptance_criteria", []) or [])
            if str(item).strip()
        ]
        inputs = [
            str(item).strip()
            for item in list(assignment.get("inputs", []) or [])
            if str(item).strip()
        ]
        if not deliverables:
            issues.append("Missing concrete deliverables for the work-item assignment.")
        if not acceptance_criteria:
            issues.append("Missing acceptance criteria for the work-item assignment.")
        if projection_spec.dependency_projection_ids and not inputs:
            issues.append("Work item depends on upstream work but does not list dependency inputs.")
        for deliverable in deliverables:
            if deliverable.lower() in out_of_scope:
                issues.append(f"Deliverable `{deliverable}` conflicts with out_of_scope.")
        if projection_spec.dependency_projection_ids and not any("handoff" in item.lower() or "dependency" in item.lower() for item in inputs):
            issues.append("Dependency inputs do not explicitly mention upstream handoffs or dependency results.")
        owner = str(projection_spec.role_id or "").strip()
        if not owner:
            issues.append("Work-item assignment is missing a role owner.")
        return issues


class CompanyRuntimeSpecBuilder(CompanyRuntimeWorkItemHelper):
    """Builds the lightweight company runtime spec used before recruitment."""

    def build_spec(self, decision: RouterDecision, *, original_message: str = "") -> CompanyRuntimeSpec:
        profile = str(
            getattr(decision, "company_profile", None)
            or self.org_engine.get_company_profile()
            or "corporate"
        ).strip() or "corporate"
        org_config = getattr(self.org_engine.config, "org", None)
        metadata: dict[str, Any] = {
            "source": "work_item_runtime",
            "execution_mode": "company_mode",
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "work_item_driven": True,
            "company_profile": profile,
            "organization_id": str(getattr(org_config, "organization_id", "") or "").strip(),
            "organization_name": str(getattr(org_config, "organization_name", "") or "").strip(),
            "organization_config_file": str(getattr(org_config, "organization_config_file", "") or "").strip(),
            "original_request": original_message,
            "request_label": "company_runtime",
            "domains": list(getattr(decision, "domains", []) or []),
            "preferred_agent": getattr(decision, "preferred_agent", None),
            "requested_sub_tasks": list(getattr(decision, "sub_tasks", []) or []),
            "org_id": getattr(decision, "org_id", None),
        }
        return CompanyRuntimeSpec(
            profile=profile,
            original_request=original_message,
            runtime_model="multi_team_org",
            work_item_driven=True,
            metadata=metadata,
        )


@dataclass
class CompanyExecutorRunState:
    """Mutable executor state for one top-level company run."""

    active_plan: CompanyWorkItemRuntimePlan | None = None
    active_tasks: list[Task] = field(default_factory=list)
    dispatcher_wake: asyncio.Event = field(default_factory=asyncio.Event)
    kanban_dirty: bool = False
    kanban_broadcast_task: asyncio.Task[None] | None = None
    runtime_invariant_issue_keys: set[tuple[str, str, str, str]] = field(default_factory=set)


class CompanyWorkItemExecutor:
    """Dispatches company work-item runtime turns through projected tasks.

    This is the formal executor name for company mode. Projected task
    records use work-item projection identity for UI, resume, and
    checkpoint payloads.
    """

    def __init__(
        self,
        org_engine: OrgEngine,
        communication: Any,
        approval_engine: Any,
        memory: Any | None,
        execute_task: Callable[[Task], Awaitable[TaskResult]],
        save_task: Callable[[Task], Awaitable[None]],
        seat_executor: SeatExecutor | None = None,
        save_runtime_session: Callable[..., Awaitable[None]] | None = None,
        progress_callback: Callable[..., Awaitable[None]] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        agent_selector: Callable[[Task, Any | None], Awaitable[str | None]] | None = None,
        emit_runtime_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_kanban_changed: Callable[[], Awaitable[None]] | None = None,
        work_item_timeout: int = 600,
        store: Any | None = None,
        llm: Any | None = None,
        role_prompt_runner: Callable[[Task, str, dict[str, Any], str, bool], Awaitable[str | None]] | None = None,
        active_task_run_registry: ActiveTaskRunRegistry | None = None,
    ) -> None:
        self.org_engine = org_engine
        self.communication = communication
        self.approval_engine = approval_engine
        self.memory = memory
        self.llm = llm
        self.work_item_helper = CompanyRuntimeWorkItemHelper(org_engine, llm=llm)
        self.store = store
        self.execute_task = execute_task
        self.seat_executor = seat_executor
        self.save_task = save_task
        self.save_runtime_session = save_runtime_session
        self.progress_callback = progress_callback
        self.checkpoint_callback = checkpoint_callback
        self.agent_selector = agent_selector
        self.emit_runtime_event = emit_runtime_event
        self.on_kanban_changed = on_kanban_changed
        self.work_item_timeout = work_item_timeout
        self.role_prompt_runner = role_prompt_runner
        self.active_task_run_registry = active_task_run_registry
        self._default_run_state = CompanyExecutorRunState()
        self._run_state_var: ContextVar[CompanyExecutorRunState | None] = ContextVar(
            f"company-executor-run-state:{id(self)}",
            default=None,
        )

        self._active_plan = None
        self._active_tasks = []
        # Kanban-push: runtime state transitions route through this hook
        # so the UI sees fresh snapshots mid-turn. Routed through
        # _notify_kanban_changed so the hook is still best-effort.
        if communication is not None and getattr(communication, "on_kanban_changed", None) is None:
            communication.on_kanban_changed = self._notify_kanban_changed
        # Dispatcher wake: signaled by `delegate_work` and by the runtime
        # after applying a `rework` verdict — any time a new TODO work
        # item becomes ready. The main loop in _execute_multi_team_org
        # waits on this Event so children are claimed+spawned without
        # waiting for the parent turn's gather batch to drain.
        self._dispatcher_wake = asyncio.Event()
        if communication is not None and getattr(communication, "on_work_items_created", None) is None:
            communication.on_work_items_created = self._signal_dispatcher_wake
        # D2: register the wake callback with the phase-transition hook
        # registry so signal_dispatcher_hook can ping us whenever a phase
        # change opens new dispatchable work — without this, the hook can
        # update task/session state but the dispatcher loop sleeps on its
        # asyncio.Event until the next periodic tick.
        from opc.layer2_organization.phase_hooks import register_dispatcher_wake
        register_dispatcher_wake(self._signal_dispatcher_wake)
        # Phase B: the old runtime reconciler / reenqueue hooks that
        # reached into in-memory runtime state on every phase transition
        # have been removed. The dispatcher's per-tick rehydrate pass
        # (see _execute_multi_team_org) is now the single convergence
        # point: on every iteration it unparks stale member sessions
        # and re-enqueues runnable work items read fresh from the DB.
        # Debounced kanban broadcaster (Fix C): per-batch push was
        # synchronous on the dispatch hot path; now we mark a dirty flag
        # and a single background coroutine coalesces + broadcasts.
        self._kanban_dirty = False
        self._kanban_broadcast_task = None
        # Trailing debounce: the broadcaster always fires once more after the
        # last dirty mark, so the final board state is never lost. Each fire
        # runs a full build_collab_sync (whole-project snapshot), which is
        # expensive on large projects — keep the window generous. UI-only;
        # the state machine never waits on this broadcast.
        self._kanban_debounce_sec: float = 1.5
        self._runtime_invariant_issue_keys = set()
        self.runtime = CompanyRuntime(
            org_engine=org_engine,
            communication=communication,
            store=store,
            save_runtime_session=save_runtime_session,
            emit_runtime_event=emit_runtime_event,
        )

    def _ensure_prompt_contract_on_work_item(
        self,
        work_item: DelegationWorkItem,
        *,
        task_metadata: dict[str, Any] | None = None,
        task_description: str = "",
    ) -> dict[str, Any]:
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        if has_prompt_contract(metadata.get("prompt_contract")):
            return dict(metadata.get("prompt_contract", {}) or {})
        contract = prompt_contract_from_work_item(
            work_item,
            task_metadata=task_metadata,
            task_description=task_description,
        )
        work_item.metadata = {**metadata, "prompt_contract": contract}
        if str(contract.get("source", {}).get("kind", "") or "") == "prompt_contract_blocker":
            work_item.metadata["prompt_contract_blocker"] = True
        return contract

    def _run_state(self) -> CompanyExecutorRunState:
        if not hasattr(self, "_default_run_state"):
            self._default_run_state = CompanyExecutorRunState()
        if not hasattr(self, "_run_state_var"):
            return self._default_run_state
        return self._run_state_var.get() or self._default_run_state

    def _use_run_state(self, state: CompanyExecutorRunState) -> Token[CompanyExecutorRunState | None]:
        return self._run_state_var.set(state)

    def _reset_run_state(self, token: Token[CompanyExecutorRunState | None]) -> None:
        self._run_state_var.reset(token)

    @property
    def _active_plan(self) -> CompanyWorkItemRuntimePlan | None:
        return self._run_state().active_plan

    @_active_plan.setter
    def _active_plan(self, value: CompanyWorkItemRuntimePlan | None) -> None:
        self._run_state().active_plan = value

    @property
    def _active_tasks(self) -> list[Task]:
        return self._run_state().active_tasks

    @_active_tasks.setter
    def _active_tasks(self, value: list[Task]) -> None:
        self._run_state().active_tasks = value

    @property
    def _dispatcher_wake(self) -> asyncio.Event:
        return self._run_state().dispatcher_wake

    @_dispatcher_wake.setter
    def _dispatcher_wake(self, value: asyncio.Event) -> None:
        self._run_state().dispatcher_wake = value

    @property
    def _kanban_dirty(self) -> bool:
        return self._run_state().kanban_dirty

    @_kanban_dirty.setter
    def _kanban_dirty(self, value: bool) -> None:
        self._run_state().kanban_dirty = bool(value)

    @property
    def _kanban_broadcast_task(self) -> asyncio.Task[None] | None:
        return self._run_state().kanban_broadcast_task

    @_kanban_broadcast_task.setter
    def _kanban_broadcast_task(self, value: asyncio.Task[None] | None) -> None:
        self._run_state().kanban_broadcast_task = value

    @property
    def _runtime_invariant_issue_keys(self) -> set[tuple[str, str, str, str]]:
        return self._run_state().runtime_invariant_issue_keys

    @_runtime_invariant_issue_keys.setter
    def _runtime_invariant_issue_keys(self, value: set[tuple[str, str, str, str]]) -> None:
        self._run_state().runtime_invariant_issue_keys = value

    async def _refresh_active_snapshot(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]]:
        if not self.store or not tasks:
            return plan, tasks
        parent_session_id = str(getattr(tasks[0], "parent_session_id", "") or tasks[0].metadata.get("parent_session_id", "") or "").strip()
        if not parent_session_id:
            return plan, tasks
        project_id = str(tasks[0].project_id or "default")
        all_tasks = await self.store.get_tasks(project_id=project_id)
        work_item_tasks = [
            task
            for task in all_tasks
            if str(getattr(task, "parent_session_id", "") or "").strip() == parent_session_id
            and projection_id_for_task(task)
        ]
        if not work_item_tasks:
            return plan, tasks
        latest_by_projection_id: dict[str, Task] = {}
        for task in sorted(work_item_tasks, key=lambda item: (item.created_at, item.id)):
            projection_id = projection_id_for_task(task)
            if projection_id:
                latest_by_projection_id[projection_id] = task
        if not latest_by_projection_id:
            return plan, tasks
        plan_data = None
        for task in sorted(latest_by_projection_id.values(), key=lambda item: (item.created_at, item.id), reverse=True):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                plan_data = candidate
                break
        if plan_data:
            plan = deserialize_company_work_item_runtime_plan(plan_data)
        projection_order = plan.projection_order_map()
        refreshed_tasks = sorted(
            latest_by_projection_id.values(),
            key=lambda task: (
                projection_order.get(projection_id_for_task(task), len(projection_order)),
                task.created_at,
                task.id,
            ),
        )
        return plan, refreshed_tasks

    async def _emit_runtime_signal(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.emit_runtime_event is None:
            return
        await self.emit_runtime_event(event_type, payload)

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _configure_external_timeouts(self, task: Task) -> None:
        """Keep external-agent timeouts below the enclosing company work-item timeout."""
        task.metadata = dict(task.metadata)
        work_item_timeout = max(1, int(self.work_item_timeout))
        buffer_seconds = min(60, max(10, work_item_timeout // 4))
        hard_timeout = max(1, work_item_timeout - buffer_seconds)

        existing_hard = self._coerce_positive_int(task.metadata.get("external_hard_timeout_seconds"))
        if existing_hard is not None:
            hard_timeout = min(hard_timeout, existing_hard)

        suggested_idle = max(30, min(hard_timeout, work_item_timeout // 3 if work_item_timeout >= 90 else hard_timeout))
        existing_idle = self._coerce_positive_int(task.metadata.get("external_idle_timeout_seconds"))
        idle_timeout = min(existing_idle, hard_timeout) if existing_idle is not None else suggested_idle

        suggested_startup = min(
            idle_timeout,
            DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS,
        )
        existing_startup = self._coerce_positive_int(task.metadata.get("external_startup_timeout_seconds"))
        startup_timeout = min(existing_startup, idle_timeout) if existing_startup is not None else suggested_startup

        task.metadata["external_hard_timeout_seconds"] = hard_timeout
        task.metadata["external_idle_timeout_seconds"] = idle_timeout
        task.metadata["external_startup_timeout_seconds"] = startup_timeout

    async def _prepare_setup_workspace(self, task: Task) -> None:
        """Ensure workspace roots exist before execution and bootstrap setup layouts when requested."""
        task.metadata = dict(task.metadata)
        work_item_turn_type = self._turn_type_for_task(task)
        workspace_root = str(task.metadata.get("workspace_root", "") or "").strip()
        comms_workspace_root = str(task.metadata.get("comms_workspace_root", "") or "").strip()
        target_output_dir = str(task.metadata.get("target_output_dir", "") or "").strip()

        prepared_roots: list[str] = []
        for raw in [workspace_root, comms_workspace_root, target_output_dir]:
            path_text = str(raw or "").strip()
            if not path_text:
                continue
            target = Path(path_text).expanduser()
            target.mkdir(parents=True, exist_ok=True)
            resolved = str(target.resolve())
            if resolved not in prepared_roots:
                prepared_roots.append(resolved)

        if not prepared_roots:
            return

        primary_root = target_output_dir or workspace_root or comms_workspace_root
        if primary_root:
            task.metadata["setup_workspace_prepared"] = str(Path(primary_root).expanduser().resolve())
        wid = linked_work_item_id_for_task(task)
        progress = [] if wid and self.store else list(task.metadata.get("progress_log", []) or [])
        marker_prefix = "[Setup]" if work_item_turn_type == "setup" else "[Workspace]"
        marker = f"{marker_prefix} Prepared workspace roots: {', '.join(prepared_roots)}"
        if wid and self.store:
            task.metadata.pop("progress_log", None)
            progress = await append_work_item_progress(self.store, wid, marker, dedupe=True)
        elif marker not in progress:
            progress.append(marker)
            task.metadata["progress_log"] = progress[-20:]
        if work_item_turn_type != "setup" or not target_output_dir:
            return
        target = Path(target_output_dir).expanduser()
        projection_id = self._projection_id_for_task(task)
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            reserved_paths: dict[str, str] = {}
            for relative in _DEFAULT_WORKSPACE_LAYOUT:
                path = target / relative
                path.mkdir(parents=True, exist_ok=True)
                reserved_paths[relative] = str(path.resolve())
            manifest_path = target / ".openopc" / "manifests" / "workspace_manifest.json"
            manifest = WorkspaceManifest(
                root_path=str(target.resolve()),
                manifest_path=str(manifest_path.resolve()),
                reserved_paths=reserved_paths,
                status="ready",
                notes=["Prepared automatically by workspace bootstrap before downstream execution."],
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
            task.metadata["workspace_manifest"] = manifest.__dict__
            artifacts = list(task.metadata.get("artifacts", []) or [])
            artifacts.append(str(manifest_path.resolve()))
            task.metadata["artifacts"] = list(dict.fromkeys(str(item).strip() for item in artifacts if str(item).strip()))
            layout_marker = f"[WorkspaceBootstrap] Prepared reserved layout under: {target.resolve()}"
            if wid and self.store:
                task.metadata.pop("progress_log", None)
                progress = await append_work_item_progress(self.store, wid, layout_marker, dedupe=True)
            elif layout_marker not in progress:
                progress.append(layout_marker)
                task.metadata["progress_log"] = progress[-20:]

    def _plan_view_for_task(self, task: Task) -> CompanyWorkItemRuntimePlan | None:
        if self._active_plan is not None:
            return self._active_plan
        plan_data = serialized_company_plan_from_metadata(task.metadata)
        if plan_data:
            return deserialize_company_work_item_runtime_plan(plan_data)
        return None

    def _projection_spec_for_task(self, task: Task) -> WorkItemProjectionSpec | None:
        plan = self._plan_view_for_task(task)
        if plan is None:
            return None
        projection_id = self._projection_id_for_task(task)
        for projection_spec in plan.projections:
            if str(projection_spec.projection_id).strip() == projection_id:
                return projection_spec
        return None

    def _inject_parallel_peers_metadata(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Inject metadata about parallel peer work items so agents know who else is running."""
        parallel_group = str(task.metadata.get("work_item_parallel_group", "") or "").strip()
        if not parallel_group:
            return
        plan = self._plan_view_for_task(task)
        if plan is None:
            return
        current_projection_id = self._projection_id_for_task(task)
        peer_projections: list[dict[str, str]] = []
        for projection in plan.projections:
            if str(projection.parallel_group or "").strip() != parallel_group:
                continue
            projection_id = str(projection.projection_id).strip()
            if projection_id == current_projection_id:
                continue
            peer_task = task_by_projection_id.get(projection_id)
            peer_role = str(projection.role_id or "").strip()
            peer_status = str(peer_task.status.value if peer_task else "unknown").strip()
            peer_projections.append({
                "projection_id": projection_id,
                "title": str(projection.title or "").strip(),
                "role_id": peer_role,
                "parallel_group": parallel_group,
                "status": peer_status,
            })
        if peer_projections:
            task.metadata = dict(task.metadata)
            task.metadata["_work_item_plan_projections"] = peer_projections
            task.metadata["_parallel_peer_count"] = len(peer_projections)

    def _resolve_work_item_assignment_before_execution(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> None:
        projection_spec = self._projection_spec_for_task(task)
        if projection_spec is None:
            return

        helper = self.work_item_helper
        work_item_turn_type = self._infer_work_item_turn_type_for_task(task, projection_spec)
        global_intent_summary = str(task.metadata.get("global_intent_summary", "") or "").strip()
        if not global_intent_summary:
            global_intent_summary = helper._fallback_global_intent_summary(
                str(task.metadata.get("original_message", "") or "")
            )
        current_assignment = helper._coerce_projection_assignment(
            dict(task.metadata.get("work_item_assignment", {}) or {}),
            projection=projection_spec,
            global_intent_summary=global_intent_summary,
        )
        assignment = dict(current_assignment)
        assignment_status = str(task.metadata.get("work_item_assignment_status", "") or "bootstrap").strip() or "bootstrap"
        source_projection_id = str(task.metadata.get("work_item_assignment_source_projection_id", "") or "").strip()

        plan = self._plan_view_for_task(task)
        downstream_consumers = helper._downstream_consumers(plan, projection_spec) if plan is not None else []
        ownership_contract = helper._build_ownership_contract(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            target_output_dir=str(task.metadata.get("target_output_dir", "") or "").strip() or None,
            downstream_consumers=downstream_consumers,
        )
        work_item_runtime_plan = helper._build_work_item_runtime_plan(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            runtime_policy=dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {}),
        )
        coordination_spec = helper._build_coordination_spec(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            runtime_policy=dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {}),
            employee_assignment=dict(task.metadata.get("employee_assignment", {}) or {}),
        )
        lint_issues = helper._lint_work_item_assignment(projection_spec=projection_spec, assignment=assignment)

        task.description = helper._build_work_item_description(assignment)
        task.metadata = dict(task.metadata)
        task.metadata["global_intent_summary"] = assignment["global_intent_summary"]
        task.metadata["work_item_assignment"] = dict(assignment)
        task.metadata["work_item_assignment_status"] = assignment_status
        task.metadata["work_item_assignment_source_projection_id"] = source_projection_id
        task.metadata["work_item_runtime_plan"] = work_item_runtime_plan
        task.metadata["adaptive"] = helper._coordination_spec_dict(coordination_spec)
        task.metadata["acceptance_criteria"] = list(assignment.get("acceptance_criteria", []))
        task.metadata["ownership_contract"] = ownership_contract.__dict__
        task.metadata["work_item_assignment_lint"] = lint_issues
        task.metadata = mark_work_item_projection(
            task.metadata,
            projection_id=str(projection_spec.projection_id or task.id).strip(),
            turn_type=work_item_turn_type,
        )

    def _infer_work_item_turn_type_for_task(self, task: Task, projection_spec: WorkItemProjectionSpec | None = None) -> str:
        if projection_spec is None:
            projection_spec = self._projection_spec_for_task(task)
        if projection_spec is None:
            existing = work_item_turn_type_from_metadata(task.metadata, fallback="")
            if existing:
                return existing
            return "execute"
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id in {_WORKSPACE_BOOTSTRAP_PROJECTION_ID, _DATA_ACQUISITION_PROJECTION_ID}:
            return self.work_item_helper._infer_work_item_turn_type(projection_spec)
        existing = work_item_turn_type_from_metadata(task.metadata, fallback="")
        if existing:
            return existing
        return self.work_item_helper._infer_work_item_turn_type(projection_spec)

    @staticmethod
    def _work_item_gate_enforcement_enabled(task: Task) -> bool:
        runtime_policy = dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {})
        review_policy = dict(runtime_policy.get("review", {}) or {})
        return bool(review_policy.get("enable_work_item_gates", False))

    def _gate_harness_for_task(self, task: Task) -> GateHarness:
        runtime_policy = dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {})
        judge_runner = self._gate_harness_judge_runner if self.role_prompt_runner is not None else None
        return GateHarness(
            policy=dict(runtime_policy.get("gate_harness", {}) or {}),
            llm=None if judge_runner is not None else self.llm,
            org_engine=self.org_engine,
            judge_runner=judge_runner,
        )

    async def _run_role_prompt(
        self,
        *,
        source_task: Task,
        system_prompt: str,
        payload: dict[str, Any],
        prompt_kind: str,
        force_new_session: bool = True,
    ) -> str | None:
        if self.role_prompt_runner is None:
            return None
        try:
            return await self.role_prompt_runner(
                source_task,
                system_prompt,
                payload,
                prompt_kind,
                force_new_session,
            )
        except Exception as exc:
            logger.debug(f"Role prompt runner failed for `{prompt_kind}` on task `{source_task.id}`: {exc}")
            return None

    async def _gate_harness_judge_runner(
        self,
        packet: Any,
        system_prompt: str,
        source_task: Task,
    ) -> str:
        raw = await self._run_role_prompt(
            source_task=source_task,
            system_prompt=system_prompt,
            payload=packet.to_dict(),
            prompt_kind="gate_harness_judge",
            force_new_session=True,
        )
        if raw is None:
            raise RuntimeError("Role prompt runner unavailable for gate harness")
        return raw

    @staticmethod
    def _parse_role_prompt_json(raw: str) -> dict[str, Any] | None:
        text = CompanyWorkItemExecutor._strip_markdown_fences(str(raw or ""))
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return dict(data) if isinstance(data, dict) else None

    def _uses_cell_runtime(self, tasks: list[Task]) -> bool:
        if not self.store or not tasks:
            return False
        return any(str((task.metadata or {}).get("delegation_run_id", "") or "").strip() for task in tasks)

    @staticmethod
    def _uses_multi_team_org_runtime(tasks: list[Task], plan: CompanyWorkItemRuntimePlan | None = None) -> bool:
        if plan is not None and str(plan.metadata.get("execution_model", "") or "").strip() == "multi_team_org":
            return True
        return any(
            str((task.metadata or {}).get("execution_model", "") or "").strip() == "multi_team_org"
            or str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
            for task in tasks
        )

    @staticmethod
    def _multi_team_notification_requires_attention(message: dict[str, Any]) -> bool:
        metadata = dict(message.get("metadata", {}) or {})
        notification_kind = str(
            message.get("notification_kind", "")
            or metadata.get("notification_kind", "")
            or ""
        ).strip().lower()
        semantic_type = str(
            message.get("semantic_type", "")
            or metadata.get("semantic_type", "")
            or ""
        ).strip().lower()
        return notification_kind in {"idle", "blocked", "completion", "status_digest", "task_complete"} or semantic_type in {
            "completion",
            "status_digest",
            "blocker",
        }

    def _multi_team_session_requires_attention(self, session: CompanyMemberSession) -> bool:
        if any(isinstance(item, dict) for item in list(session.protocol_backlog or [])):
            return True
        if any(isinstance(item, dict) and bool(item.get("actionable", True)) for item in list(session.actionable_chat or [])):
            return True
        return any(
            isinstance(item, dict) and self._multi_team_notification_requires_attention(item)
            for item in list(session.notification_backlog or [])
        )

    def _synthetic_inbox_task_exists(self, tasks: list[Task], *, role_id: str, source_message_id: str) -> bool:
        for task in tasks:
            if (
                str(task.assigned_to or "").strip() == role_id
                and bool((task.metadata or {}).get("synthetic_inbox_turn", False))
                and str((task.metadata or {}).get("source_message_id", "") or "").strip() == source_message_id
                and task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED}
            ):
                return True
        return False

    @staticmethod
    def _unblock_attention_session(session: CompanyMemberSession) -> None:
        """Flip a parked manager session back to ``idle`` so it can claim the
        attention work item we just created/revived.

        ``claim_runnable_tasks`` in :class:`CompanyRuntime` skips sessions
        whose normalized role status is ``"blocked"`` unless a review
        soft-wake is available. When a manager delegates children and parks, ``complete_claim``
        leaves them ``blocked``; without this hook, an attention work item
        queued against that session will never be claimed — the review queue
        stays untouched, the worker's "Review needed" message rots in the
        inbox, and the whole runtime stalls.

        We clear the focused work item too; in the three-state model an
        ``idle`` role must not retain focus. ``prepare_task_for_session`` will
        overwrite the current task/assignment when the attention turn is
        actually claimed.
        """
        current_status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        raw_status = str(session.status or "").strip().lower()
        if raw_status and raw_status not in {"idle", "running", "blocked"}:
            session.status = "idle"
            session.resident_status = "idle"
            session.focused_work_item_id = ""
            session.updated_at = datetime.now()
            return
        if current_status != "blocked":
            session.status = current_status
            session.resident_status = current_status
            return
        session.status = "idle"
        session.resident_status = "idle"
        session.focused_work_item_id = ""
        session.updated_at = datetime.now()

    @staticmethod
    def _attention_work_kind_for_session(session: CompanyMemberSession) -> str:
        turn_mode = str(
            session.current_turn_mode
            or dict(session.inbox_state or {}).get("current_turn_mode", "")
            or ""
        ).strip().lower()
        if turn_mode == "deliver_required":
            return "deliver"
        if turn_mode == "synthesize_required":
            return "aggregate"
        if turn_mode == "dispatch_required":
            return "dispatch"
        if turn_mode == "monitor_children":
            return "monitor"
        if turn_mode in {"review_execute", "review_pending"}:
            return "review"
        return "plan"

    @staticmethod
    def _attention_title_for_session(session: CompanyMemberSession, work_kind: str) -> str:
        role_label = str(session.role_id or "seat").strip() or "seat"
        mapping = {
            "deliver": f"Delivery Turn: {role_label}",
            "aggregate": f"Aggregation Turn: {role_label}",
            "dispatch": f"Dispatch Turn: {role_label}",
            "monitor": f"Monitor Children: {role_label}",
            "review": f"Review Turn: {role_label}",
            "plan": f"Attention Turn: {role_label}",
        }
        return mapping.get(work_kind, f"Attention Turn: {role_label}")

    @staticmethod
    def _store_is_ready(store: Any | None) -> bool:
        if store is None:
            return False
        ready = getattr(store, "is_ready", True)
        if callable(ready):
            try:
                return bool(ready())
            except Exception:
                return False
        return bool(ready)

    def _attention_parent_context_metadata(
        self,
        *,
        parent_work_item: DelegationWorkItem | None,
        parent_task: Task | None,
        work_kind: str,
        attention_title: str,
    ) -> dict[str, Any]:
        if parent_work_item is None:
            return {}
        parent_meta = dict(parent_work_item.metadata or {})
        parent_task_meta = dict((parent_task.metadata if parent_task is not None else {}) or {})
        parent_task_snapshot = dict((parent_task.context_snapshot if parent_task is not None else {}) or {})
        parent_work_item_id = str(parent_work_item.work_item_id or "").strip()
        parent_title = str(parent_work_item.title or "").strip()
        parent_summary = str(parent_work_item.summary or parent_meta.get("brief", "") or "").strip()
        latest_directive = str(
            parent_meta.get("latest_user_directive")
            or parent_meta.get("manager_mutation_user_input")
            or parent_task_snapshot.get("user_supplied_input")
            or parent_task_meta.get("latest_user_directive")
            or parent_task_meta.get("manager_mutation_user_input")
            or parent_task_meta.get("user_supplied_input")
            or ""
        ).strip()
        inherited: dict[str, Any] = {
            "attention_business_parent_work_item_id": parent_work_item_id,
            "business_parent_work_item_id": parent_work_item_id,
            "business_parent_title": parent_title,
            "business_parent_summary": parent_summary,
        }
        if latest_directive:
            inherited["latest_user_directive"] = latest_directive
            inherited["manager_mutation_user_input"] = str(
                parent_meta.get("manager_mutation_user_input") or latest_directive
            ).strip()

        parent_contract = dict(parent_meta.get("prompt_contract", {}) or parent_task_meta.get("prompt_contract", {}) or {})
        if has_prompt_contract(parent_contract):
            attention_contract = copy.deepcopy(parent_contract)
            parent_brief = str(
                attention_contract.get("task_brief")
                or parent_summary
                or parent_title
                or ""
            ).strip()
            brief_lines = [
                f"{attention_title}: monitor and reconcile the child board for business parent `{parent_work_item_id}`.",
            ]
            if latest_directive:
                brief_lines.append(
                    "Latest user directive is authoritative for this manager turn: "
                    + clip_text(latest_directive, limit=1000, marker="latest directive truncated").text
                )
            if parent_brief:
                brief_lines.append("Business parent brief: " + parent_brief)
            attention_contract["task_brief"] = "\n\n".join(line for line in brief_lines if line).strip()
            assignment = dict(attention_contract.get("assignment_context", {}) or {})
            upstream = str(assignment.get("upstream_intent_summary", "") or "").strip()
            if latest_directive:
                directive_line = f"Latest user directive: {latest_directive}"
                if directive_line not in upstream:
                    upstream = (
                        directive_line
                        + ("\n\nBusiness parent upstream context: " + upstream if upstream else "")
                    )
            assignment["upstream_intent_summary"] = upstream
            assignment["owned_outcome_kind"] = str(work_kind or assignment.get("owned_outcome_kind") or "monitor").strip()
            attention_contract["assignment_context"] = assignment
            attention_contract["source"] = {
                "kind": "attention_parent_context",
                "parent_work_item_id": parent_work_item_id,
                "attention_work_kind": str(work_kind or "").strip(),
            }
            inherited["prompt_contract"] = attention_contract
        elif parent_summary or parent_title or latest_directive:
            task_brief = parent_summary or parent_title
            if latest_directive:
                task_brief = (
                    f"Latest user directive is authoritative: {latest_directive}"
                    + (f"\n\nBusiness parent brief: {task_brief}" if task_brief else "")
                )
            inherited["prompt_contract"] = make_prompt_contract(
                task_brief=task_brief,
                upstream_intent_summary=(
                    f"Latest user directive: {latest_directive}" if latest_directive else ""
                ),
                owned_outcome_kind=str(work_kind or "monitor").strip() or "monitor",
                source={
                    "kind": "attention_parent_context",
                    "parent_work_item_id": parent_work_item_id,
                    "attention_work_kind": str(work_kind or "").strip(),
                },
            )
        return inherited

    async def _upsert_attention_work_item(
        self,
        *,
        root_task: Task,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        session: CompanyMemberSession,
        source_message: dict[str, Any],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not self.store:
            return tasks, work_items
        run_id = str((root_task.metadata or {}).get("delegation_run_id", "") or "").strip()
        seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
        team_id = str(session.team_id or (session.metadata or {}).get("team_id", "") or "").strip()
        if not run_id or not seat_id or not team_id:
            return tasks, work_items
        work_kind = self._attention_work_kind_for_session(session)
        attention_key = f"{seat_id}:{work_kind}"
        source_message_id = str(source_message.get("msg_id", "") or "").strip()
        summary = str(source_message.get("body", "") or source_message.get("subject", "") or "").strip()
        # Fix 2: resolve session.role_session_id, else build canonical ID
        # from (run_id, role_id, team_instance_id). Never construct a
        # seat-scoped fallback — that was one of the three divergent
        # generator paths that produced duplicate DB rows.
        role_id_for_session = str(
            session.role_id or (session.metadata or {}).get("role_id", "") or ""
        ).strip()
        team_instance_id_for_session = str(
            session.team_instance_id
            or (session.metadata or {}).get("team_instance_id", "")
            or ""
        ).strip()
        role_runtime_session_id = (
            str(session.role_session_id or "").strip()
            or (
                canonical_role_session_id(
                    run_id=run_id,
                    role_id=role_id_for_session,
                    team_instance_id=team_instance_id_for_session,
                )
                if role_id_for_session
                else ""
            )
        )
        seat_state_id = str(session.seat_state_id or f"seat-state::{run_id}::{seat_id}").strip()
        manager_role_id = str(session.manager_role_id or (session.metadata or {}).get("manager_role_id", "") or "").strip()
        manager_seat_id = str((session.metadata or {}).get("manager_seat_id", "") or "").strip()
        current_work_item_id = str(
            session.focused_work_item_id
            or dict(session.current_work_item or {}).get("work_item_id", "")
            or ""
        ).strip()
        current_work_item = next(
            (item for item in work_items if str(item.work_item_id or "").strip() == current_work_item_id),
            None,
        )
        current_task = next(
            (
                task
                for task in tasks
                if linked_work_item_id_for_task(task) == current_work_item_id
            ),
            None,
        )
        current_dependency_ids: list[str] = []
        current_dependencies_done = True
        if current_work_item is not None:
            current_dependency_ids = [
                str(item).strip()
                for item in list((current_work_item.metadata or {}).get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            work_item_by_id = {item.work_item_id: item for item in work_items}
            current_dependencies_done = all(
                (work_item_by_id.get(dep_id).phase if work_item_by_id.get(dep_id) is not None else None) == Phase.APPROVED
                for dep_id in current_dependency_ids
            )
            if work_kind in {"deliver", "aggregate"} and current_dependency_ids and not current_dependencies_done:
                return tasks, work_items
        attention_inherited_metadata = self._attention_parent_context_metadata(
            parent_work_item=current_work_item,
            parent_task=current_task,
            work_kind=work_kind,
            attention_title=self._attention_title_for_session(session, work_kind),
        )
        if current_work_item is not None and current_task is not None:
            if not current_dependency_ids or current_dependencies_done:
                target_phase = current_work_item.phase
                if current_work_item.phase in {Phase.WAITING_DEPENDENCIES, Phase.WAITING_FOR_CHILDREN, Phase.PAUSED, Phase.NEEDS_ATTENTION}:
                    target_phase = (
                        Phase.RUNNING
                        if current_work_item.phase in IN_PROGRESS_PHASES
                        else Phase.READY
                    )
                    await self.store.update_delegation_work_item(
                        current_work_item.work_item_id,
                        phase=target_phase,
                        metadata_updates={
                            "last_attention_source_message_id": source_message_id,
                            "last_attention_at": datetime.now().isoformat(),
                        },
                    )
                if current_task.status in {TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.IDLE}:
                    # Phase A: sync local task.status to match the phase the
                    # hook just projected. Hardcoded PENDING was a latent bug
                    # when target_phase was RUNNING (work_item was in
                    # IN_PROGRESS_PHASES) — the subsequent save_task would
                    # overwrite the hook's task.status=RUNNING with PENDING.
                    current_task.status = task_status_for_phase(target_phase)
                    current_task.metadata = dict(current_task.metadata or {})
                    current_task.metadata["message_priority"] = "seat_attention"
                    await self.save_task(current_task)
                # A manager who parked "blocked" waiting on children becomes
                # eligible again the moment they receive actionable mail
                # (review request, completion update, blocker). Without
                # this explicit unblock, ``claim_runnable_tasks`` skips
                # "blocked" sessions and the attention work item we just
                # resumed never gets claimed — the manager silently never
                # comes back to review children.
                self._unblock_attention_session(session)
                # Push the resumed attention target to the UI immediately.
                await self._notify_kanban_changed()
                return tasks, await self.store.list_delegation_work_items(run_id)
        attention_work_item = next(
            (
                item
                for item in work_items
                if str(item.seat_id or "").strip() == seat_id
                and bool(dict(item.metadata or {}).get("attention_work_item", False))
                and str(dict(item.metadata or {}).get("attention_key", "") or "").strip() == attention_key
                and item.phase not in DONE_PHASES
            ),
            None,
        )
        target_phase: Phase | None = None
        if attention_work_item is None:
            attention_projection_id = f"attention::{seat_id}::{work_kind}::{uuid.uuid4().hex[:8]}"
            attention_work_item = DelegationWorkItem(
                run_id=run_id,
                cell_id=team_id,
                team_instance_id=str(session.team_instance_id or "").strip(),
                team_id=team_id,
                role_id=str(session.role_id or "").strip(),
                seat_id=seat_id,
                seat_state_id=seat_state_id,
                role_runtime_session_id=role_runtime_session_id,
                parent_work_item_id=current_work_item_id or None,
                source_role_id=str(source_message.get("from_agent", "") or "").strip() or None,
                title=self._attention_title_for_session(session, work_kind),
                summary=summary,
                kind=work_kind,
                projection_id=attention_projection_id,
                phase=Phase.READY,
                batch_id=f"attention::{run_id}::{seat_id}",
                batch_index=0,
                manager_role_id=manager_role_id,
                manager_seat_id=manager_seat_id,
                metadata=mark_work_item_projection(mark_work_item_runtime({
                    **attention_inherited_metadata,
                    "runtime_model": "multi_team_org",
                    "session_scope_id": str((session.metadata or {}).get("session_scope_id", "") or "").strip(),
                    "delegation_turn_kind": work_kind,
                    "work_kind": work_kind,
                    "team_id": team_id,
                    "seat_id": seat_id,
                    "seat_state_id": seat_state_id,
                    "assigned_role_runtime_id": role_runtime_session_id,
                    "contact_role_ids": list((session.metadata or {}).get("contact_role_ids", []) or []),
                    "allowed_delegate_role_ids": list((session.metadata or {}).get("allowed_delegate_role_ids", []) or []),
                    "attention_work_item": True,
                    "attention_key": attention_key,
                    "attention_source_message_id": source_message_id,
                    "needs_manager_attention": False,
                    "user_visible": False,
                    "authoritative_output": False,
                }, version=work_item_runtime_version(root_task.metadata)),
                    projection_id=attention_projection_id,
                    turn_type=self._runtime_work_kind_to_work_item_turn_type(work_kind),
                ),
            )
            await self.store.save_delegation_work_item(attention_work_item)
        else:
            # Re-trigger an existing attention card: bring it back to a
            # runnable state so the dispatcher will re-spawn the agent loop.
            if attention_work_item.phase == Phase.PAUSED:
                target_phase = Phase.RUNNING
            elif attention_work_item.phase in TODO_PHASES and attention_work_item.phase != Phase.READY:
                target_phase = Phase.READY
            await self.store.update_delegation_work_item(
                attention_work_item.work_item_id,
                phase=target_phase,
                summary=summary or attention_work_item.summary,
                metadata_updates={
                    **attention_inherited_metadata,
                    "attention_source_message_id": source_message_id,
                    "last_attention_source_message_id": source_message_id,
                    "last_attention_at": datetime.now().isoformat(),
                },
            )
            attention_work_item = await self.store.get_delegation_work_item(
                attention_work_item.work_item_id
            ) or attention_work_item
        updated_work_items = await self.store.list_delegation_work_items(run_id)
        updated_tasks = await self._materialize_work_item_tasks(tasks, updated_work_items)
        projected_task = next(
            (
                task
                for task in updated_tasks
                if linked_work_item_id_for_task(task) == attention_work_item.work_item_id
            ),
            None,
        )
        if projected_task is not None and projected_task.status in {TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.IDLE}:
            # Phase A: sync from the phase we just wrote (if any). When
            # target_phase is None (work_item stayed at its current phase),
            # materialize already projected the current phase — but the
            # status check above said it's still BLOCKED/AWAITING_PEER/IDLE,
            # meaning materialize projected them. Fall back to PENDING
            # (historical default) in that edge case.
            projected_task.status = (
                task_status_for_phase(target_phase)
                if target_phase is not None
                else TaskStatus.PENDING
            )
            projected_task.metadata = dict(projected_task.metadata or {})
            projected_task.metadata["message_priority"] = "seat_attention"
            await self.save_task(projected_task)
        # Same rationale as the current_work_item branch above: a manager
        # session parked "blocked" after delegating children will be
        # skipped by ``claim_runnable_tasks`` unless we flip it back to
        # "idle" now that a fresh attention work item is queued for them.
        self._unblock_attention_session(session)
        # Push the newly-created attention work item to the UI immediately
        # so reviewers / dispatchers surface on the kanban without waiting
        # for the next gather boundary.
        await self._notify_kanban_changed()
        return updated_tasks, updated_work_items

    async def _queue_multi_team_response_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not self.store or not tasks:
            return tasks, work_items
        refreshed_tasks = list(tasks)
        root_task = sorted(refreshed_tasks, key=lambda item: (item.created_at, item.id))[0]
        work_item_by_id = {str(item.work_item_id or "").strip(): item for item in work_items if str(item.work_item_id or "").strip()}
        task_by_work_item_id = await self._task_by_work_item_id(refreshed_tasks)
        for session in self.runtime.member_sessions.values():
            session_status = normalize_role_runtime_status(
                session.status,
                session.focused_work_item_id,
            )
            session.status = session_status
            session.resident_status = session_status
            if session_status == "idle":
                session.focused_work_item_id = ""
            if session_status not in {"idle", "blocked"}:
                continue
            if not self._multi_team_session_requires_attention(session):
                continue
            source_message = None
            for bucket_name, bucket in (
                ("protocol", list(session.protocol_backlog or [])),
                ("chat", list(session.actionable_chat or [])),
                ("notification", list(session.notification_backlog or [])),
            ):
                for item in bucket:
                    if not isinstance(item, dict):
                        continue
                    if bucket_name == "notification" and not self._multi_team_notification_requires_attention(item):
                        continue
                    source_message = dict(item)
                    break
                if source_message is not None:
                    break
            if source_message is None:
                continue
            source_message_id = str(source_message.get("msg_id", "") or "").strip()
            refreshed_tasks, work_items = await self._upsert_attention_work_item(
                root_task=root_task,
                tasks=refreshed_tasks,
                work_items=work_items,
                session=session,
                source_message=source_message,
            )
            work_item_by_id = {str(item.work_item_id or "").strip(): item for item in work_items if str(item.work_item_id or "").strip()}
            task_by_work_item_id = await self._task_by_work_item_id(refreshed_tasks)
        return refreshed_tasks, work_items

    async def _load_delegation_work_items(self, tasks: list[Task]) -> list[DelegationWorkItem]:
        if not self.store or not tasks:
            return []
        run_id = str((tasks[0].metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id or not hasattr(self.store, "list_delegation_work_items"):
            return []
        return await self.store.list_delegation_work_items(run_id)

    @staticmethod
    def _review_status_for_level(review_level: str) -> TaskStatus:
        return (
            TaskStatus.AWAITING_MANAGER_REVIEW
            if str(review_level or "").strip().lower() == "manager"
            else TaskStatus.AWAITING_HUMAN
        )

    def _review_chain_for_task(self, task: Task) -> list[str]:
        direct_manager = str(task.metadata.get("manager_role_id", "") or "").strip()
        if direct_manager:
            chain = [direct_manager]
            if self.org_engine is not None:
                current = direct_manager
                seen = {self._role_id_for_task(task), direct_manager}
                while current:
                    agent = self.org_engine.get_agent(current)
                    parent = str(getattr(agent, "reports_to", "") or "").strip()
                    if not parent or parent == "owner" or parent in seen:
                        break
                    chain.append(parent)
                    seen.add(parent)
                    current = parent
            return chain
        if self.org_engine is None:
            return []
        role_id = self._role_id_for_task(task)
        if not role_id or not hasattr(self.org_engine, "get_chain_of_command"):
            return []
        try:
            chain = list(self.org_engine.get_chain_of_command(role_id))
        except Exception:
            return []
        return [
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in chain[1:]
            if str(getattr(agent, "role_id", "") or "").strip()
        ]

    @staticmethod
    def _manager_inbox_fingerprint(session: CompanyMemberSession) -> str:
        digest = dict(session.manager_digest or {})
        messages = [
            *(list(digest.get("actionable_chat", []) or [])),
            *(list(digest.get("pending_decisions", []) or [])),
        ]
        if not messages:
            return ""
        normalized = [
            {
                "msg_id": str(item.get("msg_id", "") or item.get("message_id", "") or "").strip(),
                "from_agent": str(item.get("from_agent", "") or "").strip(),
                "subject": str(item.get("subject", "") or "").strip(),
                "body": str(item.get("body", "") or item.get("summary", "") or "").strip()[:240],
                "notification_kind": str(item.get("notification_kind", "") or "").strip(),
            }
            for item in messages
            if isinstance(item, dict)
        ]
        if not normalized:
            return ""
        encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _session_mailbox_messages(session: CompanyMemberSession) -> list[dict[str, Any]]:
        return [
            *(list(session.actionable_chat or [])),
            *(list(session.protocol_backlog or [])),
            *(list(session.notification_backlog or [])),
        ]

    @classmethod
    def _mailbox_release_matched(
        cls,
        session: CompanyMemberSession,
        work_item: DelegationWorkItem,
    ) -> tuple[bool, str]:
        metadata = dict(work_item.metadata or {})
        if str(metadata.get("release_policy", "auto") or "auto").strip().lower() != "mailbox_ack":
            return (False, "")
        source_message_id = str(metadata.get("source_message_id", "") or "").strip()
        required_semantic_type = str(metadata.get("release_on_semantic_type", "") or "").strip().lower()
        if not source_message_id and not required_semantic_type:
            return (False, "")
        for item in cls._session_mailbox_messages(session):
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("msg_id", "") or "").strip()
            semantic_type = str(
                item.get("semantic_type")
                or dict(item.get("metadata", {}) or {}).get("semantic_type")
                or ""
            ).strip().lower()
            if source_message_id and msg_id != source_message_id:
                continue
            if required_semantic_type and semantic_type != required_semantic_type:
                continue
            return (True, msg_id)
        return (False, "")

    @staticmethod
    def _checkpoint_basis_hash(task: Task) -> str:
        output_metadata = dict((getattr(task, "context_snapshot", {}) or {}).get("work_item_owned_outputs", {}) or {})
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        else:
            result_content = ""
        payload = {
            "task_id": task.id,
            **work_item_identity_payload_for_task(task),
            "delivery_revision": task.metadata.get("delivery_revision", ""),
            "owner_directive_revision": task.metadata.get("owner_directive_revision", ""),
            "result_content": result_content,
            "work_item_summary": str(output_metadata.get("work_item_summary", "") or task.metadata.get("work_item_summary", "") or "").strip(),
            "work_item_summary_for_downstream": str(
                output_metadata.get("work_item_summary_for_downstream", "")
                or task.metadata.get("work_item_summary_for_downstream", "")
                or ""
            ).strip(),
            "artifact_index": list(output_metadata.get("work_item_artifact_index", []) or task.metadata.get("work_item_artifact_index", []) or []),
            "verification_status": dict(output_metadata.get("verification_status", {}) or task.metadata.get("verification_status", {}) or {}),
            "verification_evidence": dict(output_metadata.get("verification_evidence", {}) or task.metadata.get("verification_evidence", {}) or {}),
            "verification_verdict": str(task.metadata.get("verification_verdict", "") or "").strip(),
            "delivery_package": output_metadata.get("delivery_package") or task.metadata.get("delivery_package") or {},
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_adaptive_metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        metadata = dict(value)
        metadata["work_item_profile"] = dict(metadata.get("work_item_profile", {}) or {})
        metadata["role_profile"] = dict(metadata.get("role_profile", {}) or {})
        normalized_signals: list[dict[str, Any]] = []
        for item in list(metadata.get("signals", []) or []):
            if not isinstance(item, dict):
                continue
            normalized_signals.append(
                {
                    "name": str(item.get("name", "") or "").strip(),
                    "owner_role_id": str(item.get("owner_role_id", "") or "").strip(),
                    "required": bool(item.get("required", True)),
                    "strict": bool(item.get("strict", False)),
                    "satisfied": bool(item.get("satisfied", False)),
                    "evidence": [
                        str(entry).strip()
                        for entry in list(item.get("evidence", []) or [])
                        if str(entry).strip()
                    ],
                }
            )
        metadata["signals"] = normalized_signals
        metadata["hard_dependency_work_item_ids"] = [
            str(item).strip()
            for item in list(metadata.get("hard_dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        metadata["soft_dependency_work_item_ids"] = [
            str(item).strip()
            for item in list(metadata.get("soft_dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        metadata["emitted_signals"] = [
            str(item).strip()
            for item in list(metadata.get("emitted_signals", []) or [])
            if str(item).strip()
        ]
        metadata["required_artifacts"] = [
            str(item).strip()
            for item in list(metadata.get("required_artifacts", []) or metadata.get("work_item_profile", {}).get("required_artifacts", []) or [])
            if str(item).strip()
        ]
        return metadata

    @staticmethod
    def _adaptive_turn_kind(adaptive: dict[str, Any], *, fallback: str = "execute") -> str:
        work_item_profile = dict(adaptive.get("work_item_profile", {}) or {})
        return str(work_item_profile.get("turn_kind", "") or fallback).strip().lower() or fallback

    @staticmethod
    def _coordination_policy_for_work_item(metadata: dict[str, Any]) -> dict[str, Any]:
        runtime_policy = dict(metadata.get("policy") or metadata.get("runtime_policy", {}) or {})
        return dict(runtime_policy.get("coordination", {}) or {})

    @classmethod
    def _strict_gate_turn_kinds_for_metadata(cls, metadata: dict[str, Any]) -> set[str]:
        coordination = cls._coordination_policy_for_work_item(metadata)
        configured = {
            str(item).strip().lower()
            for item in list(coordination.get("strict_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        return configured or {"verify", "deliver"}

    @classmethod
    def _mixed_gate_turn_kinds_for_metadata(cls, metadata: dict[str, Any]) -> set[str]:
        coordination = cls._coordination_policy_for_work_item(metadata)
        configured = {
            str(item).strip().lower()
            for item in list(coordination.get("mixed_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        return configured or {"synthesize", "review", "integration"}

    @classmethod
    def _required_signals_satisfied(cls, adaptive: dict[str, Any]) -> bool:
        for signal in list(adaptive.get("signals", []) or []):
            if not isinstance(signal, dict):
                continue
            if bool(signal.get("required", True)) and not bool(signal.get("satisfied", False)):
                return False
        return True

    @staticmethod
    def _required_artifacts_present(adaptive: dict[str, Any], task: Task | None = None) -> bool:
        required_artifacts = [
            str(item).strip()
            for item in list(adaptive.get("required_artifacts", []) or [])
            if str(item).strip()
        ]
        if not required_artifacts:
            return True
        if task is None:
            return False
        available = {
            str(item).strip()
            for item in list(task.metadata.get("artifacts", []) or [])
            if str(item).strip()
        }
        output_metadata = CompanyWorkItemExecutor._work_item_output_metadata_for_task(task)
        available.update(
            str(item.get("value", "") or "").strip()
            for item in list(output_metadata.get("work_item_artifact_index", []) or task.metadata.get("work_item_artifact_index", []) or [])
            if isinstance(item, dict) and str(item.get("value", "") or "").strip()
        )
        return all(any(required in candidate for candidate in available) for required in required_artifacts)

    @classmethod
    def _work_item_is_runnable(
        cls,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
        task_by_work_item_id: dict[str, Task] | None = None,
    ) -> bool:
        phase = work_item.phase
        metadata = dict(work_item.metadata or {})
        review_execution_work_item = is_review_execution_work_item_metadata(metadata)
        report_execution_work_item = is_report_execution_work_item_metadata(metadata)
        if str(metadata.get("dispatch_hold", "") or "").strip():
            return False
        # Attempt-ledger brake: cards whose recent attempts keep crashing or
        # keep getting interrupted must not be re-enqueued — this check also
        # covers the review/report `or phase == RUNNING` arms below that
        # bypass is_dispatchable. The per-tick ledger reconcile pass
        # terminalizes such cards with a visible blocked_reason.
        if attempt_ledger_dispatch_block_reason(metadata):
            return False
        # Hidden auxiliary cards (review / report) are still runnable —
        # they are the kanban-push primitives the dispatcher schedules.
        # Worker work items marked hidden for any other reason stay
        # excluded.
        if (
            should_hide_work_item_from_company_kanban(metadata)
            and not review_execution_work_item
            and not report_execution_work_item
        ):
            return False
        if review_execution_work_item:
            target_work_item_id = str(metadata.get("review_target_work_item_id", "") or "").strip()
            if not target_work_item_id:
                return False
            target_work_item = work_item_by_id.get(target_work_item_id)
            if target_work_item is None:
                return False
            if target_work_item.phase not in IN_REVIEW_PHASES:
                return False
            review_owner_seat_id = str(target_work_item.metadata.get("review_owner_seat_id", "") or "").strip()
            if review_owner_seat_id and review_owner_seat_id != str(work_item.seat_id or "").strip():
                return False
            return is_dispatchable(work_item) or phase == Phase.RUNNING
        if report_execution_work_item:
            # The report card is owned by the worker seat. Sanity-check
            # the parent worker work item is still in review (it should
            # be — the report card was spawned the moment the parent
            # transitioned to AWAITING_MANAGER_REVIEW). If the parent
            # has somehow already moved past review, the report card is
            # obsolete; let it sit (cleanup elsewhere).
            target_work_item_id = str(metadata.get("report_target_work_item_id", "") or "").strip()
            if target_work_item_id:
                target_work_item = work_item_by_id.get(target_work_item_id)
                if target_work_item is not None and target_work_item.phase not in IN_REVIEW_PHASES:
                    return False
            return is_dispatchable(work_item) or phase == Phase.RUNNING
        # Worker can resume from in_progress sub-states (paused, needs_attention,
        # waiting_for_children) once the awaited event arrives, or from any
        # in-flight phase whose claim was cleared by the stale-claim sweeper
        # (Bug C — restart recovery).
        runnable_in_progress = {Phase.PAUSED, Phase.NEEDS_ATTENTION, Phase.WAITING_FOR_CHILDREN}
        if not (is_runnable(phase) or phase in runnable_in_progress or is_orphaned(work_item)):
            return False
        if str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org":
            followup_task = (
                task_by_work_item_id.get(str(getattr(work_item, "work_item_id", "") or "").strip())
                if task_by_work_item_id is not None
                else None
            )
            followup_task_pending = (
                followup_task is not None
                and followup_task.status == TaskStatus.PENDING
                and bool((followup_task.metadata or {}).get("followup_routed_to_final_decider", False))
                and str((followup_task.metadata or {}).get("current_turn_mode", "") or "").strip() == "dispatch_required"
            )
            if (
                bool(metadata.get("followup_routed_to_final_decider", False))
                and str(metadata.get("current_turn_mode", "") or "").strip() == "dispatch_required"
                and (
                    followup_task is None
                    or followup_task_pending
                    or followup_task.status == TaskStatus.PENDING
                )
            ):
                return True
            dependency_ids = [
                str(item).strip()
                for item in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            dependency_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
                dependency_ids,
                work_item_by_id,
                owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
            )
            dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
            settled_failure_ids = settled_failure_dependency_ids(metadata)
            for dep_id in dependency_ids:
                dependency = work_item_by_id.get(dep_id)
                if dependency is None:
                    continue
                dep_phase = dependency.phase
                dep_class = str(
                    dependency_classes.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
                    or DEPENDENCY_CLASS_DEFAULT
                ).strip().lower()
                if dep_class == "info":
                    continue
                if dep_class == "soft":
                    if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                        if dep_id in settled_failure_ids:
                            continue
                        return False
                    continue
                if dep_phase != Phase.APPROVED:
                    # Failure-triage release: the frontier pass stamped this
                    # card's dependency_settlement over the dep (terminal
                    # failure, or stuck behind one), so it is settled
                    # context here, not a blocker.
                    if dep_id in settled_failure_ids:
                        continue
                    return False
            return True
        adaptive = cls._normalize_adaptive_metadata(metadata.get("adaptive", {}))
        dep_classes_map = dict(metadata.get("dependency_classes", {}) or {})
        all_dep_ids = list(dict.fromkeys([
            *[
                str(item).strip()
                for item in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ],
            *list(adaptive.get("hard_dependency_work_item_ids", []) or []),
        ]))
        all_dep_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
            all_dep_ids,
            work_item_by_id,
            owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
        )
        settled_failure_ids = settled_failure_dependency_ids(metadata)
        for dep_id in all_dep_ids:
            # Default must match _dependency_release_state and the doomed
            # computation (both DEPENDENCY_CLASS_DEFAULT="hard"): a "soft"
            # default here let a card count as runnable while the frontier
            # counted it doomed — divergent verdicts on the same dep.
            dep_class = dep_classes_map.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
            dependency = work_item_by_id.get(dep_id)
            if dependency is None:
                continue
            dep_phase = dependency.phase
            if dep_class == "hard" and dep_phase != Phase.APPROVED:
                if dep_id in settled_failure_ids:
                    continue
                return False
            if dep_class == "soft" and dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                if dep_id in settled_failure_ids:
                    continue
                return False
        if str(adaptive.get("normalized_state", "") or "").strip().lower() == "invalidated":
            return False
        if list(adaptive.get("missing_decisions", []) or []):
            return False
        if not cls._required_signals_satisfied(adaptive):
            return False
        task = None
        if task_by_work_item_id is not None:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
        if not cls._required_artifacts_present(adaptive, task):
            return False
        turn_kind = cls._adaptive_turn_kind(adaptive, fallback=str(metadata.get("work_kind", "") or work_item.kind or "execute").strip().lower() or "execute")
        strict_gate_turn_kinds = cls._strict_gate_turn_kinds_for_metadata(metadata)
        if turn_kind in strict_gate_turn_kinds:
            return True
        mixed_gate_turn_kinds = cls._mixed_gate_turn_kinds_for_metadata(metadata)
        if turn_kind in mixed_gate_turn_kinds:
            return True
        return True

    async def _reconcile_attempt_ledger(
        self,
        work_items: list[DelegationWorkItem],
        active_work_item_tasks: dict[
            "asyncio.Task[TaskResult | None]",
            tuple["CompanyMemberSession", Task],
        ],
    ) -> list[DelegationWorkItem]:
        """Per-tick attempt-ledger reconcile: the structural anti-loop pass.

        Two responsibilities, both driven by durable state instead of any
        code path "remembering" to behave:

        1. **Back-fill dead attempts.** A card whose claim opened an attempt
           (``attempt_settled == False``) but has no live owner in THIS
           dispatcher (not in ``active_work_item_tasks`` / claimed sets) had
           its coroutine or process die without a verdict — kill -9, cancel
           before harvest, or a settlement write that never landed. Settle it
           as ``interrupted`` so the ledger sees it.
        2. **Terminalize over-limit cards.** When the ledger shows a run of
           consecutive crashed/interrupted attempts over the limit, transition
           the card to FAILED with a visible ``blocked_reason`` (quarantine
           via ``dispatch_hold`` if even that write fails). Eligibility checks
           (``is_dispatchable`` / ``_work_item_is_runnable``) independently
           refuse such cards, so either write path converges the loop.
        """
        if not work_items or self.store is None:
            return work_items
        in_flight_ids: set[str] = {
            linked_work_item_id_for_task(claimed_task)
            for _member, claimed_task in active_work_item_tasks.values()
        }
        in_flight_ids.discard("")
        in_flight_ids.update(
            str(item) for item in getattr(self.runtime, "_claimed_work_item_ids", set()) or set()
        )
        reconciled: list[DelegationWorkItem] = []
        for work_item in work_items:
            phase = getattr(work_item, "phase", None)
            work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
            if not work_item_id or phase in DONE_PHASES:
                reconciled.append(work_item)
                continue
            metadata = dict(getattr(work_item, "metadata", {}) or {})
            if has_open_attempt(metadata) and work_item_id not in in_flight_ids:
                if await settle_open_attempt_as_interrupted(self.store, work_item):
                    metadata = dict(getattr(work_item, "metadata", {}) or {})
                    logger.info(
                        "[attempt_ledger] settled dead attempt as interrupted "
                        "work_item={} phase={} interrupted_streak={}",
                        work_item_id,
                        getattr(phase, "value", phase),
                        metadata.get("attempt_interrupted_streak"),
                    )
            block_reason = attempt_ledger_dispatch_block_reason(metadata)
            if not block_reason:
                reconciled.append(work_item)
                continue
            summary_text = (
                f"Work item quarantined by the attempt ledger: {block_reason}. "
                "Its recent execution attempts kept dying without a durable verdict; "
                "manual triage (rework or new card) is required."
            )
            try:
                updated = await transition_work_item(
                    self.store,
                    work_item_id,
                    target_phase=Phase.FAILED,
                    reason="attempt_ledger_limit",
                    summary=summary_text,
                    metadata_updates={"attempt_ledger_block_reason": block_reason},
                    release_claim=True,
                )
                if updated is not None:
                    work_item = updated
                try:
                    await self.store.update_delegation_work_item(
                        work_item_id,
                        blocked_reason=block_reason,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "[attempt_ledger] blocked_reason write failed for {}", work_item_id
                    )
                await self._emit_progress(
                    f"[Company:{projection_id_for_work_item(work_item)}] {summary_text}"
                )
            except InvalidPhaseTransition:
                # A concurrent writer moved the card; re-read and keep going —
                # eligibility checks still refuse it while the ledger is over
                # the limit.
                logger.opt(exception=True).debug(
                    "[attempt_ledger] FAILED terminalize lost a phase race for {}",
                    work_item_id,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "[attempt_ledger] FAILED terminalize did not land for {}; "
                    "quarantining via dispatch_hold",
                    work_item_id,
                )
                try:
                    await self.store.update_delegation_work_item(
                        work_item_id,
                        blocked_reason=block_reason,
                        metadata_updates={
                            "dispatch_hold": "attempt_ledger_quarantine",
                            "attempt_ledger_block_reason": block_reason,
                        },
                    )
                except Exception:
                    logger.opt(exception=True).error(
                        "[attempt_ledger] quarantine hold write also failed for {}",
                        work_item_id,
                    )
            reconciled.append(work_item)
        return reconciled

    def _task_effective_projection_spec(self, task: Task) -> WorkItemProjectionSpec:
        projection = self._projection_spec_for_task(task)
        if projection is not None:
            return projection
        return WorkItemProjectionSpec(
            projection_id=self._projection_id_for_task(task),
            turn_type=self._turn_type_for_task(task, fallback="execute"),
            title=str(task.title or "Runtime Work Item").strip() or "Runtime Work Item",
            summary=str(task.description or "").strip(),
            role_id=str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "executor").strip() or "executor",
            dependency_projection_ids=[],
            execution_strategy=WorkItemExecutionStrategy.AUTO.value,
            metadata=dict(task.metadata.get("work_item_metadata", {}) or {}),
        )

    @staticmethod
    def _work_item_effective_projection_spec(work_item: DelegationWorkItem) -> WorkItemProjectionSpec:
        return WorkItemProjectionSpec(
            projection_id=projection_id_for_work_item(work_item),
            turn_type=turn_type_for_work_item(work_item),
            title=str(work_item.title or work_item.projection_id or "Runtime Work Item").strip(),
            summary=str(work_item.summary or "").strip(),
            role_id=str(work_item.role_id or "executor").strip() or "executor",
            team_id=str(work_item.team_id or "").strip(),
            seat_id=str(work_item.seat_id or "").strip(),
            manager_role_id=str(work_item.manager_role_id or "").strip(),
            manager_seat_id=str(work_item.manager_seat_id or "").strip(),
            metadata=dict(work_item.metadata or {}),
        )

    def _build_runtime_coordination_spec(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        task: Task | None,
        work_item: DelegationWorkItem | None,
        plan: CompanyWorkItemRuntimePlan | None,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        handoff_records: list[Any],
        work_item_decisions: list[Any],
    ) -> dict[str, Any]:
        helper = self.work_item_helper
        base_assignment = {
            "inputs": list((task.metadata.get("work_item_assignment", {}) or {}).get("inputs", []) if task else []),
            "deliverables": list((task.metadata.get("work_item_assignment", {}) or {}).get("deliverables", []) if task else []),
        }
        base_spec = helper._coordination_spec_dict(
            helper._build_coordination_spec(
                projection_spec=projection_spec,
                assignment=base_assignment,
                work_item_turn_type=self._infer_work_item_turn_type_for_task(task, projection_spec) if task is not None else helper._infer_work_item_turn_type(projection_spec),
                runtime_policy=dict((task.metadata or {}).get("policy") or (task.metadata or {}).get("runtime_policy", {}) if task is not None else (plan.metadata.get("policy") or plan.metadata.get("runtime_policy", {}) if plan is not None else {})),
                employee_assignment=dict((task.metadata or {}).get("employee_assignment", {}) if task is not None else {}),
            )
        )
        adaptive = self._normalize_adaptive_metadata(base_spec)
        turn_kind = self._adaptive_turn_kind(adaptive)
        work_item_by_id = {item.work_item_id: item for item in work_items}
        def _work_item_is_approved(candidate: DelegationWorkItem | None) -> bool:
            return candidate is not None and getattr(candidate, "phase", None) == Phase.APPROVED

        current_work_item_id = str(getattr(work_item, "work_item_id", "") or linked_work_item_id_for_task(task) or "").strip()
        emitted_signal_sources: dict[str, list[str]] = {signal: [] for signal in _CANONICAL_COORDINATION_SIGNALS}
        turn_kind_by_work_item: dict[str, str] = {}
        for candidate in work_items:
            candidate_task = next(
                (
                    item for item in tasks
                    if linked_work_item_id_for_task(item) == str(candidate.work_item_id or "").strip()
                ),
                None,
            )
            candidate_projection = self._task_effective_projection_spec(candidate_task) if candidate_task is not None else self._work_item_effective_projection_spec(candidate)
            candidate_adaptive = self._normalize_adaptive_metadata(
                dict((candidate_task.metadata or {}).get("adaptive", {}) if candidate_task is not None else (candidate.metadata or {}).get("adaptive", {}))
            )
            if not candidate_adaptive:
                candidate_adaptive = self._normalize_adaptive_metadata(
                    helper._coordination_spec_dict(
                        helper._build_coordination_spec(
                            projection_spec=candidate_projection,
                            assignment={"inputs": [], "deliverables": []},
                            work_item_turn_type=self._infer_work_item_turn_type_for_task(candidate_task, candidate_projection) if candidate_task is not None else helper._infer_work_item_turn_type(candidate_projection),
                            runtime_policy=dict((candidate_task.metadata or {}).get("policy") or (candidate_task.metadata or {}).get("runtime_policy", {}) if candidate_task is not None else (plan.metadata.get("policy") or plan.metadata.get("runtime_policy", {}) if plan is not None else {})),
                            employee_assignment=dict((candidate_task.metadata or {}).get("employee_assignment", {}) if candidate_task is not None else {}),
                        )
                    )
            )
            candidate_turn_kind = self._adaptive_turn_kind(candidate_adaptive, fallback=str((candidate.metadata or {}).get("work_kind", "") or candidate.kind or "execute").strip().lower() or "execute")
            turn_kind_by_work_item[candidate.work_item_id] = candidate_turn_kind
            if not _work_item_is_approved(candidate):
                continue
            for emitted in list(candidate_adaptive.get("emitted_signals", []) or []):
                signal_name = str(emitted).strip()
                if signal_name:
                    emitted_signal_sources.setdefault(signal_name, []).append(candidate.work_item_id)
        hard_dependency_ids = [
            str(item).strip()
            for item in list((work_item.metadata or {}).get("dependency_work_item_ids", []) if work_item is not None else [])
            if str(item).strip()
        ]
        # Cell-scoped inferred dependencies: only add hard deps within the same
        # cell (or when no cell_id is set).  Cross-cell work items are treated as
        # soft / info by the flexible dependency system and should not create
        # implicit hard blockers.
        current_cell = str((work_item.metadata or {}).get("cell_id", "") or (work_item.metadata or {}).get("delegation_cell_id", "") or "").strip() if work_item is not None else ""

        def _same_cell(candidate: "DelegationWorkItem") -> bool:
            if not current_cell:
                return True
            candidate_cell = str((candidate.metadata or {}).get("cell_id", "") or (candidate.metadata or {}).get("delegation_cell_id", "") or "").strip()
            return not candidate_cell or candidate_cell == current_cell

        if turn_kind == "execute":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) in {"setup", "acquire"}
                and _same_cell(item)
            )
        elif turn_kind == "verify":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) in {"setup", "acquire", "execute"}
                and _same_cell(item)
            )
        elif turn_kind == "deliver":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) != "deliver"
                and _same_cell(item)
            )
        hard_dependency_ids = list(dict.fromkeys(item for item in hard_dependency_ids if item))
        required_decisions: list[str] = []
        missing_decisions: list[str] = []
        manager_release_satisfied = False
        mixed_gate_turn_kinds = self._mixed_gate_turn_kinds_for_metadata(
            task.metadata if task is not None else (work_item.metadata if work_item is not None else {})
        )
        decision_gate_requested = bool(
            (
                (work_item.metadata or {}).get("needs_manager_attention", False)
                or work_item.phase == Phase.NEEDS_ATTENTION
            )
            if work_item is not None
            else False
        )
        if turn_kind in mixed_gate_turn_kinds and decision_gate_requested:
            requirement = f"manager_release:{projection_spec.projection_id}"
            required_decisions.append(requirement)
            matching_decisions = []
            for record in work_item_decisions:
                record_projection_id = str(getattr(record, "projection_id", "") or "").strip()
                details = dict(getattr(record, "details", {}) or {})
                if record_projection_id == str(projection_spec.projection_id or "").strip():
                    matching_decisions.append(record)
                    continue
                target_projection_id = str(
                    details.get("target_projection_id")
                    or ""
                ).strip()
                if target_projection_id == str(projection_spec.projection_id or "").strip():
                    matching_decisions.append(record)
                    continue
                if current_work_item_id and str(details.get("target_work_item_id", "") or "").strip() == current_work_item_id:
                    matching_decisions.append(record)
            manager_release_satisfied = bool(matching_decisions)
            if not manager_release_satisfied:
                missing_decisions.append(requirement)
        normalized_signals: list[dict[str, Any]] = []
        has_setup_work_item = any(kind == "setup" for kind in turn_kind_by_work_item.values())
        has_acquire_work_item = any(kind == "acquire" for kind in turn_kind_by_work_item.values())
        has_verify_work_item = any(kind == "verify" for kind in turn_kind_by_work_item.values())
        non_provider_execute = [
            work_item_id
            for work_item_id, candidate_turn_kind in turn_kind_by_work_item.items()
            if candidate_turn_kind == "execute"
        ]
        for signal in list(adaptive.get("signals", []) or []):
            signal_name = str(signal.get("name", "") or "").strip()
            evidence: list[str] = []
            satisfied = False
            if signal_name == "scope_locked":
                satisfied = all(
                    (dependency := work_item_by_id.get(dep_id)) is not None
                    and _work_item_is_approved(dependency)
                    for dep_id in [
                        str(item).strip()
                        for item in list((work_item.metadata or {}).get("dependency_work_item_ids", []) if work_item is not None else [])
                        if str(item).strip()
                    ]
                )
                if not work_item or not list((work_item.metadata or {}).get("dependency_work_item_ids", []) or []):
                    satisfied = True
                if satisfied:
                    evidence.append("manager_scope_ready")
            elif signal_name == "env_ready":
                satisfied = True if not has_setup_work_item else bool(emitted_signal_sources.get("env_ready"))
                evidence = list(emitted_signal_sources.get("env_ready", []) or [])
            elif signal_name == "inputs_ready":
                satisfied = True if not has_acquire_work_item else bool(emitted_signal_sources.get("inputs_ready"))
                evidence = list(emitted_signal_sources.get("inputs_ready", []) or [])
            elif signal_name == "implementation_ready":
                satisfied = True if not non_provider_execute else all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in non_provider_execute
                    if dep_id != current_work_item_id
                )
                evidence = [dep_id for dep_id in non_provider_execute if dep_id != current_work_item_id]
            elif signal_name == "qa_ready":
                verify_items = [dep_id for dep_id, candidate_turn_kind in turn_kind_by_work_item.items() if candidate_turn_kind == "verify" and dep_id != current_work_item_id]
                satisfied = True if not has_verify_work_item else all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in verify_items
                )
                evidence = verify_items
            elif signal_name == "delivery_ready":
                remaining = [
                    item.work_item_id
                    for item in work_items
                    if item.work_item_id != current_work_item_id
                    and turn_kind_by_work_item.get(item.work_item_id) != "deliver"
                ]
                satisfied = all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in remaining
                )
                evidence = remaining
            else:
                signal_sources = list(emitted_signal_sources.get(signal_name, []) or [])
                satisfied = bool(signal_sources)
                evidence = signal_sources
            normalized_signals.append(
                {
                    **dict(signal),
                    "name": signal_name,
                    "satisfied": satisfied,
                    "evidence": evidence,
                }
            )
        adaptive["signals"] = normalized_signals
        adaptive["hard_dependency_work_item_ids"] = hard_dependency_ids
        adaptive["soft_dependency_work_item_ids"] = []
        adaptive["required_decisions"] = required_decisions
        adaptive["missing_decisions"] = missing_decisions
        adaptive["manager_release_satisfied"] = manager_release_satisfied
        adaptive["required_artifacts"] = list(adaptive.get("required_artifacts", []) or [])
        adaptive["evidence"] = list(dict.fromkeys([
            *list(adaptive.get("evidence", []) or []),
            f"work_item_decisions:{len(work_item_decisions)}",
            f"work_item_release_decisions:{int(manager_release_satisfied)}/{len(required_decisions)}",
        ]))
        missing_dependencies = [
            dep_id
            for dep_id in hard_dependency_ids
            if not _work_item_is_approved(work_item_by_id.get(dep_id))
        ]
        missing_signals = [
            str(item.get("name", "") or "").strip()
            for item in normalized_signals
            if bool(item.get("required", True)) and not bool(item.get("satisfied", False))
        ]
        manager_attention_pending = (
            decision_gate_requested
            and bool(required_decisions)
            and not manager_release_satisfied
            and not missing_dependencies
            and not missing_signals
        )
        if str((task.metadata or {}).get("upstream_ceo_rework_source_projection_id", "") if task is not None else "").strip():
            adaptive["normalized_state"] = "invalidated"
        elif missing_dependencies:
            adaptive["normalized_state"] = "waiting_for_deps"
        elif manager_attention_pending:
            adaptive["normalized_state"] = "needs_manager_attention"
        elif missing_decisions or missing_signals:
            adaptive["normalized_state"] = "waiting_for_gate" if turn_kind in self._strict_gate_turn_kinds_for_metadata(task.metadata if task is not None else (work_item.metadata if work_item is not None else {})) else "waiting_for_deps"
        else:
            phase_value = (
                work_item.phase.value
                if work_item is not None
                else (task.status.value if task is not None else "planned")
            )
            state_map = {
                Phase.RUNNING.value: "running",
                Phase.APPROVED.value: "done",
                Phase.FAILED.value: "failed",
                Phase.CANCELLED.value: "cancelled",
                Phase.WAITING_FOR_PEER.value: "awaiting_peer",
                Phase.WAITING_FOR_CHILDREN.value: "blocked",
                Phase.AWAITING_MANAGER_REVIEW.value: "awaiting_manager_review",
                Phase.AWAITING_HUMAN.value: "awaiting_human",
                Phase.PAUSED.value: "blocked",
                Phase.NEEDS_ATTENTION.value: "needs_manager_attention",
                Phase.WAITING_DEPENDENCIES.value: "waiting_for_deps",
                Phase.QUEUED.value: "ready",
                Phase.READY.value: "ready",
                Phase.READY_FOR_REWORK.value: "ready",
            }
            adaptive["normalized_state"] = state_map.get(phase_value, "planned")
        adaptive["blocked_reason"] = ""
        if missing_dependencies:
            adaptive["blocked_reason"] = f"Waiting for hard dependencies: {', '.join(missing_dependencies)}"
        elif missing_decisions:
            adaptive["blocked_reason"] = f"Waiting for runtime decision: {', '.join(missing_decisions)}"
        elif missing_signals:
            adaptive["blocked_reason"] = f"Waiting for required signals: {', '.join(missing_signals)}"
        elif adaptive["normalized_state"] == "invalidated":
            adaptive["blocked_reason"] = "Upstream gated work item re-entered rework; this work item was invalidated."
        return adaptive

    async def _refresh_adaptive_coordination(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not tasks:
            return tasks, work_items
        handoff_records: list[Any] = []
        work_item_decisions: list[Any] = []
        if self.store and hasattr(self.store, "get_handoff_records"):
            try:
                handoff_records = await self.store.get_handoff_records(project_id=tasks[0].project_id, limit=50)
            except Exception:
                handoff_records = []
        if self.store and hasattr(self.store, "get_work_item_decisions"):
            try:
                work_item_decisions = await self.store.get_work_item_decisions(project_id=tasks[0].project_id, limit=50)
            except Exception:
                work_item_decisions = []
        task_by_work_item_id = {
            work_item_id: task
            for work_item_id, task in (await self._task_by_work_item_id(tasks)).items()
            if not bool((task.metadata or {}).get("synthetic_inbox_turn", False))
        }
        changed_work_items: list[DelegationWorkItem] = []
        for task in tasks:
            projection_spec = self._task_effective_projection_spec(task)
            task.metadata = dict(task.metadata)
            task.metadata["adaptive"] = self._build_runtime_coordination_spec(
                projection_spec=projection_spec,
                task=task,
                work_item=None,
                plan=self._active_plan,
                tasks=tasks,
                work_items=work_items,
                handoff_records=handoff_records,
                work_item_decisions=work_item_decisions,
            )
        for work_item in work_items:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            projection_spec = self._task_effective_projection_spec(task) if task is not None else self._work_item_effective_projection_spec(work_item)
            adaptive = self._build_runtime_coordination_spec(
                projection_spec=projection_spec,
                task=task,
                work_item=work_item,
                plan=self._active_plan,
                tasks=tasks,
                work_items=work_items,
                handoff_records=handoff_records,
                work_item_decisions=work_item_decisions,
            )
            metadata = dict(work_item.metadata or {})
            if bool(adaptive.get("manager_release_satisfied", False)):
                metadata["needs_manager_attention"] = False
                if work_item.phase == Phase.NEEDS_ATTENTION and self.store:
                    await self.store.update_delegation_work_item(
                        work_item.work_item_id,
                        phase=Phase.RUNNING,
                    )
                    refreshed = await self.store.get_delegation_work_item(work_item.work_item_id)
                    if refreshed is not None:
                        work_item.phase = refreshed.phase
            if metadata.get("adaptive") != adaptive:
                metadata["adaptive"] = adaptive
                metadata["needs_manager_attention"] = bool(metadata.get("needs_manager_attention", False))
                work_item.metadata = metadata
                work_item.blocked_reason = str(adaptive.get("blocked_reason", "") or "").strip()
                changed_work_items.append(work_item)
            elif work_item.blocked_reason != str(adaptive.get("blocked_reason", "") or "").strip():
                work_item.blocked_reason = str(adaptive.get("blocked_reason", "") or "").strip()
                changed_work_items.append(work_item)
        for work_item in changed_work_items:
            if self.store:
                await self.store.save_delegation_work_item(work_item)
        run_id = str((work_items[0].run_id if work_items else "") or (tasks[0].metadata.get("delegation_run_id", "") if tasks else "") or "").strip()
        if run_id and self.store and hasattr(self.store, "get_delegation_run") and hasattr(self.store, "save_delegation_run"):
            run = await self.store.get_delegation_run(run_id)
            if run is not None:
                run_metadata = dict(run.metadata or {})
                compiled_coordination_spec = {
                    "version": 1,
                    "company_profile": str(getattr(run, "company_profile", "") or ""),
                    "execution_model": str(getattr(run, "execution_model", "") or ""),
                    "tasks": {
                        self._projection_id_for_task(task): dict(
                            self._normalize_adaptive_metadata((task.metadata or {}).get("adaptive", {}))
                        )
                        for task in tasks
                        if self._projection_id_for_task(task)
                    },
                    "work_items": {
                        str(item.work_item_id or "").strip(): dict(
                            self._normalize_adaptive_metadata((item.metadata or {}).get("adaptive", {}))
                        )
                        for item in work_items
                        if str(item.work_item_id or "").strip()
                    },
                }
                if run_metadata.get("coordination_spec") != compiled_coordination_spec:
                    run_metadata["coordination_spec"] = compiled_coordination_spec
                    run.metadata = run_metadata
                    await self.store.save_delegation_run(run)
        return tasks, work_items

    def _is_coordinator_role(self, role_cfg: Any, session: CompanyMemberSession | None = None) -> bool:
        role_id = str(getattr(role_cfg, "id", "") or getattr(session, "role_id", "") or "").strip()
        if role_cfg is None and self.org_engine is not None and role_id:
            agent = self.org_engine.get_agent(role_id)
            if agent is not None and list(getattr(agent, "can_spawn", []) or []):
                return True
        if role_cfg is None:
            return False
        explicit_role_type = str(getattr(role_cfg, "role_type", "") or "").strip().lower()
        if explicit_role_type == "coordinator":
            return True
        can_spawn = [str(item).strip() for item in list(getattr(role_cfg, "can_spawn", []) or []) if str(item).strip()]
        if can_spawn:
            return True
        if session is not None:
            current_work_item = dict(session.current_work_item or {})
            work_kind = str(
                current_work_item.get("kind")
                or current_work_item.get("work_kind")
                or ""
            ).strip().lower()
            work_kind = canonical_work_item_turn_type_for_kind(work_kind, fallback=work_kind)
            if work_kind in {"delegate", "dispatch", "monitor", "aggregate", "synthesize", "deliver"}:
                return True
        return False

    @staticmethod
    def _normalize_follow_up_actions(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        allowed_actions = {"delegate_rereview", "delegate_rework", "delegate_followup"}
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "") or "").strip().lower()
            target_role_id = str(item.get("target_role_id", "") or "").strip()
            if action not in allowed_actions or not target_role_id:
                continue
            normalized.append(
                {
                    "action": action,
                    "target_role_id": target_role_id,
                    "title": str(item.get("title", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                    "reason": str(item.get("reason", "") or "").strip(),
                    "scope_key": str(item.get("scope_key", "") or "").strip(),
                    "dedupe_key": str(item.get("dedupe_key", "") or "").strip(),
                    "depends_on_work_item_ids": [
                        str(dep).strip()
                        for dep in list(item.get("depends_on_work_item_ids", []) or [])
                        if str(dep).strip()
                    ],
                }
            )
        return normalized

    def _apply_work_item_projection_to_task(
        self,
        task: Task,
        work_item: DelegationWorkItem,
    ) -> bool:
        """Project the WorkItem source-of-truth envelope onto its runtime Task.

        Existing runtime Tasks can outlive manager mutations. When the final decider or a
        manager calls ``modify_work_item`` after Stop, the WorkItem title,
        summary, prompt contract, and mutation metadata are updated immediately,
        while the Task row may still contain the pre-Stop prompt. Refresh the
        execution-copy fields before the Task is claimed again so resumed
        external agents receive the revised contract.
        """
        changed = False

        projected_status = task_status_for_phase(work_item.phase)
        if task.status != projected_status:
            task.status = projected_status
            changed = True

        title = str(getattr(work_item, "title", "") or "").strip()
        if title and task.title != title:
            task.title = title
            changed = True

        summary = str(getattr(work_item, "summary", "") or "").strip()
        if summary and task.description != summary:
            task.description = summary
            changed = True

        before_metadata = dict(task.metadata or {})
        task.metadata = dict(before_metadata)
        work_item_metadata = dict(work_item.metadata or {})
        work_kind = str(
            work_item_metadata.get("work_kind")
            or work_item_metadata.get("delegation_turn_kind")
            or work_item.kind
            or ""
        ).strip().lower()
        if work_kind:
            canonical_turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            task.metadata["work_kind"] = work_kind
            task.metadata["delegation_turn_kind"] = work_kind
            task.metadata = mark_projected_work_item_task(
                task.metadata,
                projection_id=self._projection_id_for_task(task),
                turn_type=canonical_turn_type,
            )
            task.metadata[WORK_ITEM_TURN_TYPE_KEY] = canonical_turn_type

        execution_metadata = copy_work_item_execution_metadata(work_item)
        for key in _STALE_REWORK_TASK_METADATA_KEYS:
            if key not in execution_metadata:
                task.metadata.pop(key, None)
        task.metadata.update(execution_metadata)

        dispatch_hold = str(work_item_metadata.get("dispatch_hold", "") or "").strip()
        if dispatch_hold:
            task.metadata["dispatch_hold"] = dispatch_hold
        else:
            for key in _COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS:
                task.metadata.pop(key, None)

        runtime_plan = dict(task.metadata.get("work_item_runtime_plan", {}) or {})
        if runtime_plan:
            projection_id = projection_id_for_work_item(work_item)
            if projection_id:
                runtime_plan["projection_id"] = projection_id
            if work_kind:
                runtime_plan["turn_type"] = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            if summary:
                runtime_plan["summary"] = summary
            task.metadata["work_item_runtime_plan"] = runtime_plan

        task.metadata["derived_work_item_projection"] = {
            "work_item_id": work_item.work_item_id,
            "projection_id": projection_id_for_work_item(work_item),
            **work_item_identity_payload(
                projection_id=projection_id_for_work_item(work_item),
                turn_type="",
            ),
            "kind": work_item.kind,
            "phase": work_item.phase.value,
            "kanban_column": kanban_column(work_item.phase),
            "cell_id": work_item.cell_id,
            "parent_work_item_id": work_item.parent_work_item_id,
        }
        if task.metadata != before_metadata:
            changed = True
        return changed

    async def _sync_task_projection_from_work_items(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> None:
        task_by_work_item_id = {
            work_item_id: task
            for work_item_id, task in task_by_linked_work_item_id(tasks).items()
            if not bool((task.metadata or {}).get("synthetic_inbox_turn", False))
        }
        for work_item in work_items:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            if task is None:
                continue
            changed = self._apply_work_item_projection_to_task(task, work_item)
            if changed and self.store and hasattr(self.store, "save_task"):
                try:
                    await self.store.save_task(task)
                except Exception:
                    logger.opt(exception=True).debug("Best-effort runtime Task projection sync failed")

    async def _refresh_ready_work_items(
        self,
        work_items: list[DelegationWorkItem],
        *,
        tasks: list[Task] | None = None,
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        work_items = await self._reconcile_missing_review_chain(work_items)
        work_item_by_id = {item.work_item_id: item for item in work_items}
        changed = False
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            release_policy = str(metadata.get("release_policy", "auto") or "auto").strip().lower()
            dependency_state = self._dependency_release_state(work_item, work_item_by_id)
            # Auto-release: QUEUED + release_policy=auto → READY (or
            # WAITING_DEPENDENCIES when upstream isn't done).
            if work_item.phase == Phase.QUEUED and release_policy == "auto":
                target = (
                    Phase.WAITING_DEPENDENCIES
                    if dependency_state["dependency_ids"] and not dependency_state["satisfied"]
                    else Phase.READY
                )
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    phase=target,
                    blocked_reason="" if target == Phase.READY else None,
                    metadata_updates=dependency_state["metadata_updates"] or None,
                )
                changed = True
                continue
            # WAITING_DEPENDENCIES → READY when all upstream is approved.
            if work_item.phase == Phase.WAITING_DEPENDENCIES and dependency_state["satisfied"]:
                target_phase = (
                    Phase.READY_FOR_REWORK
                    if str(metadata.get("rework_feedback", "") or "").strip()
                    else Phase.READY
                )
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    phase=target_phase,
                    blocked_reason="",
                    metadata_updates=dependency_state["metadata_updates"] or None,
                )
                changed = True
            elif work_item.phase == Phase.WAITING_DEPENDENCIES and dependency_state["metadata_updates"]:
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    metadata_updates=dependency_state["metadata_updates"],
                )
                changed = True
        # Failure frontier for late-created cards: a delivery/aggregate card
        # created AFTER its dependency already failed never sees a failure
        # transition hook, and the per-item pass above only releases on
        # all-approved. Detection is cheap and idempotent — released cards
        # (stamp present, phase moved) stop matching.
        if has_pending_settlement_release(work_item_by_id):
            run_id = str(work_items[0].run_id or "").strip()
            if run_id:
                try:
                    if await refresh_dependents_for_run(self.store, run_id=run_id):
                        changed = True
                except Exception:
                    logger.opt(exception=True).debug(
                        "Best-effort settlement frontier refresh failed for run "
                        f"{run_id}"
                    )
        if not changed:
            return work_items
        try:
            self._signal_dispatcher_wake()
        except Exception:
            logger.opt(exception=True).debug("Best-effort dispatcher wake after dependency release failed")
        try:
            await self._notify_kanban_changed()
        except Exception:
            logger.opt(exception=True).debug("Best-effort kanban notify after dependency release failed")
        run_id = str(work_items[0].run_id or "").strip()
        return await self.store.list_delegation_work_items(run_id)

    @staticmethod
    def _dependency_release_state(
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> dict[str, Any]:
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        raw_dependency_ids = [
            str(item).strip()
            for item in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
            raw_dependency_ids,
            work_item_by_id,
            owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
        )
        metadata_updates: dict[str, Any] = {}
        if dependency_ids != raw_dependency_ids:
            metadata_updates["dependency_work_item_ids"] = list(dependency_ids)
            metadata_updates["dependency_pruned_at"] = datetime.now().isoformat()
        if pruned_dependency_ids:
            previous_pruned = [
                str(item).strip()
                for item in list(metadata.get("pruned_dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            metadata_updates["pruned_dependency_work_item_ids"] = list(
                dict.fromkeys([*previous_pruned, *pruned_dependency_ids])
            )

        dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
        waiting_on: list[str] = []
        for dep_id in dependency_ids:
            dependency = work_item_by_id.get(dep_id)
            dep_phase = getattr(dependency, "phase", None) if dependency is not None else None
            if not isinstance(dep_phase, Phase):
                try:
                    dep_phase = Phase(str(dep_phase or ""))
                except Exception:
                    dep_phase = None
            dep_class = str(dependency_classes.get(dep_id, "hard") or "hard").strip().lower()
            if dep_class == "info":
                continue
            if dep_class == "soft":
                if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                    waiting_on.append(dep_id)
                continue
            if dep_phase != Phase.APPROVED:
                waiting_on.append(dep_id)

        if waiting_on:
            if list(metadata.get("waiting_on_work_item_ids", []) or []) != waiting_on:
                metadata_updates["waiting_on_work_item_ids"] = waiting_on
        elif list(metadata.get("waiting_on_work_item_ids", []) or []):
            metadata_updates["waiting_on_work_item_ids"] = []

        return {
            "dependency_ids": dependency_ids,
            "metadata_updates": metadata_updates,
            "satisfied": not waiting_on,
            "waiting_on": waiting_on,
        }

    async def _reconcile_missing_review_chain(
        self,
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        """Converge every passive review parent to one report/review chain.

        ``Phase.AWAITING_MANAGER_REVIEW`` is the sole scheduling predicate.
        Auxiliary WorkItems are the durable journal: an active card is
        reused; a completed report whose review write was interrupted is
        consumed directly; otherwise a fresh report attempt is created.
        Runtime Tasks and parent attempt counters are never required.
        """
        if not self.store or not work_items or not hasattr(self.store, "save_delegation_work_item"):
            return work_items
        waiting = [
            item for item in work_items
            if item.phase == Phase.AWAITING_MANAGER_REVIEW
        ]
        if not waiting:
            return work_items
        repaired_ids: list[str] = []
        for parent in waiting:
            target_id = parent.work_item_id
            active_report = self._active_auxiliary_item(
                work_items,
                target_id,
                kind="report",
            )
            if active_report is not None:
                continue

            reports = self._targeting_auxiliary_items(
                work_items,
                target_id,
                kind="report",
            )
            reviews = self._targeting_auxiliary_items(
                work_items,
                target_id,
                kind="review",
            )
            applied_reports = [
                report
                for report in reports
                if report.phase in DONE_PHASES
                and str((report.metadata or {}).get("report_card_outcome", "") or "").strip()
                == "applied"
            ]
            source_report = applied_reports[-1] if applied_reports else None
            linked_reviews = []
            if source_report is not None:
                linked_reviews = [
                    review
                    for review in reviews
                    if str(
                        (review.metadata or {}).get(
                            "review_source_report_work_item_id", ""
                        )
                        or ""
                    ).strip()
                    == source_report.work_item_id
                ]
            latest_linked_review = linked_reviews[-1] if linked_reviews else None
            resolution = dict(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_resolution", {}
                )
                or {}
            )
            resolution_applied_id = str(
                (parent.metadata or {}).get(
                    "review_resolution_applied_work_item_id", ""
                )
                or ""
            ).strip()
            resolution_state = str(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_resolution_state", ""
                )
                or ""
            ).strip()
            if (
                latest_linked_review is not None
                and resolution
                and resolution_state != "stale"
                and resolution_applied_id != latest_linked_review.work_item_id
            ):
                try:
                    applied = await self._apply_review_resolution(
                        latest_linked_review,
                        parent,
                    )
                except Exception:
                    logger.opt(exception=True).warning(
                        "Failed to project durable review resolution for "
                        f"work_item_id={target_id}"
                    )
                    applied = None
                if applied is not None:
                    repaired_ids.append(target_id)
                # The terminal verdict is authoritative. Do not start a new
                # handoff chain until that exact resolution is projected.
                continue

            active_review = self._active_auxiliary_item(
                work_items,
                target_id,
                kind="review",
            )
            active_review_source_id = str(
                (getattr(active_review, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or ""
            ).strip()
            if active_review is not None and (
                source_report is None
                or active_review_source_id == source_report.work_item_id
            ):
                continue
            linked_outcome = str(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_work_item_outcome", ""
                )
                or ""
            ).strip()
            source_needs_review = source_report is not None and (
                latest_linked_review is None
                or linked_outcome == "verdict_parse_failed"
            )
            try:
                if source_needs_review and source_report is not None:
                    source_metadata = dict(source_report.metadata or {})
                    completion_report = str(
                        source_metadata.get("completion_report", "") or ""
                    ).strip()
                    parent_updates = {
                        "completion_report": completion_report,
                        "review_evidence": copy.deepcopy(
                            dict(source_metadata.get("review_evidence", {}) or {})
                        ),
                        "report_completion_raw": str(
                            source_metadata.get("report_completion_raw", "") or ""
                        ),
                    }
                    # Repair the parent projection before making review
                    # runnable. If this write fails, the terminal report
                    # remains the retryable durability point.
                    await self.store.update_delegation_work_item(
                        target_id,
                        metadata_updates=parent_updates,
                    )
                    retry_updates: dict[str, Any] = {}
                    if linked_outcome == "verdict_parse_failed":
                        retry_updates = {
                            "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                            "review_retry_reason": "verdict_parse_failed",
                            "review_retry_of_attempt": self._auxiliary_attempt_number(
                                latest_linked_review,
                                kind="review",
                            ),
                        }
                    spawned = await self._ensure_review_work_item_for_work_item(
                        target_id,
                        completion_report=completion_report,
                        metadata_updates=retry_updates,
                        source_report_item=source_report,
                        run_items=work_items,
                    )
                else:
                    # No unconsumed durable report exists. This is either the
                    # first handoff for the phase or a later rework cycle.
                    spawned = await self._ensure_report_work_item_for_work_item(
                        target_id,
                        run_items=work_items,
                    )
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to reconcile report/review chain for "
                    f"work_item_id={target_id}"
                )
                continue
            if spawned is not None:
                repaired_ids.append(target_id)
        if not repaired_ids:
            return work_items
        logger.info(
            "Reconciled report/review chains for work items: "
            + ", ".join(repaired_ids)
        )
        run_id = str(work_items[0].run_id or "").strip()
        if run_id and hasattr(self.store, "list_delegation_work_items"):
            return await self.store.list_delegation_work_items(run_id)
        return work_items

    async def _reconcile_role_serial_queues(
        self,
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        run_id = str(work_items[0].run_id or "").strip()
        if not run_id:
            return work_items
        result = await reconcile_role_serial_queues(self.store, run_id)
        if (
            result.get("cleared_markers")
            or result.get("pruned_pending_ids")
            or result.get("promoted_work_item_ids")
            or result.get("cleared_focus_session_ids")
        ):
            return await self.store.list_delegation_work_items(run_id)
        return work_items

    async def _promote_manager_work_items_from_inbox(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        session_by_key: dict[str, CompanyMemberSession] = {}
        for session in self.runtime.member_sessions.values():
            seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
            if seat_id:
                session_by_key[seat_id] = session
            if str(session.role_id or "").strip():
                session_by_key.setdefault(str(session.role_id).strip(), session)
        changed = False
        task_by_work_item_id = await self._task_by_work_item_id(tasks)
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            phase = work_item.phase
            manager_session = (
                session_by_key.get(str(work_item.manager_seat_id or "").strip())
                or session_by_key.get(str(work_item.manager_role_id or "").strip())
            )
            if manager_session is not None and phase == Phase.QUEUED:
                matched, matched_msg_id = self._mailbox_release_matched(manager_session, work_item)
                if matched:
                    metadata["mailbox_release_satisfied"] = True
                    metadata["mailbox_release_message_id"] = matched_msg_id
                    metadata["mailbox_release_checked_at"] = datetime.now().isoformat()
                    await self.store.update_delegation_work_item(
                        work_item.work_item_id,
                        phase=Phase.READY,
                        metadata_updates=metadata,
                    )
                    work_item = await self.store.get_delegation_work_item(work_item.work_item_id) or work_item
                    metadata = dict(work_item.metadata or {})
                    phase = work_item.phase
                    changed = True
                    if hasattr(self.store, "save_delegation_event"):
                        try:
                            await self.store.save_delegation_event(
                                DelegationEvent(
                                    run_id=str(work_item.run_id or "").strip(),
                                    work_item_id=work_item.work_item_id,
                                    cell_id=work_item.cell_id,
                                    role_id=work_item.role_id,
                                    event_type="manager_work_item_released_from_mailbox",
                                    payload={
                                        "manager_seat_id": str(work_item.manager_seat_id or "").strip(),
                                        "message_id": matched_msg_id,
                                        "release_policy": str(metadata.get("release_policy", "") or "").strip(),
                                    },
                                )
                            )
                        except Exception:
                            logger.debug("Best-effort mailbox release event persistence failed")
            if phase in DONE_PHASES:
                continue
            work_kind = str(
                metadata.get("work_kind")
                or metadata.get("delegation_turn_kind")
                or work_item.kind
                or ""
            ).strip().lower()
            if work_kind not in {"aggregate", "deliver", "synthesize", "review"}:
                continue
            session = session_by_key.get(str(work_item.seat_id or "").strip()) or session_by_key.get(str(work_item.role_id or "").strip())
            if session is None:
                continue
            fingerprint = self._manager_inbox_fingerprint(session)
            if not fingerprint:
                continue
            last_fingerprint = str(metadata.get("last_ready_from_inbox_fingerprint", "") or "").strip()
            if fingerprint == last_fingerprint:
                continue
            metadata["last_ready_from_inbox_fingerprint"] = fingerprint
            metadata["needs_manager_attention"] = True
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            if task is not None:
                metadata["last_ready_from_checkpoint_basis_hash"] = self._checkpoint_basis_hash(task)
            target_phase = phase
            if phase == Phase.RUNNING:
                target_phase = Phase.NEEDS_ATTENTION
            await self.store.update_delegation_work_item(
                work_item.work_item_id,
                phase=target_phase if target_phase != phase else None,
                metadata_updates=metadata,
            )
            if task is not None and phase in IN_REVIEW_PHASES:
                supersede = getattr(self.store, "supersede_pending_checkpoints", None)
                if callable(supersede):
                    await supersede(
                        project_id=task.project_id or "default",
                        task_id=task.id,
                        checkpoint_types=["company_work_item_gate"],
                    )
            session_status = normalize_role_runtime_status(
                session.status,
                session.focused_work_item_id,
            )
            if session_status != "running":
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                role_session = self.runtime._role_session_for_member_session(session)
                if role_session is not None:
                    role_session.status = "idle"
                    role_session.focused_work_item_id = ""
                    role_session.updated_at = datetime.now()
                    if hasattr(self.store, "save_delegation_role_session"):
                        await self.store.save_delegation_role_session(role_session)
                await self.runtime._persist_session(session, task=task)
            changed = True
            if hasattr(self.store, "save_delegation_event"):
                try:
                    await self.store.save_delegation_event(
                        DelegationEvent(
                            run_id=str(work_item.run_id or "").strip(),
                            work_item_id=work_item.work_item_id,
                            cell_id=work_item.cell_id,
                            role_id=work_item.role_id,
                            event_type="manager_work_item_promoted",
                            payload={
                                "seat_id": work_item.seat_id,
                                "fingerprint": fingerprint,
                                "work_kind": work_kind,
                                "previous_status": status,
                                "needs_manager_attention": True,
                            },
                        )
                    )
                except Exception:
                    logger.debug("Best-effort manager promotion event persistence failed")
        if not changed:
            return work_items
        run_id = str(work_items[0].run_id or "").strip()
        return await self.store.list_delegation_work_items(run_id)

    async def _task_by_work_item_id(self, tasks: list[Task]) -> dict[str, Task]:
        if self.store and hasattr(self.store, "hydrate_task_work_item_links"):
            try:
                await self.store.hydrate_task_work_item_links(tasks)
            except Exception:
                logger.opt(exception=True).debug("_task_by_work_item_id: link hydration failed")
        return task_by_linked_work_item_id(tasks)

    async def _work_item_id_for_task(self, task: Task | None) -> str:
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id or task is None or not self.store:
            return work_item_id
        get_work_item_for_task = getattr(self.store, "get_work_item_for_runtime_task", None)
        if not callable(get_work_item_for_task):
            return ""
        try:
            item = await get_work_item_for_task(task.id)
        except Exception:
            logger.opt(exception=True).debug("_work_item_id_for_task: link lookup failed")
            return ""
        work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
        if work_item_id:
            set_linked_work_item_id(task, work_item_id)
        return work_item_id

    @staticmethod
    def _fatal_runtime_projection_issues(
        task: Task,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem] | None = None,
    ) -> list[WorkItemRuntimeInvariantIssue]:
        return [
            issue
            for issue in validate_work_item_runtime_projection(
                task,
                work_item,
                work_item_by_id=work_item_by_id,
            )
            if issue.severity == "error"
        ]

    @classmethod
    def _raise_for_runtime_projection_issues(
        cls,
        task: Task,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem] | None = None,
    ) -> None:
        issues = cls._fatal_runtime_projection_issues(task, work_item, work_item_by_id)
        if not issues:
            return
        raise RuntimeError(
            "work-item runtime invariant failed before dispatch: "
            + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        )

    async def _diagnose_work_item_runtime_projection_issues(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[WorkItemRuntimeInvariantIssue]:
        issues = diagnose_work_item_runtime_projections(tasks, work_items)
        if not issues:
            return []
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        for issue in issues:
            key = issue.fingerprint()
            if key in self._runtime_invariant_issue_keys:
                continue
            self._runtime_invariant_issue_keys.add(key)
            logger.warning(
                "work-item runtime invariant violation: "
                f"code={issue.code} severity={issue.severity} "
                f"run_id={issue.run_id} work_item={issue.work_item_id} "
                f"task={issue.runtime_task_id} projection={issue.projection_id} "
                f"message={issue.message}"
            )
            if not self.store or not hasattr(self.store, "save_delegation_event"):
                continue
            work_item = work_item_by_id.get(issue.work_item_id)
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=issue.run_id or str(getattr(work_item, "run_id", "") or "").strip(),
                        work_item_id=issue.work_item_id or None,
                        cell_id=str(getattr(work_item, "cell_id", "") or "").strip() or None,
                        role_id=str(getattr(work_item, "role_id", "") or "").strip() or None,
                        event_type=WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
                        payload=issue.to_event_payload(),
                    )
                )
            except Exception:
                logger.opt(exception=True).debug("Best-effort runtime invariant event persistence failed")
        return issues

    async def _record_work_item_runtime_diagnostic(
        self,
        *,
        code: str,
        severity: str,
        work_item: DelegationWorkItem | None = None,
        task: Task | None = None,
        message: str,
        details: dict[str, Any] | None = None,
        warn: bool = True,
    ) -> None:
        issue = WorkItemRuntimeInvariantIssue(
            code=code,
            severity=severity,
            run_id=str(getattr(work_item, "run_id", "") or (getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip(),
            work_item_id=str(getattr(work_item, "work_item_id", "") or linked_work_item_id_for_task(task) or "").strip(),
            runtime_task_id=str(getattr(task, "id", "") or "").strip(),
            projection_id=(
                projection_id_for_work_item(work_item)
                if work_item is not None
                else projection_id_for_task(task)
            ),
            message=message,
            details=dict(details or {}),
        )
        key = issue.fingerprint()
        if key in self._runtime_invariant_issue_keys:
            return
        self._runtime_invariant_issue_keys.add(key)
        if warn:
            logger.warning(
                "work-item runtime diagnostic: "
                f"code={issue.code} severity={issue.severity} "
                f"run_id={issue.run_id} work_item={issue.work_item_id} "
                f"task={issue.runtime_task_id} projection={issue.projection_id} "
                f"message={issue.message}"
            )
        if not self.store or not hasattr(self.store, "save_delegation_event"):
            return
        try:
            await self.store.save_delegation_event(
                DelegationEvent(
                    run_id=issue.run_id,
                    work_item_id=issue.work_item_id or None,
                    cell_id=str(getattr(work_item, "cell_id", "") or "").strip() or None,
                    role_id=str(getattr(work_item, "role_id", "") or getattr(task, "assigned_to", "") or "").strip() or None,
                    event_type=WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
                    payload=issue.to_event_payload(),
                )
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort runtime diagnostic event persistence failed")

    async def _materialize_work_item_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[Task]:
        """Materialize company WorkItems into runtime Task envelopes.

        In company mode, DelegationWorkItem is the business unit. The Task
        records created here are execution projections for existing
        agent/tool/session APIs. The WorkItem -> Task relation is owned by
        the structured runtime link table and should not be treated as a
        second company-mode business identity.
        """
        if not self.store or not work_items:
            return tasks
        existing_tasks = list(tasks)
        if hasattr(self.store, "hydrate_task_work_item_links"):
            await self.store.hydrate_task_work_item_links(existing_tasks)
        existing_task_ids = {str(task.id or "").strip() for task in existing_tasks if str(task.id or "").strip()}
        existing_work_item_ids = set(task_by_linked_work_item_id(existing_tasks))
        root_task = sorted(existing_tasks, key=lambda item: (item.created_at, item.id))[0]
        runtime_topology = dict((root_task.metadata or {}).get("runtime_topology", {}) or {})
        root_parent_session_id = str(
            root_task.parent_session_id
            or root_task.session_id
            or (root_task.metadata or {}).get("parent_session_id", "")
            or "company-session"
        ).strip() or "company-session"
        target_output_dir = str((root_task.metadata or {}).get("target_output_dir", "") or "").strip()
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        newly_materialized_tasks: list[Task] = []
        for work_item in work_items:
            work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
            if not work_item_id or work_item_id in existing_work_item_ids:
                continue
            phase = work_item.phase
            metadata = dict(work_item.metadata or {})
            review_execution_work_item = is_review_execution_work_item_metadata(metadata)
            report_execution_work_item = is_report_execution_work_item_metadata(metadata)
            if (
                should_hide_work_item_from_company_kanban(metadata)
                and not review_execution_work_item
                and not report_execution_work_item
            ):
                continue
            if phase in DONE_PHASES:
                continue
            persisted = None
            get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
            if callable(get_runtime_task):
                persisted = await get_runtime_task(work_item_id)
            if persisted is not None:
                set_linked_work_item_id(persisted, work_item_id)
                self._raise_for_runtime_projection_issues(persisted, work_item, work_item_by_id)
                if persisted.id not in existing_task_ids:
                    existing_tasks.append(persisted)
                    existing_task_ids.add(persisted.id)
                    existing_work_item_ids.add(work_item_id)
                continue
            seat_id = str(getattr(work_item, "seat_id", "") or dict(getattr(work_item, "metadata", {}) or {}).get("seat_id", "") or "").strip()
            topology_seat = next(
                (
                    dict(seat)
                    for seat in list(runtime_topology.get("seats", []) or [])
                    if str(seat.get("seat_id", "") or "").strip() == seat_id
                ),
                {},
            )
            work_kind = str(
                dict(getattr(work_item, "metadata", {}) or {}).get("work_kind", "")
                or getattr(work_item, "kind", "")
                or "execute"
            ).strip().lower() or "execute"
            projection_id = projection_id_for_work_item(work_item)
            session_id = f"{root_parent_session_id}:{work_item_id}"
            employee_assignment = dict(topology_seat.get("employee_assignment", {}) or {})
            preferred_external_agent = str(topology_seat.get("preferred_external_agent", "") or "").strip() or None
            selected_execution_agent, assigned_external_agent, role_force_native_execution = (
                resolve_effective_execution_agent(
                    topology_seat.get("selected_execution_agent"),
                    preferred_external_agent,
                    force_native_execution=bool(topology_seat.get("force_native_execution", False)),
                )
            )
            resolved_force_native_execution = bool((root_task.metadata or {}).get("force_native_execution", False) or role_force_native_execution)
            if resolved_force_native_execution:
                selected_execution_agent = "native"
                assigned_external_agent = None
            preferred_external_agent = assigned_external_agent
            turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            current_turn_mode = self._initial_current_turn_mode_for_work_item(
                turn_type,
                topology_seat,
                review_execution_work_item=review_execution_work_item,
                report_execution_work_item=report_execution_work_item,
            )
            work_item.metadata = mark_work_item_projection(
                dict(work_item.metadata or {}),
                projection_id=projection_id,
                turn_type=turn_type,
            )
            if current_turn_mode and not str(work_item.metadata.get("current_turn_mode", "") or "").strip():
                work_item.metadata["current_turn_mode"] = current_turn_mode
            self._ensure_prompt_contract_on_work_item(
                work_item,
                task_metadata=dict(root_task.metadata or {}),
                task_description=str(getattr(work_item, "summary", "") or root_task.metadata.get("original_message", "") or "").strip(),
            )
            if employee_assignment:
                work_item.metadata["employee_assignment"] = copy.deepcopy(employee_assignment)
            prompt_ctx = str((employee_assignment or {}).get("prompt_context", "") or "").strip()
            if prompt_ctx:
                work_item.metadata["employee_prompt_context"] = prompt_ctx
            delta_ctx = str((employee_assignment or {}).get("delta_context", "") or "").strip()
            if delta_ctx:
                work_item.metadata["employee_delta_context"] = delta_ctx
            owner_execution_copy = build_work_item_owner_execution_copy(work_item)
            owner_execution_copy.setdefault(
                "delegation_role_session_id",
                canonical_role_session_id(
                    run_id=str(getattr(work_item, "run_id", "") or "").strip(),
                    role_id=str(getattr(work_item, "role_id", "") or "").strip(),
                    team_instance_id=str(getattr(work_item, "team_instance_id", "") or "").strip(),
                ),
            )
            owner_execution_copy["work_kind"] = turn_type
            task_metadata = mark_work_item_projection(mark_work_item_runtime({
                "mode": "company",
                "execution_mode": str((root_task.metadata or {}).get("execution_mode", "") or "company_mode").strip(),
                "execution_model": str((root_task.metadata or {}).get("execution_model", "") or "multi_team_org").strip(),
                "runtime_model": str((root_task.metadata or {}).get("runtime_model", "") or "multi_team_org").strip(),
                "original_message": str((root_task.metadata or {}).get("original_message", "") or "").strip(),
                "company_profile": str((root_task.metadata or {}).get("company_profile", "") or "").strip(),
                "delegation_playbook": dict((root_task.metadata or {}).get("delegation_playbook", {}) or {}),
                "runtime_topology": copy.deepcopy(runtime_topology),
                **owner_execution_copy,
                "seat_manager_role_id": str(topology_seat.get("manager_role_id", "") or getattr(work_item, "manager_role_id", "") or "").strip(),
                "manager_role_id": str(topology_seat.get("manager_role_id", "") or getattr(work_item, "manager_role_id", "") or "").strip(),
                "manager_seat_id": str(topology_seat.get("manager_seat_id", "") or getattr(work_item, "manager_seat_id", "") or "").strip(),
                "managed_team_id": str(topology_seat.get("managed_team_id", "") or "").strip(),
                "seat_contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                "force_native_execution": resolved_force_native_execution,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": bool(topology_seat.get("execution_agent_locked", False)),
                "selected_execution_agent_source": (
                    str(topology_seat.get("selected_execution_agent_source", "") or "").strip()
                    or (
                        "recruitment_user_override"
                        if bool(topology_seat.get("execution_agent_locked", False))
                        else ""
                    )
                ),
                "work_item_execution_strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if resolved_force_native_execution
                    else WorkItemExecutionStrategy.EXTERNAL.value
                    if assigned_external_agent
                    else WorkItemExecutionStrategy.AUTO.value
                ),
                "adaptive": copy.deepcopy(dict((getattr(work_item, "metadata", {}) or {}).get("adaptive", {}) or {})),
                "execution_task_ids": [work_item_id],
                "parent_session_id": root_parent_session_id,
                "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
                "target_output_dir": target_output_dir,
                "output_root": target_output_dir,
                "workspace_root": str((root_task.metadata or {}).get("workspace_root", "") or "").strip(),
                "comms_workspace_root": str((root_task.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_root": str((root_task.metadata or {}).get("comms_root", "") or "").strip(),
                "user_visible": bool(dict(getattr(work_item, "metadata", {}) or {}).get("user_visible", False)),
                "authoritative_output": bool(dict(getattr(work_item, "metadata", {}) or {}).get("authoritative_output", False)),
                "review_task": review_execution_work_item,
                "review_execution_work_item": review_execution_work_item,
                "report_execution_work_item": report_execution_work_item,
                "skip_work_item_sync": review_execution_work_item or report_execution_work_item,
            }, version=work_item_runtime_version(root_task.metadata)),
                projection_id=projection_id,
                turn_type=turn_type,
            )
            task_metadata.update(copy_work_item_execution_metadata(work_item))
            task_metadata.update(owner_execution_copy)
            task_metadata[WORK_ITEM_TURN_TYPE_KEY] = turn_type
            temp_task = Task(
                id=str(uuid.uuid4()),
                title=str(getattr(work_item, "title", "") or projection_id or "Runtime Work Item").strip(),
                description=str(getattr(work_item, "summary", "") or root_task.metadata.get("original_message", "") or "").strip(),
                assigned_to=str(getattr(work_item, "role_id", "") or "").strip(),
                status=task_status_for_phase(phase),
                project_id=root_task.project_id,
                session_id=session_id,
                parent_session_id=root_parent_session_id,
                assigned_external_agent=assigned_external_agent,
                metadata=task_metadata,
            )
            dependency_projection_ids: list[str] = []
            projection_spec = self._projection_spec_for_task(temp_task)
            if projection_spec is not None:
                dependency_projection_ids = [
                    str(item).strip()
                    for item in list(projection_spec.dependency_projection_ids or [])
                    if str(item).strip()
                ]
            if not dependency_projection_ids:
                dependency_projection_ids = [
                    str(projection_id_for_work_item(work_item_by_id.get(dep_id)) if work_item_by_id.get(dep_id) is not None else dep_id).strip()
                    for dep_id in [
                        str(item).strip()
                        for item in list(dict(getattr(work_item, "metadata", {}) or {}).get("dependency_work_item_ids", []) or [])
                        if str(item).strip()
                    ]
                    if str(projection_id_for_work_item(work_item_by_id.get(dep_id)) if work_item_by_id.get(dep_id) is not None else dep_id).strip()
                ]
            task = Task(
                id=temp_task.id,
                title=temp_task.title,
                description=temp_task.description,
                assigned_to=temp_task.assigned_to,
                status=temp_task.status,
                project_id=temp_task.project_id,
                session_id=temp_task.session_id,
                parent_session_id=temp_task.parent_session_id,
                assigned_external_agent=temp_task.assigned_external_agent,
                dependencies=dependency_projection_ids,
                metadata=task_metadata,
            )
            set_linked_work_item_id(task, work_item_id)
            await self.store.save_delegation_work_item(work_item)
            ensure_runtime_task = getattr(self.store, "ensure_runtime_task_for_work_item", None)
            if callable(ensure_runtime_task):
                task = await ensure_runtime_task(work_item, lambda task=task: task)
            else:
                await self.store.save_task(task)
                link_runtime_task = getattr(self.store, "link_work_item_runtime_task", None)
                if callable(link_runtime_task):
                    linked = await link_runtime_task(work_item_id, task.id)
                    if not linked:
                        raise RuntimeError(
                            "failed to link new runtime Task "
                            f"{task.id} for WorkItem {work_item_id}"
                        )
            set_linked_work_item_id(task, work_item_id)
            self._raise_for_runtime_projection_issues(task, work_item, work_item_by_id)
            if self.memory is not None and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="child",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "work_item_id": work_item_id,
                        "role_id": str(getattr(work_item, "role_id", "") or "").strip(),
                        "seat_id": seat_id,
                        "origin_session_id": task.parent_session_id,
                    },
                )
            if task.id not in existing_task_ids:
                existing_tasks.append(task)
                existing_task_ids.add(task.id)
                newly_materialized_tasks.append(task)
            else:
                for existing_task in existing_tasks:
                    if str(getattr(existing_task, "id", "") or "").strip() == task.id:
                        set_linked_work_item_id(existing_task, work_item_id)
                        break
            existing_work_item_ids.add(work_item_id)
        if newly_materialized_tasks:
            task_by_projection_id: dict[str, Task] = {}
            for task in existing_tasks:
                task_by_projection_id[task.id] = task
                task_by_projection_id[self._projection_id_for_task(task)] = task
            for task in newly_materialized_tasks:
                if not list(task.dependencies or []):
                    continue
                await self._record_handoffs(task, task_by_projection_id)
                await self.save_task(task)
        return existing_tasks

    @staticmethod
    def _runtime_work_kind_to_work_item_turn_type(work_kind: str) -> str:
        return canonical_work_item_turn_type_for_kind(work_kind)

    @staticmethod
    def _initial_current_turn_mode_for_work_item(
        turn_type: str,
        topology_seat: dict[str, Any] | None,
        *,
        review_execution_work_item: bool = False,
        report_execution_work_item: bool = False,
    ) -> str:
        normalized_turn = canonical_work_item_turn_type_for_kind(turn_type)
        if normalized_turn == "deliver":
            return "deliver_required"
        if normalized_turn == "aggregate":
            return "synthesize_required"
        if report_execution_work_item or normalized_turn == "report":
            return "report_required"
        if review_execution_work_item or normalized_turn == "review":
            return "review_execute"
        seat = dict(topology_seat or {})
        direct_reports = list(seat.get("direct_report_seat_ids", []) or [])
        allowed_delegates = list(seat.get("allowed_delegate_role_ids", []) or [])
        managed_team_id = str(seat.get("managed_team_id", "") or "").strip()
        if direct_reports or allowed_delegates or managed_team_id:
            return "dispatch_required"
        return "worker_execute"

    @staticmethod
    def _driver_ownership_task(
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> Task | None:
        preferred = {
            str(task_id or "").strip()
            for task_id in set(preferred_task_ids or set())
            if str(task_id or "").strip()
        }
        candidates = [
            task
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
            and (not preferred or task.id in preferred)
        ]
        for task in candidates:
            if linked_work_item_id_for_task(task):
                return task
        for task in candidates:
            if is_work_item_runtime_metadata(dict(task.metadata or {})):
                return task
        return candidates[0] if candidates else None

    def acquire_driver_ownership(
        self,
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> CompanyExecutorDriverOwnership | None:
        registry = self.active_task_run_registry
        task = self._driver_ownership_task(
            tasks,
            preferred_task_ids=preferred_task_ids,
        )
        if registry is None or task is None:
            return None
        project_id = str(task.project_id or "default").strip() or "default"
        try:
            attempt_token = registry.register(project_id, task.id)
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        return CompanyExecutorDriverOwnership(
            registry=registry,
            project_id=project_id,
            task_id=task.id,
            attempt_token=attempt_token,
        )

    async def execute(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        ownership = self.acquire_driver_ownership(tasks)
        try:
            plan = _coerce_company_work_item_runtime_plan(plan) or CompanyWorkItemRuntimePlan()
            plan.metadata = {
                **dict(plan.metadata or {}),
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
            }
            if ownership is None:
                return await self._execute_multi_team_org(plan, tasks)
            with ownership.bind():
                return await self._execute_multi_team_org(plan, tasks)
        finally:
            if ownership is not None:
                ownership.release()

    async def _execute_multi_team_org(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        run_token = self._use_run_state(
            CompanyExecutorRunState(active_plan=plan, active_tasks=list(tasks))
        )
        runtime_token = self.runtime.use_state(self.runtime.create_state())
        try:
            return await self._execute_multi_team_org_scoped(plan, tasks)
        finally:
            self.runtime.reset_state(runtime_token)
            self._reset_run_state(run_token)

    async def _execute_multi_team_org_scoped(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        self._active_plan = plan
        self._active_tasks = tasks
        await self.runtime.bootstrap(tasks)
        self._stall_counter = 0
        # Continuous-dispatch loop (kanban-push).  Previously this method
        # ran claim → asyncio.gather(ALL work items) → iterate, which meant
        # children created mid-turn by a leader could not be picked up
        # until every sibling in the current batch also finished — often
        # 30-60s of dead time.  We now keep work items running as
        # ``asyncio.Task`` instances in ``active_work_item_tasks`` and wake
        # the loop on whichever happens first: a work item completing, a
        # delegation tool firing ``_dispatcher_wake``, or a short polling
        # tick for DB-driven state (external agents, scheduled timers).
        active_work_item_tasks: dict[
            asyncio.Task[TaskResult | None],
            tuple[CompanyMemberSession, Task],
        ] = {}
        # Start with the wake event cleared so the first iteration always
        # performs a full load/claim pass.
        self._dispatcher_wake.clear()
        poll_timeout_sec = 0.5
        active_work_poll_timeout_sec = 5.0
        try:
            while True:
                if self.store:
                    project_id = str(tasks[0].project_id or "default").strip() if tasks else "default"
                    parent_session_id = str(
                        getattr(tasks[0], "parent_session_id", "") or (tasks[0].metadata or {}).get("parent_session_id", "") or ""
                    ).strip() if tasks else ""
                    all_tasks = await self.store.get_tasks(project_id=project_id)
                    tasks = [
                        task
                        for task in all_tasks
                        if str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
                        == str((self._active_tasks[0].metadata or {}).get("delegation_run_id", "") or "").strip()
                        and (
                            str(getattr(task, "parent_session_id", "") or "").strip() == parent_session_id
                            or str(getattr(task, "session_id", "") or "").strip() == parent_session_id
                        )
                    ] or list(self._active_tasks)
                self._active_tasks = tasks
                # Consumer half of `_park_for_blocking_comms`: blocking
                # replies arrive as durable inbox files, so each tick checks
                # parked tasks and releases the ones whose replies are all
                # present. In-flight tasks are skipped — their coroutine
                # still owns the Task object and a late save_task would
                # clobber the transition.
                in_flight_task_ids = {
                    claimed.id for _member, claimed in active_work_item_tasks.values()
                }
                for parked in tasks:
                    if (
                        parked.status == TaskStatus.AWAITING_PEER
                        and parked.id not in in_flight_task_ids
                    ):
                        await self._try_unpark_blocking_comms(parked)
                await self.runtime.refresh_inbox_state(tasks)
                work_items = await self._load_delegation_work_items(tasks)
                work_items = await self._reconcile_attempt_ledger(
                    work_items,
                    active_work_item_tasks,
                )
                work_items = await self._refresh_ready_work_items(work_items, tasks=tasks)
                tasks = await self._materialize_work_item_tasks(tasks, work_items)
                self._active_tasks = tasks
                work_items = await self._load_delegation_work_items(tasks)
                tasks, work_items = await self._queue_multi_team_response_tasks(tasks, work_items)
                work_items = await self._refresh_ready_work_items(work_items, tasks=tasks)
                work_items = await self._reconcile_role_serial_queues(work_items)
                tasks = await self._materialize_work_item_tasks(tasks, work_items)
                sync_result = self._sync_task_projection_from_work_items(tasks, work_items)
                if inspect.isawaitable(sync_result):
                    await sync_result
                await self._diagnose_work_item_runtime_projection_issues(tasks, work_items)
                work_item_by_id = {item.work_item_id: item for item in work_items}
                task_by_work_item_id = await self._task_by_work_item_id(tasks)
                runnable_work_items = [
                    item for item in work_items
                    if self._work_item_is_runnable(item, work_item_by_id, task_by_work_item_id)
                ]
                self.runtime.enqueue_runnable_work_items(
                    runnable_work_items,
                    task_by_work_item_id=task_by_work_item_id,
                )
                plain_runnable_tasks = [
                    task
                    for task in tasks
                    if (
                        not linked_work_item_id_for_task(task)
                        and task.status == TaskStatus.PENDING
                    )
                ]
                self.runtime.enqueue_runnable_tasks(plain_runnable_tasks)
                runnable_work_item_ids = {item.work_item_id for item in runnable_work_items}
                runnable = [
                    task for task in tasks
                    if (
                        linked_work_item_id_for_task(task) in runnable_work_item_ids
                        or (
                            not linked_work_item_id_for_task(task)
                            and task.status == TaskStatus.PENDING
                        )
                    )
                ]
                active_tasks = [
                    task for task in tasks
                    if task.status in {TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.AWAITING_MANAGER_REVIEW, TaskStatus.AWAITING_HUMAN, TaskStatus.AWAITING_REVIEW}
                ]
                # Phase B rehydrate: unpark any in-memory CompanyMemberSession
                # whose focused work_item has been made dispatchable by a
                # prior write (children-done wake, rework bounce, etc.).
                # Replaces sync_member_session_hook + reenqueue callbacks
                # with a single idempotent in-memory pass driven by DB truth.
                self._rehydrate_parked_member_sessions(work_items)
                # Claim whatever is immediately claimable and spawn each
                # work item as an independent asyncio.Task so the loop no
                # longer blocks on the slowest sibling.
                claims = await self._claim_and_create_work_item_tasks(
                    tasks,
                    work_items,
                    active_work_item_tasks,
                )
                # Termination: only when nothing is in-flight AND nothing
                # else is runnable.  If work items are still running, even an
                # "empty runnable" snapshot may become non-empty within
                # milliseconds (leader delegating children), so we defer
                # the break until the live set is drained.
                if not active_work_item_tasks:
                    if not active_tasks:
                        break
                    if not runnable and not claims:
                        human_waiting = [
                            t for t in active_tasks
                            if t.status in {TaskStatus.AWAITING_HUMAN, TaskStatus.AWAITING_MANAGER_REVIEW, TaskStatus.AWAITING_REVIEW}
                        ]
                        if human_waiting:
                            # Convergent exit: nothing is in flight, nothing is
                            # claimable, and every remaining active task waits on
                            # a human.  The wait is resolved through a separate
                            # engine turn (checkpoint reply → phase ready →
                            # re-dispatch), never inside this loop, so polling
                            # here can only spin forever while the caller's turn
                            # hangs and its claims block the resuming turn.
                            pending_task_ids = await self._pending_checkpoint_task_ids(
                                str(human_waiting[0].project_id or "default")
                            )
                            unparked = [t for t in human_waiting if t.id not in pending_task_ids]
                            self._stall_counter += 1
                            if not unparked or self._stall_counter >= _HUMAN_WAIT_MAX_STALL_TICKS:
                                if unparked:
                                    logger.warning(
                                        "_execute_multi_team_org: exiting after {} stalled ticks with {} "
                                        "human-waiting task(s) lacking a pending checkpoint: {}",
                                        self._stall_counter,
                                        len(unparked),
                                        [t.id for t in unparked],
                                    )
                                summary = self._summarize_human_parked_exit(tasks, human_waiting)
                                await self._emit_progress(
                                    "[Company] runtime turn parked: "
                                    f"{len(human_waiting)} work item(s) awaiting human input; "
                                    "answer the pending approval/review card(s) to continue."
                                )
                                return summary
                            await asyncio.sleep(5)
                            continue
                        for task in active_tasks:
                            if task.status == TaskStatus.AWAITING_PEER:
                                await self._save_peer_checkpoint(task)
                        break
                    self._stall_counter = 0
                else:
                    self._stall_counter = 0
                # Wait on: (a) any active work item completing, (b) a
                # dispatcher wake signaled by a delegation tool, or (c) a
                # short poll tick for external/DB-driven state changes.
                wake_waiter: asyncio.Task[bool] | None = None
                wait_futures: set[asyncio.Future[Any]] = set(active_work_item_tasks.keys())
                if not self._dispatcher_wake.is_set():
                    wake_waiter = asyncio.create_task(self._dispatcher_wake.wait())
                    wait_futures.add(wake_waiter)
                if wait_futures:
                    wait_timeout = (
                        active_work_poll_timeout_sec
                        if active_work_item_tasks
                        else poll_timeout_sec
                    )
                    try:
                        await asyncio.wait(
                            wait_futures,
                            return_when=asyncio.FIRST_COMPLETED,
                            timeout=wait_timeout,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            "_execute_multi_team_org: asyncio.wait raised; continuing"
                        )
                # Consume the wake signal (if any) — setting it again
                # during the next iteration is harmless and idempotent.
                self._dispatcher_wake.clear()
                if wake_waiter is not None and not wake_waiter.done():
                    wake_waiter.cancel()
                    try:
                        await wake_waiter
                    except (asyncio.CancelledError, Exception):
                        pass
                # Harvest any work items that finished during this tick.
                for completed in [t for t in list(active_work_item_tasks.keys()) if t.done()]:
                    session_task = active_work_item_tasks.pop(completed, None)
                    if session_task is None:
                        continue
                    claimed_member_session, claimed_task = session_task
                    exc = completed.exception()
                    if exc is not None:
                        await self._handle_claimed_work_item_exception(
                            claimed_member_session,
                            claimed_task,
                            exc,
                        )
                # Debounced UI push — fire-and-forget so the hot path
                # never awaits `build_collab_sync`.
                self._schedule_kanban_notification()
        except asyncio.CancelledError:
            claimed_pairs = list(active_work_item_tasks.values())
            # Coroutines that already died on a real exception must settle
            # their attempt BEFORE this turn unwinds. The old gather(...,
            # return_exceptions=True) below silently discarded them, leaving
            # the card RUNNING with an open attempt — the exact gap that let
            # a deterministic crash replay forever across suspend/resume
            # cycles (issue #10). Cancellation of live coroutines is still a
            # legitimate suspend; only genuine crashes are harvested here.
            crashed_pairs: list[tuple[CompanyMemberSession, Task, BaseException]] = []
            for work_item_task, session_task in list(active_work_item_tasks.items()):
                if not work_item_task.done() or work_item_task.cancelled():
                    continue
                crash_exc = work_item_task.exception()
                if crash_exc is None or isinstance(crash_exc, asyncio.CancelledError):
                    continue
                crashed_member_session, crashed_task = session_task
                crashed_pairs.append((crashed_member_session, crashed_task, crash_exc))
            for crashed_member_session, crashed_task, crash_exc in crashed_pairs:
                try:
                    await asyncio.shield(
                        self._handle_claimed_work_item_exception(
                            crashed_member_session,
                            crashed_task,
                            crash_exc,
                        )
                    )
                except asyncio.CancelledError:
                    logger.warning(
                        "company runtime cancellation: crashed work item settlement "
                        "for task={} continues shielded in background",
                        crashed_task.id,
                    )
                except Exception:
                    logger.opt(exception=True).error(
                        "company runtime cancellation: failed to settle crashed work item task={}",
                        crashed_task.id,
                    )
            for work_item_task in list(active_work_item_tasks.keys()):
                if not work_item_task.done():
                    work_item_task.cancel()
            if active_work_item_tasks:
                await asyncio.gather(
                    *list(active_work_item_tasks.keys()),
                    return_exceptions=True,
                )
                active_work_item_tasks.clear()
            store_ready = self._store_is_ready(self.store)
            update_role_session = getattr(self.store, "update_delegation_role_session", None)
            for member_session, claimed_task in claimed_pairs:
                try:
                    self.runtime._claimed_task_ids.discard(claimed_task.id)
                    work_item_id = linked_work_item_id_for_task(claimed_task)
                    if work_item_id:
                        self.runtime._claimed_work_item_ids.discard(work_item_id)
                    member_session.status = "idle"
                    member_session.resident_status = "idle"
                    member_session.current_task_id = ""
                    member_session.focused_work_item_id = ""
                    member_session.current_work_item = {}
                    member_session.current_assignment = {}
                    member_session.updated_at = datetime.now()
                    role_session = self.runtime._role_session_for_member_session(member_session)
                    if role_session is not None:
                        role_session.status = "idle"
                        role_session.focused_work_item_id = ""
                        role_session.current_work_item = {}
                        role_session.updated_at = datetime.now()
                    role_session_id = str(
                        getattr(role_session, "role_session_id", "")
                        or (claimed_task.metadata or {}).get("delegation_role_session_id", "")
                        or ""
                    ).strip()
                    if role_session_id and callable(update_role_session) and store_ready:
                        await update_role_session(
                            role_session_id,
                            focused_work_item_id="",
                            current_work_item={},
                            status="idle",
                            metadata_updates={
                                "last_suspend_memory_reset_at": datetime.now().isoformat(),
                                "last_suspend_task_id": claimed_task.id,
                            },
                        )
                except Exception:
                    logger.opt(exception=True).debug("company runtime cancellation: failed session idle reset")
            raise
        finally:
            # Drain any work items still running (shouldn't happen given the
            # termination invariants above, but keep the runtime honest
            # if an unexpected break-point is hit).
            if active_work_item_tasks:
                drain_results = await asyncio.gather(
                    *list(active_work_item_tasks.keys()),
                    return_exceptions=True,
                )
                for (completed_task, (drained_session, drained_task)), res in zip(
                    list(active_work_item_tasks.items()), drain_results
                ):
                    if isinstance(res, Exception):
                        await self._handle_claimed_work_item_exception(
                            drained_session,
                            drained_task,
                            res,
                        )
                active_work_item_tasks.clear()
        return self._summarize_multi_team_org_results(tasks)

    @staticmethod
    def _runtime_scope_for_tasks(tasks: list[Task]) -> tuple[str, str]:
        project_id = "default"
        runtime_session_id = ""
        for task in tasks:
            project_id = str(task.project_id or project_id).strip() or "default"
            metadata = dict(task.metadata or {})
            runtime_session_id = str(
                getattr(task, "parent_session_id", "")
                or metadata.get("company_runtime_root_session_id")
                or metadata.get("parent_session_id")
                or ""
            ).strip()
            if runtime_session_id:
                return project_id, runtime_session_id
        for task in tasks:
            runtime_session_id = str(getattr(task, "session_id", "") or "").strip()
            if runtime_session_id:
                return project_id, runtime_session_id
        return project_id, ""

    async def _claim_and_create_work_item_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        active_work_item_tasks: dict[
            asyncio.Task[TaskResult | None],
            tuple[CompanyMemberSession, Task],
        ],
    ) -> list[tuple[CompanyMemberSession, Task]]:
        """Keep durable claim and coroutine ownership in one scope boundary."""

        async def claim_and_create() -> list[tuple[CompanyMemberSession, Task]]:
            claims = await self.runtime.claim_runnable_tasks(
                tasks,
                work_items=work_items,
            )
            for member_session, claimed_task in claims:
                work_item_task = self._create_claimed_work_item_task(
                    member_session,
                    claimed_task,
                    {},
                )
                active_work_item_tasks[work_item_task] = (
                    member_session,
                    claimed_task,
                )
            return claims

        registry = self.active_task_run_registry
        project_id, runtime_session_id = self._runtime_scope_for_tasks(tasks)
        if registry is None or not runtime_session_id:
            return await claim_and_create()
        async with registry.scope_lock(project_id, runtime_session_id):
            return await claim_and_create()

    def _create_claimed_work_item_task(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> asyncio.Task[TaskResult | None]:
        """Register ownership before scheduling the full claimed-item coroutine."""

        registry = self.active_task_run_registry
        project_id = str(task.project_id or "default").strip() or "default"
        attempt_token = ""
        if registry is not None:
            try:
                attempt_token = registry.register(project_id, task.id)
            except ActiveTaskRunAdmissionClosed as exc:
                raise asyncio.CancelledError(str(exc)) from exc

        async def run_owned() -> TaskResult | None:
            try:
                return await self._run_claimed_work_item(
                    member_session,
                    task,
                    task_by_projection_id,
                )
            finally:
                if registry is not None and attempt_token:
                    registry.unregister(project_id, task.id, attempt_token)

        try:
            return asyncio.create_task(run_owned())
        except BaseException:
            if registry is not None and attempt_token:
                registry.unregister(project_id, task.id, attempt_token)
            raise

    async def _run_claimed_work_item(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> TaskResult | None:
        result = await self._run_work_item(task, task_by_projection_id, member_session=member_session)
        # Process coordinator spawn requests after work-item completion
        spawn_requests = list((task.metadata or {}).get("coordinator_spawn_requests", []))
        if spawn_requests:
            import uuid

            for req in spawn_requests:
                spawn_projection_id = f"coord-spawn-{uuid.uuid4().hex[:8]}"
                spawn_task = Task(
                    id=str(uuid.uuid4()),
                    title=f"Coordinator-routed work for {req.get('target_role', 'unknown')}",
                    description=str(req.get("prompt", "")),
                    assigned_to=str(req.get("target_role", "")),
                    status=TaskStatus.PENDING,
                    metadata=mark_work_item_projection({
                        "coordinator_spawned": True,
                        "spawned_by": task.assigned_to,
                    }, projection_id=spawn_projection_id, turn_type="execute"),
                    project_id=task.project_id,
                )
                await self.save_task(spawn_task)
                await self._emit_progress(
                    f"[Company] coordinator spawned task for {req.get('target_role', '?')}: {spawn_task.title}",
                    task_id=spawn_task.id,
                )
        await self.runtime.complete_claim(member_session, task, result=result)
        # Phase A Step 7: tail-end reverse-projection sync removed. Each
        # intermediate path (DONE via _apply_done_transition, FAILED via
        # transition_work_item_from_task, BLOCKED / PENDING / etc.) is now
        # responsible for its own phase write + side effects. The review-
        # verdict branch below is still explicit since review work items
        # have a specialized finalizer.
        if bool((task.metadata or {}).get("review_execution_work_item", False)):
            # Review work-item completion: runtime reads the structured
            # verdict produced by the review agent and auto-applies it
            # to the child work item (approve → done, rework → todo).
            # Then closes the hidden review work item and refreshes
            # downstream dependents.
            await self._finalize_review_work_item(task)
        await self.save_task(task)
        # Notify the manager/coordinator role that this work item completed.
        if task.status == TaskStatus.DONE and not bool((task.metadata or {}).get("synthetic_inbox_turn", False)):
            await self._notify_manager_of_completion(task, result)
        return result

    def _claimed_work_item_needs_cleanup(
        self,
        member_session: CompanyMemberSession,
        task: Task,
    ) -> bool:
        work_item_id = linked_work_item_id_for_task(task)
        claimed_task_ids = set(getattr(self.runtime, "_claimed_task_ids", set()) or set())
        claimed_work_item_ids = set(getattr(self.runtime, "_claimed_work_item_ids", set()) or set())
        if task.id in claimed_task_ids:
            return True
        if work_item_id and work_item_id in claimed_work_item_ids:
            return True
        return (
            str(getattr(member_session, "current_task_id", "") or "").strip() == task.id
            and normalize_role_runtime_status(
                getattr(member_session, "status", ""),
                getattr(member_session, "focused_work_item_id", ""),
            ) == "running"
        )

    async def _handle_claimed_work_item_exception(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        exc: Exception,
    ) -> None:
        projection_id = self._projection_id_for_task(task)
        work_item_id = linked_work_item_id_for_task(task)
        summary = (
            f"[Company:{projection_id}] claimed work item crashed but was isolated from the rest "
            f"of the session ({type(exc).__name__}: {exc})"
        )
        logger.opt(exception=exc).error(summary)
        exception_record = {
            "type": type(exc).__name__,
            "message": str(exc),
            **work_item_identity_payload(projection_id=projection_id, turn_type=""),
            "task_id": task.id,
            "work_item_id": work_item_id,
            "recorded_at": datetime.now().isoformat(),
        }
        task.metadata = dict(task.metadata or {})
        claim_active = self._claimed_work_item_needs_cleanup(member_session, task)
        if claim_active:
            task.metadata["claimed_work_item_exception"] = dict(exception_record)
            failure_result = TaskResult(
                status=TaskStatus.FAILED,
                content=summary,
                artifacts={"runtime_exception": dict(exception_record)},
            )
            task.result = {
                "content": failure_result.content,
                "artifacts": dict(failure_result.artifacts or {}),
            }
            # Phase A: phase write first → hook projects task.status=FAILED
            # onto the DB row and syncs our local task.status too. The
            # attempt_outcome="crashed" settlement rides the same write so
            # the dispatcher's crash-streak brake has durable accounting.
            # If even this write fails, quarantine the card via a plain
            # metadata hold — dispatch_hold blocks is_dispatchable without
            # needing phase-machine legality — so a store hiccup can never
            # convert a crash into an endless re-dispatch loop.
            try:
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="claimed_work_item_exception",
                    summary=summary or None,
                    release_claim=True,
                    attempt_outcome="crashed",
                )
            except Exception as transition_exc:
                logger.opt(exception=transition_exc).error(
                    f"[Company:{projection_id}] FAILED transition for crashed work item "
                    "did not land; quarantining via dispatch_hold"
                )
                if work_item_id and self.store is not None and hasattr(self.store, "update_delegation_work_item"):
                    try:
                        await self.store.update_delegation_work_item(
                            work_item_id,
                            blocked_reason=summary,
                            metadata_updates={
                                "dispatch_hold": "crash_quarantine",
                                "crash_quarantine_at": datetime.now().isoformat(),
                                "crash_quarantine_error": str(exc),
                            },
                        )
                    except Exception:
                        logger.opt(exception=True).error(
                            f"[Company:{projection_id}] crash quarantine write also failed"
                        )
            try:
                await self.runtime.complete_claim(member_session, task, result=failure_result)
            except Exception as cleanup_exc:
                logger.opt(exception=cleanup_exc).error(
                    f"[Company:{projection_id}] failed to release crashed claimed work item"
                )
            # Phase A Step 7: the preceding transition_work_item_from_task
            # (Phase.FAILED, Step 3 migration) already drove the phase write.
            # Review-path finalizer stays explicit for review work items.
            if bool((task.metadata or {}).get("review_execution_work_item", False)):
                try:
                    await self._finalize_review_work_item(task)
                except Exception as sync_exc:
                    logger.opt(exception=sync_exc).error(
                        f"[Company:{projection_id}] failed to finalize crashed review work item"
                    )
        else:
            post_claim = list(task.metadata.get("post_claim_runtime_exceptions", []) or [])
            post_claim.append(dict(exception_record))
            task.metadata["post_claim_runtime_exceptions"] = post_claim[-4:]
        try:
            await self.save_task(task)
        except Exception as save_exc:
            logger.opt(exception=save_exc).error(
                f"[Company:{projection_id}] failed to persist isolated work item exception"
            )
        await self._notify_kanban_changed()
        try:
            await self._emit_progress(summary, task_id=task.id)
        except Exception as progress_exc:
            logger.opt(exception=progress_exc).error(
                f"[Company:{projection_id}] failed to emit isolated work item exception progress"
            )

    async def _notify_manager_of_completion(self, task: Task, result: TaskResult | None) -> None:
        """Send a structured completion notification to this task's manager role."""
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org":
            return
        manager_role = str((task.metadata or {}).get("manager_role_id", "") or "").strip()
        if not manager_role or not self.communication:
            return
        from opc.core.models import AgentMessage, CommsSemanticType, MessageUrgency
        summary_parts = [f"Work item **{task.title}** completed by {task.assigned_to or 'unknown'}."]
        if result and hasattr(result, "output") and result.output:
            output_clip = clip_text(str(result.output), limit=500, marker="completion output preview truncated")
            output_preview = output_clip.text
            summary_parts.append(f"\n**Output preview:**\n{output_preview}")
        try:
            notification = AgentMessage(
                msg_type="inform",
                from_agent=task.assigned_to or task.metadata.get("work_item_role_id", "system"),
                to_agents=[manager_role],
                subject=f"[COMPLETED] {task.title}",
                body="\n".join(summary_parts),
                urgency=MessageUrgency.HIGH,
                semantic_type=CommsSemanticType.WORK_ITEM_RESULT,
                metadata={
                    "completion_task_id": task.id,
                    "auto_notification": True,
                    "output_preview_truncated": output_clip.truncated if result and getattr(result, "output", None) else False,
                    "output_preview_omitted_chars": output_clip.omitted_chars if result and getattr(result, "output", None) else 0,
                    "linked_work_item_id": linked_work_item_id_for_task(task),
                },
                task_id=task.id,
            )
            await self.communication.send_dm(notification, task=task)
        except Exception:
            pass  # Best-effort notification; don't fail the work item

    @staticmethod
    def _work_item_revision_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def _stale_work_item_revision_result_record(self, task: Task) -> dict[str, Any] | None:
        if not self.store:
            return None
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or not hasattr(self.store, "get_delegation_work_item"):
            return None
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            logger.opt(exception=True).debug("work-item revision stale guard: failed to load work item")
            return None
        if work_item is None:
            return None
        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
        current_revision = self._work_item_revision_value(work_item_metadata.get("manager_mutation_revision"))
        if current_revision <= 0:
            return None
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        started_revision = self._work_item_revision_value(
            task_metadata.get("started_work_item_revision")
            or task_metadata.get("claimed_work_item_revision")
        )
        if current_revision <= started_revision:
            return None
        return {
            "work_item_id": work_item_id,
            "task_id": task.id,
            "manager_mutation_id": str(work_item_metadata.get("manager_mutation_id", "") or "").strip(),
            "manager_mutation_action": str(work_item_metadata.get("manager_mutation_action", "") or "").strip(),
            "manager_mutation_revision": current_revision,
            "started_work_item_revision": started_revision,
            "manager_mutation_reason": str(work_item_metadata.get("manager_mutation_reason", "") or "").strip(),
            "recorded_at": datetime.now().isoformat(),
        }

    async def _reject_stale_work_item_revision_result(
        self,
        task: Task,
        result: TaskResult,
    ) -> TaskResult | None:
        stale_record = await self._stale_work_item_revision_result_record(task)
        if stale_record is None:
            return None
        stale_record["content"] = str(result.content or "")
        stale_record["artifacts"] = dict(result.artifacts or {})
        task.metadata = dict(task.metadata or {})
        stale_history = list(task.metadata.get("stale_work_item_revision_results", []) or [])
        stale_history.append(dict(stale_record))
        task.metadata["stale_work_item_revision_results"] = stale_history[-5:]
        task.metadata["latest_stale_work_item_revision_result"] = dict(stale_record)
        task.result = {
            "content": result.content,
            "artifacts": dict(result.artifacts or {}),
            "stale_work_item_revision_result": dict(stale_record),
        }
        await self._append_progress(
            task,
            "Ignored stale work-item result because a manager changed this WorkItem before the turn completed.",
        )
        if self.save_task:
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] stale result ignored after work-item mutation",
            task_id=task.id,
        )
        return TaskResult(
            status=TaskStatus.CANCELLED,
            content="Stale work-item result ignored because a manager changed this WorkItem before the turn completed.",
            artifacts={
                "stale_work_item_revision_result": dict(stale_record),
            },
        )

    @staticmethod
    def _is_self_evolution_work_item(task: Task) -> bool:
        metadata = dict(task.metadata or {})
        turn_kind = str(
            metadata.get("work_item_turn_type")
            or metadata.get("work_kind")
            or metadata.get("delegation_turn_kind")
            or ""
        ).strip().lower()
        return turn_kind == "self_evolution" or bool(metadata.get("self_evolution_work_item", False))

    @staticmethod
    def _parse_self_evolution_patch_json(raw: str | None) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.lower().startswith("json\n"):
                text = text.split("\n", 1)[1].strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _self_evolution_patch_validation_error(patches: list[Any], employee_id: str) -> str:
        if not patches:
            return ""
        if not employee_id:
            return "This self-evolution work item has no assigned employee; return `{ \"patches\": [] }`."
        for index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                return f"Patch at index {index} must be a JSON object."
            patch_employee_id = str(patch.get("employee_id", "") or "").strip()
            if not patch_employee_id:
                return f"Patch at index {index} must include employee_id `{employee_id}`."
            if patch_employee_id != employee_id:
                return (
                    f"Patch at index {index} targets employee_id `{patch_employee_id}`, "
                    f"but this work item may only update `{employee_id}`."
                )
        return ""

    async def _retry_or_fail_self_evolution_output(
        self,
        task: Task,
        result: TaskResult,
        *,
        retry_count: int,
        max_retries: int,
        feedback: str,
    ) -> TaskResult:
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata["self_evolution_patch_retry_feedback"] = feedback
        task.context_snapshot["self_evolution_patch_retry_feedback"] = feedback
        task.metadata["self_evolution_patch_retry_count"] = retry_count + 1
        if retry_count + 1 < max_retries:
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] retrying self-evolution JSON "
                f"({retry_count + 1}/{max_retries})",
                task_id=task.id,
            )
            return TaskResult(status=TaskStatus.PENDING, content=feedback, artifacts=dict(result.artifacts or {}))
        error_record = {
            "error": "invalid_self_evolution_json",
            "message": feedback,
            "attempts": retry_count + 1,
        }
        task.metadata["self_evolution_error"] = error_record
        task.result = {"content": feedback, "artifacts": {"self_evolution_error": error_record}}
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            await update_work_item_owned_metadata(self.store, work_item_id, {
                "self_evolution_error": error_record,
                "self_evolution_recorded": [],
            })
        await transition_work_item_from_task(
            self.store,
            task,
            target_status_or_phase=Phase.FAILED,
            reason="invalid_self_evolution_json",
            summary=feedback,
        )
        await self.save_task(task)
        return TaskResult(status=TaskStatus.FAILED, content=feedback, artifacts={"self_evolution_error": error_record})

    async def _finalize_self_evolution_work_item(
        self,
        task: Task,
        result: TaskResult,
    ) -> TaskResult | None:
        content = str(result.content or "").strip()
        data = self._parse_self_evolution_patch_json(content)
        patches = data.get("patches") if isinstance(data, dict) else None
        retry_count = int((task.metadata or {}).get("self_evolution_patch_retry_count", 0) or 0)
        max_retries = int((task.metadata or {}).get("self_evolution_patch_max_retries", 3) or 3)
        if data is None or not isinstance(patches, list):
            feedback = (
                "Self-evolution output must be strict JSON with a top-level `patches` list. "
                "Do not include prose, markdown, or delivery content."
            )
            return await self._retry_or_fail_self_evolution_output(
                task,
                result,
                retry_count=retry_count,
                max_retries=max_retries,
                feedback=feedback,
            )

        assignment = dict((task.metadata or {}).get("employee_assignment", {}) or {})
        employee_id = str(assignment.get("employee_id", "") or "").strip()
        patch_error = self._self_evolution_patch_validation_error(patches, employee_id)
        if patch_error:
            return await self._retry_or_fail_self_evolution_output(
                task,
                result,
                retry_count=retry_count,
                max_retries=max_retries,
                feedback=patch_error,
            )
        organization_id = str(
            getattr(task, "org_id", "")
            or (task.metadata or {}).get("organization_id", "")
            or (task.metadata or {}).get("org_id", "")
            or DEFAULT_ORGANIZATION_ID
        ).strip() or DEFAULT_ORGANIZATION_ID
        evolution = getattr(getattr(self, "memory", None), "employee_evolution", None)
        recorded: list[dict[str, Any]] = []
        if callable(getattr(evolution, "apply_employee_evolution_patch", None)):
            source = {
                "checkpoint_id": str((task.metadata or {}).get("self_evolution_checkpoint_id", "") or "").strip(),
                "checkpoint_type": "company_delivery_feedback",
                "human_action": str((task.metadata or {}).get("self_evolution_human_action", "") or "").strip(),
                "human_feedback": str((task.metadata or {}).get("self_evolution_human_feedback", "") or "").strip(),
                "project_id": str(task.project_id or "").strip(),
                "delivery_task_id": str((task.metadata or {}).get("self_evolution_delivery_task_id", "") or "").strip(),
                "delivery_projection_id": str((task.metadata or {}).get("self_evolution_delivery_projection_id", "") or "").strip(),
                "source_work_item_id": linked_work_item_id_for_task(task),
                "source_role_id": str(task.assigned_to or (task.metadata or {}).get("work_item_role_id", "") or "").strip(),
                "recorded_at": datetime.now().isoformat(),
            }
            recorded = evolution.apply_employee_evolution_patch(
                organization_id=organization_id,
                patch={"patches": patches},
                source=source,
                allowed_employee_ids={employee_id} if employee_id else set(),
            )

        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata.pop("self_evolution_patch_retry_feedback", None)
        task.context_snapshot.pop("self_evolution_patch_retry_feedback", None)
        task.metadata["self_evolution_patch_retry_count"] = retry_count
        task.metadata["self_evolution_recorded"] = list(recorded)
        task.metadata["self_evolution_patch"] = {"patches": patches}
        task.metadata["self_evolution_completed_at"] = datetime.now().isoformat()
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            await update_work_item_owned_metadata(self.store, work_item_id, {
                "self_evolution_recorded": list(recorded),
                "self_evolution_patch": {"patches": patches},
                "self_evolution_completed_at": task.metadata["self_evolution_completed_at"],
            })
        result.artifacts = {
            **dict(result.artifacts or {}),
            "self_evolution_recorded": list(recorded),
            "self_evolution_patch_count": len(patches),
        }
        return None

    async def _run_work_item(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
        *,
        member_session: CompanyMemberSession | None = None,
    ) -> TaskResult | None:
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        projection_id = self._projection_id_for_task(task)
        # Phase A Step 7: pre-work-item RUNNING marker removed. The pre-claim
        # transition_work_item_from_task(Phase.RUNNING) at the start of the
        # while-loop (Step 4 migration) and the per-iteration turn_start
        # transition inside the loop cover this responsibility correctly.
        await self._emit_progress(f"[Company:{projection_id}] starting {task.title}", task_id=task.id)
        if member_session:
            self.runtime.prepare_task_for_session(member_session, task)
            await self.runtime._sync_current_turn_mode_to_work_item(task, member_session.current_turn_mode)
        role = self.org_engine.get_role_for_work_item(task.assigned_to or task.metadata.get("work_item_role_id", ""), task.tags)
        task.assigned_to = role.role_id
        self._apply_role_defaults(task, role)
        if self.seat_executor is not None:
            await self.seat_executor.prepare_seat(
                task,
                member_session=member_session,
                role=role,
            )
        await self._prepare_setup_workspace(task)
        # Snapshot of which messages were unread when this turn started.
        # On successful completion we move only those to seen/, so any
        # mail that arrives mid-turn correctly stays as "new" for the
        # next turn (which may trigger a follow-up reactivation).
        self._snapshot_inbox_for_turn(task)
        self._inject_inbox_into_context(task, member_session)
        await self._inject_manager_board_into_context(task, member_session)
        self._inject_scratchpad_into_context(task)
        if not multi_team_org:
            await self._record_handoffs(task, task_by_projection_id)
            self._inject_parallel_peers_metadata(task, task_by_projection_id)
            self._inject_work_item_role_map(task)
            self._resolve_work_item_assignment_before_execution(task, task_by_projection_id)
            self._inherit_environment_manifest(task, task_by_projection_id)
            self._inherit_workspace_manifest(task, task_by_projection_id)
            self._inherit_data_acquisition_report(task, task_by_projection_id)
        lint_issues = [str(item).strip() for item in list(task.metadata.get("work_item_assignment_lint", []) or []) if str(item).strip()]
        if lint_issues and not multi_team_org:
            await self._append_progress(task, "Work-item assignment lint failed before execution.")
            await self._append_progress(task, "\n".join(f"- {issue}" for issue in lint_issues))
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="work_item_assignment_lint",
            )
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{projection_id}] assignment lint failed",
                task_id=task.id,
            )
            return TaskResult(status=TaskStatus.FAILED, content="\n".join(lint_issues))
        # Phase A: mark the work_item RUNNING via the phase channel. The
        # forward hook syncs task.status=RUNNING both on the DB row and
        # locally. Idempotent if the dispatcher already moved the phase.
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.RUNNING,
            reason="pre_execution_claim",
        )
        if self.agent_selector:
            # The selector also owns checkpoint attempt pins.  Forced-native
            # work must pass through it so a resumed native pin is validated
            # and consumed instead of leaking into a later dispatch.
            await self.agent_selector(task, role)
        elif task.metadata.get("force_native_execution"):
            task.assigned_external_agent = None
        else:
            if not task.assigned_external_agent:
                strategy = task.metadata.get("work_item_execution_strategy", "auto")
                orchestration_profile = str(task.metadata.get("work_item_orchestration_profile", "") or "").strip()
                if strategy == WorkItemExecutionStrategy.EXTERNAL.value:
                    task.assigned_external_agent = role.preferred_external_agent
                elif strategy == WorkItemExecutionStrategy.NATIVE.value:
                    task.assigned_external_agent = None
                elif orchestration_profile == "company_execute_native_first":
                    task.assigned_external_agent = None
                elif role.preferred_external_agent and strategy in {
                    WorkItemExecutionStrategy.AUTO.value,
                    WorkItemExecutionStrategy.MIXED.value,
                }:
                    task.assigned_external_agent = role.preferred_external_agent
        self._configure_external_timeouts(task)

        while True:
            task.metadata.pop("_retry_contract_enforcement", None)
            # The dispatch outcome is attempt-scoped.  Reset every transient
            # producer/escape marker together so a retry, rework, or follow-up
            # cannot inherit an earlier turn's board mutation or justification.
            task.metadata = reset_manager_dispatch_turn_metadata(task.metadata)
            manager_dispatch_retry_count = int(
                task.metadata.get("_manager_dispatch_retry_count", 0) or 0
            )
            dispatch_guard_before = await self._snapshot_manager_dispatch_state(
                task,
                member_session=member_session,
            )
            # Phase A: mark work_item RUNNING via phase channel. On retries
            # within this while-loop the phase may have regressed (e.g. to
            # READY_FOR_REWORK) so the explicit transition is still meaningful.
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.RUNNING,
                reason="turn_start",
            )
            await self.save_task(task)

            try:
                result = await asyncio.wait_for(
                    (
                        self.seat_executor.run_turn(task, member_session=member_session)
                        if self.seat_executor is not None
                        else self.execute_task(task)
                    ),
                    timeout=self.work_item_timeout,
                )
            except asyncio.CancelledError:
                # Suspension is a checkpoint transition owned by OPCEngine.
                # This task object may be stale by the time cancellation is
                # observed, so persisting it here can erase the canonical
                # checkpoint type, stop intent, or WorkItem hold.
                raise
            except asyncio.TimeoutError:
                logger.error(f"Company work item {projection_id} timed out after {self.work_item_timeout}s")
                await self._emit_progress(
                    f"[Company:{projection_id}] timed out after {self.work_item_timeout}s",
                    task_id=task.id,
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="work_item_timeout",
                    attempt_outcome="crashed",
                )
                await self.save_task(task)
                return TaskResult(status=TaskStatus.FAILED, content=f"Work item timed out after {self.work_item_timeout}s.")
            stale_result = await self._reject_stale_work_item_revision_result(task, result)
            if stale_result is not None:
                return stale_result
            if result.status != TaskStatus.DONE:
                if result.status in _REVIEW_WAITING_STATUSES:
                    await transition_work_item_from_task(
                        self.store,
                        task,
                        target_status_or_phase=phase_for_task_status(result.status),
                        reason="work_item_awaiting_review",
                    )
                    review_level = "manager" if result.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human"
                    await self._append_progress(task, f"Work item paused awaiting {review_level} review.")
                    await self._emit_progress(
                        f"[Company:{projection_id}] awaiting {review_level} review",
                        task_id=task.id,
                    )
                    await self.save_task(task)
                elif result.status == TaskStatus.AWAITING_PEER:
                    await transition_work_item_from_task(
                        self.store,
                        task,
                        target_status_or_phase=Phase.WAITING_FOR_PEER,
                        reason="work_item_awaiting_peer",
                    )
                    await self._append_progress(task, "Work item paused awaiting peer coordination.")
                    await self._save_peer_checkpoint(task)
                    await self._emit_progress(f"[Company:{projection_id}] awaiting peer", task_id=task.id)
                    await self.save_task(task)
                else:
                    await transition_work_item_from_task(
                        self.store,
                        task,
                        target_status_or_phase=result.status,
                        reason=f"work_item_{result.status.value}",
                    )
                    await self._append_progress(
                        task,
                        f"Work item ended with status {result.status.value}.",
                    )
                    await self._emit_progress(f"[Company:{projection_id}] failed", task_id=task.id)
                    await self.save_task(task)
                return result

            # Comms park check: if the agent sent any blocking messages
            # this turn whose replies have not yet arrived in its own
            # inbox, park this work item in AWAITING_PEER. The receiver will
            # be reactivated automatically by the inbound-mail rule on
            # its own work item; once it writes a reply with `reply_to`
            # matching, the scheduler tick (or the next direct call to
            # `_try_unpark_blocking_comms`) will resume this task.
            if await self._park_for_blocking_comms(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] parked awaiting blocking reply",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.AWAITING_PEER,
                    content="Work item parked awaiting blocking comms reply.",
                    artifacts=dict(result.artifacts or {}),
                )

            created_follow_up_work_item_ids = await self._materialize_follow_up_work_items(task, result)
            dispatch_guard_issues = await self._enforce_manager_dispatch_guard(
                task,
                result,
                before_state=dispatch_guard_before,
                created_follow_up_work_item_ids=created_follow_up_work_item_ids,
                member_session=member_session,
            )
            if dispatch_guard_issues:
                await self._append_progress(task, "Manager dispatch guard rejected the turn.")
                await self._append_progress(task, "\n".join(f"- {issue}" for issue in dispatch_guard_issues))
                max_dispatch_retries = int(
                    task.metadata.get("manager_dispatch_guard_max_retries", 2) or 2
                )
                task.context_snapshot = dict(task.context_snapshot or {})
                # Build feedback that escalates on each retry: first attempt
                # restates the rule; later attempts add the counter so the
                # agent sees "this is strike N of M" and knows the next
                # non-delegating turn is terminal.
                violation_text = "\n".join(dispatch_guard_issues)
                if manager_dispatch_retry_count:
                    violation_text = (
                        f"(Retry {manager_dispatch_retry_count}/{max_dispatch_retries}) "
                        + violation_text
                    )
                task.context_snapshot["manager_dispatch_guard_violation"] = violation_text
                if manager_dispatch_retry_count < max_dispatch_retries:
                    task.metadata = dict(task.metadata or {})
                    task.metadata["_manager_dispatch_retry_count"] = (
                        manager_dispatch_retry_count + 1
                    )
                    await self.save_task(task)
                    await self._emit_progress(
                        f"[Company:{projection_id}] "
                        f"retrying manager dispatch turn "
                        f"({manager_dispatch_retry_count + 1}/{max_dispatch_retries})",
                        task_id=task.id,
                    )
                    continue
                # Retries exhausted. Dispatch is a soft constraint: the org
                # chart fixes who *can* delegate, but not every task needs
                # every seat, so accept the turn output as normal completion
                # instead of failing the work item. Record the unresolved
                # guard note so reviewers and the delivery report can weigh
                # the output accordingly.
                task.metadata = dict(task.metadata or {})
                task.metadata["manager_dispatch_guard_unresolved"] = violation_text
                await self._append_progress(
                    task,
                    "Dispatch guard reminders exhausted; accepting the turn output "
                    "without delegation (noted for review).",
                )
                await self._emit_progress(
                    f"[Company:{projection_id}] accepted manager turn without delegation "
                    f"after dispatch guard reminders were exhausted",
                    task_id=task.id,
                )
            task.metadata.pop("_manager_dispatch_retry_count", None)
            task.metadata.pop("manager_dispatch_guard_terminal_violation", None)
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot.pop("manager_dispatch_guard_violation", None)
            output_bundle = self._capture_work_item_outputs(task, result)
            await self._persist_work_item_owned_output_metadata(task, output_bundle)
            if await self._park_for_delegated_children(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] parked awaiting delegated child work",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.BLOCKED,
                    content="Work item delegated downstream work and is waiting for child work items to complete.",
                    artifacts=dict(result.artifacts or {}),
                )
            if await self._block_completion_for_unread_inbox(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] inbox gate pending",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.PENDING,
                    content="Work item paused by inbox completion gate; handle pending mailbox messages before finishing.",
                    artifacts=dict(result.artifacts or {}),
                )
            if self._is_self_evolution_work_item(task):
                self_evolution_result = await self._finalize_self_evolution_work_item(task, result)
                if self_evolution_result is not None:
                    if self_evolution_result.status == TaskStatus.PENDING:
                        continue
                    return self_evolution_result
            if multi_team_org:
                # Review verdicts are applied mechanically by
                # ``_finalize_review_work_item`` (runtime reads the
                # structured verdict emitted by the review agent and
                # updates the child work item directly).  No retry loop
                # is needed: one review turn produces one verdict.
                await self._append_progress(task, f"Team-runtime turn completed by role {task.assigned_to}.")
                await self._apply_done_transition(task, result=result)
                if self._is_authoritative_delivery_work_item(task) or self._requires_user_feedback(task):
                    await self._finalize_completed_work_item(task)
                    return result
                if not self._is_self_evolution_work_item(task):
                    self._append_to_scratchpad(task, result)
                self._archive_consumed_inbox_snapshot(task)
                if await self._reactivate_for_unread_mail(task):
                    await self._emit_progress(
                        f"[Company:{projection_id}] reactivated by inbound comms",
                        task_id=task.id,
                    )
                else:
                    await self._emit_progress(
                        f"[Company:{projection_id}] completed",
                        task_id=task.id,
                    )
                return result
            contract_issues = await self._enforce_work_item_contracts(task, result)
            if contract_issues:
                if bool(task.metadata.pop("_retry_contract_enforcement", False)):
                    continue
                return TaskResult(
                    status=task.status,
                    content="\n".join(contract_issues),
                    artifacts=dict(result.artifacts or {}),
                )
            gate = self._gate_from_metadata(task.metadata.get("work_item_gate"))
            if gate and self._work_item_gate_enforcement_enabled(task):
                await self._apply_gate(task, gate, task_by_projection_id)
            else:
                if gate:
                    await self._append_progress(task, f"Work-item gate `{gate.gate_type}` skipped by runtime policy.")
                await self._append_progress(task, f"Work item completed by role {task.assigned_to}.")
                await self._apply_done_transition(task, result=result)
                completion_action = await self._finalize_work_item_with_gate_harness(task, task_by_projection_id)
                if task.status == TaskStatus.DONE:
                    # Append completion summary to shared scratchpad
                    self._append_to_scratchpad(task, result)
                    # Archive whatever was unread at TURN START — those
                    # are the messages this turn had a chance to read.
                    # Anything that arrived during the turn stays as
                    # `new` and will trigger reactivation below.
                    self._archive_consumed_inbox_snapshot(task)
                    # Comms reactivation hook: if the agent has any unread
                    # mail when its turn ends, the work item is not actually
                    # finished — there is information addressed to this
                    # role that it has not consumed. Re-open the task as
                    # PENDING so the scheduler claims it for another turn.
                    # Convergence is enforced by the prompt rules ("only
                    # send when you need confirmation/changes; silence is
                    # ack"), not by a hard counter — we just record the
                    # reactivation depth for telemetry / anomaly detection.
                    if await self._reactivate_for_unread_mail(task):
                        await self._emit_progress(
                            f"[Company:{projection_id}] reactivated by inbound comms",
                            task_id=task.id,
                        )
                    else:
                        await self._emit_progress(
                            f"[Company:{projection_id}] completed",
                            task_id=task.id,
                        )
                elif task.status in _REVIEW_WAITING_STATUSES:
                    review_label = "manager review" if task.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human review"
                    await self._emit_progress(
                        f"[Company:{projection_id}] awaiting {review_label}",
                        task_id=task.id,
                    )
            return result

    # Turn types whose completion is review-exempt only when THIS attempt
    # actually changed the delegated business board.  Historical children do
    # not describe what the current turn produced.
    _DELEGATION_OUTPUT_TURN_TYPES = frozenset({"dispatch", "intake", "plan"})

    def _has_agent_manager_above(self, task: Task, linked_work_item: Any) -> bool:
        """True when the card's manager is a real agent role that can run a
        review turn. Top seats report to the human ``owner`` and intentionally
        auto-approve their own zero-delegation output; subsequent refinement
        happens through the existing owner/final-decider conversation."""
        manager_role_id = (
            str((task.metadata or {}).get("manager_role_id", "") or "").strip()
            or str(getattr(linked_work_item, "manager_role_id", "") or "").strip()
        )
        if not manager_role_id or manager_role_id == "owner":
            return False
        get_agent = getattr(getattr(self, "org_engine", None), "get_agent", None)
        if callable(get_agent):
            try:
                return get_agent(manager_role_id) is not None
            except Exception:
                return True
        return True

    async def _apply_done_transition(
        self,
        task: Task,
        *,
        result: TaskResult | None = None,
    ) -> Phase | None:
        """Canonical 'worker work item completed' transition for company-mode.

        **Contract (Phase A Step 7, root-fixed post-new20)**: this helper
        handles **WORKER** tasks only. Review execution work_items
        (``metadata['review_execution_work_item'] = True``) are routed
        elsewhere — do NOT process them here, even partially. Calling this
        helper with a review card is a no-op by design; see below.

        Routing (worker tasks):
        - dispatch / intake / plan with a current-turn business-board mutation
          → Phase.APPROVED; without one → manager review when an agent
          manager exists, otherwise top-seat auto-approval
        - aggregate/synthesis → Phase.APPROVED
        - final user-visible delivery cards → Phase.AWAITING_HUMAN
          (user reviews final delivery only)
        - non-final delivery/attention cards → Phase.APPROVED
        - otherwise → Phase.AWAITING_MANAGER_REVIEW + spawn hidden manager
          review work_item in the manager seat's queue (kanban-push core)

        Side effects (only for worker tasks):
        - On AWAITING_MANAGER_REVIEW: spawn the worker report WorkItem; its
          terminal payload then spawns the manager review WorkItem.
        - On APPROVED: rely on refresh_dependents_hook (Step 1 fix) to
          cascade parent frontier refresh; also save delegation audit event.
        - Kanban UI notify via _notify_kanban_changed.

        **Why the review no-op**: the pre-Step-7 code enforced this split
        at ``_run_claimed_work_item`` tail via ``skip_work_item_sync`` + an
        ``elif review_execution_work_item: _finalize_review_work_item(...)``
        branch. Step 7 distributed the worker transition to 7 inline sites
        but the review path stayed at the tail. If this helper processed
        review cards (spawning a review-of-review manager review card, OR
        calling _finalize_review_work_item eagerly), it would conflict with
        the tail's finalize call — either infinite review-of-review
        recursion (reproduced as final decider stuck on
        ``review::review::review::...::v1`` with 15+ nesting in new20),
        or double verdict application.

        **The invariant**: exactly one finalization per review card, at
        the tail. This helper enforces that by early-returning for review
        cards. _finalize_review_work_item stays the SOLE review closer.

        Returns the resolved Phase for worker transitions, or ``None`` when
        (a) the task is a review card (no-op by contract), (b) there is no
        linked work_item (task-mode fallback).
        """
        if bool((task.metadata or {}).get("review_execution_work_item", False)):
            # CONTRACT: this helper is worker-only. Review cards are
            # finalized by _run_claimed_work_item's tail via
            # _finalize_review_work_item. Any side effect here (spawn or
            # finalize) would double-fire with the tail. See docstring.
            return None
        if bool((task.metadata or {}).get("report_execution_work_item", False)):
            # Two-turn worker→review flow: this is the hidden report card
            # finishing. Take the report turn's output as the parent's
            # canonical completion_report, refresh review_evidence, then
            # spawn the actual review card. Finally close the report card
            # itself as APPROVED (it served its purpose).
            return await self._apply_report_done_transition(task, result=result)
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            # Task-mode leakage: fall back to local DONE sync via the helper's
            # built-in fallback behaviour.
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.DONE,
                reason="apply_done_task_mode_fallback",
            )
            return None
        linked_work_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                linked_work_item = None
        linked_work_item_metadata = dict(getattr(linked_work_item, "metadata", {}) or {})
        task.metadata = dict(task.metadata or {})
        for key in (
            "user_visible",
            "authoritative_output",
            "review_owner_kind",
            "requires_user_feedback",
            "feedback_scope",
        ):
            if key not in task.metadata and key in linked_work_item_metadata:
                task.metadata[key] = copy.deepcopy(linked_work_item_metadata[key])

        # Determine summary from result (preferred) or task.result (legacy).
        summary = ""
        if result is not None:
            summary = str(result.content or "").strip()
        elif task.result and isinstance(task.result, dict):
            summary = str(task.result.get("content", "") or "").strip()

        # Route DONE to one of {APPROVED, AWAITING_HUMAN, AWAITING_MANAGER_REVIEW}.
        raw_work_kind = self._turn_type_for_task(
            task,
            fallback=str(task.metadata.get("work_kind", "") or "execute"),
        )
        work_kind = canonical_work_item_turn_type_for_kind(raw_work_kind, fallback="")
        linked_attention_id = str((task.metadata or {}).get("attention_work_item_id", "") or "").strip()
        is_attention_work_item = (
            bool((task.metadata or {}).get("attention_work_item", False))
            or bool(linked_work_item_metadata.get("attention_work_item", False))
            or (bool(linked_attention_id) and linked_attention_id == work_item_id)
        )
        manager_reviewable = is_manager_reviewable_turn(work_kind) if work_kind else True
        is_delivery_card = (
            is_delivery_turn(task.metadata)
            or str(task.metadata.get("review_owner_kind", "") or "").strip().lower() == "human"
        )
        manager_turn_context: dict[str, str] = {}
        if (
            not manager_reviewable
            and not is_attention_work_item
            and not is_delivery_card
            and work_kind in self._DELEGATION_OUTPUT_TURN_TYPES
        ):
            board_mutated = bool(
                (task.metadata or {}).get("manager_board_mutation_performed", False)
            )
            justification = str(
                (task.metadata or {}).get("manager_no_delegation_justification", "") or ""
            ).strip()
            unresolved = str(
                (task.metadata or {}).get("manager_dispatch_guard_unresolved", "") or ""
            ).strip()
            manager_turn_context = {
                "outcome": "delegated" if board_mutated else "self_produced",
                "source": (
                    "board_mutation"
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
                manager_turn_context["note"] = note
            if not board_mutated and self._has_agent_manager_above(
                task, linked_work_item,
            ):
                manager_reviewable = True
        if is_attention_work_item:
            # Attention work items are wake-up wrappers that let a parked
            # manager consume inbox/board state and call orchestration tools.
            # They are not business deliverables, so completing one must not
            # spawn a report/review chain for the wrapper itself.
            target_phase = Phase.APPROVED
        elif is_delivery_card:
            target_phase = (
                Phase.AWAITING_HUMAN
                if self._requires_user_feedback(task)
                else Phase.APPROVED
            )
        elif not manager_reviewable:
            # Dispatch cards deliver the child work-item set, while aggregate /
            # synthesize cards roll approved child results up to the parent.
            # These turn types are explicitly non-reviewable; routing them to
            # AWAITING_MANAGER_REVIEW leaves no review card able to consume them.
            target_phase = Phase.APPROVED
        else:
            target_phase = Phase.AWAITING_MANAGER_REVIEW

        # Persist the evidence and reviewer identity on the authoritative
        # WorkItem when it enters a passive review phase.
        metadata_updates: dict[str, Any] = {
            **work_item_identity_payload_for_task(task),
            "adaptive": dict(task.metadata.get("adaptive", {}) or {}),
        }
        if is_attention_work_item:
            metadata_updates["attention_work_item_outcome"] = "completed"
        if target_phase in {Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN}:
            review_owner_role_id = str(task.metadata.get("manager_role_id", "") or "").strip()
            review_owner_seat_id = str(task.metadata.get("manager_seat_id", "") or "").strip()
            if not review_owner_role_id or not review_owner_seat_id:
                if linked_work_item is not None:
                    if not review_owner_role_id:
                        review_owner_role_id = str(getattr(linked_work_item, "manager_role_id", "") or "").strip()
                    if not review_owner_seat_id:
                        review_owner_seat_id = str(getattr(linked_work_item, "manager_seat_id", "") or "").strip()
            if target_phase == Phase.AWAITING_MANAGER_REVIEW and not review_owner_role_id:
                logger.warning(
                    "_apply_done_transition auto-approved manager-reviewable work item "
                    "because no manager reviewer role was available; non-final work items "
                    f"must not enter human review. task_id={task.id} work_item_id={work_item_id}"
                )
                target_phase = Phase.APPROVED
            metadata_updates["review_owner_role_id"] = review_owner_role_id
            metadata_updates["review_owner_seat_id"] = review_owner_seat_id
            if summary:
                metadata_updates["completion_report"] = summary
            review_evidence = self._build_review_evidence(task, summary)
            if manager_turn_context:
                review_evidence = dict(review_evidence or {})
                review_evidence["manager_dispatch"] = dict(manager_turn_context)
            if review_evidence:
                metadata_updates["review_evidence"] = review_evidence

        # Phase write + local status sync via the canonical helper. Returns
        # False only if wid disappeared between our lookup and the call —
        # treat that as "someone else handled it" and skip side effects.
        wrote = await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=target_phase,
            reason="apply_done_transition",
            summary=summary or None,
            metadata_updates=metadata_updates,
        )
        if not wrote:
            return None

        # Re-read persisted phase to check for silent-degrade case (the
        # helper preserves the persisted phase if our target isn't in
        # ALLOWED_TRANSITIONS from the persisted state — e.g. a reviewer
        # already flipped the card while we were finishing). We only fire
        # spawn / refresh side effects if the transition actually landed
        # at our requested target.
        persisted_phase: Phase | None = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                refreshed_item = await self.store.get_delegation_work_item(work_item_id)
                if refreshed_item is not None:
                    persisted_phase = getattr(refreshed_item, "phase", None)
            except Exception:
                persisted_phase = None

        if persisted_phase == Phase.AWAITING_MANAGER_REVIEW:
            # Two-turn worker→review handoff: spawn a hidden report card
            # first (NOT the review card directly). The same worker session
            # resumes under a report-generation prompt to produce a clean
            # structured handoff. Only after the report card completes
            # (handled in _apply_report_done_transition) do we spawn the
            # actual review card. The completion_report we just stamped
            # onto the parent metadata is the worker's last execute-turn
            # prose — used as fallback if the report turn never produces
            # output; it will be overwritten by the report turn's content
            # when that turn finishes.
            await self._ensure_report_work_item_for_work_item(
                work_item_id,
                worker_task=task,
            )

        # Delegation audit event. Best-effort — never let persistence
        # failure propagate into the state machine.
        if hasattr(self.store, "save_delegation_event"):
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=str(task.metadata.get("delegation_run_id", "") or "").strip(),
                        work_item_id=work_item_id,
                        cell_id=str(task.metadata.get("delegation_cell_id", "") or "").strip() or None,
                        role_id=str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip() or None,
                        event_type="work_item_status_updated",
                        payload={
                            "task_id": task.id,
                            "task_status": task.status.value,
                            **work_item_identity_payload_for_task(task),
                            "summary": clip_text(summary, limit=500, marker="event summary truncated").text if summary else "",
                            **(
                                {"manager_dispatch": dict(manager_turn_context)}
                                if manager_turn_context
                                else {}
                            ),
                        },
                    )
                )
            except Exception:
                logger.debug("Best-effort delegation event persistence failed")

        # Push the transition to the kanban UI so the card moves columns
        # immediately. Uses _schedule_kanban_notification's debounce.
        await self._notify_kanban_changed()
        return persisted_phase

    async def _apply_report_done_transition(
        self,
        task: Task,
        *,
        result: TaskResult | None = None,
    ) -> Phase | None:
        """Close a hidden report card and spawn the actual review card.

        Two-turn worker→review handoff: the worker's execute turn DONE
        spawned a hidden report card; this is that report card finishing.
        The report turn's ``result.content`` is the canonical handoff
        text. The runtime first closes this report card with the full payload
        as one durable write, then projects that payload to the parent, then
        spawns review. Restart reconciliation can resume after either later
        write without asking the worker to report twice.

        The report card itself transitions to APPROVED — it served its
        purpose; nothing reviews it. The parent worker work_item stays in
        AWAITING_MANAGER_REVIEW (it was already there when the execute
        turn finished). What changes for the parent is the metadata
        payload that the upcoming review turn consumes.
        """
        meta = dict(task.metadata or {})
        report_card_id = linked_work_item_id_for_task(task)
        parent_work_item_id = str(meta.get("report_target_work_item_id", "") or "").strip()
        if not parent_work_item_id:
            # Defensive: report card with no parent pointer is corrupt;
            # close it and bail. Won't lose data — a future worker DONE
            # would re-spawn a new report card.
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "no_parent",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of orphan report card failed")
            return None

        # The report turn's prose IS the handoff. Try a structured parse
        # for downstream consumers, but pass the raw prose through
        # regardless so reviewers see what the worker actually wrote.
        report_raw = ""
        if result is not None:
            report_raw = str(result.content or "").strip()
        elif task.result and isinstance(task.result, dict):
            report_raw = str(task.result.get("content", "") or "").strip()
        parsed_report = self._parse_worker_report(report_raw)

        parent_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                parent_item = await self.store.get_delegation_work_item(parent_work_item_id)
            except Exception:
                parent_item = None
        if parent_item is None:
            # Parent disappeared — nothing we can do; close the report card.
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "parent_missing",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of orphan report card failed")
            return None

        parent_metadata = dict(getattr(parent_item, "metadata", {}) or {})
        if getattr(parent_item, "phase", None) != Phase.AWAITING_MANAGER_REVIEW:
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "parent_not_awaiting_review",
                            "report_parent_phase": str(
                                getattr(getattr(parent_item, "phase", None), "value", "") or ""
                            ),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of non-reviewable report card failed")
            await self._record_work_item_runtime_diagnostic(
                code="report_parent_not_awaiting_review",
                severity="info",
                work_item=parent_item,
                task=task,
                message="Report card target is no longer awaiting manager review; review card was not spawned.",
                details={
                    "parent_phase": str(
                        getattr(getattr(parent_item, "phase", None), "value", "") or ""
                    ),
                    "report_card_id": report_card_id,
                },
                warn=False,
            )
            return None
        completion_report = report_raw or str(parent_metadata.get("completion_report", "") or "")
        review_evidence = self._review_evidence_from_work_item(
            parent_item,
            completion_report,
        )
        if isinstance(parsed_report, dict) and parsed_report:
            review_evidence = dict(review_evidence or {})
            review_evidence.setdefault("worker_report", {})
            review_evidence["worker_report"].update(parsed_report)

        parent_metadata_updates = {
            "completion_report": completion_report,
            "review_evidence": review_evidence,
            "report_completion_raw": report_raw,
        }

        # Durability point: close the report and persist its complete payload
        # in one WorkItem write *before* projecting it to the parent or
        # creating review.  A crash after this write can be repaired using
        # the terminal report alone.
        persisted_report: DelegationWorkItem | None = None
        if report_card_id and hasattr(self.store, "update_delegation_work_item"):
            try:
                persisted_report = await self.store.update_delegation_work_item(
                    report_card_id,
                    phase=Phase.APPROVED,
                    claimed_by_role_runtime_session_id="",
                    claimed_by_seat_id="",
                    metadata_updates={
                        "claimed_by_role_session_id": "",
                        "claimed_task_id": "",
                        "report_card_outcome": "applied",
                        "completion_report": completion_report,
                        "review_evidence": review_evidence,
                        "report_completion_raw": report_raw,
                        "last_report_turn_finished_at": datetime.now().isoformat(),
                    },
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "report_done: failed to persist terminal report payload"
                )
        if persisted_report is None:
            # Do not create a review from volatile data. The report card stays
            # active. Release its persisted claim so the live dispatcher can
            # retry immediately; restart recovery is not required.
            await self._release_auxiliary_claim_for_retry(report_card_id)
            return None

        try:
            await self.store.update_delegation_work_item(
                parent_work_item_id,
                metadata_updates=parent_metadata_updates,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "report_done: failed to update parent metadata with report payload"
            )
            await self._notify_kanban_changed()
            return Phase.APPROVED

        review_owner_role_id = str(
            parent_metadata.get("review_owner_role_id", "")
            or parent_item.manager_role_id
            or ""
        ).strip()
        review_owner_seat_id = str(
            parent_metadata.get("review_owner_seat_id", "")
            or parent_item.manager_seat_id
            or ""
        ).strip()
        if not review_owner_role_id or not review_owner_seat_id:
            await self._record_work_item_runtime_diagnostic(
                code="report_parent_missing_review_owner",
                severity="warning",
                work_item=parent_item,
                task=task,
                message="Reviewable report target has no review owner; review card was not spawned.",
                details={"parent_work_item_id": parent_work_item_id, "report_card_id": report_card_id},
            )
        else:
            await self._ensure_review_work_item_for_work_item(
                parent_work_item_id,
                completion_report=completion_report,
                metadata_updates={
                    "review_owner_role_id": review_owner_role_id,
                    "review_owner_seat_id": review_owner_seat_id,
                },
                source_report_item=persisted_report,
            )
        await self._notify_kanban_changed()
        return Phase.APPROVED

    async def _notify_kanban_changed(self) -> None:
        """Best-effort UI push.  Callers must NEVER let a UI-side failure
        propagate into the company-mode state machine.

        Per-transition call-sites (for example attention-card upserts) can
        fire many times in rapid
        succession when several work items flip status in the same tick.  We
        route them through ``_schedule_kanban_notification`` so the heavy
        ``build_collab_sync`` pass runs once per debounce window instead of
        once per transition — a strict responsiveness win, since none of
        these callers require the broadcast to have landed before they
        return (they were already wrapped in ``try/except: pass``).
        """
        if self.on_kanban_changed is None:
            return
        self._schedule_kanban_notification()

    def _signal_dispatcher_wake(self) -> None:
        """Synchronous wake-signal called by collaboration tools after
        persisting new TODO work items.  Safe to call from any coroutine;
        setting an already-set Event is a no-op.  The main loop in
        ``_execute_multi_team_org`` awaits this event so newly-delegated
        children are claimed+spawned immediately instead of waiting for
        the parent turn's gather batch to drain."""
        try:
            self._dispatcher_wake.set()
        except Exception:
            pass

    def _rehydrate_parked_member_sessions(self, work_items: list[Any]) -> None:
        """Per-tick dispatcher convergence: unpark in-memory member
        sessions whose focused work-item has been freed by a wake write.

        Phase B replaces the old per-transition
        ``_reconcile_member_session_after_phase`` / reenqueue hooks with
        this idempotent refresh. On every iteration of the dispatcher
        loop, we read the current work-items from the DB, find parked
        sessions whose focused card is now dispatchable (e.g. all
        children approved, or a reviewer returned a rework verdict),
        and flip their status back to ``idle`` so the next
        ``claim_runnable_tasks`` pass will pick them up.

        Pure in-memory + synchronous. No I/O.
        """
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return
        work_item_by_id: dict[str, Any] = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        now = datetime.now()
        for session in runtime.member_sessions.values():
            current = normalize_role_runtime_status(
                getattr(session, "status", ""),
                getattr(session, "focused_work_item_id", ""),
            )
            session.status = current
            session.resident_status = current
            if current != "blocked":
                continue
            focused_id = str(getattr(session, "focused_work_item_id", "") or "").strip()
            if not focused_id:
                # Nothing holding it parked; flip to idle.
                session.status = "idle"
                session.resident_status = "idle"
                session.updated_at = now
                continue
            work_item = work_item_by_id.get(focused_id)
            if work_item is None:
                # Focused card vanished (approved + archived).
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                session.updated_at = now
                continue
            if is_dispatchable(work_item):
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                session.updated_at = now
                # Also refresh the in-memory DelegationRoleSession mirror
                # so downstream reads (e.g. in claim_runnable_tasks) agree.
                role_session = runtime.role_sessions.get(
                    str(getattr(session, "role_session_id", "") or "").strip()
                )
                if role_session is not None and normalize_role_runtime_status(
                    getattr(role_session, "status", ""),
                    getattr(role_session, "focused_work_item_id", ""),
                ) == "blocked":
                    role_session.status = "idle"
                    role_session.focused_work_item_id = ""
                    role_session.updated_at = now

    def _schedule_kanban_notification(self) -> None:
        """Debounced, fire-and-forget UI push (Fix C).

        Marks the board dirty and ensures exactly one broadcaster
        coroutine is running; it coalesces rapid-fire changes into a
        single `build_collab_sync` + websocket broadcast after
        ``_kanban_debounce_sec`` of quiet.  Dispatch-loop iterations
        therefore never block on snapshot construction.
        """
        if self.on_kanban_changed is None:
            return
        self._kanban_dirty = True
        task = self._kanban_broadcast_task
        if task is not None and not task.done():
            return
        try:
            self._kanban_broadcast_task = asyncio.create_task(
                self._run_kanban_broadcaster()
            )
        except RuntimeError:
            # No running event loop (e.g. during teardown) — skip.
            self._kanban_dirty = False

    async def _run_kanban_broadcaster(self) -> None:
        try:
            while self._kanban_dirty:
                self._kanban_dirty = False
                try:
                    await asyncio.sleep(self._kanban_debounce_sec)
                except asyncio.CancelledError:
                    return
                # If more dirt accumulated during the debounce window,
                # clear the flag again before broadcasting so a change
                # arriving mid-snapshot still triggers a follow-up pass.
                self._kanban_dirty = False
                if self.on_kanban_changed is None:
                    return
                try:
                    await self.on_kanban_changed()
                except Exception:
                    # The hook logs its own traceback; swallow here.
                    pass
        finally:
            self._kanban_broadcast_task = None

    @staticmethod
    def _review_work_item_id_for_work_item(work_item_id: str, attempt: int) -> str:
        """Per-attempt review work-item ID.

        Each AWAITING_MANAGER_REVIEW entry creates a fresh review work
        item with a new attempt number. Old attempts are immutable
        history. This eliminates the bug class where re-using a single
        deterministic ID caused stuck states once a previous attempt
        landed in a terminal phase (CANCELLED).
        """
        return review_work_item_id_for_attempt(work_item_id, attempt)

    @staticmethod
    def _safe_positive_int(value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    @classmethod
    def _auxiliary_attempt_number(
        cls,
        item: DelegationWorkItem,
        *,
        kind: str,
    ) -> int:
        """Read an auxiliary attempt from durable card identity.

        Parent counters are only caches: a process can crash after the card
        write and before the counter write.  The card's metadata, batch index,
        and deterministic id therefore all participate in recovery.
        """
        metadata = dict(getattr(item, "metadata", {}) or {})
        values = [
            cls._safe_positive_int(metadata.get(f"{kind}_attempt")),
            cls._safe_positive_int(getattr(item, "batch_index", 0)),
        ]
        item_id = str(getattr(item, "work_item_id", "") or "").strip()
        match = re.search(r"::v(\d+)$", item_id)
        if match:
            values.append(cls._safe_positive_int(match.group(1)))
        return max(values or [0])

    @classmethod
    def _targeting_auxiliary_items(
        cls,
        run_items: list[DelegationWorkItem],
        target_work_item_id: str,
        *,
        kind: str,
    ) -> list[DelegationWorkItem]:
        target_key = f"{kind}_target_work_item_id"
        target = str(target_work_item_id or "").strip()
        items = [
            item
            for item in list(run_items or [])
            if str((getattr(item, "metadata", {}) or {}).get(target_key, "") or "").strip()
            == target
        ]
        return sorted(
            items,
            key=lambda item: (
                cls._auxiliary_attempt_number(item, kind=kind),
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "work_item_id", "") or ""),
            ),
        )

    @classmethod
    def _active_auxiliary_item(
        cls,
        run_items: list[DelegationWorkItem],
        target_work_item_id: str,
        *,
        kind: str,
    ) -> DelegationWorkItem | None:
        active = [
            item
            for item in cls._targeting_auxiliary_items(
                run_items,
                target_work_item_id,
                kind=kind,
            )
            if getattr(item, "phase", None) not in DONE_PHASES
        ]
        return active[-1] if active else None

    @classmethod
    def _next_auxiliary_attempt(
        cls,
        parent_item: DelegationWorkItem,
        run_items: list[DelegationWorkItem],
        *,
        kind: str,
    ) -> int:
        metadata = dict(getattr(parent_item, "metadata", {}) or {})
        attempts = [cls._safe_positive_int(metadata.get(f"{kind}_attempt_count"))]
        attempts.extend(
            cls._auxiliary_attempt_number(item, kind=kind)
            for item in cls._targeting_auxiliary_items(
                run_items,
                parent_item.work_item_id,
                kind=kind,
            )
        )
        return max(attempts or [0]) + 1

    async def _run_items_for_parent(
        self,
        parent_item: DelegationWorkItem,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> list[DelegationWorkItem]:
        if run_items is not None:
            return list(run_items)
        if not self.store or not hasattr(self.store, "list_delegation_work_items"):
            return [parent_item]
        try:
            return await self.store.list_delegation_work_items(parent_item.run_id)
        except Exception:
            logger.opt(exception=True).debug("Failed to list auxiliary work items")
            return [parent_item]

    async def _insert_auxiliary_work_item_if_absent(
        self,
        item: DelegationWorkItem,
    ) -> tuple[DelegationWorkItem | None, bool]:
        """Create a deterministic auxiliary card without overwriting a race winner.

        Production stores provide an SQLite-level insert-if-absent operation.
        The read/save fallback keeps lightweight test stores compatible; it is
        intentionally not used as the production concurrency guarantee.
        """
        if not self.store:
            return None, False
        insert_if_absent = getattr(
            self.store,
            "insert_delegation_work_item_if_absent",
            None,
        )
        if callable(insert_if_absent):
            created = bool(await insert_if_absent(item))
            if created:
                return item, True
            try:
                return await self.store.get_delegation_work_item(item.work_item_id), False
            except Exception:
                return None, False

        try:
            existing = await self.store.get_delegation_work_item(item.work_item_id)
        except Exception:
            existing = None
        if existing is not None:
            return existing, False
        await self.store.save_delegation_work_item(item)
        return item, True

    @staticmethod
    def _work_item_output_metadata_for_task(task: Task) -> dict[str, Any]:
        """Return WorkItem-owned output metadata carried as runtime context."""
        context_outputs = dict((getattr(task, "context_snapshot", {}) or {}).get("work_item_owned_outputs", {}) or {})
        metadata = dict(getattr(task, "metadata", {}) or {})
        for key in (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "follow_up_actions",
            "downstream_assignments",
            "open_questions",
            "assumptions",
            "decisions",
            "risks",
            "completion_report",
            "handoff_context",
            "context_preview",
        ):
            if key not in context_outputs and metadata.get(key) not in (None, "", [], {}):
                context_outputs[key] = copy.deepcopy(metadata.get(key))
        return context_outputs

    @staticmethod
    def _set_work_item_output_context(task: Task, updates: dict[str, Any]) -> None:
        """Carry WorkItem-owned outputs on the runtime context without persisting them as Task metadata."""
        clean_updates = {
            str(key): copy.deepcopy(value)
            for key, value in dict(updates or {}).items()
            if value not in (None, "", [], {})
        }
        if not clean_updates:
            return
        task.context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        current = dict(task.context_snapshot.get("work_item_owned_outputs", {}) or {})
        current.update(clean_updates)
        task.context_snapshot["work_item_owned_outputs"] = current

    def _build_review_evidence(self, worker_task: Task, completion_report: str) -> dict[str, Any]:
        output_metadata = self._work_item_output_metadata_for_task(worker_task)
        artifact_manifest = self._normalize_work_item_artifact_index(
            output_metadata.get("work_item_artifact_index", [])
        )[:12]
        verification_status = dict(output_metadata.get("verification_status", {}) or {})
        verification_checks: list[dict[str, str]] = []
        for item in list(worker_task.metadata.get("automated_verification_results", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                }
            )
        verification_evidence = dict(output_metadata.get("verification_evidence", {}) or {})
        for item in list(verification_evidence.get("checks", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or item.get("raw_output", "") or "").strip(),
                }
            )
        for item in list(output_metadata.get("verification", []) or worker_task.metadata.get("verification", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                }
            )
        key_commands: list[str] = []
        for entry in verification_checks:
            command = str(entry.get("command", "") or "").strip()
            if command and command not in key_commands:
                key_commands.append(command)
        output_paths: list[str] = []
        changed_areas: list[str] = []
        for artifact in artifact_manifest:
            if not isinstance(artifact, dict):
                continue
            value = str(artifact.get("value", "") or "").strip()
            if value and value not in output_paths:
                output_paths.append(value)
            label = str(artifact.get("label", "") or artifact.get("kind", "") or "").strip()
            if label and label not in changed_areas:
                changed_areas.append(label)
            if value and value not in changed_areas:
                changed_areas.append(value)
        for ref in list(worker_task.metadata.get("artifacts", []) or []):
            item = str(ref or "").strip()
            if not item:
                continue
            if item not in changed_areas:
                changed_areas.append(item)
            if ":" in item:
                _, _, maybe_path = item.partition(":")
                maybe_path = maybe_path.strip()
                if maybe_path and maybe_path not in output_paths:
                    output_paths.append(maybe_path)
        target_output_dir = str(worker_task.metadata.get("target_output_dir", "") or "").strip()
        if target_output_dir and target_output_dir not in output_paths:
            output_paths.append(target_output_dir)
        evidence: dict[str, Any] = {
            "completion_summary": str(completion_report or "").strip(),
            "artifact_manifest": artifact_manifest,
            "changed_areas": changed_areas[:12],
            "verification_results": {
                "status": verification_status,
                "checks": verification_checks[:10],
            },
            "key_commands": key_commands[:10],
            "output_paths": output_paths[:12],
            "open_risks": [
                str(item).strip()
                for item in list(output_metadata.get("risks", []) or worker_task.metadata.get("risks", []) or [])
                if str(item).strip()
            ][:10],
        }
        prior_evidence = dict(worker_task.metadata.get("review_evidence", {}) or {})
        manager_dispatch = dict(prior_evidence.get("manager_dispatch", {}) or {})
        if manager_dispatch:
            # The report turn refreshes evidence after the worker completion.
            # Keep the attempt-scoped dispatch note that caused this parent to
            # enter review; it is audit context, not a scheduling predicate.
            evidence["manager_dispatch"] = manager_dispatch
        return evidence

    def _build_report_source_snapshot(self, worker_task: Task) -> dict[str, Any]:
        summary = self._task_summary_for_map(worker_task)
        result_content = ""
        if isinstance(worker_task.result, dict):
            result_content = str(worker_task.result.get("content", "") or "").strip()
        elif worker_task.result is not None:
            result_content = str(getattr(worker_task.result, "content", "") or "").strip()
        return {
            "summary": summary,
            "result_content": result_content,
            "evidence": self._build_review_evidence(
                worker_task,
                summary or result_content,
            ),
        }

    def _review_evidence_from_work_item(
        self,
        item: DelegationWorkItem,
        completion_report: str,
    ) -> dict[str, Any]:
        """Build recovery-safe evidence using only the durable WorkItem.

        The execute DONE path normally persisted a richer evidence snapshot
        already.  On restart that snapshot is authoritative; direct
        WorkItem-owned artifact/verification fields provide a minimal fallback
        if the process died before a runtime Task could contribute extras.
        """
        metadata = dict(getattr(item, "metadata", {}) or {})
        evidence = copy.deepcopy(dict(metadata.get("review_evidence", {}) or {}))
        evidence["completion_summary"] = str(completion_report or "").strip()
        evidence.setdefault(
            "artifact_manifest",
            self._normalize_work_item_artifact_index(
                metadata.get("work_item_artifact_index", [])
            )[:12],
        )
        evidence.setdefault("changed_areas", [])
        evidence.setdefault(
            "verification_results",
            {
                "status": dict(metadata.get("verification_status", {}) or {}),
                "checks": list(
                    dict(metadata.get("verification_evidence", {}) or {}).get(
                        "checks", []
                    )
                    or []
                )[:10],
            },
        )
        evidence.setdefault("key_commands", [])
        evidence.setdefault("output_paths", [])
        evidence.setdefault(
            "open_risks",
            [
                str(value).strip()
                for value in list(metadata.get("risks", []) or [])
                if str(value).strip()
            ][:10],
        )
        return evidence

    def _report_source_snapshot_from_work_item(
        self,
        item: DelegationWorkItem,
    ) -> dict[str, Any]:
        metadata = dict(getattr(item, "metadata", {}) or {})
        summary = str(
            metadata.get("completion_report", "")
            or metadata.get("work_item_summary_for_downstream", "")
            or getattr(item, "deliverable_summary", "")
            or getattr(item, "summary", "")
            or ""
        ).strip()
        return {
            "summary": summary,
            "result_content": str(metadata.get("report_source_result_content", "") or "").strip(),
            "evidence": self._review_evidence_from_work_item(item, summary),
        }

    @staticmethod
    def _review_approval_blocker_reason(review_metadata: dict[str, Any]) -> str:
        """Return a concrete reason to reject an internally contradictory approval.

        This intentionally only catches high-confidence contradictions: failed
        verification, blocked/partial artifact status, or an approval with no
        artifacts while the report explicitly says evidence is missing.
        """
        evidence = dict(review_metadata.get("review_evidence", {}) or {})
        artifact_manifest = [
            dict(item)
            for item in list(evidence.get("artifact_manifest", []) or [])
            if isinstance(item, dict)
        ]
        output_paths = [
            str(item).strip()
            for item in list(evidence.get("output_paths", []) or [])
            if str(item).strip()
        ]
        verification_results = dict(evidence.get("verification_results", {}) or {})
        verification_status = dict(verification_results.get("status", {}) or {})
        verification_label = str(verification_status.get("label", "") or "").strip().lower()
        verification_summary = str(verification_status.get("summary", "") or "").strip()
        if verification_label in {"failed", "fail", "blocked", "missing", "missing_evidence"}:
            return (
                "Reviewer approved the work, but verification evidence is "
                f"`{verification_label}`"
                + (f": {verification_summary}" if verification_summary else ".")
            )

        blocked_statuses = {"blocked", "partial", "failed", "missing"}
        artifact_statuses = [
            str(item.get("status", "") or "").strip().lower()
            for item in artifact_manifest
            if str(item.get("status", "") or "").strip()
        ]
        if artifact_statuses and all(status in blocked_statuses for status in artifact_statuses):
            return "Reviewer approved the work, but all known artifacts are marked blocked, partial, failed, or missing."

        report_text = str(
            review_metadata.get("review_completion_report")
            or review_metadata.get("completion_report")
            or ""
        ).strip().lower()
        missing_evidence_phrases = (
            "no evidence",
            "without evidence",
            "no artifact",
            "no artifacts",
            "not verified",
            "cannot verify",
            "unable to verify",
            "status: blocked",
            '"status":"blocked"',
            '"status": "blocked"',
        )
        if not artifact_manifest and not output_paths and any(phrase in report_text for phrase in missing_evidence_phrases):
            return "Reviewer approved the work, but the completion report says evidence or artifacts are missing."

        return ""

    async def _ensure_review_work_item_for_work_item(
        self,
        work_item_id: str,
        *,
        worker_task: Task | None = None,
        completion_report: str = "",
        metadata_updates: dict[str, Any] | None = None,
        source_report_item: DelegationWorkItem | None = None,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> DelegationWorkItem | None:
        """Ensure one active review card from durable WorkItem state.

        A runtime Task may enrich the initial evidence, but it is never the
        owner of lifecycle identity.  This lets restart reconciliation repair
        the chain without manufacturing a Task or trusting a lagging attempt
        counter on the parent.
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        target_work_item_id = str(work_item_id or "").strip()
        if not target_work_item_id:
            return None
        try:
            worker_item = await self.store.get_delegation_work_item(target_work_item_id)
        except Exception:
            worker_item = None
        if worker_item is None or worker_item.phase != Phase.AWAITING_MANAGER_REVIEW:
            return None
        worker_metadata = dict(worker_item.metadata or {})
        updates = dict(metadata_updates or {})
        task_metadata = dict(getattr(worker_task, "metadata", {}) or {})
        if source_report_item is not None:
            source_report_metadata = dict(source_report_item.metadata or {})
            if (
                str(source_report_metadata.get("report_target_work_item_id", "") or "").strip()
                != target_work_item_id
                or source_report_item.phase not in DONE_PHASES
                or str(source_report_metadata.get("report_card_outcome", "") or "").strip()
                != "applied"
            ):
                return None
        manager_role_id = str(
            updates.get("review_owner_role_id", "")
            or worker_metadata.get("review_owner_role_id", "")
            or worker_item.manager_role_id
            or ""
        ).strip()
        manager_seat_id = str(
            updates.get("review_owner_seat_id", "")
            or worker_metadata.get("review_owner_seat_id", "")
            or worker_item.manager_seat_id
            or ""
        ).strip()
        if not manager_role_id or not manager_seat_id:
            return None
        run_id = str(worker_item.run_id or "").strip()
        if not run_id:
            return None
        cell_id = str(worker_item.cell_id or "").strip()
        team_instance_id = str(worker_item.team_instance_id or "").strip()
        team_id = str(worker_item.team_id or worker_metadata.get("team_id", "") or "").strip()
        worker_role_id = str(worker_item.role_id or "").strip()
        worker_seat_id = str(worker_item.seat_id or "").strip()
        target_title = str(worker_item.title or target_work_item_id).strip()
        target_description = str(worker_item.summary or "").strip()
        target_prompt_contract = self._ensure_prompt_contract_on_work_item(
            worker_item,
            task_metadata=task_metadata or worker_metadata,
            task_description=str(getattr(worker_task, "description", "") or target_description).strip(),
        )
        if not has_prompt_contract(worker_metadata.get("prompt_contract")):
            try:
                await self.store.update_delegation_work_item(
                    target_work_item_id,
                    metadata_updates={"prompt_contract": target_prompt_contract},
                )
                worker_metadata = {**worker_metadata, "prompt_contract": target_prompt_contract}
            except Exception:
                logger.opt(exception=True).debug("Best-effort target prompt_contract snapshot update failed")
        review_prompt_contract = make_prompt_contract(
            task_brief=(
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            ),
            target_contract=target_prompt_contract,
            source={"kind": "review_auxiliary_work_item"},
        )
        all_run_items = await self._run_items_for_parent(worker_item, run_items)
        source_metadata = dict(getattr(source_report_item, "metadata", {}) or {})
        source_report_id = str(
            getattr(source_report_item, "work_item_id", "")
            or updates.get("review_source_report_work_item_id", "")
            or ""
        ).strip()
        durable_completion = str(
            completion_report
            or source_metadata.get("completion_report", "")
            or worker_metadata.get("completion_report", "")
            or ""
        ).strip()
        if isinstance(source_metadata.get("review_evidence"), dict):
            review_evidence = copy.deepcopy(source_metadata["review_evidence"])
        elif worker_task is not None:
            review_evidence = self._build_review_evidence(worker_task, durable_completion)
        else:
            review_evidence = self._review_evidence_from_work_item(
                worker_item,
                durable_completion,
            )
        # A report turn owns the handoff while it is active. Creating review
        # in parallel would let the reviewer consume an incomplete payload.
        if self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="report",
        ) is not None:
            return None
        existing_card = self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="review",
        )
        if existing_card is not None:
            existing_source_id = str(
                (existing_card.metadata or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or ""
            ).strip()
            if source_report_id and existing_source_id != source_report_id:
                closed = await self._persist_terminal_review_card(
                    existing_card.work_item_id,
                    phase=Phase.CANCELLED,
                    outcome="superseded_by_newer_report",
                )
                if closed is None:
                    return None
                existing_card = None
            else:
                try:
                    return await self.store.update_delegation_work_item(
                        existing_card.work_item_id,
                        summary=(
                            "Review the completed child deliverable and decide whether to "
                            "approve it or request rework."
                        ),
                        metadata_updates={
                            "review_completion_report": durable_completion,
                            "review_evidence": review_evidence,
                            "review_source_report_work_item_id": source_report_id,
                            "review_target_prompt_contract": target_prompt_contract,
                            "prompt_contract": review_prompt_contract,
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort in-flight review refresh failed")
                    return existing_card

        attempt_no = self._next_auxiliary_attempt(
            worker_item,
            all_run_items,
            kind="review",
        )
        review_work_item_id = review_work_item_id_for_attempt(target_work_item_id, attempt_no)
        worker_task_id = str(
            getattr(worker_task, "id", "")
            or worker_metadata.get("worker_task_id", "")
            or worker_metadata.get("claimed_task_id", "")
            or ""
        ).strip()
        session_scope_id = str(worker_metadata.get("session_scope_id", "") or "").strip()
        if worker_task is not None:
            session_scope_id = task_session_scope_id(worker_task) or session_scope_id
        review_metadata: dict[str, Any] = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "session_scope_id": session_scope_id,
            "delegation_turn_kind": "review",
            "work_kind": "review",
            "team_id": team_id,
            "seat_id": manager_seat_id,
            "review_task": True,
            "review_execution_work_item": True,
            "review_attempt": attempt_no,
            "review_owner_role_id": manager_role_id,
            "review_owner_seat_id": manager_seat_id,
            "review_target_work_item_id": target_work_item_id,
            "review_source_report_work_item_id": source_report_id,
            "review_target_worker_task_id": worker_task_id,
            "review_target_worker_role_id": worker_role_id,
            "review_target_worker_seat_id": worker_seat_id,
            "review_completion_report": durable_completion,
            "review_target_title": target_title,
            "review_target_description": target_description,
            "review_target_prompt_contract": target_prompt_contract,
            "review_evidence": review_evidence,
            "current_turn_mode": "review_execute",
            "prompt_contract": review_prompt_contract,
            "hidden_from_company_kanban": True,
            "user_visible": False,
            "authoritative_output": False,
            "skip_work_item_sync": True,
            **{
                key: copy.deepcopy(updates[key])
                for key in (
                    "review_retry_hint",
                    "review_retry_of_attempt",
                    "review_retry_reason",
                    "max_review_reworks",
                )
                if key in updates
            },
        }, version=work_item_runtime_version(worker_metadata)),
            projection_id=review_work_item_id,
            turn_type="review",
        )
        review_work_item = DelegationWorkItem(
            work_item_id=review_work_item_id,
            run_id=run_id,
            cell_id=cell_id,
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=manager_role_id,
            seat_id=manager_seat_id,
            parent_work_item_id=target_work_item_id,
            source_role_id=worker_role_id or None,
            source_seat_id=worker_seat_id or None,
            title=f"Review #{attempt_no}: {target_title}",
            summary=(
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            ),
            kind="review",
            projection_id=review_work_item_id,
            phase=Phase.READY,
            batch_id=f"review::{run_id}::{target_work_item_id}",
            batch_index=attempt_no,
            handoff_status="released",
            continuation_source="review_queue",
            manager_role_id=manager_role_id,
            manager_seat_id=manager_seat_id,
            metadata=review_metadata,
        )
        try:
            persisted_review, created = await self._insert_auxiliary_work_item_if_absent(
                review_work_item
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort review work-item create failed")
            return None
        if persisted_review is None:
            return None
        if not created and persisted_review.phase in DONE_PHASES:
            # The attempt number came from a stale dispatcher snapshot.  Do
            # not overwrite immutable history; the next reconcile pass will
            # observe it and choose the following deterministic attempt.
            return None
        # Cache only. Card identity remains authoritative if this write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={"review_attempt_count": attempt_no},
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort review_attempt_count update failed")
        return persisted_review

    async def _ensure_report_work_item_for_work_item(
        self,
        work_item_id: str,
        *,
        worker_task: Task | None = None,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> DelegationWorkItem | None:
        """Upsert a hidden report-generation work item that drives the
        worker's handoff turn before the reviewer is invoked.

        Two-turn worker→review handoff: instead of treating the worker's
        last execute-turn prose as the completion report (which produced
        unstable, sometimes self-contradicting reports), the runtime
        spawns a separate hidden card that resumes the same worker
        session under a dedicated report-generation prompt. The worker
        produces a structured (or narrative) handoff, and only then does
        the runtime spawn the review card.

        Mirrors ``_ensure_review_work_item_for_work_item`` for per-attempt
        idempotent spawn / refresh semantics. The assignee is the worker
        itself (NOT the manager), because the worker is the one writing
        the report on its own session context.
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        target_work_item_id = str(work_item_id or "").strip()
        if not target_work_item_id:
            return None
        try:
            worker_item = await self.store.get_delegation_work_item(target_work_item_id)
        except Exception:
            worker_item = None
        if worker_item is None or worker_item.phase != Phase.AWAITING_MANAGER_REVIEW:
            if worker_item is not None:
                await self._record_work_item_runtime_diagnostic(
                    code="report_parent_not_awaiting_review",
                    severity="info",
                    work_item=worker_item,
                    task=worker_task,
                    message="A WorkItem outside manager-review phase does not spawn a report card.",
                    details={"parent_phase": worker_item.phase.value},
                    warn=False,
                )
            return None
        worker_metadata = dict(worker_item.metadata or {})
        task_metadata = dict(getattr(worker_task, "metadata", {}) or {})
        run_id = str(worker_item.run_id or "").strip()
        if not run_id:
            return None
        cell_id = str(worker_item.cell_id or "").strip()
        team_instance_id = str(worker_item.team_instance_id or "").strip()
        team_id = str(worker_item.team_id or worker_metadata.get("team_id", "") or "").strip()
        worker_role_id = str(worker_item.role_id or "").strip()
        worker_seat_id = str(worker_item.seat_id or "").strip()
        if not worker_role_id or not worker_seat_id:
            return None
        manager_role_id = str(
            worker_metadata.get("review_owner_role_id", "")
            or worker_item.manager_role_id
            or ""
        ).strip()
        manager_seat_id = str(
            worker_metadata.get("review_owner_seat_id", "")
            or worker_item.manager_seat_id
            or ""
        ).strip()
        target_title = str(worker_item.title or target_work_item_id).strip()
        target_description = str(worker_item.summary or "").strip()
        target_prompt_contract = self._ensure_prompt_contract_on_work_item(
            worker_item,
            task_metadata=task_metadata or worker_metadata,
            task_description=str(getattr(worker_task, "description", "") or target_description).strip(),
        )
        if not has_prompt_contract(worker_metadata.get("prompt_contract")):
            try:
                await self.store.update_delegation_work_item(
                    target_work_item_id,
                    metadata_updates={"prompt_contract": target_prompt_contract},
                )
                worker_metadata = {**worker_metadata, "prompt_contract": target_prompt_contract}
            except Exception:
                logger.opt(exception=True).debug("Best-effort target prompt_contract update before report failed")

        report_prompt_contract = make_prompt_contract(
            task_brief=(
                "Write a structured handoff report for the deliverable you just "
                "completed. Do not do new execution work."
            ),
            target_contract=target_prompt_contract,
            source={"kind": "report_auxiliary_work_item"},
        )

        all_run_items = await self._run_items_for_parent(worker_item, run_items)
        report_source = (
            self._build_report_source_snapshot(worker_task)
            if worker_task is not None
            else self._report_source_snapshot_from_work_item(worker_item)
        )
        if self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="review",
        ) is not None:
            return None
        existing_card = self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="report",
        )
        if existing_card is not None:
            try:
                await self.store.update_delegation_work_item(
                    existing_card.work_item_id,
                    metadata_updates={
                        "report_target_prompt_contract": target_prompt_contract,
                        "prompt_contract": report_prompt_contract,
                        "report_source_summary": report_source["summary"],
                        "report_source_result_content": report_source["result_content"],
                        "report_source_evidence": report_source["evidence"],
                    },
                )
            except Exception:
                logger.opt(exception=True).debug("Best-effort in-flight report refresh failed")
            return existing_card

        attempt_no = self._next_auxiliary_attempt(
            worker_item,
            all_run_items,
            kind="report",
        )
        report_id = report_work_item_id_for_attempt(target_work_item_id, attempt_no)
        worker_task_id = str(
            getattr(worker_task, "id", "")
            or worker_metadata.get("worker_task_id", "")
            or worker_metadata.get("claimed_task_id", "")
            or ""
        ).strip()
        session_scope_id = str(worker_metadata.get("session_scope_id", "") or "").strip()
        if worker_task is not None:
            session_scope_id = task_session_scope_id(worker_task) or session_scope_id
        report_metadata: dict[str, Any] = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "session_scope_id": session_scope_id,
            "delegation_turn_kind": "report",
            "work_kind": "report",
            "team_id": team_id,
            "seat_id": worker_seat_id,
            "report_execution_work_item": True,
            "report_attempt": attempt_no,
            "report_target_work_item_id": target_work_item_id,
            "report_target_worker_task_id": worker_task_id,
            "report_target_worker_role_id": worker_role_id,
            "report_target_worker_seat_id": worker_seat_id,
            "report_target_title": target_title,
            "report_target_description": target_description,
            "report_target_prompt_contract": target_prompt_contract,
            "report_source_summary": report_source["summary"],
            "report_source_result_content": report_source["result_content"],
            "report_source_evidence": report_source["evidence"],
            "manager_role_id": manager_role_id,
            "manager_seat_id": manager_seat_id,
            "current_turn_mode": "report_required",
            "prompt_contract": report_prompt_contract,
            "hidden_from_company_kanban": True,
            "user_visible": False,
            "authoritative_output": False,
            "skip_work_item_sync": True,
        }, version=work_item_runtime_version(worker_metadata)),
            projection_id=report_id,
            turn_type="report",
        )
        report_work_item = DelegationWorkItem(
            work_item_id=report_id,
            run_id=run_id,
            cell_id=cell_id,
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=worker_role_id,
            seat_id=worker_seat_id,
            parent_work_item_id=target_work_item_id,
            source_role_id=worker_role_id or None,
            source_seat_id=worker_seat_id or None,
            title=f"Report #{attempt_no}: {target_title}",
            summary=(
                "Write a structured handoff report for the deliverable you just "
                "completed. The reviewer will independently verify your claims."
            ),
            kind="report",
            projection_id=report_id,
            phase=Phase.READY,
            batch_id=f"report::{run_id}::{target_work_item_id}",
            batch_index=attempt_no,
            handoff_status="released",
            continuation_source="report_queue",
            manager_role_id=manager_role_id,
            manager_seat_id=manager_seat_id,
            metadata=report_metadata,
        )
        try:
            persisted_report, created = await self._insert_auxiliary_work_item_if_absent(
                report_work_item
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort report work-item create failed")
            return None
        if persisted_report is None:
            return None
        if not created and persisted_report.phase in DONE_PHASES:
            # A concurrent writer already completed this deterministic
            # attempt.  Preserve it and let fresh reconciliation advance.
            return None
        # Cache only. Card identity remains authoritative if this write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={"report_attempt_count": attempt_no},
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort report_attempt_count update failed")
        return persisted_report

    async def _release_auxiliary_claim_for_retry(
        self,
        work_item_id: str,
    ) -> None:
        """Release a failed terminal-write claim so the live loop can retry.

        Startup recovery already clears stale claims.  This is the equivalent
        same-process recovery path for the narrow window where an auxiliary
        turn completed but its durable terminal journal write failed.
        """
        if not self.store or not work_item_id:
            return
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
                metadata_updates={
                    "claimed_by_role_session_id": "",
                    "claimed_task_id": "",
                },
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to release auxiliary WorkItem claim for retry: "
                f"{work_item_id}"
            )

    @staticmethod
    def _build_review_resolution(
        *,
        review_work_item_id: str,
        target_work_item_id: str,
        target_phase: Phase,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
        review_outcome: str,
        source_report_work_item_id: str,
    ) -> dict[str, Any]:
        """Build the immutable verdict journal persisted on a review card."""
        child_updates = copy.deepcopy(dict(metadata_updates or {}))
        # This stamp is committed atomically with the child phase projection.
        # It prevents a completed rework cycle from replaying an old verdict
        # when the same child later re-enters AWAITING_MANAGER_REVIEW.
        child_updates["review_resolution_applied_work_item_id"] = review_work_item_id
        return {
            "target_work_item_id": target_work_item_id,
            "target_phase": target_phase.value,
            "blocked_reason": str(blocked_reason or ""),
            "metadata_updates": child_updates,
            "review_outcome": str(review_outcome or "").strip(),
            "source_report_work_item_id": str(source_report_work_item_id or "").strip(),
            "decided_at": datetime.now().isoformat(),
        }

    async def _persist_terminal_review_resolution(
        self,
        *,
        review_work_item_id: str,
        target_work_item_id: str,
        target_phase: Phase,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
        review_outcome: str,
        source_report_work_item_id: str,
    ) -> DelegationWorkItem | None:
        """Commit a review verdict before projecting it to the child."""
        resolution = self._build_review_resolution(
            review_work_item_id=review_work_item_id,
            target_work_item_id=target_work_item_id,
            target_phase=target_phase,
            blocked_reason=blocked_reason,
            metadata_updates=metadata_updates,
            review_outcome=review_outcome,
            source_report_work_item_id=source_report_work_item_id,
        )
        return await self._persist_terminal_review_card(
            review_work_item_id,
            phase=Phase.APPROVED,
            outcome=review_outcome,
            resolution=resolution,
        )

    async def _persist_terminal_review_card(
        self,
        review_work_item_id: str,
        *,
        phase: Phase,
        outcome: str,
        resolution: dict[str, Any] | None = None,
    ) -> DelegationWorkItem | None:
        """Persist a terminal review card or release its claim for retry."""
        metadata_updates: dict[str, Any] = {
            "claimed_by_role_session_id": "",
            "claimed_task_id": "",
            "review_work_item_outcome": outcome,
            "last_review_turn_finished_at": datetime.now().isoformat(),
        }
        if resolution is not None:
            metadata_updates["review_resolution"] = copy.deepcopy(resolution)
            metadata_updates["review_resolution_state"] = "pending"
        try:
            current = None
            if hasattr(self.store, "get_delegation_work_item"):
                current = await self.store.get_delegation_work_item(review_work_item_id)
            update_kwargs: dict[str, Any] = {
                "claimed_by_role_runtime_session_id": "",
                "claimed_by_seat_id": "",
                "metadata_updates": metadata_updates,
            }
            if current is None or current.phase not in DONE_PHASES or current.phase == phase:
                update_kwargs["phase"] = phase
            persisted = await self.store.update_delegation_work_item(
                review_work_item_id,
                **update_kwargs,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "review_done: failed to persist terminal review card"
            )
            persisted = None
        if persisted is None:
            await self._release_auxiliary_claim_for_retry(review_work_item_id)
        return persisted

    async def _mark_review_resolution_stale(
        self,
        review_item: DelegationWorkItem,
        *,
        reason: str,
    ) -> None:
        """Retire a late verdict without mutating its target WorkItem."""
        try:
            await self.store.update_delegation_work_item(
                review_item.work_item_id,
                metadata_updates={
                    "review_resolution_state": "stale",
                    "review_resolution_stale_reason": str(reason or "").strip(),
                    "review_resolution_stale_at": datetime.now().isoformat(),
                    "review_work_item_outcome": (
                        "target_no_longer_awaiting_manager_review"
                        if reason == "target_phase_changed"
                        else "superseded_by_newer_report"
                    ),
                },
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to mark stale review resolution: "
                f"{review_item.work_item_id}"
            )

    async def _current_review_resolution_target(
        self,
        *,
        target_work_item_id: str,
        source_report_work_item_id: str,
    ) -> tuple[DelegationWorkItem | None, str]:
        """Return the authoritative target and an empty reason when current."""
        try:
            target = await self.store.get_delegation_work_item(
                target_work_item_id
            )
        except Exception:
            target = None
        if target is None or target.phase != Phase.AWAITING_MANAGER_REVIEW:
            return target, "target_phase_changed"
        run_items = await self._run_items_for_parent(target)
        applied_reports = [
            report
            for report in self._targeting_auxiliary_items(
                run_items,
                target_work_item_id,
                kind="report",
            )
            if report.phase in DONE_PHASES
            and str(
                (report.metadata or {}).get("report_card_outcome", "") or ""
            ).strip()
            == "applied"
        ]
        latest_report = applied_reports[-1] if applied_reports else None
        if latest_report is None and source_report_work_item_id:
            return target, "source_report_missing"
        if latest_report is not None and (
            source_report_work_item_id != latest_report.work_item_id
        ):
            return target, "source_report_superseded"
        return target, ""

    async def _apply_review_resolution(
        self,
        review_item: DelegationWorkItem,
        target_item: DelegationWorkItem,
    ) -> DelegationWorkItem | None:
        """Idempotently project a durable terminal review onto its child."""
        if review_item.phase not in DONE_PHASES:
            return None
        review_metadata = dict(review_item.metadata or {})
        if str(review_metadata.get("review_resolution_state", "") or "").strip() == "stale":
            return None
        resolution = dict(review_metadata.get("review_resolution", {}) or {})
        target_work_item_id = str(
            resolution.get("target_work_item_id", "") or ""
        ).strip()
        if not target_work_item_id or target_work_item_id != target_item.work_item_id:
            return None
        resolution_source_id = str(
            resolution.get("source_report_work_item_id", "") or ""
        ).strip()
        review_source_id = str(
            review_metadata.get("review_source_report_work_item_id", "") or ""
        ).strip()
        if resolution_source_id and review_source_id and (
            resolution_source_id != review_source_id
        ):
            await self._mark_review_resolution_stale(
                review_item,
                reason="source_report_superseded",
            )
            return None
        source_report_work_item_id = resolution_source_id or review_source_id
        authoritative_target, stale_reason = (
            await self._current_review_resolution_target(
                target_work_item_id=target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
            )
        )
        if stale_reason:
            await self._mark_review_resolution_stale(
                review_item,
                reason=stale_reason,
            )
            return None
        if authoritative_target is None:
            return None
        if str(
            (authoritative_target.metadata or {}).get(
                "review_resolution_applied_work_item_id", ""
            )
            or ""
        ).strip() == review_item.work_item_id:
            return authoritative_target
        try:
            target_phase = Phase(str(resolution.get("target_phase", "") or ""))
        except ValueError:
            return None
        if target_phase not in {
            Phase.APPROVED,
            Phase.READY_FOR_REWORK,
            Phase.AWAITING_HUMAN,
        }:
            return None
        metadata_updates = resolution.get("metadata_updates", {})
        if not isinstance(metadata_updates, dict):
            return None
        metadata_updates = copy.deepcopy(metadata_updates)
        metadata_updates["review_resolution_applied_work_item_id"] = (
            review_item.work_item_id
        )
        atomic_apply = getattr(
            self.store,
            "apply_delegation_review_resolution",
            None,
        )
        if callable(atomic_apply):
            applied = await atomic_apply(
                target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
                target_phase=target_phase,
                blocked_reason=str(resolution.get("blocked_reason", "") or ""),
                metadata_updates=metadata_updates,
            )
        else:
            applied = await self.store.update_delegation_work_item(
                target_work_item_id,
                phase=target_phase,
                blocked_reason=str(resolution.get("blocked_reason", "") or ""),
                metadata_updates=metadata_updates,
            )
        if applied is None:
            _target, stale_reason = await self._current_review_resolution_target(
                target_work_item_id=target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
            )
            if stale_reason:
                await self._mark_review_resolution_stale(
                    review_item,
                    reason=stale_reason,
                )
            return None
        try:
            await self.store.update_delegation_work_item(
                review_item.work_item_id,
                metadata_updates={"review_resolution_state": "applied"},
            )
        except Exception:
            logger.opt(exception=True).debug(
                "Best-effort review resolution applied marker failed"
            )
        return applied

    async def _finalize_review_work_item(self, review_task: Task) -> None:
        """Apply the review verdict to the child work item and close the
        hidden review card.

        The runtime is intentionally minimal here:

        * If the verdict has a parseable ``approve`` / ``reject`` label,
          apply it mechanically unless the approve is internally
          contradictory with explicit blocked/missing evidence. Reject
          cycles as machine-readable rework; non-final review never escalates
          to human review.
        * If the verdict cannot be parsed at all (no extractable label),
          retry the reviewer with a parse-failure hint. After
          ``MAX_VERDICT_PARSE_RETRIES``, close the review as done/approved
          with audit metadata instead of sending the worker back for rework.

        The runtime does NOT inspect issue counts, summary length, or prose
        quality, and does NOT silently flip reject to approve. It only blocks
        high-confidence contradictory approvals where evidence says blocked,
        failed, or missing.

        Durability contract: persist the terminal review card and complete
        resolution first, then project that resolution to the child. The
        parent's atomic applied stamp makes reconcile replay safe across both
        process crashes and later rework cycles.
        """
        if not self.store:
            await self._notify_kanban_changed()
            return
        review_work_item_id = linked_work_item_id_for_task(review_task)
        review_item = None
        if review_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                review_item = await self.store.get_delegation_work_item(
                    review_work_item_id
                )
            except Exception:
                review_item = None
        review_metadata = {
            **dict(getattr(review_item, "metadata", {}) or {}),
            **dict(review_task.metadata or {}),
            **self._work_item_output_metadata_for_task(review_task),
        }
        target_work_item_id = str(review_metadata.get("review_target_work_item_id", "") or "").strip()
        if not review_work_item_id or not target_work_item_id:
            await self._notify_kanban_changed()
            return
        child_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                child_item = await self.store.get_delegation_work_item(target_work_item_id)
            except Exception:
                child_item = None
        child_phase = child_item.phase if child_item is not None else None
        if child_phase != Phase.AWAITING_MANAGER_REVIEW:
            # A manager verdict is valid only for the exact passive phase it
            # was created to consume. In particular, a late manager turn must
            # never jump over an AWAITING_HUMAN decision.
            await self._persist_terminal_review_card(
                review_work_item_id,
                phase=Phase.APPROVED,
                outcome="target_no_longer_awaiting_manager_review",
            )
            await self._notify_kanban_changed()
            return
        verdict = self._normalize_review_verdict(review_metadata.get("structured_review_verdict"))
        verdict_label = str(verdict.get("label", "") or "").strip().lower() if verdict else ""
        approval_blocker_reason = (
            self._review_approval_blocker_reason(review_metadata)
            if verdict_label == "approve"
            else ""
        )
        if approval_blocker_reason:
            verdict = {
                "label": "reject",
                "summary": "Approval withheld because the report or evidence is internally contradictory.",
                "blocking_issues": [approval_blocker_reason],
                "followups": [],
            }
            verdict_label = "reject"
            review_task.metadata = dict(review_task.metadata or {})
            review_task.metadata["structured_review_verdict"] = verdict
            review_metadata["structured_review_verdict"] = verdict

        # Verdict-parse retry: if the reviewer didn't emit a parseable
        # approve/reject label, tell the reviewer and give them another
        # review turn. Beyond MAX_VERDICT_PARSE_RETRIES, close the review
        # without reworking the child; parse failures are reviewer-side
        # output failures, not worker deliverable failures.
        if verdict_label not in {"approve", "reject"}:
            prior_parse_retries = await self._durable_review_parse_retry_count(
                child_item,
            )
            if prior_parse_retries < MAX_VERDICT_PARSE_RETRIES:
                retry_spawned = await self._retry_verdict_parse_failed(
                    review_task=review_task,
                    review_work_item_id=review_work_item_id,
                    target_work_item_id=target_work_item_id,
                    new_retry_count=prior_parse_retries + 1,
                )
                if retry_spawned:
                    await self._notify_kanban_changed()
                    return
                logger.warning(
                    "verdict-parse-retry spawn deferred; keeping child in review "
                    f"child={target_work_item_id}"
                )
                # The prior review card is either still retryable or already
                # a terminal parse-failure journal. Reconciliation can create
                # the next review; a transient card write must never approve
                # the worker output.
                await self._notify_kanban_changed()
                return
            # Retry budget is explicitly exhausted. Do not send the child
            # back to the worker for a reviewer formatting problem.
            auto_done_reason = (
                f"Reviewer produced an unparseable verdict {prior_parse_retries + 1} time(s); "
                "runtime is closing the review as done instead of requesting worker rework."
            )
            auto_close_verdict = {
                "label": "approve",
                "summary": "Auto-closed because reviewer verdict was unparseable after retry budget.",
                "blocking_issues": [],
                "followups": [
                    "Inspect reviewer output formatting; the worker was not reworked for this reviewer-side failure."
                ],
            }
            child_metadata_updates: dict[str, Any] = {
                "reviewed_at": datetime.now().isoformat(),
                "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                "review_verdict_parse_retry_count": prior_parse_retries + 1,
                "review_feedback_updated_at": datetime.now().isoformat(),
                "review_verdict_parse_failed_auto_done": True,
                "review_parse_failure_feedback": auto_done_reason,
                "rework_feedback": "",
                "structured_review_verdict": auto_close_verdict,
            }
            source_report_work_item_id = str(
                dict(getattr(review_item, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or review_metadata.get("review_source_report_work_item_id", "")
                or ""
            ).strip()
            persisted_review = await self._persist_terminal_review_resolution(
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                target_phase=Phase.APPROVED,
                blocked_reason="",
                metadata_updates=child_metadata_updates,
                review_outcome="verdict_parse_failed_auto_done",
                source_report_work_item_id=source_report_work_item_id,
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
            try:
                applied_child = await self._apply_review_resolution(
                    persisted_review,
                    child_item,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "_finalize_review_work_item: failed to project auto-close resolution"
                )
                applied_child = None
            if applied_child is None:
                # The terminal review is the recovery point; reconcile will
                # project it without rerunning the reviewer.
                await self._notify_kanban_changed()
                return
            child_item = applied_child
            child_phase = applied_child.phase
            await self._ack_lifecycle_inbox_for_review(
                review_task=review_task,
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                child_item=child_item,
            )
            await self._notify_kanban_changed()
            return

        decision = "approve" if verdict_label == "approve" else "rework"
        next_phase = Phase.APPROVED if decision == "approve" else Phase.READY_FOR_REWORK
        review_outcome = decision
        persisted_review: DelegationWorkItem | None = None

        # Apply the verdict to the child work item if it is still
        # awaiting review. If the child already moved on, skip the
        # mutation but still finalize the hidden review item.
        if child_phase == Phase.AWAITING_MANAGER_REVIEW:
            feedback = self._review_feedback_with_fallback(review_task)
            prior_feedback_version = self._review_feedback_version(
                dict(getattr(child_item, "metadata", {}) or {})
            )
            child_metadata_updates = {
                "reviewed_at": datetime.now().isoformat(),
                "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                "rework_feedback": "" if next_phase == Phase.APPROVED else feedback,
                "structured_review_verdict": verdict or {},
            }
            escalation_reason: str | None = None
            if next_phase == Phase.READY_FOR_REWORK:
                prior_rework_count = int(
                    dict(getattr(child_item, "metadata", {}) or {}).get(
                        "review_rework_count", 0
                    ) or 0
                )
                # Configurable cap on rework cycles. Default 5; either
                # the review task or the child's metadata may override.
                max_review_reworks = self._resolve_max_review_reworks(
                    review_task=review_task, child_item=child_item
                )
                if prior_rework_count >= max_review_reworks:
                    auto_done_reason = (
                        f"Rework count ({prior_rework_count}) reached the configured "
                        f"cap of {max_review_reworks}; marking the work item done instead "
                        f"of requesting another rework. Latest reviewer feedback:\n{feedback}"
                    ).strip()
                    next_phase = Phase.APPROVED
                    review_outcome = "auto_done_rework_cap"
                    child_metadata_updates["rework_feedback"] = ""
                    child_metadata_updates["review_rework_cap_reached_auto_done"] = True
                    child_metadata_updates["review_rework_cap"] = max_review_reworks
                    child_metadata_updates["review_rework_count_at_auto_done"] = prior_rework_count
                    child_metadata_updates["review_rework_cap_feedback"] = auto_done_reason
                else:
                    child_metadata_updates["review_rework_count"] = prior_rework_count + 1
                    child_metadata_updates["review_feedback_version"] = prior_feedback_version + 1
                    child_metadata_updates["review_feedback_updated_at"] = datetime.now().isoformat()
            elif next_phase == Phase.APPROVED:
                child_metadata_updates["review_rework_count"] = 0
            blocked_reason = (
                ""
                if next_phase == Phase.APPROVED
                else str(escalation_reason or "")
            )
            source_report_work_item_id = str(
                dict(getattr(review_item, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or review_metadata.get("review_source_report_work_item_id", "")
                or ""
            ).strip()
            persisted_review = await self._persist_terminal_review_resolution(
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                target_phase=next_phase,
                blocked_reason=blocked_reason,
                metadata_updates=child_metadata_updates,
                review_outcome=review_outcome,
                source_report_work_item_id=source_report_work_item_id,
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
            try:
                applied_child = await self._apply_review_resolution(
                    persisted_review,
                    child_item,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "_finalize_review_work_item: failed to project review resolution"
                )
                applied_child = None
            if applied_child is None:
                # The verdict is already durable. A later reconcile pass will
                # replay this exact resolution onto the still-waiting child.
                await self._notify_kanban_changed()
                return
            child_item = applied_child
            child_phase = applied_child.phase
            if (
                next_phase == Phase.AWAITING_HUMAN
                and child_phase == Phase.AWAITING_HUMAN
                and feedback
            ):
                try:
                    target_task = await self._load_review_target_task(
                        review_task=review_task,
                        child_item=child_item,
                    )
                    if target_task is not None:
                        target_task = copy.deepcopy(target_task)
                        target_task.metadata = dict(target_task.metadata or {})
                        target_task.metadata.update({
                            "rework_feedback": feedback,
                            "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                            "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                            "review_feedback_version": int(
                                child_metadata_updates.get("review_feedback_version", prior_feedback_version) or 0
                            ),
                        })
                        if callable(getattr(self, "save_task", None)):
                            await self.save_task(target_task)
                        await self._save_review_rework_human_checkpoint(
                            target_task,
                            feedback=feedback,
                            review_owner_role_id=str(child_item.manager_role_id or "").strip(),
                            review_feedback_version=int(
                                child_metadata_updates.get("review_feedback_version", prior_feedback_version) or 0
                            ),
                            escalation_reason=escalation_reason or "",
                        )
                except Exception:
                    logger.opt(exception=True).warning(
                        "_finalize_review_work_item: failed to persist human-intervention checkpoint"
                    )
        if persisted_review is None:
            # The target moved before this reviewer finished. Close the
            # auxiliary card, but never apply a stale verdict to that newer
            # target state.
            persisted_review = await self._persist_terminal_review_card(
                review_work_item_id,
                phase=Phase.APPROVED,
                outcome="target_no_longer_awaiting_review",
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
        await self._ack_lifecycle_inbox_for_review(
            review_task=review_task,
            review_work_item_id=review_work_item_id,
            target_work_item_id=target_work_item_id,
            child_item=child_item,
        )
        # Dispatcher wake: a rework decision reopens the child on the
        # worker seat; signal the main loop so the rework turn starts
        # without waiting for the next gather batch.
        if next_phase == Phase.READY_FOR_REWORK:
            try:
                self._signal_dispatcher_wake()
            except Exception:
                logger.opt(exception=True).debug("_signal_dispatcher_wake failed")
        if child_phase == Phase.APPROVED:
            await self._refresh_delegation_dependents(review_task)
        await self._notify_kanban_changed()

    async def _ack_lifecycle_inbox_for_review(
        self,
        *,
        review_task: Task,
        review_work_item_id: str,
        target_work_item_id: str,
        child_item: Any | None = None,
    ) -> None:
        """Archive protocol mail that was consumed by a review-card verdict."""
        if not self.communication:
            return
        service_factory = getattr(self.communication, "_collaboration_service", None)
        if not callable(service_factory):
            return
        role_id = str(
            review_task.assigned_to
            or (review_task.metadata or {}).get("work_item_role_id", "")
            or ""
        ).strip()
        if not role_id:
            return
        review_metadata = dict(review_task.metadata or {})
        child_metadata = dict(getattr(child_item, "metadata", {}) or {})
        task_ids = {
            str(review_task.id or "").strip(),
            str(review_metadata.get("review_target_worker_task_id", "") or "").strip(),
            str(review_metadata.get("report_target_worker_task_id", "") or "").strip(),
            str(review_metadata.get("task_id", "") or "").strip(),
        }
        work_item_ids = {
            str(target_work_item_id or "").strip(),
            str(review_work_item_id or "").strip(),
            str(getattr(child_item, "work_item_id", "") or "").strip(),
            str(getattr(child_item, "parent_work_item_id", "") or "").strip(),
            str(review_metadata.get("review_target_work_item_id", "") or "").strip(),
            str(review_metadata.get("report_target_work_item_id", "") or "").strip(),
        }
        cleanup_items_by_id: dict[str, Any] = {}
        root_work_item_ids = {item for item in work_item_ids if item}
        if child_item is not None:
            child_id = str(getattr(child_item, "work_item_id", "") or "").strip()
            if child_id:
                cleanup_items_by_id[child_id] = child_item

        def _phase_value(item: Any) -> str:
            phase = getattr(item, "phase", "")
            return str(getattr(phase, "value", phase) or "").strip()

        def _cleanup_phase_eligible(item: Any) -> bool:
            eligible = {phase.value for phase in DONE_PHASES}
            eligible.add(Phase.AWAITING_HUMAN.value)
            return _phase_value(item) in eligible

        if self.store and hasattr(self.store, "list_delegation_work_items"):
            run_id = str(
                getattr(child_item, "run_id", "")
                or review_metadata.get("delegation_run_id", "")
                or review_metadata.get("run_id", "")
                or ""
            ).strip()
            if run_id:
                try:
                    all_items = await self.store.list_delegation_work_items(run_id)
                except Exception:
                    all_items = []
                by_id = {
                    str(getattr(item, "work_item_id", "") or "").strip(): item
                    for item in list(all_items or [])
                    if str(getattr(item, "work_item_id", "") or "").strip()
                }
                by_parent: dict[str, list[Any]] = {}
                for item in list(all_items or []):
                    parent_id = str(getattr(item, "parent_work_item_id", "") or "").strip()
                    if parent_id:
                        by_parent.setdefault(parent_id, []).append(item)
                for root_id in list(root_work_item_ids):
                    item = by_id.get(root_id)
                    if item is not None:
                        cleanup_items_by_id.setdefault(root_id, item)
                stack = list(root_work_item_ids)
                visited: set[str] = set()
                while stack:
                    current_id = stack.pop()
                    if not current_id or current_id in visited:
                        continue
                    visited.add(current_id)
                    for descendant in by_parent.get(current_id, []):
                        descendant_id = str(getattr(descendant, "work_item_id", "") or "").strip()
                        if not descendant_id:
                            continue
                        stack.append(descendant_id)
                        if _cleanup_phase_eligible(descendant):
                            cleanup_items_by_id.setdefault(descendant_id, descendant)

        def _safe_attempt(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        attempt_limits = [
            _safe_attempt(child_metadata.get("report_attempt_count", 0)),
            _safe_attempt(child_metadata.get("review_attempt_count", 0)),
            _safe_attempt(review_metadata.get("report_attempt", 0)),
            _safe_attempt(review_metadata.get("review_attempt", 0)),
            1,
        ]
        for cleanup_item in cleanup_items_by_id.values():
            item_id = str(getattr(cleanup_item, "work_item_id", "") or "").strip()
            if item_id:
                work_item_ids.add(item_id)
            parent_id = str(getattr(cleanup_item, "parent_work_item_id", "") or "").strip()
            if parent_id:
                work_item_ids.add(parent_id)
            item_metadata = dict(getattr(cleanup_item, "metadata", {}) or {})
            attempt_limits.extend([
                _safe_attempt(item_metadata.get("report_attempt_count", 0)),
                _safe_attempt(item_metadata.get("review_attempt_count", 0)),
                _safe_attempt(item_metadata.get("report_attempt", 0)),
                _safe_attempt(item_metadata.get("review_attempt", 0)),
            ])
            for key in (
                "claimed_task_id",
                "task_id",
                "completion_task_id",
                "review_target_worker_task_id",
                "report_target_worker_task_id",
            ):
                value = str(item_metadata.get(key, "") or "").strip()
                if value:
                    task_ids.add(value)
        for base_id in list(work_item_ids):
            if not base_id:
                continue
            max_attempt = max(attempt_limits or [1])
            for attempt in range(1, max_attempt + 1):
                work_item_ids.add(report_work_item_id_for_attempt(base_id, attempt))
                work_item_ids.add(review_work_item_id_for_attempt(base_id, attempt))
        projection_ids = {
            projection_id_for_task(review_task),
            str(review_metadata.get("work_item_projection_id", "") or "").strip(),
            str(review_metadata.get("review_target_projection_id", "") or "").strip(),
            str(getattr(child_item, "projection_id", "") or "").strip(),
            projection_id_for_work_item(child_item) if child_item is not None else "",
        }
        for cleanup_item in cleanup_items_by_id.values():
            projection_ids.add(str(getattr(cleanup_item, "projection_id", "") or "").strip())
            projection_ids.add(projection_id_for_work_item(cleanup_item))
        try:
            service = service_factory()
            ack_by_refs = getattr(service, "ack_inbox_messages_by_refs", None)
            if not callable(ack_by_refs):
                return
            await ack_by_refs(
                CollaborationContext.from_task(review_task, role_id=role_id),
                agent_id=role_id,
                work_item_ids=sorted(item for item in work_item_ids if item),
                projection_ids=sorted(item for item in projection_ids if item),
                task_ids=sorted(item for item in task_ids if item),
                semantic_types=["approval_request", "blocker", "completion", "status_digest"],
                task=review_task,
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort lifecycle inbox cleanup after review failed")

    @staticmethod
    def _resolve_max_review_reworks(
        *,
        review_task: Task,
        child_item: Any,
    ) -> int:
        """Return the configured max-rework cap for this work item.

        Resolution order:
        1. Review task metadata: ``max_review_reworks`` (per-attempt
           override, e.g. set by a recovery/escalation flow).
        2. Child work-item metadata: ``max_review_reworks`` (per-task
           override, e.g. set by a manager when delegating).
        3. Module default ``DEFAULT_MAX_REVIEW_REWORKS`` (5).
        """
        for source in (
            dict(review_task.metadata or {}),
            dict(getattr(child_item, "metadata", {}) or {}),
        ):
            raw = source.get("max_review_reworks")
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return DEFAULT_MAX_REVIEW_REWORKS

    async def _retry_verdict_parse_failed(
        self,
        *,
        review_task: Task,
        review_work_item_id: str,
        target_work_item_id: str,
        new_retry_count: int,
    ) -> bool:
        """Spawn Review #N+1 because the previous reviewer turn produced a
        verdict the runtime could not parse into approve/reject.

        Distinct from worker rework: this is a reviewer-side output
        recovery, NOT a re-evaluation of the deliverable. Counts against
        ``review_verdict_parse_retry_count`` (independent budget from
        ``review_rework_count``).
        """
        if not self.store or not hasattr(self.store, "update_delegation_work_item"):
            return False
        review_metadata = dict(review_task.metadata or {})
        prior_review_item = None
        target_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                prior_review_item = await self.store.get_delegation_work_item(review_work_item_id)
            except Exception:
                prior_review_item = None
            try:
                target_item = await self.store.get_delegation_work_item(target_work_item_id)
            except Exception:
                target_item = None
        prior_review_metadata = dict(getattr(prior_review_item, "metadata", {}) or {})
        target_metadata = dict(getattr(target_item, "metadata", {}) or {})
        review_owner_role_id = str(
            prior_review_metadata.get("review_owner_role_id", "")
            or review_metadata.get("review_owner_role_id", "")
            or target_metadata.get("review_owner_role_id", "")
            or getattr(target_item, "manager_role_id", "")
            or ""
        ).strip()
        review_owner_seat_id = str(
            prior_review_metadata.get("review_owner_seat_id", "")
            or review_metadata.get("review_owner_seat_id", "")
            or target_metadata.get("review_owner_seat_id", "")
            or getattr(target_item, "manager_seat_id", "")
            or ""
        ).strip()
        worker_task_id = str(
            review_metadata.get("review_target_worker_task_id", "") or ""
        ).strip()
        worker_task = None
        if worker_task_id:
            try:
                worker_task = await self.store.get_task(worker_task_id)
            except Exception:
                worker_task = None
        retry_worker_task = copy.deepcopy(worker_task) if worker_task is not None else None
        if retry_worker_task is not None:
            retry_worker_task.metadata = dict(getattr(worker_task, "metadata", {}) or {})
            if target_item is not None:
                retry_worker_task.title = str(getattr(target_item, "title", "") or retry_worker_task.title or "")
                retry_worker_task.description = str(
                    getattr(target_item, "summary", "") or retry_worker_task.description or ""
                )
                retry_worker_task.assigned_to = str(
                    getattr(target_item, "role_id", "") or retry_worker_task.assigned_to or ""
                ).strip()
                retry_worker_task.metadata.update(build_work_item_owner_execution_copy(target_item))
            if review_owner_role_id:
                retry_worker_task.metadata["manager_role_id"] = review_owner_role_id
                retry_worker_task.metadata["review_owner_role_id"] = review_owner_role_id
            if review_owner_seat_id:
                retry_worker_task.metadata["manager_seat_id"] = review_owner_seat_id
                retry_worker_task.metadata["review_owner_seat_id"] = review_owner_seat_id

        source_report_item = None
        source_report_id = str(
            prior_review_metadata.get("review_source_report_work_item_id", "") or ""
        ).strip()
        if source_report_id:
            try:
                source_report_item = await self.store.get_delegation_work_item(source_report_id)
            except Exception:
                source_report_item = None

        persisted_prior = await self._persist_terminal_review_card(
            review_work_item_id,
            phase=Phase.CANCELLED,
            outcome="verdict_parse_failed",
        )
        if persisted_prior is None:
            return False

        # Cache only. The terminal review card above is the durable retry
        # count and recovery point if this parent metadata write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={
                    "review_verdict_parse_retry_count": new_retry_count,
                    "review_verdict_parse_retry_at": datetime.now().isoformat(),
                },
            )
        except Exception:
            logger.opt(exception=True).debug(
                "verdict-parse-retry: failed to stamp counter on child"
            )

        completion_report = str(
            review_metadata.get("review_completion_report", "") or ""
        ).strip()
        new_review_item = await self._ensure_review_work_item_for_work_item(
            target_work_item_id,
            worker_task=retry_worker_task,
            completion_report=completion_report,
            metadata_updates={
                "review_owner_role_id": review_owner_role_id,
                "review_owner_seat_id": review_owner_seat_id,
                "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                "review_retry_of_attempt": int(
                    review_metadata.get("review_attempt", 0) or 0
                ),
                "review_retry_reason": "verdict_parse_failed",
            },
            source_report_item=source_report_item,
        )
        if new_review_item is None:
            return False
        try:
            base_summary = str(getattr(new_review_item, "summary", "") or "").strip() or (
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            )
            await self.store.update_delegation_work_item(
                getattr(new_review_item, "work_item_id", ""),
                summary=base_summary + _REVIEW_VERDICT_PARSE_RETRY_HINT,
                metadata_updates={
                    "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                    "review_retry_reason": "verdict_parse_failed",
                    "review_retry_of_attempt": int(
                        review_metadata.get("review_attempt", 0) or 0
                    ),
                },
            )
        except Exception:
            logger.opt(exception=True).debug(
                "verdict-parse-retry: extending new summary failed"
            )

        try:
            self._signal_dispatcher_wake()
        except Exception:
            logger.opt(exception=True).debug("verdict-parse-retry: dispatcher wake failed")

        logger.info(
            f"verdict-parse-retry spawned: child={target_work_item_id} "
            f"retry_count={new_retry_count} "
            f"new_review={getattr(new_review_item, 'work_item_id', '?')}"
        )
        return True

    @staticmethod
    def _review_feedback_version(metadata: dict[str, Any] | None) -> int:
        payload = dict(metadata or {})
        for key in ("review_feedback_version", "review_rework_count"):
            try:
                parsed = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0

    async def _durable_review_parse_retry_count(
        self,
        parent_item: DelegationWorkItem,
    ) -> int:
        """Count reviewer format retries from parent cache and terminal cards."""
        cached = self._safe_positive_int(
            (parent_item.metadata or {}).get("review_verdict_parse_retry_count", 0)
        )
        run_items = await self._run_items_for_parent(parent_item)
        durable = sum(
            1
            for review_item in self._targeting_auxiliary_items(
                run_items,
                parent_item.work_item_id,
                kind="review",
            )
            if str(
                (review_item.metadata or {}).get("review_work_item_outcome", "")
                or ""
            ).strip()
            == "verdict_parse_failed"
        )
        return max(cached, durable)

    async def _load_review_target_task(
        self,
        *,
        review_task: Task,
        child_item: DelegationWorkItem | None,
    ) -> Task | None:
        if not self.store or not hasattr(self.store, "get_task"):
            return None
        candidate_task_ids: list[str] = []
        if child_item is not None:
            get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
            if callable(get_runtime_task):
                try:
                    linked_task = await get_runtime_task(str(getattr(child_item, "work_item_id", "") or "").strip())
                except Exception:
                    linked_task = None
                linked_task_id = str(getattr(linked_task, "id", "") or "").strip()
                if linked_task_id:
                    candidate_task_ids.append(linked_task_id)
        for raw in (
            (review_task.metadata or {}).get("review_target_worker_task_id"),
        ):
            value = str(raw or "").strip()
            if value and value not in candidate_task_ids:
                candidate_task_ids.append(value)
        for task_id in candidate_task_ids:
            try:
                target = await self.store.get_task(task_id)
            except Exception:
                target = None
            if target is not None:
                return target
        return None

    async def _save_review_rework_human_checkpoint(
        self,
        task: Task,
        *,
        feedback: str,
        review_owner_role_id: str,
        review_feedback_version: int,
        escalation_reason: str,
    ) -> None:
        if not self.checkpoint_callback:
            return

        pending_getter = getattr(self.store, "get_pending_checkpoints", None)
        if callable(pending_getter):
            try:
                pending = await pending_getter(
                    project_id=str(task.project_id or "default"),
                    session_id=str(task.session_id or "").strip() or None,
                    checkpoint_types=["task_user_input"],
                )
            except Exception:
                pending = []
            for checkpoint in pending:
                payload = dict(getattr(checkpoint, "payload", {}) or {})
                existing_task_id = str(
                    payload.get("task_id")
                    or payload.get("waiting_task_id")
                    or ""
                ).strip()
                if existing_task_id != str(task.id or "").strip():
                    continue
                if str(payload.get("manual_intervention_source", "") or "").strip() != "review_rework_escalation":
                    continue
                if self._review_feedback_version(payload) == review_feedback_version:
                    return

        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        summary = (
            str(escalation_reason or "").strip()
            or "Manual intervention required before this work item can continue."
        )
        prompt = "\n\n".join(
            part for part in (
                summary,
                "Please decide how this work item should continue.",
                "Reviewer feedback:",
                feedback,
            )
            if str(part).strip()
        ).strip()
        pause_request = {
            "reason": summary,
            "questions": [
                "Should this work item get another rework attempt, be approved as-is, or be redirected?"
            ],
            "required_fields": ["decision"],
            "context_note": (
                f"Reviewer: {review_owner_role_id}" if review_owner_role_id else "Reviewer feedback is attached below."
            ),
            "resume_hint": "Reply with the decision and any guidance the resumed work item should follow.",
        }
        await self.checkpoint_callback(
            {
                "checkpoint_type": "task_user_input",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": str(task.metadata.get("execution_mode", "company_mode") or "company_mode"),
                    "task_ids": [t.id for t in self._active_tasks] if self._active_tasks else [task.id],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "prompt": prompt,
                    "pause_request": pause_request,
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "manual_intervention_source": "review_rework_escalation",
                    "review_owner_role_id": review_owner_role_id,
                    "review_feedback_version": review_feedback_version,
                    "review_feedback": feedback,
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )

    async def _refresh_delegation_dependents(self, task: Task) -> None:
        """Propagate dependency completion/escalation into parent phases.

        Thin wrapper around
        ``opc.layer2_organization.work_item_transition.refresh_dependents_for_run``
        (the free function). The free function is also registered as a
        phase-transition hook (``refresh_dependents_hook``), so any
        terminal child transition auto-triggers the refresh — this
        explicit call remains for historical APPROVED-verdict callers
        and as a belt-and-suspenders path. Re-entrancy is guarded
        inside the free function via a ContextVar.
        """
        if not self.store:
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id:
            return
        await refresh_dependents_for_run(
            self.store,
            run_id=run_id,
            source_task_id=str(task.id or "").strip() or None,
            source_work_item_id=linked_work_item_id_for_task(task) or None,
            source_cell_id=str((task.metadata or {}).get("delegation_cell_id", "") or "").strip() or None,
            source_role_id=str(task.assigned_to or (task.metadata or {}).get("work_item_role_id", "") or "").strip() or None,
        )

    def _comms_layout_for_task(self, task: Task):
        """Best-effort comms layout resolution for `task`. Returns None if unavailable.

        Prefers `comms_workspace_root` (workspace root, sibling of
        deliverable folders) over `target_output_dir` (the project's
        deliverable folder itself) so the comms tree never pollutes
        deliverables.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return None
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
            or str(task.metadata.get("setup_workspace_prepared", "") or "").strip()
        )
        if not workspace_root:
            return None
        project_id = str(task.project_id or "default") or "default"
        session_id = (
            str(task.parent_session_id or "").strip()
            or str(task.session_id or "").strip()
            or "default"
        )
        try:
            return _comms.resolve_layout(workspace_root, project_id, session_id)
        except Exception:
            return None

    def _inject_inbox_into_context(self, task: Task, member_session: CompanyMemberSession | None) -> None:
        """Inject unread inbox messages into the agent's context so it sees
        them without having to poll the mailbox manually."""
        if member_session is None:
            return
        inbox_messages = list(member_session.inbox_state.get("actionable_chat", []) or [])
        if not inbox_messages:
            return
        capped = inbox_messages[:5]
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["injected_inbox"] = capped
        summary_lines = []
        for m in capped:
            if not isinstance(m, dict):
                continue
            from_agent = str(m.get("from_agent", "?"))
            subject = str(m.get("subject", ""))
            body_clip = clip_text(str(m.get("body", "")), limit=200, marker="inbox message preview truncated")
            msg_id = str(m.get("msg_id", "") or m.get("message_id", "") or "").strip()
            id_suffix = f" (msg_id={msg_id})" if msg_id else ""
            summary_lines.append(f"- **[{from_agent}]** {subject}{id_suffix}: {body_clip.text}")
        if summary_lines:
            inbox_section = "\n\n## Messages From Other Teams\n" + "\n".join(summary_lines) + "\n"
            task.description = str(task.description or "") + inbox_section

    @staticmethod
    def _is_attention_work_item(item: Any | None) -> bool:
        return bool(dict(getattr(item, "metadata", {}) or {}).get("attention_work_item", False))

    @staticmethod
    def _work_item_kind(item: Any | None) -> str:
        return str(getattr(item, "kind", "") or "").strip().lower()

    async def _resolve_manager_board_parent_for_task(self, task: Task) -> tuple[str, str]:
        current_work_item_id = linked_work_item_id_for_task(task)
        explicit_parent = str(
            (task.metadata or {}).get("manager_board_parent_work_item_id", "")
            or (task.metadata or {}).get("attention_business_parent_work_item_id", "")
            or ""
        ).strip()
        if explicit_parent:
            current_item = None
            if current_work_item_id and self.store and hasattr(self.store, "get_delegation_work_item"):
                current_item = await self.store.get_delegation_work_item(current_work_item_id)
            return explicit_parent, current_work_item_id if self._is_attention_work_item(current_item) else ""
        if not current_work_item_id or not self.store or not hasattr(self.store, "get_delegation_work_item"):
            return current_work_item_id, ""
        current_item = await self.store.get_delegation_work_item(current_work_item_id)
        if current_item is None or not self._is_attention_work_item(current_item):
            return current_work_item_id, ""
        attention_work_item_id = str(getattr(current_item, "work_item_id", "") or current_work_item_id).strip()
        parent_id = str(getattr(current_item, "parent_work_item_id", "") or "").strip()
        if not parent_id:
            return current_work_item_id, attention_work_item_id
        parent_item = await self.store.get_delegation_work_item(parent_id)
        if (
            parent_item is not None
            and self._work_item_kind(parent_item) in {"deliver", "delivery"}
            and str(getattr(parent_item, "parent_work_item_id", "") or "").strip()
        ):
            return str(getattr(parent_item, "parent_work_item_id", "") or "").strip(), attention_work_item_id
        return parent_id, attention_work_item_id

    async def _inject_manager_board_into_context(
        self,
        task: Task,
        member_session: CompanyMemberSession | None,
    ) -> None:
        if not self.store or not hasattr(self.store, "list_manager_board"):
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        seat_id = str((task.metadata or {}).get("delegation_seat_id", "") or "").strip()
        if not run_id or not seat_id:
            return
        turn_mode = self._manager_dispatch_turn_mode(task, member_session=member_session)
        current_work_item_id = linked_work_item_id_for_task(task)
        current_item = None
        if current_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            current_item = await self.store.get_delegation_work_item(current_work_item_id)
        is_attention_turn = self._is_attention_work_item(current_item)
        if turn_mode not in {"dispatch_required", "monitor_children", "synthesize_required", "deliver_required"} and not is_attention_turn:
            return
        parent_work_item_id, attention_work_item_id = await self._resolve_manager_board_parent_for_task(task)
        if not parent_work_item_id:
            return
        parent_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            parent_item = await self.store.get_delegation_work_item(parent_work_item_id)
        board_items = await self.store.list_manager_board(
            run_id,
            manager_seat_id=seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        board_items = [
            item for item in board_items
            if not should_hide_work_item_from_company_kanban(dict(item.metadata or {}))
            and str((item.metadata or {}).get("upstream_visibility", "") or "").strip().lower() != "hidden"
        ]
        if not board_items and not is_attention_turn:
            return

        def _child_payload(item: DelegationWorkItem) -> dict[str, Any]:
            meta = dict(item.metadata or {})
            deps = [
                str(dep).strip()
                for dep in list(meta.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            return {
                "work_item_id": str(item.work_item_id or "").strip(),
                "role_id": str(item.role_id or "").strip(),
                "title": str(item.title or "").strip(),
                "kind": str(item.kind or "").strip(),
                "phase": item.phase.value,
                "kanban_column": kanban_column(item.phase),
                "scope_key": str(meta.get("scope_key", "") or "").strip(),
                "dependency_work_item_ids": deps,
            }

        child_payloads = [_child_payload(item) for item in board_items]
        counts: dict[str, int] = {}
        for child in child_payloads:
            phase = str(child.get("phase", "") or "").strip()
            counts[phase] = counts.get(phase, 0) + 1
        task.metadata = dict(task.metadata or {})
        task.metadata["manager_board_parent_work_item_id"] = parent_work_item_id
        if attention_work_item_id:
            task.metadata["attention_business_parent_work_item_id"] = parent_work_item_id
            task.metadata["attention_work_item_id"] = attention_work_item_id
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["manager_board_parent_work_item_id"] = parent_work_item_id
        task.context_snapshot["manager_board_attention_work_item_id"] = attention_work_item_id
        if parent_item is not None:
            parent_meta = dict(parent_item.metadata or {})
            parent_latest_for_snapshot = str(
                parent_meta.get("latest_user_directive")
                or parent_meta.get("manager_mutation_user_input")
                or ""
            ).strip()
            task.context_snapshot["manager_board_parent"] = {
                "work_item_id": str(parent_item.work_item_id or "").strip(),
                "role_id": str(parent_item.role_id or "").strip(),
                "title": str(parent_item.title or "").strip(),
                "summary": str(parent_item.summary or "").strip(),
                "kind": str(parent_item.kind or "").strip(),
                "latest_user_directive": parent_latest_for_snapshot,
            }
        task.context_snapshot["manager_board_children"] = child_payloads
        task.context_snapshot["manager_board_phase_counts"] = counts

        lines = [
            "\n\n## Current Manager Board",
            f"Business parent work_item_id: `{parent_work_item_id}`",
        ]
        parent_latest_directive = ""
        if parent_item is not None:
            parent_meta = dict(parent_item.metadata or {})
            parent_latest_directive = str(
                parent_meta.get("latest_user_directive")
                or parent_meta.get("manager_mutation_user_input")
                or ""
            ).strip()
            if parent_latest_directive:
                task.metadata["latest_user_directive"] = parent_latest_directive
                task.context_snapshot["latest_user_directive"] = parent_latest_directive
                parent_mutation_input = str(parent_meta.get("manager_mutation_user_input", "") or "").strip()
                if parent_mutation_input:
                    task.metadata["manager_mutation_user_input"] = parent_mutation_input
            if is_attention_turn:
                inherited = self._attention_parent_context_metadata(
                    parent_work_item=parent_item,
                    parent_task=None,
                    work_kind=self._work_item_kind(current_item) or turn_mode,
                    attention_title=str(task.title or "Attention Turn").strip() or "Attention Turn",
                )
                for key, value in inherited.items():
                    if value not in (None, "", [], {}):
                        task.metadata[key] = value
            parent_title = str(parent_item.title or "").strip()
            parent_summary = str(parent_item.summary or parent_meta.get("brief", "") or "").strip()
            if parent_title:
                lines.append(f"Business parent title: {parent_title}")
            if parent_summary:
                lines.append(
                    "Business parent brief: "
                    + clip_text(parent_summary, limit=600, marker="business parent brief truncated").text
                )
            if parent_latest_directive:
                lines.append(
                    "Latest user directive for this business parent: "
                    + clip_text(parent_latest_directive, limit=800, marker="business parent directive truncated").text
                )
        if attention_work_item_id:
            lines.append(
                f"Current attention work_item_id: `{attention_work_item_id}`. "
                "This turn is a wake-up wrapper; do not treat it as a fresh empty dispatch board."
            )
        if counts:
            counts_text = ", ".join(f"{phase}={count}" for phase, count in sorted(counts.items()))
            lines.append(f"Children by phase: {counts_text}")
        settlement = {}
        if current_item is not None:
            settlement = dict(
                (current_item.metadata or {}).get("dependency_settlement", {}) or {}
            )
        failed_board_items = [
            item for item in board_items
            if getattr(item, "phase", None) in (Phase.FAILED, Phase.CANCELLED)
        ]
        if failed_board_items or settlement:
            lines.append("### Failed or cancelled children (triage needed)")
            for item in failed_board_items[:8]:
                item_meta = dict(item.metadata or {})
                reason = str(item_meta.get("last_transition_reason", "") or "").strip()
                summary_text = str(item.summary or "").strip()
                entry = (
                    f"- `{str(item.work_item_id or '').strip()}` [{item.phase.value}] "
                    f"{str(item.role_id or '').strip()}"
                )
                if reason:
                    entry += f" reason={reason}"
                if summary_text:
                    entry += ": " + clip_text(
                        summary_text, limit=240, marker="failed child summary truncated"
                    ).text
                lines.append(entry)
                preserved = str(item_meta.get("last_turn_preserved_content", "") or "").strip()
                if preserved:
                    lines.append(
                        "  Output preserved from its final turn (not lost): "
                        + clip_text(preserved, limit=700, marker="preserved output truncated").text
                    )
            stuck_ids = [
                str(item).strip()
                for item in list(settlement.get("stuck", []) or [])
                if str(item).strip()
            ]
            if stuck_ids:
                lines.append(
                    "Downstream children blocked by these failures: "
                    + ", ".join(f"`{sid}`" for sid in stuck_ids[:8])
                    + (" ..." if len(stuck_ids) > 8 else "")
                    + ". They are cancelled automatically when this turn completes "
                    "unless you rebuild or rewire them."
                )
            lines.append(
                "You can rebuild the failed work with `delegate_work` (pair it with "
                "`delete_work_item` + `replacement_dependency_work_item_ids` to rewire "
                "dependents), continue with the successful results only, or record the "
                "gap in your handoff so the upper role or user can decide."
            )
        lines.append(
            "Use `manager_board_read` without `parent_work_item_id` to inspect this business board. "
            "Do not call `delegate_work` again for any existing `scope_key`; use `modify_work_item` or `delete_work_item` "
            "for wrong existing children, otherwise review, release, monitor, or synthesize the children below."
        )
        followup_text = str(task.context_snapshot.get("user_supplied_input", "") or "").strip()
        is_final_decider_followup = bool((task.metadata or {}).get("followup_routed_to_final_decider", False)) or bool(followup_text)
        if is_final_decider_followup:
            if followup_text:
                followup_preview = clip_text(
                    followup_text,
                    limit=800,
                    marker="follow-up truncated",
                ).text
                lines.append(f"Latest user follow-up: {followup_preview}")
            lines.append(
                "Reconcile this existing board before continuing: classify current children as keep, revise, delete, "
                "or replace. If `delegate_work` creates a replacement for an obsolete child, also call "
                "`delete_work_item` with `replacement_dependency_work_item_ids` or `modify_work_item` so stale "
                "running work and downstream delivery dependencies do not keep the old direction alive."
            )
        for child in child_payloads[:12]:
            scope = str(child.get("scope_key", "") or "").strip() or "(no scope_key)"
            title = clip_text(str(child.get("title", "") or ""), limit=140, marker="child title truncated").text
            deps = list(child.get("dependency_work_item_ids", []) or [])
            dep_text = f", deps={len(deps)}" if deps else ""
            lines.append(
                f"- `{child['work_item_id']}` [{child['phase']}] "
                f"{child['role_id']} scope=`{scope}`{dep_text}: {title}"
            )
        if len(child_payloads) > 12:
            lines.append(f"- ... {len(child_payloads) - 12} more children omitted; call `manager_board_read` for the full board.")
        task.description = str(task.description or "") + "\n".join(lines) + "\n"

    def _inject_scratchpad_into_context(self, task: Task) -> None:
        """Load shared team scratchpad into the agent's context."""
        layout = self._comms_layout_for_task(task)
        if layout is None:
            return
        scratchpad_path = layout.scratchpad_path
        if not scratchpad_path.exists():
            return
        try:
            scratchpad_text = scratchpad_path.read_text(encoding="utf-8")
            content_clip = clip_text(scratchpad_text, limit=4000, marker="team scratchpad preview truncated")
            content = content_clip.text
        except Exception:
            return
        if content.strip():
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot["team_scratchpad"] = content
            task.context_snapshot["team_scratchpad_path"] = str(scratchpad_path)
            task.context_snapshot["team_scratchpad_truncated"] = content_clip.truncated
            task.context_snapshot["team_scratchpad_omitted_chars"] = content_clip.omitted_chars

    def _append_to_scratchpad(self, task: Task, result: TaskResult | None) -> None:
        """Append a completion summary to the shared team scratchpad."""
        layout = self._comms_layout_for_task(task)
        if layout is None:
            return
        shared_dir = layout.shared_root
        try:
            shared_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        scratchpad_path = layout.scratchpad_path
        role = task.assigned_to or task.metadata.get("work_item_role_id", "unknown")
        title = task.title or "Untitled"
        output_preview = ""
        if result and hasattr(result, "output") and result.output:
            output_preview = clip_text(str(result.output), limit=300, marker="scratchpad output preview truncated").text
        task_ref = str(linked_work_item_id_for_task(task) or task.id or "").strip()
        ref_line = f"task_ref={task_ref}\n" if task_ref else ""
        entry = f"\n---\n### [{role}] {title} — DONE\n{ref_line}{output_preview}\n"
        try:
            with open(scratchpad_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass

    @staticmethod
    def _manager_dispatch_turn_mode(
        task: Task,
        member_session: CompanyMemberSession | None = None,
    ) -> str:
        return str(
            (task.metadata or {}).get("current_turn_mode", "")
            or (task.context_snapshot or {}).get("current_turn_mode", "")
            or getattr(member_session, "current_turn_mode", "")
            or ""
        ).strip().lower()

    # Turn kinds where a manager turn completes *without* dispatching any
    # child work by design: delivery/synthesize/aggregate roll sub-team
    # results up to the parent, and review evaluates a peer's output.
    # Firing the dispatch guard on these marks a legitimate terminal turn
    # as failed (new16/app13 reproduced this: final delivery produced
    # substantive output, guard rejected "no delegate_work call" → task
    # status FAILED despite disk artifacts being complete).
    _NON_DISPATCH_TURN_KINDS: frozenset[str] = frozenset({
        "deliver", "delivery",
        "synthesize", "synthesis",
        "aggregate",
        "review",
        "monitor",
        "self_evolution",
    })

    @classmethod
    def _task_turn_kind(cls, task: Task) -> str:
        """Best-effort turn-kind inference for guard-filtering. Checks the
        three metadata fields that callers stamp in different code paths —
        ``work_kind`` is the modern work-item runtime field, the other two are
        legacy signals from the work-item planner and gate policy."""
        meta = task.metadata or {}
        for key in ("work_kind", "delegation_turn_kind", "work_item_turn_type"):
            value = str(meta.get(key, "") or "").strip().lower()
            if value:
                return value
        return ""

    @classmethod
    def _requires_manager_dispatch_guard(
        cls,
        task: Task,
        member_session: CompanyMemberSession | None = None,
    ) -> bool:
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() != "multi_team_org":
            return False
        # Fix 3 (follow-up): skip the guard on work items where "no delegate_work
        # call" is the expected shape, regardless of what current_turn_mode
        # resolved to. See ``_NON_DISPATCH_TURN_KINDS``. Without this, the
        # Final-delivery work item in new16/app13 got marked failed even
        # though the artifacts were written and the subteam work approved.
        turn_kind = cls._task_turn_kind(task)
        if turn_kind in cls._NON_DISPATCH_TURN_KINDS:
            return False
        if cls._manager_dispatch_turn_mode(task, member_session=member_session) != "dispatch_required":
            return False
        direct_report_seat_ids = [
            str(item).strip()
            for item in list(
                (task.metadata or {}).get("direct_report_seat_ids", [])
                or dict(getattr(member_session, "metadata", {}) or {}).get("direct_report_seat_ids", [])
                or []
            )
            if str(item).strip()
        ]
        allowed_delegate_role_ids = [
            str(item).strip()
            for item in list(
                (task.metadata or {}).get("allowed_delegate_role_ids", [])
                or dict(getattr(member_session, "metadata", {}) or {}).get("allowed_delegate_role_ids", [])
                or []
            )
            if str(item).strip()
        ]
        managed_team_id = str(
            (task.metadata or {}).get("managed_team_id", "")
            or dict(getattr(member_session, "metadata", {}) or {}).get("managed_team_id", "")
            or ""
        ).strip()
        return bool(direct_report_seat_ids or allowed_delegate_role_ids or managed_team_id)

    async def _snapshot_manager_dispatch_state(
        self,
        task: Task,
        *,
        member_session: CompanyMemberSession | None = None,
    ) -> dict[str, Any] | None:
        if not self._requires_manager_dispatch_guard(task, member_session=member_session):
            return None
        if not self.store or not hasattr(self.store, "list_delegation_work_items"):
            return None
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not run_id or not parent_work_item_id:
            return None
        work_items = await self.store.list_delegation_work_items(run_id)
        child_mutation_state: dict[str, dict[str, Any]] = {}
        child_work_item_ids = {
            str(getattr(item, "work_item_id", "") or "").strip()
            for item in work_items
            if str(getattr(item, "parent_work_item_id", "") or "").strip() == parent_work_item_id
            and not is_runtime_auxiliary_work_item(item)
            and str(getattr(item, "work_item_id", "") or "").strip()
        }
        for item in work_items:
            item_id = str(getattr(item, "work_item_id", "") or "").strip()
            if not item_id or item_id not in child_work_item_ids:
                continue
            metadata = dict(getattr(item, "metadata", {}) or {})
            try:
                mutation_revision = int(metadata.get("manager_mutation_revision", 0) or 0)
            except (TypeError, ValueError):
                mutation_revision = 0
            child_mutation_state[item_id] = {
                "manager_mutation_revision": mutation_revision,
                "manager_mutation_action": str(metadata.get("manager_mutation_action", "") or "").strip(),
                "deleted_by_manager_tool": bool(metadata.get("deleted_by_manager_tool", False)),
                "hidden_from_company_kanban": bool(metadata.get("hidden_from_company_kanban", False)),
                "upstream_visibility": str(metadata.get("upstream_visibility", "") or "").strip().lower(),
            }
        dependency_work_item_ids = {
            str(item).strip()
            for item in list((task.metadata or {}).get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        }
        parent = await self.store.get_delegation_work_item(parent_work_item_id) if hasattr(self.store, "get_delegation_work_item") else None
        if parent is not None:
            dependency_work_item_ids.update(
                str(item).strip()
                for item in list((getattr(parent, "metadata", {}) or {}).get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            )
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        normalized_dependency_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
            list(dependency_work_item_ids),
            work_item_by_id,
            owner_work_item_id=parent_work_item_id,
        )
        return {
            "run_id": run_id,
            "parent_work_item_id": parent_work_item_id,
            "child_work_item_ids": child_work_item_ids,
            "dependency_work_item_ids": set(normalized_dependency_ids),
            "child_mutation_state": child_mutation_state,
        }

    @staticmethod
    def _genuine_no_delegation_justification(text: str) -> str:
        """Cleaned justification text, or "" for empty input or an echo of
        the instruction template's `<specific reason>` placeholder — with
        any markdown decoration, quoting, or trailing punctuation around
        the placeholder stripped before the check."""
        reason = re.sub(r"[\s*_`~\"']+$", "", str(text or "")).strip()
        if not reason:
            return ""
        core = reason
        previous = None
        while previous != core:
            previous = core
            core = core.strip().strip("*_~`\"'")
            core = re.sub(r"[\s.。!！,，;；:：]+$", "", core)
        if re.fullmatch(r"<[^<>]*>", core):
            return ""
        return reason

    @staticmethod
    def _extract_no_delegation_justification(task: Task, result: TaskResult | None) -> str:
        artifact_candidates = []
        if result and getattr(result, "artifacts", None):
            artifacts = dict(result.artifacts or {})
            artifact_candidates.extend(
                [
                    str(artifacts.get("manager_no_delegation_justification", "") or "").strip(),
                    str(artifacts.get("no_delegation_justification", "") or "").strip(),
                    str(artifacts.get("manager_no_delegation_reason", "") or "").strip(),
                    str(artifacts.get("no_delegation_reason", "") or "").strip(),
                ]
            )
        artifact_candidates.extend(
            [
                str((task.metadata or {}).get("manager_no_delegation_justification", "") or "").strip(),
                str((task.metadata or {}).get("no_delegation_justification", "") or "").strip(),
            ]
        )
        for candidate in artifact_candidates:
            cleaned = CompanyWorkItemExecutor._genuine_no_delegation_justification(candidate)
            if cleaned:
                return cleaned
        content = str(getattr(result, "content", "") or "").strip()
        for line in content.splitlines():
            match = _NO_DELEGATION_JUSTIFICATION_LINE.match(str(line))
            if not match:
                continue
            reason = CompanyWorkItemExecutor._genuine_no_delegation_justification(
                match.group("reason")
            )
            if reason:
                return reason
        return ""

    @staticmethod
    def _no_delegation_justification_is_infra_failure(
        justification: str,
        result: TaskResult | None,
    ) -> bool:
        artifacts = dict(getattr(result, "artifacts", {}) or {}) if result is not None else {}
        failure = artifacts.get("collaboration_infrastructure_failure")
        if isinstance(failure, dict) and str(failure.get("error_type", "") or "").strip() == "infrastructure":
            return True
        text = str(justification or "").strip().lower()
        if not text:
            return False
        markers = (
            "disk i/o error",
            "database is locked",
            "readonly database",
            "unable to open database file",
            "collaboration broker rpc",
            "broker rpc failed",
            "sqlite3.operationalerror",
            "sqlite operationalerror",
        )
        return any(marker in text for marker in markers)

    async def _enforce_manager_dispatch_guard(
        self,
        task: Task,
        result: TaskResult | None,
        *,
        before_state: dict[str, Any] | None,
        created_follow_up_work_item_ids: list[str] | None = None,
        member_session: CompanyMemberSession | None = None,
    ) -> list[str]:
        if before_state is None:
            return []
        after_state = await self._snapshot_manager_dispatch_state(task, member_session=member_session)
        if after_state is None:
            return []
        before_child_ids = set(before_state.get("child_work_item_ids", set()) or set())
        after_child_ids = set(after_state.get("child_work_item_ids", set()) or set())
        before_dependency_ids = set(before_state.get("dependency_work_item_ids", set()) or set())
        after_dependency_ids = set(after_state.get("dependency_work_item_ids", set()) or set())
        before_child_mutation_state = dict(before_state.get("child_mutation_state", {}) or {})
        after_child_mutation_state = dict(after_state.get("child_mutation_state", {}) or {})

        def _is_manager_mutation_marker(state: dict[str, Any]) -> bool:
            try:
                mutation_revision = int(state.get("manager_mutation_revision", 0) or 0)
            except (TypeError, ValueError):
                mutation_revision = 0
            return (
                mutation_revision > 0
                or bool(state.get("deleted_by_manager_tool", False))
                or bool(state.get("hidden_from_company_kanban", False))
                or str(state.get("upstream_visibility", "") or "") == "hidden"
            )

        manager_mutated_existing_child_ids = {
            item_id
            for item_id in before_child_ids & after_child_ids
            if (after_marker := dict(after_child_mutation_state.get(item_id, {}) or {}))
            != dict(before_child_mutation_state.get(item_id, {}) or {})
            and _is_manager_mutation_marker(after_marker)
        }
        created_follow_up_ids = {
            str(item).strip()
            for item in list(created_follow_up_work_item_ids or [])
            if str(item).strip()
        }
        if (
            after_child_ids - before_child_ids
            or before_child_ids - after_child_ids
            or after_dependency_ids - before_dependency_ids
            or manager_mutated_existing_child_ids
            or created_follow_up_ids
            or bool((task.metadata or {}).get("manager_board_mutation_performed", False))
        ):
            task.metadata = dict(task.metadata or {})
            task.metadata["manager_board_mutation_performed"] = True
            task.metadata.pop("manager_no_delegation_justification", None)
            task.metadata.pop("manager_dispatch_guard_unresolved", None)
            return []
        justification = self._extract_no_delegation_justification(task, result)
        if justification:
            if self._no_delegation_justification_is_infra_failure(justification, result):
                return [
                    "Dispatch-required manager turn hit a collaboration infrastructure failure "
                    "while trying to inspect or mutate the work-item board. Retry the collaboration "
                    "tool path instead of accepting `NO_DELEGATION_JUSTIFICATION` as normal completion."
                ]
            task.metadata = dict(task.metadata or {})
            task.metadata["manager_no_delegation_justification"] = justification
            task.metadata.pop("manager_dispatch_guard_unresolved", None)
            return []
        direct_reports = [
            str(item).strip()
            for item in list((task.metadata or {}).get("direct_report_role_ids", []) or [])
            if str(item).strip()
        ]
        direct_report_hint = f" Direct reports in scope: {', '.join(direct_reports[:6])}." if direct_reports else ""
        return [
            "Dispatch-required manager turn finished without creating child work. "
            "Use `delegate_work(...)` for new child work, or `modify_work_item(...)` / `delete_work_item(...)` "
            "when revising an existing board, "
            "or finish with `NO_DELEGATION_JUSTIFICATION: <specific reason>` when no downstream seat is a fit."
            + direct_report_hint
        ]

    def _snapshot_inbox_for_turn(self, task: Task) -> None:
        """Record the set of unread message filenames at turn start.

        Used by `_archive_consumed_inbox_snapshot` after the turn ends:
        only files captured in the snapshot get moved to seen/, so mail
        that arrived mid-turn (which the agent never had a chance to
        read) stays as `new` and naturally triggers a follow-up turn.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return
        try:
            headers = _comms.list_unread(layout, role_id)
        except Exception:
            return
        snapshot = [str(h.path.name) for h in headers]
        task.metadata = dict(task.metadata)
        task.metadata["_comms_turn_inbox_snapshot"] = snapshot

    def _archive_consumed_inbox_snapshot(self, task: Task) -> None:
        """Clear turn-start inbox bookkeeping without marking mail as read.

        Mailbox consumption is now explicit: `reply_message` acknowledges the
        original message, and non-reply work uses `inbox(action="ack")`.
        Prompt injection only means the agent had a chance to see a message;
        it is not a read receipt.
        """
        task.metadata = dict(task.metadata)
        task.metadata.pop("_comms_turn_inbox_snapshot", None)

    # Hard stop: refuse to re-open the same task more than this many times
    # due to inbound comms. A cap well above typical multi-round coordination
    # (~2–3) but below any reasonable runaway. Exceeding this triggers a
    # warning and the task stays DONE so upstream senders time out cleanly.
    COMMS_REACTIVATION_DEPTH_LIMIT = 8
    # Size of the short rolling window used to detect cross-role ping-pong
    # loops (e.g. A→B→A→B…). A window of 20 is plenty to catch the pattern
    # without growing task metadata unboundedly.
    COMMS_CROSS_ROLE_HISTORY_MAX = 20
    # If the same (from_role, subject_hash) appears this many times in the
    # recent history, treat it as a ping-pong loop and refuse reactivation.
    COMMS_CROSS_ROLE_REPEAT_THRESHOLD = 4
    COMMS_REACTIVATION_WARNING_COOLDOWN_SECONDS = 60

    def _resolve_task_inbox_context(
        self,
        task: Task,
    ) -> tuple[str, Any] | None:
        """Return ``(role_id, comms_layout)`` for ``task``'s role, or None.

        Returns None when the task has no role, no workspace, or when the
        comms layout cannot be constructed — in every case the caller should
        skip reactivation. Kept as a shared helper so both the end-of-turn
        hook and the background sweeper follow the exact same resolution
        rules.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return None
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if not role_id:
            return None
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
            or str(task.metadata.get("setup_workspace_prepared", "") or "").strip()
        )
        if not workspace_root:
            return None
        project_id = str(task.project_id or "default") or "default"
        session_id = (
            str(task.parent_session_id or "").strip()
            or str(task.session_id or "").strip()
            or "default"
        )
        try:
            layout = _comms.resolve_layout(workspace_root, project_id, session_id)
        except Exception:
            return None
        return role_id, layout

    def _collect_actionable_unread(
        self,
        layout: Any,
        role_id: str,
        *,
        work_item_id: str = "",
        task_id: str = "",
        require_work_item_scope: bool = False,
    ) -> list[dict[str, Any]]:
        """Scan the role's `inbox/new/` and return classified actionable messages.

        In multi-team company mode, reactivation must be scoped to the
        relevant work item. A role-level inbox can contain messages for many
        child/review cards; treating the whole inbox as actionable for every
        DONE task cross-contaminates review state and triggers false
        ping-pong warnings.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return []
        try:
            unread_headers = _comms.list_unread(layout, role_id)
        except Exception:
            return []
        actionable: list[dict[str, Any]] = []
        for header in unread_headers:
            frontmatter = dict(getattr(header, "raw_frontmatter", {}) or {})
            if not self._unread_header_matches_scope(
                frontmatter,
                work_item_id=work_item_id,
                task_id=task_id,
                require_work_item_scope=require_work_item_scope,
            ):
                continue
            classified = classify_worker_message(
                {
                    "msg_id": str(getattr(header, "message_id", "") or "").strip(),
                    "msg_type": str(frontmatter.get("msg_type", "") or "question"),
                    "from_agent": str(getattr(header, "from_role", "") or "").strip(),
                    "subject": str(getattr(header, "subject", "") or "").strip(),
                    "reply_needed": bool(getattr(header, "blocking", False)),
                    "urgency": str(getattr(header, "priority", "") or "").strip() or "normal",
                    "task_id": str(frontmatter.get("task_id", "") or "").strip(),
                    "metadata": frontmatter,
                    "transport_kind": str(frontmatter.get("transport_kind", "") or "").strip(),
                    "semantic_type": str(frontmatter.get("semantic_type") or frontmatter.get("kind") or "").strip(),
                }
            )
            if worker_message_is_actionable(classified):
                actionable.append(classified)
        return actionable

    async def _block_completion_for_unread_inbox(self, task: Task) -> bool:
        """Hold completion when the current role has unacknowledged actionable mail."""
        context = self._resolve_task_inbox_context(task)
        if context is None:
            return False
        role_id, layout = context
        work_item_id = linked_work_item_id_for_task(task)
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        actionable_unread = self._collect_actionable_unread(
            layout,
            role_id,
            work_item_id=work_item_id,
            task_id=str(task.id or "").strip(),
            require_work_item_scope=bool(multi_team_org and work_item_id),
        )
        if not actionable_unread:
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("inbox_gate_pending_message_ids", None)
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot.pop("inbox_completion_gate", None)
            return False

        pending_ids = [
            str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
            for item in actionable_unread
            if str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
        ]
        summaries = [
            {
                "msg_id": str(item.get("msg_id", "") or item.get("message_id", "") or "").strip(),
                "from_agent": str(item.get("from_agent", "") or "").strip(),
                "subject": str(item.get("subject", "") or "").strip(),
                "reply_needed": bool(item.get("reply_needed", False)),
                "urgency": str(item.get("urgency", "") or "normal").strip() or "normal",
                "message_class": str(item.get("message_class", "") or "").strip(),
                "protocol_type": str(item.get("protocol_type", "") or "").strip(),
            }
            for item in actionable_unread[:8]
        ]
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata["inbox_gate_pending_message_ids"] = pending_ids
        task.metadata["inbox_gate_blocked_at"] = datetime.now().isoformat()
        task.context_snapshot["inbox_completion_gate"] = {
            "reason": "Actionable inbox messages must be replied to or acknowledged before this work item can complete.",
            "pending_message_ids": pending_ids,
            "messages": summaries,
        }
        await self._append_progress(
            task,
            "Inbox completion gate blocked finish: "
            f"{len(pending_ids)} actionable unread message(s) require reply or `inbox(action=\"ack\")`.",
        )
        await transition_work_item_from_task(
            self.store,
            task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="inbox_completion_gate",
            metadata_updates={
                "inbox_gate_pending_message_ids": pending_ids,
                "inbox_gate_blocked_at": task.metadata["inbox_gate_blocked_at"],
            },
        )
        await self.save_task(task)
        return True

    @staticmethod
    def _work_item_refs_from_frontmatter(frontmatter: dict[str, Any]) -> set[str]:
        refs = {
            str(frontmatter.get("target_work_item_id", "") or "").strip(),
            str(frontmatter.get("source_work_item_id", "") or "").strip(),
            str(frontmatter.get("work_item_id", "") or "").strip(),
        }
        metadata = dict(frontmatter.get("metadata", {}) or {})
        refs.update(
            {
                str(metadata.get("target_work_item_id", "") or "").strip(),
                str(metadata.get("source_work_item_id", "") or "").strip(),
                str(metadata.get("work_item_id", "") or "").strip(),
            }
        )
        nested_refs = dict(frontmatter.get("refs", {}) or {})
        refs.update(
            {
                str(nested_refs.get("target_work_item_id", "") or "").strip(),
                str(nested_refs.get("source_work_item_id", "") or "").strip(),
                str(nested_refs.get("work_item_id", "") or "").strip(),
            }
        )
        return {item for item in refs if item}

    @classmethod
    def _unread_header_matches_scope(
        cls,
        frontmatter: dict[str, Any],
        *,
        work_item_id: str = "",
        task_id: str = "",
        require_work_item_scope: bool = False,
    ) -> bool:
        target_work_item_id = str(work_item_id or "").strip()
        target_task_id = str(task_id or "").strip()
        refs = cls._work_item_refs_from_frontmatter(frontmatter)
        if target_work_item_id:
            if refs:
                return target_work_item_id in refs
            if require_work_item_scope:
                legacy_task_id = str(frontmatter.get("task_id", "") or "").strip()
                return bool(target_task_id and legacy_task_id == target_task_id)
            return True
        if require_work_item_scope:
            return False
        if target_task_id:
            legacy_task_id = str(frontmatter.get("task_id", "") or "").strip()
            return not legacy_task_id or legacy_task_id == target_task_id
        return True

    @staticmethod
    def _subject_hash(subject: str) -> str:
        """Stable short hash for ping-pong detection on (from_role, subject)."""
        normalized = (subject or "").strip().lower()
        if not normalized:
            return ""
        digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore"))
        return digest.hexdigest()[:10]

    def _reactivation_blocked_by_history(
        self,
        task: Task,
        actionable_unread: list[dict[str, Any]],
        unread_fingerprint: list[str],
    ) -> str | None:
        """Return a skip reason when loop-protection rejects reactivation.

        Three guards, evaluated in order:
          1. Same unread fingerprint as the previous reactivation → no new
             information, nothing would change if we re-ran the agent.
          2. Hard depth cap — stop re-opening the same task after
             ``COMMS_REACTIVATION_DEPTH_LIMIT`` rounds.
          3. Cross-role ping-pong — if ``(from_role, subject_hash)`` has
             appeared ≥ ``COMMS_CROSS_ROLE_REPEAT_THRESHOLD`` times in the
             recent rolling window, treat it as a loop.
        """
        last_fingerprint = sorted(
            {
                str(item).strip()
                for item in list(task.metadata.get("comms_last_reactivation_fingerprint", []) or [])
                if str(item).strip()
            }
        )
        if unread_fingerprint and unread_fingerprint == last_fingerprint:
            return (
                "Unread comms set is unchanged from the previous reactivation; "
                "skipping another auto-reactivation to avoid a no-progress loop."
            )
        current_depth = int(task.metadata.get("comms_reactivation_depth", 0) or 0)
        if current_depth >= self.COMMS_REACTIVATION_DEPTH_LIMIT:
            return (
                f"Comms reactivation depth={current_depth} has reached the hard "
                f"limit ({self.COMMS_REACTIVATION_DEPTH_LIMIT}); refusing to re-open "
                "this task again. Upstream senders will time out or escalate."
            )
        # Cross-role ping-pong: look for (from_role, subject_hash) repeats
        # in the short rolling window. Each history entry was recorded at a
        # prior successful reactivation. If the same sender keeps resurfacing
        # the same subject, that's a loop even when msg_ids differ.
        history = list(task.metadata.get("comms_cross_role_history", []) or [])
        repeat_counts: dict[tuple[str, str, str], int] = {}
        for entry in history:
            key = (
                str(entry.get("from_role", "") or "").strip(),
                str(entry.get("subject_hash", "") or "").strip(),
                str(entry.get("target_work_item_id", "") or "").strip(),
            )
            if not key[0]:
                continue
            repeat_counts[key] = repeat_counts.get(key, 0) + 1
        current_work_item_id = linked_work_item_id_for_task(task)
        for msg in actionable_unread:
            from_role = str(msg.get("from_agent", "") or "").strip()
            if not from_role:
                continue
            subject_hash = self._subject_hash(str(msg.get("subject", "") or ""))
            msg_metadata = dict(msg.get("metadata", {}) or {})
            msg_work_item_refs = self._work_item_refs_from_frontmatter(msg_metadata)
            target_work_item_id = (
                current_work_item_id
                if current_work_item_id in msg_work_item_refs or current_work_item_id
                else sorted(msg_work_item_refs)[0]
                if msg_work_item_refs
                else ""
            )
            key = (from_role, subject_hash, target_work_item_id)
            prior = repeat_counts.get(key, 0)
            if prior >= self.COMMS_CROSS_ROLE_REPEAT_THRESHOLD:
                return (
                    f"Cross-role comms ping-pong detected: role `{from_role}` has "
                    f"re-sent the same subject {prior + 1} times; refusing to "
                    "reactivate. Escalate to a human reviewer instead."
                )
        return None

    async def _reactivate_for_unread_mail(self, task: Task) -> bool:
        """Re-open `task` as PENDING if its role has unread actionable mail.

        Returns True when the task was re-opened (caller should NOT mark
        completion progress), False otherwise.

        Three loop-protection guards are enforced via
        ``_reactivation_blocked_by_history``:
        (a) unread-set fingerprint equality, (b) hard depth cap, and
        (c) per-(from_role, subject_hash) cross-role ping-pong detection.
        This helper is also called by ``CommsReactivationSweeper`` so any
        evolution of the guard logic applies uniformly to both callers.
        """
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        if multi_team_org:
            # Company runtime routes inbound attention through role/session
            # inbox refresh + work-item attention/review cards. Re-opening
            # arbitrary DONE tasks from a role-wide mailbox is the old
            # task-centric path that caused cross-work-item contamination.
            return False
        context = self._resolve_task_inbox_context(task)
        if context is None:
            return False
        role_id, layout = context
        work_item_id = linked_work_item_id_for_task(task)
        actionable_unread = self._collect_actionable_unread(
            layout,
            role_id,
            work_item_id=work_item_id,
            task_id=str(task.id or "").strip(),
            require_work_item_scope=False,
        )
        if not actionable_unread:
            return False

        task.metadata = dict(task.metadata)
        unread_fingerprint = sorted(
            {
                str(item.get("msg_id", "") or "").strip()
                for item in actionable_unread
                if str(item.get("msg_id", "") or "").strip()
            }
        )
        skip_reason = self._reactivation_blocked_by_history(
            task,
            actionable_unread,
            unread_fingerprint,
        )
        if skip_reason:
            now = datetime.now()
            last_key = str(task.metadata.get("comms_last_blocked_reactivation_key", "") or "").strip()
            last_at_raw = str(task.metadata.get("comms_last_blocked_reactivation_at", "") or "").strip()
            block_key = hashlib.sha1(
                json.dumps(
                    {
                        "role_id": role_id,
                        "work_item_id": work_item_id,
                        "reason": skip_reason,
                        "fingerprint": unread_fingerprint,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            should_log = True
            if last_key == block_key and last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    should_log = (now - last_at).total_seconds() >= self.COMMS_REACTIVATION_WARNING_COOLDOWN_SECONDS
                except Exception:
                    should_log = True
            if should_log:
                await self._append_progress(task, skip_reason)
                task.metadata["comms_last_blocked_reactivation_at"] = now.isoformat()
                task.metadata["comms_last_blocked_reactivation_key"] = block_key
            await self.save_task(task)
            if should_log:
                logger.warning(
                    "[comms_reactivation] task={} role={} work_item={} blocked: {}",
                    getattr(task, "id", ""),
                    role_id,
                    work_item_id,
                    skip_reason,
                )
            return False

        depth = int(task.metadata.get("comms_reactivation_depth", 0) or 0) + 1
        task.metadata["comms_reactivation_depth"] = depth
        task.metadata["comms_last_reactivation_fingerprint"] = unread_fingerprint

        # Extend the rolling cross-role history with one entry per actionable
        # sender in this reactivation round. Trimmed to COMMS_CROSS_ROLE_HISTORY_MAX.
        history = list(task.metadata.get("comms_cross_role_history", []) or [])
        for msg in actionable_unread:
            from_role = str(msg.get("from_agent", "") or "").strip()
            if not from_role:
                continue
            msg_metadata = dict(msg.get("metadata", {}) or {})
            msg_work_item_refs = self._work_item_refs_from_frontmatter(msg_metadata)
            history.append(
                {
                    "from_role": from_role,
                    "subject_hash": self._subject_hash(str(msg.get("subject", "") or "")),
                    "target_work_item_id": work_item_id if work_item_id else sorted(msg_work_item_refs)[0] if msg_work_item_refs else "",
                    "semantic_type": str(msg.get("semantic_type") or msg_metadata.get("semantic_type") or "").strip(),
                    "msg_id": str(msg.get("msg_id", "") or "").strip(),
                    "depth": depth,
                }
            )
        task.metadata["comms_cross_role_history"] = history[-self.COMMS_CROSS_ROLE_HISTORY_MAX:]

        # Re-open the task. The scheduler picks up PENDING tasks each
        # tick; the agent will see the unread mail in its next prompt
        # via context_assembler.render_inbox_section. For external agents
        # the broker's _restore_session_resume_from_store then re-hydrates
        # the prior codex/claude_code session_id so the resumed turn has
        # full context.
        await self._append_progress(
            task,
            f"Reactivated by inbound comms (depth={depth}); "
            "agent will read inbox/new/ on next turn.",
        )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="reactivated_by_inbound_comms",
        )
        await self.save_task(task)
        return True

    async def _park_for_delegated_children(self, task: Task) -> bool:
        if not self.store or not hasattr(self.store, "get_delegation_work_item"):
            return False
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not parent_work_item_id:
            return False
        dependency_ids = [
            str(item).strip()
            for item in list(task.metadata.get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        ]
        parent_work_item = await self.store.get_delegation_work_item(parent_work_item_id)
        if parent_work_item is not None:
            dependency_ids = list(
                dict.fromkeys(
                    [
                        *dependency_ids,
                        *[
                            str(item).strip()
                            for item in list((parent_work_item.metadata or {}).get("dependency_work_item_ids", []) or [])
                            if str(item).strip()
                        ],
                    ]
                )
            )
        if dependency_ids and parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                run_items_for_deps = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                run_items_for_deps = []
            work_item_by_id = {
                str(getattr(item, "work_item_id", "") or "").strip(): item
                for item in run_items_for_deps
                if str(getattr(item, "work_item_id", "") or "").strip()
            }
            dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
                dependency_ids,
                work_item_by_id,
                owner_work_item_id=parent_work_item_id,
            )
            if pruned_dependency_ids and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        parent_work_item_id,
                        metadata_updates={
                            "dependency_work_item_ids": dependency_ids,
                            "pruned_dependency_work_item_ids": pruned_dependency_ids,
                            "dependency_pruned_at": datetime.now().isoformat(),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to persist pruned delegated-child dependencies for %s",
                        parent_work_item_id,
                    )
        # Belt-and-suspenders: if neither task.metadata nor parent.metadata
        # carry the dependency ids but the parent actually has children
        # filed against it (parent_work_item_id pointer), derive the deps
        # from the live work-item dependency topology. Without this, any future code
        # path that creates children without stamping the dependency ids
        # would silently bypass parking and send the manager up for review
        # while children are still running.
        if not dependency_ids and parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                run_items = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                run_items = []
            dependency_ids = [
                str(getattr(child, "work_item_id", "") or "").strip()
                for child in run_items
                if str(getattr(child, "parent_work_item_id", "") or "").strip() == parent_work_item_id
                and not is_runtime_auxiliary_work_item(child)
                and not bool((getattr(child, "metadata", {}) or {}).get("deleted_by_manager_tool", False))
                and str(getattr(child, "work_item_id", "") or "").strip()
            ]
        if not dependency_ids:
            return False
        # Intake special-case: the top-level "receive user request + dispatch"
        # card's deliverable IS the delegation; there is nothing further
        # for it to integrate once children return. Instead of parking it
        # in WAITING_FOR_CHILDREN (which forces a wake and an empty "turn 2"
        # where the agent has nothing to produce), we:
        #   1. Approve the intake directly — its job is done.
        #   2. Materialize a separate delivery card, dependent on the
        #      same children, whose job is to synthesise and hand the
        #      final result to the user.
        # The delivery card is reviewed by the human user (not an upper
        # agent), so its review phase resolves to AWAITING_HUMAN.
        if (
            parent_work_item is not None
            and str(getattr(parent_work_item, "kind", "") or "").strip().lower() == "intake"
            and not bool((parent_work_item.metadata or {}).get("intake_delivery_spawned", False))
        ):
            try:
                await self._spawn_delivery_card_after_intake(
                    task=task,
                    intake_work_item=parent_work_item,
                    dependency_ids=dependency_ids,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "failed to spawn delivery card for intake %s — falling back to normal parking",
                    parent_work_item_id,
                )
            else:
                task.metadata = dict(task.metadata)
                task.metadata.pop("delegation_pending_work_item_ids", None)
                task.metadata.pop("delegated_children_pending", None)
                task.metadata.pop("delegation_wait_for_work_item_ids", None)
                await self._append_progress(
                    task,
                    "Intake dispatched; delivery card spawned, intake approved.",
                )
                # task.status will be synchronised to DONE by the phase hook
                # once we flip the intake work item to APPROVED below.
                if hasattr(self.store, "update_delegation_work_item"):
                    refreshed_intake = None
                    if hasattr(self.store, "get_delegation_work_item"):
                        try:
                            refreshed_intake = await self.store.get_delegation_work_item(parent_work_item_id)
                        except Exception:
                            refreshed_intake = None
                    current_phase = getattr(refreshed_intake or parent_work_item, "phase", None)
                    if current_phase == Phase.WAITING_FOR_CHILDREN:
                        await self.store.update_delegation_work_item(
                            parent_work_item_id,
                            phase=Phase.RUNNING,
                            blocked_reason="",
                            metadata_updates={
                                "frontier": "intake_delivery_spawned",
                            },
                        )
                    await self.store.update_delegation_work_item(
                        parent_work_item_id,
                        phase=Phase.APPROVED,
                        metadata_updates={
                            "dependency_work_item_ids": dependency_ids,
                            "waiting_on_work_item_ids": [],
                            "delegated_children_pending": False,
                            "intake_delivery_spawned": True,
                        },
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                    )
                await self.save_task(task)
                return False  # intake does not park; it closes out
        # A dependency only counts as pending while it can still move on its
        # own: terminal deps (APPROVED/FAILED/CANCELLED) and doomed deps
        # (transitively blocked by a terminal failure) are settled. Without
        # this, a triage turn that accepts partial results re-parks forever
        # on the already-FAILED child it just triaged — the settlement
        # release and this gate must agree on what "settled" means.
        park_doomed_ids: set[str] = set()
        park_items_by_id: dict[str, Any] = {}
        if parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                park_run_items = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                park_run_items = []
            park_items_by_id = {
                str(getattr(item, "work_item_id", "") or "").strip(): item
                for item in park_run_items
                if str(getattr(item, "work_item_id", "") or "").strip()
            }
            park_doomed_ids = compute_doomed_work_item_ids(park_items_by_id)
        pending_dependency_ids: list[str] = []
        settled_failures_present = False
        for dep_id in dependency_ids:
            dependency = park_items_by_id.get(dep_id)
            if dependency is None:
                dependency = await self.store.get_delegation_work_item(dep_id)
            if dependency is None:
                pending_dependency_ids.append(dep_id)
                continue
            if dependency.phase == Phase.APPROVED:
                continue
            if dependency.phase in DONE_PHASES or dep_id in park_doomed_ids:
                settled_failures_present = True
                continue
            pending_dependency_ids.append(dep_id)
        task.metadata = dict(task.metadata)
        task.metadata["delegation_wait_for_work_item_ids"] = dependency_ids
        if not pending_dependency_ids:
            task.metadata.pop("delegation_pending_work_item_ids", None)
            parent_meta_for_stamp = (
                dict(getattr(parent_work_item, "metadata", {}) or {})
                if parent_work_item is not None
                else {}
            )
            has_settlement_stamp = bool(
                dict(parent_meta_for_stamp.get("dependency_settlement", {}) or {})
            )
            if settled_failures_present and not has_settlement_stamp:
                # Race: the children settled with failures while this turn
                # was still running, so the failure's transition hook fired
                # before this card could park — no triage release has been
                # scheduled (and the concurrent refresh may already have
                # regressed our phase to WAITING_FOR_CHILDREN without a
                # stamp). Park now and run the frontier immediately: the
                # settlement release re-arms the triage turn.
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.WAITING_FOR_CHILDREN,
                    reason="park_for_settled_failures",
                    metadata_updates={
                        "dependency_work_item_ids": dependency_ids,
                        "waiting_on_work_item_ids": [],
                        "delegated_children_pending": True,
                    },
                )
                await self.save_task(task)
                try:
                    await refresh_dependents_for_run(
                        self.store,
                        run_id=str(getattr(parent_work_item, "run_id", "") or "").strip(),
                        source_work_item_id=parent_work_item_id,
                        source_task_id=task.id,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "park_for_settled_failures: frontier refresh failed for "
                        f"{parent_work_item_id}"
                    )
                return True
            return False
        task.metadata["delegation_pending_work_item_ids"] = pending_dependency_ids
        await self._append_progress(
            task,
            "Waiting on delegated child work items: "
            + ", ".join(pending_dependency_ids[:8])
            + (" ..." if len(pending_dependency_ids) > 8 else ""),
        )
        park_metadata_updates: dict[str, Any] = {
            "dependency_work_item_ids": dependency_ids,
            "waiting_on_work_item_ids": pending_dependency_ids,
            "delegated_children_pending": True,
        }
        parent_meta_now = (
            dict(getattr(parent_work_item, "metadata", {}) or {})
            if parent_work_item is not None
            else {}
        )
        if bool(parent_meta_now.get("synthesis_turn_started")) or parent_meta_now.get(
            "dependency_settlement"
        ):
            # Re-parking after a synthesis / failure-triage turn rebuilt the
            # board: reset the one-shot synthesis marker and the stale
            # settlement stamp so the next wake runs a clean synthesis pass
            # over the new children (and the old failed-dep release cannot
            # leak into the rebuilt card's runnability check).
            park_metadata_updates["synthesis_turn_started"] = False
            park_metadata_updates["dependency_settlement"] = {}
            pre_kind = str(parent_meta_now.get("pre_synthesis_work_kind", "") or "").strip()
            if pre_kind and str(parent_meta_now.get("work_kind", "") or "").strip().lower() in {
                "synthesis",
                "synthesize",
            }:
                park_metadata_updates["work_kind"] = pre_kind
                park_metadata_updates["delegation_turn_kind"] = pre_kind
        # Phase A: single phase write, hook projects task.status=BLOCKED and
        # syncs local. Replaces the old "write task.status BLOCKED, save,
        # then separately write work_item.phase=WAITING_FOR_CHILDREN" double-pass.
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.WAITING_FOR_CHILDREN,
            reason="park_for_delegated_children",
            metadata_updates=park_metadata_updates,
        )
        await self.save_task(task)
        return True

    async def _spawn_delivery_card_after_intake(
        self,
        *,
        task: Task,
        intake_work_item: Any,
        dependency_ids: list[str],
    ) -> None:
        """Create the user-facing delivery work item once intake dispatches.

        Runs once per intake (idempotent via ``intake_delivery_spawned``
        flag set by the caller). The delivery card:

            - Inherits the intake's seat/team/role identity (same top-level
              agent owns it)
            - Depends on every child the intake just delegated, so it
              auto-advances WAITING_DEPENDENCIES → READY once they all
              approve (``_refresh_delegation_dependents`` already handles
              that edge)
            - Carries ``review_owner_kind="human"`` so the final review
              goes to AWAITING_HUMAN instead of bouncing to an
              upper-level agent (there is none above the root).
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return
        run_id = str(getattr(intake_work_item, "run_id", "") or "").strip()
        if not run_id:
            return
        intake_meta = dict(getattr(intake_work_item, "metadata", {}) or {})
        role_id = str(getattr(intake_work_item, "role_id", "") or "").strip()
        delivery_projection_id = f"{role_id or 'root'}::delivery::{uuid.uuid4().hex[:8]}"
        intake_title = str(getattr(intake_work_item, "title", "") or "").strip()
        delivery_title = (
            f"Deliver final result to user: {intake_title}"
            if intake_title
            else "Deliver final result to user"
        )[:240]
        original_message = str(intake_meta.get("original_message", "") or "").strip()
        delivery_policy = {
            "user_visible": True,
            "authoritative_output": True,
            "requires_user_feedback": True,
        }
        plan_data = (
            serialized_company_plan_from_metadata(intake_meta)
            or serialized_company_plan_from_metadata(dict(intake_meta.get("delegation_playbook", {}) or {}))
        )
        if isinstance(plan_data, dict) and plan_data:
            try:
                plan = deserialize_company_work_item_runtime_plan(plan_data)
                root_projection_id = str(plan.root_projection_id or "").strip()
                raw_projection_policies: dict[str, dict[str, Any]] = {}
                for raw_projection in list(plan_data.get("projections", []) or []):
                    if not isinstance(raw_projection, dict):
                        continue
                    raw_projection_id = str(raw_projection.get("projection_id", "") or "").strip()
                    raw_policy = raw_projection.get("delivery_policy")
                    if raw_projection_id and isinstance(raw_policy, dict):
                        raw_projection_policies[raw_projection_id] = dict(raw_policy)
                for projection in plan.projections:
                    projection_id = str(projection.projection_id or "").strip()
                    if (
                        str(projection.projection_id or "").strip() == root_projection_id
                        or (
                            not root_projection_id
                            and str(projection.role_id or "").strip() == role_id
                            and str(projection.turn_type or "").strip() == "intake"
                        )
                    ):
                        raw_policy = raw_projection_policies.get(projection_id)
                        if raw_policy:
                            for key in ("user_visible", "authoritative_output"):
                                if key in raw_policy:
                                    delivery_policy[key] = bool(raw_policy.get(key))
                            if bool(raw_policy.get("requires_user_feedback", False)):
                                delivery_policy["requires_user_feedback"] = True
                        break
            except Exception:
                logger.opt(exception=True).debug("Failed to read delivery policy from intake work-item plan")
        # Owner-facing synthetic delivery cards are the stable handoff point
        # for follow-up directives. A projection-level false must not suppress
        # the human review card; review closure is an explicit runtime tool.
        delivery_policy["requires_user_feedback"] = True
        delivery_metadata = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": str(intake_meta.get("runtime_model", "") or "multi_team_org").strip(),
            "session_scope_id": task_session_scope_id(task) or str(intake_meta.get("session_scope_id", "") or "").strip(),
            "delegation_turn_kind": "delivery",
            "team_id": str(getattr(intake_work_item, "team_id", "") or intake_meta.get("team_id", "") or "").strip(),
            "team_instance_id": str(getattr(intake_work_item, "team_instance_id", "") or "").strip(),
            "seat_id": str(getattr(intake_work_item, "seat_id", "") or intake_meta.get("seat_id", "") or "").strip(),
            "seat_state_id": str(getattr(intake_work_item, "seat_state_id", "") or intake_meta.get("seat_state_id", "") or "").strip(),
            "batch_id": str(getattr(intake_work_item, "batch_id", "") or "").strip(),
            "work_kind": "delivery",
            "manager_role_id": str(getattr(intake_work_item, "manager_role_id", "") or "").strip(),
            "manager_seat_id": str(getattr(intake_work_item, "manager_seat_id", "") or "").strip(),
            "dependency_work_item_ids": list(dependency_ids),
            "waiting_on_work_item_ids": list(dependency_ids),
            "assigned_role_runtime_id": str(getattr(intake_work_item, "role_runtime_session_id", "") or intake_meta.get("assigned_role_runtime_id", "") or "").strip(),
            "contact_role_ids": list(intake_meta.get("contact_role_ids", []) or []),
            "allowed_delegate_role_ids": list(intake_meta.get("allowed_delegate_role_ids", []) or []),
            "delegation_playbook": dict(intake_meta.get("delegation_playbook", {}) or {}),
            "comms_workspace_root": str(intake_meta.get("comms_workspace_root", "") or "").strip(),
            "target_output_dir": str(intake_meta.get("target_output_dir", "") or "").strip(),
            "review_owner_kind": "human",
            "original_message": original_message,
            "intake_work_item_id": str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            "user_visible": bool(delivery_policy.get("user_visible", True)),
            "authoritative_output": bool(delivery_policy.get("authoritative_output", True)),
            "requires_user_feedback": bool(delivery_policy.get("requires_user_feedback", True)),
            "feedback_scope": "final",
        }, version=work_item_runtime_version(intake_meta)),
            projection_id=delivery_projection_id,
            turn_type="deliver",
        )
        delivery_work_item = DelegationWorkItem(
            run_id=run_id,
            cell_id=str(getattr(intake_work_item, "cell_id", "") or "").strip() or role_id,
            team_instance_id=delivery_metadata["team_instance_id"],
            team_id=delivery_metadata["team_id"],
            role_id=role_id,
            seat_id=delivery_metadata["seat_id"],
            seat_state_id=delivery_metadata["seat_state_id"],
            role_runtime_session_id=delivery_metadata["assigned_role_runtime_id"],
            parent_work_item_id=str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            source_role_id=role_id or None,
            source_seat_id=delivery_metadata["seat_id"] or None,
            title=delivery_title,
            summary=(
                "Synthesise all sub-team approved outputs and hand a final, "
                "user-facing result back to the requester. Do not re-delegate unless "
                "a critical gap is discovered — the team's work is done."
            ),
            kind="delivery",
            projection_id=delivery_projection_id,
            phase=Phase.WAITING_DEPENDENCIES,
            batch_id=delivery_metadata["batch_id"],
            batch_index=int(getattr(intake_work_item, "batch_index", 0) or 0) + 1,
            continuation_source=str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            manager_role_id=delivery_metadata["manager_role_id"],
            manager_seat_id=delivery_metadata["manager_seat_id"],
            metadata=delivery_metadata,
        )
        await self.store.save_delegation_work_item(delivery_work_item)
        if hasattr(self.store, "save_delegation_event"):
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=run_id,
                        work_item_id=delivery_work_item.work_item_id,
                        cell_id=delivery_work_item.cell_id,
                        role_id=delivery_work_item.role_id,
                        event_type="delivery_work_item_created",
                        payload={
                            "intake_work_item_id": delivery_metadata["intake_work_item_id"],
                            "dependency_work_item_ids": list(dependency_ids),
                        },
                    )
                )
            except Exception:
                logger.debug("Best-effort delivery work-item event persistence failed")

    async def _park_for_blocking_comms(self, task: Task) -> bool:
        """If `task`'s role sent any blocking messages this turn whose
        replies have not yet arrived, park the work item in AWAITING_PEER.

        Returns True if parking happened (caller should bail out of
        the normal completion flow), False if there is nothing to wait
        for. Tolerant of missing comms layout / missing role.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return False
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return False
        try:
            unresolved = _comms.find_unresolved_blocking_outbox(layout, role_id)
        except Exception:
            return False
        if not unresolved:
            return False

        task.metadata = dict(task.metadata)
        peer_wait = dict(task.metadata.get("peer_wait", {}) or {})
        peer_wait["kind"] = "comms_blocking"
        peer_wait["blocking_message_ids"] = [h.message_id for h in unresolved]
        peer_wait["awaiting_replies_from"] = sorted({h.to_role for h in unresolved})
        peer_wait["parked_at"] = datetime.now(timezone.utc).isoformat()
        task.metadata["peer_wait"] = peer_wait
        for h in unresolved:
            await self._append_progress(
                task,
                f"Parked awaiting blocking reply: msg `{h.message_id}` "
                f"sent to `{h.to_role}` ({h.subject!r}).",
            )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.WAITING_FOR_PEER,
            reason="park_for_blocking_comms",
        )
        await self.save_task(task)
        return True

    async def _try_unpark_blocking_comms(self, task: Task) -> bool:
        """For an AWAITING_PEER task with kind=comms_blocking, check
        whether all expected replies have arrived. If yes, unpark by
        flipping back to PENDING and recording the reply paths in
        metadata so the next prompt can surface them.

        Returns True if the task was unparked, False otherwise.
        """
        if task.status != TaskStatus.AWAITING_PEER:
            return False
        peer_wait = dict(task.metadata.get("peer_wait", {}) or {})
        wait_kind = str(peer_wait.get("kind") or "")
        # An empty kind is an orphaned wait (e.g. a legacy resolver popped
        # `peer_wait` while the work item stayed WAITING_FOR_PEER); those
        # are recoverable from the durable comms state below. Waits with a
        # different explicit kind (meeting, message-id) have their own
        # resolvers.
        if wait_kind and wait_kind != "comms_blocking":
            return False
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return False
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return False
        blocking_ids = list(peer_wait.get("blocking_message_ids", []) or [])
        if not blocking_ids:
            # No recorded ids (orphaned or empty wait): fall back to the
            # park predicate itself — any unanswered blocking outbox
            # message keeps the task parked, none means release.
            if _comms.find_unresolved_blocking_outbox(layout, role_id):
                return False
            task.metadata = dict(task.metadata)
            task.metadata.pop("peer_wait", None)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="unpark_blocking_comms_empty",
            )
            await self.save_task(task)
            return True
        replies: dict[str, str] = {}
        for mid in blocking_ids:
            reply = _comms.find_reply_to(layout, role_id, mid)
            if reply is None:
                return False  # at least one still unresolved
            replies[str(mid)] = str(reply.path)
        # All replies present — unpark.
        task.metadata = dict(task.metadata)
        task.metadata["comms_resolved_blocking_replies"] = replies
        task.metadata.pop("peer_wait", None)
        await self._append_progress(
            task,
            f"All blocking replies received ({len(replies)}); resuming work item.",
        )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="unpark_blocking_comms_replies_received",
        )
        await self.save_task(task)
        return True

    async def _enforce_work_item_contracts(self, task: Task, result: TaskResult) -> list[str]:
        issues: list[str] = []
        projection_id = self._projection_id_for_task(task)
        work_item_turn_type = self._turn_type_for_task(task)
        ownership_contract = dict(task.metadata.get("ownership_contract", {}) or {})
        verification_required = bool(task.metadata.get("work_item_verification_required", False))
        output_metadata = self._work_item_output_metadata_for_task(task)
        artifact_index = list(output_metadata.get("work_item_artifact_index", []) or [])
        work_item_summary = str(output_metadata.get("work_item_summary", "") or "").strip()
        verification_status = dict(output_metadata.get("verification_status", {}) or {})
        verification_evidence = dict(output_metadata.get("verification_evidence", {}) or {})

        if ownership_contract and work_item_turn_type == "execute":
            if not work_item_summary:
                issues.append("Ownership/artifact contract violation: missing work-item summary.")
            if not artifact_index:
                issues.append("Ownership/artifact contract violation: missing work-item artifact index.")
            if verification_required and not verification_status:
                issues.append("Verification is required for this work item but no verification_status was recorded.")
            if verification_required and not self._verification_evidence_satisfies_contract(verification_evidence):
                issues.append("Verification evidence is missing or incomplete for a verification-required execute work item.")

        # --- Collaboration awareness check for parallel work items ---
        collaboration_warning = await self._check_collaboration_awareness(task)
        if collaboration_warning:
            task.metadata = dict(task.metadata)
            task.metadata["_collaboration_awareness_warning"] = collaboration_warning

        if issues and await self._prepare_contract_rework(task, issues):
            return issues

        if issues:
            task.metadata["artifact_contract_status"] = "failed"
            contract_failure_status = (
                TaskStatus.AWAITING_MANAGER_REVIEW
                if work_item_turn_type == "execute"
                else TaskStatus.FAILED
            )
            await self._append_progress(task, "Work-item contract enforcement failed.")
            for issue in issues:
                await self._append_progress(task, issue)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=contract_failure_status,
                reason="contract_enforcement_failed",
            )
            await self.save_task(task)
            await self._emit_runtime_signal(
                "artifact_contract_failed",
                {
                    "task_id": task.id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                    "member_session_id": task.metadata.get("member_session_id", ""),
                    "issues": list(issues),
                },
            )
            if verification_required and not self._verification_evidence_satisfies_contract(verification_evidence):
                await self._emit_runtime_signal(
                    "verification_required",
                    {
                        "task_id": task.id,
                        **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                        "reason": "missing_verification_evidence",
                    },
                )
            await self._emit_progress(
                f"[Company:{projection_id}] contract enforcement failed",
                task_id=task.id,
            )
            return issues

        if ownership_contract and work_item_turn_type == "execute":
            task.metadata["artifact_contract_status"] = "satisfied"
        else:
            task.metadata["artifact_contract_status"] = task.metadata.get("artifact_contract_status", "not_required")
        if verification_evidence:
            await self._emit_runtime_signal(
                "verification_completed",
                {
                    "task_id": task.id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                    "verification_evidence": dict(verification_evidence),
                },
            )
        return []

    @staticmethod
    def _contract_rework_limit(task: Task) -> int:
        try:
            value = int(task.metadata.get("contract_rework_max_retries", _DEFAULT_CONTRACT_REWORK_MAX_RETRIES) or 0)
        except Exception:
            value = _DEFAULT_CONTRACT_REWORK_MAX_RETRIES
        return max(0, value)

    @staticmethod
    def _contract_issue_retriable(issue: str) -> bool:
        normalized = str(issue or "").strip().lower()
        if not normalized:
            return False
        non_retriable_markers = (
            "acknowledgement is still pending",
        )
        return not any(marker in normalized for marker in non_retriable_markers)

    async def _check_collaboration_awareness(self, task: Task) -> str:
        """Check whether a completing work item had any meaningful collaboration with parallel peers.

        Returns a warning string if the work item had parallel peers but zero inter-agent
        messages (questions, DMs, broadcasts, etc.).  The warning is informational —
        it does NOT block completion — but it is injected into downstream context so
        that QA / review work items can flag potential integration gaps.
        """
        parallel_peer_count = int(task.metadata.get("_parallel_peer_count", 0) or 0)
        if parallel_peer_count == 0:
            return ""
        # Count non-handoff messages sent or received by this role during execution
        comm_store = getattr(self.communication, "store", None) if self.communication else None
        if comm_store is None:
            return ""
        role_id = str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        if not role_id:
            return ""
        peer_roles = {
            str(peer.get("role_id", "")).strip()
            for peer in list(task.metadata.get("_work_item_plan_projections", []) or [])
            if isinstance(peer, dict) and str(peer.get("role_id", "")).strip() and str(peer.get("role_id", "")).strip() != role_id
        }
        try:
            all_messages = await comm_store.get_messages_for_agent(
                agent_id=role_id,
                task_id=task.id,
                unread_only=False,
                limit=50,
            )
        except Exception:
            return ""
        # Also count messages this role *sent* (not just received)
        try:
            sent_messages = await comm_store.get_outbox_for_agent(
                agent_id=role_id,
                task_id=task.id,
                limit=50,
            )
        except (AttributeError, TypeError):
            sent_messages = []
        collaboration_types = {"question", "answer", "flag_issue", "decision_needed", "request_review"}
        collab_message_ids: set[str] = set()
        for msg in list(all_messages) + list(sent_messages):
            msg_type = str(getattr(msg, "msg_type", "") or "").strip()
            from_agent = str(getattr(msg, "from_agent", "") or "").strip()
            recipients = {str(item).strip() for item in list(getattr(msg, "to_agents", []) or []) if str(item).strip()}
            if msg_type in collaboration_types and (
                from_agent in peer_roles
                or bool(peer_roles & recipients)
                or not peer_roles
            ):
                collab_message_ids.add(str(getattr(msg, "msg_id", "") or "").strip())
        layout = self._comms_layout_for_task(task)
        if layout is not None:
            try:
                from opc.layer2_organization import comms as _comms

                file_headers = _comms.list_role_messages(
                    layout,
                    role_id,
                    include_new=True,
                    include_seen=True,
                    include_outbox=True,
                )
            except Exception:
                file_headers = []
            for header in file_headers:
                msg_id = str(header.message_id or header.path.name).strip()
                if not msg_id:
                    continue
                fm = dict(header.raw_frontmatter or {})
                semantic_type = str(fm.get("semantic_type") or fm.get("kind") or "").strip().lower()
                msg_type = str(fm.get("msg_type") or "").strip().lower()
                peer_involved = (
                    header.from_role in peer_roles
                    or header.to_role in peer_roles
                    or not peer_roles
                )
                if not peer_involved:
                    continue
                if msg_type in collaboration_types or semantic_type in {
                    "work_update",
                    "blocked_on_decision",
                } or bool(header.blocking):
                    collab_message_ids.add(msg_id)
        collab_count = len({item for item in collab_message_ids if item})
        if collab_count > 0:
            return ""
        peer_projections = list(task.metadata.get("_work_item_plan_projections", []) or [])
        peer_names = ", ".join(
            str(p.get("role_id", "")).strip()
            for p in peer_projections[:6]
            if isinstance(p, dict) and str(p.get("role_id", "")).strip()
        )
        return (
            f"Work item `{self._projection_id_for_task(task)}` (role: {role_id}) "
            f"completed with {parallel_peer_count} parallel peer(s) ({peer_names}) "
            f"but had ZERO inter-agent collaboration messages. "
            f"Potential integration gaps may exist."
        )

    def _build_contract_rework_record(
        self,
        *,
        task: Task,
        issues: list[str],
        rework_round: int,
        max_retries: int,
    ) -> dict[str, Any]:
        return {
            "task_id": task.id,
            **work_item_identity_payload_for_task(task),
            "work_item_title": task.title,
            "issues": [str(item).strip() for item in issues if str(item).strip()],
            "rework_round": rework_round,
            "max_retries": max_retries,
            "requested_at": datetime.now().isoformat(),
        }

    def _render_contract_rework_summary(self, rework_request: dict[str, Any]) -> str:
        work_item_title = (
            str(rework_request.get("work_item_title", "")).strip()
            or str(rework_request.get("work_item_projection_title", "")).strip()
            or "Current work item"
        )
        issues = [
            str(item).strip()
            for item in list(rework_request.get("issues", []) or [])
            if str(item).strip()
        ]
        rework_round = int(rework_request.get("rework_round", 1) or 1)
        max_retries = int(rework_request.get("max_retries", _DEFAULT_CONTRACT_REWORK_MAX_RETRIES) or _DEFAULT_CONTRACT_REWORK_MAX_RETRIES)
        lines = [
            f"Contract rework requested for {work_item_title}.",
            f"Round: {rework_round}/{max_retries}",
            "Your previous submission was incomplete. Fix every missing item below before finishing again.",
        ]
        if issues:
            lines.append("Missing required outputs:")
            lines.extend(f"- {issue}" for issue in issues)
        lines.append(
            "Do not stop at a high-level summary. Produce the missing summary, artifact index, verification evidence, and handoff details explicitly in your next completion."
        )
        return "\n".join(lines)

    @staticmethod
    def _reset_contract_outputs_for_retry(task: Task) -> None:
        for key in (
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
            "gate_harness_status",
            "gate_harness_constraints",
            "gate_harness_pending_decision",
            "gate_harness_decision",
            "gate_harness_evidence",
        ):
            task.metadata.pop(key, None)
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot.pop("latest_artifacts", None)
        task.context_snapshot.pop("work_item_owned_outputs", None)

    async def _prepare_contract_rework(self, task: Task, issues: list[str]) -> bool:
        work_item_turn_type = self._turn_type_for_task(task)
        if work_item_turn_type != "execute":
            return False
        projection_id = self._projection_id_for_task(task)
        normalized_issues = [str(item).strip() for item in issues if str(item).strip()]
        if not normalized_issues or not all(self._contract_issue_retriable(issue) for issue in normalized_issues):
            return False
        rework_count = int(task.metadata.get("contract_rework_count", 0) or 0)
        max_retries = self._contract_rework_limit(task)
        if rework_count >= max_retries:
            return False

        rework_round = rework_count + 1
        rework_request = self._build_contract_rework_record(
            task=task,
            issues=normalized_issues,
            rework_round=rework_round,
            max_retries=max_retries,
        )
        task.metadata = dict(task.metadata)
        task.metadata["contract_rework_count"] = rework_round
        task.metadata["contract_rework_feedback"] = "\n".join(normalized_issues)
        task.metadata["contract_rework_request"] = dict(rework_request)
        task.metadata["artifact_contract_status"] = "reworking"
        task.metadata["_retry_contract_enforcement"] = True
        task.result = None
        self._reset_contract_outputs_for_retry(task)
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="contract_rework_retry",
        )
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["latest_contract_rework"] = dict(rework_request)
        await self._append_progress(task, self._render_contract_rework_summary(rework_request))
        await self.save_task(task)
        await self._emit_runtime_signal(
            "artifact_contract_retry",
            {
                "task_id": task.id,
                **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                "rework_round": rework_round,
                "max_retries": max_retries,
                "issues": normalized_issues,
            },
        )
        await self._emit_progress(
            f"[Company:{projection_id}] reworking contract enforcement ({rework_round}/{max_retries})",
            task_id=task.id,
        )
        return True

    def _record_gate_harness_history(self, task: Task, decision: GateHarnessDecision) -> None:
        history = [
            dict(item)
            for item in list(task.metadata.get("gate_harness_history", []) or [])
            if isinstance(item, dict)
        ]
        history.append(
            {
                "action": decision.action,
                "summary": decision.summary,
                "target_projection_id": target_projection_id_for_decision(decision),
                "blocker_fingerprint": decision.blocker_fingerprint,
                "blocker_types": list(decision.blocker_types),
                "recorded_at": datetime.now().isoformat(),
            }
        )
        task.metadata["gate_harness_history"] = history[-12:]

    def _build_gate_harness_rework_record(
        self,
        *,
        source_task: Task,
        target_task: Task,
        decision: GateHarnessDecision,
        rework_round: int,
    ) -> dict[str, Any]:
        target_projection_id = self._projection_id_for_task(target_task)
        return {
            "source_projection_id": self._projection_id_for_task(source_task),
            "source_work_item_title": source_task.title,
            **gate_rework_payload(target_projection_id=target_projection_id),
            "target_work_item_title": target_task.title,
            "feedback": decision.summary,
            "blockers": list(decision.blockers),
            "blocker_types": list(decision.blocker_types),
            "constraints": list(decision.constraints),
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }

    def _render_gate_harness_rework_summary(self, request: dict[str, Any]) -> str:
        lines = [
            f"Gate harness requested rework for {str(request.get('target_work_item_title', '') or 'current work item').strip()}.",
            f"Requested by: {str(request.get('source_work_item_title', '') or 'runtime harness').strip()}",
            f"Round: {int(request.get('rework_round', 1) or 1)}",
        ]
        feedback = str(request.get("feedback", "") or "").strip()
        if feedback:
            lines.append(f"## Gate Harness Summary\n{feedback}")
        blockers = [
            str(item).strip()
            for item in list(request.get("blockers", []) or [])
            if str(item).strip()
        ]
        if blockers:
            lines.append("## Blocking Findings\n" + "\n".join(f"- {item}" for item in blockers))
        constraints = [
            str(item).strip()
            for item in list(request.get("constraints", []) or [])
            if str(item).strip()
        ]
        if constraints:
            lines.append("## Constraints To Preserve\n" + "\n".join(f"- {item}" for item in constraints))
        lines.append("Resume the prior work-item session, fix the blocking issues above, and resubmit.")
        return "\n\n".join(lines)

    async def _gate_harness_initiate_rework(
        self,
        source_task: Task,
        decision: GateHarnessDecision,
        task_by_projection_id: dict[str, Task],
    ) -> Task | None:
        touched_task_ids: set[str] = set()
        target_projection_ids = target_projection_ids_for_decision(decision)
        if not target_projection_ids:
            target_projection_ids = [self._projection_id_for_task(source_task)]
        primary_target: Task | None = None
        for target_projection_id in target_projection_ids:
            target_task = task_by_projection_id.get(target_projection_id)
            if target_task is None:
                continue
            if primary_target is None:
                primary_target = target_task
            rework_round = int(target_task.metadata.get("gate_harness_rework_count", 0) or 0) + 1
            request = self._build_gate_harness_rework_record(
                source_task=source_task,
                target_task=target_task,
                decision=decision,
                rework_round=rework_round,
            )
            affected_projection_ids = [target_projection_id, *self._collect_downstream_projection_ids(target_projection_id)]
            for affected_projection_id in affected_projection_ids:
                affected_task = task_by_projection_id.get(affected_projection_id)
                if affected_task is None or affected_task.id in touched_task_ids:
                    continue
                touched_task_ids.add(affected_task.id)
                affected_task.metadata = dict(affected_task.metadata)
                affected_task.context_snapshot = dict(affected_task.context_snapshot)
                affected_task.result = None
                self._reset_work_item_outputs_for_rework(affected_task)
                if affected_projection_id == target_projection_id:
                    affected_task.metadata["gate_harness_rework_count"] = rework_round
                    affected_task.metadata["gate_harness_rework_feedback"] = decision.summary
                    affected_task.metadata["gate_harness_rework_request"] = dict(request)
                    history = list(affected_task.metadata.get("gate_harness_rework_requests", []) or [])
                    history.append(dict(request))
                    affected_task.metadata["gate_harness_rework_requests"] = history[-6:]
                    affected_task.context_snapshot["latest_gate_harness_rework"] = dict(request)
                    await self._append_progress(affected_task, self._render_gate_harness_rework_summary(request))
                else:
                    affected_task.metadata["upstream_gate_harness_rework_source_projection_id"] = target_projection_id
                    affected_task.context_snapshot["upstream_gate_harness_rework_source_projection_id"] = target_projection_id
                    adaptive = self._normalize_adaptive_metadata(affected_task.metadata.get("adaptive", {}))
                    adaptive["normalized_state"] = "invalidated"
                    affected_task.metadata["adaptive"] = adaptive
                    await self._append_progress(
                        affected_task,
                        f"Reset because upstream work-item projection `{target_projection_id}` entered gate-harness rework.",
                    )
                await transition_work_item_from_task(
                    self.store, affected_task,
                    target_status_or_phase=TaskStatus.PENDING,
                    reason="gate_harness_rework_reset",
                )
                await self.save_task(affected_task)
                # Emit work_item_progress event so the UI reverts the work item from
                # "done" (checkmark) back to "active" (dots) during rework.
                await self._emit_progress(
                    f"[Company:{affected_projection_id}] reworking (gate harness rework round {rework_round})",
                    task_id=affected_task.id,
                )
        return primary_target

    def _render_gate_harness_checkpoint_prompt(self, task: Task, decision: GateHarnessDecision) -> str:
        action_label = {
            "await_user_decision": "user decision",
            "escalate": "manual approval",
            "replan": "runtime replan",
        }.get(decision.action, "runtime decision")
        lines = [
            f"Gate harness recommends `{decision.action}` for work item `{task.title}`.",
            decision.summary,
        ]
        blockers = [str(item).strip() for item in list(decision.blockers or []) if str(item).strip()]
        if blockers:
            lines.append("Blocking findings:")
            lines.extend(f"- {item}" for item in blockers[:6])
        constraints = [str(item).strip() for item in list(decision.constraints or []) if str(item).strip()]
        if constraints:
            lines.append("Constraints if you continue:")
            lines.extend(f"- {item}" for item in constraints[:6])
        lines.append(
            f"Reply `approve` / `continue` to accept this {action_label} handling, or `deny` / `stop` to reject it."
        )
        return "\n".join(lines).strip()

    async def _pause_for_gate_harness_decision(
        self,
        task: Task,
        decision: GateHarnessDecision,
        *,
        review_level: str,
        review_target_role_id: str = "",
        review_chain_role_ids: list[str] | None = None,
    ) -> None:
        normalized_review_level = str(review_level or "").strip().lower() or "human"
        decision_target_projection_id = target_projection_id_for_decision(decision)
        task.status = self._review_status_for_level(normalized_review_level)
        task.metadata = dict(task.metadata)
        task.metadata["gate_harness_pending_decision"] = decision.to_dict()
        task.metadata["gate_harness_review_level"] = normalized_review_level
        task.metadata["gate_harness_review_target_role_id"] = str(review_target_role_id or "").strip()
        task.metadata["gate_harness_review_chain_role_ids"] = [
            str(item).strip()
            for item in list(review_chain_role_ids or [])
            if str(item).strip()
        ]
        await self._append_progress(
            task,
            f"Gate harness paused the work item with action `{decision.action}` for {normalized_review_level} review.",
        )
        await self._append_progress(task, decision.summary)
        await self.save_task(task)
        gate = WorkItemGatePolicy(
            gate_type="review" if normalized_review_level == "manager" else "human_confirmation",
            instructions=decision.summary,
            reviewer_role=str(review_target_role_id or "").strip() or None,
            requires_human=normalized_review_level != "manager",
            on_reject="rework" if decision_target_projection_id else "halt",
            rework_projection_id=decision_target_projection_id or None,
            max_retries=1,
            metadata={
                "source": "gate_harness",
                "recommended_action": decision.action,
                "review_level": normalized_review_level,
                "review_target_role_id": str(review_target_role_id or "").strip(),
                "review_chain_role_ids": [
                    str(item).strip()
                    for item in list(review_chain_role_ids or [])
                    if str(item).strip()
                ],
                "constraints": list(decision.constraints),
                "blockers": list(decision.blockers),
                "blocker_types": list(decision.blocker_types),
                "prompt_override": self._render_gate_harness_checkpoint_prompt(task, decision),
                **gate_rework_payload(
                    rework_projection_id=decision_target_projection_id,
                ),
            },
        )
        if decision_target_projection_id:
            mark_gate_rework_projection(gate, decision_target_projection_id)
        await self._save_checkpoint(task, gate)

    async def _apply_gate_harness(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> str:
        harness = self._gate_harness_for_task(task)
        if not harness.policy.enabled:
            return "pass"
        task.metadata = dict(task.metadata)
        packet, decision = await harness.evaluate(task, task_by_projection_id)
        task.metadata["gate_harness_evidence"] = packet.to_dict()
        task.metadata["gate_harness_decision"] = decision.to_dict()
        self._record_gate_harness_history(task, decision)

        if decision.action == "pass":
            task.metadata["gate_harness_status"] = "passed"
            task.metadata.pop("gate_harness_constraints", None)
            task.metadata.pop("gate_harness_pending_decision", None)
            return "pass"

        if decision.action == "pass_with_constraints":
            task.metadata["gate_harness_status"] = "passed_with_constraints"
            task.metadata["gate_harness_constraints"] = list(decision.constraints or decision.blockers or decision.residual_risks)
            merged_risks = self._merge_unique_items(
                list(self._work_item_output_metadata_for_task(task).get("risks", []) or task.metadata.get("risks", []) or []),
                list(task.metadata["gate_harness_constraints"]),
            )
            linked_work_item_id = linked_work_item_id_for_task(task)
            if linked_work_item_id:
                self._set_work_item_output_context(task, {"risks": merged_risks})
                await update_work_item_owned_metadata(self.store, linked_work_item_id, {"risks": merged_risks})
                task.metadata.pop("risks", None)
            else:
                task.metadata["risks"] = merged_risks
            task.metadata.pop("gate_harness_pending_decision", None)
            await self._append_progress(task, f"Gate harness allowed the work item to continue with constraints: {decision.summary}")
            await self.save_task(task)
            return "pass_with_constraints"

        if decision.action == "rerun_work_item":
            task.result = None
            task.metadata["gate_harness_status"] = "rerun_pending"
            await self._append_progress(task, decision.summary)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="gate_harness_rerun_work_item",
            )
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness requested a rerun",
                task_id=task.id,
            )
            return "rerun_work_item"

        if decision.action == "rework_same_work_item":
            rework_task = await self._gate_harness_initiate_rework(task, decision, task_by_projection_id)
            if rework_task is None:
                failed_target = target_projection_id_for_decision(decision)
                task.metadata["gate_harness_status"] = "rework_failed"
                await self._append_progress(task, f"Gate harness could not restore rework target `{failed_target}`.")
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="gate_harness_rework_failed",
                )
                await self.save_task(task)
                return "rework_failed"
            task.metadata["gate_harness_status"] = "reworking"
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness requested {decision.action}",
                task_id=task.id,
            )
            return decision.action

        if decision.action in {"await_user_decision", "replan", "escalate"}:
            task.metadata["gate_harness_status"] = "awaiting_decision"
            review_chain = self._review_chain_for_task(task)
            review_level = "human"
            review_target_role_id = ""
            if decision.action in {"replan", "escalate"} and review_chain:
                review_level = "manager"
                review_target_role_id = review_chain[0]
            await self._pause_for_gate_harness_decision(
                task,
                decision,
                review_level=review_level,
                review_target_role_id=review_target_role_id,
                review_chain_role_ids=review_chain,
            )
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness paused for {decision.action} ({review_level} review)",
                task_id=task.id,
            )
            return decision.action

        task.metadata["gate_harness_status"] = "passed"
        return "pass"

    async def _finalize_work_item_with_gate_harness(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> str:
        action = await self._apply_gate_harness(task, task_by_projection_id)
        if action not in {"pass", "pass_with_constraints"}:
            return action
        await self._finalize_completed_work_item(task)
        return action

    async def _apply_gate(self, task: Task, gate: WorkItemGatePolicy, task_by_projection_id: dict[str, Task]) -> None:
        if gate.gate_type == "automated_verification":
            await self._apply_automated_verification_gate(task, gate)
            return

        metadata = {
            "role_id": task.assigned_to,
            "gate_type": gate.gate_type,
            **work_item_identity_payload_for_task(task),
        }
        approved = True
        decision = None
        if gate.gate_type in {"approval", "human_confirmation"} or gate.requires_human:
            approved, decision = await self.approval_engine.authorize_work_item_action(
                task=task,
                work_item_title=task.title,
                metadata=metadata,
                on_progress=self.progress_callback,
                force_human=(gate.gate_type == "human_confirmation" or gate.requires_human),
            )
        if decision and decision.action == ApprovalAction.REQUIRE_INPUT:
            review_level = "manager" if gate.reviewer_role and not gate.requires_human else "human"
            task.status = self._review_status_for_level(review_level)
            await self._append_progress(
                task,
                "Awaiting manager review." if review_level == "manager" else "Awaiting human confirmation.",
            )
            await self.save_task(task)
            await self._save_checkpoint(task, gate)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting {'manager review' if review_level == 'manager' else 'confirmation'}",
                task_id=task.id,
            )
            return
        verdict = self._structured_or_inferred_verdict(task, gate)
        if gate.gate_type == "approval" and not approved:
            verdict = "reject"
        if gate.gate_type == "human_confirmation":
            if not approved:
                verdict = "reject"
            else:
                verdict = "approve"

        if verdict == "reject":
            reviewer_feedback = self._structured_review_feedback(task)
            if not reviewer_feedback and task.result and isinstance(task.result, dict) and task.result.get("content"):
                reviewer_feedback = str(task.result["content"]).strip()
            rework_task = await self.prepare_gate_rework(
                task,
                gate,
                task_by_projection_id,
                reviewer_feedback,
            )
            if rework_task:
                rework_projection_id = rework_projection_id_for_gate(gate)
                await self._append_progress(task, f"Gate rejected output. Reworking work item {rework_projection_id}.")
                if rework_task is not task:
                    await self.save_task(rework_task)
                await self.save_task(task)
                await self._emit_progress(
                    f"[Company:{self._projection_id_for_task(task)}] rejected; reworking {rework_projection_id}",
                    task_id=task.id,
                )
                return

            await self._append_progress(task, "Gate rejected output and no rework remained.")
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="gate_rejected_no_rework",
            )
            await self.save_task(task)
            await self._emit_progress(f"[Company:{self._projection_id_for_task(task)}] rejected", task_id=task.id)
            return

        if await self._block_completion_for_unread_inbox(task):
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] inbox gate pending",
                task_id=task.id,
            )
            return

        await self._append_progress(task, f"Gate {gate.gate_type} passed.")
        await self._apply_done_transition(task)
        await self.save_task(task)
        completion_action = await self._finalize_work_item_with_gate_harness(task, task_by_projection_id)
        if task.status == TaskStatus.DONE:
            await self._emit_progress(f"[Company:{self._projection_id_for_task(task)}] gate passed", task_id=task.id)
        elif task.status in _REVIEW_WAITING_STATUSES:
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting {'manager review' if task.status == TaskStatus.AWAITING_MANAGER_REVIEW else 'confirmation'}",
                task_id=task.id,
            )

    async def _apply_automated_verification_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        """Run verification commands from the gate metadata and auto-pass/fail."""
        import sys as _sys
        is_windows = _sys.platform.startswith("win")
        readiness_artifact = str(gate.metadata.get("readiness_artifact", "") or "").strip()
        if readiness_artifact == "workspace_manifest":
            await self._apply_workspace_manifest_gate(task, gate)
            return
        if readiness_artifact == "data_acquisition_report":
            await self._apply_data_acquisition_gate(task, gate)
            return

        verification_commands = list(gate.metadata.get("verification_commands", []) or [])
        if is_windows:
            verification_commands = list(gate.metadata.get("verification_commands_win", []) or []) or verification_commands
        if not verification_commands:
            manifest = dict(task.metadata.get("environment_manifest", {}) or {})
            checks_key = "verification_checks_win" if is_windows else "verification_checks"
            fallback_key = "verification_checks"
            checks = list(manifest.get(checks_key, []) or []) or list(manifest.get(fallback_key, []) or [])
            verification_commands = [
                check.get("command", "") for check in checks
                if isinstance(check, dict) and check.get("command")
            ]
        if not verification_commands:
            await self._append_progress(task, "Automated verification gate: no commands to verify, auto-pass.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            return

        from opc.layer4_tools.shell import bash_exec, powershell_exec
        exec_fn = powershell_exec if is_windows else bash_exec
        all_passed = True
        check_results: list[dict[str, Any]] = []
        for cmd in verification_commands:
            cmd_str = str(cmd).strip()
            if not cmd_str:
                continue
            result = await exec_fn(command=cmd_str, timeout=60)
            passed = result.get("success", False)
            check_results.append({
                "command": cmd_str,
                "passed": passed,
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr", "")),
                "platform": "windows" if is_windows else ("macos" if _sys.platform == "darwin" else "linux"),
            })
            if not passed:
                all_passed = False

        task.metadata["automated_verification_results"] = check_results

        if all_passed:
            await self._append_progress(
                task,
                f"Automated verification gate passed: {len(check_results)}/{len(check_results)} checks succeeded.",
            )
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
        else:
            failed = [c for c in check_results if not c["passed"]]
            rework_task = await self.prepare_gate_rework(
                task,
                gate,
                {
                    task.id: task,
                    self._projection_id_for_task(task): task,
                },
                "",
            )
            if rework_task:
                feedback = "\n".join(
                    f"FAILED: {c['command']} (exit {c['exit_code']}): {c['stderr']}"
                    for c in failed
                )
                await self._append_progress(task, f"Automated verification gate failed:\n{feedback}")
                if rework_task is not task:
                    await self.save_task(rework_task)
                await self.save_task(task)
            else:
                await self._append_progress(
                    task,
                    f"Automated verification failed: {len(failed)} check(s) did not pass. No rework remaining.",
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="automated_verification_no_rework",
                )
                await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification failed",
                task_id=task.id,
            )

    async def _apply_workspace_manifest_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
        root_path = str(manifest.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip()
        required_dirs = [
            str(item).strip()
            for item in list(gate.metadata.get("required_dirs", []) or _DEFAULT_WORKSPACE_LAYOUT)
            if str(item).strip()
        ]
        check_results: list[dict[str, Any]] = []
        all_passed = bool(root_path)
        root = Path(root_path).expanduser() if root_path else None
        if root is not None:
            root_exists = root.exists()
            check_results.append({
                "command": "workspace_root_exists",
                "passed": root_exists,
                "exit_code": 0 if root_exists else 1,
                "stdout": "",
                "stderr": "" if root_exists else "Workspace root is missing.",
            })
            all_passed = all_passed and root_exists
            for relative in required_dirs:
                candidate = root / relative
                passed = candidate.exists() and candidate.is_dir()
                check_results.append({
                    "command": f"workspace_dir_exists:{relative}",
                    "passed": passed,
                    "exit_code": 0 if passed else 1,
                    "stdout": "",
                    "stderr": "" if passed else f"Missing required workspace directory `{relative}`.",
                })
                all_passed = all_passed and passed
        task.metadata["automated_verification_results"] = check_results
        if all_passed:
            await self._append_progress(task, "Workspace manifest gate passed.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
            return
        feedback = "\n".join(
            item["stderr"] or f"Check failed: {item['command']}"
            for item in check_results
            if not item.get("passed")
        ) or "Workspace manifest gate failed."
        rework_task = await self.prepare_gate_rework(
            task,
            gate,
            {
                task.id: task,
                self._projection_id_for_task(task): task,
            },
            feedback,
        )
        if rework_task:
            await self._append_progress(task, feedback)
            if rework_task is not task:
                await self.save_task(rework_task)
            await self.save_task(task)
        else:
            await self._append_progress(task, feedback)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="workspace_manifest_gate_no_rework",
            )
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] verification failed",
            task_id=task.id,
        )

    async def _apply_data_acquisition_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        report = dict(task.metadata.get("data_acquisition_report", {}) or {})
        status = str(report.get("status", "") or "").strip().lower()
        allowed = {
            str(item).strip().lower()
            for item in list(gate.metadata.get("allowed_statuses", []) or [])
            if str(item).strip()
        } or {"ready", "already_present", "not_required"}
        blocking = {
            str(item).strip().lower()
            for item in list(gate.metadata.get("blocking_statuses", []) or [])
            if str(item).strip()
        } or {"partial", "missing_critical"}
        require_attempt_evidence = bool(gate.metadata.get("require_attempt_evidence_for_blocking", False))
        valid_evidence, feedback = self._evaluate_data_acquisition_gate(
            task,
            report,
            status=status,
            allowed=allowed,
            blocking=blocking,
            require_attempt_evidence=require_attempt_evidence,
        )
        task.metadata["automated_verification_results"] = [{
            "command": "data_acquisition_status",
            "passed": status in allowed and valid_evidence,
            "exit_code": 0 if status in allowed and valid_evidence else 1,
            "stdout": "",
            "stderr": "" if status in allowed and valid_evidence else feedback,
        }]
        if status in allowed and valid_evidence:
            await self._append_progress(task, f"Data acquisition gate passed with status `{status}`.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
            return
        rework_task = await self.prepare_gate_rework(
            task,
            gate,
            {
                task.id: task,
                self._projection_id_for_task(task): task,
            },
            feedback,
        )
        if rework_task:
            await self._append_progress(task, feedback)
            if rework_task is not task:
                await self.save_task(rework_task)
            await self.save_task(task)
        else:
            await self._append_progress(task, feedback)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="data_acquisition_gate_no_rework",
            )
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] verification failed",
            task_id=task.id,
        )

    def _evaluate_data_acquisition_gate(
        self,
        task: Task,
        report: dict[str, Any],
        *,
        status: str,
        allowed: set[str],
        blocking: set[str],
        require_attempt_evidence: bool,
    ) -> tuple[bool, str]:
        if not status:
            return False, "Data acquisition report is missing or unreadable."
        present_inputs = self._normalize_data_acquisition_items(report.get("present_inputs", []))
        prepared_assets = self._normalize_data_acquisition_items(report.get("prepared_assets", []))
        attempted_sources = self._normalize_data_acquisition_items(report.get("attempted_sources", []))
        attempted_tools = self._normalize_data_acquisition_items(report.get("attempted_tools", []))
        blocked_reasons = self._normalize_data_acquisition_items(report.get("blocked_reasons", []))
        acquisition_attempted = bool(report.get("acquisition_attempted", False))
        is_media_task = requires_binary_asset_acquisition(task, report)
        designated_input_dir = str(report.get("designated_input_dir", "") or "").strip()
        download_manifest_path = str(report.get("download_manifest_path", "") or default_download_manifest_path(task)).strip()
        if status in {"ready", "already_present"}:
            if is_media_task:
                if has_downloaded_binary_asset(
                    task=task,
                    report=report,
                    download_manifest_path=download_manifest_path,
                    designated_input_dir=designated_input_dir,
                ):
                    return True, ""
                return False, (
                    "Data acquisition report is incomplete for a media task: ready/already_present requires "
                    "a download manifest with at least one downloaded binary asset prepared inside the workspace."
                )
            if present_inputs or prepared_assets:
                return True, ""
            return False, (
                "Data acquisition report is incomplete: ready/already_present requires explicit prepared assets "
                "or present inputs."
            )
        if status == "not_required":
            return True, ""
        if status in blocking:
            if not require_attempt_evidence:
                return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."
            if acquisition_attempted or attempted_sources or attempted_tools or prepared_assets or blocked_reasons:
                return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."
            return False, (
                "Data acquisition report is incomplete: blocking statuses require evidence of acquisition attempts, "
                "prepared assets, or documented blockers before the work item may stop."
            )
        return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."

    async def _record_handoffs(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Propagate upstream collaboration warnings to the current task.

        The legacy "send a handoff message per cross-role dependency" path
        (StructuredHandoff + send_handoff + file-system ``handoffs/`` mirror)
        was removed as dead code — it was gated by ``not multi_team_org`` and
        never fired in the multi-team runtime, and the filesystem
        ``handoffs/`` tree was empty across every new02+ project session.
        Downstream tasks now receive upstream context via the normal prompt-
        building path (``task.metadata['work_item_summary_for_downstream']``, set when a
        work item completes in ``_ingest_work_item_result``) plus any collaboration
        warnings that upstream roles recorded — which is all this method
        still does.
        """
        collab_warnings: list[str] = []
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            warning = str(dep_task.metadata.get("_collaboration_awareness_warning", "") or "").strip()
            if warning:
                collab_warnings.append(warning)
        if collab_warnings:
            task.metadata = dict(task.metadata)
            task.metadata["_upstream_collaboration_warnings"] = collab_warnings
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["upstream_collaboration_warnings"] = collab_warnings

    async def _mirror_fields_to_work_item(self, task: Task, keys: list[str]) -> None:
        """Persist selected WorkItem-owned task metadata to WorkItem.

        This helper is retained as the migration bridge for old call sites:
        only keys declared WorkItem-owned in metadata_ownership are persisted.
        Runtime Task-owned fields are ignored.
        """
        if not self.store:
            return
        wid = linked_work_item_id_for_task(task)
        if not wid:
            return
        if not keys:
            return
        source = task.metadata or {}
        updates: dict[str, Any] = {k: source.get(k) for k in keys if k in source}
        if not updates:
            return
        try:
            await update_work_item_owned_metadata(self.store, wid, updates)
        except Exception:
            logger.opt(exception=True).debug(
                "WorkItem-owned metadata write failed keys=%s task=%s", keys, task.id
            )

    async def _append_progress(self, task: Task, message: str) -> None:
        wid = linked_work_item_id_for_task(task)
        task.metadata = dict(task.metadata or {})
        if wid and self.store:
            task.metadata.pop("progress_log", None)
            await append_work_item_progress(self.store, wid, message)
        else:
            progress = list(task.metadata.get("progress_log", []))
            progress.append(message)
            task.metadata["progress_log"] = progress
        working_memory = list(task.metadata.get("working_memory", []))
        working_memory.append(message)
        task.metadata["working_memory"] = working_memory[-12:]

    @staticmethod
    def _normalize_gate_feedback(feedback: str, *, fallback: str) -> str:
        text = str(feedback or "").strip()
        if not text:
            text = fallback
        return clip_text(
            text,
            limit=_MAX_GATE_REVIEW_FEEDBACK_CHARS,
            marker="gate feedback preview truncated",
        ).text

    def _build_gate_rework_record(
        self,
        *,
        review_task: Task,
        gate: WorkItemGatePolicy,
        reviewer_feedback: str,
        rework_round: int,
    ) -> dict[str, Any]:
        reviewer_role = str(
            gate.reviewer_role
            or review_task.assigned_to
            or review_task.metadata.get("work_item_role_id", "")
            or ""
        ).strip()
        review_projection_id = self._projection_id_for_task(review_task)
        rework_projection_id = rework_projection_id_for_gate(gate)
        return {
            "review_task_id": review_task.id,
            **gate_rework_payload(
                review_projection_id=review_projection_id,
                target_projection_id=rework_projection_id,
            ),
            "review_work_item_title": review_task.title,
            "reviewer_role": reviewer_role,
            "feedback": reviewer_feedback,
            "gate_instructions": gate.instructions,
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }

    def _render_gate_rework_summary(self, rework_request: dict[str, Any]) -> str:
        review_work_item_title = str(rework_request.get("review_work_item_title", "")).strip() or "Gate review"
        reviewer_feedback = str(rework_request.get("feedback", "")).strip()
        gate_instructions = str(rework_request.get("gate_instructions", "")).strip()
        rework_round = int(rework_request.get("rework_round", 1) or 1)
        lines = [f"Rework requested by {review_work_item_title}.", f"Round: {rework_round}"]
        if reviewer_feedback:
            lines.append(f"## Reviewer Feedback\n{reviewer_feedback}")
        if gate_instructions:
            lines.append(f"## Gate Criteria\n{gate_instructions}")
        lines.append("Address ALL issues listed above before resubmitting.")
        return "\n\n".join(lines)

    async def prepare_gate_rework(
        self,
        review_task: Task,
        gate: WorkItemGatePolicy,
        task_by_projection_id: dict[str, Task],
        reviewer_feedback: str,
    ) -> Task | None:
        rework_count = int(review_task.metadata.get("gate_rework_count", 0))
        rework_projection_id = rework_projection_id_for_gate(gate)
        if gate.on_reject != "rework" or not rework_projection_id or rework_count >= gate.max_retries:
            return None

        rework_task = task_by_projection_id.get(rework_projection_id)
        if rework_task is None:
            return None

        normalized_feedback = self._normalize_gate_feedback(
            reviewer_feedback,
            fallback=(
                f"{review_task.title} requested changes. "
                "Review the gate criteria, address the issues, and resubmit."
            ),
        )
        rework_round = rework_count + 1
        rework_request = self._build_gate_rework_record(
            review_task=review_task,
            gate=gate,
            reviewer_feedback=normalized_feedback,
            rework_round=rework_round,
        )

        review_task.metadata = dict(review_task.metadata)
        review_task.metadata["gate_rework_count"] = rework_round
        review_task.metadata["last_gate_review_feedback"] = normalized_feedback
        review_task.metadata["last_gate_review_feedback_full"] = str(reviewer_feedback or "").strip()
        review_task.metadata["last_gate_rework_request"] = dict(rework_request)
        review_task.result = None
        review_task.context_snapshot = dict(review_task.context_snapshot)
        review_task.context_snapshot["last_gate_rework_request"] = dict(rework_request)
        # Review task now carries last_gate_review_feedback and projects to
        # Phase.READY_FOR_REWORK through the canonical transition helper.
        await transition_work_item_from_task(
            self.store, review_task,
            target_status_or_phase=Phase.READY_FOR_REWORK,
            reason="prepare_gate_rework_review_task",
        )

        rework_task.result = None
        rework_task.metadata = dict(rework_task.metadata)
        rework_task.metadata["gate_review_feedback"] = normalized_feedback
        rework_task.metadata["gate_review_feedback_full"] = str(reviewer_feedback or "").strip()
        rework_task.metadata["gate_instructions"] = gate.instructions
        rework_task.metadata["gate_rework_round"] = rework_round
        rework_task.metadata["gate_rework_request"] = dict(rework_request)
        rework_task.context_snapshot = dict(rework_task.context_snapshot)
        rework_task.context_snapshot["latest_gate_rework"] = dict(rework_request)
        # Rework task carries gate_review_feedback → Phase.READY_FOR_REWORK
        # (same convention). Dispatched to the worker's queue as a rework
        # card with feedback.
        await transition_work_item_from_task(
            self.store, rework_task,
            target_status_or_phase=Phase.READY_FOR_REWORK,
            reason="prepare_gate_rework_rework_task",
        )
        await self._append_progress(rework_task, self._render_gate_rework_summary(rework_request))
        # Emit work-item progress events so the UI reverts the cards from
        # "done" (checkmark) back to "active" (dots) during rework.
        rework_projection_label = self._projection_id_for_task(rework_task) or rework_projection_id
        await self._emit_progress(
            f"[Company:{rework_projection_label}] reworking (gate rework round {rework_round})",
            task_id=rework_task.id,
        )
        review_projection_label = self._projection_id_for_task(review_task)
        await self._emit_progress(
            f"[Company:{review_projection_label}] reworking (gate rework round {rework_round})",
            task_id=review_task.id,
        )
        return rework_task

    async def _save_checkpoint(self, task: Task, gate: WorkItemGatePolicy) -> None:
        if not self.checkpoint_callback or not self._active_plan:
            return
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        prompt_override = str(dict(gate.metadata or {}).get("prompt_override", "") or "").strip()
        gate_metadata = dict(gate.metadata or {})
        gate_rework_projection_id = rework_projection_id_for_gate(gate)
        if gate_rework_projection_id:
            gate_metadata.update(
                gate_rework_payload(
                    rework_projection_id=gate_rework_projection_id,
                )
            )
        review_level = str(
            gate_metadata.get("review_level")
            or ("manager" if gate.reviewer_role and not gate.requires_human else "human")
        ).strip().lower() or "human"
        review_target_role_id = str(
            gate_metadata.get("review_target_role_id")
            or gate.reviewer_role
            or ""
        ).strip()
        review_chain_role_ids = [
            str(item).strip()
            for item in list(gate_metadata.get("review_chain_role_ids", []) or [])
            if str(item).strip()
        ]
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_work_item_gate",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "task_ids": [t.id for t in self._active_tasks],
                    "gate": {
                        "type": gate.gate_type,
                        "instructions": gate.instructions,
                        "reviewer_role": gate.reviewer_role,
                        "requires_human": gate.requires_human,
                        "on_reject": gate.on_reject,
                        "rework_projection_id": gate_rework_projection_id or None,
                        "max_retries": gate.max_retries,
                        "metadata": gate_metadata,
                    },
                    "prompt": prompt_override,
                    "review_level": review_level,
                    "review_target_role_id": review_target_role_id,
                    "review_chain_role_ids": review_chain_role_ids,
                    "basis_hash": self._checkpoint_basis_hash(task),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(self._active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )

    async def _save_feedback_checkpoint(self, task: Task) -> None:
        if not self.checkpoint_callback:
            return
        if not self._is_final_human_acceptance_task(task):
            logger.debug(
                "_save_feedback_checkpoint skipped for non-final work item "
                f"task_id={task.id} projection_id={self._projection_id_for_task(task)}"
            )
            return
        active_plan = self._active_plan or CompanyWorkItemRuntimePlan(
            profile=str(task.metadata.get("company_profile", "") or "company"),
            projections=[],
        )
        active_tasks = list(self._active_tasks) or [task]
        if self._active_plan is None:
            self._active_plan = active_plan
        if not self._active_tasks:
            self._active_tasks = list(active_tasks)
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        feedback_scope = str(task.metadata.get("feedback_scope", "") or "").strip()
        if not feedback_scope and self._is_authoritative_delivery_work_item(task):
            feedback_scope = "final"
        feedback_scope = feedback_scope or "final"
        feedback_kind = "final delivery" if feedback_scope == "final" else "work item"
        followup_message = str(task.metadata.get("feedback_followup_message", "") or "").strip()
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        else:
            result_content = ""
        linked_work_item_id = linked_work_item_id_for_task(task)
        delivery_revision = task.metadata.get("delivery_revision", "")
        owner_directive_revision = task.metadata.get("owner_directive_revision", "")
        if followup_message:
            prompt = (
                f"{followup_message}\n\n"
                f"The {feedback_kind} remains open for self-evolution review. Use this card only to "
                "record full agreement, ignore, or feedback that should update employee experience."
            )
        else:
            prompt = (
                f"This {feedback_kind} is ready for self-evolution review.\n"
                "Use this card only to record full agreement, ignore, or feedback that should update employee experience."
            )
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_delivery_feedback",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "waiting_work_item_id": linked_work_item_id,
                    "session_id": task.session_id,
                    "task_ids": [t.id for t in active_tasks],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "feedback_scope": feedback_scope,
                    "prompt": prompt,
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "delivery_revision": delivery_revision,
                    "owner_directive_revision": owner_directive_revision,
                    "latest_user_directive": str(task.metadata.get("latest_user_directive", "") or "").strip(),
                    "result_content": result_content,
                    "basis_hash": self._checkpoint_basis_hash(task),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )

    async def _save_peer_checkpoint(self, task: Task) -> None:
        if not self.checkpoint_callback or not self._active_plan:
            return
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_peer_wait",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    "task_ids": [t.id for t in self._active_tasks],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "peer_wait": dict(task.metadata.get("peer_wait", {})),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(self._active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )

    def _apply_role_defaults(self, task: Task, role: Any) -> None:
        if not task.metadata.get("handoff_template_ref") and getattr(role, "handoff_template_ref", None):
            task.metadata["handoff_template_ref"] = role.handoff_template_ref
        if not task.metadata.get("memory_policy_ref") and getattr(role, "memory_policy_ref", None):
            task.metadata["memory_policy_ref"] = role.memory_policy_ref
        if not task.metadata.get("artifact_contract_ref") and getattr(role, "artifact_contract_ref", None):
            task.metadata["artifact_contract_ref"] = role.artifact_contract_ref

    def _downstream_assignment_for_projection(self, dep_task: Task, next_task: Task) -> dict[str, Any] | None:
        target_projection_id = self._projection_id_for_task(next_task)
        dep_output_metadata = self._work_item_output_metadata_for_task(dep_task)
        for item in list(dep_output_metadata.get("downstream_assignments", []) or dep_task.metadata.get("downstream_assignments", []) or []):
            if not isinstance(item, dict):
                continue
            item_projection_id = str(
                item.get("work_item_projection_id")
                or item.get("target_projection_id")
                or item.get("projection_id")
                or ""
            ).strip()
            if item_projection_id != target_projection_id:
                continue
            return dict(item)
        return None

    def _capture_work_item_outputs(self, task: Task, result: TaskResult) -> WorkItemOutputBundle:
        summary = (result.content or "").strip()
        runtime_state = self._extract_runtime_state(result)
        structured_payload = self._extract_structured_work_item_payload(summary, result.artifacts)
        existing_artifacts = list(task.metadata.get("artifacts", []) or [])
        task.metadata = dict(task.metadata)
        for key in (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "follow_up_actions",
            "downstream_assignments",
        ):
            task.metadata.pop(key, None)
        work_item_updates: dict[str, Any] = {}
        runtime_audit_updates: dict[str, Any] = {}
        if runtime_state:
            runtime_audit_updates["runtime_v2"] = runtime_state
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["runtime_v2"] = runtime_state
        if summary:
            work_item_updates["work_item_summary"] = summary
            work_item_updates["work_item_summary_for_downstream"] = summary
        artifacts = self._merge_unique_items(
            existing_artifacts,
            self._collect_artifact_refs(result.artifacts),
        )
        if artifacts:
            task.metadata["artifacts"] = artifacts
        if structured_payload.get("runtime_plan"):
            task.metadata["work_item_runtime_plan"] = structured_payload["runtime_plan"]
        artifact_index = self._normalize_work_item_artifact_index(structured_payload.get("artifact_index"))
        if not artifact_index:
            artifact_index = self._build_work_item_artifact_index(
                result.artifacts,
                list(task.metadata.get("artifacts", [])),
            )
        if artifact_index:
            work_item_updates["work_item_artifact_index"] = artifact_index
        delivery_package = self._normalize_delivery_package(structured_payload.get("delivery_package"))
        if delivery_package:
            work_item_updates["delivery_package"] = delivery_package
        follow_up_actions = self._normalize_follow_up_actions(
            structured_payload.get("follow_up_actions")
            or (result.artifacts.get("follow_up_actions") if result.artifacts else [])
        )
        if follow_up_actions:
            work_item_updates["follow_up_actions"] = follow_up_actions
        downstream_assignments = self._normalize_downstream_assignments(
            task,
            result.artifacts.get("downstream_assignments", []) if result.artifacts else [],
        )
        if downstream_assignments:
            work_item_updates["downstream_assignments"] = downstream_assignments
        review_verdict = self._normalize_review_verdict(structured_payload.get("review_verdict"))
        if review_verdict:
            work_item_updates["structured_review_verdict"] = review_verdict
        verification = result.artifacts.get("verification", []) if result.artifacts else []
        verification_evidence = dict(result.artifacts.get("verification_evidence", {}) if result.artifacts else {})
        if verification_evidence:
            work_item_updates["verification_evidence"] = verification_evidence
            runtime_audit_updates["runtime_verification_evidence"] = verification_evidence
        if verification:
            work_item_updates["verification"] = verification
            runtime_audit_updates["runtime_verification"] = verification
            verification_notes = [
                f"verification {item.get('verifier', '')}: {item.get('status', '')} - {item.get('summary', '')}".strip()
                for item in verification
                if isinstance(item, dict)
            ]
            risks = self._merge_unique_items(
                list(work_item_updates.get("risks", [])),
                [note for note in verification_notes if "failed" in note or "inconclusive" in note],
            )
            if risks:
                work_item_updates["risks"] = risks
        task.metadata["acceptance_criteria"] = list(task.metadata.get("acceptance_criteria", []))
        verification_status = self._build_verification_status(task, result, review_verdict=review_verdict)
        if verification_status:
            work_item_updates["verification_status"] = verification_status
            runtime_audit_updates["runtime_verification_status"] = verification_status
        if result.artifacts:
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["latest_artifacts"] = dict(result.artifacts)
        if work_item_updates:
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["work_item_owned_outputs"] = copy.deepcopy(work_item_updates)
        update_runtime_task_owned_metadata(task, runtime_audit_updates)
        if linked_work_item_id_for_task(task):
            strip_disallowed_work_item_metadata_from_runtime_task(task)
        else:
            task.metadata.update(copy.deepcopy(work_item_updates))

        turn_type = self._turn_type_for_task(task)
        projection_id = self._projection_id_for_task(task)
        if turn_type == "setup":
            self._capture_environment_manifest(task, result)
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            self._capture_workspace_manifest(task, result)
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            self._capture_data_acquisition_log(task, result)
            self._capture_data_acquisition_report(task, result)
            self._synthesize_data_acquisition_execution_record(task)
        return WorkItemOutputBundle(
            work_item_updates=work_item_updates,
            runtime_audit_updates=runtime_audit_updates,
            summary=summary,
        )

    async def _persist_work_item_owned_output_metadata(
        self,
        task: Task,
        bundle: WorkItemOutputBundle | None = None,
    ) -> None:
        """Persist business output metadata to the linked WorkItem.

        Task keeps result/runtime audit for execution replay, but the
        collaboration/board summary belongs to DelegationWorkItem.
        """
        if not self.store:
            return
        wid = linked_work_item_id_for_task(task)
        if not wid:
            return
        source = dict(bundle.work_item_updates if bundle is not None else task.metadata or {})
        business_keys = (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "follow_up_actions",
            "downstream_assignments",
            "open_questions",
            "assumptions",
            "decisions",
            "risks",
            "completion_report",
            "handoff_context",
            "context_preview",
        )
        updates = {
            key: copy.deepcopy(source.get(key))
            for key in business_keys
            if source.get(key) not in (None, "", [], {})
        }
        try:
            summary = str(source.get("work_item_summary") or source.get("work_item_summary_for_downstream") or "").strip()
            if summary:
                updates.setdefault("deliverable_summary", summary)
            await update_work_item_owned_metadata(self.store, wid, updates)
            if summary and hasattr(self.store, "update_delegation_work_item"):
                await self.store.update_delegation_work_item(
                    wid,
                    deliverable_summary=summary,
                )
        except Exception:
            logger.opt(exception=True).debug(
                "WorkItem-owned output metadata write failed task=%s", task.id
            )

    async def _materialize_follow_up_work_items(
        self,
        task: Task,
        result: TaskResult,
    ) -> list[str]:
        if not self.store or not is_work_item_runtime_metadata(task.metadata):
            return []
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not parent_work_item_id:
            return []
        output_metadata = self._work_item_output_metadata_for_task(task)
        actions = self._normalize_follow_up_actions(
            list(output_metadata.get("follow_up_actions", []) or task.metadata.get("follow_up_actions", []) or [])
            or (result.artifacts.get("follow_up_actions", []) if result.artifacts else [])
        )
        if not actions:
            return []
        parent_work_item = await self.store.get_delegation_work_item(parent_work_item_id)
        if parent_work_item is None:
            return []
        root_task = sorted(self._active_tasks, key=lambda item: (item.created_at, item.id))[0] if self._active_tasks else task
        runtime_topology = dict((root_task.metadata or {}).get("runtime_topology", {}) or {})
        seats = [dict(item) for item in list(runtime_topology.get("seats", []) or []) if isinstance(item, dict)]
        run_id = str(parent_work_item.run_id or task.metadata.get("delegation_run_id", "") or "").strip()
        if not run_id:
            return []
        existing_work_items = await self.store.list_delegation_work_items(run_id)
        follow_up_dependency_ids: list[str] = []
        created_work_item_ids: list[str] = []
        parent_metadata = dict(parent_work_item.metadata or {})
        parent_dependency_ids = [
            str(item).strip()
            for item in list(parent_metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        for action in actions:
            target_role_id = str(action.get("target_role_id", "") or "").strip()
            topology_seat = next(
                (
                    seat
                    for seat in seats
                    if str(seat.get("role_id", "") or "").strip() == target_role_id
                ),
                {},
            )
            seat_id = str(topology_seat.get("seat_id", "") or "").strip()
            if not seat_id:
                continue
            dedupe_key = str(action.get("dedupe_key", "") or "").strip() or (
                f"{str(task.metadata.get('delegation_seat_id', '') or '').strip()}::{target_role_id}::{action['action']}::{str(action.get('title', '') or '').strip()}"
            )
            duplicate = next(
                (
                    item
                    for item in existing_work_items
                    if str(item.manager_seat_id or "").strip() == str(task.metadata.get("delegation_seat_id", "") or "").strip()
                    and str((item.metadata or {}).get("follow_up_dedupe_key", "") or "").strip() == dedupe_key
                    and item.phase not in DONE_PHASES
                ),
                None,
            )
            if duplicate is not None:
                follow_up_dependency_ids.append(str(duplicate.work_item_id))
                continue
            dependency_work_item_ids = [
                str(dep).strip()
                for dep in list(action.get("depends_on_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            work_kind = "review" if action["action"] == "delegate_rereview" else "execute"
            turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            follow_up_projection_id = f"followup::{target_role_id}::{uuid.uuid4().hex[:8]}"
            follow_up_work_item = DelegationWorkItem(
                run_id=run_id,
                cell_id=str(topology_seat.get("team_id", "") or target_role_id).strip(),
                team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                team_id=str(topology_seat.get("team_id", "") or "").strip(),
                role_id=target_role_id,
                seat_id=seat_id,
                seat_state_id=str(topology_seat.get("seat_state_id", "") or f"seat-state::{run_id}::{seat_id}").strip(),
                # Fix 2: canonical fallback when topology lacks the ID.
                role_runtime_session_id=(
                    str(topology_seat.get("role_runtime_session_id", "") or "").strip()
                    or canonical_role_session_id(
                        run_id=run_id,
                        role_id=target_role_id,
                        team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                    )
                ),
                parent_work_item_id=parent_work_item_id,
                source_role_id=self._role_id_for_task(task) or None,
                source_seat_id=str(task.metadata.get("delegation_seat_id", "") or "").strip() or None,
                title=str(action.get("title", "") or action["action"].replace("_", " ").title()).strip(),
                summary=str(action.get("summary", "") or action.get("reason", "") or result.content or "").strip(),
                kind=work_kind,
                projection_id=follow_up_projection_id,
                phase=Phase.WAITING_DEPENDENCIES if dependency_work_item_ids else Phase.READY,
                batch_id=str(parent_work_item.batch_id or f"batch::{run_id}::followup").strip(),
                batch_index=int(parent_work_item.batch_index or 0) + 1,
                continuation_source=str(parent_work_item.work_item_id or "").strip(),
                manager_role_id=self._role_id_for_task(task),
                manager_seat_id=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                metadata=mark_work_item_projection(mark_work_item_runtime({
                    "runtime_model": str(task.metadata.get("runtime_model", "") or "multi_team_org").strip(),
                    "session_scope_id": task_session_scope_id(task),
                    "delegation_turn_kind": work_kind,
                    "team_id": str(topology_seat.get("team_id", "") or "").strip(),
                    "seat_id": seat_id,
                    "seat_state_id": str(topology_seat.get("seat_state_id", "") or f"seat-state::{run_id}::{seat_id}").strip(),
                    "batch_id": str(parent_work_item.batch_id or f"batch::{run_id}::followup").strip(),
                    "work_kind": work_kind,
                    "manager_role_id": self._role_id_for_task(task),
                    "dependency_work_item_ids": dependency_work_item_ids,
                    "scope_key": str(action.get("scope_key", "") or dedupe_key).strip(),
                    # Fix 2: canonical fallback (same resolution as above).
                    "assigned_role_runtime_id": (
                        str(topology_seat.get("role_runtime_session_id", "") or "").strip()
                        or canonical_role_session_id(
                            run_id=run_id,
                            role_id=target_role_id,
                            team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                        )
                    ),
                    "contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                    "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                    "delegation_playbook": dict(task.metadata.get("delegation_playbook", {}) or {}),
                    "comms_workspace_root": str(task.metadata.get("comms_workspace_root", "") or "").strip(),
                    "target_output_dir": str(task.metadata.get("target_output_dir", "") or "").strip(),
                    "user_visible": False,
                    "authoritative_output": False,
                    "follow_up_dedupe_key": dedupe_key,
                    "follow_up_action": action["action"],
                    "follow_up_reason": str(action.get("reason", "") or "").strip(),
                    "created_from_task_id": task.id,
                    "created_from_work_item_id": parent_work_item_id,
                }, version=work_item_runtime_version(task.metadata)),
                    projection_id=follow_up_projection_id,
                    turn_type=turn_type,
                ),
            )
            await self.store.save_delegation_work_item(follow_up_work_item)
            existing_work_items.append(follow_up_work_item)
            follow_up_dependency_ids.append(follow_up_work_item.work_item_id)
            created_work_item_ids.append(follow_up_work_item.work_item_id)
            if hasattr(self.store, "save_delegation_event"):
                try:
                    await self.store.save_delegation_event(
                        DelegationEvent(
                            run_id=run_id,
                            work_item_id=follow_up_work_item.work_item_id,
                            cell_id=follow_up_work_item.cell_id,
                            role_id=follow_up_work_item.role_id,
                            event_type="follow_up_work_item_created",
                            payload={
                                "parent_work_item_id": parent_work_item_id,
                                "action": action["action"],
                                "dedupe_key": dedupe_key,
                                "target_role_id": target_role_id,
                            },
                        )
                    )
                except Exception:
                    logger.debug("Best-effort follow-up delegation event persistence failed")
        if not follow_up_dependency_ids:
            return []
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in existing_work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        merged_dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
            list(dict.fromkeys([*parent_dependency_ids, *follow_up_dependency_ids])),
            work_item_by_id,
            owner_work_item_id=parent_work_item_id,
        )
        parent_work_item.metadata = {
            **parent_metadata,
            "dependency_work_item_ids": merged_dependency_ids,
            "follow_up_actions": copy.deepcopy(actions),
        }
        if pruned_dependency_ids:
            parent_work_item.metadata["pruned_dependency_work_item_ids"] = list(
                dict.fromkeys(
                    [
                        *list(parent_metadata.get("pruned_dependency_work_item_ids", []) or []),
                        *pruned_dependency_ids,
                    ]
                )
            )
            parent_work_item.metadata["dependency_pruned_at"] = datetime.now().isoformat()
        await self.store.save_delegation_work_item(parent_work_item)
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if callable(supersede):
            await supersede(
                project_id=task.project_id or "default",
                task_id=task.id,
                checkpoint_types=["company_work_item_gate", "company_delivery_feedback"],
            )
        task.metadata = dict(task.metadata)
        task.metadata["delegation_wait_for_work_item_ids"] = merged_dependency_ids
        if linked_work_item_id_for_task(task):
            self._set_work_item_output_context(task, {"follow_up_actions": actions})
            task.metadata.pop("follow_up_actions", None)
        else:
            task.metadata["follow_up_actions"] = actions
        try:
            frontier_changed = await refresh_dependents_for_run(
                self.store,
                run_id=run_id,
                source_work_item_id=parent_work_item_id,
                source_task_id=task.id,
                source_role_id=self._role_id_for_task(task),
                source_cell_id=str(getattr(parent_work_item, "cell_id", "") or "").strip() or None,
            )
            if frontier_changed:
                self._signal_dispatcher_wake()
                await self._notify_kanban_changed()
        except Exception:
            logger.opt(exception=True).debug("Best-effort follow-up dependency frontier refresh failed")
        return created_work_item_ids

    def _capture_environment_manifest(self, task: Task, result: TaskResult) -> None:
        """Extract environment manifest from a setup work item's output."""
        manifest_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            manifest_data = dict(result.artifacts.get("environment_manifest", {}) or {})
        if not manifest_data:
            summary = str(result.content or "").strip()
            manifest_data = self._parse_env_manifest_from_text(summary)
        if not manifest_data:
            return
        import sys as _sys
        detected_platform = "windows" if _sys.platform.startswith("win") else ("macos" if _sys.platform == "darwin" else "linux")
        manifest = EnvironmentManifest(
            platform=str(manifest_data.get("platform", "") or detected_platform),
            tools_installed=list(manifest_data.get("tools_installed", []) or []),
            env_vars=dict(manifest_data.get("env_vars", {}) or {}),
            runtime_type=str(manifest_data.get("runtime_type", "native") or "native"),
            runtime_path=str(manifest_data.get("runtime_path", "") or ""),
            activate_command=str(manifest_data.get("activate_command", "") or ""),
            shell_prefix=str(manifest_data.get("shell_prefix", "") or ""),
            shell_prefix_win=str(manifest_data.get("shell_prefix_win", "") or ""),
            gpu_available=bool(manifest_data.get("gpu_available", False)),
            gpu_info=str(manifest_data.get("gpu_info", "") or ""),
            verification_checks=list(manifest_data.get("verification_checks", []) or []),
            verification_checks_win=list(manifest_data.get("verification_checks_win", []) or []),
            notes=str(manifest_data.get("notes", "") or ""),
        )
        task.metadata["environment_manifest"] = manifest.__dict__

    def _capture_workspace_manifest(self, task: Task, result: TaskResult) -> None:
        manifest_data = dict(task.metadata.get("workspace_manifest", {}) or {})
        if result.artifacts and isinstance(result.artifacts, dict):
            manifest_data = {**manifest_data, **dict(result.artifacts.get("workspace_manifest", {}) or {})}
        if not manifest_data:
            manifest_data = self._parse_workspace_manifest_from_text(str(result.content or "").strip())
        if not manifest_data:
            return
        reserved_paths = {
            str(key).strip(): str(value).strip()
            for key, value in dict(manifest_data.get("reserved_paths", {}) or {}).items()
            if str(key).strip() and str(value).strip()
        }
        notes = [str(item).strip() for item in list(manifest_data.get("notes", []) or []) if str(item).strip()]
        manifest = WorkspaceManifest(
            root_path=str(manifest_data.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip(),
            manifest_path=str(manifest_data.get("manifest_path", "") or "").strip(),
            reserved_paths=reserved_paths,
            status=str(manifest_data.get("status", "ready") or "ready").strip(),
            notes=notes,
        )
        task.metadata["workspace_manifest"] = manifest.__dict__

    def _capture_data_acquisition_log(self, task: Task, result: TaskResult) -> None:
        log_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            log_data = dict(result.artifacts.get("data_acquisition_log", {}) or {})
        if not log_data:
            log_data = self._load_data_acquisition_artifact_file(task, artifact_kind="log")
        if not log_data:
            return
        attempted_sources = self._normalize_data_acquisition_items(log_data.get("attempted_sources", []))
        attempted_tools = self._normalize_data_acquisition_items(log_data.get("attempted_tools", []))
        prepared_assets = self._normalize_data_acquisition_items(log_data.get("prepared_assets", []))
        blocked_reasons = self._normalize_data_acquisition_items(log_data.get("blocked_reasons", []))
        notes = self._normalize_data_acquisition_items(log_data.get("notes", []))
        acquisition_attempted = self._infer_data_acquisition_attempted(
            log_data,
            attempted_sources=attempted_sources,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
        )
        normalized = dict(log_data)
        normalized["attempted_sources"] = attempted_sources
        normalized["attempted_tools"] = attempted_tools
        normalized["prepared_assets"] = prepared_assets
        normalized["blocked_reasons"] = blocked_reasons
        normalized["notes"] = notes
        normalized["acquisition_attempted"] = acquisition_attempted
        normalized["log_path"] = self._data_acquisition_standard_path(task, artifact_kind="log")
        normalized["source_candidates_path"] = str(
            log_data.get("source_candidates_path", "") or default_source_candidates_path(task)
        ).strip()
        normalized["download_manifest_path"] = str(
            log_data.get("download_manifest_path", "") or default_download_manifest_path(task)
        ).strip()
        task.metadata["data_acquisition_log"] = normalized

    def _capture_data_acquisition_report(self, task: Task, result: TaskResult) -> None:
        report_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            report_data = dict(result.artifacts.get("data_acquisition_report", {}) or {})
        if not report_data:
            report_data = self._parse_data_acquisition_report_from_text(str(result.content or "").strip())
        if not report_data:
            report_data = self._load_data_acquisition_artifact_file(task, artifact_kind="report")
        if not report_data:
            return
        log_data = dict(task.metadata.get("data_acquisition_log", {}) or {})
        designated_input_dir = str(report_data.get("designated_input_dir", "") or "").strip()
        if not designated_input_dir:
            designated_input_dir = str(
                dict(task.metadata.get("workspace_manifest", {}) or {}).get("reserved_paths", {}).get("inputs", "")
                or ""
            ).strip()
        required_inputs = self._normalize_data_acquisition_items(report_data.get("required_inputs", []))
        present_inputs = self._normalize_data_acquisition_items(report_data.get("present_inputs", []))
        missing_inputs = self._normalize_data_acquisition_items(report_data.get("missing_inputs", []))
        attempted_sources = self._normalize_data_acquisition_items(
            report_data.get("attempted_sources", log_data.get("attempted_sources", []))
        )
        attempted_tools = self._merge_unique_items(
            self._normalize_data_acquisition_items(report_data.get("attempted_tools", [])),
            self._normalize_data_acquisition_items(log_data.get("attempted_tools", [])),
        )
        prepared_assets = self._normalize_data_acquisition_items(
            report_data.get("prepared_assets", log_data.get("prepared_assets", []))
        )
        blocked_reasons = self._normalize_data_acquisition_items(
            report_data.get("blocked_reasons", log_data.get("blocked_reasons", []))
        )
        notes = self._merge_unique_items(
            self._normalize_data_acquisition_items(report_data.get("notes", [])),
            self._normalize_data_acquisition_items(log_data.get("notes", [])),
        )
        acquisition_attempted = self._infer_data_acquisition_attempted(
            report_data,
            log_data=log_data,
            attempted_sources=attempted_sources,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
        )
        report = DataAcquisitionReport(
            status=str(report_data.get("status", "missing_critical") or "missing_critical").strip(),
            designated_input_dir=designated_input_dir,
            required_inputs=required_inputs,
            present_inputs=present_inputs,
            missing_inputs=missing_inputs,
            attempted_sources=attempted_sources,
            attempted_tools=attempted_tools,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
            acquisition_attempted=acquisition_attempted,
            report_path=self._data_acquisition_standard_path(task, artifact_kind="report"),
            log_path=str(log_data.get("log_path", "") or self._data_acquisition_standard_path(task, artifact_kind="log")).strip(),
            source_candidates_path=str(
                report_data.get("source_candidates_path", "")
                or log_data.get("source_candidates_path", "")
                or default_source_candidates_path(task)
            ).strip(),
            download_manifest_path=str(
                report_data.get("download_manifest_path", "")
                or log_data.get("download_manifest_path", "")
                or default_download_manifest_path(task)
            ).strip(),
            provenance_summary=self._normalize_data_acquisition_summary(report_data.get("provenance_summary", "")),
            notes=notes,
        )
        task.metadata["data_acquisition_report"] = report.__dict__

    def _synthesize_data_acquisition_execution_record(self, task: Task) -> None:
        report = dict(task.metadata.get("data_acquisition_report", {}) or {})
        log_data = dict(task.metadata.get("data_acquisition_log", {}) or {})
        if not report and not log_data:
            return
        output_path = Path(default_execution_record_path(task))
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        workspace_root = str(
            dict(task.metadata.get("workspace_manifest", {}) or {}).get("root_path", "")
            or task.metadata.get("target_output_dir", "")
            or ""
        ).strip()
        attempted_tools = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("attempted_tools", [])),
            self._normalize_data_acquisition_items(report.get("attempted_tools", [])),
        )
        attempted_sources = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("attempted_sources", [])),
            self._normalize_data_acquisition_items(report.get("attempted_sources", [])),
        )
        prepared_assets = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("prepared_assets", [])),
            self._normalize_data_acquisition_items(report.get("prepared_assets", [])),
        )
        blocked_reasons = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("blocked_reasons", [])),
            self._normalize_data_acquisition_items(report.get("blocked_reasons", [])),
        )
        lines = [
            "# Data Acquisition Execution Record",
            "",
            "## Scope",
            "Record the discovered sources, prepared assets, download attempts, and final readiness outcome for this data acquisition run.",
            "",
            "## Workspace",
            f"`{workspace_root}`" if workspace_root else "(unknown)",
            "",
            "## Execution Sequence",
            "1. Discover candidate sources.",
            "2. Verify candidate provenance.",
            "3. Prepare files or manifests inside the workspace.",
            "4. Publish readiness artifacts.",
            "",
            "## Attempted Tools",
        ]
        if attempted_tools:
            lines.extend(f"- `{item}`" for item in attempted_tools)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Structured Artifacts",
            f"- Source candidates: `{str(report.get('source_candidates_path', '') or log_data.get('source_candidates_path', '') or default_source_candidates_path(task)).strip()}`",
            f"- Download manifest: `{str(report.get('download_manifest_path', '') or log_data.get('download_manifest_path', '') or default_download_manifest_path(task)).strip()}`",
            f"- Readiness report: `{str(report.get('report_path', '') or self._data_acquisition_standard_path(task, artifact_kind='report')).strip()}`",
            f"- Acquisition log: `{str(report.get('log_path', '') or log_data.get('log_path', '') or self._data_acquisition_standard_path(task, artifact_kind='log')).strip()}`",
            "",
            "## Attempted Sources",
        ])
        if attempted_sources:
            lines.extend(f"- {item}" for item in attempted_sources)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Prepared Assets",
        ])
        if prepared_assets:
            lines.extend(f"- {item}" for item in prepared_assets)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Final Self-Audit",
            f"- Status: `{str(report.get('status', '') or 'missing_critical').strip()}`",
            f"- Acquisition attempted: `{bool(report.get('acquisition_attempted', False) or log_data.get('acquisition_attempted', False))}`",
        ])
        provenance_summary = str(report.get("provenance_summary", "") or "").strip()
        if provenance_summary:
            lines.append(f"- Provenance summary: {provenance_summary}")
        if blocked_reasons:
            lines.append("- Blockers:")
            lines.extend(f"  - {item}" for item in blocked_reasons[:10])
        try:
            output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        except OSError:
            return
        artifacts = list(task.metadata.get("artifacts", []) or [])
        execution_record = str(output_path.resolve())
        if execution_record not in artifacts:
            task.metadata["artifacts"] = [*artifacts, execution_record]

    @staticmethod
    def _data_acquisition_candidate_paths(text: str) -> list[str]:
        candidates: list[str] = []
        stripped = text.strip()
        if not stripped:
            return candidates
        candidates.append(stripped)
        if stripped.startswith("```"):
            segments = stripped.split("```")
            for segment in segments:
                candidate = segment.strip()
                if not candidate:
                    continue
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("{") or candidate.startswith("["):
                    candidates.append(candidate)
        return candidates

    def _data_acquisition_standard_path(self, task: Task, *, artifact_kind: str) -> str:
        work_item_gate = dict(task.metadata.get("work_item_gate", {}) or {})
        gate_metadata = dict(work_item_gate.get("metadata", {}) or {})
        key = "standard_log_path" if artifact_kind == "log" else "standard_report_path"
        default_value = (
            _DEFAULT_DATA_ACQUISITION_LOG_PATH
            if artifact_kind == "log"
            else _DEFAULT_DATA_ACQUISITION_REPORT_PATH
        )
        relative_path = str(gate_metadata.get(key, "") or default_value).strip()
        if not relative_path:
            return ""
        if Path(relative_path).is_absolute():
            return relative_path
        workspace_manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
        reserved_paths = dict(workspace_manifest.get("reserved_paths", {}) or {})
        if relative_path.startswith("deliverables/") and reserved_paths.get("deliverables"):
            suffix = Path(relative_path).parts[1:]
            return str(Path(str(reserved_paths["deliverables"])).joinpath(*suffix))
        root_path = str(workspace_manifest.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip()
        if root_path:
            return str(Path(root_path) / relative_path)
        return relative_path

    def _load_data_acquisition_artifact_file(self, task: Task, *, artifact_kind: str) -> dict[str, Any]:
        artifact_path = self._data_acquisition_standard_path(task, artifact_kind=artifact_kind)
        if not artifact_path:
            return {}
        path = Path(artifact_path)
        if not path.is_file():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        if artifact_kind == "report" and isinstance(parsed.get("data_acquisition_report"), dict):
            return dict(parsed.get("data_acquisition_report", {}) or {})
        if artifact_kind == "log" and isinstance(parsed.get("data_acquisition_log"), dict):
            return dict(parsed.get("data_acquisition_log", {}) or {})
        return parsed

    @staticmethod
    def _stringify_data_acquisition_item(value: Any) -> str:
        if isinstance(value, dict):
            label = str(
                value.get("name", "")
                or value.get("path", "")
                or value.get("source", "")
                or value.get("url", "")
                or value.get("title", "")
                or value.get("id", "")
                or ""
            ).strip()
            if not label:
                try:
                    label = json.dumps(value, ensure_ascii=False, sort_keys=True)
                except TypeError:
                    label = str(value).strip()
            qualifiers: list[str] = []
            status = str(value.get("status", "") or "").strip()
            if status:
                qualifiers.append(status)
            if bool(value.get("critical", False)):
                qualifiers.append("critical")
            return f"{label} ({', '.join(qualifiers)})" if qualifiers else label
        if isinstance(value, (list, tuple, set)):
            joined = ", ".join(
                text
                for text in (
                    CompanyWorkItemExecutor._stringify_data_acquisition_item(item)
                    for item in value
                )
                if text
            )
            return joined.strip()
        return str(value).strip()

    def _normalize_data_acquisition_items(self, value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        elif value in (None, "", [], {}):
            raw_items = []
        else:
            raw_items = [value]
        normalized: list[str] = []
        for item in raw_items:
            text = self._stringify_data_acquisition_item(item)
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    def _normalize_data_acquisition_summary(self, value: Any) -> str:
        if isinstance(value, dict):
            parts = []
            for key, item in value.items():
                key_text = str(key).strip()
                item_text = self._stringify_data_acquisition_item(item)
                if key_text and item_text:
                    parts.append(f"{key_text}: {item_text}")
            return "; ".join(parts).strip()
        if isinstance(value, list):
            return "; ".join(self._normalize_data_acquisition_items(value)).strip()
        return str(value or "").strip()

    def _infer_data_acquisition_attempted(
        self,
        report_data: dict[str, Any],
        *,
        log_data: dict[str, Any] | None = None,
        attempted_sources: list[str] | None = None,
        prepared_assets: list[str] | None = None,
        blocked_reasons: list[str] | None = None,
    ) -> bool:
        explicit = report_data.get("acquisition_attempted")
        if isinstance(explicit, bool):
            return explicit
        log_payload = dict(log_data or {})
        if isinstance(log_payload.get("acquisition_attempted"), bool):
            return bool(log_payload.get("acquisition_attempted"))
        attempted_items = list(attempted_sources or self._normalize_data_acquisition_items(report_data.get("attempted_sources", [])))
        if not attempted_items:
            attempted_items = self._normalize_data_acquisition_items(log_payload.get("attempted_sources", []))
        prepared_items = list(prepared_assets or self._normalize_data_acquisition_items(report_data.get("prepared_assets", [])))
        if not prepared_items:
            prepared_items = self._normalize_data_acquisition_items(log_payload.get("prepared_assets", []))
        blocked_items = list(blocked_reasons or self._normalize_data_acquisition_items(report_data.get("blocked_reasons", [])))
        if not blocked_items:
            blocked_items = self._normalize_data_acquisition_items(log_payload.get("blocked_reasons", []))
        acquisition_actions = self._normalize_data_acquisition_items(report_data.get("acquisition_actions", []))
        acquisition_actions = self._merge_unique_items(
            acquisition_actions,
            self._normalize_data_acquisition_items(log_payload.get("acquisition_actions", [])),
        )
        attempted_tools = self._normalize_data_acquisition_items(report_data.get("attempted_tools", []))
        attempted_tools = self._merge_unique_items(
            attempted_tools,
            self._normalize_data_acquisition_items(log_payload.get("attempted_tools", [])),
        )
        return bool(attempted_items or prepared_items or blocked_items or acquisition_actions or attempted_tools)

    @staticmethod
    def _parse_env_manifest_from_text(text: str) -> dict[str, Any]:
        """Best-effort extraction of env manifest from free-form text."""
        import json as _json
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and "environment_manifest" in stripped:
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict) and "environment_manifest" in parsed:
                        return dict(parsed["environment_manifest"])
                except _json.JSONDecodeError:
                    pass
            if stripped.startswith("{") and "tools_installed" in stripped:
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        return parsed
                except _json.JSONDecodeError:
                    pass
        return {}

    @staticmethod
    def _parse_workspace_manifest_from_text(text: str) -> dict[str, Any]:
        import json as _json
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "workspace_manifest" in parsed and isinstance(parsed["workspace_manifest"], dict):
                return dict(parsed["workspace_manifest"])
            if isinstance(parsed, dict) and "reserved_paths" in parsed:
                return parsed
        return {}

    @staticmethod
    def _parse_data_acquisition_report_from_text(text: str) -> dict[str, Any]:
        import json as _json
        for candidate in CompanyWorkItemExecutor._data_acquisition_candidate_paths(text):
            try:
                parsed = _json.loads(candidate)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "data_acquisition_report" in parsed and isinstance(parsed["data_acquisition_report"], dict):
                return dict(parsed["data_acquisition_report"])
            if isinstance(parsed, dict) and str(parsed.get("status", "")).strip():
                return parsed
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "data_acquisition_report" in parsed and isinstance(parsed["data_acquisition_report"], dict):
                return dict(parsed["data_acquisition_report"])
            if isinstance(parsed, dict) and str(parsed.get("status", "")).strip():
                return parsed
        return {}

    def _inherit_environment_manifest(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Propagate environment manifests from upstream setup work items."""
        if task.metadata.get("environment_manifest"):
            return
        merged_env_vars: dict[str, str] = {}
        merged_tools: list[dict[str, Any]] = []
        shell_prefix_parts: list[str] = []
        shell_prefix_win_parts: list[str] = []
        has_manifest = False

        def _collect_from_manifest(manifest: dict[str, Any]) -> None:
            nonlocal has_manifest
            if not manifest:
                return
            has_manifest = True
            merged_env_vars.update(dict(manifest.get("env_vars", {}) or {}))
            merged_tools.extend(list(manifest.get("tools_installed", []) or []))
            prefix = str(manifest.get("shell_prefix", "") or "").strip()
            if prefix and prefix not in shell_prefix_parts:
                shell_prefix_parts.append(prefix)
            prefix_win = str(manifest.get("shell_prefix_win", "") or "").strip()
            if prefix_win and prefix_win not in shell_prefix_win_parts:
                shell_prefix_win_parts.append(prefix_win)

        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task:
                _collect_from_manifest(dict(dep_task.metadata.get("environment_manifest", {}) or {}))
        if not has_manifest:
            for dependency_projection_id in task.dependencies:
                dep_task = task_by_projection_id.get(dependency_projection_id)
                if not dep_task:
                    continue
                for grand_dep_id in dep_task.dependencies:
                    grand_dep = task_by_projection_id.get(grand_dep_id)
                    if grand_dep:
                        _collect_from_manifest(dict(grand_dep.metadata.get("environment_manifest", {}) or {}))
        if has_manifest:
            task.metadata["inherited_environment"] = {
                "env_vars": merged_env_vars,
                "tools_available": merged_tools,
                "shell_prefix": " && ".join(shell_prefix_parts) if shell_prefix_parts else "",
                "shell_prefix_win": " ; ".join(shell_prefix_win_parts) if shell_prefix_win_parts else "",
            }

    def _inherit_workspace_manifest(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        if task.metadata.get("workspace_manifest"):
            return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task and dep_task.metadata.get("workspace_manifest"):
                task.metadata["workspace_manifest"] = dict(dep_task.metadata.get("workspace_manifest", {}) or {})
                return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            for grand_dep_id in dep_task.dependencies:
                grand_dep = task_by_projection_id.get(grand_dep_id)
                if grand_dep and grand_dep.metadata.get("workspace_manifest"):
                    task.metadata["workspace_manifest"] = dict(grand_dep.metadata.get("workspace_manifest", {}) or {})
                    return

    def _inherit_data_acquisition_report(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        if task.metadata.get("data_acquisition_report"):
            return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task and dep_task.metadata.get("data_acquisition_report"):
                task.metadata["data_acquisition_report"] = dict(dep_task.metadata.get("data_acquisition_report", {}) or {})
                return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            for grand_dep_id in dep_task.dependencies:
                grand_dep = task_by_projection_id.get(grand_dep_id)
                if grand_dep and grand_dep.metadata.get("data_acquisition_report"):
                    task.metadata["data_acquisition_report"] = dict(grand_dep.metadata.get("data_acquisition_report", {}) or {})
                    return

    def _normalize_downstream_assignments(
        self,
        task: Task,
        value: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        source_projection_id = self._projection_id_for_task(task)
        plan = self._plan_view_for_task(task)
        if plan is None:
            return []
        allowed_projection_ids = set(plan.dependent_projection_ids(source_projection_id))
        if not allowed_projection_ids:
            return []

        helper = self.work_item_helper
        projection_lookup = plan.projection_by_id()
        global_intent_summary = str(task.metadata.get("global_intent_summary", "") or "").strip()
        if not global_intent_summary:
            global_intent_summary = helper._fallback_global_intent_summary(
                str(task.metadata.get("original_message", "") or "")
            )

        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            target_projection_id = str(
                item.get("work_item_projection_id")
                or item.get("projection_id")
                or ""
            ).strip()
            if not target_projection_id or target_projection_id not in allowed_projection_ids:
                continue
            projection = projection_lookup.get(target_projection_id)
            if projection is None:
                continue
            item_global_intent = str(item.get("global_intent_summary", "") or global_intent_summary).strip() or global_intent_summary
            normalized.append(
                helper._coerce_projection_assignment(
                    item,
                    projection=projection,
                    global_intent_summary=item_global_intent,
                )
            )
        return normalized

    @staticmethod
    def _extract_runtime_state(result: TaskResult | None) -> dict[str, Any]:
        artifacts = dict((result.artifacts if result else None) or {})
        runtime_session_id = str(artifacts.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return {}
        return {
            "runtime_session_id": runtime_session_id,
            "active_subagents": list(artifacts.get("active_subagents", []) or []),
            "permission_requests": list(artifacts.get("permission_requests", []) or []),
            "compaction_boundaries": list(artifacts.get("compaction_boundaries", []) or []),
            "compaction_records": list(artifacts.get("compaction_records", artifacts.get("compaction_boundaries", [])) or []),
            "resume_cursor": artifacts.get("resume_cursor"),
            "worktree_path": str(artifacts.get("worktree_path", "") or "").strip(),
            "task_ledger": list(artifacts.get("task_ledger", []) or []),
            "prefetch_hits": list(artifacts.get("prefetch_hits", []) or []),
            "verification": dict(artifacts.get("verification", {}) or {}),
            "verification_evidence": dict(artifacts.get("verification_evidence", {}) or {}),
            "verification_verdict": str(artifacts.get("verification_verdict", "") or "").strip(),
            "artifact_manifest": list(artifacts.get("artifact_manifest", []) or []),
            "resume_state": dict(artifacts.get("resume_state", {}) or {}),
        }

    def _runtime_checkpoint_payload(self, task: Task) -> dict[str, Any]:
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if not runtime_state:
            runtime_state = self._extract_runtime_state(
                TaskResult(
                    status=TaskStatus.DONE,
                    artifacts=dict(task.result.get("artifacts", {}) if isinstance(task.result, dict) else {}),
                )
            )
        if not runtime_state:
            return {}
        return {
            "runtime_v2": runtime_state,
            "runtime_session_id": runtime_state.get("runtime_session_id", ""),
            "resume_cursor": runtime_state.get("resume_cursor"),
            "active_subagents": list(runtime_state.get("active_subagents", []) or []),
            "permission_requests": list(runtime_state.get("permission_requests", []) or []),
            "compaction_boundaries": list(runtime_state.get("compaction_boundaries", []) or []),
            "compaction_records": list(runtime_state.get("compaction_records", []) or []),
            "worktree_path": runtime_state.get("worktree_path", ""),
            "task_ledger": list(runtime_state.get("task_ledger", []) or []),
            "prefetch_hits": list(runtime_state.get("prefetch_hits", []) or []),
            "verification": dict(runtime_state.get("verification", {}) or {}),
            "verification_evidence": dict(runtime_state.get("verification_evidence", {}) or {}),
            "verification_verdict": runtime_state.get("verification_verdict", ""),
            "resume_state": dict(runtime_state.get("resume_state", {}) or {}),
        }

    def _collect_artifact_refs(self, artifacts: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for key, value in (artifacts or {}).items():
            if isinstance(value, str) and value.strip():
                refs.append(f"{key}: {value.strip()}")
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        refs.append(f"{key}: {item.strip()}")
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, str) and nested_value.strip():
                        refs.append(f"{key}.{nested_key}: {nested_value.strip()}")
        unique_refs: list[str] = []
        for ref in refs:
            if ref not in unique_refs:
                unique_refs.append(ref)
        return unique_refs[:10]

    def _work_item_checkpoint_payload(self, task: Task) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        context_outputs = dict((task.context_snapshot or {}).get("work_item_owned_outputs", {}) or {})
        for key in (
            "work_item_artifact_index",
            "work_item_summary",
            "work_item_summary_for_downstream",
            "structured_review_verdict",
            "verification_status",
            "verification_evidence",
            "delivery_package",
        ):
            value = context_outputs.get(key)
            if value not in (None, "", [], {}):
                payload[key] = copy.deepcopy(value)
        for key in (
            "work_item_turn_type",
            "work_item_runtime_plan",
            "work_item_artifact_index",
            "work_item_summary",
            "work_item_orchestration_profile",
            "work_item_verification_required",
            "structured_review_verdict",
            "verification_status",
            "verification_evidence",
            "artifact_contract_status",
            "member_session_id",
            "member_session_state",
            "message_priority",
            "ownership_contract",
            "workspace_manifest",
            "data_acquisition_log",
            "data_acquisition_report",
            "gate_harness_status",
            "gate_harness_constraints",
            "gate_harness_pending_decision",
            "gate_harness_decision",
            "gate_harness_evidence",
        ):
            if key not in task.metadata:
                continue
            value = task.metadata.get(key)
            if value in (None, "", [], {}):
                continue
            payload[key] = value
        return payload

    def _extract_structured_work_item_payload(
        self,
        content: str,
        artifacts: dict[str, Any] | None,
    ) -> dict[str, Any]:
        artifact_payload = dict(artifacts or {})
        payload: dict[str, Any] = {}
        for key in (
            "runtime_plan",
            "work_item_runtime_plan",
            "artifact_index",
            "work_item_artifact_index",
            "review_verdict",
            "structured_review_verdict",
            "delivery_package",
            "final_delivery_package",
            "follow_up_actions",
        ):
            if key in artifact_payload:
                payload[key] = artifact_payload[key]
        decoder = json.JSONDecoder()
        search = str(content or "").strip()
        start = search.find("{")
        while start != -1:
            try:
                data, consumed = decoder.raw_decode(search[start:])
            except json.JSONDecodeError:
                start = search.find("{", start + 1)
                continue
            if isinstance(data, dict):
                for key in (
                    "runtime_plan",
                    "work_item_runtime_plan",
                    "artifact_index",
                    "work_item_artifact_index",
                    "review_verdict",
                    "structured_review_verdict",
                    "delivery_package",
                    "final_delivery_package",
                    "follow_up_actions",
                ):
                    if key in data and key not in payload:
                        payload[key] = data[key]
                if "review_verdict" not in payload and any(key in data for key in ("verdict", "decision", "status")):
                    payload["review_verdict"] = data
            start = search.find("{", start + consumed)
        if "runtime_plan" not in payload and "work_item_runtime_plan" in payload:
            payload["runtime_plan"] = payload["work_item_runtime_plan"]
        if "artifact_index" not in payload and "work_item_artifact_index" in payload:
            payload["artifact_index"] = payload["work_item_artifact_index"]
        if "review_verdict" not in payload and "structured_review_verdict" in payload:
            payload["review_verdict"] = payload["structured_review_verdict"]
        if "delivery_package" not in payload and "final_delivery_package" in payload:
            payload["delivery_package"] = payload["final_delivery_package"]
        return payload

    def _normalize_delivery_package(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        package = {str(key): val for key, val in value.items()}
        for list_key in (
            "delivered_items",
            "artifact_manifest",
            "constraints",
            "risks",
            "open_issues",
            "next_steps",
            "source_projection_refs",
        ):
            items = package.get(list_key, [])
            if not isinstance(items, list):
                package[list_key] = []
                continue
            normalized_items: list[Any] = []
            for item in items:
                if isinstance(item, dict):
                    normalized_items.append(dict(item))
                elif isinstance(item, str) and item.strip():
                    normalized_items.append(item.strip())
            package[list_key] = normalized_items
        summary = str(package.get("executive_summary", "") or package.get("summary", "") or "").strip()
        if summary:
            package["executive_summary"] = summary
        return package

    def _build_work_item_artifact_index(
        self,
        artifacts: dict[str, Any] | None,
        fallback_refs: list[str],
    ) -> list[dict[str, str]]:
        index: list[dict[str, str]] = []
        for key, value in dict(artifacts or {}).items():
            if isinstance(value, str) and value.strip():
                index.append({"kind": str(key), "label": str(key), "value": value.strip()})
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        index.append({"kind": str(key), "label": str(key), "value": item.strip()})
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, str) and nested_value.strip():
                        index.append({
                            "kind": str(key),
                            "label": f"{key}.{nested_key}",
                            "value": nested_value.strip(),
                        })
        for ref in fallback_refs:
            if isinstance(ref, str) and ref.strip():
                index.append({"kind": "artifact_ref", "label": "artifact_ref", "value": ref.strip()})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in index:
            fingerprint = (item.get("kind", ""), item.get("label", ""), item.get("value", ""))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]

    def _normalize_work_item_artifact_index(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                rendered = {
                    "kind": str(item.get("kind", "") or "artifact").strip() or "artifact",
                    "label": str(item.get("label", "") or item.get("name", "") or "artifact").strip() or "artifact",
                    "value": str(item.get("value", "") or item.get("location", "") or item.get("path", "") or "").strip(),
                }
                if rendered["value"]:
                    normalized.append(rendered)
            elif isinstance(item, str) and item.strip():
                normalized.append({"kind": "artifact", "label": "artifact", "value": item.strip()})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in normalized:
            fingerprint = (item["kind"], item["label"], item["value"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]

    def _parse_worker_report(self, raw_content: str) -> dict[str, Any] | None:
        """Best-effort parse of a worker handoff report.

        The report-generation prompt suggests (but does not strictly
        require) a JSON object on the last line with shape::

            {
              "summary": str,
              "deliverables": [{"name", "path", "status"}],
              "acceptance_status": [{"criterion", "met", "evidence"}],
              "risks": [str],
              "next_actions": [str]
            }

        Per design: when parsing fails we DO NOT re-prompt the worker —
        we just hand the raw prose to the reviewer. So this helper
        returns ``None`` on failure and the caller falls back to prose.
        """
        text = str(raw_content or "").strip()
        if not text:
            return None
        # Try to find a JSON object in the tail of the prose.
        candidates: list[str] = []
        # 1. Try fenced ```json blocks anywhere.
        import re as _re
        for match in _re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.DOTALL):
            candidates.append(match.group(1))
        # 2. Try the last balanced { ... } in the text.
        depth = 0
        start = -1
        last_balanced: str | None = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        last_balanced = text[start : i + 1]
        if last_balanced:
            candidates.append(last_balanced)
        for blob in reversed(candidates):
            try:
                parsed = json.loads(blob)
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            # Accept anything dict-shaped — the schema is suggestive,
            # not strict. Keep only the recognized top-level keys so
            # downstream consumers see a clean payload.
            allowed_keys = {
                "summary",
                "deliverables",
                "acceptance_status",
                "risks",
                "next_actions",
            }
            cleaned: dict[str, Any] = {}
            for key in allowed_keys:
                if key in parsed:
                    cleaned[key] = parsed[key]
            # If none of the recognized keys were present, treat the
            # blob as non-report JSON (e.g. a tool call payload that
            # happens to trail the prose) and skip it.
            if not cleaned:
                continue
            return cleaned
        return None

    def _normalize_review_verdict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"approve", "approved", "pass", "passed", "accept", "accepted"}:
                return {"label": "approve", "summary": value.strip()}
            if lowered in {"reject", "rejected", "fail", "failed", "rework"}:
                return {"label": "reject", "summary": value.strip()}
            return {}
        if not isinstance(value, dict):
            return {}
        # Accept both the raw agent JSON shape (review_verdict|verdict|
        # decision|status) AND the already-normalized shape (label) emitted
        # by the external broker's adapter.infer_review_verdict.
        raw = str(
            value.get("review_verdict")
            or value.get("verdict")
            or value.get("decision")
            or value.get("status")
            or value.get("label")
            or ""
        ).strip().lower()
        if raw in {"approved", "pass", "passed", "accept", "accepted"}:
            raw = "approve"
        elif raw in {"rejected", "fail", "failed", "rework"}:
            raw = "reject"
        if raw not in {"approve", "reject"}:
            return {}
        blocking = value.get("blocking_issues", [])
        followups = value.get("followups", [])
        return {
            "label": raw,
            "summary": str(value.get("summary", "") or "").strip(),
            "blocking_issues": [
                str(item).strip()
                for item in (blocking if isinstance(blocking, list) else [])
                if str(item).strip()
            ][:8],
            "followups": [
                str(item).strip()
                for item in (followups if isinstance(followups, list) else [])
                if str(item).strip()
            ][:8],
        }

    def _structured_or_inferred_verdict(self, task: Task, gate: WorkItemGatePolicy) -> str:
        output_metadata = self._work_item_output_metadata_for_task(task)
        structured = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if structured.get("label") in {"approve", "reject"}:
            return str(structured["label"])
        return "reject"

    def _review_feedback_with_fallback(self, review_task: Task) -> str:
        """Return the reviewer's feedback string, with a content
        fallback when the agent did not emit a structured verdict.

        Mirrors the gate path's salvage at ``_apply_review_gate``
        (search for ``not reviewer_feedback``). Lifted out of the
        inline ``_finalize_review_work_item`` block so unit tests
        can exercise the fallback without standing up the whole
        CompanyMode + dispatcher harness.
        """
        feedback = self._structured_review_feedback(review_task)
        if feedback:
            return feedback
        review_result = getattr(review_task, "result", None)
        if isinstance(review_result, dict):
            return str(review_result.get("content", "") or "").strip()
        if review_result is not None:
            return str(getattr(review_result, "content", "") or "").strip()
        return ""

    def _structured_review_feedback(self, task: Task) -> str:
        output_metadata = self._work_item_output_metadata_for_task(task)
        structured = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if not structured:
            return ""
        lines: list[str] = []
        summary = str(structured.get("summary", "") or "").strip()
        if summary:
            lines.append(summary)
        blocking = list(structured.get("blocking_issues", []) or [])
        if blocking:
            lines.append("Blocking issues:")
            lines.extend(f"- {item}" for item in blocking)
        followups = list(structured.get("followups", []) or [])
        if followups:
            lines.append("Follow-ups:")
            lines.extend(f"- {item}" for item in followups)
        return "\n".join(lines).strip()

    def _build_verification_status(
        self,
        task: Task,
        result: TaskResult,
        *,
        review_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        verification_evidence = dict(result.artifacts.get("verification_evidence", {}) if result.artifacts else {})
        if verification_evidence:
            label = "verified" if str(verification_evidence.get("verdict", "")).strip().lower() == "pass" else "not_verified"
            return {
                "label": label,
                "source": "runtime_verifier_evidence",
                "summary": str(verification_evidence.get("summary", "") or verification_evidence.get("raw_output", "") or "").strip(),
            }
        verification_entries = result.artifacts.get("verification", []) if result.artifacts else []
        if isinstance(verification_entries, list) and verification_entries:
            statuses: list[str] = []
            summaries: list[str] = []
            for item in verification_entries:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "") or "").strip()
                summary = str(item.get("summary", "") or item.get("verdict", "") or "").strip()
                if status:
                    statuses.append(status)
                if summary:
                    summaries.append(summary)
            label = "verified"
            if any(status in {"issues", "failed", "inconclusive"} for status in statuses):
                label = "not_verified"
            return {
                "label": label,
                "source": "runtime_verifier",
                "summary": "; ".join(summaries[:3]).strip(),
            }
        if review_verdict.get("label"):
            return {
                "label": f"review_{review_verdict['label']}",
                "source": "review_work_item",
                "summary": str(review_verdict.get("summary", "") or "").strip(),
            }
        explicit = task.metadata.get("work_item_verification_required")
        if explicit is False:
            return {
                "label": "not_required",
                "source": "work_item_policy",
                "summary": "This work item does not require a separate verification pass.",
            }
        return {}

    @staticmethod
    def _verification_evidence_satisfies_contract(verification_evidence: dict[str, Any]) -> bool:
        status = str(verification_evidence.get("status", "") or "").strip().lower()
        return status in {"provided", "unavailable"}

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[1] if "\n" in value else value[3:]
            if value.endswith("```"):
                value = value[:-3]
        return value.strip()

    @staticmethod
    def _projection_id_for_task(task: Task) -> str:
        return projection_id_for_task(task)

    @staticmethod
    def _turn_type_for_task(task: Task, *, fallback: str = "") -> str:
        return turn_type_for_task(task, fallback=fallback)

    @staticmethod
    def _role_id_for_task(task: Task) -> str:
        return str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()

    def _role_name_for_task(self, task: Task) -> str:
        role_id = self._role_id_for_task(task)
        if not role_id:
            return ""
        agent = self.org_engine.get_agent(role_id) if self.org_engine else None
        return str(getattr(agent, "name", "") or task.metadata.get("work_item_role_name", "") or role_id).strip()

    @staticmethod
    def _task_summary_for_map(task: Task) -> str:
        output_metadata = CompanyWorkItemExecutor._work_item_output_metadata_for_task(task)
        summary = str(
            output_metadata.get("work_item_summary", "")
            or output_metadata.get("work_item_summary_for_downstream", "")
            or task.metadata.get("work_item_summary", "")
            or task.metadata.get("work_item_summary_for_downstream", "")
            or ""
        ).strip()
        if summary:
            return summary
        if isinstance(task.result, dict) and task.result.get("content"):
            return str(task.result.get("content", "")).strip()
        return ""

    def _task_open_issues(self, task: Task) -> list[str]:
        issues: list[str] = []
        output_metadata = self._work_item_output_metadata_for_task(task)
        review_verdict = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if review_verdict.get("label") == "reject":
            summary = str(review_verdict.get("summary", "") or "review rejected").strip()
            issues.append(f"review rejected: {summary}")
        if task.status in {
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.AWAITING_PEER,
            *list(_REVIEW_WAITING_STATUSES),
        }:
            issues.append(f"status: {task.status.value}")
        for metadata_key, label in (
            ("gate_review_feedback", "gate rework"),
            ("contract_rework_feedback", "contract rework"),
            ("ceo_rework_feedback", "executive rework"),
            ("gate_harness_rework_feedback", "gate harness rework"),
        ):
            text = str(task.metadata.get(metadata_key, "") or "").strip()
            if text:
                issues.append(f"{label}: {text}")
        pending_decision = dict(task.metadata.get("gate_harness_pending_decision", {}) or {})
        if pending_decision:
            issues.append(f"gate harness pending: {str(pending_decision.get('summary', '') or '').strip()}")
        deduped: list[str] = []
        for issue in issues:
            if issue and issue not in deduped:
                deduped.append(issue)
        return deduped[:8]

    def _build_role_task_map(self, tasks: list[Task]) -> dict[str, dict[str, Any]]:
        role_task_map: dict[str, dict[str, Any]] = {}
        for task in tasks:
            role_id = self._role_id_for_task(task)
            if not role_id:
                continue
            entry = role_task_map.setdefault(
                role_id,
                {
                    "role_id": role_id,
                    "role_name": self._role_name_for_task(task),
                    "responsibility": str(
                        getattr(self.org_engine.get_agent(role_id) if self.org_engine else None, "responsibility", "") or ""
                    ).strip(),
                    "employees": [],
                    "work_items": [],
                },
            )
            employee_assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_payload = {
                "employee_id": str(employee_assignment.get("employee_id", "") or "").strip(),
                "employee_name": str(employee_assignment.get("name", "") or "").strip(),
                "role_id": str(employee_assignment.get("role_id", "") or role_id).strip(),
            }
            if employee_payload["employee_id"] and employee_payload not in entry["employees"]:
                entry["employees"].append(employee_payload)
            entry["work_items"].append(
                {
                    "projection_id": self._projection_id_for_task(task),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "title": task.title,
                    "status": getattr(task.status, "value", str(task.status)),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "assigned_to": role_id,
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": employee_assignment,
                    "work_item_assignment": dict(task.metadata.get("work_item_assignment", {}) or {}),
                }
            )
        return role_task_map

    def _inject_work_item_role_map(self, task: Task) -> None:
        turn_type = turn_type_for_task(task, fallback="")
        if turn_type not in {"intake", "plan", "dispatch", "aggregate", "deliver", "review"} and not bool(
            task.metadata.get("authoritative_output", False)
        ):
            return
        role_task_map = self._build_role_task_map(list(self._active_tasks))
        if not role_task_map:
            return
        task.metadata = dict(task.metadata)
        task.context_snapshot = dict(task.context_snapshot)
        task.metadata["work_item_role_task_map"] = role_task_map
        task.context_snapshot["work_item_role_task_map"] = role_task_map

    @staticmethod
    def _metadata_flag_true(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _is_final_human_acceptance_metadata(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        work_item_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Return True only for the final user-visible delivery acceptance card."""
        meta = dict(metadata or {})
        item_meta = dict(work_item_metadata or {})
        combined = {**item_meta, **meta}
        if self._metadata_flag_true(combined.get("attention_work_item", False)):
            return False
        if not self._metadata_flag_true(combined.get("authoritative_output", False)):
            return False
        if not self._metadata_flag_true(combined.get("user_visible", False)):
            return False
        if str(combined.get("feedback_scope", "") or "").strip().lower() != "final":
            return False
        return (
            work_item_turn_type_from_metadata(combined, fallback="") == "deliver"
            or is_delivery_turn(combined)
            or str(combined.get("review_owner_kind", "") or "").strip().lower() == "human"
        )

    def _is_final_human_acceptance_task(
        self,
        task: Task,
        work_item: Any | None = None,
    ) -> bool:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        if str(task_metadata.get("execution_mode", "") or "").strip() != "company_mode":
            return False
        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {}) if work_item is not None else None
        return self._is_final_human_acceptance_metadata(
            task_metadata,
            work_item_metadata=work_item_metadata,
        )

    def _is_authoritative_delivery_work_item(self, task: Task) -> bool:
        return self._is_final_human_acceptance_task(task)

    def _build_ceo_rework_record(
        self,
        *,
        source_task: Task,
        target_task: Task,
        feedback: str,
        rework_round: int,
        source: str,
    ) -> dict[str, Any]:
        target_projection_id = self._projection_id_for_task(target_task)
        return {
            "source": source,
            "requested_by_projection_id": self._projection_id_for_task(source_task),
            "requested_by_work_item_title": source_task.title,
            "requested_by_role_id": self._role_id_for_task(source_task),
            **gate_rework_payload(target_projection_id=target_projection_id),
            "target_work_item_title": target_task.title,
            "target_role_id": self._role_id_for_task(target_task),
            "feedback": feedback,
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }

    def _render_ceo_rework_summary(self, rework_request: dict[str, Any]) -> str:
        target_work_item = str(
            rework_request.get("target_work_item_title", "")
            or "Current work item"
        ).strip()
        requested_by = str(
            rework_request.get("requested_by_work_item_title", "")
            or "Executive review"
        ).strip()
        feedback = str(rework_request.get("feedback", "") or "").strip()
        round_no = int(rework_request.get("rework_round", 1) or 1)
        lines = [
            f"Executive rework requested for {target_work_item}.",
            f"Requested by: {requested_by}",
            f"Round: {round_no}",
        ]
        if feedback:
            lines.append(f"## Executive Feedback\n{feedback}")
        lines.append("Resume your previous work session, address the issues above, and then resubmit this work item.")
        return "\n\n".join(lines)

    @staticmethod
    def _reset_work_item_outputs_for_rework(task: Task) -> None:
        task.metadata = dict(task.metadata)
        for key in (
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
        ):
            task.metadata.pop(key, None)
        task.context_snapshot = dict(task.context_snapshot)
        for key in (
            "latest_artifacts",
            "delivery_package",
            "work_item_owned_outputs",
            "latest_ceo_rework",
            "upstream_ceo_rework_source_projection_id",
            "latest_gate_harness_rework",
            "upstream_gate_harness_rework_source_projection_id",
        ):
            task.context_snapshot.pop(key, None)

    def _collect_downstream_projection_ids(self, projection_id: str) -> list[str]:
        plan = self._active_plan
        if plan is None or not projection_id:
            return []
        dependents: dict[str, list[str]] = {}
        for projection in plan.projections:
            for dep in projection.dependency_projection_ids:
                dependents.setdefault(str(dep).strip(), []).append(str(projection.projection_id).strip())
        ordered: list[str] = []
        queue = list(dependents.get(projection_id, []))
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            queue.extend(dependents.get(current, []))
        return ordered

    def _fallback_ceo_pre_delivery_assessment(
        self,
        delivery_task: Task,
        tasks: list[Task],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        blocking_projection_ids: list[str] = []
        for task in tasks:
            if task.id == delivery_task.id:
                continue
            if self._task_open_issues(task):
                blocking_projection_ids.append(self._projection_id_for_task(task))
        if not blocking_projection_ids:
            return {
                "deliverable": True,
                "summary": "No unresolved blocking work-item issues were detected before delivery.",
                "rework_targets": [],
            }
        return {
            "deliverable": False,
            "summary": (
                f"Delivery is not ready because the work-item runtime still has {len(package.get('open_issues', [])) or len(blocking_projection_ids)} "
                "open issue(s)."
            ),
            "rework_targets": [
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "feedback": "Resolve the outstanding issues recorded in the work-item runtime before the final delivery is sent to the user.",
                }
                for projection_id in list(dict.fromkeys(blocking_projection_ids))
            ],
        }

    @staticmethod
    def _pre_delivery_assessment_unavailable(
        fallback: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        fallback_deliverable = bool(fallback.get("deliverable", True))
        summary = str(fallback.get("summary", "") or "").strip()
        payload = {
            "deliverable": fallback_deliverable,
            "summary": summary or "Pre-delivery assessment was unavailable.",
            "rework_targets": [],
            "assessment_status": "unavailable",
            "assessment_failure_kind": reason,
            "assessment_infrastructure_failure": True,
        }
        if not fallback_deliverable:
            payload["awaiting_human"] = True
            payload["summary"] = (
                f"{payload['summary']} Pre-delivery assessment could not produce a "
                "structured decision, so automatic rework is suspended."
            )
        return payload

    @staticmethod
    def _resolve_max_pre_delivery_reworks(task: Task) -> int:
        raw = getattr(task, "metadata", {}).get("max_pre_delivery_reworks", DEFAULT_MAX_PRE_DELIVERY_REWORKS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_MAX_PRE_DELIVERY_REWORKS
        return max(0, value)

    def _resolve_ceo_rework_targets(
        self,
        raw_targets: Any,
        task_by_projection_id: dict[str, Task],
        *,
        fallback_projection_ids: list[str] | None = None,
        default_feedback: str = "",
    ) -> list[dict[str, str]]:
        resolved: list[dict[str, str]] = []
        seen_projection_ids: set[str] = set()
        items = list(raw_targets) if isinstance(raw_targets, list) else []
        for item in items:
            projection_id = ""
            role_id = ""
            feedback = default_feedback
            if isinstance(item, str):
                token = str(item).strip()
                if token in task_by_projection_id:
                    projection_id = token
                else:
                    role_id = token
            elif isinstance(item, dict):
                projection_id = str(
                    item.get("target_projection_id")
                    or item.get("work_item_projection_id")
                    or item.get("projection_id")
                    or ""
                ).strip()
                role_id = str(item.get("role_id", "") or "").strip()
                feedback = str(item.get("feedback", "") or item.get("reason", "") or default_feedback).strip()
            if not projection_id and role_id:
                matching = [
                    task
                    for task in list(self._active_tasks)
                    if self._role_id_for_task(task) == role_id
                ]
                matching.sort(
                    key=lambda task: (
                        0 if self._task_open_issues(task) else 1,
                        0 if task.status != TaskStatus.DONE else 1,
                        self._projection_id_for_task(task),
                    )
                )
                if matching:
                    projection_id = self._projection_id_for_task(matching[0])
            target_task = task_by_projection_id.get(projection_id)
            if target_task is None or projection_id in seen_projection_ids:
                continue
            seen_projection_ids.add(projection_id)
            resolved.append(
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "role_id": self._role_id_for_task(target_task),
                    "feedback": feedback,
                }
            )
        if resolved:
            return resolved
        for projection_id in list(fallback_projection_ids or []):
            target_task = task_by_projection_id.get(projection_id)
            if target_task is None or projection_id in seen_projection_ids:
                continue
            seen_projection_ids.add(projection_id)
            resolved.append(
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "role_id": self._role_id_for_task(target_task),
                    "feedback": default_feedback,
                }
            )
        return resolved

    async def _ceo_pre_delivery_assessment(
        self,
        delivery_task: Task,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = self._fallback_ceo_pre_delivery_assessment(delivery_task, tasks, package)
        work_item_tasks: list[dict[str, Any]] = []
        for task in tasks:
            output_metadata = self._work_item_output_metadata_for_task(task)
            work_item_tasks.append(
                {
                    "task_id": task.id,
                    "projection_id": self._projection_id_for_task(task),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "title": task.title,
                    "status": getattr(task.status, "value", str(task.status)),
                    "role_id": self._role_id_for_task(task),
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": dict(task.metadata.get("employee_assignment", {}) or {}),
                    "work_item_assignment": dict(task.metadata.get("work_item_assignment", {}) or {}),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "risks": [str(item).strip() for item in list(output_metadata.get("risks", []) or []) if str(item).strip()],
                    "dependency_projection_ids": list(task.dependencies),
                }
            )
        prompt = {
            "project_id": delivery_task.project_id,
            "company_profile": plan.profile,
            "delivery_projection_id": self._projection_id_for_task(delivery_task),
            "delivery_projection_title": delivery_task.title,
            "delivery_role_id": self._role_id_for_task(delivery_task),
            "delivery_role_name": self._role_name_for_task(delivery_task),
            "role_task_map": self._build_role_task_map(tasks),
            "delivery_package": package,
            "work_item_tasks": work_item_tasks,
        }
        raw = await self._run_role_prompt(
            source_task=delivery_task,
            system_prompt=EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
            payload=prompt,
            prompt_kind="ceo_pre_delivery_assessment",
            force_new_session=True,
        )
        data = self._parse_role_prompt_json(raw) if raw is not None else None
        if data is None and self.role_prompt_runner is not None:
            reason = "role_prompt_empty_result" if raw is None else "role_prompt_non_json_output"
            logger.debug("Executive pre-delivery assessment unavailable: {}", reason)
            return self._pre_delivery_assessment_unavailable(fallback, reason=reason)
        if data is None and self.role_prompt_runner is None and self.llm is not None:
            try:
                data = await call_llm_json_with_retry(
                    self.llm,
                    system=EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
                    payload=prompt,
                    task_type="quick_tasks",
                    label="ceo_pre_delivery_assessment",
                )
            except LLMRetryError as exc:
                logger.debug(f"Executive pre-delivery assessment failed after retries: {exc}")
                return fallback
            except Exception as exc:
                logger.debug(f"Executive pre-delivery assessment construction failed: {exc}")
                return fallback
        if data is None:
            logger.debug("Executive pre-delivery assessment returned non-JSON output")
            return fallback
        summary = str(data.get("summary", "") or fallback.get("summary", "")).strip()
        return {
            "deliverable": bool(data.get("deliverable", fallback.get("deliverable", True))),
            "summary": summary or str(fallback.get("summary", "")).strip(),
            "rework_targets": list(data.get("rework_targets", []) or []),
        }

    async def _ceo_initiate_rework(
        self,
        target_projection_id: str,
        feedback: str,
        task_by_projection_id: dict[str, Task],
        *,
        source_task: Task,
        source: str,
    ) -> Task | None:
        target_task = task_by_projection_id.get(target_projection_id)
        if target_task is None:
            return None
        normalized_feedback = self._normalize_gate_feedback(
            feedback,
            fallback=(
                f"{source_task.title} cannot move forward yet. "
                "Review the executive feedback, address the blocking issues, and resubmit."
            ),
        )
        rework_round = int(target_task.metadata.get("ceo_rework_count", 0) or 0) + 1
        rework_request = self._build_ceo_rework_record(
            source_task=source_task,
            target_task=target_task,
            feedback=normalized_feedback,
            rework_round=rework_round,
            source=source,
        )
        affected_projection_ids = [target_projection_id, *self._collect_downstream_projection_ids(target_projection_id)]
        touched_task_ids: set[str] = set()
        for affected_projection_id in affected_projection_ids:
            affected_task = task_by_projection_id.get(affected_projection_id)
            if affected_task is None or affected_task.id in touched_task_ids:
                continue
            touched_task_ids.add(affected_task.id)
            affected_task.metadata = dict(affected_task.metadata)
            affected_task.context_snapshot = dict(affected_task.context_snapshot)
            affected_task.result = None
            self._reset_work_item_outputs_for_rework(affected_task)
            if affected_projection_id == target_projection_id:
                affected_task.metadata["ceo_rework_count"] = rework_round
                affected_task.metadata["ceo_rework_feedback"] = normalized_feedback
                affected_task.metadata["ceo_rework_feedback_full"] = str(feedback or "").strip()
                affected_task.metadata["ceo_rework_request"] = dict(rework_request)
                history = list(affected_task.metadata.get("ceo_rework_requests", []) or [])
                history.append(dict(rework_request))
                affected_task.metadata["ceo_rework_requests"] = history[-6:]
                affected_task.context_snapshot["ceo_rework_feedback"] = normalized_feedback
                affected_task.context_snapshot["latest_ceo_rework"] = dict(rework_request)
                await self._append_progress(affected_task, self._render_ceo_rework_summary(rework_request))
            else:
                affected_task.metadata["upstream_ceo_rework_source_projection_id"] = target_projection_id
                affected_task.context_snapshot["upstream_ceo_rework_source_projection_id"] = target_projection_id
                adaptive = self._normalize_adaptive_metadata(affected_task.metadata.get("adaptive", {}))
                adaptive["normalized_state"] = "invalidated"
                affected_task.metadata["adaptive"] = adaptive
                await self._append_progress(
                    affected_task,
                    f"Reset because upstream work-item projection `{target_projection_id}` entered executive-directed rework.",
                )
            await transition_work_item_from_task(
                self.store, affected_task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="ceo_rework_reset",
            )
            await self.save_task(affected_task)
            # Emit work_item_progress event so the UI reverts the work item from
            # "done" (checkmark) back to "active" (dots) during rework.
            await self._emit_progress(
                f"[Company:{affected_projection_id}] reworking ({source} rework round {rework_round})",
                task_id=affected_task.id,
            )
        return target_task

    async def _mark_run_awaiting_owner_from_delivery(
        self,
        task: Task,
        *,
        summary: str = "",
    ) -> None:
        if not self.store or not hasattr(self.store, "get_delegation_run") or not hasattr(self.store, "save_delegation_run"):
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id:
            return
        try:
            run = await self.store.get_delegation_run(run_id)
        except Exception:
            logger.opt(exception=True).debug("Failed to load delegation run for owner review lifecycle update")
            return
        if run is None:
            return
        run.lifecycle_status = "awaiting_owner"
        run.status = "running"
        if summary:
            run.latest_deliverable_summary = str(summary or "").strip()
        run.metadata = {
            **dict(run.metadata or {}),
            "awaiting_owner_review": True,
            "awaiting_owner_review_task_id": task.id,
            "awaiting_owner_review_at": datetime.now().isoformat(),
        }
        try:
            await self.store.save_delegation_run(run)
        except Exception:
            logger.opt(exception=True).debug("Failed to save delegation run owner review lifecycle update")

    async def _finalize_completed_work_item(self, task: Task) -> None:
        if self._is_authoritative_delivery_work_item(task):
            plan = self._active_plan or CompanyWorkItemRuntimePlan(
                profile=str(task.metadata.get("company_profile", "") or "company"),
            )
            tasks = list(self._active_tasks) or [task]
            package = self._build_authoritative_delivery_package(plan, tasks, task)
            task.metadata = dict(task.metadata)
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["delivery_package"] = package
            self._set_work_item_output_context(task, {"delivery_package": package})
            linked_work_item_id = linked_work_item_id_for_task(task)
            if linked_work_item_id:
                await update_work_item_owned_metadata(self.store, linked_work_item_id, {"delivery_package": package})
                task.metadata.pop("delivery_package", None)
            else:
                task.metadata["delivery_package"] = package
            assessment = await self._ceo_pre_delivery_assessment(task, plan, tasks, package)
            task.metadata["ceo_pre_delivery_assessment"] = dict(assessment)
            if bool(assessment.get("awaiting_human")):
                task.metadata["pre_delivery_assessment_status"] = str(
                    assessment.get("assessment_status", "awaiting_human") or "awaiting_human"
                )
                task.metadata["pre_delivery_assessment_failure_kind"] = str(
                    assessment.get("assessment_failure_kind", "") or ""
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.AWAITING_HUMAN,
                    reason="pre_delivery_assessment_unavailable",
                )
                await self._append_progress(
                    task,
                    str(assessment.get("summary", "") or "Final delivery is awaiting human review."),
                )
                await self._mark_run_awaiting_owner_from_delivery(
                    task,
                    summary=str(assessment.get("summary", "") or "").strip(),
                )
                await self.save_task(task)
                await self._save_feedback_checkpoint(task)
                await self._emit_progress(
                    f"[Company:{self._projection_id_for_task(task)}] final delivery awaiting human review",
                    task_id=task.id,
                )
                return
            if not bool(assessment.get("deliverable", True)):
                task_by_projection_id: dict[str, Task] = {}
                fallback_projection_ids = [
                    self._projection_id_for_task(candidate)
                    for candidate in tasks
                    if candidate.id != task.id and self._task_open_issues(candidate)
                ]
                for work_item_task in tasks:
                    task_by_projection_id[work_item_task.id] = work_item_task
                    task_by_projection_id[self._projection_id_for_task(work_item_task)] = work_item_task
                rework_targets = self._resolve_ceo_rework_targets(
                    assessment.get("rework_targets", []),
                    task_by_projection_id,
                    fallback_projection_ids=fallback_projection_ids,
                    default_feedback=str(assessment.get("summary", "") or "").strip(),
                )
                if rework_targets:
                    try:
                        prior_pre_delivery_reworks = int(task.metadata.get("pre_delivery_rework_count", 0) or 0)
                    except (TypeError, ValueError):
                        prior_pre_delivery_reworks = 0
                    max_pre_delivery_reworks = self._resolve_max_pre_delivery_reworks(task)
                    if prior_pre_delivery_reworks >= max_pre_delivery_reworks:
                        task.metadata["pre_delivery_rework_cap_reached"] = True
                        task.metadata["pre_delivery_rework_cap"] = max_pre_delivery_reworks
                        await transition_work_item_from_task(
                            self.store, task,
                            target_status_or_phase=Phase.AWAITING_HUMAN,
                            reason="pre_delivery_rework_cap_reached",
                        )
                        await self._append_progress(
                            task,
                            (
                                "Final delivery reached the pre-delivery rework cap "
                                f"({max_pre_delivery_reworks}); awaiting human review."
                            ),
                        )
                        await self._mark_run_awaiting_owner_from_delivery(
                            task,
                            summary="Final delivery reached the pre-delivery rework cap; awaiting human review.",
                        )
                        await self.save_task(task)
                        await self._save_feedback_checkpoint(task)
                        await self._emit_progress(
                            f"[Company:{self._projection_id_for_task(task)}] pre-delivery rework cap reached",
                            task_id=task.id,
                        )
                        return
                    task.metadata["pre_delivery_rework_count"] = prior_pre_delivery_reworks + 1
                    for item in rework_targets:
                        target_projection_id = str(
                            item.get("target_projection_id")
                            or item.get("work_item_projection_id")
                            or ""
                        ).strip()
                        if not target_projection_id:
                            continue
                        await self._ceo_initiate_rework(
                            target_projection_id,
                            item.get("feedback", "") or str(assessment.get("summary", "") or ""),
                            task_by_projection_id,
                            source_task=task,
                            source="pre_delivery",
                        )
                    if task.status != TaskStatus.PENDING:
                        task.result = None
                        self._reset_work_item_outputs_for_rework(task)
                        await transition_work_item_from_task(
                            self.store, task,
                            target_status_or_phase=TaskStatus.PENDING,
                            reason="pre_delivery_rework_withheld",
                        )
                    await self._append_progress(task, "Final delivery withheld pending executive-directed rework.")
                    await self.save_task(task)
                    await self._emit_progress(
                        f"[Company:{self._projection_id_for_task(task)}] executive withheld delivery for rework",
                        task_id=task.id,
                    )
                    return
        if self._requires_user_feedback(task):
            await self._append_progress(task, "Awaiting user feedback before learning from this delivery.")
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.AWAITING_HUMAN,
                reason="awaiting_user_feedback_on_delivery",
            )
            await self._mark_run_awaiting_owner_from_delivery(
                task,
                summary=str(task.result.get("content", "") if isinstance(task.result, dict) else task.result or "").strip(),
            )
            await self.save_task(task)
            await self._save_feedback_checkpoint(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting user feedback",
                task_id=task.id,
            )
            return
        await self.save_task(task)

    def _requires_user_feedback(self, task: Task) -> bool:
        if getattr(task, "metadata", {}).get("execution_mode") != "company_mode":
            return False
        return (
            self._metadata_flag_true(getattr(task, "metadata", {}).get("requires_user_feedback", False))
            and self._is_final_human_acceptance_task(task)
        )

    def _infer_verdict(self, content: str, gate: WorkItemGatePolicy) -> str:
        # 1) Try structured JSON verdict first
        structured = self._extract_structured_verdict(content)
        if structured:
            return structured

        # 2) Keyword matching (improved)
        lower = content.lower()
        reject_terms = [
            "reject", "rejected", "needs changes", "needs change",
            "not approved", "blocked", "fail", "not ready",
            "rework needed", "does not meet", "insufficient",
        ]
        approve_terms = [
            "approve", "approved", "accepted", "pass",
            "looks good", "ready to proceed", "meets criteria",
        ]
        strict_gate = bool(gate.metadata.get("strict_gate_inference", False)) or (
            gate.instructions and "strict" in gate.instructions.lower()
        )
        has_reject = any(term in lower for term in reject_terms)
        has_approve = any(term in lower for term in approve_terms)

        if has_reject and has_approve:
            # Ambiguous: in strict mode reject wins; otherwise last-position wins
            if strict_gate:
                return "reject"
            last_reject = max((lower.rfind(t) for t in reject_terms if t in lower), default=-1)
            last_approve = max((lower.rfind(t) for t in approve_terms if t in lower), default=-1)
            return "reject" if last_reject > last_approve else "approve"
        if has_reject:
            return "reject"
        if has_approve:
            return "approve"
        # No keywords: strict mode defaults to reject, otherwise approve
        return "reject" if strict_gate else "approve"

    @staticmethod
    def _extract_structured_verdict(content: str) -> str | None:
        """Extract verdict from a JSON object embedded in the content."""
        import json as _json

        start = content.find("{")
        while start != -1:
            end = content.find("}", start)
            if end == -1:
                break
            try:
                data = _json.loads(content[start : end + 1])
                raw = str(
                    data.get("verdict") or data.get("decision") or data.get("status") or ""
                ).lower().strip()
                if raw in ("reject", "rejected", "fail", "failed", "rework"):
                    return "reject"
                if raw in ("approve", "approved", "pass", "passed", "accept", "accepted"):
                    return "approve"
            except (ValueError, AttributeError):
                pass
            start = content.find("{", start + 1)
        return None

    def _gate_from_metadata(self, gate_data: dict[str, Any] | None) -> WorkItemGatePolicy | None:
        if not gate_data:
            return None
        gate_metadata = dict(gate_data.get("metadata", {}) or {})
        rework_projection_id = str(
            gate_data.get("rework_projection_id")
            or gate_metadata.get("rework_projection_id")
            or ""
        ).strip()
        gate = WorkItemGatePolicy(
            gate_type=str(gate_data.get("type", "review") or "review"),
            instructions=gate_data.get("instructions", ""),
            reviewer_role=gate_data.get("reviewer_role"),
            requires_human=bool(gate_data.get("requires_human", False)),
            on_reject=gate_data.get("on_reject", "halt"),
            rework_projection_id=rework_projection_id or None,
            max_retries=int(gate_data.get("max_retries", 1)),
            metadata=gate_metadata,
        )
        if rework_projection_id:
            mark_gate_rework_projection(gate, rework_projection_id)
        return gate

    def _deps_done(self, task: Task, tasks: list[Task]) -> bool:
        task_by_id = {t.id: t for t in tasks}
        missing = [dep for dep in task.dependencies if dep not in task_by_id]
        if missing:
            raise ValueError(f"Task `{task.title}` has unresolved dependencies: {', '.join(missing)}")
        return all(task_by_id[dep].status == TaskStatus.DONE for dep in task.dependencies)

    def _deps_satisfied(self, task: Task, tasks: list[Task]) -> bool:
        """Flexible dependency check supporting hard/soft/info classification.

        - hard: dep must be DONE (used for synthesize/deliver waiting on children)
        - soft: dep must be at least RUNNING or DONE (default — maximises parallelism)
        - info: never blocks (awareness only, e.g. parallel siblings)
        """
        task_by_id = {t.id: t for t in tasks}
        missing = [dep for dep in task.dependencies if dep not in task_by_id]
        if missing:
            raise ValueError(f"Task `{task.title}` has unresolved dependencies: {', '.join(missing)}")
        dep_classes = task.metadata.get("dependency_classes") or {}
        for dep_id in task.dependencies:
            dep_task = task_by_id[dep_id]
            dep_class = dep_classes.get(dep_id) or self._infer_dependency_class(task, dep_task)
            if dep_class == "hard" and dep_task.status != TaskStatus.DONE:
                return False
            if dep_class == "soft" and dep_task.status not in {TaskStatus.DONE, TaskStatus.RUNNING}:
                return False
            # "info" deps never block
        return True

    @staticmethod
    def _infer_dependency_class(task: Task, dep_task: Task) -> str:
        """Infer dependency class from work-item metadata when not explicitly annotated.

        Returns "hard", "soft", or "info".
        """
        task_meta = task.metadata or {}
        dep_meta = dep_task.metadata or {}
        # Siblings in the same parallel group → info (awareness, not blocking)
        task_pg = str(task_meta.get("work_item_parallel_group", "") or "").strip()
        dep_pg = str(dep_meta.get("work_item_parallel_group", "") or "").strip()
        if task_pg and task_pg == dep_pg:
            return "info"
        # Synthesize/deliver work items hard-depend on their direct children
        task_kind = work_item_turn_type_from_metadata(task_meta, fallback="")
        if task_kind in {"synthesize", "deliver"}:
            dep_manager = str(dep_meta.get("manager_role_id", "") or "").strip()
            task_role = str(task.assigned_to or task_meta.get("work_item_role_id", "") or "").strip()
            if dep_manager == task_role:
                return "hard"
        # Default: soft — allow tasks to proceed with partial upstream info
        return "soft"

    def _select_authoritative_delivery_task(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> Task | None:
        projection_order = plan.projection_order_map()
        candidates = [
            task
            for task in tasks
            if task.status == TaskStatus.DONE
            and (
                bool(task.metadata.get("authoritative_output", False))
                or bool(task.metadata.get("user_visible", False))
                or self._turn_type_for_task(task) in {"deliver", "aggregate"}
            )
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda task: (
                projection_order.get(self._projection_id_for_task(task), len(projection_order)),
                task.created_at,
                task.id,
            ),
        )[-1]

    def _build_authoritative_delivery_package(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        delivery_task: Task,
    ) -> dict[str, Any]:
        delivery_outputs = self._work_item_output_metadata_for_task(delivery_task)
        package = self._normalize_delivery_package(
            delivery_outputs.get("delivery_package")
            or (delivery_task.context_snapshot or {}).get("delivery_package")
            or delivery_task.metadata.get("delivery_package")
        )
        if not package:
            package = {
                "executive_summary": str(
                    (delivery_task.result or {}).get("content")
                    or delivery_outputs.get("work_item_summary", "")
                    or delivery_outputs.get("work_item_summary_for_downstream", "")
                    or delivery_task.metadata.get("work_item_summary", "")
                    or delivery_task.title
                ).strip(),
                "delivered_items": [],
                "artifact_manifest": [],
                "constraints": [],
                "risks": [],
                "open_issues": [],
                "next_steps": [],
                "source_projection_refs": [],
            }
        package.setdefault("delivered_items", [])
        package.setdefault("artifact_manifest", [])
        package.setdefault("constraints", [])
        package.setdefault("risks", [])
        package.setdefault("open_issues", [])
        package.setdefault("next_steps", [])
        package.setdefault("source_projection_refs", [])
        package["role_task_map"] = self._build_role_task_map(tasks)

        for task in tasks:
            projection_id = self._projection_id_for_task(task)
            output_metadata = self._work_item_output_metadata_for_task(task)
            package["source_projection_refs"].append(
                {
                    "projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "title": task.title,
                    "status": task.status.value,
                    "assigned_to": self._role_id_for_task(task),
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": dict(task.metadata.get("employee_assignment", {}) or {}),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "gate_harness_status": str(task.metadata.get("gate_harness_status", "") or "").strip(),
                    "constraints": list(task.metadata.get("gate_harness_constraints", []) or []),
                }
            )
            if task.id == delivery_task.id:
                continue
            summary = str(
                output_metadata.get("work_item_summary", "")
                or output_metadata.get("work_item_summary_for_downstream", "")
                or ""
            ).strip()
            if summary and task.status == TaskStatus.DONE:
                package["delivered_items"].append({
                    "work_item_title": task.title,
                    "status": task.status.value,
                    "summary": summary,
                })
            for item in list(output_metadata.get("work_item_artifact_index", []) or []):
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                entry["work_item_title"] = task.title
                package["artifact_manifest"].append(entry)
            for risk in list(output_metadata.get("risks", []) or []):
                text = str(risk or "").strip()
                if text:
                    package["risks"].append(text)
            for constraint in list(task.metadata.get("gate_harness_constraints", []) or []):
                text = str(constraint or "").strip()
                if text:
                    package["constraints"].append(f"{task.title}: {text}")
            review_verdict = self._normalize_review_verdict(
                output_metadata.get("structured_review_verdict")
                or task.metadata.get("structured_review_verdict")
            )
            if review_verdict.get("label") == "reject":
                package["open_issues"].append(
                    f"{task.title}: {str(review_verdict.get('summary', '') or 'review rejected').strip()}"
                )
            if task.status in {
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.AWAITING_PEER,
                *list(_REVIEW_WAITING_STATUSES),
            }:
                package["open_issues"].append(f"{task.title}: {task.status.value}")

        if not str(package.get("executive_summary", "") or "").strip():
            package["executive_summary"] = delivery_task.title
        return package

    def _render_delivery_package(self, plan: CompanyWorkItemRuntimePlan, package: dict[str, Any]) -> str:
        parts = [f"## Company Work-Item Runtime: {plan.profile}", "## Final Delivery"]
        summary = str(package.get("executive_summary", "") or "").strip()
        if summary:
            parts.append(summary)

        delivered_items = list(package.get("delivered_items", []) or [])
        if delivered_items:
            lines = []
            for item in delivered_items:
                if isinstance(item, dict):
                    work_item_title = str(item.get("work_item_title", "") or item.get("title", "") or "Work item").strip()
                    summary_text = str(item.get("summary", "") or "").strip()
                    if summary_text:
                        lines.append(f"- {work_item_title}: {summary_text}")
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"- {text}")
            if lines:
                parts.append("### Delivered Items")
                parts.append("\n".join(lines[:8]))

        artifact_manifest = list(package.get("artifact_manifest", []) or [])
        if artifact_manifest:
            lines = []
            for item in artifact_manifest:
                if not isinstance(item, dict):
                    continue
                work_item_title = str(item.get("work_item_title", "") or "").strip()
                label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
                value = str(item.get("value", "") or "").strip()
                text = f"- {work_item_title}: {label}" if work_item_title else f"- {label}"
                if value:
                    text = f"{text} -> {value}"
                lines.append(text)
            if lines:
                parts.append("### Evidence")
                parts.append("\n".join(lines[:12]))

        constraints = [str(item).strip() for item in list(package.get("constraints", []) or []) if str(item).strip()]
        if constraints:
            parts.append("### Constraints")
            parts.append("\n".join(f"- {item}" for item in constraints[:8]))

        open_issues = [str(item).strip() for item in list(package.get("open_issues", []) or []) if str(item).strip()]
        if open_issues:
            parts.append("### Open Issues")
            parts.append("\n".join(f"- {item}" for item in open_issues[:8]))

        risks = [str(item).strip() for item in list(package.get("risks", []) or []) if str(item).strip()]
        if risks:
            parts.append("### Risks")
            parts.append("\n".join(f"- {item}" for item in risks[:8]))
        return "\n\n".join(parts)

    def _summarize_results(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
        if self._uses_multi_team_org_runtime(tasks, plan):
            return self._summarize_multi_team_org_results(tasks)
        delivery_task = self._select_authoritative_delivery_task(plan, tasks)
        if delivery_task is not None:
            package = self._build_authoritative_delivery_package(plan, tasks, delivery_task)
            return self._render_delivery_package(plan, package)
        parts = [f"## Company Work-Item Runtime: {plan.profile}"]
        for task in tasks:
            status = task.status.value
            content = ""
            if task.result and task.result.get("content"):
                content = task.result["content"].strip()
            parts.append(f"### {task.title} [{status}]")
            if content:
                parts.append(content)
        return "\n\n".join(parts)

    @staticmethod
    def _task_has_delegated_downstream_work(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        if bool(metadata.get("manager_board_mutation_performed", False)):
            return True
        if bool(metadata.get("delegated_children_pending", False)):
            return True
        for key in (
            "delegation_wait_for_work_item_ids",
            "delegation_pending_work_item_ids",
            "manager_board_modified_work_item_ids",
            "manager_board_deleted_work_item_ids",
        ):
            if [
                str(item).strip()
                for item in list(metadata.get(key, []) or [])
                if str(item).strip()
            ]:
                return True
        return False

    async def _pending_checkpoint_task_ids(self, project_id: str) -> set[str]:
        """Task ids referenced by pending execution checkpoints for the project."""
        get_pending = getattr(self.store, "get_pending_checkpoints", None)
        if not callable(get_pending) or not self._store_is_ready(self.store):
            return set()
        try:
            rows = await get_pending(project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                "_pending_checkpoint_task_ids: pending checkpoint load failed"
            )
            return set()
        task_ids: set[str] = set()
        for row in rows or []:
            payload = dict(getattr(row, "payload", {}) or {})
            task_id = str(
                getattr(row, "task_id", "")
                or payload.get("waiting_task_id", "")
                or payload.get("task_id", "")
                or ""
            ).strip()
            if task_id:
                task_ids.add(task_id)
        return task_ids

    def _summarize_human_parked_exit(self, tasks: list[Task], human_waiting: list[Task]) -> str:
        lines = [
            "## Organization Runtime Parked",
            "All remaining work items are waiting on human input. "
            "Answer the pending approval/review card(s) and the run will continue from where it stopped.",
            "",
        ]
        for task in sorted(human_waiting, key=lambda item: (item.created_at, item.id)):
            status = str(task.status.value if isinstance(task.status, TaskStatus) else task.status)
            lines.append(f"- {task.title}: {status}")
        return "\n".join(lines)

    def _summarize_multi_team_org_results(self, tasks: list[Task]) -> str:
        if not tasks:
            return "No organization runtime tasks were found."
        ordered = sorted(tasks, key=lambda item: (item.created_at, item.id))
        def _task_content(task: Task) -> str:
            return str(((task.result or {}).get("content", "") if isinstance(task.result, dict) else "") or "").strip()

        final_delivery_task = next(
            (
                task for task in sorted(ordered, key=lambda item: (item.created_at, item.id), reverse=True)
                if task.status == TaskStatus.DONE
                and _task_content(task)
                and str((task.metadata or {}).get("feedback_scope", "") or "").strip().lower() == "final"
                and turn_type_for_task(task, fallback="") == "deliver"
                and bool((task.metadata or {}).get("authoritative_output", False))
            ),
            None,
        )
        if final_delivery_task is not None:
            return _task_content(final_delivery_task)

        root_task = next(
            (
                task for task in ordered
                if bool((task.metadata or {}).get("authoritative_output", False))
                and str((task.metadata or {}).get("execution_model", "") or "").strip() == "multi_team_org"
                and not self._task_has_delegated_downstream_work(task)
            ),
            ordered[0],
        )
        root_content = _task_content(root_task)
        if root_task.status == TaskStatus.DONE and root_content:
            return root_content
        lines = ["## Organization Runtime Snapshot"]
        for task in ordered:
            status = str(task.status.value if isinstance(task.status, TaskStatus) else task.status)
            lines.append(f"- {task.title}: {status} [{status}]")
        if root_content:
            lines.append("")
            lines.append("## Latest Root Summary")
            lines.append(root_content)
        return "\n".join(lines)

    def _progress_identity_for_task_id(self, task_id: str | None) -> tuple[str, str]:
        tid = str(task_id or "").strip()
        if not tid:
            return "", ""
        for task in list(self._active_tasks or []):
            if str(getattr(task, "id", "") or "").strip() != tid:
                continue
            role_id = self._role_id_for_task(task)
            role_name = self._role_name_for_task(task)
            return role_id, role_name
        return "", ""

    async def _emit_progress(self, message: str, *, task_id: str | None = None) -> None:
        logger.info(message)
        if self.progress_callback:
            kwargs: dict[str, Any] = {"task_id": task_id}
            role_id, role_name = self._progress_identity_for_task_id(task_id)
            if role_id:
                kwargs["agent_role_id"] = role_id
            if role_name:
                kwargs["agent_name"] = role_name
            try:
                await self.progress_callback(message, **kwargs)
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                try:
                    await self.progress_callback(message, task_id=task_id)
                except TypeError as fallback_exc:
                    if "unexpected keyword argument" not in str(fallback_exc):
                        raise
                    await self.progress_callback(message)

    def _validate_dependencies(self, tasks: list[Task]) -> None:
        valid_ids = {task.id for task in tasks}
        for task in tasks:
            missing = [dep for dep in task.dependencies if dep not in valid_ids]
            if missing:
                raise ValueError(f"Task `{task.title}` references unknown dependencies: {', '.join(missing)}")

    def _extract_bullets(self, content: str, prefixes: tuple[str, ...]) -> list[str]:
        items: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip().lstrip("-").strip()
            lower = line.lower()
            if any(lower.startswith(prefix) for prefix in prefixes):
                items.append(line)
        return items

    def _merge_unique_items(self, existing: list[str], new_items: list[str]) -> list[str]:
        merged = list(existing)
        for item in new_items:
            value = item.strip()
            if value and value not in merged:
                merged.append(value)
        return merged[:12]
