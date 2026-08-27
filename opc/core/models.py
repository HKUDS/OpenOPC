"""Core data models for the OPC system."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

RoleRuntimeStatus = Literal["idle", "running", "blocked"]
ROLE_RUNTIME_STATUSES: frozenset[str] = frozenset({"idle", "running", "blocked"})


def normalize_role_runtime_status(
    status: Any,
    focused_work_item_id: Any = "",
    *,
    default: RoleRuntimeStatus = "idle",
) -> RoleRuntimeStatus:
    """Collapse company-mode role/member/seat runtime status to three states.

    Legacy values are normalized at the boundary so the runtime does not keep
    carrying ``cold`` / ``reserved`` / ``draining`` style states internally.
    """
    text = str(status or "").strip().lower()
    focused = bool(str(focused_work_item_id or "").strip())
    if text == "idle":
        return "idle"
    if text == "running":
        return "running" if focused else "idle"
    if text == "blocked":
        return "blocked"
    if text == "":
        return "blocked" if focused else default
    if text == "cold":
        return "idle"
    if text in {"reserved", "booting", "draining"}:
        return "running" if focused else "idle"
    if text in {"dead", "handoff_pending"}:
        return "blocked"
    if focused:
        return "blocked"
    return default

class ExecutionMode(str, Enum):
    TASK_MODE = "task_mode"
    COMPANY_MODE = "company_mode"
    PROJECT_MODE = "task_mode"
    SINGLE_AGENT = "task_mode"
    MULTI_AGENT = "company_mode"

    @classmethod
    def _missing_(cls, value: object) -> "ExecutionMode" | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if normalized in {"project_mode", "task_mode", "project", "task"}:
            return cls.TASK_MODE
        if normalized in {"company_mode", "company"}:
            return cls.COMPANY_MODE
        return None


class CompanyProfile(str, Enum):
    CORPORATE = "corporate"
    CUSTOM = "custom"


class GoalLevel(str, Enum):
    COMPANY = "company"
    DEPARTMENT = "department"
    TEAM = "team"
    TASK = "task"


class GoalStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class WorkItemExecutionStrategy(str, Enum):
    AUTO = "auto"
    NATIVE = "native"
    EXTERNAL = "external"
    MIXED = "mixed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    IDLE = "idle"
    BLOCKED = "blocked"
    AWAITING_PEER = "awaiting_peer"
    AWAITING_MANAGER_REVIEW = "awaiting_manager_review"
    AWAITING_HUMAN = "awaiting_human"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Phase(str, Enum):
    """Single authoritative state of a delegation work item.

    Replaces the previous mixture of `status` + 5 metadata sub-state fields.
    Pure-function projections (kanban_column / is_runnable / effective_owner /
    verdict / task_status_for_phase) and the transition table live in
    `opc.layer2_organization.phase`.
    """

    # ─── kanban column: todo (not yet started) ────────────────────────────
    QUEUED = "queued"                               # manager has not released
    READY = "ready"                                 # released, dispatchable
    READY_FOR_REWORK = "ready_for_rework"           # returned by reviewer
    WAITING_DEPENDENCIES = "waiting_dependencies"   # upstream not done

    # ─── kanban column: in_progress (worker holds the card) ───────────────
    RUNNING = "running"
    WAITING_FOR_PEER = "waiting_for_peer"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    PAUSED = "paused"                               # soft-interrupted
    NEEDS_ATTENTION = "needs_attention"             # worker flagged blocker

    # ─── kanban column: in_review (manager holds the card) ────────────────
    AWAITING_MANAGER_REVIEW = "awaiting_manager_review"
    AWAITING_HUMAN = "awaiting_human"

    # ─── kanban column: done (terminal) ───────────────────────────────────
    APPROVED = "approved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    BLOCKED = "blocked"
    ERROR = "error"


class MessageUrgency(str, Enum):
    BLOCKING = "blocking"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class MessageStatus(str, Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    REPLIED = "replied"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AgentEndpointType(str, Enum):
    COMPANY_ROLE = "company_role"
    NATIVE_SUBAGENT = "native_subagent"
    EXTERNAL_AGENT = "external_agent"


class CommsTransportKind(str, Enum):
    DM = "dm"
    BROADCAST = "broadcast"
    MEETING = "meeting"
    SYSTEM = "system"


class CommsSemanticType(str, Enum):
    # Values observed in runtime or kept for the legacy single-team path that
    # a future C-batch cleanup will evaluate. 10 other values
    # (ASSIGNMENT, DEPENDENCY_REQUEST, DEPENDENCY_REPLY, APPROVAL_REPLY,
    # CROSS_TEAM_REQUEST, CROSS_TEAM_REPLY, PERMISSION_REQUEST,
    # PERMISSION_RESPONSE, PLAN_APPROVAL_REQUEST, PLAN_APPROVAL_RESPONSE)
    # were removed: zero code writes them and no runtime message ever
    # carries them — they were only referenced in classification set
    # literals (themselves dead code paths) and worker_envelope look-up
    # tables. See plans/task-cleanup-dead-comms.md.
    WORK_UPDATE = "work_update"
    IDLE_NOTIFICATION = "idle_notification"
    BLOCKED_ON_DECISION = "blocked_on_decision"
    HANDOFF_READY = "handoff_ready"
    WORK_ITEM_RESULT = "work_item_result"
    BLOCKER = "blocker"
    APPROVAL_REQUEST = "approval_request"
    COMPLETION = "completion"
    STATUS_DIGEST = "status_digest"


class CommsState(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class MeetingStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DECIDED = "decided"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalAction(str, Enum):
    AUTO_APPROVE = "auto_approve"
    ESCALATE = "escalate"
    REJECT = "reject"
    REQUIRE_INPUT = "require_input"


class PermissionResolution(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionScope(str, Enum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class ReorgScope(str, Enum):
    TASK_ADJUSTMENT = "task_adjustment"
    ORG_MUTATION = "org_mutation"


class ReorgRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReorgProposalStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    APPLYING = "applying"
    DENIED = "denied"
    APPLIED = "applied"
    FAILED = "failed"
    EXECUTION_UNKNOWN = "execution_unknown"
    CANCELLED = "cancelled"


class ReorgEventKind(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    DENIED = "denied"
    APPLIED = "applied"
    MIGRATED = "migrated"
    AUTO_TASK_ADJUSTED = "auto_task_adjusted"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Layer 0: Messages
# ---------------------------------------------------------------------------

@dataclass
class UserMessage:
    channel: str
    user_id: str
    content: str
    attachments: list[Any] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemMessage:
    channel: str
    user_id: str
    session_id: str
    content: str
    message_type: Literal["reply", "escalation", "progress", "suggestion"] = "reply"
    actions: list[dict] = field(default_factory=list)
    task_ref: str | None = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Layer 1: Mode Selection (replaces old RouterDecision)
# ---------------------------------------------------------------------------

@dataclass
class ModeSelection:
    """Lightweight mode selection — the user explicitly chooses task or
    company mode; no LLM-based routing is needed."""
    mode: ExecutionMode = ExecutionMode.TASK_MODE
    org_id: str | None = None
    preferred_agent: str | None = None
    domains: list[str] = field(default_factory=list)
    company_profile: str | None = None
    sub_tasks: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


RouterDecision = ModeSelection


# ---------------------------------------------------------------------------
# Layer 2: Tasks & Organization
# ---------------------------------------------------------------------------

# Boundary note:
# - In task mode, Task is the user-visible business unit.
# - In company mode, Task is only the runtime execution envelope used by
#   agent/tool/session infrastructure. The company business identity is
#   DelegationWorkItem.work_item_id, and its business state is
#   DelegationWorkItem.phase.
@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str | None = None
    parent_session_id: str | None = None
    title: str = ""
    description: str = ""
    assigned_to: str = ""
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5
    dependencies: list[str] = field(default_factory=list)
    execution_lock: bool = False
    context_snapshot: dict = field(default_factory=dict)
    assigned_external_agent: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    deadline: datetime | None = None
    result: dict | None = None
    parent_id: str | None = None
    project_id: str = "default"
    tags: list[str] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    metadata: dict = field(default_factory=dict)
    org_id: str | None = None
    goal_id: str | None = None
    checkout_run_id: str | None = None
    execution_locked_at: datetime | None = None
    linked_work_item_id: str = field(default="", repr=False, compare=False)


@dataclass
class AdaptiveRoleProfile:
    label: str = ""
    facets: list[str] = field(default_factory=list)
    authority_scope: list[str] = field(default_factory=list)
    execution_bias: str = "balanced"
    review_bias: str = "balanced"
    collaboration_style: str = "async"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class AdaptiveWorkItemProfile:
    turn_kind: str = "execute"
    dependency_class: str = "hard"
    blocked_by_projection_ids: list[str] = field(default_factory=list)
    blocked_by_signals: list[str] = field(default_factory=list)
    required_artifacts: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    gate_owner_role_id: str = ""
    soft_release_allowed: bool = False
    confidence: float = 0.0


@dataclass
class AdaptiveSignalSpec:
    name: str
    owner_role_id: str = ""
    required: bool = True
    strict: bool = False
    satisfied: bool = False
    evidence: list[str] = field(default_factory=list)


@dataclass
class CoordinationSpec:
    version: int = 1
    inference_mode: str = "heuristic"
    fallback_mode: str = "conservative"
    role_profile: AdaptiveRoleProfile = field(default_factory=AdaptiveRoleProfile)
    work_item_profile: AdaptiveWorkItemProfile = field(default_factory=AdaptiveWorkItemProfile)
    signals: list[AdaptiveSignalSpec] = field(default_factory=list)
    emitted_signals: list[str] = field(default_factory=list)
    hard_dependency_work_item_ids: list[str] = field(default_factory=list)
    soft_dependency_work_item_ids: list[str] = field(default_factory=list)
    normalized_state: str = "planned"
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)


@dataclass
class DelegationRun:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str = ""
    company_profile: str = CompanyProfile.CORPORATE.value
    execution_model: str = "actor_runtime"
    final_decider_role_id: str = ""
    top_level_role_ids: list[str] = field(default_factory=list)
    status: str = "pending"
    lifecycle_status: str = "active"
    current_revision: int = 1
    latest_deliverable_summary: str = ""
    recovery_pointer: dict[str, Any] = field(default_factory=dict)
    project_dossier: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Durable single-controller lease.  These fields live on the run row so
    # controller ownership has exactly the same project/run/root scope as the
    # workflow it fences; normal DelegationRun saves deliberately do not own
    # them (the Store CAS methods below do).
    controller_owner_token: str = ""
    controller_lease_generation: int = 0
    controller_lease_acquired_at: datetime | None = None
    controller_lease_heartbeat_at: datetime | None = None
    controller_lease_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class DelegationCell:
    cell_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    manager_role_id: str = ""
    member_role_ids: list[str] = field(default_factory=list)
    status: str = "idle"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


# Company-mode boundary note:
# DelegationWorkItem is the user/business work unit. The structured
# work_item_runtime_links table owns the runtime Task projection relation.
@dataclass
class DelegationWorkItem:
    work_item_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    cell_id: str = ""
    team_instance_id: str = ""
    team_id: str = ""
    role_id: str = ""
    seat_id: str = ""
    seat_state_id: str = ""
    role_runtime_session_id: str = ""
    parent_work_item_id: str | None = None
    source_role_id: str | None = None
    source_seat_id: str | None = None
    title: str = ""
    summary: str = ""
    kind: str = "execute"
    projection_id: str = ""
    phase: Phase = Phase.READY
    batch_id: str = ""
    batch_index: int = 0
    deliverable_summary: str = ""
    blocked_reason: str = ""
    handoff_status: str = "pending"
    continuation_source: str = ""
    manager_role_id: str = ""
    manager_seat_id: str = ""
    claimed_by_role_runtime_session_id: str = ""
    claimed_by_seat_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self) -> None:
        if not isinstance(self.phase, Phase):
            try:
                self.phase = Phase(str(self.phase or Phase.READY.value))
            except ValueError:
                self.phase = Phase.READY
        self.metadata = dict(self.metadata or {})


@dataclass
class DelegationEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    work_item_id: str | None = None
    cell_id: str | None = None
    role_id: str | None = None
    event_type: str = "created"
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class RoleRuntimeSession:
    role_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    project_id: str = "default"
    team_instance_id: str = ""
    team_id: str = ""
    role_id: str = ""
    seat_id: str = ""
    seat_state_id: str = ""
    employee_id: str = ""
    focused_work_item_id: str = ""
    background_work_item_ids: list[str] = field(default_factory=list)
    manager_role_ids: list[str] = field(default_factory=list)
    manager_seat_ids: list[str] = field(default_factory=list)
    seat_ids: list[str] = field(default_factory=list)
    adapter_session_state: dict[str, Any] = field(default_factory=dict)
    inbox_state: dict[str, Any] = field(default_factory=dict)
    memory_slices_by_work_item: dict[str, list[str]] = field(default_factory=dict)
    resume_state: dict[str, Any] = field(default_factory=dict)
    current_work_item: dict[str, Any] = field(default_factory=dict)
    latest_notification: dict[str, Any] = field(default_factory=dict)
    manager_digest: dict[str, Any] = field(default_factory=dict)
    status: str = "idle"
    # Fix 5 PR3: FIFO queue of work_item_ids awaiting this role's session.
    # A role executes one work item at a time (``focused_work_item_id``);
    # new runnable work for the same role is appended here by the
    # ``enqueue_session_work_on_runnable_hook``. When focus clears, the
    # ``clear_session_focus_on_terminal_hook`` pops the head and signals
    # the dispatcher to pick it up. Gated by
    # ``OrgConfig.role_serial_queue_enabled`` (on by default for company mode).
    pending_work_item_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


DelegationRoleSession = RoleRuntimeSession


@dataclass
class CompanyMemberSession:
    member_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role_session_id: str = ""
    team_instance_id: str = ""
    team_id: str = ""
    role_id: str = ""
    seat_id: str = ""
    seat_state_id: str = ""
    employee_id: str = ""
    status: str = "idle"
    resident_status: str = "idle"
    current_task_id: str = ""
    focused_work_item_id: str = ""
    background_work_item_ids: list[str] = field(default_factory=list)
    inbox_cursor: int = 0
    working_memory: list[str] = field(default_factory=list)
    memory_slices_by_work_item: dict[str, list[str]] = field(default_factory=dict)
    resume_state: dict[str, Any] = field(default_factory=dict)
    adapter_session_state: dict[str, Any] = field(default_factory=dict)
    pending_inbox: list[dict[str, Any]] = field(default_factory=list)
    queued_inbox: list[dict[str, Any]] = field(default_factory=list)
    actionable_chat: list[dict[str, Any]] = field(default_factory=list)
    protocol_backlog: list[dict[str, Any]] = field(default_factory=list)
    notification_backlog: list[dict[str, Any]] = field(default_factory=list)
    actionable_inbox_count: int = 0
    protocol_backlog_count: int = 0
    notification_backlog_count: int = 0
    latest_notification: dict[str, Any] = field(default_factory=dict)
    manager_role_id: str = ""
    manager_role_ids: list[str] = field(default_factory=list)
    inbox_state: dict[str, Any] = field(default_factory=dict)
    current_turn_mode: str = ""
    current_assignment: dict[str, Any] = field(default_factory=dict)
    current_work_item: dict[str, Any] = field(default_factory=dict)
    manager_digest: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class TeamInstance:
    team_instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    project_id: str = "default"
    team_id: str = ""
    session_id: str = ""
    status: str = "pending"
    seat_ids: list[str] = field(default_factory=list)
    role_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class SeatState:
    seat_state_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    team_instance_id: str = ""
    run_id: str = ""
    project_id: str = "default"
    team_id: str = ""
    seat_id: str = ""
    role_id: str = ""
    employee_id: str = ""
    member_session_id: str = ""
    role_runtime_session_id: str = ""
    status: str = "idle"
    resident_status: str = "idle"
    current_task_id: str = ""
    current_work_item_id: str = ""
    manager_role_id: str = ""
    manager_seat_id: str = ""
    manager_role_ids: list[str] = field(default_factory=list)
    manager_seat_ids: list[str] = field(default_factory=list)
    inbox_state: dict[str, Any] = field(default_factory=dict)
    resume_state: dict[str, Any] = field(default_factory=dict)
    current_work_item: dict[str, Any] = field(default_factory=dict)
    latest_notification: dict[str, Any] = field(default_factory=dict)
    manager_digest: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class StructuredReviewVerdict:
    label: Literal["approve", "reject"] = "reject"
    summary: str = ""
    blocking_issues: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)


@dataclass
class ArtifactContract:
    summary: str = ""
    write_scope: str = ""
    expected_artifacts: list[str] = field(default_factory=list)
    downstream_consumer: list[str] = field(default_factory=list)
    allowed_collaboration_targets: list[str] = field(default_factory=list)
    status: str = "pending"
    issues: list[str] = field(default_factory=list)


@dataclass
class VerificationEvidence:
    status: str = "missing"
    verdict: str = ""
    summary: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str = ""


@dataclass
class EnvironmentManifest:
    """Structured record of environment state produced by a setup work item."""

    platform: str = ""
    tools_installed: list[dict[str, Any]] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    runtime_type: str = "native"
    runtime_path: str = ""
    activate_command: str = ""
    shell_prefix: str = ""
    shell_prefix_win: str = ""
    gpu_available: bool = False
    gpu_info: str = ""
    verification_checks: list[dict[str, Any]] = field(default_factory=list)
    verification_checks_win: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


@dataclass
class WorkspaceManifest:
    """Structured record of the shared workspace prepared for a runtime."""

    root_path: str = ""
    manifest_path: str = ""
    reserved_paths: dict[str, str] = field(default_factory=dict)
    status: str = "ready"
    notes: list[str] = field(default_factory=list)


@dataclass
class DataAcquisitionReport:
    """Structured readiness report for task-critical external inputs."""

    status: str = "missing_critical"
    designated_input_dir: str = ""
    required_inputs: list[str] = field(default_factory=list)
    present_inputs: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    attempted_sources: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    prepared_assets: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)
    acquisition_attempted: bool = False
    report_path: str = ""
    log_path: str = ""
    source_candidates_path: str = ""
    download_manifest_path: str = ""
    provenance_summary: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class RecruitmentNeed:
    """One staffing decision the recruiter must make for a company role.

    The recruiter runs once before execution enters the org, so each need
    corresponds to a single role in the topology (not a fixed runtime step).
    """
    role_id: str
    role_name: str = ""
    role_responsibility: str = ""
    request_text: str = ""
    domains: list[str] = field(default_factory=list)
    existing_employee_ids: list[str] = field(default_factory=list)


@dataclass
class RecruitmentCandidateRecommendation:
    template_id: str
    template_name: str
    category: str = ""
    domains: list[str] = field(default_factory=list)
    prompt_ref: str = ""
    preferred_external_agent: str | None = None
    source_path: str = ""
    rationale: str = ""
    proposed_employee_name: str = ""
    proposed_employee_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentEmployeeRecommendation:
    employee_id: str
    employee_name: str
    role_id: str
    category: str = ""
    domains: list[str] = field(default_factory=list)
    learned_skill_refs: list[str] = field(default_factory=list)
    experience_score: float = 0.0
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentProposal:
    role_id: str
    status: Literal["existing_staff", "proposed_hire", "fallback_role_only", "direct_role_execution"] = "fallback_role_only"
    rationale: str = ""
    role_labels: list[str] = field(default_factory=list)
    candidate: RecruitmentCandidateRecommendation | None = None
    existing_employee: RecruitmentEmployeeRecommendation | None = None
    existing_employee_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecruitmentPlan:
    company_profile: str = CompanyProfile.CORPORATE.value
    proposals: list[RecruitmentProposal] = field(default_factory=list)
    recruiter_feedback: list[str] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReorgRoleChange:
    action: Literal["add", "remove", "replace", "update"] = "update"
    role_id: str = ""
    replacement_role_id: str | None = None
    role: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class ReorgTaskAdjustment:
    task_id: str = ""
    action: Literal["reassign", "reprioritize", "update_description", "append_acceptance_criteria", "request_review"] = "reassign"
    new_role_id: str | None = None
    priority: int | None = None
    description_append: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReorgChangeSet:
    role_changes: list[ReorgRoleChange] = field(default_factory=list)
    task_adjustments: list[ReorgTaskAdjustment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReorgMigrationPlan:
    affected_task_ids: list[str] = field(default_factory=list)
    affected_checkpoint_ids: list[str] = field(default_factory=list)
    affected_handoff_ids: list[str] = field(default_factory=list)
    role_mapping: dict[str, str] = field(default_factory=dict)
    invalidated_waits: list[str] = field(default_factory=list)
    migration_notes: list[str] = field(default_factory=list)
    compatibility_warnings: list[str] = field(default_factory=list)
    rollback_snapshot_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrgSnapshot:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    org_version: int = 1
    runtime_topology_version: int = 1
    company_name: str = ""
    topology: str = ""
    roles: list[dict[str, Any]] = field(default_factory=list)
    company_profile: str = CompanyProfile.CORPORATE.value
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ReorgProposal:
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str | None = None
    task_id: str | None = None
    initiated_by: str = "owner"
    source_role_id: str = ""
    scope: ReorgScope = ReorgScope.ORG_MUTATION
    risk_level: ReorgRiskLevel = ReorgRiskLevel.MEDIUM
    status: ReorgProposalStatus = ReorgProposalStatus.PROPOSED
    title: str = ""
    summary: str = ""
    rationale: str = ""
    user_confirmation_required: bool = True
    old_org_version: int = 1
    new_org_version: int = 1
    old_runtime_topology_version: int = 1
    new_runtime_topology_version: int = 1
    changeset: ReorgChangeSet = field(default_factory=ReorgChangeSet)
    migration_plan: ReorgMigrationPlan = field(default_factory=ReorgMigrationPlan)
    impact_summary: dict[str, Any] = field(default_factory=dict)
    approval_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class ReorgApplicationClaimReceipt:
    outcome: str
    operation_token: str
    proposal: ReorgProposal | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome == "acquired"


@dataclass(frozen=True)
class ReorgApplicationFinishReceipt:
    outcome: str
    operation_token: str
    proposal: ReorgProposal | None = None

    @property
    def applied(self) -> bool:
        return self.outcome in {"applied", "duplicate"}


@dataclass(frozen=True)
class ReorgDecisionReceipt:
    outcome: str
    proposal: ReorgProposal | None = None

    @property
    def applied(self) -> bool:
        return self.outcome in {"applied", "duplicate"}


@dataclass
class TaskResult:
    status: TaskStatus
    content: str = ""
    artifacts: dict = field(default_factory=dict)
    escalation: dict | None = None
    cost: float = 0.0
    token_usage: dict = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    action: ApprovalAction
    risk_level: RiskLevel
    rationale: str
    confidence: float = 0.0
    requires_user_input: bool = False
    policy_source: str = "heuristic"
    suggested_response: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimePermissionDecision:
    resolution: PermissionResolution
    scope: PermissionScope = PermissionScope.ONCE
    risk_level: RiskLevel = RiskLevel.LOW
    rationale: str = ""
    source: str = "runtime"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelCapabilitySet:
    model: str
    supports_streaming: bool = True
    supports_tool_calling: bool = True
    supports_streaming_tool_calls: bool = True
    supports_thinking: bool = False
    supports_multimodal: bool = False
    supports_documents: bool = False
    supports_video: bool = False
    provider_family: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeLLMEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ExternalSession:
    agent_type: str
    project_id: str = "default"
    session_id: str = ""
    opc_session_id: str | None = None
    task_id: str | None = None
    workspace_path: str = ""
    run_mode: str = "batch"
    status: str = "unknown"
    metadata: dict = field(default_factory=dict)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class ExecutionCheckpoint:
    checkpoint_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str | None = None
    checkpoint_type: str = ""
    status: str = "pending"
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


CheckpointDecisionOutcome = Literal[
    "accepted",
    "duplicate",
    "conflict",
    "invalid_state",
    "not_found",
    "empty_decision",
    "option_id_required",
    "invalid_option_id",
    "input_required",
    "invalid_user_input_answers",
    "invalid_gate_action",
    "invalid_delivery_action",
    "feedback_text_required",
    "invalid_staffing_action",
    "invalid_staffing_selections",
    "invalid_recruitment_action",
    "invalid_recruitment_role_agents",
    "invalid_reorg_action",
    "invalid_failure_review_action",
    "unsupported_checkpoint_type",
]


@dataclass(frozen=True)
class ExecutionCheckpointDecisionReceipt:
    """Durable result of submitting one human decision.

    ``duplicate`` is a successful idempotent replay of the exact same decision
    hash and value; transport request ids may differ after reconnect.  A
    ``conflict`` means a different answer already owns the checkpoint and must
    never be silently replaced.
    """

    outcome: CheckpointDecisionOutcome
    checkpoint: ExecutionCheckpoint | None = None
    live_waiter_notified: bool = False

    @property
    def acknowledged(self) -> bool:
        return self.outcome in {"accepted", "duplicate"}


CheckpointClaimOutcome = Literal[
    "claimed",
    "reclaimed",
    "renewed",
    "started",
    "duplicate",
    "busy",
    "controller_busy",
    "invalid_state",
    "not_found",
]


@dataclass(frozen=True)
class ExecutionCheckpointClaimReceipt:
    """Result of acquiring the lease for an answered checkpoint."""

    outcome: CheckpointClaimOutcome
    checkpoint: ExecutionCheckpoint | None = None
    claim_id: str = ""

    @property
    def acquired(self) -> bool:
        return self.outcome in {
            "claimed",
            "reclaimed",
            "renewed",
            "started",
            "duplicate",
        }


CheckpointConsumptionOutcome = Literal[
    "finished",
    "released",
    "duplicate",
    "conflict",
    "invalid_state",
    "not_found",
]


@dataclass(frozen=True)
class ExecutionCheckpointConsumptionReceipt:
    """Result of finishing or releasing a checkpoint consumption lease."""

    outcome: CheckpointConsumptionOutcome
    checkpoint: ExecutionCheckpoint | None = None

    @property
    def applied(self) -> bool:
        return self.outcome in {"finished", "released", "duplicate"}


RuntimeToolContinuationClaimOutcome = Literal[
    "claimed",
    "reclaimed",
    "renewed",
    "duplicate",
    "busy",
    "conflict",
    "not_found",
]


@dataclass(frozen=True)
class RuntimeToolContinuationClaimReceipt:
    """Durable single-flight lease for one task/runtime continuation."""

    outcome: RuntimeToolContinuationClaimOutcome
    claim_id: str = ""
    consumer_id: str = ""
    request_generation: int = 0
    lease_expires_at: str = ""

    @property
    def acquired(self) -> bool:
        return self.outcome in {"claimed", "reclaimed", "renewed", "duplicate"}


RuntimeToolContinuationFinishOutcome = Literal[
    "finished",
    "more_work",
    "conflict",
    "not_found",
]


@dataclass(frozen=True)
class RuntimeToolContinuationFinishReceipt:
    """Result of releasing a continuation runner after draining permits."""

    outcome: RuntimeToolContinuationFinishOutcome
    request_generation: int = 0

    @property
    def applied(self) -> bool:
        return self.outcome in {"finished", "more_work"}


# ---------------------------------------------------------------------------
# Layer 2: Inter-Agent Communication
# ---------------------------------------------------------------------------


@dataclass
class AgentEndpointRef:
    endpoint_id: str
    endpoint_type: AgentEndpointType = AgentEndpointType.COMPANY_ROLE
    role_id: str = ""
    task_id: str = ""
    projection_id: str = ""
    session_id: str = ""


@dataclass
class CommsEnvelope:
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    project_id: str = "default"
    task_id: str = ""
    projection_id: str = ""
    transport_kind: CommsTransportKind = CommsTransportKind.DM
    semantic_type: CommsSemanticType = CommsSemanticType.WORK_UPDATE
    state: CommsState = CommsState.OPEN
    from_endpoint: AgentEndpointRef = field(default_factory=lambda: AgentEndpointRef(endpoint_id=""))
    to_endpoint: AgentEndpointRef = field(default_factory=lambda: AgentEndpointRef(endpoint_id=""))
    subject: str = ""
    content: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)
    transport_metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class ResidentAssignmentEnvelope:
    assignment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    member_session_id: str = ""
    team_instance_id: str = ""
    team_id: str = ""
    seat_id: str = ""
    seat_state_id: str = ""
    role_runtime_session_id: str = ""
    work_item_projection_id: str = ""
    work_item_turn_type: str = ""
    role_id: str = ""
    employee_id: str = ""
    manager_role_id: str = ""
    task_id: str = ""
    session_id: str = ""
    write_scope: str = ""
    ownership_contract: str = ""
    dependency_snapshot: list[str] = field(default_factory=list)
    pending_inbox: list[dict[str, Any]] = field(default_factory=list)
    actionable_chat: list[dict[str, Any]] = field(default_factory=list)
    protocol_backlog: list[dict[str, Any]] = field(default_factory=list)
    latest_notification: dict[str, Any] = field(default_factory=dict)
    resident_status: str = "idle"
    team_memory_digest: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class AgentMessage:
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    msg_type: Literal[
        "question", "inform", "request_review", "flag_issue",
        "decision_needed", "ack", "answer"
    ] = "inform"
    from_agent: str = ""
    to_agents: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    context_ref: str | None = None
    urgency: MessageUrgency = MessageUrgency.NORMAL
    reply_needed: bool = False
    requires_ack: bool = False
    timeout_action: str | None = None
    reply_to_msg_id: str | None = None
    task_id: str | None = None
    status: MessageStatus = MessageStatus.SENT
    timestamp: datetime = field(default_factory=datetime.now)
    processed_at: datetime | None = None
    transport_kind: CommsTransportKind = CommsTransportKind.DM
    semantic_type: CommsSemanticType = CommsSemanticType.WORK_UPDATE
    comms_state: CommsState = CommsState.OPEN
    correlation_id: str = ""
    refs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructuredHandoff:
    handoff_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    objective: str = ""
    completed_work: str = ""
    artifacts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    summary: str = ""
    source_task_id: str | None = None
    source_projection_id: str | None = None
    source_projection_title: str | None = None
    source_work_item_id: str | None = None
    target_work_item_id: str | None = None


@dataclass
class MeetingRoom:
    room_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    topic: str = ""
    participants: list[str] = field(default_factory=list)
    shared_context: str = ""
    agenda: list[str] = field(default_factory=list)
    max_rounds: int = 5
    decision_owner: str = "coordinator"
    status: MeetingStatus = MeetingStatus.OPEN
    decision_method: str = ""
    current_round: int = 0
    pending_participants: list[str] = field(default_factory=list)
    consensus: dict[str, Any] = field(default_factory=dict)
    outcome: dict | None = None
    transcript: list[dict] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    last_activity_at: datetime = field(default_factory=datetime.now)
    deadline_at: datetime | None = None


@dataclass
class WorkItemDecisionRecord:
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    task_id: str | None = None
    role_id: str = ""
    projection_id: str = ""
    category: str = "general"
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ArtifactRecord:
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    task_id: str | None = None
    projection_id: str = ""
    role_id: str = ""
    name: str = ""
    artifact_type: str = "generic"
    location: str = ""
    status: str = "active"
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class RoleMemoryRecord:
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    role_id: str = ""
    scope: str = "project"
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class HandoffRecord:
    handoff_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str | None = None
    task_id: str | None = None
    from_role: str = ""
    to_role: str = ""
    source_projection_id: str = ""
    target_projection_id: str = ""
    source_work_item_id: str = ""
    target_work_item_id: str = ""
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    requires_ack: bool = False
    status: str = "sent"
    received_at: datetime | None = None
    acked_at: datetime | None = None
    accepted_at: datetime | None = None
    rejected_at: datetime | None = None
    response_summary: str = ""
    ack_message_id: str | None = None
    response_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ReorgEventRecord:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    proposal_id: str = ""
    project_id: str = "default"
    event_kind: ReorgEventKind = ReorgEventKind.PROPOSED
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionRecord:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    parent_session_id: str | None = None
    title: str = ""
    mode: str = "primary"
    status: str = "active"
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionMessageRecord:
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    role: str = "user"
    task_id: str | None = None
    agent_id: str | None = None
    parent_message_id: str | None = None
    summary_flag: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionPartRecord:
    part_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message_id: str = ""
    session_id: str = ""
    part_type: str = "text"
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionCompactionRecord:
    compaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    compaction_message_id: str = ""
    source_boundary_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionMemorySnapshotRecord:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str = ""
    summary_message_id: str = ""
    source_boundary_message_id: str = ""
    summary_text: str = ""
    memory_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class AgentCompactionRecord:
    compaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str = ""
    employee_id: str = ""
    role_id: str = ""
    compaction_message_id: str = ""
    source_boundary_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class AgentMemorySnapshotRecord:
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str = ""
    employee_id: str = ""
    role_id: str = ""
    memory_scope: str = "session"
    memory_kind: str = "process"
    summary_message_id: str = ""
    source_boundary_message_id: str = ""
    summary_text: str = ""
    memory_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class SessionLinkRecord:
    link_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str = "default"
    session_id: str = ""
    linked_session_id: str | None = None
    task_id: str | None = None
    link_type: str = "child_session"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Layer 3: Agent
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    role_id: str
    name: str
    responsibility: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: str | None = None
    reports_to: str = "owner"
    icon: str | None = None
    can_spawn: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    preferred_external_agent: str | None = None
    prompt_refs: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    handoff_template_ref: str | None = None
    memory_policy_ref: str | None = None
    artifact_contract_ref: str | None = None
    runtime_policy: dict[str, Any] = field(default_factory=dict)
    org_id: str | None = None
    budget_monthly_cents: int = 0
    spent_monthly_cents: int = 0
    heartbeat_enabled: bool = False
    heartbeat_interval_sec: int = 300
    last_heartbeat_at: datetime | None = None
    capabilities: str = ""


# ---------------------------------------------------------------------------
# Layer 4: Organization Entities
# ---------------------------------------------------------------------------

@dataclass
class Organization:
    org_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    status: str = "active"
    company_profile: str = CompanyProfile.CORPORATE.value
    budget_monthly_cents: int = 0
    spent_monthly_cents: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class Goal:
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    org_id: str = ""
    parent_id: str | None = None
    owner_agent_id: str | None = None
    level: GoalLevel = GoalLevel.TASK
    title: str = ""
    description: str = ""
    status: GoalStatus = GoalStatus.ACTIVE
    priority: int = 5
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class CostEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    org_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OrgAgent:
    """Persistent agent membership within an organization."""
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    org_id: str = ""
    role_id: str = ""
    name: str = ""
    reports_to: str | None = None
    budget_monthly_cents: int = 0
    spent_monthly_cents: int = 0
    heartbeat_enabled: bool = False
    heartbeat_interval_sec: int = 300
    last_heartbeat_at: datetime | None = None
    status: str = "idle"
    capabilities: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Layer 6: Events
# ---------------------------------------------------------------------------

@dataclass
class OPCEvent:
    event_type: str
    payload: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
