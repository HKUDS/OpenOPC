"""OPC Engine — the central orchestrator that wires all layers together."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import shutil
import time
import uuid
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.attachment_store import AttachmentRef, AttachmentStore
from opc.core.attachment_content import can_extract_text, extract_attachment_text
from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.company_tools import (
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from opc.core.config import (
    DEFAULT_ORGANIZATION_ID,
    OPCConfig,
    company_org_path,
    get_opc_home,
    get_project_workplace,
)
from opc.core.events import EventBus
from opc.core.models import (
    ApprovalAction,
    ApprovalDecision,
    DelegationCell,
    DelegationEvent,
    DelegationRoleSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExecutionMode,
    MeetingRoom,
    ModeSelection,
    OPCEvent,
    Phase,
    ReorgChangeSet,
    ReorgProposal,
    ReorgProposalStatus,
    RiskLevel,
    RouterDecision,
    SeatState,
    SessionLinkRecord,
    SystemMessage,
    Task,
    TaskResult,
    TaskStatus, UserMessage,
    TeamInstance,
    WorkItemExecutionStrategy,
    CompanyProfile,
)
from opc.database.store import OPCStore
from opc.llm.provider import LLMProvider
from opc.layer0_interaction.message_bus import MessageBus
from opc.channels import ChannelManager
from opc.layer1_perception.context_assembler import ContextAssembler, ExternalContextLayers
from opc.layer1_perception.context_loader import ContextLoader
from opc.layer1_perception.task_router import TaskRouter
from opc.layer2_organization.org_engine import (
    OrgEngine,
    TASK_MODE_COMPANY_ONLY_TOOLS,
)
from opc.layer2_organization.task_graph import TaskGraphScheduler
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.escalation import EscalationEngine
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.collaboration_policy import ownership_guard_violation
from opc.layer2_organization.secretary import SecretaryService
from opc.layer2_organization.company_mode import (
    CompanyExecutorDriverOwnership,
    CompanyRuntimeSpec,
    CompanyRuntimeSpecBuilder,
    CompanyWorkItemExecutor,
    deserialize_company_runtime_spec,
    deserialize_company_work_item_runtime_plan,
    serialize_company_runtime_spec,
    serialize_company_work_item_runtime_plan,
    serialized_company_plan_from_metadata,
)
from opc.layer2_organization.company_runtime import canonical_role_session_id
from opc.layer2_organization.company_runtime_identity import (
    build_company_runtime_identity_index,
    is_company_runtime_task,
    is_pure_company_ui_anchor,
    load_company_runtime_identity_index,
)
from opc.layer2_organization.metadata_ownership import (
    build_work_item_owner_execution_copy,
)
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    InvalidPhaseTransition,
    task_status_for_phase,
)
from opc.layer2_organization.prompt_contract import (
    has_prompt_contract,
    is_report_prompt_turn,
    make_prompt_contract,
    prompt_contract_from_work_item,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.reactivation_sweeper import CommsReactivationSweeper
from opc.layer2_organization.session_scoping import (
    external_resume_allowed_for_scope,
    is_top_level_company_session,
    task_session_scope_id,
)
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata
from opc.layer2_organization.seat_executor import EngineSeatExecutor
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
)
from opc.layer2_organization.work_item_runtime_invariants import (
    validate_work_item_runtime_projection,
)
from opc.layer2_organization.work_item_identity import (
    canonical_work_item_turn_type_for_kind,
    mark_projected_work_item_task,
    mark_work_item_projection,
    projection_id_for_task,
    projection_id_for_work_item,
    rework_projection_id_for_gate,
    result_delivery_identity_payload_for_task,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_identity_payload_from_metadata,
    work_item_projection_id_from_metadata,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer2_organization.recruiter import (
    apply_recruitment_role_agent_overrides,
    CompanyRecruiter,
    build_fallback_role_ids,
    build_recruitment_feedback,
    build_recruitment_plan_from_payload,
    build_staffing_experience_modes,
    build_staffing_overrides,
    extract_recruitment_role_agent_overrides,
    normalize_recruitment_agent_choice,
    recruitment_plan_requires_confirmation,
    resolve_effective_execution_agent,
    serialize_recruitment_plan,
)
from opc.layer2_organization.reorg_manager import ReorgManager
from opc.layer2_organization.talent_market import TalentMarket
from opc.layer3_agent.native_agent import NativeAgent
from opc.layer3_agent.prompt_harness.builder import _final_decider_role_id, _memory_skill_user_facing
from opc.layer3_agent.adapters.registry import AdapterRegistry
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer3_agent.external_session_identity import (
    external_session_matches_provider_token,
    external_session_status_allows_resume,
    is_provider_session_token,
    provider_token_from_external_session,
    select_best_external_resume_session,
)
from opc.layer4_tools.registry import ToolRegistry, ToolDefinition
from opc.layer4_tools.shell import create_shell_tool, create_shell_tools
from opc.layer4_tools.file_ops import create_file_tools
from opc.layer4_tools.user_input import create_user_input_tool
from opc.layer4_tools.web_search import create_web_tools
from opc.layer4_tools.browser import browser_snapshot, create_browser_tools
from opc.layer4_tools.git_ops import create_git_tools
from opc.layer4_tools.python_exec import create_python_tool
from opc.layer4_tools.collaboration import (
    build_external_cli_tool_contract_lines,
    create_collaboration_tools,
)
from opc.layer4_tools.todo import create_todo_tools
from opc.layer4_tools.agent_runtime import create_agent_runtime_tools
from opc.layer2_organization.heartbeat import HeartbeatScheduler
from opc.mcp_client import MCPManager
from opc.layer5_memory.memory_manager import MemoryManager


_REVIEW_WAITING_STATUSES = {
    TaskStatus.AWAITING_MANAGER_REVIEW,
    TaskStatus.AWAITING_HUMAN,
    TaskStatus.AWAITING_REVIEW,
}
_WAITING_TASK_STATUSES = {
    *_REVIEW_WAITING_STATUSES,
    TaskStatus.AWAITING_PEER,
}
_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES = {
    "company_runtime_suspended",
    "company_runtime_interrupted",
}
_COMPANY_RUNTIME_CONTROL_METADATA_KEYS = (
    "dispatch_hold",
    "company_runtime_stop_state",
    "company_runtime_stop_intent_id",
    "company_runtime_stop_marked_at",
    "company_runtime_suspend_checkpoint_type",
    "company_runtime_suspended_at",
)
from opc.layer5_memory.history_compactor import HistoryCompactor
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager
from opc.layer5_memory.capability_manager import CapabilityManager
from opc.layer5_memory.skill_library import SkillLibrary
from opc.layer6_observability.cost_tracker import CostTracker
from opc.layer6_observability.opc_logger import setup_logging

AGENT_SELECTION_PROMPT = """\
You are the task execution-agent selector for an AI orchestration system.

Given a concrete task, its assigned role, execution metadata, and the currently available
external agents, choose the best execution agent for THIS task only.

Return strict JSON:
{
  "selected_agent": "native" | "claude_code" | "cursor" | "codex" | "opencode",
  "reasoning": "short explanation"
}

Rules:
- Respect hard constraints:
  - If execution_strategy is "native", return "native".
  - If execution_strategy is "external", choose one available external agent.
  - If no external agents are available, return "native".
- For "auto" and "mixed", decide based on the task's real needs:
  - role responsibility and work-item turn type
  - subtask objective and expected artifacts
  - whether the work is tool-heavy, coding-heavy, file/system-heavy, or better suited for direct native reasoning
  - any preferred external agent from role or work-item metadata
- Use "native" for lighter planning/review/approval/conversational tasks when external delegation is not clearly beneficial.
- Use an external agent for substantial implementation, CLI-heavy, repo/file-editing, automation, or multi-step execution tasks when that is a better fit.
- Choose only from the provided available agents. If a preferred external agent is unavailable, choose the best available alternative or "native".
- If retry_feedback is present, fix the exact issue it describes and return a corrected answer.
- Return JSON only. No markdown fences, no extra text.
"""

COMPANY_FEEDBACK_ATTRIBUTION_PROMPT = """\
You evaluate user feedback for a completed company work-item runtime.

Return strict JSON:
{
  "overall_outcome": "success" | "partial_success" | "failure",
  "summary": "short summary grounded in the user's feedback",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "employees": [
    {
      "employee_id": "employee id",
      "outcome": "success" | "partial_success" | "failure",
      "reason": "why this employee received this outcome",
      "strengths": ["..."],
      "weaknesses": ["..."]
    }
  ]
}

Rules:
- User feedback is the source of truth. Do not upgrade a negative user judgment into success.
- Use partial_success when the user gives mixed or qualified feedback.
- Attribute strengths and weaknesses only to employees that actually appear in the runtime data.
- Keep the result concise and actionable.
- Return JSON only.
"""


class ExternalRecruiterLLMAdapter:
    """Expose an external task agent through the recruiter's simple_chat contract."""

    def __init__(self, engine: "OPCEngine", agent_name: str) -> None:
        self.engine = engine
        self.agent_name = agent_name

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        engine = self.engine
        if not engine.adapter_registry or not engine.external_broker:
            raise RuntimeError("External recruitment agent infrastructure is not initialized.")
        adapter = engine.adapter_registry.get(self.agent_name)
        if adapter is None:
            raise RuntimeError(f"External recruitment agent `{self.agent_name}` is not available.")

        description = "\n\n".join(
            part
            for part in (
                "You are acting as OPC's recruitment planner.",
                "Return JSON only.",
                "Do not edit files.",
                "Do not run tools unless necessary.",
                "Follow the system contract exactly.",
                f"SYSTEM:\n{system or ''}",
                f"PAYLOAD:\n{prompt}",
            )
            if str(part).strip()
        )
        task = Task(
            title=f"Recruitment planning via {self.agent_name}",
            description=description,
            assigned_to="recruiter",
            status=TaskStatus.PENDING,
            assigned_external_agent=self.agent_name,
            project_id=engine.project_id or "default",
            metadata={
                "mode": "recruitment",
                "task_type": task_type or "quick_tasks",
                "recruitment_planning": True,
                "selected_execution_agent": self.agent_name,
                "selected_execution_agent_source": "recruitment_user_override",
                "execution_agent_locked": True,
                "preferred_external_agent": self.agent_name,
            },
        )
        run_adapter, _ = await engine._configure_external_adapter_for_task(task, adapter)
        adapter_config = getattr(run_adapter, "config", None)
        if adapter_config is not None:
            cloned_config = (
                adapter_config.model_copy(deep=True)
                if hasattr(adapter_config, "model_copy")
                else adapter_config
            )
            if hasattr(cloned_config, "session_mode"):
                cloned_config.session_mode = "new"
            if hasattr(cloned_config, "session_id"):
                cloned_config.session_id = ""
            run_adapter = run_adapter.__class__(config=cloned_config)

        workspace = engine._resolve_external_workspace(task)
        prepared_task = copy.deepcopy(task)
        result = await engine.external_broker.run(
            adapter=run_adapter,
            task=task,
            workspace_path=workspace,
            prepared_task=prepared_task,
        )
        if result.status != TaskStatus.DONE:
            detail = str(result.content or "").strip()
            raise RuntimeError(detail or f"External recruitment agent `{self.agent_name}` did not complete.")
        return str(result.content or "").strip()


class OPCEngine:
    """Central orchestrator — initializes and coordinates all layers."""

    def __init__(
        self,
        config: OPCConfig | None = None,
        opc_home: Path | None = None,
        project_id: str | None = None,
        store: OPCStore | None = None,
        owns_store: bool = True,
        run_startup_reconcile: bool = True,
        active_task_run_registry: ActiveTaskRunRegistry | None = None,
        owns_active_task_run_registry: bool | None = None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_runtime_event: Callable[[OPCEvent], Coroutine[Any, Any, None]] | None = None,
        on_escalation: Callable[[str, list[dict]], Coroutine[Any, Any, str | None]] | None = None,
    ) -> None:
        self.config = config or OPCConfig()
        self.opc_home = opc_home or get_opc_home()
        self.project_id = project_id
        self.on_progress = on_progress
        self.on_runtime_event = on_runtime_event
        self.on_escalation = on_escalation
        # Called with (parent_task_id, [child_task_id, ...]) when company mode creates work items.
        self.on_company_runtime_children: Callable[[str, list[str]], None] | None = None
        self.on_company_kanban_callback_factory: Callable[[Any], Callable[[], Coroutine[Any, Any, None]]] | None = None

        # Core infrastructure
        self.event_bus = EventBus()
        self.store: OPCStore | None = store
        self._owns_store = bool(owns_store)
        self._run_startup_reconcile = bool(run_startup_reconcile)
        if active_task_run_registry is None:
            self._active_task_run_registry = ActiveTaskRunRegistry()
            default_registry_owner = True
        else:
            self._active_task_run_registry = active_task_run_registry
            default_registry_owner = False
        self._owns_active_task_run_registry = (
            default_registry_owner
            if owns_active_task_run_registry is None
            else bool(owns_active_task_run_registry)
        )
        self.llm: LLMProvider | None = None
        self.attachment_store: AttachmentStore | None = None

        # Layers
        self.message_bus = MessageBus()
        self.tool_registry = ToolRegistry()
        self.memory: MemoryManager | None = None
        self.history_compactor: HistoryCompactor | None = None
        self.preferences: PreferenceManager | None = None
        self.secretary_policies: SecretaryPolicyManager | None = None
        self.skills: SkillLibrary | None = None
        self.capability_manager: CapabilityManager | None = None
        self.adapter_registry: AdapterRegistry | None = None
        self.org_engine: OrgEngine | None = None
        self.task_scheduler: TaskGraphScheduler | None = None
        self.escalation: EscalationEngine | None = None
        self.communication: CommunicationManager | None = None
        self.company_runtime_spec_builder: CompanyRuntimeSpecBuilder | None = None
        self.company_recruiter: CompanyRecruiter | None = None
        self.company_executor: CompanyWorkItemExecutor | None = None
        self.reorg_manager: ReorgManager | None = None
        self.cost_tracker: CostTracker | None = None
        self.approval_engine: ApprovalEngine | None = None
        self.external_broker: ExternalAgentBroker | None = None
        self.secretary: SecretaryService | None = None
        self.mcp_manager: MCPManager | None = None
        self.channel_manager: ChannelManager | None = None
        self.talent_market: TalentMarket | None = None
        self.heartbeat_scheduler: HeartbeatScheduler | None = None
        self.comms_reactivation_sweeper: CommsReactivationSweeper | None = None

        # Perception layer
        self.context_loader: ContextLoader | None = None
        self.context_assembler: ContextAssembler | None = None
        self.task_router: TaskRouter | None = None

        self._initialized = False
        self._shutting_down = False
        self._runtime_config_signature: tuple[tuple[str, float], ...] | None = None
        self._project_delegate_lock: asyncio.Lock | None = None
        self._project_engine_delegates: dict[str, OPCEngine] = {}

    def bind_store(self, store: OPCStore, *, owns_store: bool | None = None) -> None:
        """Rebind the active store across components that cache it."""
        self.store = store
        if owns_store is not None:
            self._owns_store = bool(owns_store)

        if self.memory:
            self.memory.store = store
        if self.history_compactor:
            self.history_compactor.store = store
        if self.org_engine:
            self.org_engine.store = store
        if self.task_scheduler:
            self.task_scheduler.store = store
        if self.communication:
            self.communication.store = store
        if self.context_assembler:
            self.context_assembler.store = store
        if self.approval_engine:
            self.approval_engine.store = store
        if self.external_broker:
            self.external_broker.store = store
        if self.secretary:
            self.secretary.store = store
        if self.reorg_manager:
            self.reorg_manager.store = store
        if self.cost_tracker:
            self.cost_tracker.store = store
        if self.context_loader:
            self.context_loader.store = store
        if self.heartbeat_scheduler:
            self.heartbeat_scheduler.store = store
        if self.comms_reactivation_sweeper:
            self.comms_reactivation_sweeper.store = store
        if self.company_executor:
            self.company_executor.store = store
            self.company_executor.save_task = store.save_task
            self.company_executor.save_runtime_session = store.save_runtime_session
            if getattr(self.company_executor, "runtime", None):
                self.company_executor.runtime.store = store
                self.company_executor.runtime.save_runtime_session = store.save_runtime_session

    def _runtime_config_signature_for(self, config_dir: Path) -> tuple[tuple[str, float], ...]:
        tracked = (
            "system_config.yaml",
            "agent_config.yaml",
            "company_corporate_config.yaml",
        )
        signature: list[tuple[str, float]] = []
        for name in tracked:
            path = config_dir / name
            mtime = path.stat().st_mtime if path.exists() else -1.0
            signature.append((name, mtime))
        corporate_path = company_org_path(config_dir, DEFAULT_ORGANIZATION_ID)
        corporate_mtime = corporate_path.stat().st_mtime if corporate_path.exists() else -1.0
        signature.append((f"company_orgs/{corporate_path.name}", corporate_mtime))
        return tuple(signature)

    def _task_mode_tool_names(self) -> list[str]:
        names = [tool.name for tool in self.tool_registry.list_tools()]
        filtered: list[str] = []
        seen: set[str] = set()
        for name in names:
            normalized = str(name or "").strip()
            if not normalized or normalized in TASK_MODE_COMPANY_ONLY_TOOLS or normalized in seen:
                continue
            filtered.append(normalized)
            seen.add(normalized)
        return filtered

    async def _refresh_runtime_config_from_disk(self) -> None:
        config_dir = self.opc_home / "config"
        if not config_dir.is_dir():
            return

        signature = self._runtime_config_signature_for(config_dir)
        if signature == self._runtime_config_signature:
            return

        loaded = OPCConfig.load(config_dir)
        self.config.system = loaded.system
        self.config.agents = loaded.agents
        self.config.autonomy = loaded.autonomy
        self._runtime_config_signature = signature

        active_org_id = None
        if str(getattr(self.config.org, "company_profile", "") or "").strip() == "custom":
            try:
                from opc.core.org_config import read_org_index, apply_org_config_payload_to_config, load_org_config_payload
                active_org_id = read_org_index(config_dir)
            except Exception:
                pass
        if active_org_id:
            try:
                payload, path = load_org_config_payload(config_dir, active_org_id)
                refreshed = apply_org_config_payload_to_config(loaded, payload, source_path=path)
                self.config.org = refreshed.org
            except Exception:
                self.config.org = loaded.org
        else:
            self.config.org = loaded.org

        if self.approval_engine:
            self.approval_engine.config = self.config.autonomy
        if self.company_executor:
            self.company_executor.work_item_timeout = self.config.system.task_mode.sub_agent_timeout_sec
        if self.adapter_registry:
            self.adapter_registry.config = self.config.agents
            await self.adapter_registry.initialize()
        if self.org_engine:
            self.org_engine.config = self.config
            self.org_engine.reload_from_config()
            self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())

        logger.info(f"Reloaded runtime config from {config_dir}")

    async def initialize(self) -> None:
        """Initialize all layers and subsystems."""
        if self._initialized:
            return

        self.opc_home.mkdir(parents=True, exist_ok=True)
        setup_logging(self.opc_home / "logs", self.config.system.log_level)
        logger.info("Initializing OPC Engine...")

        # Database
        if self.store is None:
            db_path = self.opc_home / "global.db"
            if self.project_id:
                proj_dir = self.opc_home / "projects" / self.project_id
                proj_dir.mkdir(parents=True, exist_ok=True)
                get_project_workplace(self.project_id).mkdir(parents=True, exist_ok=True)
                db_path = proj_dir / "tasks.db"
            self.store = OPCStore(db_path)
            self._owns_store = True
            # Fix 5 PR3: surface the serial-queue feature flag on the store
            # so phase hooks (which only receive ``store``) can gate their
            # queue-aware branches without reaching back to the engine.
            self.store.role_serial_queue_enabled = bool(
                getattr(self.config.org, "role_serial_queue_enabled", True)
            )
            await self.store.initialize()
        else:
            self.store.role_serial_queue_enabled = bool(
                getattr(self.config.org, "role_serial_queue_enabled", True)
            )
            if self._owns_store:
                await self.store.initialize()
        self._ensure_attachment_store()

        # LLM
        self.llm = LLMProvider(self.config.llm, opc_home=self.opc_home)

        # Layer 4: Tools
        self._register_tools()

        # MCP server connections
        self.mcp_manager = MCPManager()
        await self._register_mcp_tools()

        # Layer 5: Memory
        self.memory = MemoryManager(self.opc_home, self.project_id, store=self.store)
        self.memory.markdown_store.ensure_memory_file(None, "# Global Memory")
        if self.project_id:
            self.memory.markdown_store.ensure_memory_file(self.project_id, f"# Project Memory ({self.project_id})")
        self.history_compactor = HistoryCompactor(
            llm=self.llm,
            store=self.store,
            memory_manager=self.memory,
            compression_threshold=self.config.system.context_compression_threshold,
        )
        self.memory.set_history_compactor(self.history_compactor)
        self.preferences = PreferenceManager(self.opc_home)
        self.secretary_policies = SecretaryPolicyManager(self.opc_home)
        self.skills = SkillLibrary(self.opc_home)
        self.skills.load_all(self.project_id)

        # Layer 3: External Agents
        self.adapter_registry = AdapterRegistry(self.config.agents)
        await self.adapter_registry.initialize()
        self.capability_manager = CapabilityManager(
            config=self.config.capabilities,
            skill_library=self.skills,
            tool_registry=self.tool_registry,
            adapter_registry=self.adapter_registry,
        )

        # Layer 2: Organization
        self.org_engine = OrgEngine(self.config, self.opc_home, store=self.store)
        self.talent_market = TalentMarket(self.opc_home, self.config)
        self.task_scheduler = TaskGraphScheduler(self.store, self.event_bus)
        self.escalation = EscalationEngine(
            self.event_bus,
            timeout_seconds=self.config.system.escalation_timeout_seconds,
            user_reply_callback=self.on_escalation,
        )
        self.communication = CommunicationManager(self.store, self.event_bus, self.llm, self.org_engine)
        await self.communication.rehydrate_queues()
        self.channel_manager = ChannelManager(self.config, self.message_bus)
        self.context_assembler = ContextAssembler(
            memory=self.memory,
            store=self.store,
            communication=self.communication,
        )
        self.approval_engine = ApprovalEngine(
            llm=self.llm,
            store=self.store,
            preferences=self.preferences,
            memory=self.memory,
            escalation=self.escalation,
            config=self.config.autonomy,
            secretary_policies=self.secretary_policies,
        )
        self.external_broker = ExternalAgentBroker(
            self.store,
            self.approval_engine,
            task_preparer=self._build_external_agent_task,
            communication=self.communication,
            org_engine=self.org_engine,
        )
        self.secretary = SecretaryService(
            llm=self.llm,
            store=self.store,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            policies=self.secretary_policies,
        )
        self.company_runtime_spec_builder = CompanyRuntimeSpecBuilder(self.org_engine, self.llm)
        self.company_recruiter = CompanyRecruiter(self.llm, self.org_engine, self.talent_market)
        self.company_executor = CompanyWorkItemExecutor(
            org_engine=self.org_engine,
            communication=self.communication,
            approval_engine=self.approval_engine,
            memory=self.memory,
            llm=self.llm,
            store=self.store,
            execute_task=self._execute_task,
            seat_executor=EngineSeatExecutor(self),
            save_task=self.store.save_task,
            save_runtime_session=self.store.save_runtime_session,
            progress_callback=self.on_progress,
            checkpoint_callback=self._save_execution_checkpoint,
            agent_selector=self._assign_task_execution_agent,
            emit_runtime_event=self._emit_company_runtime_event,
            work_item_timeout=self.config.system.task_mode.sub_agent_timeout_sec,
            role_prompt_runner=self._run_role_prompt_via_task_execution_agent,
            active_task_run_registry=self._active_task_run_registry,
        )
        self.communication.set_meeting_turn_runner(self._run_meeting_turn)
        self.reorg_manager = ReorgManager(
            store=self.store,
            org_engine=self.org_engine,
            approval_engine=self.approval_engine,
            communication=self.communication,
            progress_callback=self.on_progress,
        )
        if self.communication is not None:
            self.communication.task_adjustment_suggester = self.reorg_manager.suggest_task_adjustment
        self.tool_registry.set_approval_callback(self._tool_approval_callback)
        self._register_collaboration_tools()

        # Layer 6: Cost tracking
        self.cost_tracker = CostTracker(self.store, self.event_bus)

        # Layer 1: Perception
        self.context_loader = ContextLoader(
            self.memory,
            self.preferences,
            self.secretary_policies,
            self.skills,
            self.capability_manager,
            self.adapter_registry,
            self.org_engine,
            self.store,
        )
        self.task_router = TaskRouter(self.llm)

        self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())

        # Heartbeat scheduler for company-mode agent autonomy
        heartbeat_cfg = self.config.system.heartbeat if hasattr(self.config.system, "heartbeat") else None
        self.heartbeat_scheduler = HeartbeatScheduler(
            store=self.store,
            org_engine=self.org_engine,
            execute_task_fn=self._execute_task,
            interval_sec=getattr(heartbeat_cfg, "default_interval_sec", 300) if heartbeat_cfg else 300,
            max_concurrent_runs=getattr(heartbeat_cfg, "max_concurrent_runs", 1) if heartbeat_cfg else 1,
            communication=self.communication,
        )

        # Comms reactivation sweeper — periodically re-opens DONE tasks whose
        # role received actionable mail after they finished. This closes the
        # gap between the end-of-turn ``_reactivate_for_unread_mail`` hook
        # (which only fires at task boundaries) and the arrival of a blocking
        # DM from a peer/manager. It replaces the old LLM "impersonation
        # reply" fallback so the recipient role's own agent answers instead.
        self.comms_reactivation_sweeper = CommsReactivationSweeper(
            store=self.store,
            project_id_getter=lambda: self.project_id or "default",
            reactivate_fn=self.company_executor._reactivate_for_unread_mail,
            interval_sec=10.0,
        )

        # Wire message bus
        self.message_bus.set_handler(self._handle_message)
        self.event_bus.subscribe_all(self._persist_event)
        if self.on_runtime_event is not None:
            self.event_bus.subscribe("runtime_event", self._forward_runtime_event)

        reconciled = 0
        if self._run_startup_reconcile:
            try:
                reconciled = await self._reconcile_interrupted_project_tasks()
            except InvalidPhaseTransition:
                logger.opt(exception=True).error(
                    "Startup reconcile hit an invalid work-item phase transition for project {}; aborting initialization",
                    self.project_id or "default",
                )
                raise
            except Exception:
                logger.opt(exception=True).error(
                    "Startup reconcile failed for project {}; aborting initialization",
                    self.project_id or "default",
                )
                raise
        if self.comms_reactivation_sweeper is not None:
            await self.comms_reactivation_sweeper.start()
        self._initialized = True
        logger.info("OPC Engine initialized successfully")
        if reconciled:
            logger.warning(
                "Reconciled {} interrupted task(s) for project {} during startup",
                reconciled,
                self.project_id or "default",
            )
        available_agents = self._available_external_agents()
        if available_agents:
            logger.info(f"External agents available: {', '.join(available_agents)}")
        else:
            logger.info("No external agents detected — using native agent only")

    def _register_tools(self) -> None:
        """Register all built-in tools."""
        self.tool_registry.register(create_user_input_tool())
        for tool in create_shell_tools():
            self.tool_registry.register(tool)
        for tool in create_file_tools():
            self.tool_registry.register(tool)
        for tool in create_web_tools():
            self.tool_registry.register(tool)
        for tool in create_browser_tools():
            self.tool_registry.register(tool)
        for tool in create_git_tools():
            self.tool_registry.register(tool)
        self.tool_registry.register(create_python_tool())
        for tool in create_todo_tools():
            self.tool_registry.register(tool)
        for tool in create_agent_runtime_tools():
            self.tool_registry.register(tool)
        logger.debug(f"Registered {len(self.tool_registry.list_tools())} tools")

    async def _register_mcp_tools(self) -> None:
        assert self.mcp_manager
        for server_cfg in self.config.system.mcp_servers:
            if not server_cfg.enabled:
                continue
            try:
                server_type = getattr(server_cfg, "type", "local") or "local"
                if server_type == "remote":
                    if not server_cfg.url:
                        logger.warning(f"MCP '{server_cfg.name}' is remote but has no url, skipping")
                        continue
                    conn = await self.mcp_manager.connect_remote(
                        name=server_cfg.name,
                        url=server_cfg.url,
                        headers=server_cfg.headers or None,
                        timeout=server_cfg.startup_timeout,
                    )
                else:
                    if not server_cfg.command:
                        logger.warning(f"MCP '{server_cfg.name}' is local but has no command, skipping")
                        continue
                    conn = await self.mcp_manager.connect_local(
                        name=server_cfg.name,
                        command=server_cfg.command,
                        env=server_cfg.env or None,
                        timeout=server_cfg.startup_timeout,
                    )
                tool_filter = set(server_cfg.tools_filter) if server_cfg.tools_filter else None
                tools = await self.mcp_manager.register_tools(conn, tool_filter)
                for tool in tools:
                    self.tool_registry.register(tool)
                logger.info(f"MCP '{server_cfg.name}' ({server_type}): registered {len(tools)} tools")
            except Exception as exc:
                logger.warning(f"MCP '{server_cfg.name}' unavailable, skipping: {exc}")

    def _register_collaboration_tools(self) -> None:
        if not self.communication:
            return
        for tool in create_collaboration_tools(
            self.communication,
            reorg_manager=self.reorg_manager,
            capability_manager=self.capability_manager,
        ):
            self.tool_registry.register(tool)

    async def _persist_event(self, event: OPCEvent) -> None:
        if self.store:
            await self.store.save_event(event)

    async def _forward_runtime_event(self, event: OPCEvent) -> None:
        if self.on_runtime_event is None:
            return
        await self.on_runtime_event(event)

    async def _emit_company_runtime_event(self, event_type: str, payload: dict[str, Any]) -> None:
        await self.event_bus.publish(
            OPCEvent(
                event_type="runtime_event",
                payload={"type": event_type, "timestamp_ms": int(time.time() * 1000), **dict(payload or {})},
            )
        )

    async def _ensure_primary_session(self, session_id: str, initial_text: str = "") -> None:
        if not self.memory:
            return
        await self.memory.ensure_session(
            session_id=session_id,
            project_id=self.project_id or "default",
            title=initial_text[:120].strip(),
            mode="primary",
        )

    async def _session_has_completed_recruitment_confirmation(self, session_id: str | None) -> bool:
        if not self.store or not session_id:
            return False
        session = await self.store.get_session(session_id)
        if not session:
            return False
        metadata = dict(session.metadata or {})
        recruitment_state = metadata.get("recruitment_confirmation")
        if isinstance(recruitment_state, dict):
            return bool(recruitment_state.get("completed"))
        if metadata.get("recruitment_confirmation_completed"):
            return True
        tasks = await self.store.get_tasks(project_id=session.project_id or (self.project_id or "default"))
        for task in tasks:
            parent_session_id = str(
                getattr(task, "parent_session_id", "")
                or dict(getattr(task, "metadata", {}) or {}).get("parent_session_id", "")
                or ""
            ).strip()
            if parent_session_id == session_id:
                return True
        return False

    async def _mark_session_recruitment_confirmation_completed(
        self,
        session_id: str | None,
        *,
        source: str,
    ) -> None:
        if not self.store or not session_id:
            return
        session = await self.store.get_session(session_id)
        if not session:
            await self._ensure_primary_session(session_id)
            session = await self.store.get_session(session_id)
        if not session:
            return
        metadata = dict(session.metadata or {})
        previous = metadata.get("recruitment_confirmation")
        previous_state = dict(previous) if isinstance(previous, dict) else {}
        metadata["recruitment_confirmation"] = {
            **previous_state,
            "completed": True,
            "source": source,
            "project_id": session.project_id,
            "completed_at": datetime.now().isoformat(),
        }
        metadata["recruitment_confirmation_completed"] = True
        session.metadata = metadata
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    @staticmethod
    def _is_placeholder_staffing_employee(employee: Any) -> bool:
        metadata = dict(getattr(employee, "metadata", {}) or {})
        return bool(metadata.get("is_default_employee") or metadata.get("is_fallback_employee"))

    def _active_staffing_employees_by_id(self) -> dict[str, Any]:
        employees_by_id: dict[str, Any] = {}
        for employee in (self.org_engine.list_employees() if self.org_engine else []):
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if not employee_id or self._is_placeholder_staffing_employee(employee):
                continue
            employees_by_id[employee_id] = employee
            legacy_ids = list(dict(getattr(employee, "metadata", {}) or {}).get("legacy_employee_ids", []) or [])
            for legacy_id in legacy_ids:
                legacy = str(legacy_id or "").strip()
                if legacy:
                    employees_by_id[legacy] = employee
        return employees_by_id

    def _canonical_staffing_employee_id(self, employee: Any, fallback: str = "") -> str:
        if employee is not None and not self._is_placeholder_staffing_employee(employee):
            template_id = str(getattr(employee, "template_id", "") or "").strip()
            if template_id:
                return template_id
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if employee_id:
                return employee_id
        return str(fallback or "").strip()

    def _staffing_employee_payload(self, employee: Any) -> dict[str, Any]:
        employee_id = str(getattr(employee, "employee_id", "") or "").strip()
        role_id = str(getattr(employee, "role_id", "") or "").strip()
        metadata = dict(getattr(employee, "metadata", {}) or {})
        role_ids: list[str] = []
        role_getter = getattr(self.org_engine, "employee_role_ids", None) if self.org_engine else None
        if callable(role_getter):
            try:
                role_ids = list(role_getter(employee))
            except Exception:
                role_ids = []
        if not role_ids:
            for value in [
                role_id,
                metadata.get("home_role_id"),
                *list(metadata.get("home_role_ids", []) or []),
                *list(metadata.get("staffed_role_ids", []) or []),
            ]:
                normalized_role_id = str(value or "").strip()
                if normalized_role_id and normalized_role_id not in role_ids:
                    role_ids.append(normalized_role_id)
        experience_score = 0.0
        if self.org_engine and self.org_engine.employee_evolution:
            try:
                experience_score = self.org_engine.employee_evolution.get_experience_score(
                    employee_id,
                    role_id=role_id,
                    domains=[],
                    project_id=self.project_id or "default",
                )
            except Exception:
                experience_score = 0.0
        return {
            "kind": "employee",
            "employee_id": employee_id,
            "employee_name": str(getattr(employee, "name", "") or employee_id),
            "template_id": str(getattr(employee, "template_id", "") or ""),
            "role_id": role_id,
            "role_ids": role_ids,
            "home_role_id": str(metadata.get("home_role_id") or role_id),
            "category": str(getattr(employee, "category", "") or ""),
            "domains": list(getattr(employee, "domains", []) or []),
            "tags": list(getattr(employee, "tags", []) or []),
            "description": str(getattr(employee, "description", "") or ""),
            "preferred_external_agent": getattr(employee, "preferred_external_agent", None),
            "experience_score": experience_score,
        }

    def _staffing_template_payload(self, template: Any) -> dict[str, Any]:
        template_id = str(getattr(template, "id", "") or "").strip()
        return {
            "kind": "template",
            "template_id": template_id,
            "template_name": str(getattr(template, "name", "") or template_id),
            "category": str(getattr(template, "category", "") or ""),
            "domains": list(getattr(template, "domains", []) or []),
            "tags": list(getattr(template, "tags", []) or []),
            "description": str(getattr(template, "description", "") or ""),
            "preferred_external_agent": getattr(template, "preferred_external_agent", None),
            "source_repo": str(getattr(template, "source_repo", "") or ""),
            "source_path": str(getattr(template, "source_path", "") or ""),
        }

    def _project_company_staffing_defaults_path(self, project_id: str | None = None) -> Path | None:
        project = str(project_id or self.project_id or "default").strip() or "default"
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project):
            logger.warning(f"Skipping company staffing defaults for unsafe project_id={project!r}")
            return None
        return self.opc_home / "projects" / project / "company_staffing_defaults.json"

    def _company_staffing_scope_key(
        self,
        decision: RouterDecision | None,
        *,
        company_profile: str | None = None,
    ) -> str:
        profile = str(
            company_profile
            or getattr(decision, "company_profile", "")
            or getattr(getattr(self.config, "org", None), "company_profile", "")
            or CompanyProfile.CORPORATE.value
        ).strip().lower() or CompanyProfile.CORPORATE.value
        org_cfg = getattr(self.config, "org", None)
        org_key = str(getattr(decision, "org_id", "") or "").strip()
        if not org_key:
            org_key = str(getattr(org_cfg, "organization_id", "") or "").strip()
        if not org_key:
            org_key = str(getattr(org_cfg, "organization_config_file", "") or "").strip()
        if not org_key:
            org_key = DEFAULT_ORGANIZATION_ID
        return f"profile:{profile}|org:{org_key}"

    def _load_project_company_staffing_defaults(
        self,
        decision: RouterDecision | None,
        *,
        company_profile: str | None = None,
    ) -> dict[str, Any]:
        path = self._project_company_staffing_defaults_path()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.opt(exception=True).debug("Failed to load project company staffing defaults")
            return {}
        scopes = dict(data.get("scopes", {}) or {}) if isinstance(data, dict) else {}
        return dict(scopes.get(self._company_staffing_scope_key(decision, company_profile=company_profile), {}) or {})

    def _saved_staffing_defaults_to_runtime_overrides(
        self,
        decision: RouterDecision | None,
        *,
        company_profile: str | None = None,
    ) -> tuple[dict[str, str], dict[str, str], set[str], dict[str, str]] | None:
        saved_defaults = self._load_project_company_staffing_defaults(
            decision,
            company_profile=company_profile,
        )
        saved_selections = dict(saved_defaults.get("staffing_selections", {}) or {})
        if not saved_selections:
            return None

        active_role_ids = {
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in (self.org_engine.list_agents() if self.org_engine else [])
            if str(getattr(agent, "role_id", "") or "").strip()
            and str(getattr(agent, "role_id", "") or "").strip() != "task_generalist"
        }
        staffing_overrides: dict[str, str] = {}
        staffing_experience_modes: dict[str, str] = {}
        fallback_role_ids: set[str] = set()

        for raw_role_id, raw_selection in saved_selections.items():
            role_id = str(raw_role_id or "").strip()
            if not role_id or (active_role_ids and role_id not in active_role_ids):
                continue
            selection = self._normalize_staffing_selection(raw_selection)
            kind = selection.get("kind", "fallback")
            selected_id = str(selection.get("id", "") or "").strip()
            if kind == "employee" and selected_id:
                staffing_overrides[role_id] = selected_id
                raw_mode = raw_selection.get("experience_mode") if isinstance(raw_selection, dict) else ""
                staffing_experience_modes[role_id] = self._normalize_staffing_experience_mode(raw_mode)
                continue
            if kind == "template" and selected_id:
                staffing_overrides[role_id] = selected_id
                staffing_experience_modes[role_id] = "template_only"
                continue
            fallback_role_ids.add(role_id)

        role_agent_overrides: dict[str, str] = {}
        for raw_role_id, raw_agent in dict(saved_defaults.get("recruitment_role_agents", {}) or {}).items():
            role_id = str(raw_role_id or "").strip()
            if not role_id or (active_role_ids and role_id not in active_role_ids):
                continue
            agent = normalize_recruitment_agent_choice(raw_agent)
            if agent:
                role_agent_overrides[role_id] = agent

        return staffing_overrides, staffing_experience_modes, fallback_role_ids, role_agent_overrides

    def _validated_saved_staffing_selection(
        self,
        raw_selection: Any,
        *,
        employee_ids: set[str],
        template_ids: set[str],
    ) -> dict[str, str]:
        selection = self._normalize_staffing_selection(raw_selection)
        kind = selection.get("kind", "fallback")
        selected_id = str(selection.get("id", "") or "").strip()
        if kind == "employee" and selected_id in employee_ids:
            return {"kind": "employee", "id": selected_id, "employee_id": selected_id}
        if kind == "template" and selected_id in template_ids:
            return {"kind": "template", "id": selected_id, "template_id": selected_id}
        return {"kind": "fallback", "id": ""}

    def _save_project_company_staffing_defaults(
        self,
        decision: RouterDecision | None,
        *,
        company_profile: str | None = None,
        role_ids: set[str] | list[str] | tuple[str, ...],
        staffing_overrides: dict[str, str] | None,
        staffing_experience_modes: dict[str, str] | None,
        fallback_role_ids: set[str] | list[str] | tuple[str, ...],
        role_agent_overrides: dict[str, str] | None,
    ) -> None:
        path = self._project_company_staffing_defaults_path()
        if path is None:
            return
        normalized_role_ids = {
            str(role_id or "").strip()
            for role_id in list(role_ids or [])
            if str(role_id or "").strip()
        }
        if not normalized_role_ids:
            return
        fallback_set = {
            str(role_id or "").strip()
            for role_id in list(fallback_role_ids or [])
            if str(role_id or "").strip()
        }
        staffing = {
            str(role_id or "").strip(): str(employee_id or "").strip()
            for role_id, employee_id in dict(staffing_overrides or {}).items()
            if str(role_id or "").strip() and str(employee_id or "").strip()
        }
        experience_modes = {
            str(role_id or "").strip(): self._normalize_staffing_experience_mode(mode)
            for role_id, mode in dict(staffing_experience_modes or {}).items()
            if str(role_id or "").strip()
        }
        role_agents = {
            str(role_id or "").strip(): normalize_recruitment_agent_choice(agent, default="codex") or "codex"
            for role_id, agent in dict(role_agent_overrides or {}).items()
            if str(role_id or "").strip()
        }
        selections: dict[str, dict[str, str]] = {}
        agents: dict[str, str] = {}
        for role_id in sorted(normalized_role_ids):
            if role_id in staffing:
                if experience_modes.get(role_id) == "template_only":
                    selections[role_id] = {
                        "kind": "template",
                        "id": staffing[role_id],
                        "template_id": staffing[role_id],
                        "experience_mode": "template_only",
                    }
                else:
                    selections[role_id] = {
                        "kind": "employee",
                        "id": staffing[role_id],
                        "employee_id": staffing[role_id],
                        "experience_mode": "with_experience",
                    }
            else:
                selections[role_id] = {"kind": "fallback", "id": ""}
            if role_id in fallback_set:
                selections[role_id] = {"kind": "fallback", "id": ""}
            agents[role_id] = role_agents.get(role_id) or "codex"

        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        scopes = dict(data.get("scopes", {}) or {})
        scope_key = self._company_staffing_scope_key(decision, company_profile=company_profile)
        profile = str(company_profile or getattr(decision, "company_profile", "") or CompanyProfile.CORPORATE.value).strip().lower()
        scopes[scope_key] = {
            "company_profile": profile or CompanyProfile.CORPORATE.value,
            "org_id": str(getattr(decision, "org_id", "") or "").strip(),
            "updated_at": datetime.now().isoformat(),
            "staffing_selections": selections,
            "recruitment_role_agents": agents,
        }
        data.update({"version": 1, "updated_at": datetime.now().isoformat(), "scopes": scopes})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _build_manual_staffing_checkpoint_payload(
        self,
        decision: RouterDecision,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_manual_preflight: bool = False,
    ) -> dict[str, Any] | None:
        if not self.org_engine or not self.talent_market:
            return None
        employees = [
            employee
            for employee in self.org_engine.list_employees()
            if not self._is_placeholder_staffing_employee(employee)
        ]
        employee_payloads = [self._staffing_employee_payload(employee) for employee in employees]
        employee_by_role: dict[str, list[dict[str, Any]]] = {}
        for employee_payload in employee_payloads:
            role_ids = [
                str(item or "").strip()
                for item in list(employee_payload.get("role_ids", []) or [])
                if str(item or "").strip()
            ] or [str(employee_payload.get("role_id", "") or "").strip()]
            for role_id in role_ids:
                if role_id:
                    employee_by_role.setdefault(role_id, []).append(employee_payload)
        template_payloads = [
            self._staffing_template_payload(template)
            for template in self.talent_market.list_available_templates()
            if str(getattr(template, "id", "") or "").strip()
        ]
        saved_defaults = self._load_project_company_staffing_defaults(
            decision,
            company_profile=str(runtime_spec.profile or "corporate"),
        )
        saved_selections = dict(saved_defaults.get("staffing_selections", {}) or {})
        saved_agents = dict(saved_defaults.get("recruitment_role_agents", {}) or {})
        employee_ids = {
            str(item.get("employee_id", "") or "").strip()
            for item in employee_payloads
            if str(item.get("employee_id", "") or "").strip()
        }
        template_ids = {
            str(item.get("template_id", "") or "").strip()
            for item in template_payloads
            if str(item.get("template_id", "") or "").strip()
        }
        roles: list[dict[str, Any]] = []
        default_agent = "codex"
        for agent in self.org_engine.list_agents():
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            if not role_id or role_id == "task_generalist":
                continue
            same_role_employees = employee_by_role.get(role_id, [])
            default_selection: dict[str, Any] = {"kind": "fallback"}
            default_source = "system"
            if role_id in saved_selections:
                default_selection = self._validated_saved_staffing_selection(
                    saved_selections.get(role_id),
                    employee_ids=employee_ids,
                    template_ids=template_ids,
                )
                if default_selection.get("kind") == "fallback" and same_role_employees:
                    default_selection = {
                        "kind": "employee",
                        "id": same_role_employees[0]["employee_id"],
                        "employee_id": same_role_employees[0]["employee_id"],
                    }
                    default_source = "org"
                else:
                    default_source = "project" if default_selection.get("kind") != "fallback" or role_id in saved_agents else "system"
            elif same_role_employees:
                default_selection = {
                    "kind": "employee",
                    "id": same_role_employees[0]["employee_id"],
                    "employee_id": same_role_employees[0]["employee_id"],
                }
                default_source = "org"
            selected_agent = normalize_recruitment_agent_choice(saved_agents.get(role_id), default=default_agent) or default_agent
            roles.append(
                {
                    "role_id": role_id,
                    "role_label": str(getattr(agent, "name", "") or role_id),
                    "role_responsibility": str(getattr(agent, "responsibility", "") or ""),
                    "default_selection": default_selection,
                    "same_role_employee_ids": [
                        str(item.get("employee_id", "") or "")
                        for item in same_role_employees
                        if str(item.get("employee_id", "") or "")
                    ],
                    "fallback_available": True,
                    "default_agent": default_agent,
                    "selected_agent": selected_agent,
                    "default_source": default_source,
                }
            )
        if not roles:
            return None
        role_agent_overrides = {
            str(role.get("role_id", "") or "").strip(): str(role.get("selected_agent", "") or "").strip()
            for role in roles
            if str(role.get("role_id", "") or "").strip() and str(role.get("selected_agent", "") or "").strip()
        }
        has_employees = bool(employee_payloads)
        has_templates = bool(template_payloads)
        if has_employees:
            staffing_strategy = "existing_staffing"
            recommended_action = "manual_approve"
            summary = "Review existing employees, optional hires, and execution agents before company runtime starts."
        elif has_templates:
            staffing_strategy = "initial_recruitment"
            recommended_action = "auto_recruit"
            summary = "No employees are staffed yet. Recruit from talent templates or choose templates manually before company runtime starts."
        else:
            staffing_strategy = "role_only_fallback"
            recommended_action = "manual_approve"
            summary = "No employees or talent templates are available. Approving will use role-only fallback execution."
        return {
            "original_message": original_message,
            "decision": self._serialize_router_decision(decision),
            "runtime_spec": serialize_company_runtime_spec(runtime_spec),
            "primary_session_id": session_id,
            "origin_channel": origin_channel,
            "origin_chat_id": origin_chat_id,
            "origin_thread_id": origin_thread_id,
            "origin_task_id": origin_task_id,
            "attachment_refs": self._normalize_attachment_refs(attachment_refs),
            "company_profile": str(runtime_spec.profile or "corporate"),
            "summary": summary,
            "staffing_strategy": staffing_strategy,
            "recommended_action": recommended_action,
            "force_manual_preflight": bool(force_manual_preflight),
            "recruitment_agent": "native",
            "staffing_defaults": {
                "source": "project" if saved_defaults else "system",
                "scope_key": self._company_staffing_scope_key(
                    decision,
                    company_profile=str(runtime_spec.profile or "corporate"),
                ),
                "updated_at": str(saved_defaults.get("updated_at", "") or ""),
            },
            "staffing_roles": roles,
            "recruitment_role_agents": role_agent_overrides,
            "staffing_pool": {
                "employees": employee_payloads,
                "templates": template_payloads,
            },
        }

    def _render_manual_staffing_summary(self, payload: dict[str, Any]) -> str:
        employees_by_id = {
            str(item.get("employee_id", "") or ""): item
            for item in list(dict(payload.get("staffing_pool", {}) or {}).get("employees", []) or [])
        }
        lines = [
            "Company mode has a pending manual staffing selection before execution.",
            "",
            f"Company profile: `{payload.get('company_profile', 'corporate')}`",
        ]
        summary = str(payload.get("summary", "") or "").strip()
        if summary:
            lines.extend(["", summary])
        recommended_action = str(payload.get("recommended_action", "") or "").strip()
        if recommended_action:
            lines.extend(["", f"Recommended action: `{recommended_action}`"])
        lines.extend(["", "Manual staffing defaults:"])
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            role_label = str(role.get("role_label", "") or role_id).strip()
            selection = dict(role.get("default_selection", {}) or {})
            if selection.get("kind") == "employee":
                employee_id = str(selection.get("employee_id") or selection.get("id") or "").strip()
                employee = employees_by_id.get(employee_id, {})
                employee_name = str(employee.get("employee_name", "") or employee_id)
                lines.append(f"- Role `{role_id}` ({role_label}): `{employee_name}` ({employee_id})")
            else:
                lines.append(f"- Role `{role_id}` ({role_label}): fallback to role-only execution")
        lines.extend(
            [
                "",
                "Reply `approve` to use these defaults, or include overrides like `approve senior_engineer=tpl:engineering-frontend-developer`.",
                "Reply `auto` or `auto recruit` to run automatic recruitment.",
                "Reply `deny` / `stop` to cancel company-mode execution.",
            ]
        )
        return "\n".join(lines)

    async def _begin_company_staffing_loop(
        self,
        decision: RouterDecision,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_manual_preflight: bool = False,
    ) -> str:
        session_confirmed = await self._session_has_completed_recruitment_confirmation(session_id)
        if not session_confirmed:
            payload = self._build_manual_staffing_checkpoint_payload(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_manual_preflight=force_manual_preflight,
            )
            if payload is not None:
                await self._save_execution_checkpoint(
                    {
                        "project_id": self.project_id or "default",
                        "session_id": session_id,
                        "checkpoint_type": "company_staffing_selection",
                        "payload": payload,
                    }
                )
                return self._render_manual_staffing_summary(payload)
        else:
            saved_runtime_staffing = self._saved_staffing_defaults_to_runtime_overrides(
                decision,
                company_profile=str(runtime_spec.profile or "corporate"),
            )
            if saved_runtime_staffing is not None:
                (
                    staffing_overrides,
                    staffing_experience_modes,
                    fallback_role_ids,
                    role_agent_overrides,
                ) = saved_runtime_staffing
                return await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
        return await self._begin_company_recruitment_loop(
            decision,
            original_message,
            runtime_spec,
            session_id=session_id,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
        )

    @staticmethod
    def _strip_task_mode_work_item_identity(metadata: dict[str, Any]) -> dict[str, Any]:
        """Task mode uses Task as a runtime container, not a company work item."""
        cleaned = dict(metadata or {})
        for key in (
            "work_item_projection_id",
            "work_item_turn_type",
            "work_item_projection_title",
            "work_item_metadata",
            "work_item_gate",
            "employee_assignment",
            "employee_prompt_context",
            "employee_delta_context",
        ):
            cleaned.pop(key, None)
        return cleaned

    @staticmethod
    def _company_reply_text_looks_internal_dispatch(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        internal_markers = (
            "已创建下游 WorkItem",
            "已创建两个子 WorkItem",
            "已完成派发",
            "Work item delegated downstream work",
            "delegated downstream work and is waiting for child work items",
        )
        return any(marker in normalized for marker in internal_markers)

    @staticmethod
    def _company_task_is_owner_facing_delivery(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        return (
            str(metadata.get("feedback_scope", "") or "").strip().lower() == "final"
            and turn_type_for_task(task, fallback="") == "deliver"
            and bool(metadata.get("authoritative_output", False))
        )

    @staticmethod
    def _company_task_is_runtime_result(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            return True
        if str(metadata.get("execution_model", "") or "").strip() == "multi_team_org":
            return True
        if str(metadata.get("work_item_projection_id", "") or "").strip():
            return True
        if str(metadata.get("company_profile", "") or "").strip():
            return True
        return bool(metadata.get("work_item_runtime"))

    @staticmethod
    def _company_task_is_internal_dispatch_result(task: Task, content: str) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        turn_type = turn_type_for_task(task, fallback="")
        if turn_type not in {"intake", "dispatch", "plan", "aggregate"}:
            return False
        if bool(metadata.get("manager_board_mutation_performed", False)):
            return True
        if bool(metadata.get("delegated_children_pending", False)):
            return True
        if [
            str(item).strip()
            for item in list(metadata.get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        ]:
            return True
        return OPCEngine._company_reply_text_looks_internal_dispatch(content)

    async def _company_reply_is_internal_runtime_result(
        self,
        session_id: str,
        assistant_text: str,
        *,
        allow_marker_fallback: bool = False,
    ) -> bool:
        text = str(assistant_text or "").strip()
        if not text:
            return False
        marker_fallback = (
            self._company_reply_text_looks_internal_dispatch(text)
            if allow_marker_fallback
            else False
        )
        if not self.store:
            return marker_fallback
        try:
            tasks = await self.store.get_tasks(project_id=self.project_id or "default")
        except Exception:
            logger.opt(exception=True).debug("failed to inspect company tasks before recording top-level reply")
            return marker_fallback
        session_key = str(session_id or "").strip()
        for task in tasks:
            if session_key and session_key not in {
                str(getattr(task, "session_id", "") or "").strip(),
                str(getattr(task, "parent_session_id", "") or "").strip(),
            }:
                continue
            result = getattr(task, "result", None)
            content = str((result or {}).get("content", "") if isinstance(result, dict) else "").strip()
            if content != text:
                continue
            if self._company_task_is_owner_facing_delivery(task):
                return True
            if self._company_task_is_internal_dispatch_result(task, content):
                return True
            if self._company_task_is_runtime_result(task):
                return True
        return False

    @staticmethod
    def _primary_reply_match_text(value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if len(paragraphs) > 1 and re.match(r"^Verification:\s", paragraphs[-1], flags=re.IGNORECASE):
            text = "\n\n".join(paragraphs[:-1]).strip()
        return text

    @staticmethod
    def _normalized_execution_agent(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_")

    @classmethod
    def _task_mode_task_uses_external_agent(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        for value in (
            getattr(task, "assigned_external_agent", None),
            metadata.get("assigned_external_agent"),
            metadata.get("preferred_external_agent"),
            metadata.get("selected_execution_agent"),
        ):
            normalized = cls._normalized_execution_agent(value)
            if normalized and normalized not in {"native", "task_generalist", "opc"}:
                return True
        return False

    @staticmethod
    def _task_is_task_mode_runtime(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            return False
        if execution_mode in {ExecutionMode.TASK_MODE.value, "task", "project_mode", "project"}:
            return True
        projection_id = str(metadata.get("work_item_projection_id", "") or "").strip()
        if projection_id and projection_id != "task_mode_execution":
            return False
        return (
            str(metadata.get("mode", "") or "").strip().lower() == "task"
            or str(metadata.get("task_mode_contract", "") or "").strip() == "single_full_capability_main_agent"
            or str(metadata.get("runtime_kind", "") or "").strip() == "task_mode_agent_turn"
            or projection_id == "task_mode_execution"
        )

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _apply_task_mode_external_timeout_defaults(self, task: Task) -> None:
        if not self._task_is_task_mode_runtime(task):
            return
        timeout = self._coerce_positive_int(self.config.system.task_mode.sub_agent_timeout_sec)
        if timeout is None:
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        changed = False
        if self._coerce_positive_int(metadata.get("external_hard_timeout_seconds")) is None:
            metadata["external_hard_timeout_seconds"] = timeout
            changed = True
        if self._coerce_positive_int(metadata.get("external_idle_timeout_seconds")) is None:
            metadata["external_idle_timeout_seconds"] = timeout
            changed = True
        if changed:
            task.metadata = metadata

    @staticmethod
    def _session_transcript_item_text(item: Any) -> str:
        parts = item.get("parts", []) if isinstance(item, dict) else []
        texts: list[str] = []
        for part in parts:
            payload = part.get("payload", {}) if isinstance(part, dict) else getattr(part, "payload", {})
            if isinstance(payload, dict):
                texts.append(str(payload.get("text", "") or ""))
        return "\n".join(texts).strip()

    async def _task_mode_external_top_level_reply_exists(
        self,
        session_id: str,
        assistant_text: str,
        *,
        task_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        if not self.store:
            return False
        transcript_loader = getattr(self.store, "get_session_transcript", None)
        if not callable(transcript_loader):
            return False
        text = self._primary_reply_match_text(assistant_text)
        if not text:
            return False
        try:
            transcript = await transcript_loader(session_id)
        except Exception:
            return False
        expected_task_id = str(task_id or "").strip()
        expected_turn_id = str(turn_id or "").strip()
        for item in transcript:
            message = item.get("message") if isinstance(item, dict) else None
            metadata = dict(getattr(message, "metadata", {}) or {}) if message is not None else {}
            kind = str(metadata.get("kind", "") or metadata.get("source_kind", "") or "").strip()
            if kind != "top_level_reply" or not metadata.get("task_mode_external_result"):
                continue
            if self._primary_reply_match_text(self._session_transcript_item_text(item)) != text:
                continue
            recorded_task_id = str(getattr(message, "task_id", "") or metadata.get("task_id", "") or "").strip()
            recorded_turn_id = str(
                metadata.get("conversation_turn_id", "")
                or metadata.get("canonical_turn_id", "")
                or metadata.get("turn_id", "")
                or ""
            ).strip()
            if expected_task_id and recorded_task_id and expected_task_id != recorded_task_id:
                continue
            if expected_turn_id and recorded_turn_id and expected_turn_id != recorded_turn_id:
                continue
            return True
        return False

    async def _record_task_mode_external_result_reply(self, task: Task, result_content: str) -> None:
        if not self.memory or not task.session_id:
            return
        if not self._task_is_task_mode_runtime(task):
            return
        if not self._task_mode_task_uses_external_agent(task):
            return
        content = str(result_content or "").strip()
        if not content:
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        turn_id = str(
            metadata.get("conversation_turn_id", "")
            or metadata.get("canonical_turn_id", "")
            or metadata.get("turn_id", "")
            or ""
        ).strip()
        if await self._task_mode_external_top_level_reply_exists(
            str(task.session_id),
            content,
            task_id=str(task.id or "").strip(),
            turn_id=turn_id,
        ):
            return

        reply_metadata: dict[str, Any] = {
            "kind": "top_level_reply",
            "task_mode_external_result": True,
            "task_id": str(task.id or "").strip(),
            "assigned_external_agent": str(getattr(task, "assigned_external_agent", "") or "").strip(),
        }
        selected_agent = str(metadata.get("selected_execution_agent", "") or "").strip()
        if selected_agent:
            reply_metadata["selected_execution_agent"] = selected_agent
        if turn_id:
            reply_metadata["conversation_turn_id"] = turn_id
            reply_metadata["canonical_turn_id"] = turn_id
            reply_metadata["turn_id"] = turn_id
            reply_metadata["ui_message_id"] = f"task-mode-external-reply:{turn_id}"

        await self.memory.record_assistant_turn(
            session_id=str(task.session_id),
            content=content,
            project_id=task.project_id or self.project_id or "default",
            task_id=task.id,
            metadata=reply_metadata,
        )

    async def _task_mode_reply_uses_native_runtime_transcript(
        self,
        session_id: str,
        assistant_text: str,
        *,
        origin_task_id: str | None = None,
        preferred_agent: str | None = None,
    ) -> bool:
        normalized_agent = self._normalized_execution_agent(preferred_agent)
        if normalized_agent and normalized_agent not in {"native", "task_generalist", "opc"}:
            return False

        text = self._primary_reply_match_text(assistant_text)
        if not text:
            return False

        if self.store:
            task_getter = getattr(self.store, "get_tasks", None)
            if callable(task_getter):
                try:
                    tasks = await task_getter(project_id=self.project_id or "default")
                except Exception:
                    tasks = []
                session_key = str(session_id or "").strip()
                origin_key = str(origin_task_id or "").strip()
                for task in tasks:
                    if not self._task_is_task_mode_runtime(task):
                        continue
                    metadata = dict(getattr(task, "metadata", {}) or {})
                    task_ids = {
                        str(getattr(task, "id", "") or "").strip(),
                        str(getattr(task, "session_id", "") or "").strip(),
                        str(metadata.get("origin_task_id", "") or "").strip(),
                    }
                    if session_key and session_key not in task_ids:
                        continue
                    if origin_key and origin_key not in task_ids:
                        continue
                    result = getattr(task, "result", None)
                    content = str((result or {}).get("content", "") if isinstance(result, dict) else "").strip()
                    if self._primary_reply_match_text(content) != text:
                        continue
                    return not self._task_mode_task_uses_external_agent(task)

            transcript_loader = getattr(self.store, "get_session_transcript", None)
            if callable(transcript_loader):
                try:
                    transcript = await transcript_loader(session_id)
                except Exception:
                    transcript = []
                for item in transcript:
                    message = item.get("message") if isinstance(item, dict) else None
                    metadata = dict(getattr(message, "metadata", {}) or {}) if message is not None else {}
                    kind = str(metadata.get("kind", "") or metadata.get("source_kind", "") or "").strip()
                    if kind != "runtime_v2_assistant":
                        continue
                    content = self._session_transcript_item_text(item)
                    if self._primary_reply_match_text(content) == text:
                        return True

        return normalized_agent in {"native", "task_generalist", "opc"}

    async def _record_primary_exchange(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        mode: str | None = None,
        origin_task_id: str | None = None,
        preferred_agent: str | None = None,
    ) -> None:
        if not self.memory:
            return
        _ = (user_text, origin_task_id)
        raw_mode = str(mode or "").strip().lower()
        requested_mode = self._normalize_requested_mode(mode)
        is_task_mode = requested_mode == "task"
        is_company_like_mode = requested_mode == "company" or raw_mode in {"company", "org", "custom"}
        if is_task_mode and await self._task_mode_external_top_level_reply_exists(
            session_id,
            assistant_text,
            task_id=origin_task_id,
        ):
            return
        if is_task_mode and await self._task_mode_reply_uses_native_runtime_transcript(
            session_id,
            assistant_text,
            origin_task_id=origin_task_id,
            preferred_agent=preferred_agent,
        ):
            # Native task-mode final replies are persisted by RuntimeV2 as
            # runtime_v2_assistant. Do not synthesize a top-level reply here.
            return
        if await self._company_reply_is_internal_runtime_result(
            session_id,
            assistant_text,
            allow_marker_fallback=is_company_like_mode,
        ):
            return
        await self.memory.record_assistant_turn(
            session_id=session_id,
            content=assistant_text,
            project_id=self.project_id or "default",
            metadata={"kind": "top_level_reply"},
        )

    async def _tool_approval_callback(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        task: Task | None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> tuple[bool, Any]:
        assert self.approval_engine
        violation = ownership_guard_violation(
            task=task,
            tool_name=tool.name,
            arguments=arguments,
            org_engine=self.org_engine,
        )
        if violation:
            return False, ApprovalDecision(
                action=ApprovalAction.REJECT,
                risk_level=RiskLevel.HIGH,
                rationale=violation,
                confidence=0.99,
                policy_source="ownership_contract",
                metadata={"tool_name": tool.name},
            )
        bridge = getattr(task, "_runtime_permission_bridge", None) if task is not None else None
        if callable(bridge):
            return await bridge(
                tool=tool,
                arguments=arguments,
                approval_engine=self.approval_engine,
                on_progress=on_progress,
            )
        metadata = {
            "category": tool.category,
            "requires_confirmation": tool.requires_confirmation,
            "description": tool.description,
        }
        return await self.approval_engine.authorize_tool_call(
            task=task,
            tool_name=tool.name,
            arguments=arguments,
            metadata=metadata,
            on_progress=on_progress,
        )

    @staticmethod
    def _conversation_turn_id_for_message(message: UserMessage) -> str:
        metadata = dict(message.metadata or {})
        for key in ("conversation_turn_id", "canonical_turn_id", "turn_id"):
            value = str(metadata.get(key, "") or "").strip()
            if value:
                return value
        ui_message_id = str(metadata.get("ui_message_id", "") or "").strip()
        if ui_message_id:
            return f"ui-turn:{ui_message_id}"
        return f"engine-turn:{message.session_id}:{uuid.uuid4().hex}"

    async def _handle_message(self, message: UserMessage) -> SystemMessage:
        """Core message handler — branches on user-selected mode (project / company)."""
        assert self.context_loader
        attachment_refs = self._normalize_attachment_refs(
            message.attachments or message.metadata.get("attachment_refs", []),
        )
        conversation_turn_id = self._conversation_turn_id_for_message(message)
        response_metadata = {
            "chat_id": message.metadata.get("chat_id", ""),
            "thread_id": message.metadata.get("thread_id", ""),
            "reply_to": message.metadata.get("reply_to", ""),
            "attachments": attachment_refs,
            "attachment_refs": attachment_refs,
            "conversation_turn_id": conversation_turn_id,
        }
        requested_mode = self._normalize_requested_mode(message.metadata.get("mode", "task"))
        origin_task_id = message.metadata.get("origin_task_id")
        preferred_agent = message.metadata.get("preferred_agent")

        async def _record_early_reply(reply: str) -> None:
            await self._record_primary_exchange(
                message.session_id,
                message.content,
                reply,
                mode=requested_mode,
                origin_task_id=origin_task_id,
                preferred_agent=preferred_agent,
            )

        if message.project_context is not None and message.project_context != self.project_id:
            logger.warning(
                "Ignoring cross-project message_context={} on engine project_id={}; "
                "route through process_message(project_id=...) so each project keeps its own store/runtime.",
                message.project_context,
                self.project_id,
            )

        await self._ensure_primary_session(message.session_id, message.content)
        if self.memory:
            user_turn_metadata: dict[str, Any] = {"kind": "top_level_user_turn"}
            for key in ("ui_message_id", "ui_created_at"):
                value = message.metadata.get(key)
                if value not in (None, "", [], {}):
                    user_turn_metadata[key] = value
            user_turn_metadata["conversation_turn_id"] = conversation_turn_id
            user_turn_metadata["canonical_turn_id"] = conversation_turn_id
            user_turn_metadata["turn_id"] = conversation_turn_id
            await self.memory.record_user_turn(
                session_id=message.session_id,
                content=message.content,
                project_id=self.project_id or "default",
                metadata=user_turn_metadata,
            )

        resumed = await self._maybe_resume_checkpoint(
            message.content,
            message.session_id,
            reply_metadata=message.metadata,
        )
        if resumed is not None:
            await _record_early_reply(resumed)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=resumed,
                message_type="reply",
                metadata=response_metadata,
            )

        reorg_reply = await self._maybe_handle_reorg_message(message.content, message.session_id)
        if reorg_reply is not None:
            await _record_early_reply(reorg_reply)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=reorg_reply,
                message_type="reply",
                metadata=response_metadata,
            )

        existing_runtime_resume = await self._maybe_resume_existing_company_runtime(
            message.content,
            message.session_id,
            force_resume=bool(message.metadata.get("ui_force_resume", False)),
        )
        if existing_runtime_resume is not None:
            await _record_early_reply(existing_runtime_resume)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=existing_runtime_resume,
                message_type="reply",
                metadata=response_metadata,
            )

        # Load context
        include_project_knowledge = self._requests_explicit_project_knowledge(message.content)
        context = await self.context_loader.load(
            project_id=self.project_id,
            session_id=message.session_id,
            include_project_knowledge=include_project_knowledge,
        )
        context.default_channel = message.channel
        context.origin_chat_id = str(message.metadata.get("chat_id", "") or "")
        context.origin_thread_id = str(message.metadata.get("thread_id", "") or "")

        # Determine mode from user metadata (no LLM router needed)
        mode = requested_mode
        org_id = message.metadata.get("org_id")
        domains = list(message.metadata.get("domains", []))
        company_profile = message.metadata.get("company_profile")

        selection = ModeSelection(
            mode=ExecutionMode.COMPANY_MODE if mode == "company" else ExecutionMode.TASK_MODE,
            org_id=org_id,
            preferred_agent=str(preferred_agent).strip() if preferred_agent else None,
            domains=domains,
            company_profile=company_profile,
            metadata={
                "company_preflight": str(message.metadata.get("company_preflight", "") or "").strip(),
            },
        )
        logger.info(f"Mode: {selection.mode.value}, org_id={org_id}, agent={preferred_agent}")

        result = await self._execute_decision(
            selection, message.content, context,
            session_id=message.session_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            conversation_turn_id=conversation_turn_id,
        )
        await self._record_primary_exchange(
            message.session_id,
            message.content,
            result,
            mode=mode,
            origin_task_id=origin_task_id,
            preferred_agent=preferred_agent,
        )

        return SystemMessage(
            channel=message.channel,
            user_id=message.user_id,
            session_id=message.session_id,
            content=result,
            message_type="reply",
            metadata=response_metadata,
        )

    def _build_company_runtime_confirmation_message(self, decision: RouterDecision, context: Any) -> str:
        profile = decision.company_profile or getattr(context, "company_profile", "corporate")
        available = ", ".join(getattr(context, "company_profiles", ["corporate", "custom"]))
        return (
            "Company mode needs a runtime profile before execution.\n\n"
            f"Recommended profile: `{profile}`\n\n"
            f"Available profiles: {available}\n"
            "Reply with `use corporate`, `use custom`, or directly describe your own company mode in natural language."
        )

    def _serialize_router_decision(self, decision: RouterDecision) -> dict[str, Any]:
        return {
            "mode": decision.mode.value,
            "preferred_agent": decision.preferred_agent,
            "domains": list(decision.domains),
            "company_profile": decision.company_profile,
            "sub_tasks": copy.deepcopy(list(getattr(decision, "sub_tasks", []) or [])),
            "org_id": getattr(decision, "org_id", None),
            "metadata": dict(getattr(decision, "metadata", {})),
        }

    def _deserialize_router_decision(self, data: dict[str, Any]) -> RouterDecision:
        return ModeSelection(
            mode=ExecutionMode(data.get("mode", ExecutionMode.TASK_MODE.value)),
            preferred_agent=data.get("preferred_agent"),
            domains=list(data.get("domains", [])),
            company_profile=data.get("company_profile"),
            sub_tasks=copy.deepcopy(list(data.get("sub_tasks", []) or [])),
            org_id=data.get("org_id"),
            metadata=dict(data.get("metadata", {})),
        )

    def _normalize_sub_tasks(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("name") or "").strip()
                description = str(item.get("description") or title).strip()
                dependencies = item.get("dependencies", [])
                if not isinstance(dependencies, list):
                    dependencies = []
                if not title and not description:
                    continue
                normalized.append(
                    {
                        **item,
                        "title": title or description[:80] or "Sub-task",
                        "description": description or title,
                        "dependencies": dependencies,
                    }
                )
                continue
            text = str(item).strip()
            if not text:
                continue
            normalized.append(
                {
                    "title": text[:80],
                    "description": text,
                    "dependencies": [],
                }
            )
        return normalized

    def _resolve_recruitment_llm(self, recruitment_agent: str | None) -> tuple[Any | None, str]:
        normalized = normalize_recruitment_agent_choice(
            recruitment_agent,
            default="native",
        ) or "native"
        if normalized == "native":
            return self.llm, normalized
        return ExternalRecruiterLLMAdapter(self, normalized), normalized

    async def _begin_company_recruitment_loop(
        self,
        decision: RouterDecision,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_confirmation: bool = False,
        role_agent_overrides: dict[str, str] | None = None,
        recruitment_agent: str | None = None,
    ) -> str:
        assert self.company_recruiter
        recruitment_llm, selected_recruitment_agent = self._resolve_recruitment_llm(recruitment_agent)
        recruitment_plan = await self.company_recruiter.build_recruitment_plan(
            runtime_spec,
            domains=decision.domains,
            project_id=self.project_id or "default",
            recruitment_llm=recruitment_llm,
            recruitment_agent=selected_recruitment_agent,
        )
        recruitment_plan.metadata = dict(getattr(recruitment_plan, "metadata", {}) or {})
        recruitment_plan.metadata.setdefault("recruitment_revision", 1)
        recruitment_plan.metadata["recruitment_agent"] = selected_recruitment_agent
        apply_recruitment_role_agent_overrides(recruitment_plan, role_agent_overrides)
        requires_confirmation = bool(force_confirmation) or recruitment_plan_requires_confirmation(recruitment_plan)
        session_confirmed = await self._session_has_completed_recruitment_confirmation(session_id)
        role_agent_overrides = extract_recruitment_role_agent_overrides(recruitment_plan)
        fallback_role_ids = build_fallback_role_ids(recruitment_plan)
        if not requires_confirmation or session_confirmed:
            if self.talent_market:
                for proposal in recruitment_plan.proposals:
                    if proposal.status != "proposed_hire" or not proposal.candidate:
                        continue
                    employee = self.talent_market.ensure_hire_template(
                        proposal.candidate.template_id,
                        proposal.role_id,
                        employee_name=proposal.candidate.proposed_employee_name,
                        employee_id=proposal.candidate.proposed_employee_id,
                    )
                    proposal.candidate.proposed_employee_id = employee.employee_id
                    proposal.candidate.proposed_employee_name = employee.name
                self.config.save(self.opc_home / "config")
                if self.org_engine:
                    self.org_engine.reload_from_config()
            staffing_overrides = build_staffing_overrides(recruitment_plan)
            staffing_experience_modes = build_staffing_experience_modes(recruitment_plan)
            if decision.mode == ExecutionMode.COMPANY_MODE:
                return await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            return await self._continue_task_mode_execution(
                decision,
                original_message,
                None,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        await self._save_execution_checkpoint(
            {
                "project_id": self.project_id or "default",
                "session_id": session_id,
                "checkpoint_type": "company_recruitment_confirmation",
                "payload": {
                    "original_message": original_message,
                    "decision": self._serialize_router_decision(decision),
                    "runtime_spec": serialize_company_runtime_spec(runtime_spec),
                    "recruitment_plan": serialize_recruitment_plan(recruitment_plan),
                    "recruitment_revision": 1,
                    "primary_session_id": session_id,
                    "origin_channel": origin_channel,
                    "origin_chat_id": origin_chat_id,
                    "origin_thread_id": origin_thread_id,
                    "origin_task_id": origin_task_id,
                    "attachment_refs": self._normalize_attachment_refs(attachment_refs),
                    "recruitment_role_agents": role_agent_overrides,
                    "recruitment_agent": selected_recruitment_agent,
                },
            }
        )
        return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)

    def _should_auto_confirm_recruitment_plan(self, recruitment_plan: Any) -> bool:
        proposals = list(getattr(recruitment_plan, "proposals", []) or [])
        if not proposals:
            return True
        if list(getattr(recruitment_plan, "recruiter_feedback", []) or []):
            return False
        common_categories = {
            "general",
            "software-engineering",
            "quality-assurance",
            "design",
            "documentation",
            "project-management",
            "operations",
        }
        common_roles = {
            str(agent.role_id).strip()
            for agent in self.org_engine.list_agents()
            if str(agent.role_id).strip()
        }
        for proposal in proposals:
            role_id = str(getattr(proposal, "role_id", "") or "").strip()
            status = str(getattr(proposal, "status", "") or "").strip()
            metadata = dict(getattr(proposal, "metadata", {}) or {})
            candidate = getattr(proposal, "candidate", None)
            existing_ids = list(getattr(proposal, "existing_employee_ids", []) or [])
            staffing_payload = {
                "role_id": role_id,
                "status": status,
                "existing_employee_ids": existing_ids,
                "candidate_category": str(getattr(candidate, "category", "") or "").strip(),
                "candidate_domains": list(getattr(candidate, "domains", []) or []),
                "triage_action": str(metadata.get("triage_action", "") or "").strip(),
            }
            if self.secretary_policies:
                policy_hit = self.secretary_policies.evaluate_tool_policy(
                    project_id=self.project_id or "default",
                    tool_name="company_staffing",
                    arguments=staffing_payload,
                    safe_command_prefixes=[],
                )
                if policy_hit and policy_hit.get("effect") == "auto_allow":
                    continue
            if status in {"direct_role_execution", "existing_staff"}:
                continue
            candidate_category = str(getattr(candidate, "category", "") or "").strip().lower()
            candidate_domains = [
                str(item).strip().lower()
                for item in list(getattr(candidate, "domains", []) or [])
                if str(item).strip()
            ]
            if (
                status == "proposed_hire"
                and role_id in common_roles
                and candidate_category in common_categories
                and len(candidate_domains) <= 1
                and len(existing_ids) <= 1
            ):
                continue
            return False
        return True

    async def _continue_task_mode_execution(
        self,
        decision: RouterDecision,
        original_message: str,
        work_item_plan: CompanyWorkItemRuntimePlan | None = None,
        *,
        session_id: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "",
        origin_thread_id: str = "",
        origin_task_id: str | None = None,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        conversation_turn_id: str | None = None,
    ) -> str:
        assert self.task_scheduler and self.store and self.org_engine
        project_id = self.project_id or "default"
        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        attachment_context = self._build_attachment_context(attachment_refs)
        workspace_contract = await self._resolve_workspace_contract(original_message, session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
        await self._sync_origin_task_execution_context(
            origin_task_id,
            session_id=session_id,
            decision=decision,
            workspace_contract=workspace_contract,
            original_message=original_message,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            attachment_refs=attachment_refs,
        )
        explicit_agent_choice = normalize_recruitment_agent_choice(decision.preferred_agent)
        explicit_external_agent = (
            explicit_agent_choice
            if explicit_agent_choice and explicit_agent_choice != "native"
            else None
        )
        include_project_knowledge = self._requests_explicit_project_knowledge(original_message)
        secretary_context = ""

        self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())
        execution_role = self.org_engine.get_task_mode_role()
        role_id = execution_role.role_id
        reusable_task = await self._find_reusable_task_mode_task(
            session_id=session_id,
            project_id=project_id,
            origin_task_id=origin_task_id,
        )
        _ = (staffing_overrides, staffing_experience_modes, fallback_role_ids)
        selected_role_agent = normalize_recruitment_agent_choice(
            (role_agent_overrides or {}).get(role_id)
        )
        preferred_external_agent = explicit_external_agent
        if not explicit_agent_choice and selected_role_agent:
            preferred_external_agent = None if selected_role_agent == "native" else selected_role_agent
        # Task mode is a single full-capability main agent by default.
        # Only an explicit user override should move execution to an external agent.
        force_native_execution = explicit_agent_choice == "native" or preferred_external_agent is None
        selected_execution_agent = (
            explicit_agent_choice
            or selected_role_agent
            or ("native" if not preferred_external_agent else preferred_external_agent)
        )
        execution_agent_locked = bool(explicit_agent_choice or selected_role_agent)
        current_turn_id = str(conversation_turn_id or "").strip()
        work_item_execution_strategy = (
            WorkItemExecutionStrategy.EXTERNAL.value
            if preferred_external_agent
            else WorkItemExecutionStrategy.NATIVE.value
        )
        if reusable_task:
            task = reusable_task
            task.title = original_message[:120].rstrip()
            task.description = original_message
            task.assigned_to = role_id
            task.priority = 5
            task.tags = []
            task.dependencies = []
            task.project_id = project_id
            task.assigned_external_agent = preferred_external_agent
            task.retry_count = 0
            task.metadata = self._strip_task_mode_work_item_identity(dict(task.metadata))
            task.context_snapshot = dict(task.context_snapshot)
            context_snapshot = task.context_snapshot if isinstance(task.context_snapshot, dict) else {}
            raw_runtime_resume = context_snapshot.get("runtime_resume", {})
            runtime_resume = dict(raw_runtime_resume) if isinstance(raw_runtime_resume, dict) else {}
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            if runtime_meta:
                task.context_snapshot["runtime_resume"] = {
                    **runtime_resume,
                    **runtime_meta,
                    "restored_from_same_session": True,
                    "restored_at": datetime.now().isoformat(),
                }
            task.metadata.pop("_runtime_v2_user_seeded", None)
            task.metadata.update({
                "mode": "task",
                "original_message": original_message,
                "workspace_root": workspace_contract.get("workspace_root"),
                "output_root": target_output_dir,
                "target_output_dir": target_output_dir,
                "comms_workspace_root": workspace_contract.get("comms_workspace_root"),
                "comms_root": workspace_contract.get("comms_root"),
                "include_project_knowledge": include_project_knowledge,
                "secretary_context": secretary_context,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "origin_task_id": origin_task_id or task.id,
                "attachment_refs": attachment_refs,
                "attachment_context": attachment_context,
                "runtime_kind": "task_mode_agent_turn",
                "work_item_role_id": role_id,
                "work_item_execution_strategy": work_item_execution_strategy,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "force_native_execution": force_native_execution,
                "task_mode_contract": "single_full_capability_main_agent",
                "router_preferred_agent": decision.preferred_agent,
                "execution_mode": decision.mode.value,
                "execution_task_ids": [task.id],
                "parent_session_id": session_id,
                "org_version": self.org_engine.current_org_version(),
                "runtime_topology_version": self.org_engine.current_runtime_topology_version(),
                "reorg_proposal_id": str(task.metadata.get("reorg_proposal_id", "") or ""),
                "migration_status": str(task.metadata.get("migration_status", "") or ""),
                "superseded_by_reorg": str(task.metadata.get("superseded_by_reorg", "") or ""),
            })
            if current_turn_id:
                runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
                runtime_meta["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2"] = runtime_meta
                task.metadata["conversation_turn_id"] = current_turn_id
                task.metadata["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2_current_turn_id"] = current_turn_id
            task.org_id = getattr(decision, "org_id", None)
            if work_item_plan:
                task.metadata["company_work_item_plan"] = serialize_company_work_item_runtime_plan(work_item_plan)
            if self.memory and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="primary",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "execution_mode": decision.mode.value,
                        "origin_task_id": task.metadata.get("origin_task_id") or task.id,
                        "runtime_kind": "task_mode_agent_turn",
                        "selected_execution_agent": task.metadata.get("selected_execution_agent"),
                        **({"conversation_turn_id": current_turn_id} if current_turn_id else {}),
                    },
                )
            await self.store.save_task(task)
            return await self._execute_single_agent([task], preferred_external_agent)

        task_dicts = [{
            "title": original_message[:120].rstrip(),
            "description": original_message,
            "assigned_to": role_id,
            "dependencies": [],
            "tags": [],
            "priority": 5,
            "session_id": session_id,
            "project_id": project_id,
            "assigned_external_agent": preferred_external_agent,
            "metadata": {
                "mode": "task",
                "original_message": original_message,
                "workspace_root": workspace_contract.get("workspace_root"),
                "output_root": target_output_dir,
                "target_output_dir": target_output_dir,
                "comms_workspace_root": workspace_contract.get("comms_workspace_root"),
                "comms_root": workspace_contract.get("comms_root"),
                "include_project_knowledge": include_project_knowledge,
                "secretary_context": secretary_context,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "origin_task_id": origin_task_id,
                "attachment_refs": attachment_refs,
                "attachment_context": attachment_context,
                "runtime_kind": "task_mode_agent_turn",
                "work_item_role_id": role_id,
                "work_item_execution_strategy": work_item_execution_strategy,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "force_native_execution": force_native_execution,
                "task_mode_contract": "single_full_capability_main_agent",
                **({
                    "conversation_turn_id": current_turn_id,
                    "current_turn_id": current_turn_id,
                    "runtime_v2_current_turn_id": current_turn_id,
                    "runtime_v2": {"current_turn_id": current_turn_id},
                } if current_turn_id else {}),
            },
        }]

        tasks = await self.task_scheduler.create_tasks(task_dicts)
        task_ids = [task.id for task in tasks]
        serialized_plan = serialize_company_work_item_runtime_plan(work_item_plan) if work_item_plan else None
        for task in tasks:
            task.metadata = self._strip_task_mode_work_item_identity(dict(task.metadata))
            task.metadata["router_preferred_agent"] = decision.preferred_agent
            task.metadata["secretary_context"] = secretary_context
            task.metadata["execution_mode"] = decision.mode.value
            task.metadata["execution_task_ids"] = task_ids
            task.metadata["origin_task_id"] = origin_task_id or task.id
            task.metadata["runtime_kind"] = "task_mode_agent_turn"
            task.metadata["parent_session_id"] = session_id
            if current_turn_id:
                runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
                runtime_meta["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2"] = runtime_meta
                task.metadata["conversation_turn_id"] = current_turn_id
                task.metadata["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2_current_turn_id"] = current_turn_id
            task.metadata["org_version"] = self.org_engine.current_org_version()
            task.metadata["runtime_topology_version"] = self.org_engine.current_runtime_topology_version()
            task.metadata.setdefault("reorg_proposal_id", "")
            task.metadata.setdefault("migration_status", "")
            task.metadata.setdefault("superseded_by_reorg", "")
            task.org_id = getattr(decision, "org_id", None)
            if serialized_plan:
                task.metadata["company_work_item_plan"] = serialized_plan
            if self.memory and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="primary",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "execution_mode": decision.mode.value,
                        "origin_task_id": task.metadata.get("origin_task_id") or task.id,
                        "runtime_kind": "task_mode_agent_turn",
                        "selected_execution_agent": task.metadata.get("selected_execution_agent"),
                        **({"conversation_turn_id": current_turn_id} if current_turn_id else {}),
                    },
                )
            await self.store.save_task(task)

        return await self._execute_single_agent(tasks, preferred_external_agent)

    @staticmethod
    def _is_task_mode_primary_task(task: Task, *, session_id: str, project_id: str) -> bool:
        if str(getattr(task, "project_id", "") or "").strip() != project_id:
            return False
        if str(getattr(task, "session_id", "") or "").strip() != session_id:
            return False
        if getattr(task, "parent_id", None):
            return False
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        if mode == "task" or task_mode_contract == "single_full_capability_main_agent":
            return True
        exec_mode = str(task.metadata.get("exec_mode", "") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode", "") or "").strip().lower()
        return (
            exec_mode in {"task", "project", "single"}
            and execution_mode in {"", "task", "task_mode", "project"}
        )

    async def _find_reusable_task_mode_task(
        self,
        *,
        session_id: str,
        project_id: str,
        origin_task_id: str | None = None,
    ) -> Task | None:
        if not self.store or not session_id:
            return None
        getter = getattr(self.store, "get_task", None)
        if origin_task_id and callable(getter):
            origin_task = await self.store.get_task(origin_task_id)
            if origin_task and self._is_task_mode_primary_task(origin_task, session_id=session_id, project_id=project_id):
                return origin_task
        lister = getattr(self.store, "get_tasks", None)
        if not callable(lister):
            return None
        tasks = await lister(project_id=project_id)
        candidates = [
            task for task in tasks
            if self._is_task_mode_primary_task(task, session_id=session_id, project_id=project_id)
        ]
        if not candidates:
            return None
        non_terminal = [
            task for task in candidates
            if task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}
        ]
        pool = non_terminal or candidates
        pool.sort(
            key=lambda task: (
                bool(dict(task.metadata.get("runtime_v2", {}) or {}).get("runtime_session_id")),
                task.created_at,
            ),
            reverse=True,
        )
        return pool[0]

    async def _continue_company_mode_execution(
        self,
        decision: RouterDecision,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "",
        origin_thread_id: str = "",
        origin_task_id: str | None = None,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        assert self.store and self.memory
        project_id = self.project_id or "default"
        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        attachment_context = self._build_attachment_context(attachment_refs)
        workspace_contract = await self._resolve_workspace_contract(original_message, session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
        force_native_execution = decision.preferred_agent == "native"
        await self._sync_origin_task_execution_context(
            origin_task_id,
            session_id=session_id,
            decision=decision,
            workspace_contract=workspace_contract,
            original_message=original_message,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            attachment_refs=attachment_refs,
        )
        secretary_context = ""
        await self._remember_session_execution_defaults(
            session_id,
            decision,
            target_output_dir=target_output_dir,
            workspace_root=workspace_contract.get("workspace_root"),
            comms_workspace_root=workspace_contract.get("comms_workspace_root"),
            comms_root=workspace_contract.get("comms_root"),
        )
        runtime_topology = self.org_engine.build_runtime_delegation_topology() if self.org_engine else {}
        runtime_topology = self._enrich_runtime_delegation_topology(
            runtime_topology=runtime_topology,
            decision=decision,
            project_id=project_id,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        company_profile = str(
            getattr(runtime_spec, "profile", "")
            or getattr(self.config.org, "company_profile", "")
            or ""
        ).strip()
        company_work_item_plan: CompanyWorkItemRuntimePlan | None = None
        if self.org_engine:
            try:
                company_work_item_plan = self.org_engine.build_company_work_item_runtime_plan(
                    company_profile or CompanyProfile.CORPORATE.value,
                    runtime_topology=runtime_topology,
                    original_request=original_message,
                )
                runtime_topology["company_work_item_plan"] = serialize_company_work_item_runtime_plan(company_work_item_plan)
                runtime_topology["runtime_blueprint_source"] = "company_work_item_runtime_plan"
            except ValueError as exc:
                return f"Cannot execute company mode: {exc}"
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        if not final_decider_role_id:
            return "Cannot execute company mode: no final decider role is available."
        delegation_playbook = self._build_runtime_delegation_playbook(
            runtime_spec=runtime_spec,
            decision=decision,
            original_message=original_message,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        if company_work_item_plan is not None:
            delegation_playbook["company_work_item_plan"] = serialize_company_work_item_runtime_plan(company_work_item_plan)
            delegation_playbook["runtime_blueprint_source"] = "company_work_item_runtime_plan"
        seat_force_native_flags = [
            bool(seat.get("force_native_execution", False))
            for seat in list(runtime_topology.get("seats", []) or [])
            if isinstance(seat, dict)
        ]
        runtime_force_native_execution = bool(seat_force_native_flags) and all(seat_force_native_flags)
        delegation_run_id, root_work_item = await self._bootstrap_runtime_delegation_run(
            session_id=session_id,
            project_id=project_id,
            runtime_spec=runtime_spec,
            original_message=original_message,
            runtime_topology=runtime_topology,
            work_item_plan=company_work_item_plan,
            delegation_playbook=delegation_playbook,
            target_output_dir=target_output_dir,
            comms_workspace_root=str(workspace_contract.get("comms_workspace_root") or "").strip(),
            force_native_execution=runtime_force_native_execution,
        )
        root_task = await self._ensure_runtime_work_item_task(
            work_item=root_work_item,
            parent_session_id=session_id,
            original_message=original_message,
            decision=decision,
            runtime_topology=runtime_topology,
            delegation_playbook=delegation_playbook,
            secretary_context=secretary_context,
            target_output_dir=target_output_dir,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            attachment_context=attachment_context,
            force_native_execution=runtime_force_native_execution,
            root_session=True,
        )
        root_task.metadata["delegation_run_id"] = delegation_run_id
        await self.store.save_task(root_task)
        if self.on_company_runtime_children:
            self.on_company_runtime_children(session_id, [root_task.id])
        return await self._execute_company_mode([root_task], runtime_spec)

    def _build_runtime_delegation_playbook(
        self,
        *,
        runtime_spec: CompanyRuntimeSpec | None,
        decision: RouterDecision,
        original_message: str,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        playbook_cfg = getattr(getattr(self.config, "org", None), "delegation_playbook", None)
        if hasattr(playbook_cfg, "model_dump"):
            playbook: dict[str, Any] = dict(playbook_cfg.model_dump())
        elif isinstance(playbook_cfg, dict):
            playbook = dict(playbook_cfg)
        else:
            playbook = {}
        playbook.setdefault("goal", "multi_team_org")
        playbook.setdefault("runtime_model", "multi_team_org")
        playbook["work_item_driven"] = True
        playbook.setdefault("original_message", original_message)
        if runtime_spec is not None:
            playbook["company_profile"] = str(runtime_spec.profile or "corporate").strip() or "corporate"
            playbook["runtime_spec"] = serialize_company_runtime_spec(runtime_spec)
        playbook["requested_sub_tasks"] = self._normalize_sub_tasks(getattr(decision, "sub_tasks", []))
        playbook["recruitment_staffing_overrides"] = {
            str(role_id).strip(): str(employee_id).strip()
            for role_id, employee_id in dict(staffing_overrides or {}).items()
            if str(role_id).strip() and str(employee_id).strip()
        }
        playbook["recruitment_staffing_experience_modes"] = {
            str(role_id).strip(): self._normalize_staffing_experience_mode(mode)
            for role_id, mode in dict(staffing_experience_modes or {}).items()
            if str(role_id).strip()
        }
        playbook["recruitment_fallback_role_ids"] = [
            role_id
            for role_id in sorted(
                {
                    str(role_id).strip()
                    for role_id in list(fallback_role_ids or [])
                    if str(role_id).strip()
                }
            )
        ]
        playbook["recruitment_role_agent_overrides"] = {
            str(role_id).strip(): str(agent_name).strip()
            for role_id, agent_name in dict(role_agent_overrides or {}).items()
            if str(role_id).strip() and str(agent_name).strip()
        }
        return playbook

    def _build_runtime_root_description(
        self,
        *,
        original_message: str,
        decision: RouterDecision,
    ) -> str:
        sections = [
            "## Global Intent Summary",
            " ".join(str(original_message or "").split()) or "Complete the requested work.",
            "",
            "## Runtime Model",
            "Company mode is driven by work items. Create downstream work with delegate_work; completed child work returns to leaders for review and synthesis.",
        ]
        normalized_sub_tasks = self._normalize_sub_tasks(getattr(decision, "sub_tasks", []))
        if normalized_sub_tasks:
            sections.extend(
                [
                    "",
                    "## Requested Subtasks",
                    *[
                        f"{index}. {item['description']}"
                        for index, item in enumerate(normalized_sub_tasks, start=1)
                    ],
                ]
            )
        return "\n".join(sections)

    async def _prepare_project_run_context(
        self,
        *,
        project_id: str,
        session_id: str,
    ) -> tuple[DelegationRun | None, int, dict[str, Any]]:
        previous_run: DelegationRun | None = None
        if self.store is not None:
            if hasattr(self.store, "get_latest_delegation_run"):
                previous_run = await self.store.get_latest_delegation_run(
                    project_id,
                    include_session_id=session_id,
                )
            elif hasattr(self.store, "list_delegation_runs"):
                prior_runs = await self.store.list_delegation_runs(project_id=project_id)
                for candidate in prior_runs:
                    if candidate.session_id != session_id:
                        previous_run = candidate
                        break
        current_revision = 1
        if previous_run is not None:
            current_revision = max(1, int(getattr(previous_run, "current_revision", 1) or 1))
            if str(getattr(previous_run, "lifecycle_status", "") or "").strip() in {"deliverable", "delivered"}:
                current_revision += 1
        dossier: dict[str, Any] = {}
        if self.memory is not None and hasattr(self.memory, "build_project_dossier"):
            try:
                dossier = await self.memory.build_project_dossier(
                    project_id=project_id,
                    run_id=getattr(previous_run, "run_id", "") or None,
                    session_id=getattr(previous_run, "session_id", "") or None,
                )
            except Exception:
                dossier = {}
        return previous_run, current_revision, dossier

    def _enrich_runtime_delegation_topology(
        self,
        *,
        runtime_topology: dict[str, Any],
        decision: RouterDecision,
        project_id: str,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.org_engine:
            return runtime_topology
        enriched = copy.deepcopy(runtime_topology)
        fallback_roles = {
            str(role_id).strip()
            for role_id in list(fallback_role_ids or [])
            if str(role_id).strip()
        }
        explicit_agent_choice = normalize_recruitment_agent_choice(decision.preferred_agent)
        explicit_external_agent = (
            explicit_agent_choice
            if explicit_agent_choice and explicit_agent_choice != "native"
            else None
        )
        explicit_force_native = explicit_agent_choice == "native"
        seats: list[dict[str, Any]] = []
        for raw_seat in list(enriched.get("seats", []) or []):
            seat = dict(raw_seat or {})
            role_id = str(seat.get("role_id", "") or "").strip()
            if not role_id:
                seats.append(seat)
                continue
            employee_assignment: dict[str, Any] = {}
            preferred_employee_id = str((staffing_overrides or {}).get(role_id, "") or "").strip()
            selected_employee = None
            if preferred_employee_id:
                selected_employee = self.org_engine.get_employee(preferred_employee_id)
            if selected_employee is None and role_id in fallback_roles and not preferred_employee_id:
                selected_employee = self.org_engine.ensure_fallback_employee_for_role(role_id, persist=False)
            if selected_employee is None:
                selected_employee = self.org_engine.get_default_employee_for_role(role_id)
            if selected_employee is None:
                candidates = [
                    employee
                    for employee in self.org_engine.list_employees(role_id=role_id)
                    if not dict(employee.metadata or {}).get("is_fallback_employee")
                ]
                selected_employee = candidates[0] if candidates else None
            if selected_employee is not None and hasattr(self.org_engine, "_build_employee_assignment"):
                experience_mode = self._normalize_staffing_experience_mode(
                    dict(staffing_experience_modes or {}).get(role_id)
                )
                employee_assignment = self.org_engine._build_employee_assignment(  # type: ignore[attr-defined]
                    selected_employee,
                    role_id=role_id,
                    domains=[],
                    project_id=project_id,
                    experience_mode=experience_mode,
                )
            selected_role_agent = normalize_recruitment_agent_choice(
                (role_agent_overrides or {}).get(role_id)
            )
            preferred_external_agent = explicit_external_agent
            if selected_role_agent:
                preferred_external_agent = None if selected_role_agent == "native" else selected_role_agent
            elif explicit_agent_choice:
                pass
            elif not preferred_external_agent:
                preferred_external_agent = str(
                    getattr(self.org_engine.get_agent(role_id), "preferred_external_agent", "") or ""
                ).strip() or None
            force_native_execution = bool(
                selected_role_agent == "native"
                or (explicit_force_native and not selected_role_agent)
            )
            selected_execution_agent = (
                selected_role_agent
                or explicit_agent_choice
                or ("native" if force_native_execution or not preferred_external_agent else preferred_external_agent)
            )
            execution_agent_locked = bool(selected_role_agent or explicit_agent_choice)
            selection_source = (
                "recruitment_user_override"
                if selected_role_agent
                else "explicit_user_agent"
                if explicit_agent_choice
                else ""
            )
            seat["employee_id"] = str(employee_assignment.get("employee_id", "") or seat.get("employee_id", "") or "").strip()
            seat["employee_assignment"] = dict(employee_assignment or {})
            seat["preferred_external_agent"] = preferred_external_agent
            seat["selected_execution_agent"] = selected_execution_agent
            seat["execution_agent_locked"] = execution_agent_locked
            seat["selected_execution_agent_source"] = selection_source
            seat["force_native_execution"] = force_native_execution
            seat["metadata"] = {
                **dict(seat.get("metadata", {}) or {}),
                "employee_prompt_context": str((employee_assignment or {}).get("prompt_context", "")).strip(),
                "employee_delta_context": str((employee_assignment or {}).get("delta_context", "")).strip(),
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "selected_execution_agent_source": selection_source,
            }
            seats.append(seat)
        enriched["seats"] = seats
        return enriched

    @staticmethod
    def _select_runtime_work_item_seat(
        runtime_topology: dict[str, Any],
        *,
        role_id: str,
        manager_role_id: str = "",
        work_item_turn_type: str = "execute",
    ) -> dict[str, Any]:
        seats = [
            dict(seat)
            for seat in list(runtime_topology.get("seats", []) or [])
            if str(seat.get("role_id", "") or "").strip() == str(role_id or "").strip()
        ]
        if not seats:
            return {}
        if work_item_turn_type in {"intake", "dispatch", "plan", "aggregate", "deliver"}:
            lead_seat = next((seat for seat in seats if bool(seat.get("is_team_lead", False))), None)
            if lead_seat is not None:
                return lead_seat
        if manager_role_id:
            manager_match = next(
                (
                    seat
                    for seat in seats
                    if str(seat.get("manager_role_id", "") or "").strip() == manager_role_id
                ),
                None,
            )
            if manager_match is not None:
                return manager_match
        lead_seat = next((seat for seat in seats if bool(seat.get("is_team_lead", False))), None)
        if lead_seat is not None:
            return lead_seat
        return seats[0]

    async def _bootstrap_runtime_delegation_run(
        self,
        *,
        session_id: str,
        project_id: str,
        runtime_spec: CompanyRuntimeSpec | None,
        original_message: str,
        runtime_topology: dict[str, Any],
        work_item_plan: CompanyWorkItemRuntimePlan | None,
        delegation_playbook: dict[str, Any],
        target_output_dir: str | None,
        comms_workspace_root: str,
        force_native_execution: bool,
    ) -> tuple[str, DelegationWorkItem]:
        assert self.store and self.org_engine
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        previous_run, current_revision, project_dossier = await self._prepare_project_run_context(
            project_id=project_id,
            session_id=session_id,
        )
        run = DelegationRun(
            project_id=project_id,
            session_id=session_id,
            company_profile=str(
                runtime_topology.get("company_profile", "")
                or (runtime_spec.profile if runtime_spec is not None else "")
                or getattr(self.config.org, "company_profile", "corporate")
            ),
            execution_model="multi_team_org",
            final_decider_role_id=final_decider_role_id,
            top_level_role_ids=list(runtime_topology.get("top_level_role_ids", []) or self.org_engine.get_top_level_role_ids()),
            status="running",
            lifecycle_status="active",
            current_revision=current_revision,
            latest_deliverable_summary=str(getattr(previous_run, "latest_deliverable_summary", "") or "").strip(),
            recovery_pointer={
                "status": "bootstrapping",
                "session_id": session_id,
                "project_id": project_id,
            },
            project_dossier=dict(project_dossier or {}),
            metadata=mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "source": "multi_team_org",
                "runtime_spec": serialize_company_runtime_spec(runtime_spec) if runtime_spec is not None else {},
                "delegation_playbook": dict(delegation_playbook),
                "runtime_topology": copy.deepcopy(runtime_topology),
                "company_work_item_plan": serialize_company_work_item_runtime_plan(work_item_plan),
                "org_snapshot": copy.deepcopy(runtime_topology),
                "target_output_dir": target_output_dir,
                "comms_workspace_root": comms_workspace_root,
                "force_native_execution": force_native_execution,
                "continuation_of_run_id": str(getattr(previous_run, "run_id", "") or "").strip(),
                "continuation_of_session_id": str(getattr(previous_run, "session_id", "") or "").strip(),
                "loaded_from_dossier": bool(project_dossier),
            }),
        )
        await self.store.save_delegation_run(run)
        if previous_run is not None and session_id != previous_run.session_id and hasattr(self.store, "save_session_link"):
            await self.store.save_session_link(
                SessionLinkRecord(
                    project_id=project_id,
                    session_id=session_id,
                    linked_session_id=previous_run.session_id,
                    link_type="continuation_of",
                    metadata={
                        "run_id": run.run_id,
                        "linked_run_id": previous_run.run_id,
                    },
                )
            )
            if str(getattr(previous_run, "lifecycle_status", "") or "").strip() in {"deliverable", "delivered"} or str(getattr(previous_run, "latest_deliverable_summary", "") or "").strip():
                await self.store.save_session_link(
                    SessionLinkRecord(
                        project_id=project_id,
                        session_id=session_id,
                        linked_session_id=previous_run.session_id,
                        link_type="revision_of",
                        metadata={
                            "run_id": run.run_id,
                            "linked_run_id": previous_run.run_id,
                            "revision": current_revision,
                        },
                    )
                )

        teams = [dict(item) for item in list(runtime_topology.get("teams", []) or []) if isinstance(item, dict)]
        seats = [dict(item) for item in list(runtime_topology.get("seats", []) or []) if isinstance(item, dict)]
        team_instance_ids: dict[str, str] = {}
        seats_by_id: dict[str, dict[str, Any]] = {}

        for team in teams:
            team_id = str(team.get("team_id", "") or "").strip()
            if not team_id:
                continue
            team_instance_id = str(team.get("team_instance_id", "") or f"team-instance::{run.run_id}::{team_id}").strip()
            team_instance_ids[team_id] = team_instance_id
            await self.store.save_team_instance(
                TeamInstance(
                    team_instance_id=team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=team_id,
                    session_id=session_id,
                    status="active",
                    seat_ids=[str(item).strip() for item in list(team.get("member_seat_ids", []) or []) if str(item).strip()],
                    role_ids=[
                        str(item).strip()
                        for item in {
                            str(team.get("lead_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(team.get("member_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    metadata=mark_work_item_runtime({
                        "parent_team_id": str(team.get("parent_team_id", "") or "").strip(),
                        "lead_role_id": str(team.get("lead_role_id", "") or "").strip(),
                        **dict(team.get("metadata", {}) or {}),
                    }),
                )
            )
            await self.store.save_delegation_cell(
                DelegationCell(
                    cell_id=team_id,
                    run_id=run.run_id,
                    manager_role_id=str(team.get("lead_role_id", "") or "").strip(),
                    member_role_ids=[str(item).strip() for item in list(team.get("member_role_ids", []) or []) if str(item).strip()],
                    status="active",
                    metadata=mark_work_item_runtime({
                        "team_id": team_id,
                        "team_instance_id": team_instance_id,
                        "parent_team_id": str(team.get("parent_team_id", "") or "").strip(),
                        "lead_role_id": str(team.get("lead_role_id", "") or "").strip(),
                        "member_seat_ids": list(team.get("member_seat_ids", []) or []),
                        **dict(team.get("metadata", {}) or {}),
                    }),
                )
            )

        for seat in seats:
            seat_id = str(seat.get("seat_id", "") or "").strip()
            role_id = str(seat.get("role_id", "") or "").strip()
            if not seat_id or not role_id:
                continue
            team_id = str(seat.get("team_id", "") or "").strip()
            employee_id = str(seat.get("employee_id", "") or "").strip() or f"{role_id}-default-session"
            team_instance_id = team_instance_ids.get(team_id, "")
            seat_state_id = str(seat.get("seat_state_id", "") or f"seat-state::{run.run_id}::{seat_id}").strip()
            # Role-instance model: one role → one role_runtime_session_id, shared across seats.
            # Fix 2: canonical fallback includes team_instance so parallel
            # role instances in the same run don't collide on a short ID.
            role_runtime_session_id = (
                str(seat.get("role_runtime_session_id", "") or "").strip()
                or canonical_role_session_id(
                    run_id=run.run_id,
                    role_id=role_id,
                    team_instance_id=team_instance_id,
                )
            )
            seats_by_id[seat_id] = {
                **seat,
                "seat_state_id": seat_state_id,
                "role_runtime_session_id": role_runtime_session_id,
                "team_instance_id": team_instance_id,
            }
            await self.store.save_seat_state(
                SeatState(
                    seat_state_id=seat_state_id,
                    team_instance_id=team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=team_id,
                    seat_id=seat_id,
                    role_id=role_id,
                    employee_id=employee_id,
                    member_session_id=f"role-session::{project_id}::{role_id}",
                    role_runtime_session_id=role_runtime_session_id,
                    status="idle",
                    resident_status="idle",
                    manager_role_id=str(seat.get("manager_role_id", "") or "").strip(),
                    manager_seat_id=str(seat.get("manager_seat_id", "") or "").strip(),
                    manager_role_ids=[
                        str(item).strip()
                        for item in {
                            str(seat.get("manager_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(seat.get("contact_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    manager_seat_ids=[
                        str(item).strip()
                        for item in [str(seat.get("manager_seat_id", "") or "").strip()]
                        if str(item).strip()
                    ],
                    metadata=mark_work_item_runtime({
                        "managed_team_id": str(seat.get("managed_team_id", "") or "").strip(),
                        "allowed_delegate_role_ids": list(seat.get("allowed_delegate_role_ids", []) or []),
                        "contact_role_ids": list(seat.get("contact_role_ids", []) or []),
                        **dict(seat.get("metadata", {}) or {}),
                    }),
                )
            )
            await self.store.save_delegation_role_session(
                DelegationRoleSession(
                    role_session_id=role_runtime_session_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_instance_id=team_instance_id,
                    team_id=team_id,
                    role_id=role_id,
                    seat_id=seat_id,
                    seat_state_id=seat_state_id,
                    employee_id=employee_id,
                    manager_role_ids=[
                        str(item).strip()
                        for item in {
                            str(seat.get("manager_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(seat.get("contact_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    manager_seat_ids=[
                        str(item).strip()
                        for item in [str(seat.get("manager_seat_id", "") or "").strip()]
                        if str(item).strip()
                    ],
                    seat_ids=[seat_id],
                    status="idle",
                    metadata=mark_work_item_runtime({
                        "shared_role_executor": bool(seat.get("shared_executor", True)),
                        "final_decider_role_id": final_decider_role_id,
                        "managed_team_id": str(seat.get("managed_team_id", "") or "").strip(),
                        "contact_role_ids": list(seat.get("contact_role_ids", []) or []),
                    }),
                )
            )

        final_decider_seat = next(
            (
                seat
                for seat in seats_by_id.values()
                if str(seat.get("role_id", "") or "").strip() == final_decider_role_id
            ),
            {},
        )
        root_team_id = str(final_decider_seat.get("team_id", "") or f"team::{final_decider_role_id}").strip()
        root_seat_id = str(final_decider_seat.get("seat_id", "") or f"seat::{root_team_id}::{final_decider_role_id}").strip()
        root_team_instance_id = str(final_decider_seat.get("team_instance_id", "") or team_instance_ids.get(root_team_id, f"team-instance::{run.run_id}::{root_team_id}")).strip()
        root_seat_state_id = str(final_decider_seat.get("seat_state_id", "") or f"seat-state::{run.run_id}::{root_seat_id}").strip()
        # Role-instance model: role_runtime_session_id is keyed by role, not seat.
        # Fix 2: canonical fallback with team_instance slot.
        root_role_runtime_id = (
            str(final_decider_seat.get("role_runtime_session_id", "") or "").strip()
            or canonical_role_session_id(
                run_id=run.run_id,
                role_id=final_decider_role_id,
                team_instance_id=root_team_instance_id,
            )
        )

        if root_team_id not in team_instance_ids:
            team_instance_ids[root_team_id] = root_team_instance_id
            await self.store.save_team_instance(
                TeamInstance(
                    team_instance_id=root_team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=root_team_id,
                    session_id=session_id,
                    status="active",
                    seat_ids=[root_seat_id],
                    role_ids=[final_decider_role_id],
                    metadata=mark_work_item_runtime(),
                )
            )
            await self.store.save_delegation_cell(
                DelegationCell(
                    cell_id=root_team_id,
                    run_id=run.run_id,
                    manager_role_id=final_decider_role_id,
                    member_role_ids=[final_decider_role_id],
                    status="active",
                    metadata=mark_work_item_runtime({"team_instance_id": root_team_instance_id}),
                )
            )
        if root_seat_id not in seats_by_id:
            await self.store.save_seat_state(
                SeatState(
                    seat_state_id=root_seat_state_id,
                    team_instance_id=root_team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=root_team_id,
                    seat_id=root_seat_id,
                    role_id=final_decider_role_id,
                    employee_id=f"{final_decider_role_id}-default-session",
                    member_session_id=f"role-session::{project_id}::{final_decider_role_id}",
                    role_runtime_session_id=root_role_runtime_id,
                    status="idle",
                    resident_status="idle",
                    metadata=mark_work_item_runtime(),
                )
            )
            await self.store.save_delegation_role_session(
                DelegationRoleSession(
                    role_session_id=root_role_runtime_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_instance_id=root_team_instance_id,
                    team_id=root_team_id,
                    role_id=final_decider_role_id,
                    seat_id=root_seat_id,
                    seat_state_id=root_seat_state_id,
                    employee_id=f"{final_decider_role_id}-default-session",
                    seat_ids=[root_seat_id],
                    status="idle",
                    metadata=mark_work_item_runtime({
                        "final_decider_role_id": final_decider_role_id,
                    }),
                )
            )

        final_decider = self.org_engine.get_agent(final_decider_role_id)
        root_title = (
            f"{getattr(final_decider, 'name', final_decider_role_id)} Intake"
            if final_decider is not None
            else "Runtime Delegation Intake"
        )
        dynamic_root_team_instance_id = f"team-instance::{run.run_id}::{root_seat_id}::root"
        plan_payload = serialize_company_work_item_runtime_plan(work_item_plan)
        root_work_item = DelegationWorkItem(
            run_id=run.run_id,
            cell_id=root_team_id,
            team_instance_id=dynamic_root_team_instance_id,
            team_id=root_team_id,
            role_id=final_decider_role_id,
            seat_id=root_seat_id,
            seat_state_id=root_seat_state_id,
            role_runtime_session_id=root_role_runtime_id,
            title=root_title,
            summary=original_message,
            kind="intake",
            phase=Phase.READY,
            batch_id=f"batch::{run.run_id}::0",
            batch_index=0,
            continuation_source=str(getattr(previous_run, "run_id", "") or "").strip(),
            metadata=mark_work_item_projection(mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "session_scope_id": session_id,
                "team_id": root_team_id,
                "team_instance_id": dynamic_root_team_instance_id,
                "seat_id": root_seat_id,
                "seat_state_id": root_seat_state_id,
                "work_kind": "intake",
                "dependency_work_item_ids": [],
                "batch_id": f"batch::{run.run_id}::0",
                "created_by_seat_id": root_seat_id,
                "assigned_role_runtime_id": root_role_runtime_id,
                "delegation_playbook": dict(delegation_playbook),
                "project_dossier": dict(project_dossier or {}),
                "contact_role_ids": list(
                    dict(final_decider_seat or {}).get("contact_role_ids", [])
                    or []
                ),
                "allowed_delegate_role_ids": list(
                    dict(final_decider_seat or {}).get("allowed_delegate_role_ids", [])
                    or []
                ),
                "target_output_dir": target_output_dir,
                "comms_workspace_root": comms_workspace_root,
                "authoritative_output": True,
                "user_visible": True,
            }), turn_type="intake"),
        )
        root_projection_id = str(
            (work_item_plan.root_projection_id if work_item_plan is not None else "")
            or plan_payload.get("root_projection_id", "")
            or ""
        ).strip()
        root_work_item.projection_id = root_projection_id or root_work_item.work_item_id
        root_work_item.metadata = mark_work_item_projection(
            root_work_item.metadata,
            projection_id=root_work_item.projection_id,
            turn_type="intake",
        )
        await self.store.save_delegation_work_item(root_work_item)
        await self.store.save_team_instance(
            TeamInstance(
                team_instance_id=dynamic_root_team_instance_id,
                run_id=run.run_id,
                project_id=project_id,
                team_id=root_team_id,
                session_id=session_id,
                status="active",
                seat_ids=[
                    str(item.get("seat_id", "") or "").strip()
                    for item in list(runtime_topology.get("seats", []) or [])
                    if str(item.get("team_id", "") or "").strip() == root_team_id and str(item.get("seat_id", "") or "").strip()
                ] or [root_seat_id],
                role_ids=[
                    str(item.get("role_id", "") or "").strip()
                    for item in list(runtime_topology.get("seats", []) or [])
                    if str(item.get("team_id", "") or "").strip() == root_team_id and str(item.get("role_id", "") or "").strip()
                ] or [final_decider_role_id],
                metadata=mark_work_item_runtime({
                    "runtime_model": "multi_team_org",
                    "manager_seat_id": root_seat_id,
                    "parent_work_item_id": root_work_item.work_item_id,
                    "root_team": True,
                }),
            )
        )
        run.metadata = dict(run.metadata or {})
        run.metadata["root_work_item_id"] = root_work_item.work_item_id
        run.metadata["root_team_instance_id"] = dynamic_root_team_instance_id
        if work_item_plan is not None:
            run.metadata["company_work_item_plan"] = serialize_company_work_item_runtime_plan(work_item_plan)
        await self.store.save_delegation_run(run)
        if hasattr(self.store, "save_delegation_event"):
            await self.store.save_delegation_event(
                DelegationEvent(
                    run_id=run.run_id,
                    work_item_id=root_work_item.work_item_id,
                    cell_id=root_work_item.cell_id,
                    role_id=root_work_item.role_id,
                    event_type="work_item_created",
                    payload={
                        "work_item_runtime": True,
                        **work_item_identity_payload(projection_id=root_work_item.projection_id, turn_type="intake"),
                        "team_id": root_team_id,
                        "seat_id": root_seat_id,
                        "batch_id": root_work_item.batch_id,
                        "work_kind": "intake",
                        "title": root_title,
                    },
                )
            )
        return run.run_id, root_work_item

    @staticmethod
    def _uses_shared_role_session(task: Task | None) -> bool:
        if task is None:
            return False
        return bool(dict(getattr(task, "metadata", {}) or {}).get("shared_role_session", False))

    @staticmethod
    def _shared_company_role_session_id(
        parent_session_id: str,
        role_id: str,
        *,
        final_decider_role_id: str = "",
        root_session: bool = False,
    ) -> str:
        parent_sid = str(parent_session_id or "").strip()
        normalized_role = str(role_id or "").strip()
        final_role = str(final_decider_role_id or "").strip()
        if root_session or (parent_sid and final_role and normalized_role == final_role):
            return parent_sid or normalized_role or str(uuid.uuid4())
        if parent_sid and normalized_role:
            return f"{parent_sid}:role:{normalized_role}"
        return parent_sid or normalized_role or str(uuid.uuid4())

    async def _ensure_runtime_work_item_task(
        self,
        *,
        work_item: DelegationWorkItem,
        parent_session_id: str,
        original_message: str,
        decision: RouterDecision,
        runtime_topology: dict[str, Any],
        delegation_playbook: dict[str, Any],
        secretary_context: str,
        target_output_dir: str | None,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None,
        attachment_refs: list[dict[str, Any]] | None,
        attachment_context: str,
        force_native_execution: bool,
        root_session: bool = False,
    ) -> Task:
        assert self.store and self.memory
        role_id = str(work_item.role_id or "").strip()
        seat_id = str((work_item.metadata or {}).get("seat_id", "") or "").strip()
        team_id = str((work_item.metadata or {}).get("team_id", "") or work_item.cell_id or "").strip()
        work_kind = str((work_item.metadata or {}).get("work_kind", "") or work_item.kind or "execute").strip().lower() or "execute"
        work_item_projection_id = projection_id_for_work_item(work_item)
        legacy_turn_type = turn_type_for_work_item(work_item, fallback="")
        mapped_turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
        work_item_turn_type = (
            legacy_turn_type
            if legacy_turn_type in {"intake", "dispatch", "plan", "setup", "execute", "review", "report", "aggregate", "deliver"}
            else mapped_turn_type
        )
        work_item_projection_ref = work_item_projection_id
        topology_seat = next(
            (
                dict(seat)
                for seat in list(runtime_topology.get("seats", []) or [])
                if str(seat.get("seat_id", "") or "").strip() == seat_id
            ),
            {},
        )
        # Fix 2: canonical fallback. assigned_role_runtime_id on metadata
        # is usually populated; we only land in the generator when a stale
        # work item predates that seeding.
        role_session_id = (
            str((work_item.metadata or {}).get("assigned_role_runtime_id", "") or "").strip()
            or canonical_role_session_id(
                run_id=str(work_item.run_id or "").strip(),
                role_id=role_id,
                team_instance_id=str(getattr(work_item, "team_instance_id", "") or "").strip(),
            )
        )
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        session_id = self._shared_company_role_session_id(
            parent_session_id,
            role_id,
            final_decider_role_id=final_decider_role_id,
            root_session=root_session,
        )
        session_title = (
            str((topology_seat.get("metadata", {}) or {}).get("role_name", "") or "").strip()
            or role_id
            or str(work_item.title or work_item_projection_ref or "Runtime Work Item").strip()
        )
        existing = None
        get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
        if callable(get_runtime_task):
            existing = await get_runtime_task(work_item.work_item_id)
        if existing is not None:
            set_linked_work_item_id(existing, work_item.work_item_id)
            existing.session_id = session_id
            existing.metadata = dict(existing.metadata or {})
            existing.metadata["shared_role_session"] = True
            existing.metadata["shared_role_id"] = role_id
            existing.metadata["company_runtime_root_session_id"] = parent_session_id
            existing.metadata = mark_work_item_projection(
                existing.metadata,
                projection_id=work_item_projection_id,
                turn_type=work_item_turn_type,
            )
            issues = [
                issue for issue in validate_work_item_runtime_projection(existing, work_item)
                if issue.severity == "error"
            ]
            if issues:
                raise RuntimeError(
                    "work-item runtime invariant failed for root runtime Task "
                    f"{existing.id}: "
                    + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                )
            await self.memory.ensure_session(
                existing.session_id,
                project_id=existing.project_id,
                title=session_title,
                mode="primary",
                parent_session_id=None,
                metadata={
                    "task_id": existing.id,
                    **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
                    "work_item_id": work_item.work_item_id,
                    "role_id": role_id,
                    "seat_id": seat_id,
                    "origin_session_id": parent_session_id,
                    "origin_channel": origin_channel,
                    "origin_chat_id": origin_chat_id,
                    "origin_thread_id": origin_thread_id,
                    "shared_role_session": True,
                    "shared_role_id": role_id,
                    "company_runtime_root_session_id": parent_session_id,
                },
            )
            await self.store.save_task(existing)
            return existing
        employee_assignment = dict(topology_seat.get("employee_assignment", {}) or {})
        if not employee_assignment and self.org_engine and role_id:
            preferred_employee_id = str(topology_seat.get("employee_id", "") or "").strip() or None
            resolved_assignment = self.org_engine.resolve_employee_for_work_item(
                role_id,
                [],
                project_id=self.project_id or "default",
                preferred_employee_id=preferred_employee_id,
            )
            employee_assignment = dict(resolved_assignment or {})
            if employee_assignment:
                topology_seat["employee_id"] = str(employee_assignment.get("employee_id", "") or "").strip()
                topology_seat["employee_assignment"] = dict(employee_assignment)
                for index, raw_seat in enumerate(list(runtime_topology.get("seats", []) or [])):
                    seat_entry = dict(raw_seat or {})
                    if str(seat_entry.get("seat_id", "") or "").strip() != seat_id:
                        continue
                    seat_entry["employee_id"] = topology_seat["employee_id"]
                    seat_entry["employee_assignment"] = dict(employee_assignment)
                    runtime_topology["seats"][index] = seat_entry
                    break
        preferred_external_agent = (
            str(topology_seat.get("preferred_external_agent", "") or "").strip()
            or str((employee_assignment or {}).get("preferred_external_agent", "") or "").strip()
            or None
        )
        selected_execution_agent, assigned_external_agent, role_force_native_execution = (
            resolve_effective_execution_agent(
                topology_seat.get("selected_execution_agent"),
                preferred_external_agent,
                force_native_execution=bool(topology_seat.get("force_native_execution", False)),
            )
        )
        resolved_force_native_execution = bool(force_native_execution or role_force_native_execution)
        if resolved_force_native_execution:
            selected_execution_agent = "native"
            assigned_external_agent = None
        preferred_external_agent = assigned_external_agent
        work_item.metadata = dict(work_item.metadata or {})
        if employee_assignment:
            work_item.metadata["employee_assignment"] = copy.deepcopy(employee_assignment)
        prompt_ctx = str((employee_assignment or {}).get("prompt_context", "") or "").strip()
        if prompt_ctx:
            work_item.metadata["employee_prompt_context"] = prompt_ctx
        delta_ctx = str((employee_assignment or {}).get("delta_context", "") or "").strip()
        if delta_ctx:
            work_item.metadata["employee_delta_context"] = delta_ctx
        owner_execution_copy = build_work_item_owner_execution_copy(work_item)
        owner_execution_copy.setdefault("delegation_role_session_id", role_session_id)
        owner_execution_copy["work_kind"] = work_item_turn_type
        task = Task(
            title=str(work_item.title or work_item_projection_ref or "Runtime Work Item").strip(),
            description=(
                self._build_runtime_root_description(original_message=original_message, decision=decision)
                if root_session
                else str(work_item.summary or original_message or "").strip()
            ),
            assigned_to=role_id,
            status=TaskStatus.PENDING,
            project_id=self.project_id or "default",
            session_id=session_id,
            parent_session_id=parent_session_id,
            assigned_external_agent=assigned_external_agent,
            metadata=mark_work_item_projection(mark_work_item_runtime({
                "mode": "company",
                "execution_mode": decision.mode.value,
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "original_message": original_message,
                "router_preferred_agent": decision.preferred_agent,
                "company_profile": decision.company_profile or getattr(self.config.org, "company_profile", "corporate"),
                "organization_id": getattr(self.config.org, "organization_id", ""),
                "organization_name": getattr(self.config.org, "organization_name", ""),
                "organization_config_file": getattr(self.config.org, "organization_config_file", ""),
                "delegation_playbook": dict(delegation_playbook),
                "recruitment_staffing_overrides": dict(
                    (delegation_playbook or {}).get("recruitment_staffing_overrides", {}) or {}
                ),
                "recruitment_fallback_role_ids": list(
                    (delegation_playbook or {}).get("recruitment_fallback_role_ids", []) or []
                ),
                "recruitment_role_agent_overrides": dict(
                    (delegation_playbook or {}).get("recruitment_role_agent_overrides", {}) or {}
                ),
                "runtime_topology": copy.deepcopy(runtime_topology),
                **owner_execution_copy,
                "work_item_projection_ref": work_item_projection_ref,
                "seat_manager_role_id": str(topology_seat.get("manager_role_id", "") or "").strip(),
                "manager_role_id": str(topology_seat.get("manager_role_id", "") or "").strip(),
                "manager_seat_id": str(topology_seat.get("manager_seat_id", "") or "").strip(),
                "managed_team_id": str(topology_seat.get("managed_team_id", "") or "").strip(),
                "seat_contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                "force_native_execution": resolved_force_native_execution,
                "employee_assignment": dict(employee_assignment or {}),
                "employee_prompt_context": str((employee_assignment or {}).get("prompt_context", "")).strip(),
                "employee_delta_context": str((employee_assignment or {}).get("delta_context", "")).strip(),
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
                "execution_task_ids": [work_item.work_item_id],
                "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
                "parent_session_id": parent_session_id,
                "origin_task_id": origin_task_id,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "attachment_refs": list(attachment_refs or []),
                "attachment_context": attachment_context,
                "secretary_context": secretary_context,
                "include_project_knowledge": self._requests_explicit_project_knowledge(original_message),
                "target_output_dir": target_output_dir,
                "output_root": target_output_dir,
                "workspace_root": str((work_item.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_workspace_root": str((work_item.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_root": str((work_item.metadata or {}).get("comms_root", "") or "").strip(),
                "org_version": self.org_engine.current_org_version() if self.org_engine else 1,
                "org_runtime_version": self.org_engine.current_runtime_topology_version() if self.org_engine else 1,
                "user_visible": bool((work_item.metadata or {}).get("user_visible", False)),
                "authoritative_output": bool((work_item.metadata or {}).get("authoritative_output", False)),
                "shared_role_session": True,
                "shared_role_id": role_id,
                "company_runtime_root_session_id": parent_session_id,
            }), projection_id=work_item_projection_id, turn_type=work_item_turn_type),
        )
        set_linked_work_item_id(task, work_item.work_item_id)
        await self.store.save_delegation_work_item(work_item)
        ensure_runtime_task = getattr(self.store, "ensure_runtime_task_for_work_item", None)
        if callable(ensure_runtime_task):
            task = await ensure_runtime_task(work_item, lambda task=task: task)
        else:
            await self.store.save_task(task)
            link_runtime_task = getattr(self.store, "link_work_item_runtime_task", None)
            if callable(link_runtime_task):
                linked = await link_runtime_task(work_item.work_item_id, task.id)
                if not linked:
                    raise RuntimeError(
                        "failed to link new runtime Task "
                        f"{task.id} for WorkItem {work_item.work_item_id}"
                    )
        set_linked_work_item_id(task, work_item.work_item_id)
        issues = [
            issue for issue in validate_work_item_runtime_projection(task, work_item)
            if issue.severity == "error"
        ]
        if issues:
            raise RuntimeError(
                "work-item runtime invariant failed for root runtime Task "
                f"{task.id}: "
                + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
            )
        await self.memory.ensure_session(
            task.session_id,
            project_id=task.project_id,
            title=session_title,
            mode="primary",
            parent_session_id=None,
            metadata={
                "task_id": task.id,
                **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
                "work_item_id": work_item.work_item_id,
                "role_id": role_id,
                "seat_id": seat_id,
                "origin_session_id": task.parent_session_id,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "shared_role_session": True,
                "shared_role_id": role_id,
                "company_runtime_root_session_id": parent_session_id,
            },
        )
        return task

    @staticmethod
    def _runtime_work_kind_to_work_item_turn_type(work_kind: str) -> str:
        return canonical_work_item_turn_type_for_kind(work_kind)

    async def _execute_decision(
        self,
        decision: RouterDecision,
        original_message: str,
        context: Any | None = None,
        *,
        session_id: str | None = None,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        conversation_turn_id: str | None = None,
    ) -> str:
        """Build tasks and run them based on mode selection (project / company)."""
        assert self.task_scheduler and self.store

        project_id = self.project_id or "default"
        primary_session_id = session_id or str(uuid.uuid4())
        await self._ensure_primary_session(primary_session_id, original_message)
        workspace_contract = await self._resolve_workspace_contract(original_message, primary_session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
        await self._remember_session_execution_defaults(
            primary_session_id,
            decision,
            target_output_dir=target_output_dir,
            workspace_root=workspace_contract.get("workspace_root"),
            comms_workspace_root=workspace_contract.get("comms_workspace_root"),
            comms_root=workspace_contract.get("comms_root"),
        )

        if decision.mode == ExecutionMode.COMPANY_MODE:
            assert self.company_runtime_spec_builder
            setup_error = self.org_engine.validate_company_runtime_setup() if self.org_engine else None
            if setup_error:
                return f"Cannot execute company mode: {setup_error}"
            runtime_spec = self.company_runtime_spec_builder.build_spec(
                decision,
                original_message=original_message,
            )

            # Budget check for company mode
            org_id = getattr(decision, "org_id", None)
            if org_id and self.store:
                from opc.layer6_observability.cost_tracker import check_budget
                allowed, reason = await check_budget(self.store, org_id=org_id)
                if not allowed:
                    return f"Cannot execute: {reason}"

            force_manual_preflight = (
                str(dict(getattr(decision, "metadata", {}) or {}).get("company_preflight", "") or "")
                .strip()
                .lower()
                == "manual"
            )
            return await self._begin_company_staffing_loop(
                decision,
                original_message,
                runtime_spec,
                session_id=primary_session_id,
                origin_channel=context.default_channel if context else "cli",
                origin_chat_id=context.origin_chat_id if context else "",
                origin_thread_id=context.origin_thread_id if context else "",
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_manual_preflight=force_manual_preflight,
            )
        return await self._continue_task_mode_execution(
            decision,
            original_message,
            None,
            session_id=primary_session_id,
            origin_channel=context.default_channel if context else "cli",
            origin_chat_id=context.origin_chat_id if context else "",
            origin_thread_id=context.origin_thread_id if context else "",
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            conversation_turn_id=conversation_turn_id,
        )

    async def _emit_external_agent_audit(
        self,
        task: Task,
        metadata: dict[str, Any],
        workspace: str,
        progress_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        agent = metadata.get("agent", task.assigned_external_agent or "external")
        model = metadata.get("model", "(cli default)")
        session_mode = metadata.get("session_mode", "auto")
        new_session = metadata.get("new_session", False)
        command = metadata.get("display_command") or metadata.get("command", "")
        if isinstance(command, str) and ("\n" in command or len(command) > 260):
            command = f"<command:{len(command)}-chars>"
        target_output_dir = metadata.get("target_output_dir", "")
        header = (
            f"[Delegating to {agent}] task={task.title} | model={model} | "
            f"session_mode={session_mode} | new_session={new_session} | "
            f"workspace={workspace}"
        )
        if target_output_dir:
            header += f" | target_output_dir={target_output_dir}"
        header += f" | cmd={command}"
        logger.debug(header)
        if progress_callback:
            await progress_callback(header)

    def _should_force_native_execution(self, task: Task) -> bool:
        return str(task.metadata.get("router_preferred_agent") or "").strip().lower() == "native" or bool(
            task.metadata.get("force_native_execution")
        )

    def _available_external_agents(self) -> list[str]:
        if not self.adapter_registry:
            return []
        return self.adapter_registry.list_available()

    def _requests_explicit_project_knowledge(self, message: str) -> bool:
        normalized = " ".join(str(message or "").strip().lower().split())
        if not normalized:
            return False
        markers = (
            "参考这个 project 的已确认决策",
            "参考项目知识",
            "引用项目知识",
            "导入项目知识",
            "参考已确认决策",
            "导入已确认决策",
            "project knowledge",
            "confirmed project decisions",
            "confirmed project knowledge",
            "import project knowledge",
            "reference project knowledge",
        )
        return any(marker in normalized for marker in markers)

    def _get_role_runtime_value(self, role: Any | None, key: str, default: str = "") -> str:
        if not role:
            return default
        policy = getattr(role, "runtime_policy", {}) or {}
        if isinstance(policy, dict):
            value = policy.get(key, default)
        else:
            value = getattr(policy, key, default)
        return str(value or default)

    def _resolve_task_role(self, task: Task) -> Any:
        assert self.org_engine
        role_id = task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        role = (
            self.org_engine.get_role_for_work_item(role_id, [])
            if role_id
            else self.org_engine.get_executor()
        )
        task.assigned_to = role.role_id
        return role

    @staticmethod
    async def _invoke_progress_callback(
        callback: Callable[[str], Coroutine[Any, Any, None]] | None,
        text: str,
        **kw: Any,
    ) -> None:
        if not callback:
            return
        if not kw:
            await callback(text)
            return
        try:
            await callback(text, **kw)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            await callback(text)

    def _resolve_progress_identity(self, task: Task) -> tuple[str, str, str]:
        origin_task_id = str(task.metadata.get("origin_task_id", "") or "").strip()
        is_child_session = bool(
            task.parent_session_id
            and task.session_id
            and task.session_id != task.parent_session_id
            and not self._uses_shared_role_session(task)
        )
        is_distinct_work_item_projection_title = bool(origin_task_id and origin_task_id != task.id)
        progress_task_id = task.id if (is_child_session or is_distinct_work_item_projection_title) else (origin_task_id or task.id)
        agent_role_id = task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        employee_assignment = dict(task.metadata.get("employee_assignment", {}) or {})
        agent_name = str(employee_assignment.get("name", "")).strip()
        if not agent_name and self.org_engine and agent_role_id:
            role = self.org_engine.get_role_for_work_item(agent_role_id, task.tags)
            agent_name = str(getattr(role, "name", "") or "").strip()
        return progress_task_id, agent_role_id, agent_name

    def _make_task_progress_callback(
        self,
        task: Task,
    ) -> Callable[[str], Coroutine[Any, Any, None]] | None:
        base_cb = self.on_progress
        if not base_cb:
            return None

        progress_task_id, agent_role_id, agent_name = self._resolve_progress_identity(task)

        async def _callback(text: str, **kw: Any) -> None:
            kw["task_id"] = progress_task_id
            if agent_role_id:
                kw.setdefault("agent_role_id", agent_role_id)
            if agent_name:
                kw.setdefault("agent_name", agent_name)
            await self._invoke_progress_callback(base_cb, text, **kw)

        return _callback

    def _reregister_company_runtime_children(
        self,
        tasks: list[Task],
        *,
        checkpoint_session_id: str | None = None,
    ) -> None:
        """Re-register child task IDs with WSHandler for progress dual-routing.

        During checkpoint resume / runtime re-execution, the original
        ``_active_runtimes`` mapping has been cleaned up. This helper
        re-registers all task IDs so that ``work_item_progress`` events
        emitted by ``_ceo_initiate_rework`` or the execution loop are
        correctly routed to the parent session's UI channel.

        The parent session ID is resolved from the tasks themselves
        (``parent_session_id`` field), falling back to the checkpoint's
        ``session_id``.
        """
        if not self.on_company_runtime_children or not tasks:
            return
        parent_session_id: str | None = None
        for task in tasks:
            candidate = str(getattr(task, "parent_session_id", "") or "").strip()
            if not candidate:
                candidate = str(task.metadata.get("parent_session_id", "") or "").strip()
            if candidate:
                parent_session_id = candidate
                break
        if not parent_session_id:
            parent_session_id = checkpoint_session_id
        if not parent_session_id:
            return
        self.on_company_runtime_children(parent_session_id, [t.id for t in tasks])

    @staticmethod
    def _external_approval_info(result: TaskResult) -> dict[str, Any]:
        artifacts = result.artifacts or {}
        approval = artifacts.get("approval", {})
        return dict(approval) if isinstance(approval, dict) else {}

    def _external_result_requires_user_review(self, result: TaskResult) -> bool:
        if result.status in _REVIEW_WAITING_STATUSES:
            return True
        artifacts = result.artifacts or {}
        return bool(artifacts.get("requires_user_input"))

    def _external_result_denied_by_user(self, result: TaskResult) -> bool:
        approval = self._external_approval_info(result)
        return (
            result.status == TaskStatus.FAILED
            and str(approval.get("action", "")).lower() == ApprovalAction.REJECT.value
            and str(approval.get("policy_source", "")).lower() == "human_escalation"
        )

    @staticmethod
    def _extract_runtime_state_from_artifacts(artifacts: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(artifacts or {})
        runtime_session_id = str(data.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return {}
        active_subagents = data.get("active_subagents", [])
        permission_requests = data.get("permission_requests", [])
        compaction_boundaries = data.get("compaction_boundaries", [])
        compaction_records = data.get("compaction_records", compaction_boundaries)
        resume_cursor = data.get("resume_cursor")
        worktree_path = str(data.get("worktree_path", "") or "").strip()
        task_ledger = data.get("task_ledger", [])
        prefetch_hits = data.get("prefetch_hits", [])
        verification = data.get("verification", {})
        verification_evidence = data.get("verification_evidence", {})
        verification_verdict = str(data.get("verification_verdict", "") or "").strip()
        artifact_manifest = data.get("artifact_manifest", [])
        resume_state = dict(data.get("resume_state", {}) or {})
        runtime_state = {
            "runtime_session_id": runtime_session_id,
            "active_subagents": active_subagents if isinstance(active_subagents, list) else [],
            "permission_requests": permission_requests if isinstance(permission_requests, list) else [],
            "compaction_boundaries": compaction_boundaries if isinstance(compaction_boundaries, list) else [],
            "compaction_records": compaction_records if isinstance(compaction_records, list) else [],
            "resume_cursor": resume_cursor,
            "worktree_path": worktree_path,
            "task_ledger": task_ledger if isinstance(task_ledger, list) else [],
            "prefetch_hits": prefetch_hits if isinstance(prefetch_hits, list) else [],
            "verification": verification if isinstance(verification, dict) else {},
            "verification_evidence": verification_evidence if isinstance(verification_evidence, dict) else {},
            "verification_verdict": verification_verdict,
            "artifact_manifest": artifact_manifest if isinstance(artifact_manifest, list) else [],
            "resume_state": resume_state,
        }
        return runtime_state

    def _apply_runtime_state_to_task(self, task: Task, result: TaskResult) -> None:
        runtime_state = self._extract_runtime_state_from_artifacts(result.artifacts)
        if not runtime_state:
            return
        task.metadata = dict(task.metadata)
        task.metadata["runtime_v2"] = runtime_state
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["runtime_v2"] = runtime_state

    def _build_runtime_checkpoint_payload(self, task: Task, result: TaskResult | None = None) -> dict[str, Any]:
        source_artifacts: dict[str, Any] | None = result.artifacts if result else None
        if not source_artifacts and isinstance(task.result, dict):
            source_artifacts = dict(task.result.get("artifacts", {}) or {})
        runtime_state = self._extract_runtime_state_from_artifacts(source_artifacts)
        if not runtime_state:
            runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if not runtime_state:
            return {}
        payload = {
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
            "artifact_manifest": list(runtime_state.get("artifact_manifest", []) or []),
            "resume_state": dict(runtime_state.get("resume_state", {}) or {}),
        }
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
        ):
            if key in task.metadata and task.metadata.get(key) not in (None, "", [], {}):
                payload[key] = task.metadata.get(key)
        return payload

    def _restore_runtime_state_from_checkpoint(self, task: Task, payload: dict[str, Any]) -> None:
        runtime_state = dict(payload.get("runtime_v2", {}) or {})
        task.metadata = dict(task.metadata)
        task.context_snapshot = dict(task.context_snapshot)
        if runtime_state:
            task.metadata["runtime_v2"] = runtime_state
            task.context_snapshot["runtime_resume"] = {
                **runtime_state,
                "restored_from_checkpoint": True,
                "restored_at": datetime.now().isoformat(),
            }
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
        ):
            if key not in payload or payload.get(key) in (None, "", [], {}):
                continue
            task.metadata[key] = payload.get(key)
            task.context_snapshot[key] = payload.get(key)

    def _generated_runtime_session_id(self, task: Task, checkpoint_type: str = "") -> str:
        seed = "::".join([
            str(task.project_id or "default"),
            str(task.session_id or ""),
            str(task.id or ""),
            str(checkpoint_type or ""),
        ])
        return f"rtmig_{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:24]}"

    async def _build_migrated_runtime_state(
        self,
        task: Task,
        *,
        checkpoint_type: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_data = dict(payload or {})
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if runtime_state.get("runtime_session_id"):
            return runtime_state

        transcript_count = 0
        compaction_boundaries: list[dict[str, Any]] = []
        if self.store and task.session_id:
            try:
                transcript = await self.store.get_session_transcript(task.session_id)
                transcript_count = len(transcript)
            except Exception:
                transcript_count = 0
            try:
                latest_compaction = await self.store.get_latest_session_compaction(task.session_id)
            except Exception:
                latest_compaction = None
            if latest_compaction:
                compaction_boundaries.append({
                    "summary": "Migrated from legacy session compaction.",
                    "source_boundary_message_id": latest_compaction.source_boundary_message_id,
                    "compaction_message_id": latest_compaction.compaction_message_id,
                    "created_at": latest_compaction.created_at.isoformat(),
                })

        permission_requests: list[dict[str, Any]] = []
        approval = dict(payload_data.get("approval", {}) or {})
        if approval:
            permission_requests.append({
                "tool_name": str(payload_data.get("tool_name", "") or ""),
                "resolution": "ask",
                "scope": "once",
                "risk_level": str(approval.get("risk_level", "medium") or "medium"),
                "rationale": str(approval.get("rationale", "") or payload_data.get("prompt", "") or "Migrated legacy approval request."),
                "source": str(approval.get("policy_source", "legacy_checkpoint") or "legacy_checkpoint"),
            })
        pause_request = dict(payload_data.get("pause_request", {}) or {})
        if pause_request and not permission_requests:
            permission_requests.append({
                "tool_name": str(payload_data.get("tool_name", "") or ""),
                "resolution": "ask",
                "scope": "once",
                "risk_level": "medium",
                "rationale": str(pause_request.get("reason", "") or "Migrated legacy pause request."),
                "source": "legacy_checkpoint",
            })

        runtime_state = {
            "runtime_session_id": self._generated_runtime_session_id(task, checkpoint_type=checkpoint_type),
            "active_subagents": list(payload_data.get("active_subagents", []) or []),
            "permission_requests": permission_requests,
            "compaction_boundaries": compaction_boundaries,
            "resume_cursor": max(transcript_count, int(payload_data.get("resume_cursor", 0) or 0)),
            "worktree_path": str(
                payload_data.get("worktree_path")
                or task.metadata.get("target_output_dir")
                or ""
            ).strip(),
            "checkpoint_source": checkpoint_type or "legacy_checkpoint",
            "migrated_from_legacy": True,
        }
        return runtime_state

    async def _ensure_checkpoint_runtime_v2_payload(
        self,
        checkpoint: ExecutionCheckpoint,
        task: Task | None = None,
    ) -> ExecutionCheckpoint:
        payload = dict(checkpoint.payload or {})
        runtime_state = dict(payload.get("runtime_v2", {}) or {})
        if runtime_state.get("runtime_session_id"):
            return checkpoint
        if not task:
            task_id = str(
                checkpoint.task_id
                or payload.get("waiting_task_id")
                or payload.get("task_id")
                or ""
            ).strip()
            if task_id and self.store:
                task = await self.store.get_task(task_id)
        if not task:
            return checkpoint

        runtime_state = await self._build_migrated_runtime_state(
            task,
            checkpoint_type=checkpoint.checkpoint_type,
            payload=payload,
        )
        if not runtime_state.get("runtime_session_id"):
            return checkpoint

        task.metadata = dict(task.metadata)
        task.metadata["runtime_v2"] = runtime_state
        task.metadata["migration_status"] = "runtime_v2_migrated"
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["runtime_v2"] = runtime_state
        if self.store:
            await self.store.save_task(task)
            if getattr(self.store, "save_runtime_session", None):
                await self.store.save_runtime_session(
                    runtime_session_id=runtime_state["runtime_session_id"],
                    task_id=task.id,
                    session_id=task.session_id,
                    project_id=task.project_id,
                    status="migrated",
                    metadata=runtime_state,
                )

        payload = {
            **payload,
            "runtime_v2": runtime_state,
            "runtime_session_id": runtime_state.get("runtime_session_id", ""),
            "resume_cursor": runtime_state.get("resume_cursor"),
            "active_subagents": list(runtime_state.get("active_subagents", []) or []),
            "permission_requests": list(runtime_state.get("permission_requests", []) or []),
            "compaction_boundaries": list(runtime_state.get("compaction_boundaries", []) or []),
            "worktree_path": runtime_state.get("worktree_path", ""),
            "migrated_to_runtime_v2": True,
        }
        checkpoint.payload = payload
        checkpoint.updated_at = datetime.now()
        if self.store:
            await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    def _estimate_task_complexity(self, task: Task, role: Any | None = None) -> tuple[int, list[str]]:
        text = f"{task.title}\n{task.description}".strip().lower()
        score = 0
        reasons: list[str] = []

        if len(text) > 1400:
            score += 2
            reasons.append("long_task_description")
        elif len(text) > 500:
            score += 1
            reasons.append("medium_task_description")

        if len(task.dependencies) > 1:
            score += 1
            reasons.append("multiple_dependencies")

        tool_heavy_keywords = (
            "implement", "fix", "debug", "refactor", "write code", "edit", "file", "files",
            "shell", "cli", "command", "script", "git", "test", "build", "deploy", "migration",
            "api", "endpoint", "database",
        )
        if any(keyword in text for keyword in tool_heavy_keywords):
            score += 2
            reasons.append("tool_heavy_work")

        multi_step_keywords = (
            "end-to-end", "complex", "multi-step", "runtime", "coordinate", "parallel",
            "integration", "architecture", "deliverable",
        )
        if any(keyword in text for keyword in multi_step_keywords):
            score += 1
            reasons.append("multi_step_work")

        if role:
            role_text = " ".join(
                str(part)
                for part in (
                    getattr(role, "role_id", ""),
                    getattr(role, "name", ""),
                    getattr(role, "responsibility", ""),
                )
                if part
            ).lower()
            if any(keyword in role_text for keyword in ("implement", "engineering", "deployment", "executor", "code", "data processing")):
                score += 1
                reasons.append("execution_oriented_role")
            if any(keyword in role_text for keyword in ("review", "approval", "qa", "plan", "planning", "coordinator")):
                score -= 2
                reasons.append("native_friendly_role")

        return max(score, 0), reasons

    def _build_execution_context_summary(self, task: Task, role: Any | None = None) -> dict[str, Any]:
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        raw_resume_state = task.context_snapshot.get("runtime_resume", {}) if isinstance(task.context_snapshot, dict) else {}
        resume_state = dict(raw_resume_state) if isinstance(raw_resume_state, dict) else {}
        attachments = list(task.metadata.get("attachment_refs", []) or [])
        work_item_projection_id = projection_id_for_task(task)
        work_item_turn_type = turn_type_for_task(
            task,
            fallback=(self._get_role_runtime_value(role, "default_turn_type", "work").lower() if role else "work"),
        )
        return {
            "execution_mode": str(task.metadata.get("execution_mode", "") or "").strip(),
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "turn_type": work_item_turn_type,
            "has_runtime_resume": bool(runtime_state or resume_state),
            "active_subagents": len(
                list(
                    (resume_state.get("active_subagents") or runtime_state.get("active_subagents") or [])
                )
            ),
            "pending_permission_requests": len(
                list(
                    (resume_state.get("permission_requests") or runtime_state.get("permission_requests") or [])
                )
            ),
            "attachment_count": len(attachments),
            "force_native_execution": bool(task.metadata.get("force_native_execution")),
            "work_item_orchestration_profile": str(task.metadata.get("work_item_orchestration_profile", "") or "").strip(),
        }

    def _build_execution_capability_matrix(self, task: Task, available: list[str]) -> dict[str, Any]:
        native_caps = self.llm.get_capabilities(task_type="coding") if self.llm else None
        matrix: dict[str, Any] = {
            "native": {
                "model": getattr(native_caps, "model", ""),
                "supports_streaming": bool(getattr(native_caps, "supports_streaming", True)),
                "supports_tool_calling": bool(getattr(native_caps, "supports_tool_calling", True)),
                "supports_streaming_tool_calls": bool(getattr(native_caps, "supports_streaming_tool_calls", True)),
                "supports_multimodal": bool(getattr(native_caps, "supports_multimodal", False)),
                "supports_subagents": bool((self.config.agents.native_subagents or {})),
                "supports_resume": True,
            }
        }
        if not self.adapter_registry:
            return matrix
        for name in available:
            adapter = self.adapter_registry.get(name)
            matrix[name] = {
                "interactive": bool(adapter.supports_interactive()) if adapter else False,
                "supports_resume": True,
                "kind": "external",
            }
        return matrix

    def _prefer_native_for_current_context(
        self,
        task: Task,
        role: Any | None,
        capability_matrix: dict[str, Any],
    ) -> tuple[bool, str]:
        context = self._build_execution_context_summary(task, role)
        native_caps = dict(capability_matrix.get("native", {}) or {})
        if context["has_runtime_resume"]:
            return True, "resume_prefers_native_v2"
        if context["active_subagents"] or context["pending_permission_requests"]:
            return True, "active_runtime_state_prefers_native_v2"
        if context["attachment_count"] and native_caps.get("supports_multimodal"):
            return True, "native_multimodal_context"
        if context.get("work_item_orchestration_profile") == "company_execute_native_first":
            return True, "company_execute_native_first"
        if context["turn_type"] in {"review", "approval", "plan"}:
            return True, "native_friendly_turn_type"
        return False, ""

    def _fallback_select_task_execution_agent(
        self,
        task: Task,
        role: Any,
        available: list[str],
    ) -> tuple[str | None, dict[str, Any]]:
        preferred = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        )
        router_preferred = str(task.metadata.get("router_preferred_agent") or "").strip()
        strategy = str(
            task.metadata.get("work_item_execution_strategy")
            or self._get_role_runtime_value(role, "execution_strategy", "auto")
            or "auto"
        ).lower()
        turn_type = self._get_role_runtime_value(role, "default_turn_type", "work").lower()
        complexity, reasons = self._estimate_task_complexity(task, role)
        capability_matrix = self._build_execution_capability_matrix(task, available)
        current_context = self._build_execution_context_summary(task, role)
        prefer_native, native_reason = self._prefer_native_for_current_context(task, role, capability_matrix)

        selected: str | None = None
        decision_reason = "native_default"
        if not available:
            decision_reason = "no_external_agents_available"
        elif prefer_native:
            decision_reason = native_reason
        elif strategy == "native":
            decision_reason = "role_or_work_item_forces_native"
        elif strategy == "external":
            selected = preferred if preferred in available else available[0]
            decision_reason = "role_or_work_item_forces_external"
        else:
            should_use_external = False
            if preferred and preferred in available and turn_type == "work" and complexity >= 2:
                should_use_external = True
                decision_reason = "preferred_external_for_complex_work_item"
            elif complexity >= 3:
                should_use_external = True
                decision_reason = "high_complexity_task"
            elif strategy == "mixed" and complexity >= 2:
                should_use_external = True
                decision_reason = "mixed_strategy_complex_task"
            elif router_preferred and router_preferred in available and complexity >= 2:
                should_use_external = True
                decision_reason = "router_preferred_external_for_complex_task"

            if should_use_external:
                if preferred and preferred in available:
                    selected = preferred
                elif router_preferred and router_preferred in available:
                    selected = router_preferred
                else:
                    selected = available[0]

        metadata = {
            "selected": selected or "native",
            "strategy": strategy,
            "role_id": task.assigned_to,
            "turn_type": turn_type,
            "complexity_score": complexity,
            "reasons": reasons,
            "decision_reason": decision_reason,
            "available_external_agents": list(available),
            "selection_source": "fallback_rules",
            "capability_matrix": capability_matrix,
            "current_execution_context": current_context,
        }
        return selected, metadata

    async def _select_task_execution_agent_via_llm(
        self,
        task: Task,
        role: Any,
        available: list[str],
    ) -> tuple[str | None, dict[str, Any]] | None:
        if not self.llm or not self.llm.has_credentials():
            # No LLM key configured: the selection calls would fail auth on every
            # retry. Skip straight to rule-based selection so a keyless setup with
            # an external agent still runs without wasted, doomed LLM attempts.
            return None

        preferred = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        ) or None
        router_preferred = str(task.metadata.get("router_preferred_agent") or "").strip() or None
        strategy = str(
            task.metadata.get("work_item_execution_strategy")
            or self._get_role_runtime_value(role, "execution_strategy", "auto")
            or "auto"
        ).lower()
        turn_type = self._get_role_runtime_value(role, "default_turn_type", "work").lower()

        base_payload = {
            "task": {
                "title": task.title,
                "description": task.description,
                "assigned_to": task.assigned_to,
                "tags": list(task.tags),
                "dependencies": list(task.dependencies),
            },
            "role": {
                "role_id": getattr(role, "role_id", ""),
                "name": getattr(role, "name", ""),
                "responsibility": getattr(role, "responsibility", ""),
                "preferred_external_agent": getattr(role, "preferred_external_agent", None),
                "runtime_policy": getattr(role, "runtime_policy", {}),
            },
            "execution_context": {
                "execution_mode": task.metadata.get("execution_mode", ""),
                **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                "turn_type": turn_type,
                "execution_strategy": strategy,
                "router_preferred_agent": router_preferred,
                "task_preferred_external_agent": preferred,
                "original_message": str(task.metadata.get("original_message", "")),
            },
            "available_external_agents": available,
            "capability_matrix": self._build_execution_capability_matrix(task, available),
        }

        retry_feedback: list[dict[str, str]] = []
        max_attempts = 3
        valid_choices = {"native", *available}

        for attempt in range(1, max_attempts + 1):
            payload = dict(base_payload)
            if retry_feedback:
                payload["retry_feedback"] = list(retry_feedback)
            try:
                raw = await self.llm.simple_chat(
                    prompt=json.dumps(payload, ensure_ascii=False),
                    system=AGENT_SELECTION_PROMPT,
                    task_type="quick_tasks",
                )
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                data = json.loads(text)
                selected_raw = str(data.get("selected_agent", "native")).strip().lower()
                reasoning = str(data.get("reasoning", "")).strip()

                if selected_raw not in valid_choices:
                    issue = (
                        f"Invalid selected_agent `{selected_raw}`. "
                        f"Choose exactly one of: {', '.join(sorted(valid_choices))}."
                    )
                    retry_feedback.append({
                        "attempt": str(attempt),
                        "issue": issue,
                        "previous_response_excerpt": text,
                    })
                    logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")
                    continue

                selected = None if selected_raw == "native" else selected_raw
                metadata = {
                    "selected": selected or "native",
                    "strategy": strategy,
                    "role_id": task.assigned_to,
                    "turn_type": turn_type,
                    "decision_reason": reasoning or "llm_selected_agent",
                    "available_external_agents": list(available),
                    "selection_source": "llm",
                    "llm_attempts": attempt,
                    "capability_matrix": base_payload["capability_matrix"],
                    "current_execution_context": self._build_execution_context_summary(task, role),
                }
                return selected, metadata
            except json.JSONDecodeError as e:
                issue = f"Response was not valid JSON: {e.msg} at char {e.pos}."
                retry_feedback.append({
                    "attempt": str(attempt),
                    "issue": issue,
                    "previous_response_excerpt": raw if 'raw' in locals() and isinstance(raw, str) else "",
                })
                logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")
            except Exception as e:
                issue = f"LLM call failed: {e}"
                retry_feedback.append({
                    "attempt": str(attempt),
                    "issue": issue,
                    "previous_response_excerpt": "",
                })
                logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")

        logger.warning("LLM agent selection exhausted retries; falling back to rules")
        return None

    async def _assign_task_execution_agent(self, task: Task, role: Any | None = None) -> str | None:
        assert self.org_engine
        task.metadata = dict(task.metadata)
        resume_pin = dict(
            task.metadata.get("_company_runtime_resume_execution_agent_pin", {})
            or {}
        )
        if resume_pin:
            available_for_audit: list[str] = []
            selected_name = normalize_recruitment_agent_choice(
                resume_pin.get("selected_execution_agent"),
                default=(
                    str(resume_pin.get("assigned_external_agent", "") or "").strip()
                    or "native"
                ),
            ) or "native"
            assigned_name = str(
                resume_pin.get("assigned_external_agent", "") or ""
            ).strip()
            if selected_name == "native":
                if assigned_name:
                    raise RuntimeError(
                        f"company runtime resume agent pin is inconsistent for task {task.id}"
                    )
                selected: str | None = None
            else:
                if assigned_name and assigned_name != selected_name:
                    raise RuntimeError(
                        f"company runtime resume agent pin is inconsistent for task {task.id}"
                    )
                available_for_audit = self._available_external_agents()
                if selected_name not in available_for_audit:
                    raise RuntimeError(
                        "company runtime resume requires unavailable external agent "
                        f"{selected_name!r} for task {task.id}"
                    )
                selected = selected_name
            task.assigned_external_agent = selected
            task.metadata["selected_execution_agent"] = selected_name
            task.metadata["preferred_external_agent"] = selected
            task.metadata["agent_selection"] = {
                "selected": selected_name,
                "strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if selected_name == "native"
                    else WorkItemExecutionStrategy.EXTERNAL.value
                ),
                "role_id": task.assigned_to
                or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "company_runtime_resume_checkpoint_pin",
                "selection_source": "company_runtime_resume_checkpoint",
                "checkpoint_id": str(resume_pin.get("checkpoint_id", "") or ""),
                "original_selection_source": str(
                    resume_pin.get("selected_execution_agent_source", "") or ""
                ),
                "available_external_agents": (
                    available_for_audit
                ),
            }
            # The pin belongs to this dispatch attempt.  Persisting the
            # resulting choice is useful audit state, but leaving the pin set
            # would silently turn an adaptive role into a permanent lock.
            task.metadata.pop(
                "_company_runtime_resume_execution_agent_pin",
                None,
            )
            return selected
        locked_agent = normalize_recruitment_agent_choice(
            task.metadata.get("selected_execution_agent"),
            default=("native" if not str(task.assigned_external_agent or "").strip() else str(task.assigned_external_agent or "").strip()),
        )
        if task.metadata.get("execution_agent_locked") and locked_agent:
            selected = None if locked_agent == "native" else locked_agent
            task.assigned_external_agent = selected
            task.metadata["preferred_external_agent"] = selected
            task.metadata["agent_selection"] = {
                "selected": locked_agent,
                "strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if locked_agent == "native"
                    else WorkItemExecutionStrategy.EXTERNAL.value
                ),
                "role_id": task.assigned_to or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "explicit_recruitment_agent_override",
                "available_external_agents": self._available_external_agents(),
                "selection_source": "explicit_recruitment_override",
            }
            return selected
        if self._should_force_native_execution(task):
            task.assigned_external_agent = None
            task.metadata["agent_selection"] = {
                "selected": "native",
                "strategy": "native",
                "role_id": task.assigned_to or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "explicit_native_override",
                "available_external_agents": [],
                "selection_source": "forced_native",
            }
            return None

        available = self._available_external_agents()
        role = role or self._resolve_task_role(task)

        llm_choice = await self._select_task_execution_agent_via_llm(task, role, available)
        if llm_choice is not None:
            selected, metadata = llm_choice
        else:
            selected, metadata = self._fallback_select_task_execution_agent(task, role, available)
            metadata["llm_attempts"] = 3 if (self.llm and self.llm.has_credentials()) else 0

        task.assigned_external_agent = selected
        task.metadata["agent_selection"] = metadata
        return selected

    def _should_use_external_pool(self, task: Task) -> bool:
        agent = task.assigned_external_agent
        return bool(agent and agent != "native")

    def _get_external_candidates(self, task: Task) -> list[tuple[str, Any]]:
        if not self.adapter_registry or not self._should_use_external_pool(task):
            return []
        preferred = task.assigned_external_agent
        ordered = self.adapter_registry.get_ordered_available()
        if not preferred:
            return ordered

        preferred_adapter = self.adapter_registry.get(preferred)
        if not preferred_adapter:
            return ordered

        selection = dict(task.metadata.get("agent_selection", {}) or {})
        if (
            str(selection.get("selection_source", "") or "").strip()
            == "company_runtime_resume_checkpoint"
        ):
            # Resume owns one exact execution backend.  Returning alternates
            # here would undermine the checkpoint pin after the selector has
            # consumed its one-shot marker.
            return [(preferred, preferred_adapter)]

        remaining = [(name, adapter) for name, adapter in ordered if name != preferred]
        return [(preferred, preferred_adapter), *remaining]

    @staticmethod
    def _workspace_root_from_output_root(output_root: str | None) -> str | None:
        raw = str(output_root or "").strip()
        if not raw:
            return None
        try:
            output_path = Path(raw).expanduser().resolve()
        except Exception:
            return None
        parent = output_path.parent
        if parent and str(parent) not in {"/", ".", ""}:
            return str(parent)
        return str(output_path)

    async def _resolve_workspace_root(
        self,
        session_id: str | None = None,
        *,
        target_output_dir: str | None = None,
    ) -> str | None:
        session_defaults = await self._load_session_execution_defaults(session_id)
        sticky_workspace = str(session_defaults.get("workspace_root") or "").strip()
        if sticky_workspace:
            return sticky_workspace
        sticky_comms_workspace = str(session_defaults.get("comms_workspace_root") or "").strip()
        if sticky_comms_workspace:
            return sticky_comms_workspace
        sticky_output = str(session_defaults.get("target_output_dir") or "").strip()
        if sticky_output:
            inferred = self._workspace_root_from_output_root(sticky_output)
            if inferred:
                return inferred
        inferred = self._workspace_root_from_output_root(target_output_dir)
        if inferred:
            return inferred
        project_id = str(self.project_id or "default").strip() or "default"
        workplace = get_project_workplace(project_id)
        workplace.mkdir(parents=True, exist_ok=True)
        return str(workplace.resolve())

    async def _resolve_workspace_contract(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, str]:
        _ = message
        session_defaults = await self._load_session_execution_defaults(session_id)
        sticky_output_root = str(session_defaults.get("target_output_dir") or "").strip()
        output_root = sticky_output_root

        workspace_root = await self._resolve_workspace_root(
            session_id,
            target_output_dir=output_root,
        )
        sticky_comms_workspace = str(session_defaults.get("comms_workspace_root") or "").strip()
        comms_workspace_root = sticky_comms_workspace or workspace_root or self._resolve_comms_workspace_root(output_root)
        comms_root = (
            str(Path(comms_workspace_root).expanduser().resolve() / ".opc-comms")
            if comms_workspace_root else ""
        )
        return {
            "workspace_root": str(workspace_root or "").strip(),
            "output_root": str(output_root or "").strip(),
            "comms_workspace_root": str(comms_workspace_root or "").strip(),
            "comms_root": comms_root,
        }

    async def _load_session_execution_defaults(self, session_id: str | None) -> dict[str, Any]:
        if not self.store or not session_id or not hasattr(self.store, "get_session"):
            return {}
        session = await self.store.get_session(session_id)
        if not session:
            return {}
        defaults = session.metadata.get("execution_defaults", {})
        return dict(defaults) if isinstance(defaults, dict) else {}

    async def _remember_session_execution_defaults(
        self,
        session_id: str | None,
        decision: RouterDecision,
        *,
        target_output_dir: str | None,
        workspace_root: str | None = None,
        comms_workspace_root: str | None = None,
        comms_root: str | None = None,
    ) -> None:
        if (
            not self.store
            or not session_id
            or not hasattr(self.store, "get_session")
            or not hasattr(self.store, "save_session")
        ):
            return
        session = await self.store.get_session(session_id)
        if not session:
            return
        metadata = dict(session.metadata)
        previous = metadata.get("execution_defaults", {})
        previous_defaults = dict(previous) if isinstance(previous, dict) else {}
        metadata["execution_defaults"] = {
            **previous_defaults,
            "mode": decision.mode.value,
            "company_profile": decision.company_profile or previous_defaults.get("company_profile", ""),
            "preferred_agent": decision.preferred_agent or previous_defaults.get("preferred_agent", ""),
            "target_output_dir": target_output_dir or previous_defaults.get("target_output_dir", ""),
            "workspace_root": workspace_root or previous_defaults.get("workspace_root", ""),
            "comms_workspace_root": comms_workspace_root or previous_defaults.get("comms_workspace_root", ""),
            "comms_root": comms_root or previous_defaults.get("comms_root", ""),
            "updated_at": datetime.now().isoformat(),
        }
        session.metadata = metadata
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    async def _sync_origin_task_execution_context(
        self,
        origin_task_id: str | None,
        *,
        session_id: str,
        decision: RouterDecision,
        workspace_contract: dict[str, Any],
        original_message: str = "",
        origin_channel: str = "",
        origin_chat_id: str = "",
        origin_thread_id: str = "",
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Keep the canonical root/UI task aligned with the resolved execution context.

        Office UI sessions use a user-facing root task as the stable session anchor,
        while company-mode execution fans out into child tasks. The root task must
        still carry the resolved workspace/comms metadata so session-scoped features
        such as the Comms panel can resolve the collaboration tree directly from the
        current session anchor instead of depending on child-task side effects.
        """
        if not self.store or not origin_task_id:
            return
        task = await self.store.get_task(origin_task_id)
        if task is None:
            return

        metadata = dict(task.metadata or {})
        task.project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        task.session_id = str(task.session_id or session_id or "").strip() or task.session_id

        company_profile = (
            decision.company_profile
            or str(metadata.get("company_profile", "") or "").strip()
        )
        profile_key = str(company_profile or "").strip().lower()
        if decision.mode == ExecutionMode.COMPANY_MODE:
            exec_mode = "org" if profile_key == "custom" else "company"
        else:
            exec_mode = "task"
        if exec_mode == "company":
            company_profile_value = "corporate"
        elif exec_mode == "org":
            company_profile_value = "custom"
        else:
            company_profile_value = str(metadata.get("company_profile", "") or "").strip()
        metadata.update({
            "exec_mode": exec_mode,
            "company_profile": company_profile_value,
            "preferred_agent": decision.preferred_agent or str(metadata.get("preferred_agent", "") or "").strip(),
            "execution_mode": decision.mode.value,
            "workspace_root": str(workspace_contract.get("workspace_root", "") or "").strip(),
            "output_root": str(workspace_contract.get("output_root", "") or "").strip(),
            "target_output_dir": str(workspace_contract.get("output_root", "") or "").strip(),
            "comms_workspace_root": str(workspace_contract.get("comms_workspace_root", "") or "").strip(),
            "comms_root": str(workspace_contract.get("comms_root", "") or "").strip(),
            "origin_task_id": str(origin_task_id).strip(),
        })
        if exec_mode != "org":
            metadata.pop("org_id", None)
            metadata.pop("organization_id", None)
        if original_message:
            metadata["original_message"] = original_message
        if origin_channel:
            metadata["origin_channel"] = origin_channel
        if origin_chat_id:
            metadata["origin_chat_id"] = origin_chat_id
        if origin_thread_id:
            metadata["origin_thread_id"] = origin_thread_id
        if attachment_refs:
            metadata["attachment_refs"] = self._normalize_attachment_refs(attachment_refs)
            metadata["attachment_context"] = self._build_attachment_context(attachment_refs)

        task.metadata = metadata
        await self.store.save_task(task)

    def _detect_explicit_mode_override(self, message: str) -> str | None:
        text = message.casefold()
        single_agent_markers = (
            "single agent",
            "single-agent",
            "单agent",
            "单 agent",
            "单代理",
            "单智能体",
            "原生agent",
            "native agent",
            "native 模式",
        )
        company_mode_markers = (
            "company mode",
            "company-mode",
            "company模式",
            "公司模式",
            "团队模式",
            "多人模式",
        )
        if any(marker in text for marker in single_agent_markers):
            return ExecutionMode.SINGLE_AGENT.value
        if any(marker in text for marker in company_mode_markers):
            return ExecutionMode.COMPANY_MODE.value
        return None

    def _looks_like_followup_request(self, message: str) -> bool:
        patterns = (
            r"继续",
            r"后续",
            r"再[来做改加补修优]",
            r"新增",
            r"添加",
            r"增加",
            r"补充",
            r"修改",
            r"改一下",
            r"完善",
            r"优化",
            r"修复",
            r"接着",
            r"顺便",
            r"follow[- ]?up",
            r"continue",
            r"also",
            r"another",
            r"add ",
            r"update",
            r"modify",
            r"change",
            r"improve",
            r"fix",
            r"tweak",
        )
        return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        return cleaned

    async def _load_company_runtime_snapshot(
        self,
        parent_session_id: str | None,
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]] | None:
        if not self.store or not parent_session_id:
            return None
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(runtime_session_id=parent_session_id)
        if identity is None:
            return None
        work_item_tasks = [
            task
            for task_id in identity.runtime_task_ids
            if task_id != identity.ui_anchor_task_id
            for task in [identity_index.task(task_id)]
            if task is not None
            and is_company_runtime_task(task)
            and (
                work_item_projection_id_from_metadata(getattr(task, "metadata", {}) or {})
                or is_work_item_runtime_metadata(getattr(task, "metadata", {}) or {})
                or linked_work_item_id_for_task(task)
            )
        ]
        if not work_item_tasks:
            return None

        latest_by_projection_id: dict[str, Task] = {}
        for task in sorted(work_item_tasks, key=lambda item: (item.created_at, item.id)):
            projection_id = str(
                work_item_projection_id_from_metadata(task.metadata)
                or linked_work_item_id_for_task(task)
                or ""
            ).strip()
            if projection_id:
                latest_by_projection_id[projection_id] = task
        if not latest_by_projection_id:
            return None

        plan_data = None
        plan_sources = list(latest_by_projection_id.values())
        config_source = identity_index.task(identity.config_source_task_id)
        if config_source is not None and config_source.id not in {
            task.id for task in plan_sources
        }:
            plan_sources.append(config_source)
        for task in sorted(
            plan_sources,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        ):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                plan_data = candidate
                break
        sample = next(iter(latest_by_projection_id.values()))
        if plan_data and isinstance(plan_data, dict):
            plan = deserialize_company_work_item_runtime_plan(plan_data)
        else:
            plan = CompanyWorkItemRuntimePlan(
                profile=str(sample.metadata.get("company_profile", "") or getattr(self.config.org, "company_profile", "corporate")).strip() or "corporate",
                metadata={
                    "execution_model": str(sample.metadata.get("execution_model", "") or "multi_team_org").strip() or "multi_team_org",
                    "runtime_model": str(sample.metadata.get("runtime_model", "") or "").strip(),
                    "work_item_runtime": is_work_item_runtime_metadata(sample.metadata),
                },
            )
        projection_order = {spec.projection_id: idx for idx, spec in enumerate(plan.projections)}
        ordered_tasks = sorted(
            latest_by_projection_id.values(),
            key=lambda task: (
                projection_order.get(
                    projection_id_for_task(task)
                    or linked_work_item_id_for_task(task),
                    len(projection_order),
                ),
                task.created_at,
                task.id,
            ),
        )
        return plan, ordered_tasks

    @staticmethod
    def _runtime_uses_multi_team_org(plan: CompanyWorkItemRuntimePlan | None) -> bool:
        if plan is None:
            return False
        metadata = dict(getattr(plan, "metadata", {}) or {})
        return (
            str(metadata.get("execution_model", "") or "").strip() == "multi_team_org"
            or str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
            or bool(getattr(plan, "projections", []) or [])
        )

    @staticmethod
    def _task_uses_multi_team_org(task: Task | None) -> bool:
        if task is None:
            return False
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("execution_model", "") or "").strip() == "multi_team_org"
            or str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
            or is_work_item_runtime_metadata(metadata)
        )

    @staticmethod
    def _is_company_runtime_suspend_checkpoint(checkpoint_type: str | None) -> bool:
        return str(checkpoint_type or "").strip() in _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES

    @staticmethod
    def _checkpoint_progress_tail(task: Task, *, limit: int = 20) -> list[str]:
        progress = list((task.metadata or {}).get("progress_log", []) or [])
        return [str(item) for item in progress[-limit:]]

    @staticmethod
    def _task_effective_execution_agent_identity(
        task: Task,
    ) -> tuple[str, str, str]:
        """Return the backend that this Task attempt actually executes on.

        Recruitment's ``selected_execution_agent`` is policy/default input and
        can remain unchanged after an unlocked adaptive selection.  Runtime
        assignment and its audit record are the attempt identity; recruitment
        metadata is only a fallback for tasks that have not recorded either.
        """

        metadata = dict(task.metadata or {})
        selection = dict(metadata.get("agent_selection", {}) or {})
        selection_agent = normalize_recruitment_agent_choice(
            selection.get("selected")
        )
        assigned_agent = normalize_recruitment_agent_choice(
            task.assigned_external_agent
        )
        if bool(metadata.get("force_native_execution")) or selection_agent == "native":
            selected_agent = "native"
            assigned_external_agent = ""
        elif assigned_agent and assigned_agent != "native":
            selected_agent = assigned_agent
            assigned_external_agent = assigned_agent
        elif selection_agent and selection_agent != "native":
            selected_agent = selection_agent
            assigned_external_agent = selection_agent
        else:
            selected_agent = normalize_recruitment_agent_choice(
                metadata.get("selected_execution_agent"),
                default="native",
            ) or "native"
            assigned_external_agent = (
                selected_agent if selected_agent != "native" else ""
            )
        selection_source = str(
            selection.get("selection_source")
            or metadata.get("selected_execution_agent_source")
            or ""
        ).strip()
        return selected_agent, assigned_external_agent, selection_source

    async def _external_resume_snapshot_for_task(self, task: Task) -> dict[str, Any]:
        session = (
            await self._load_best_external_resume_session_for_task(task)
            or await self._load_latest_external_session_for_task(task)
        )
        if not session:
            return {}
        metadata = dict(getattr(session, "metadata", {}) or {})
        return {
            "agent_type": str(getattr(session, "agent_type", "") or "").strip(),
            "session_id": str(getattr(session, "session_id", "") or "").strip(),
            "opc_session_id": str(getattr(session, "opc_session_id", "") or "").strip(),
            "task_id": str(getattr(session, "task_id", "") or "").strip(),
            "status": str(getattr(session, "status", "") or "").strip(),
            "workspace_path": str(getattr(session, "workspace_path", "") or "").strip(),
            "resume_session_id": str(metadata.get("resume_session_id", "") or "").strip(),
            "provider_session_id": str(metadata.get("provider_session_id", "") or "").strip(),
            "metadata": metadata,
            "updated_at": getattr(session, "updated_at", datetime.now()).isoformat(),
        }

    async def _company_runtime_checkpoint_payload(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = self.project_id or "default"
        now = datetime.now().isoformat()
        run_id = ""
        company_profile = str(plan.profile or "").strip()
        task_snapshots: list[dict[str, Any]] = []
        active_work_items: list[dict[str, Any]] = []
        role_runtime_session_ids: list[str] = []
        seat_state_ids: list[str] = []
        adapter_session_state_by_role: dict[str, dict[str, Any]] = {}
        native_runtime_resume_by_task: dict[str, dict[str, Any]] = {}
        external_sessions_by_task: dict[str, dict[str, Any]] = {}

        get_work_item = getattr(self.store, "get_delegation_work_item", None) if self.store else None
        get_role_session = getattr(self.store, "get_delegation_role_session", None) if self.store else None

        for task in tasks:
            metadata = dict(task.metadata or {})
            run_id = run_id or str(metadata.get("delegation_run_id", "") or "").strip()
            company_profile = company_profile or str(metadata.get("company_profile", "") or "").strip()
            role_session_id = str(metadata.get("delegation_role_session_id", "") or "").strip()
            seat_state_id = str(metadata.get("delegation_seat_state_id", "") or "").strip()
            if seat_state_id:
                seat_state_ids.append(seat_state_id)
            raw_runtime_resume = task.context_snapshot.get("runtime_resume", {}) if isinstance(task.context_snapshot, dict) else {}
            runtime_resume = dict(raw_runtime_resume) if isinstance(raw_runtime_resume, dict) else {}
            runtime_v2 = dict(metadata.get("runtime_v2", {}) or {})
            if runtime_resume or runtime_v2:
                native_runtime_resume_by_task[task.id] = {**runtime_v2, **runtime_resume}
            external_snapshot = await self._external_resume_snapshot_for_task(task)
            if external_snapshot:
                external_sessions_by_task[task.id] = external_snapshot
            (
                selected_execution_agent,
                assigned_external_agent,
                agent_selection_source,
            ) = self._task_effective_execution_agent_identity(task)
            employee_assignment = dict(metadata.get("employee_assignment", {}) or {})
            execution_identity = {
                "role_id": str(
                    task.assigned_to
                    or metadata.get("work_item_role_id", "")
                    or ""
                ).strip(),
                "seat_id": str(
                    metadata.get("delegation_seat_id", "")
                    or metadata.get("seat_id", "")
                    or ""
                ).strip(),
                "role_runtime_session_id": role_session_id,
                "employee_id": str(employee_assignment.get("employee_id", "") or "").strip(),
                "employee_assignment": copy.deepcopy(employee_assignment),
                "selected_execution_agent": selected_execution_agent,
                "assigned_external_agent": assigned_external_agent,
                "preferred_external_agent": str(
                    metadata.get("preferred_external_agent", "") or ""
                ).strip(),
                "execution_agent_locked": bool(metadata.get("execution_agent_locked", False)),
                "selected_execution_agent_source": str(
                    metadata.get("selected_execution_agent_source", "") or ""
                ).strip(),
                "agent_selection_source": agent_selection_source,
            }

            work_item_id = linked_work_item_id_for_task(task)
            work_item_snapshot: dict[str, Any] = {}
            if work_item_id and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    work_item_snapshot = {
                        "work_item_id": work_item_id,
                        "phase": (
                            getattr(work_item, "phase").value
                            if isinstance(getattr(work_item, "phase", None), Phase)
                            else str(getattr(work_item, "phase", "") or "")
                        ),
                        "role_id": str(getattr(work_item, "role_id", "") or ""),
                        "seat_id": str(getattr(work_item, "seat_id", "") or ""),
                        "role_runtime_session_id": str(getattr(work_item, "role_runtime_session_id", "") or ""),
                        "claimed_by_role_runtime_session_id": str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or ""),
                        "claimed_by_seat_id": str(getattr(work_item, "claimed_by_seat_id", "") or ""),
                        "projection_id": str(getattr(work_item, "projection_id", "") or ""),
                        "kind": str(getattr(work_item, "kind", "") or ""),
                        "metadata": dict(getattr(work_item, "metadata", {}) or {}),
                    }
                    execution_identity["seat_id"] = (
                        work_item_snapshot["seat_id"]
                        or execution_identity["seat_id"]
                    )
                    execution_identity["role_id"] = (
                        work_item_snapshot["role_id"]
                        or execution_identity["role_id"]
                    )
                    execution_identity["role_runtime_session_id"] = (
                        work_item_snapshot["role_runtime_session_id"]
                        or execution_identity["role_runtime_session_id"]
                    )
                    work_item_assignment = dict(
                        work_item_snapshot["metadata"].get("employee_assignment", {})
                        or {}
                    )
                    if "employee_assignment" in work_item_snapshot["metadata"]:
                        execution_identity["employee_id"] = str(
                            work_item_assignment.get("employee_id", "") or ""
                        ).strip()
                        execution_identity["employee_assignment"] = copy.deepcopy(
                            work_item_assignment
                        )
                    active_work_items.append(work_item_snapshot)

            role_session_id = str(
                execution_identity["role_runtime_session_id"] or ""
            ).strip()
            if role_session_id:
                role_runtime_session_ids.append(role_session_id)
            if role_session_id and callable(get_role_session):
                try:
                    role_session = await get_role_session(role_session_id)
                except Exception:
                    role_session = None
                if role_session is not None:
                    adapter_session_state_by_role[role_session_id] = dict(
                        getattr(role_session, "adapter_session_state", {}) or {}
                    )

            task_snapshots.append({
                "task_id": task.id,
                "session_id": task.session_id,
                "parent_session_id": task.parent_session_id,
                "status": task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
                "title": task.title,
                "assigned_to": task.assigned_to,
                "assigned_external_agent": assigned_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_identity": execution_identity,
                "work_item_id": work_item_id,
                "projection_id": projection_id_for_task(task),
                "turn_type": turn_type_for_task(task, fallback=""),
                "role_session_id": role_session_id,
                "seat_state_id": seat_state_id,
                "runtime_resume": native_runtime_resume_by_task.get(task.id, {}),
                "external_session": external_sessions_by_task.get(task.id, {}),
                "progress_tail": self._checkpoint_progress_tail(task),
                "work_item": work_item_snapshot,
            })

        payload: dict[str, Any] = {
            "version": 2,
            "stop_intent_id": stop_intent_id or "",
            "stop_state": "suspended",
            "suspend_started_at": now,
            "suspend_finalized_at": now,
            "checkpoint_type": checkpoint_type,
            "reason": reason,
            "project_id": project_id,
            "parent_session_id": parent_session_id,
            "session_id": parent_session_id,
            "origin_task_id": origin_task_id or "",
            "run_id": run_id,
            "company_profile": company_profile or getattr(self.config.org, "company_profile", "corporate"),
            "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
            "plan": serialize_company_work_item_runtime_plan(plan),
            "task_ids": [task.id for task in tasks],
            "active_work_items": active_work_items,
            "task_snapshots": task_snapshots,
            "role_runtime_session_ids": sorted(dict.fromkeys(role_runtime_session_ids)),
            "seat_state_ids": sorted(dict.fromkeys(seat_state_ids)),
            "native_runtime_resume": native_runtime_resume_by_task,
            "adapter_session_state": adapter_session_state_by_role,
            "external_sessions": external_sessions_by_task,
            "progress_tail": {
                task.id: self._checkpoint_progress_tail(task)
                for task in tasks
            },
            "created_at": now,
        }
        payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        return payload

    async def _build_company_runtime_suspend_checkpoint(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
        payload_updates: dict[str, Any] | None = None,
    ) -> ExecutionCheckpoint:
        payload = await self._company_runtime_checkpoint_payload(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
        )
        if payload_updates:
            payload.update(dict(payload_updates))
            payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        return ExecutionCheckpoint(
            project_id=self.project_id or "default",
            session_id=parent_session_id,
            checkpoint_type=checkpoint_type,
            task_id=origin_task_id,
            payload=payload,
        )

    async def _save_company_runtime_suspend_checkpoint(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
        payload_updates: dict[str, Any] | None = None,
    ) -> tuple[ExecutionCheckpoint, bool]:
        assert self.store
        checkpoint = await self._build_company_runtime_suspend_checkpoint(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
            payload_updates=payload_updates,
        )
        return await self.store.get_or_create_active_execution_checkpoint(
            checkpoint,
            checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
        )

    async def get_pending_company_runtime_suspend_checkpoint(
        self,
        parent_session_id: str | None,
    ) -> ExecutionCheckpoint | None:
        if not self.store:
            return None
        sid = str(parent_session_id or "").strip()
        if not sid:
            return None
        checkpoints = await self.store.get_pending_checkpoints(
            project_id=self.project_id or "default",
            session_id=sid,
            checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
        )
        return checkpoints[0] if checkpoints else None

    async def get_active_company_runtime_suspend_checkpoint(
        self,
        parent_session_id: str | None,
    ) -> ExecutionCheckpoint | None:
        if not self.store:
            return None
        sid = str(parent_session_id or "").strip()
        if not sid:
            return None
        getter = getattr(self.store, "get_execution_checkpoints", None)
        if callable(getter):
            checkpoints = await getter(
                project_id=self.project_id or "default",
                session_id=sid,
                checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
                statuses=["pending", "resuming"],
            )
            return checkpoints[0] if checkpoints else None
        return await self.get_pending_company_runtime_suspend_checkpoint(sid)

    @staticmethod
    def _suspend_target_phase(current: Phase | str | None) -> Phase:
        if isinstance(current, Phase):
            phase = current
        else:
            try:
                phase = Phase(str(current or Phase.READY.value))
            except ValueError:
                return Phase.READY
        if phase in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
            return phase
        if phase == Phase.RUNNING:
            return Phase.PAUSED
        if phase == Phase.READY_FOR_REWORK:
            return Phase.READY_FOR_REWORK
        if phase in {Phase.QUEUED, Phase.READY}:
            return phase
        return Phase.READY

    async def _company_runtime_task_is_fully_suspended(
        self,
        task: Task,
        *,
        checkpoint_type: str,
    ) -> bool:
        if not self.store:
            return False
        metadata = dict(task.metadata or {})
        if (
            str(metadata.get("company_runtime_stop_state", "") or "").strip() != "suspended"
            or str(metadata.get("company_runtime_suspend_checkpoint_type", "") or "").strip()
            != checkpoint_type
            or not str(metadata.get("company_runtime_suspended_at", "") or "").strip()
            or task.execution_lock
            or task.execution_locked_at is not None
        ):
            return False

        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            getter = getattr(self.store, "get_delegation_work_item", None)
            if not callable(getter):
                return False
            try:
                work_item = await getter(work_item_id)
            except Exception:
                return False
            if work_item is None:
                return False
            work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
            if getattr(work_item, "phase", None) not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
                if (
                    str(metadata.get("dispatch_hold", "") or "").strip()
                    != "company_runtime_suspended"
                    or str(work_item_metadata.get("dispatch_hold", "") or "").strip()
                    != "company_runtime_suspended"
                    or str(work_item_metadata.get("suspend_checkpoint_type", "") or "").strip()
                    != checkpoint_type
                    or str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or "").strip()
                    or str(getattr(work_item, "claimed_by_seat_id", "") or "").strip()
                    or str(work_item_metadata.get("claimed_by_role_session_id", "") or "").strip()
                    or str(work_item_metadata.get("claimed_task_id", "") or "").strip()
                ):
                    return False

        role_session_id = str(metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id:
            getter = getattr(self.store, "get_delegation_role_session", None)
            if callable(getter):
                try:
                    role_session = await getter(role_session_id)
                except Exception:
                    return False
                if role_session is not None and (
                    str(getattr(role_session, "focused_work_item_id", "") or "").strip()
                    or str(getattr(role_session, "status", "") or "").strip().lower() != "idle"
                ):
                    return False

        latest_session = await self._load_latest_external_session_for_task(task)
        if latest_session is not None:
            session_status = str(getattr(latest_session, "status", "") or "").strip().lower()
            if session_status not in {"suspended", "failed", "cancelled", "denied", "rejected"}:
                return False
        return True

    async def _suspend_company_runtime_tasks(
        self,
        tasks: list[Task],
        *,
        reason: str,
        checkpoint_type: str,
        stop_intent_id: str | None = None,
    ) -> list[str]:
        if not self.store:
            return []

        affected: list[str] = []
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        update_role_session = getattr(self.store, "update_delegation_role_session", None)
        update_work_item = getattr(self.store, "update_delegation_work_item", None)
        for task in tasks:
            work_item_id = linked_work_item_id_for_task(task)
            work_item = None
            if work_item_id:
                if not callable(get_work_item) or not callable(update_work_item):
                    raise RuntimeError(
                        f"company runtime cannot durably suspend work item {work_item_id}"
                    )
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    logger.opt(exception=True).error(
                        "company runtime suspend: failed to load authoritative work item {}",
                        work_item_id,
                    )
                    raise
                if work_item is None:
                    raise RuntimeError(
                        f"company runtime work item {work_item_id} no longer exists"
                    )
            task_is_terminal = task.status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            work_item_is_active = bool(
                work_item is not None
                and getattr(work_item, "phase", None)
                not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
            )
            if task_is_terminal and not work_item_is_active:
                continue
            if await self._company_runtime_task_is_fully_suspended(
                task,
                checkpoint_type=checkpoint_type,
            ):
                continue
            affected.append(task.id)
            task.metadata = dict(task.metadata or {})
            original_task_status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status or "")
            task.metadata["last_stop_reason"] = reason
            task.metadata["company_runtime_suspend_checkpoint_type"] = checkpoint_type
            task.metadata["company_runtime_suspended_at"] = datetime.now().isoformat()
            task.metadata["company_runtime_stop_state"] = "suspended"
            task.metadata["company_runtime_stop_intent_id"] = stop_intent_id or task.metadata.get("company_runtime_stop_intent_id", "")
            task.metadata["company_runtime_stop_marked_at"] = task.metadata.get("company_runtime_stop_marked_at") or datetime.now().isoformat()
            task.metadata.setdefault("suspended_task_status", original_task_status)
            task.execution_lock = False
            task.execution_locked_at = None
            task.result = task.result if task.result else None

            suspended_phase: Phase | None = None
            if work_item_id:
                if (
                    getattr(work_item, "phase", None)
                    not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
                ):
                    current_phase = getattr(work_item, "phase", None)
                    suspended_phase = current_phase if isinstance(current_phase, Phase) else self._suspend_target_phase(current_phase)
                    work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
                    original_claim = {
                        "claimed_by_role_runtime_session_id": str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or ""),
                        "claimed_by_seat_id": str(getattr(work_item, "claimed_by_seat_id", "") or ""),
                        "claimed_by_role_session_id": str(work_item_metadata.get("claimed_by_role_session_id", "") or ""),
                        "claimed_task_id": str(work_item_metadata.get("claimed_task_id", "") or task.id),
                    }
                    try:
                        await update_work_item(
                            work_item_id,
                            metadata_updates={
                                "dispatch_hold": "company_runtime_suspended",
                                "suspended_at": datetime.now().isoformat(),
                                "suspend_reason": reason,
                                "suspend_checkpoint_type": checkpoint_type,
                                "suspend_intent_id": stop_intent_id or "",
                                "suspended_phase": suspended_phase.value,
                                "suspended_task_status": original_task_status,
                                "suspended_claim": original_claim,
                                "claimed_by_role_session_id": "",
                                "claimed_task_id": "",
                            },
                            claimed_by_role_runtime_session_id="",
                            claimed_by_seat_id="",
                        )
                    except Exception:
                        logger.opt(exception=True).error(
                            "company runtime suspend: authoritative hold/release failed for {}",
                            work_item_id,
                        )
                        raise
                    else:
                        task.metadata["dispatch_hold"] = "company_runtime_suspended"
                        task.metadata["suspended_phase"] = suspended_phase.value

            latest_session = await self._load_latest_external_session_for_task(task)
            if latest_session is not None:
                try:
                    session_status = str(getattr(latest_session, "status", "") or "").strip().lower()
                    if session_status not in {"failed", "cancelled", "denied", "rejected"}:
                        latest_session.status = "suspended"
                        latest_session.metadata = {
                            **dict(getattr(latest_session, "metadata", {}) or {}),
                            "company_runtime_suspended_at": datetime.now().isoformat(),
                            "company_runtime_suspend_checkpoint_type": checkpoint_type,
                            "company_runtime_stop_intent_id": stop_intent_id or "",
                        }
                        await self.store.save_external_session(latest_session)
                except Exception:
                    logger.opt(exception=True).debug("company runtime suspend: external session status update failed")

            fresh = await self.store.get_task(task.id)
            target = fresh or task
            target.metadata = {**dict(getattr(target, "metadata", {}) or {}), **dict(task.metadata or {})}
            target.execution_lock = False
            target.execution_locked_at = None
            if task_is_terminal:
                # The WorkItem hold write fires the generic phase projection
                # hook, which can briefly rewrite a stale terminal Task to the
                # authoritative nonterminal phase.  A suspend is not a resume:
                # retain the pre-existing Task projection while attaching the
                # durable hold; resume will project the WorkItem deliberately.
                target.status = task.status
            elif target.status not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                if suspended_phase is not None:
                    target.status = task_status_for_phase(suspended_phase)
                elif original_task_status:
                    try:
                        target.status = TaskStatus(original_task_status)
                    except ValueError:
                        target.status = TaskStatus.IDLE
            await self.store.save_task(target)

            role_session_id = str((target.metadata or {}).get("delegation_role_session_id", "") or "").strip()
            if role_session_id and callable(update_role_session):
                try:
                    await update_role_session(
                        role_session_id,
                        focused_work_item_id="",
                        status="idle",
                        metadata_updates={
                            "last_suspend_reason": reason,
                            "last_suspend_checkpoint_type": checkpoint_type,
                            "last_suspended_at": datetime.now().isoformat(),
                            "last_suspend_intent_id": stop_intent_id or "",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("company runtime suspend: role session idle update failed")
        return affected

    async def _checkpoint_and_suspend_company_runtime_scope(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
    ) -> tuple[ExecutionCheckpoint, bool, list[str]]:
        """Install durable holds before publishing a resumable checkpoint.

        A pending checkpoint is an execution capability, so exposing it before
        WorkItem/Task holds are committed lets another controller resume while
        Stop is still suspending the scope.  Build the pre-stop snapshot first,
        commit all holds, and only then atomically create (or normalize) the
        single active checkpoint.
        """

        assert self.store
        candidate = await self._build_company_runtime_suspend_checkpoint(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
        )
        existing = await self.get_active_company_runtime_suspend_checkpoint(
            parent_session_id
        )
        checkpoint = existing or candidate
        payload = dict(checkpoint.payload or {})
        effective_reason = str(payload.get("reason", "") or reason)
        effective_type = str(checkpoint.checkpoint_type or checkpoint_type)
        effective_stop_intent_id = str(
            payload.get("stop_intent_id", "") or stop_intent_id or ""
        )
        affected = await self._suspend_company_runtime_tasks(
            tasks,
            reason=effective_reason,
            checkpoint_type=effective_type,
            stop_intent_id=effective_stop_intent_id,
        )

        created = False
        if existing is None:
            checkpoint, created = (
                await self.store.get_or_create_active_execution_checkpoint(
                    candidate,
                    checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
                )
            )
            if checkpoint.checkpoint_id != candidate.checkpoint_id:
                payload = dict(checkpoint.payload or {})
                affected.extend(
                    await self._suspend_company_runtime_tasks(
                        tasks,
                        reason=str(payload.get("reason", "") or reason),
                        checkpoint_type=str(
                            checkpoint.checkpoint_type or checkpoint_type
                        ),
                        stop_intent_id=str(
                            payload.get("stop_intent_id", "")
                            or stop_intent_id
                            or ""
                        ),
                    )
                )

        # Stop wins over an in-flight resume only after its holds are durable.
        # If resume completed in the meantime, create a fresh checkpoint over
        # those already-installed holds instead of leaving the scope held with
        # no resumable fact.
        for _attempt in range(4):
            status = str(checkpoint.status or "").strip().lower()
            if status in {"pending", "resuming"}:
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="pending",
                    payload_updates=(
                        {
                            "resume_state": "interrupted",
                            "resume_interrupted_at": datetime.now().isoformat(),
                            "resume_interruption_reason": reason,
                        }
                        if status == "resuming"
                        else None
                    ),
                    expected_statuses={status},
                )
                if transitioned:
                    return checkpoint, created, list(dict.fromkeys(affected))
                refreshed = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if refreshed is not None:
                    checkpoint = refreshed
                    continue

            replacement = ExecutionCheckpoint(
                project_id=candidate.project_id,
                session_id=candidate.session_id,
                checkpoint_type=candidate.checkpoint_type,
                task_id=candidate.task_id,
                payload=dict(candidate.payload or {}),
            )
            checkpoint, replacement_created = (
                await self.store.get_or_create_active_execution_checkpoint(
                    replacement,
                    checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
                )
            )
            created = created or replacement_created

        raise RuntimeError(
            "company runtime checkpoint kept changing while suspension was finalized"
        )

    async def suspend_company_runtime(
        self,
        *,
        origin_task_id: str,
        session_id: str | None = None,
        reason: str = "user_stop",
        checkpoint_type: str = "company_runtime_suspended",
        stop_intent_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.store:
            return None
        project_id = str(self.project_id or "default").strip() or "default"
        requested_session_id = str(session_id or "").strip()
        identity_index = await load_company_runtime_identity_index(
            self.store,
            project_id,
        )
        identity = identity_index.resolve(task_id=origin_task_id)
        if identity is None and requested_session_id:
            identity = identity_index.resolve(runtime_session_id=requested_session_id)
        if identity is None:
            return None
        parent_session_id = identity.runtime_session_id
        if requested_session_id and requested_session_id != parent_session_id:
            requested_identity = identity_index.resolve(
                runtime_session_id=requested_session_id,
            )
            if requested_identity is None or requested_identity != identity:
                return None

        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            snapshot = await self._load_company_runtime_snapshot(parent_session_id)
            if not snapshot:
                return None
            plan, tasks = snapshot
            if not tasks:
                return None

            checkpoint, created, affected_ids = (
                await self._checkpoint_and_suspend_company_runtime_scope(
                    checkpoint_type=checkpoint_type,
                    reason=reason,
                    parent_session_id=parent_session_id,
                    origin_task_id=origin_task_id,
                    plan=plan,
                    tasks=tasks,
                    stop_intent_id=stop_intent_id,
                )
            )
            idempotent = not created

            payload = dict(checkpoint.payload or {})
            effective_stop_intent_id = str(
                payload.get("stop_intent_id", "") or stop_intent_id or ""
            )
            return {
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "session_id": parent_session_id,
                "task_ids": affected_ids,
                "stop_intent_id": effective_stop_intent_id,
                "idempotent": idempotent,
            }

    @staticmethod
    def _company_runtime_dependencies_satisfied(
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> bool:
        from opc.layer2_organization.work_item_transition import (
            settled_failure_dependency_ids,
        )

        metadata = dict(getattr(work_item, "metadata", {}) or {})
        dependency_ids = [
            str(item).strip()
            for item in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        if not dependency_ids:
            return True
        dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
        settled_failure_ids = settled_failure_dependency_ids(metadata)
        for dep_id in dependency_ids:
            dependency = work_item_by_id.get(dep_id)
            if dependency is None:
                continue
            dep_phase = getattr(dependency, "phase", None)
            dep_class = str(dependency_classes.get(dep_id, "hard") or "hard").strip().lower()
            if dep_class == "info":
                continue
            if dep_class == "soft":
                if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                    if dep_id in settled_failure_ids:
                        continue
                    return False
                continue
            if dep_phase != Phase.APPROVED:
                # Failure-triage release: the frontier pass released this
                # card over the dep (dependency_settlement stamp). Stop/
                # resume must not regress a released triage card back into
                # WAITING_* — with the failed dep already terminal, no
                # later event would ever wake it again.
                if dep_id in settled_failure_ids:
                    continue
                return False
        return True

    @classmethod
    def _company_runtime_resume_target_phase(
        cls,
        work_item: DelegationWorkItem,
        restored_phase: Phase | None,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> Phase:
        current_phase = getattr(work_item, "phase", Phase.READY)
        if not isinstance(current_phase, Phase):
            try:
                current_phase = Phase(str(current_phase or Phase.READY.value))
            except ValueError:
                current_phase = Phase.READY
        if current_phase in DONE_PHASES:
            return current_phase
        original_phase = restored_phase or current_phase
        if original_phase in DONE_PHASES:
            return current_phase
        if original_phase in IN_REVIEW_PHASES:
            return original_phase
        if original_phase == Phase.WAITING_DEPENDENCIES:
            return Phase.WAITING_DEPENDENCIES

        deps_satisfied = cls._company_runtime_dependencies_satisfied(work_item, work_item_by_id)
        if not deps_satisfied:
            if original_phase == Phase.RUNNING:
                return Phase.WAITING_FOR_CHILDREN
            if original_phase in {Phase.READY, Phase.READY_FOR_REWORK, Phase.QUEUED}:
                return Phase.WAITING_DEPENDENCIES
            return original_phase
        if original_phase == Phase.QUEUED:
            return Phase.READY
        return original_phase

    @staticmethod
    def _checkpoint_task_execution_identity(
        task_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the immutable execution identity captured at suspension.

        Checkpoints created before the explicit ``execution_identity`` field
        already contain enough role/agent data to derive a safe identity.  The
        derivation is intentionally local to checkpoint consumption; it is not
        a second runtime resolver.
        """

        explicit = dict(task_snapshot.get("execution_identity", {}) or {})
        work_item_snapshot = dict(task_snapshot.get("work_item", {}) or {})
        work_item_metadata = dict(work_item_snapshot.get("metadata", {}) or {})
        employee_assignment = dict(
            explicit.get("employee_assignment", {})
            or work_item_metadata.get("employee_assignment", {})
            or {}
        )
        assigned_external_agent = str(
            explicit.get("assigned_external_agent")
            if "assigned_external_agent" in explicit
            else task_snapshot.get("assigned_external_agent", "")
            or ""
        ).strip()
        selected_execution_agent = normalize_recruitment_agent_choice(
            explicit.get("selected_execution_agent")
            or task_snapshot.get("selected_execution_agent"),
            default=assigned_external_agent or "native",
        ) or "native"
        return {
            "role_id": str(
                explicit.get("role_id")
                or work_item_snapshot.get("role_id")
                or task_snapshot.get("assigned_to")
                or ""
            ).strip(),
            "seat_id": str(
                explicit.get("seat_id")
                or work_item_snapshot.get("seat_id")
                or ""
            ).strip(),
            "role_runtime_session_id": str(
                explicit.get("role_runtime_session_id")
                or work_item_snapshot.get("role_runtime_session_id")
                or task_snapshot.get("role_session_id")
                or ""
            ).strip(),
            "employee_id": str(
                explicit.get("employee_id")
                or employee_assignment.get("employee_id")
                or ""
            ).strip(),
            "employee_assignment": copy.deepcopy(employee_assignment),
            "selected_execution_agent": selected_execution_agent,
            "assigned_external_agent": assigned_external_agent,
            "preferred_external_agent": str(
                explicit.get("preferred_external_agent", "") or ""
            ).strip(),
            "execution_agent_locked": (
                bool(explicit.get("execution_agent_locked", False))
                if "execution_agent_locked" in explicit
                else None
            ),
            "selected_execution_agent_source": str(
                explicit.get("agent_selection_source")
                or explicit.get("selected_execution_agent_source", "")
                or ""
            ).strip(),
            "explicit": bool(explicit),
        }

    @classmethod
    def _restore_and_pin_company_resume_execution_identity(
        cls,
        task: Task,
        work_item: DelegationWorkItem | None,
        task_snapshot: dict[str, Any],
        role_session: DelegationRoleSession | None,
        *,
        checkpoint_id: str,
    ) -> None:
        """Validate durable role identity and pin this resumed attempt's agent.

        A resume must continue the suspended actor, not re-run recruitment or
        adaptive backend selection.  Non-empty durable values that disagree
        with the checkpoint fail closed.  Missing Task projection fields are
        restored from the checkpoint after the authoritative WorkItem has also
        been checked.
        """

        identity = cls._checkpoint_task_execution_identity(task_snapshot)
        metadata = dict(task.metadata or {})
        task_role_id = str(
            task.assigned_to or metadata.get("work_item_role_id", "") or ""
        ).strip()
        task_seat_id = str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()
        task_role_session_id = str(
            metadata.get("delegation_role_session_id", "") or ""
        ).strip()
        task_assignment = dict(metadata.get("employee_assignment", {}) or {})
        task_employee_id = str(task_assignment.get("employee_id", "") or "").strip()

        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
        work_item_assignment = dict(
            work_item_metadata.get("employee_assignment", {}) or {}
        )
        if work_item is not None:
            current_values: dict[str, list[str]] = {
                "role_id": [str(getattr(work_item, "role_id", "") or "").strip()],
                "seat_id": [str(getattr(work_item, "seat_id", "") or "").strip()],
                "role_runtime_session_id": [str(
                    getattr(work_item, "role_runtime_session_id", "") or ""
                ).strip()],
                "employee_id": [str(
                    work_item_assignment.get("employee_id", "") or ""
                ).strip()],
            }
        else:
            current_values = {
                "role_id": [task_role_id],
                "seat_id": [task_seat_id],
                "role_runtime_session_id": [task_role_session_id],
                "employee_id": [task_employee_id],
            }
        for field_name, values in current_values.items():
            expected = str(identity.get(field_name, "") or "").strip()
            if not expected:
                continue
            for current in values:
                if current and current != expected:
                    raise RuntimeError(
                        "company runtime resume identity mismatch for "
                        f"task {task.id}: {field_name}={current!r}, "
                        f"checkpoint={expected!r}"
                    )

        expected_role_session_id = str(
            identity.get("role_runtime_session_id", "") or ""
        ).strip()
        if expected_role_session_id and identity.get("explicit") and role_session is None:
            raise RuntimeError(
                "company runtime resume identity mismatch for "
                f"task {task.id}: role runtime session {expected_role_session_id!r} is missing"
            )
        if role_session is not None:
            # A role session's scalar seat is its home/primary seat, while the
            # checkpoint seat is the authoritative WorkItem's placement.
            role_session_values = {
                "role_id": str(getattr(role_session, "role_id", "") or "").strip(),
                "employee_id": str(getattr(role_session, "employee_id", "") or "").strip(),
            }
            for field_name, current in role_session_values.items():
                expected = str(identity.get(field_name, "") or "").strip()
                if expected and current and current != expected:
                    raise RuntimeError(
                        "company runtime resume identity mismatch for "
                        f"task {task.id}: role session {field_name}={current!r}, "
                        f"checkpoint={expected!r}"
                    )

        expected_agent = str(
            identity.get("selected_execution_agent", "") or "native"
        ).strip()
        expected_assigned_agent = str(
            identity.get("assigned_external_agent", "") or ""
        ).strip()
        if expected_agent == "native":
            if expected_assigned_agent:
                raise RuntimeError(
                    f"company runtime resume checkpoint has inconsistent native agent for task {task.id}"
                )
        elif expected_assigned_agent and expected_assigned_agent != expected_agent:
            raise RuntimeError(
                f"company runtime resume checkpoint has inconsistent external agent for task {task.id}"
            )
        else:
            expected_assigned_agent = expected_agent

        current_agent, current_assigned_agent, _current_source = (
            cls._task_effective_execution_agent_identity(task)
        )
        if (
            current_agent != expected_agent
            or current_assigned_agent != expected_assigned_agent
        ):
            raise RuntimeError(
                "company runtime resume identity mismatch for "
                f"task {task.id}: execution_agent={current_agent!r}, "
                f"checkpoint={expected_agent!r}"
            )

        expected_source = str(
            identity.get("selected_execution_agent_source", "") or ""
        ).strip()

        metadata["work_item_role_id"] = identity["role_id"] or task_role_id
        if identity["seat_id"]:
            metadata["delegation_seat_id"] = identity["seat_id"]
        if identity["role_runtime_session_id"]:
            metadata["delegation_role_session_id"] = identity[
                "role_runtime_session_id"
            ]
        if identity.get("explicit") or identity["employee_assignment"]:
            metadata["employee_assignment"] = copy.deepcopy(
                identity["employee_assignment"]
            )
        metadata["selected_execution_agent"] = expected_agent
        metadata["preferred_external_agent"] = (
            str(identity.get("preferred_external_agent", "") or "").strip()
            or (expected_assigned_agent if expected_agent != "native" else None)
        )
        metadata["_company_runtime_resume_execution_agent_pin"] = {
            "checkpoint_id": str(checkpoint_id or "").strip(),
            "selected_execution_agent": expected_agent,
            "assigned_external_agent": expected_assigned_agent,
            "selected_execution_agent_source": expected_source,
        }
        task.assigned_to = identity["role_id"] or task_role_id
        task.assigned_external_agent = expected_assigned_agent or None
        task.metadata = metadata

    async def _prepare_company_runtime_tasks_for_resume(
        self,
        tasks: list[Task],
        payload: dict[str, Any],
        *,
        resume_task_ids: set[str] | None = None,
    ) -> list[Task]:
        assert self.store

        task_snapshot_by_id = {
            str(item.get("task_id", "") or "").strip(): dict(item)
            for item in list(payload.get("task_snapshots", []) or [])
            if isinstance(item, dict) and str(item.get("task_id", "") or "").strip()
        }
        work_item_snapshot_by_id = {
            str(item.get("work_item_id", "") or "").strip(): dict(item)
            for item in list(payload.get("active_work_items", []) or [])
            if isinstance(item, dict) and str(item.get("work_item_id", "") or "").strip()
        }
        refreshed: list[Task] = []
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        get_role_session = getattr(self.store, "get_delegation_role_session", None)
        list_work_items = getattr(self.store, "list_delegation_work_items", None)
        update_role_session = getattr(self.store, "update_delegation_role_session", None)
        update_work_item = getattr(self.store, "update_delegation_work_item", None)
        work_item_by_id: dict[str, DelegationWorkItem] = {}
        run_ids = {
            str((getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip()
            for task in tasks
            if str((getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip()
        }
        if callable(list_work_items):
            for run_id in sorted(run_ids):
                try:
                    for item in await list_work_items(run_id):
                        work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
                        if work_item_id:
                            work_item_by_id[work_item_id] = item
                except Exception:
                    logger.opt(exception=True).debug("company runtime resume: failed to load run work items")
        for task in tasks:
            if resume_task_ids is not None and task.id not in resume_task_ids:
                refreshed.append(task)
                continue
            work_item_id = linked_work_item_id_for_task(task)
            work_item = work_item_by_id.get(work_item_id)
            if work_item_id and (
                not callable(get_work_item) or not callable(update_work_item)
            ):
                raise RuntimeError(
                    f"company runtime cannot durably resume work item {work_item_id}"
                )
            if work_item is None and work_item_id:
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    logger.opt(exception=True).error(
                        "company runtime resume: failed to load authoritative work item {}",
                        work_item_id,
                    )
                    raise
                if work_item is not None:
                    work_item_by_id[work_item_id] = work_item
            if work_item_id and work_item is None:
                raise RuntimeError(
                    f"company runtime work item {work_item_id} no longer exists"
                )
            task_is_terminal = task.status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            work_item_is_nonterminal = bool(
                work_item is not None
                and getattr(work_item, "phase", None) not in DONE_PHASES
            )
            if task_is_terminal and not work_item_is_nonterminal:
                refreshed.append(task)
                continue
            task_snapshot = task_snapshot_by_id.get(task.id, {})
            if not task_snapshot:
                raise RuntimeError(
                    f"company runtime resume checkpoint has no task identity snapshot for {task.id}"
                )
            identity = self._checkpoint_task_execution_identity(task_snapshot)
            # Resume availability gate: the checkpoint pins one exact
            # execution backend, so a pinned external agent that is disabled
            # (or whose binary vanished) makes this work item deterministically
            # un-runnable — every dispatch would claim, transition to RUNNING,
            # and crash on the selector's availability check. Fail the item
            # closed HERE, before any pin/claim, with a visible diagnostic;
            # the rest of the organization resumes normally and the failed-
            # child settlement machinery hands the card to manager triage.
            pinned_agent = str(
                identity.get("selected_execution_agent", "") or "native"
            ).strip() or "native"
            work_item_already_terminal = bool(
                work_item is not None
                and getattr(work_item, "phase", None) in DONE_PHASES
            )
            # A missing adapter registry means availability is UNKNOWN (engine
            # not fully initialized / delegate context) — fail open and let the
            # dispatch-time selector guard decide; only a registry that exists
            # and excludes the pinned agent is proof of unavailability.
            if (
                pinned_agent != "native"
                and not work_item_already_terminal
                and self.adapter_registry is not None
                and pinned_agent not in self._available_external_agents()
            ):
                diagnostic = (
                    f"Cannot resume work item: its execution is pinned to external agent "
                    f"'{pinned_agent}', which is currently disabled or unavailable. "
                    f"Re-enable '{pinned_agent}' in the config and restart to run it, "
                    "or re-issue the work as a new request."
                )
                task.metadata = dict(task.metadata or {})
                for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                    task.metadata.pop(key, None)
                progress = list(task.metadata.get("progress_log", []) or [])
                progress.append(diagnostic)
                task.metadata["progress_log"] = progress[-20:]
                task.metadata["resume_unavailable_external_agent"] = pinned_agent
                existing_result = dict(task.result or {})
                existing_result["content"] = diagnostic
                task.result = existing_result
                await self._fail_task_via_phase(
                    task,
                    reason="resume_external_agent_unavailable",
                )
                if work_item_id and callable(update_work_item):
                    try:
                        await update_work_item(
                            work_item_id,
                            blocked_reason=diagnostic,
                            metadata_updates={"dispatch_hold": ""},
                        )
                    except Exception:
                        logger.opt(exception=True).debug(
                            "company runtime resume: blocked_reason write failed for {}",
                            work_item_id,
                        )
                if self.on_progress:
                    try:
                        await self.on_progress(
                            f"[Company:{projection_id_for_task(task) or task.title}] {diagnostic}"
                        )
                    except Exception:
                        logger.opt(exception=True).debug(
                            "company runtime resume: unavailable-agent progress emit failed"
                        )
                logger.warning(
                    "company runtime resume: failed work item {} closed — pinned external "
                    "agent {!r} unavailable",
                    work_item_id or task.id,
                    pinned_agent,
                )
                fresh = await self.store.get_task(task.id)
                refreshed.append(fresh or task)
                continue
            expected_role_session_id = str(
                identity.get("role_runtime_session_id", "") or ""
            ).strip()
            role_session = None
            if expected_role_session_id and callable(get_role_session):
                role_session = await get_role_session(expected_role_session_id)
            self._restore_and_pin_company_resume_execution_identity(
                task,
                work_item,
                task_snapshot,
                role_session,
                checkpoint_id=str(payload.get("checkpoint_id", "") or ""),
            )
            task.metadata = dict(task.metadata or {})
            task.context_snapshot = dict(task.context_snapshot or {})
            runtime_resume = dict(payload.get("native_runtime_resume", {}) or {}).get(task.id)
            if isinstance(runtime_resume, dict) and runtime_resume:
                task.context_snapshot["runtime_resume"] = dict(runtime_resume)
                task.metadata["runtime_v2"] = dict(runtime_resume)
            external_sessions = dict(payload.get("external_sessions", {}) or {})
            external_session = external_sessions.get(task.id)
            task.metadata.pop("external_resume_checkpoint_session_updated_at", None)
            task.metadata.pop("external_resume_checkpoint_session_status", None)
            if isinstance(external_session, dict):
                token_allowed = external_session_status_allows_resume(
                    external_session.get("status")
                )
                agent_type = str(
                    external_session.get("agent_type")
                    or task.assigned_external_agent
                    or ""
                ).strip()
                assigned_agent_type = str(task.assigned_external_agent or "").strip()
                token_candidates = [
                    str(external_session.get("resume_session_id") or "").strip(),
                    str(external_session.get("provider_session_id") or "").strip(),
                    str(external_session.get("session_id") or "").strip(),
                ]
                token = next(
                    (
                        candidate
                        for candidate in token_candidates
                        if is_provider_session_token(
                            candidate,
                            agent_type=agent_type,
                            project_id=str(task.project_id or "default"),
                        )
                    ),
                    "",
                )
                if (
                    token
                    and agent_type
                    and token_allowed
                    and agent_type == assigned_agent_type
                ):
                    task.metadata["external_resume_session_id"] = token
                    task.metadata["external_resume_agent_type"] = agent_type
                    task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
                    task.metadata["external_resume_checkpoint_session_updated_at"] = str(
                        external_session.get("updated_at", "") or ""
                    ).strip()
                    task.metadata["external_resume_checkpoint_session_status"] = str(
                        external_session.get("status", "") or ""
                    ).strip()
                    task.metadata.pop("external_resume_fallback", None)
                elif task.assigned_external_agent:
                    task.metadata.pop("external_resume_session_id", None)
                    task.metadata.pop("external_resume_agent_type", None)
                    task.metadata.pop("external_resume_session_scope_id", None)
                    task.metadata["external_resume_fallback"] = "context_replay"
            task.execution_lock = False
            task.execution_locked_at = None
            task.result = None
            work_item_snapshot = work_item_snapshot_by_id.get(work_item_id, {})
            task_work_item_snapshot = task_snapshot.get("work_item", {})
            phase_value = str(work_item_snapshot.get("phase", "") or "").strip()
            if not phase_value and isinstance(task_work_item_snapshot, dict):
                phase_value = str(task_work_item_snapshot.get("phase", "") or "").strip()
            try:
                restored_phase = Phase(phase_value) if phase_value else None
            except ValueError:
                restored_phase = None
            work_item_phase = getattr(work_item, "phase", None) if work_item is not None else None
            if work_item is not None and work_item_phase in DONE_PHASES:
                task.status = task_status_for_phase(work_item_phase)
                task.execution_lock = False
                task.execution_locked_at = None
                for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                    task.metadata.pop(key, None)
                task.metadata["company_runtime_resume_checkpoint_id"] = str(payload.get("checkpoint_id", "") or "")
                task.metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
                await self.store.save_task(task)
                fresh = await self.store.get_task(task.id)
                refreshed.append(fresh or task)
                continue
            target_phase: Phase | None = None
            if work_item is not None and getattr(work_item, "phase", None) not in DONE_PHASES:
                target_phase = self._company_runtime_resume_target_phase(
                    work_item,
                    restored_phase,
                    work_item_by_id,
                )
                task.status = task_status_for_phase(target_phase)
            elif restored_phase is not None and restored_phase not in DONE_PHASES:
                target_phase = restored_phase
                task.status = task_status_for_phase(restored_phase)
            else:
                suspended_status = str(
                    task.metadata.get("suspended_task_status")
                    or task_snapshot.get("status")
                    or ""
                ).strip()
                try:
                    task.status = TaskStatus(suspended_status) if suspended_status else TaskStatus.PENDING
                except ValueError:
                    task.status = TaskStatus.PENDING
            for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                task.metadata.pop(key, None)
            task.metadata["company_runtime_resume_checkpoint_id"] = str(payload.get("checkpoint_id", "") or "")
            task.metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
            progress = list(task.metadata.get("progress_log", []) or [])
            progress.append("Resumed from company runtime suspend checkpoint.")
            task.metadata["progress_log"] = progress[-20:]

            if work_item_id:
                if work_item is None:
                    try:
                        work_item = await get_work_item(work_item_id)
                    except Exception:
                        logger.opt(exception=True).error(
                            "company runtime resume: failed to reload authoritative work item {}",
                            work_item_id,
                        )
                        raise
                if work_item is not None and getattr(work_item, "phase", None) not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
                    phase_kwargs: dict[str, Any] = {}
                    if target_phase is not None and target_phase not in DONE_PHASES:
                        current_phase = getattr(work_item, "phase", None)
                        if current_phase != target_phase:
                            phase_kwargs["phase"] = target_phase
                    try:
                        await update_work_item(
                            work_item_id,
                            **phase_kwargs,
                            metadata_updates={
                                "dispatch_hold": "",
                                "resume_requested_at": datetime.now().isoformat(),
                                "resume_source_checkpoint_id": str(payload.get("checkpoint_id", "") or ""),
                                "resume_source_checkpoint_type": str(payload.get("checkpoint_type", "") or ""),
                                "claimed_by_role_session_id": "",
                                "claimed_task_id": "",
                            },
                            claimed_by_role_runtime_session_id="",
                            claimed_by_seat_id="",
                        )
                    except Exception:
                        logger.opt(exception=True).debug("company runtime resume: phase restore/hold clear failed")
                        try:
                            await update_work_item(
                                work_item_id,
                                metadata_updates={
                                    "dispatch_hold": "",
                                    "resume_requested_at": datetime.now().isoformat(),
                                    "resume_source_checkpoint_id": str(payload.get("checkpoint_id", "") or ""),
                                },
                                claimed_by_role_runtime_session_id="",
                                claimed_by_seat_id="",
                            )
                        except Exception:
                            logger.opt(exception=True).error(
                                "company runtime resume: authoritative fallback hold clear failed"
                            )
                            raise

            role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
            if role_session_id and callable(update_role_session):
                try:
                    await update_role_session(
                        role_session_id,
                        focused_work_item_id="",
                        status="idle",
                        metadata_updates={
                            "last_resume_checkpoint_type": str(payload.get("checkpoint_type", "") or ""),
                            "last_resume_requested_at": datetime.now().isoformat(),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("company runtime resume: role session update failed")
            await self.store.save_task(task)
            fresh = await self.store.get_task(task.id)
            refreshed.append(fresh or task)
        return refreshed

    async def _clear_company_runtime_parent_stop_state(
        self,
        parent_session_id: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.store:
            return
        parent_session_id = str(parent_session_id or "").strip()
        if not parent_session_id:
            return
        checkpoint_id = str(payload.get("checkpoint_id", "") or "").strip()
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(
            runtime_session_id=parent_session_id,
            checkpoint_id=checkpoint_id,
        )
        if identity is None or not identity.ui_anchor_task_id:
            return
        task = identity_index.task(identity.ui_anchor_task_id)
        if task is None or not is_pure_company_ui_anchor(
            task,
            identity.runtime_session_id,
        ):
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not any(key in metadata for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS):
            return
        for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
            metadata.pop(key, None)
        metadata["company_runtime_resume_checkpoint_id"] = checkpoint_id
        metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
        task.metadata = metadata
        task.execution_lock = False
        task.execution_locked_at = None
        await self.store.save_task(task)

    @staticmethod
    def _clear_pending_reorg_marker(task: Task) -> None:
        task.metadata = dict(task.metadata)
        task.metadata.pop("pending_reorg_proposal_id", None)
        task.metadata.pop("pending_reorg_scope", None)

    async def _reconcile_company_work_item_plan_state(
        self,
        parent_session_id: str,
        plan: CompanyWorkItemRuntimePlan,
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]] | None:
        if not self.store:
            return None
        snapshot = await self._load_company_runtime_snapshot(parent_session_id)
        if not snapshot:
            return None
        _, tasks = snapshot
        ordered_tasks = list(tasks)
        all_task_ids = [task.id for task in ordered_tasks]
        serialized_plan = serialize_company_work_item_runtime_plan(plan)
        current_org_version = self.org_engine.current_org_version() if self.org_engine else 1
        current_runtime_topology_version = self.org_engine.current_runtime_topology_version() if self.org_engine else 1

        for task in ordered_tasks:
            self._clear_pending_reorg_marker(task)
            task.metadata["company_work_item_plan"] = serialized_plan
            task.metadata["execution_task_ids"] = list(all_task_ids)
            task.metadata["org_version"] = current_org_version
            task.metadata["runtime_topology_version"] = current_runtime_topology_version
            await self.store.save_task(task)
        return plan, ordered_tasks

    @staticmethod
    def _uses_primary_session_external_continuity(task: Task) -> bool:
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not session_id:
            return False
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        return mode == "task" or task_mode_contract == "single_full_capability_main_agent"

    async def _load_latest_external_session_for_task(self, task: Task) -> Any | None:
        if not self.store or not task.id:
            return None
        project_id = task.project_id or self.project_id or "default"
        agent_type = str(task.assigned_external_agent or "").strip()
        fallback = getattr(self.store, "get_external_session", None)
        if (
            callable(fallback)
            and agent_type
            and self._uses_primary_session_external_continuity(task)
            and str(getattr(task, "session_id", "") or "").strip()
        ):
            session = await fallback(
                agent_type,
                project_id,
                opc_session_id=str(task.session_id or "").strip(),
            )
            if session:
                return session
        getter = getattr(self.store, "get_latest_external_session_for_task", None)
        if callable(getter):
            try:
                session = await getter(project_id, task.id)
                if (
                    session
                    and agent_type
                    and str(getattr(session, "agent_type", "") or "").strip() != agent_type
                ):
                    if callable(fallback):
                        return await fallback(agent_type, project_id, task_id=task.id)
                    return None
                return session
            except TypeError:
                pass
        if not agent_type:
            return None
        if not callable(fallback):
            return None
        return await fallback(agent_type, project_id, task_id=task.id)

    async def _load_best_external_resume_session_for_task(
        self,
        task: Task,
    ) -> Any | None:
        """Prefer a real provider thread over monitoring placeholder rows.

        A live run initially owns a synthetic ``agent:project:task`` row.  The
        provider may publish its real thread id milliseconds later with the
        same (or an older) timestamp.  Recency alone is therefore not a valid
        resume-token selector.
        """

        if not self.store or not task.id:
            return None
        agent_type = str(task.assigned_external_agent or "").strip()
        if not agent_type:
            return None
        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return None
        try:
            sessions = await list_sessions(
                project_id=task.project_id or self.project_id or "default",
                task_id=task.id,
                limit=100,
            )
        except TypeError:
            return None
        except Exception:
            logger.opt(exception=True).debug(
                "failed to list external resume-session candidates"
            )
            return None
        selected, _token = select_best_external_resume_session(
            sessions,
            agent_type=agent_type,
            project_id=str(task.project_id or self.project_id or "default"),
        )
        return selected

    async def _checkpoint_external_resume_token_was_terminalized(
        self,
        task: Task,
        *,
        agent_type: str,
        token: str,
        checkpoint_updated_at: str,
        checkpoint_status: str,
    ) -> bool:
        """Let a newer durable terminal row veto a checkpoint's working token."""

        if not self.store or not task.id or not checkpoint_updated_at:
            return False
        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return False
        try:
            sessions = await list_sessions(
                project_id=task.project_id or self.project_id or "default",
                task_id=task.id,
                limit=100,
            )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to verify checkpoint external resume token status"
            )
            return False
        matching = [
            session
            for session in sessions
            if str(getattr(session, "agent_type", "") or "").strip() == agent_type
            and external_session_matches_provider_token(session, token)
        ]
        if not matching:
            return False

        def _timestamp(value: Any) -> float:
            candidate = value
            if isinstance(candidate, str):
                try:
                    candidate = datetime.fromisoformat(candidate)
                except ValueError:
                    return 0.0
            try:
                return float(candidate.timestamp())
            except Exception:
                return 0.0

        latest = max(
            matching,
            key=lambda session: _timestamp(getattr(session, "updated_at", None)),
        )
        latest_status = str(getattr(latest, "status", "") or "").strip().lower()
        if external_session_status_allows_resume(latest_status):
            return False
        latest_timestamp = _timestamp(getattr(latest, "updated_at", None))
        checkpoint_timestamp = _timestamp(checkpoint_updated_at)
        return latest_timestamp > checkpoint_timestamp or (
            latest_timestamp >= checkpoint_timestamp
            and latest_status != str(checkpoint_status or "").strip().lower()
        )

    @staticmethod
    def _clone_external_adapter(adapter: Any) -> Any:
        config = getattr(adapter, "config", None)
        if config is None:
            return adapter
        if hasattr(config, "model_copy"):
            cloned_config = config.model_copy(deep=True)
        else:
            cloned_config = config
        return adapter.__class__(config=cloned_config)

    @staticmethod
    def _task_requests_external_resume(task: Task) -> bool:
        if is_work_item_runtime_metadata(task.metadata or {}):
            resume_scope_id = str(
                (task.metadata or {}).get("external_resume_session_scope_id", "")
                or ""
            ).strip()
            if not external_resume_allowed_for_scope(task, resume_scope_id=resume_scope_id):
                return False
            return bool(
                str(task.assigned_external_agent or "").strip()
                and (
                    str(task.metadata.get("external_resume_session_id", "") or "").strip()
                    or str(task.metadata.get("delegation_seat_id", "") or "").strip()
                    or str(task.metadata.get("delegation_role_session_id", "") or "").strip()
                )
            )
        if str(task.metadata.get("external_rework_strategy", "") or "").strip() == "resume_if_possible":
            return bool(
                task.metadata.get("gate_rework_request")
                or task.metadata.get("contract_rework_request")
                or task.metadata.get("interrupted_recovery")
                or task.retry_count > 0
            )
        session_id = str(getattr(task, "session_id", "") or "").strip()
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        return bool(
            str(task.assigned_external_agent or "").strip()
            and session_id
            and (mode == "task" or task_mode_contract == "single_full_capability_main_agent")
        )

    async def _configure_external_adapter_for_task(self, task: Task, adapter: Any) -> tuple[Any, dict[str, Any]]:
        run_adapter = self._clone_external_adapter(adapter)
        resume_metadata: dict[str, Any] = {}
        resume_scope_id = str(
            (task.metadata or {}).get("external_resume_session_scope_id", "")
            or ""
        ).strip()
        if is_top_level_company_session(task) and not external_resume_allowed_for_scope(task, resume_scope_id=resume_scope_id):
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("external_resume_session_id", None)
            task.metadata.pop("external_resume_session_scope_id", None)
            task.metadata.pop("external_resume_agent_type", None)
            cloned_config = run_adapter.config.model_copy(deep=True) if hasattr(run_adapter.config, "model_copy") else run_adapter.config
            if hasattr(cloned_config, "session_mode"):
                cloned_config.session_mode = "new"
            if hasattr(cloned_config, "session_id"):
                cloned_config.session_id = ""
            run_adapter = run_adapter.__class__(config=cloned_config)
        if not self._task_requests_external_resume(task):
            return run_adapter, resume_metadata
        supports_resume = bool(
            run_adapter.supports_session_resume()
            if hasattr(run_adapter, "supports_session_resume")
            else str(getattr(run_adapter.config, "resume_session_flag", "") or "").strip()
        )
        if not supports_resume:
            return run_adapter, resume_metadata
        metadata_agent_type = str(task.metadata.get("external_resume_agent_type", "") or "").strip()
        metadata_session_token = str(task.metadata.get("external_resume_session_id", "") or "").strip()
        metadata_token_is_unusable = bool(
            metadata_session_token
            and (
                not metadata_agent_type
                or metadata_agent_type != run_adapter.agent_type
            )
        )
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        session_token = (
            metadata_session_token
            if metadata_session_token
            and metadata_agent_type == run_adapter.agent_type
            and is_provider_session_token(
                metadata_session_token,
                agent_type=run_adapter.agent_type,
                project_id=project_id,
            )
            else ""
        )
        if session_token and await self._checkpoint_external_resume_token_was_terminalized(
            task,
            agent_type=run_adapter.agent_type,
            token=session_token,
            checkpoint_updated_at=str(
                task.metadata.get("external_resume_checkpoint_session_updated_at", "")
                or ""
            ).strip(),
            checkpoint_status=str(
                task.metadata.get("external_resume_checkpoint_session_status", "")
                or ""
            ).strip(),
        ):
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("external_resume_session_id", None)
            task.metadata.pop("external_resume_agent_type", None)
            task.metadata.pop("external_resume_session_scope_id", None)
            task.metadata["external_resume_fallback"] = "context_replay_provider_terminal"
            cloned_config = (
                run_adapter.config.model_copy(deep=True)
                if hasattr(run_adapter.config, "model_copy")
                else run_adapter.config
            )
            if hasattr(cloned_config, "session_mode"):
                cloned_config.session_mode = "new"
            if hasattr(cloned_config, "session_id"):
                cloned_config.session_id = ""
            return run_adapter.__class__(config=cloned_config), resume_metadata
        latest_session = (
            await self._load_best_external_resume_session_for_task(task)
            or await self._load_latest_external_session_for_task(task)
        )
        if latest_session and str(getattr(latest_session, "agent_type", "") or "").strip() != run_adapter.agent_type:
            latest_session = None
        if not session_token:
            session_token = provider_token_from_external_session(
                latest_session,
                agent_type=run_adapter.agent_type,
                project_id=project_id,
            )
        if not session_token and not latest_session and metadata_token_is_unusable:
            return run_adapter, resume_metadata
        if not session_token:
            return run_adapter, resume_metadata
        cloned_config = run_adapter.config.model_copy(deep=True) if hasattr(run_adapter.config, "model_copy") else run_adapter.config
        if hasattr(cloned_config, "session_mode"):
            cloned_config.session_mode = "resume"
        if hasattr(cloned_config, "session_id"):
            cloned_config.session_id = session_token
        run_adapter = run_adapter.__class__(config=cloned_config)
        resume_metadata = {
            "resume_source_session": str(getattr(latest_session, "session_id", "") or "").strip(),
            "resume_session_token": session_token,
            "resume_session_mode": "resume",
            "resume_agent_type": run_adapter.agent_type,
        }
        return run_adapter, resume_metadata

    async def _task_runtime_is_live(self, task: Task) -> bool:
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        return self._active_task_run_registry.is_active(project_id, task.id)

    def _describe_interrupted_task_reason(self, task: Task, session: Any | None) -> str:
        projection_id = str(projection_id_for_task(task) or task.title or task.id).strip()
        if not session:
            return (
                f"Execution for work item `{projection_id}` was interrupted while the task was still marked running. "
                "Use `continue` to restart this work item safely."
            )
        status = str(getattr(session, "status", "") or "").strip().lower() or "unknown"
        metadata = dict(getattr(session, "metadata", {}) or {})
        failure_reason = str(metadata.get("failure_reason", "") or "").strip()
        if failure_reason:
            return (
                f"Execution for work item `{projection_id}` was interrupted after the external session ended with "
                f"status `{status}`. Latest note: {failure_reason}"
            )
        if status == "done":
            return (
                f"Execution for work item `{projection_id}` finished in the external agent, but OpenOPC was interrupted "
                "before the work-item result was persisted locally. Use `continue` to rerun it safely."
            )
        if status == "cancelled":
            return (
                f"Execution for work item `{projection_id}` was interrupted because the external agent session was cancelled. "
                "Use `continue` to restart this work item."
            )
        return (
            f"Execution for work item `{projection_id}` was interrupted after the external session moved to `{status}`. "
            "Use `continue` to restart this work item."
        )

    async def _mark_task_interrupted(
        self,
        task: Task,
        *,
        reason: str,
        session: Any | None = None,
    ) -> bool:
        if not self.store:
            return False
        if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return False
        work_item_id = linked_work_item_id_for_task(task)
        linked_work_item = None
        if work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                linked_work_item = None
        stable_waiting_phase_values = {
            Phase.AWAITING_MANAGER_REVIEW.value,
            Phase.AWAITING_HUMAN.value,
        }
        linked_phase = getattr(linked_work_item, "phase", None)
        linked_phase_value = str(getattr(linked_phase, "value", linked_phase or "") or "").strip()
        if task.status in _REVIEW_WAITING_STATUSES or linked_phase_value in stable_waiting_phase_values:
            logger.info(
                "Preserving stable waiting task {} during interrupted-task recovery (status={}, work_item_phase={})",
                task.id,
                getattr(task.status, "value", str(task.status)),
                linked_phase_value,
            )
            return False
        previous_status = task.status
        task.status = TaskStatus.FAILED
        task.execution_lock = False
        task.execution_locked_at = None
        existing_result = dict(task.result or {})
        artifacts = dict(existing_result.get("artifacts", {}) or {})
        session_status = str(getattr(session, "status", "") or "").strip()
        session_updated_at = getattr(session, "updated_at", None)
        artifacts.update(
            {
                "interrupted": True,
                "interrupted_detected_at": datetime.now().isoformat(),
                "interrupted_previous_status": getattr(previous_status, "value", str(previous_status)),
                "latest_external_session_status": session_status,
                "latest_external_session_updated_at": session_updated_at.isoformat() if session_updated_at else "",
            }
        )
        existing_result["content"] = str(existing_result.get("content") or reason).strip()
        existing_result["artifacts"] = artifacts
        task.result = existing_result
        task.metadata = dict(task.metadata)
        progress_log = list(task.metadata.get("progress_log", []))
        if not progress_log or progress_log[-1] != reason:
            progress_log.append(reason)
        task.metadata["progress_log"] = progress_log[-20:]
        task.metadata["interrupted_recovery"] = {
            "detected_at": datetime.now().isoformat(),
            "previous_status": getattr(previous_status, "value", str(previous_status)),
            "latest_external_session_status": session_status,
            "latest_external_session_updated_at": session_updated_at.isoformat() if session_updated_at else "",
            "reason": reason,
        }
        await self.store.save_task(task)
        if work_item_id and hasattr(self.store, "update_delegation_work_item"):
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.PAUSED,
                summary=reason,
                metadata_updates={
                    "interrupted_recovery": dict(task.metadata.get("interrupted_recovery", {}) or {}),
                    "task_status": task.status.value,
                },
            )
        role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id and hasattr(self.store, "update_delegation_role_session"):
            await self.store.update_delegation_role_session(
                role_session_id,
                focused_work_item_id=work_item_id,
                status="blocked",
                metadata_updates={
                    "interrupted_task_id": task.id,
                    "interrupted_detected_at": datetime.now().isoformat(),
                },
            )
        return True

    async def _fail_task_via_phase(
        self,
        task: Task,
        *,
        reason: str,
    ) -> None:
        """Mark a task as FAILED through the phase channel so all projection
        layers (task.status, role_session, in-memory member_session, UI
        column) update atomically. Falls back to direct task.status write
        for tasks that don't have a linked delegation work item (legacy
        plain tasks).

        Callers should set task.metadata / task.result BEFORE calling this;
        this function is the terminal ``save`` step.
        """
        if not self.store:
            return
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            try:
                from opc.layer2_organization.work_item_transition import transition_work_item
                await transition_work_item(
                    self.store, work_item_id,
                    target_phase=Phase.FAILED,
                    reason=reason,
                    release_claim=True,
                )
                # The phase hook chain updated task.status; merge any metadata
                # the caller prepared on the in-memory task object back to the
                # persisted row.
                fresh = await self.store.get_task(task.id)
                if fresh is not None:
                    fresh.metadata = dict(task.metadata or {})
                    fresh.result = dict(task.result or {})
                    await self.store.save_task(fresh)
                return
            except Exception:
                logger.opt(exception=True).warning(
                    f"_fail_task_via_phase: transition failed for {task.id}, falling back to direct status write"
                )
        # Legacy fallback: no work item → direct status write (no cascade).
        task.status = TaskStatus.FAILED
        await self.store.save_task(task)

    @staticmethod
    def _is_company_feedback_waiting_task(task: Task) -> bool:
        if task.status != TaskStatus.AWAITING_HUMAN:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        if OPCEngine._metadata_flag_true(metadata.get("self_evolution_review_completed", False)):
            return False
        turn_kind = str(
            metadata.get("work_kind")
            or metadata.get("delegation_turn_kind")
            or metadata.get("work_item_turn_type")
            or ""
        ).strip().lower()
        if turn_kind in {"deliver", "delivery"}:
            return True
        return bool(str(metadata.get("feedback_scope", "") or "").strip())

    async def _preserve_stable_waiting_task_after_restart(
        self,
        task: Task,
        *,
        reason: str,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> bool:
        """Keep durable review/human waiting states intact during startup.

        Review and human-feedback waits are stable states.  A missing checkpoint
        after process restart means the UI card may need recovery, not that the
        work item should be failed/paused like a dead running process.
        """
        if not self.store:
            return False
        task.metadata = dict(task.metadata or {})
        status_value = getattr(task.status, "value", str(task.status))
        existing_preserved = dict(
            task.metadata.get("startup_reconcile_preserved_waiting_state", {})
            or {}
        )
        marker_changed = (
            str(existing_preserved.get("status", "") or "") != status_value
            or str(existing_preserved.get("reason", "") or "") != reason
        )
        if marker_changed:
            task.metadata["startup_reconcile_preserved_waiting_state"] = {
                "detected_at": datetime.now().isoformat(),
                "status": status_value,
                "reason": reason,
            }
            await self.store.save_task(task)
        restored_checkpoint = False
        if self._is_company_feedback_waiting_task(task):
            try:
                await self._save_company_feedback_followup_checkpoint(task, tasks, plan)
                restored_checkpoint = True
            except Exception:
                logger.opt(exception=True).debug(
                    "Best-effort restore of human feedback checkpoint failed for task {}",
                    task.id,
                )
        if restored_checkpoint:
            task.metadata = dict(task.metadata or {})
            preserved = dict(task.metadata.get("startup_reconcile_preserved_waiting_state", {}) or {})
            if not str(preserved.get("restored_checkpoint_type", "") or "").strip():
                preserved["restored_checkpoint_type"] = "company_delivery_feedback"
                preserved["restored_checkpoint_at"] = datetime.now().isoformat()
                task.metadata["startup_reconcile_preserved_waiting_state"] = preserved
                await self.store.save_task(task)
                marker_changed = True
        return marker_changed or restored_checkpoint

    def _company_runtime_plan_for_tasks(
        self,
        tasks: list[Task],
    ) -> CompanyWorkItemRuntimePlan:
        for task in sorted(tasks, key=lambda item: (item.created_at, item.id), reverse=True):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                return deserialize_company_work_item_runtime_plan(candidate)
        sample = tasks[0]
        return CompanyWorkItemRuntimePlan(
            profile=str(
                (sample.metadata or {}).get("company_profile", "")
                or getattr(self.config.org, "company_profile", "corporate")
            ).strip()
            or "corporate",
            metadata={
                "execution_model": str(
                    (sample.metadata or {}).get("execution_model", "") or "multi_team_org"
                ).strip()
                or "multi_team_org",
                "work_item_driven": True,
            },
        )

    async def _normalize_legacy_company_interruption(
        self,
        task: Task,
    ) -> dict[str, Any] | None:
        """Convert the old FAILED+marker patch into checkpoint-ready state.

        A work item whose authoritative phase is genuinely terminal is never
        revived.  The old recovery marker, interrupted result artifact, or a
        PAUSED work-item phase is accepted only as evidence that FAILED was a
        crash projection rather than a business outcome.
        """
        if not self.store or task.status != TaskStatus.FAILED:
            return None
        metadata = dict(task.metadata or {})
        marker = metadata.get("interrupted_recovery")
        marker = dict(marker) if isinstance(marker, dict) else {}
        result = dict(task.result or {})
        artifacts = dict(result.get("artifacts", {}) or {})
        work_item_id = linked_work_item_id_for_task(task)
        work_item = None
        if work_item_id:
            getter = getattr(self.store, "get_delegation_work_item", None)
            if callable(getter):
                try:
                    work_item = await getter(work_item_id)
                except Exception:
                    work_item = None
        phase = getattr(work_item, "phase", None)
        if phase in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
            return None
        previous_status_value = str(
            marker.get("previous_status")
            or artifacts.get("interrupted_previous_status")
            or ""
        ).strip()
        interrupted_artifact = bool(artifacts.get("interrupted", False))
        paused_work_item = phase == Phase.PAUSED
        if not previous_status_value and not interrupted_artifact and not paused_work_item:
            return None

        previous_status = None
        if previous_status_value:
            try:
                candidate = TaskStatus(previous_status_value)
            except ValueError:
                candidate = None
            if candidate not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                previous_status = candidate
        if paused_work_item:
            target_status = task_status_for_phase(Phase.PAUSED)
        else:
            target_status = previous_status or TaskStatus.PENDING

        provenance = {
            "task_id": task.id,
            "work_item_id": work_item_id or "",
            "legacy_failed_status": TaskStatus.FAILED.value,
            "restored_status": target_status.value,
            "work_item_phase": phase.value if isinstance(phase, Phase) else str(phase or ""),
            "interrupted_recovery": marker,
            "interrupted_artifacts": {
                key: artifacts.get(key)
                for key in (
                    "interrupted",
                    "interrupted_detected_at",
                    "interrupted_previous_status",
                    "latest_external_session_status",
                    "latest_external_session_updated_at",
                )
                if key in artifacts
            },
        }
        task.status = target_status
        task.execution_lock = False
        task.execution_locked_at = None
        return provenance

    async def _clear_migrated_company_interruption_markers(
        self,
        tasks: list[Task],
        migrated_task_ids: set[str],
    ) -> None:
        if not self.store or not migrated_task_ids:
            return
        artifact_keys = {
            "interrupted",
            "interrupted_detected_at",
            "interrupted_previous_status",
            "latest_external_session_status",
            "latest_external_session_updated_at",
        }
        for task in tasks:
            if task.id not in migrated_task_ids:
                continue
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("interrupted_recovery", None)
            if task.result:
                task.result = dict(task.result)
                artifacts = dict(task.result.get("artifacts", {}) or {})
                for key in artifact_keys:
                    artifacts.pop(key, None)
                task.result["artifacts"] = artifacts
            await self.store.save_task(task)

    async def _reconcile_company_runtime_state(
        self,
        parent_session_id: str,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> int:
        if not self.store:
            return 0
        project_id = self.project_id or "default"
        pending_checkpoints = await self.store.get_pending_checkpoints(project_id=project_id)
        legacy_provenance: list[dict[str, Any]] = []
        for task in tasks:
            provenance = await self._normalize_legacy_company_interruption(task)
            if provenance is not None:
                legacy_provenance.append(provenance)
        migrated_task_ids = {
            str(item.get("task_id", "") or "").strip()
            for item in legacy_provenance
            if str(item.get("task_id", "") or "").strip()
        }
        normalize_active = getattr(
            self.store,
            "normalize_active_execution_checkpoints",
            None,
        )
        if callable(normalize_active):
            pending_suspend_checkpoint = await normalize_active(
                project_id=project_id,
                session_id=parent_session_id,
                checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
            )
        else:
            pending_suspend_checkpoint = await self.get_active_company_runtime_suspend_checkpoint(
                parent_session_id
            )
        if pending_suspend_checkpoint is not None:
            payload = dict(pending_suspend_checkpoint.payload or {})
            checkpoint_status = str(getattr(pending_suspend_checkpoint, "status", "") or "").strip()
            if checkpoint_status == "resuming" or legacy_provenance:
                payload_updates: dict[str, Any] = {}
                if checkpoint_status == "resuming":
                    payload_updates.update({
                        "resume_state": "interrupted",
                        "resume_interrupted_at": datetime.now().isoformat(),
                    })
                if legacy_provenance:
                    payload_updates["legacy_interrupted_recovery"] = legacy_provenance
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    pending_suspend_checkpoint,
                    status="pending",
                    payload_updates=payload_updates,
                    expected_statuses={checkpoint_status},
                )
                if not transitioned:
                    return 0
                payload = dict(pending_suspend_checkpoint.payload or {})
            await self._clear_migrated_company_interruption_markers(tasks, migrated_task_ids)
            affected = await self._suspend_company_runtime_tasks(
                tasks,
                reason=str(payload.get("reason", "") or "startup_recovery"),
                checkpoint_type=str(pending_suspend_checkpoint.checkpoint_type or "company_runtime_interrupted"),
                stop_intent_id=str(payload.get("stop_intent_id", "") or ""),
            )
            return len(affected)
        visible_session_ids = {
            parent_session_id,
            *{
                str(getattr(task, "session_id", "") or "").strip()
                for task in tasks
                if str(getattr(task, "session_id", "") or "").strip()
            },
        }
        group_task_ids = {str(task.id or "").strip() for task in tasks}
        checkpoint_task_ids = {
            str(
                checkpoint.task_id
                or checkpoint.payload.get("waiting_task_id")
                or checkpoint.payload.get("task_id")
                or ""
            ).strip()
            for checkpoint in pending_checkpoints
            if (
                str(checkpoint.session_id or "").strip() in visible_session_ids
                or str(
                    checkpoint.task_id
                    or checkpoint.payload.get("waiting_task_id")
                    or checkpoint.payload.get("task_id")
                    or ""
                ).strip() in group_task_ids
            )
        }
        updated = 0
        interrupted_tasks: list[Task] = []
        get_work_item = getattr(
            self.store,
            "get_delegation_work_item",
            None,
        )
        for task in tasks:
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            work_item_id = linked_work_item_id_for_task(task)
            work_item = (
                await get_work_item(work_item_id)
                if work_item_id and callable(get_work_item)
                else None
            )
            work_item_phase = getattr(work_item, "phase", None)
            task_held = (
                str(task_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
                or str(task_metadata.get("company_runtime_stop_state", "") or "").strip() in {"suspending", "suspended"}
            )
            terminal_card = (
                work_item_phase in DONE_PHASES
                if work_item is not None
                else task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
            )
            if terminal_card:
                # Terminal cards are done — a leftover suspend hold on them is
                # residue (stale-snapshot save, suspend racing a FAILED write),
                # not an interruption. Classifying it as interrupted here
                # rebuilt a pending company_runtime_interrupted checkpoint on
                # EVERY boot for a run that had already converged. Scrub the
                # residue instead so startup is idempotent.
                if task_held:
                    task.metadata = dict(task_metadata)
                    for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                        task.metadata.pop(key, None)
                    try:
                        await self.store.save_task(task)
                        updated += 1
                    except Exception:
                        logger.opt(exception=True).debug(
                            "startup reconcile: terminal-card hold scrub failed for {}",
                            task.id,
                        )
                continue
            if task_held:
                interrupted_tasks.append(task)
                continue
            # A linked WorkItem is the company workflow state.  Task.status is
            # only its UI/execution projection and may lag on either side of a
            # crash.  Fall back to Task status only for legacy envelopes that
            # have no durable WorkItem.
            stable_review_wait = (
                work_item_phase in IN_REVIEW_PHASES
                if work_item is not None
                else task.status in _REVIEW_WAITING_STATUSES
            )
            if stable_review_wait:
                if task.id not in checkpoint_task_ids:
                    waiting_label = (
                        work_item_phase.value
                        if isinstance(work_item_phase, Phase)
                        else task.status.value
                    )
                    reason = (
                        f"Work item `{projection_id_for_task(task) or task.title}` was left in "
                        f"`{waiting_label}` but no pending checkpoint could be found after restart. "
                        "Preserving stable waiting state."
                    )
                    if await self._preserve_stable_waiting_task_after_restart(
                        task,
                        reason=reason,
                        plan=plan,
                        tasks=tasks,
                    ):
                        updated += 1
                continue
            if task.id in checkpoint_task_ids and (
                task.status in _WAITING_TASK_STATUSES
                or work_item_phase == Phase.WAITING_FOR_PEER
            ):
                # A durable peer/specialized wait already owns this Task.
                # Startup must not layer a company-runtime interruption over
                # that independent waiting checkpoint.
                continue
            if work_item is not None and work_item_phase not in DONE_PHASES:
                # A controller coroutine is required to dispatch READY work,
                # revisit dependency waits, and finalize RUNNING work.  After
                # process start the registry is empty, so every such WorkItem
                # is interrupted even when its Task projection is still
                # PENDING/BLOCKED or was prematurely terminalized.
                interrupted_tasks.append(task)
                continue
            if task.status == TaskStatus.RUNNING and not await self._task_runtime_is_live(task):
                interrupted_tasks.append(task)
                continue
            if task.status in _WAITING_TASK_STATUSES and task.id not in checkpoint_task_ids:
                interrupted_tasks.append(task)
        if legacy_provenance:
            interrupted_tasks.extend(
                task for task in tasks if task.id in migrated_task_ids
            )
        if interrupted_tasks:
            interrupted_task_ids = list(dict.fromkeys(task.id for task in interrupted_tasks))
            task_by_id = {task.id: task for task in tasks}
            interrupted_task_objects = [task_by_id[task_id] for task_id in interrupted_task_ids]
            origin_task_id = str(getattr(interrupted_task_objects[0], "id", "") or "").strip()
            checkpoint, _created = await self._save_company_runtime_suspend_checkpoint(
                checkpoint_type="company_runtime_interrupted",
                reason="startup_recovery",
                parent_session_id=parent_session_id,
                origin_task_id=origin_task_id or None,
                plan=plan,
                tasks=tasks,
                payload_updates=(
                    {"legacy_interrupted_recovery": legacy_provenance}
                    if legacy_provenance
                    else None
                ),
            )
            if (
                legacy_provenance
                and "legacy_interrupted_recovery"
                not in dict(checkpoint.payload or {})
            ):
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="pending",
                    payload_updates={
                        "legacy_interrupted_recovery": legacy_provenance,
                        "resume_state": "interrupted",
                        "resume_interrupted_at": datetime.now().isoformat(),
                    },
                    expected_statuses={"pending", "resuming"},
                )
                if not transitioned:
                    return updated
            await self._clear_migrated_company_interruption_markers(tasks, migrated_task_ids)
            checkpoint_payload = dict(checkpoint.payload or {})
            affected = await self._suspend_company_runtime_tasks(
                tasks,
                reason=str(checkpoint_payload.get("reason", "") or "startup_recovery"),
                checkpoint_type=str(
                    checkpoint.checkpoint_type or "company_runtime_interrupted"
                ),
                stop_intent_id=str(
                    checkpoint_payload.get("stop_intent_id", "") or ""
                ),
            )
            updated += len(affected) or len(interrupted_task_objects)
        return updated

    async def _reconcile_interrupted_project_tasks(self) -> int:
        if not self.store:
            return 0
        project_id = self.project_id or "default"
        tasks = await self.store.get_tasks(project_id=project_id)
        if not tasks:
            return 0

        checkpoint_getter = getattr(self.store, "get_execution_checkpoints", None)
        active_company_checkpoints = (
            await checkpoint_getter(
                project_id=project_id,
                checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
                statuses=["pending", "resuming"],
            )
            if callable(checkpoint_getter)
            else []
        )
        identity_index = build_company_runtime_identity_index(
            tasks,
            active_company_checkpoints,
        )
        task_by_id = {task.id: task for task in tasks}
        runtime_groups: dict[str, list[Task]] = {}
        runtime_task_ids: set[str] = set()
        ui_anchor_task_ids: set[str] = set()
        for identity in identity_index.identities:
            if identity.ui_anchor_task_id:
                ui_anchor_task_ids.add(identity.ui_anchor_task_id)
            group = [
                task_by_id[task_id]
                for task_id in identity.runtime_task_ids
                if task_id != identity.ui_anchor_task_id
                and task_id in task_by_id
                and is_company_runtime_task(task_by_id[task_id])
            ]
            if group:
                runtime_groups[identity.runtime_session_id] = group
                runtime_task_ids.update(task.id for task in group)

        updated = 0
        for anchor_task_id in ui_anchor_task_ids:
            anchor = task_by_id.get(anchor_task_id)
            if anchor is not None and anchor.status == TaskStatus.RUNNING:
                if await self._clear_stale_company_session_anchor(anchor):
                    updated += 1

        for task in tasks:
            if task.id in runtime_task_ids or task.id in ui_anchor_task_ids:
                continue
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            if (
                str(task_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
                or str(task_metadata.get("company_runtime_stop_state", "") or "").strip() in {"suspending", "suspended"}
            ):
                continue
            if is_company_runtime_task(task):
                continue
            if self._is_company_primary_session_anchor_task(task):
                if task.status == TaskStatus.RUNNING:
                    if await self._clear_stale_company_session_anchor(task):
                        updated += 1
                continue
            if task.status == TaskStatus.RUNNING and not await self._task_runtime_is_live(task):
                session = await self._load_latest_external_session_for_task(task)
                reason = self._describe_interrupted_task_reason(task, session)
                if await self._mark_task_interrupted(task, reason=reason, session=session):
                    updated += 1

        for parent_session_id, group in runtime_groups.items():
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                updated += await self._reconcile_company_runtime_state(
                    parent_session_id,
                    self._company_runtime_plan_for_tasks(group),
                    group,
                )
        return updated

    async def prepare_active_company_runtimes_for_shutdown(
        self,
        *,
        _controller_shutdown: bool = False,
    ) -> list[dict[str, Any]]:
        """Persist checkpoints for company attempts owned by this controller.

        Callers invoke this before cancelling in-flight session coroutines.  It
        is safe to call repeatedly: an existing suspended/interrupted
        checkpoint is reused and never replaced.
        """
        self._shutting_down = True
        if not self._owns_active_task_run_registry and not _controller_shutdown:
            # Project delegates and CustomRuntimeRunner engines borrow the
            # controller registry.  Their local teardown must not close
            # controller-wide admission or checkpoint unrelated attempts.
            return []
        if self._owns_active_task_run_registry:
            self._active_task_run_registry.close_admission()
        project_id = str(self.project_id or "default").strip() or "default"
        active_task_ids = self._active_task_run_registry.active_task_ids(project_id)
        delegate_prepared: list[dict[str, Any]] = []
        for delegate in list(self._project_engine_delegates.values()):
            delegate_prepared.extend(
                await delegate.prepare_active_company_runtimes_for_shutdown(
                    _controller_shutdown=True,
                )
            )
        if not self.store or not bool(getattr(self.store, "is_ready", True)):
            return delegate_prepared
        if not active_task_ids:
            return delegate_prepared
        identity_index = await load_company_runtime_identity_index(
            self.store,
            project_id,
        )
        active_by_scope: dict[str, list[Task]] = {}
        for task_id in active_task_ids:
            identity = identity_index.resolve(task_id=task_id)
            task = identity_index.task(task_id)
            if (
                identity is None
                or task is None
                or task_id == identity.ui_anchor_task_id
                or not is_company_runtime_task(task)
            ):
                continue
            active_by_scope.setdefault(identity.runtime_session_id, []).append(task)

        prepared: list[dict[str, Any]] = delegate_prepared
        for parent_session_id, active_scope_tasks in active_by_scope.items():
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                snapshot = await self._load_company_runtime_snapshot(parent_session_id)
                if snapshot is None:
                    tasks = active_scope_tasks
                    plan = self._company_runtime_plan_for_tasks(tasks)
                else:
                    plan, tasks = snapshot
                origin_task_id = str(active_scope_tasks[0].id or "").strip() or None
                checkpoint, created, affected_task_ids = (
                    await self._checkpoint_and_suspend_company_runtime_scope(
                        checkpoint_type="company_runtime_interrupted",
                        reason="service_shutdown",
                        parent_session_id=parent_session_id,
                        origin_task_id=origin_task_id,
                        plan=plan,
                        tasks=tasks,
                    )
                )
                idempotent = not created
                prepared.append(
                    {
                        "session_id": parent_session_id,
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "checkpoint_type": checkpoint.checkpoint_type,
                        "task_ids": affected_task_ids,
                        "idempotent": idempotent,
                    }
                )
        return prepared

    @staticmethod
    def _is_company_primary_session_anchor_task(task: Task) -> bool:
        """Return True for the user-facing chat task that anchors a company run.

        The anchor is not a business work item. It may be set to RUNNING while
        a user message is being routed, but actual company progress/failure is
        represented by child work-item tasks and DelegationWorkItem phases.
        """
        metadata = dict(getattr(task, "metadata", {}) or {})
        if str(metadata.get("work_item_projection_id", "") or "").strip():
            return False
        if linked_work_item_id_for_task(task):
            return False
        if str(getattr(task, "parent_session_id", "") or "").strip():
            return False
        if str(getattr(task, "parent_id", "") or "").strip():
            return False
        exec_mode = str(metadata.get("exec_mode", "") or metadata.get("mode", "") or "").strip().lower()
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        return exec_mode in {"company", "org", "custom"} or execution_mode == ExecutionMode.COMPANY_MODE.value

    async def _clear_stale_company_session_anchor(self, task: Task) -> bool:
        if not self.store:
            return False
        fresh = await self.store.get_task(task.id)
        target = fresh or task
        if target.status != TaskStatus.RUNNING:
            return False
        target.status = TaskStatus.IDLE
        target.execution_lock = False
        target.execution_locked_at = None
        target.metadata = dict(target.metadata or {})
        progress = list(target.metadata.get("progress_log", []) or [])
        message = "Recovered stale company session routing state after startup; work-item runtime state was left intact."
        if not progress or progress[-1] != message:
            progress.append(message)
        target.metadata["progress_log"] = progress[-20:]
        await self.store.save_task(target)
        logger.info(
            "Recovered stale company session anchor {} for project {} without marking it failed",
            target.id,
            target.project_id or self.project_id or "default",
        )
        return True

    @staticmethod
    def _format_company_runtime_snapshot(
        tasks: list[Task],
        *,
        heading: str = "## Latest Runtime Snapshot",
        annotations: dict[str, str] | None = None,
    ) -> str:
        lines = [heading]
        notes = dict(annotations or {})
        for task in tasks:
            projection_id = str(projection_id_for_task(task) or task.title or task.id).strip()
            title = str(task.title or projection_id).strip() or projection_id
            line = f"- `{projection_id}` ({title}): {task.status.value}"
            note = str(notes.get(projection_id, "") or "").strip()
            if note:
                line += f" [{note}]"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _company_followup_turn_priority(task: Task) -> int:
        turn_type = turn_type_for_task(task, fallback="")
        return {
            "intake": 0,
            "dispatch": 1,
            "plan": 2,
            "deliver": 3,
            "aggregate": 4,
            "review": 5,
            "execute": 6,
        }.get(turn_type, 7)

    @staticmethod
    def _company_followup_status_priority(task: Task) -> int:
        status = task.status
        turn_type = turn_type_for_task(task, fallback="")
        if status == TaskStatus.PENDING:
            return 0
        if status == TaskStatus.DONE and turn_type in {"intake", "dispatch", "plan"}:
            return 1
        if status in _WAITING_TASK_STATUSES:
            return 2
        if status == TaskStatus.BLOCKED:
            return 3
        if status == TaskStatus.DONE:
            return 4
        if status == TaskStatus.FAILED:
            return 5
        if status == TaskStatus.RUNNING:
            return 6
        if status == TaskStatus.CANCELLED:
            return 7
        return 8

    @staticmethod
    def _metadata_flag_true(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    _FINAL_DELIVERY_CURRENT_OUTPUT_KEYS = {
        "delivery_package",
        "final_delivery_package",
        "feedback_followup_message",
        "ceo_pre_delivery_assessment",
        "pre_delivery_assessment_status",
        "pre_delivery_assessment_failure_kind",
        "pre_delivery_rework_cap_reached",
        "pre_delivery_rework_cap",
        "feedback_close_user_message",
    }

    @classmethod
    def _clear_current_final_delivery_outputs(cls, task: Task) -> None:
        """Clear only the current delivery cache before a new owner revision."""
        task.metadata = dict(getattr(task, "metadata", {}) or {})
        task.context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        for key in cls._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS:
            task.metadata.pop(key, None)
            task.context_snapshot.pop(key, None)
        owned_outputs = dict(task.context_snapshot.get("work_item_owned_outputs", {}) or {})
        for key in cls._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS:
            owned_outputs.pop(key, None)
        if owned_outputs:
            task.context_snapshot["work_item_owned_outputs"] = owned_outputs
        else:
            task.context_snapshot.pop("work_item_owned_outputs", None)

    @classmethod
    def _is_open_final_delivery_review_task(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        if cls._metadata_flag_true(metadata.get("feedback_closed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("human_review_closed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("self_evolution_review_completed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("feedback_superseded", False)):
            return False
        if task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}:
            return False
        if str(metadata.get("execution_mode", "") or "").strip() != ExecutionMode.COMPANY_MODE.value:
            return False
        if str(metadata.get("feedback_scope", "") or "").strip().lower() != "final":
            return False
        if not cls._metadata_flag_true(metadata.get("authoritative_output", False)):
            return False
        if not cls._metadata_flag_true(metadata.get("user_visible", False)):
            return False
        if not cls._metadata_flag_true(metadata.get("requires_user_feedback", False)):
            return False
        return turn_type_for_task(task, fallback="") == "deliver"

    @staticmethod
    def _final_delivery_followup_status_priority(task: Task) -> int:
        return {
            TaskStatus.AWAITING_HUMAN: 0,
            TaskStatus.AWAITING_REVIEW: 1,
            TaskStatus.AWAITING_MANAGER_REVIEW: 2,
            TaskStatus.PENDING: 3,
            TaskStatus.BLOCKED: 4,
            TaskStatus.DONE: 5,
            TaskStatus.RUNNING: 6,
            TaskStatus.CANCELLED: 7,
            TaskStatus.FAILED: 8,
        }.get(task.status, 9)

    def _company_followup_target_task(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> Task | None:
        # FAILED/CANCELLED cards are terminal in the phase machine — there is
        # no legal reopen edge, so routing a follow-up onto one can only crash
        # the resume turn (`InvalidPhaseTransition: failed -> ready`) after
        # having already clobbered the Task projection to PENDING. DONE stays
        # eligible: the approved→rework reopen has a dedicated legal store op
        # (`reopen_approved_delegation_work_item_for_rework`).
        tasks = [
            task
            for task in tasks
            if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}
        ]
        final_delivery_candidates = [
            task for task in tasks
            if self._is_open_final_delivery_review_task(task)
        ]
        if final_delivery_candidates:
            return sorted(
                final_delivery_candidates,
                key=lambda task: (
                    self._final_delivery_followup_status_priority(task),
                    -float(task.created_at.timestamp()),
                    str(task.id),
                ),
            )[0]

        final_decider_role_id = str(
            getattr(plan, "final_decider_role_id", "")
            or plan.metadata.get("final_decider_role_id", "")
            or ""
        ).strip()
        if not final_decider_role_id and self.org_engine:
            getter = getattr(self.org_engine, "get_final_decider_role_id", None)
            if callable(getter):
                try:
                    final_decider_role_id = str(getter(strict=False) or "").strip()
                except TypeError:
                    final_decider_role_id = str(getter() or "").strip()
        if not final_decider_role_id:
            top_level_role_ids = [
                str(item).strip()
                for item in list(getattr(plan, "top_level_role_ids", []) or plan.metadata.get("top_level_role_ids", []) or [])
                if str(item).strip()
            ]
            if len(top_level_role_ids) == 1:
                final_decider_role_id = top_level_role_ids[0]
        if not final_decider_role_id:
            fallback_candidates = [
                task
                for task in tasks
                if bool(dict(getattr(task, "metadata", {}) or {}).get("authoritative_output", False))
                or str(dict(getattr(task, "metadata", {}) or {}).get("feedback_scope", "") or "").strip() == "final"
                or turn_type_for_task(task, fallback="") == "deliver"
            ]
            if not fallback_candidates:
                return None
            return sorted(
                fallback_candidates,
                key=lambda task: (
                    self._company_followup_status_priority(task),
                    self._company_followup_turn_priority(task),
                    -float(task.created_at.timestamp()),
                    str(task.id),
                ),
            )[0]
        candidates = [
            task
            for task in tasks
            if str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip() == final_decider_role_id
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda task: (
                self._company_followup_status_priority(task),
                self._company_followup_turn_priority(task),
                -float(task.created_at.timestamp()),
                str(task.id),
            ),
        )[0]

    async def _prepare_company_followup_target(
        self,
        task: Task,
        user_reply: str,
        *,
        resume_source: str = "primary_session_followup",
        context_updates: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        assert self.store
        # Terminal guard (belt to the selection-level filter): FAILED and
        # CANCELLED have no legal reopen edge. Mutating the Task projection
        # first and letting the store reject the phase write afterwards is
        # exactly the ordering that left task=PENDING vs work_item=FAILED
        # divergence — refuse up front instead.
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            logger.warning(
                "_prepare_company_followup_target: refusing terminal target task {} (status={})",
                task.id,
                task.status.value,
            )
            return
        reply = str(user_reply or "").strip()
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["user_supplied_input"] = reply
        if not isinstance(task.context_snapshot.get("runtime_resume"), dict):
            task.context_snapshot.pop("runtime_resume", None)
        task.context_snapshot["skip_session_history"] = True
        task.context_snapshot["current_turn_mode"] = "dispatch_required"
        if context_updates:
            task.context_snapshot.update(dict(context_updates))
        task.status = TaskStatus.PENDING
        task.result = None
        task.execution_lock = False
        task.execution_locked_at = None
        task.metadata = dict(task.metadata or {})
        final_delivery_followup = self._is_open_final_delivery_review_task(task)
        next_delivery_revision: int | None = None
        if final_delivery_followup:
            current_revisions: list[int] = []
            for source in (task.metadata, task.context_snapshot):
                for key in ("delivery_revision", "owner_directive_revision"):
                    try:
                        current_revisions.append(int(dict(source or {}).get(key, 0) or 0))
                    except (TypeError, ValueError):
                        pass
            next_delivery_revision = max(current_revisions or [0]) + 1
            self._clear_current_final_delivery_outputs(task)
            task.context_snapshot["delivery_revision"] = next_delivery_revision
            task.context_snapshot["owner_directive_revision"] = next_delivery_revision
        task.metadata["followup_routed_to_final_decider"] = True
        task.metadata["current_turn_mode"] = "dispatch_required"
        if final_delivery_followup:
            task.metadata.update({
                "work_kind": "delivery",
                "delegation_turn_kind": "delivery",
                "work_item_turn_type": "deliver",
                "review_owner_kind": "human",
                "feedback_scope": "final",
                "requires_user_feedback": True,
                "authoritative_output": True,
                "user_visible": True,
            })
            if next_delivery_revision is not None:
                task.metadata["delivery_revision"] = next_delivery_revision
                task.metadata["owner_directive_revision"] = next_delivery_revision
        else:
            task.metadata["delegation_turn_kind"] = "dispatch"
        if reply:
            task.metadata["latest_user_directive"] = reply
            task.metadata["manager_mutation_user_input"] = reply
            task.metadata["user_supplied_input"] = reply
            if final_delivery_followup:
                task.context_snapshot["latest_user_directive"] = reply
        if metadata_updates:
            task.metadata.update(dict(metadata_updates))
        task.metadata.pop("delegation_pending_work_item_ids", None)
        task.metadata.pop("delegated_children_pending", None)
        task.metadata.pop("delegation_wait_for_work_item_ids", None)
        task.metadata = reset_manager_dispatch_turn_metadata(task.metadata)
        progress = list(task.metadata.get("progress_log", []) or [])
        progress.append(f"Company follow-up routed to final decider ({resume_source}): {reply}")
        task.metadata["progress_log"] = progress[-20:]
        await self.store.save_task(task)

        work_item_id = linked_work_item_id_for_task(task)
        work_item = None
        if not work_item_id:
            get_work_item_for_task = getattr(self.store, "get_work_item_for_runtime_task", None)
            if callable(get_work_item_for_task):
                try:
                    work_item = await get_work_item_for_task(task.id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
        if work_item_id and hasattr(self.store, "update_delegation_work_item"):
            target_phase = Phase.READY
            get_work_item = getattr(self.store, "get_delegation_work_item", None)
            reopen_approved = getattr(self.store, "reopen_approved_delegation_work_item_for_rework", None)
            if work_item is None and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                    current_phase = getattr(work_item, "phase", None)
                    if not isinstance(current_phase, Phase):
                        current_phase = Phase(str(current_phase or ""))
                    if current_phase in {Phase.AWAITING_HUMAN, Phase.AWAITING_MANAGER_REVIEW}:
                        target_phase = Phase.READY_FOR_REWORK
                except Exception:
                    target_phase = Phase.READY
            resume_metadata = {
                "resume_requested_at": datetime.now().isoformat(),
                "resume_user_reply": reply,
                "resume_source": resume_source,
                "current_turn_mode": "dispatch_required",
                "delegation_turn_kind": "dispatch",
                "followup_routed_to_final_decider": True,
                "followup_attention": "user_supplied_input",
                "latest_user_directive": reply,
                "manager_mutation_user_input": reply,
                "user_supplied_input": reply,
                "dependency_gate_bypass_reason": "final_decider_followup",
                "delegated_children_pending": False,
                "delegation_wait_for_work_item_ids": [],
                "waiting_on_work_item_ids": [],
            }
            if final_delivery_followup:
                resume_metadata.update({
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "work_item_turn_type": "deliver",
                    "review_owner_kind": "human",
                    "feedback_scope": "final",
                    "requires_user_feedback": True,
                    "authoritative_output": True,
                    "user_visible": True,
                })
                if next_delivery_revision is not None:
                    resume_metadata["delivery_revision"] = next_delivery_revision
                    resume_metadata["owner_directive_revision"] = next_delivery_revision
            if metadata_updates:
                resume_metadata.update(dict(metadata_updates))
            metadata_unset = (
                sorted(self._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS)
                if final_delivery_followup
                else None
            )
            current_phase = getattr(work_item, "phase", None) if work_item is not None else None
            if current_phase == Phase.APPROVED and callable(reopen_approved):
                await reopen_approved(
                    work_item_id,
                    target_phase=Phase.READY_FOR_REWORK,
                    summary=None,
                    deliverable_summary="",
                    blocked_reason="",
                    metadata_updates=resume_metadata,
                    metadata_unset=metadata_unset,
                    release_claim=True,
                )
            else:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    phase=target_phase,
                    summary=None,
                    metadata_updates=resume_metadata,
                    metadata_unset=metadata_unset,
                    claimed_by_role_runtime_session_id="",
                    claimed_by_seat_id="",
                )
        role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id and hasattr(self.store, "update_delegation_role_session"):
            await self.store.update_delegation_role_session(
                role_session_id,
                focused_work_item_id="",
                current_work_item={},
                status="idle",
                metadata_updates={
                    "resume_requested_at": datetime.now().isoformat(),
                    "resume_user_reply": reply,
                    "resume_source": resume_source,
                },
            )
        seat_state_id = str(task.metadata.get("seat_state_id") or task.metadata.get("delegation_seat_state_id") or "").strip()
        if seat_state_id and hasattr(self.store, "update_seat_state"):
            await self.store.update_seat_state(
                seat_state_id,
                current_task_id="",
                current_work_item_id="",
                current_work_item={},
                status="idle",
                resident_status="idle",
                metadata_updates={
                    "resume_requested_at": datetime.now().isoformat(),
                    "resume_user_reply": reply,
                    "resume_source": resume_source,
                    "current_turn_mode": "dispatch_required",
                },
            )

    async def _resume_company_runtime_via_final_decider(
        self,
        *,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        user_reply: str,
        session_id: str | None,
        resume_source: str = "primary_session_followup",
        context_updates: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
        degrade_to_plain_resume_on_missing_target: bool = False,
    ) -> str | None:
        assert self.company_executor
        reply = str(user_reply or "").strip()
        if not reply:
            return None
        target_task = self._company_followup_target_task(plan, tasks)
        if target_task is None:
            # No live final-decider card remains (e.g. it failed terminally
            # during resume preparation). For the suspend-checkpoint caller,
            # degrade to a plain runtime resume so the run converges and the
            # checkpoint drains, instead of parking the checkpoint pending
            # forever and dead-ending every follow-up.
            if degrade_to_plain_resume_on_missing_target:
                result = await self.company_executor.execute(plan, tasks)
                note = (
                    "The final-decider work item for this run is no longer active "
                    "(failed or cancelled), so the follow-up could not be routed to it. "
                    "Resumed the remaining runtime instead; re-issue the request as a "
                    "new task if further work is needed."
                )
                return f"{note}\n\n{str(result or '').strip()}".strip()
            return None
        projection_label = projection_id_for_task(target_task) or str(target_task.title or target_task.id).strip()
        projection_title = str(target_task.title or projection_label).strip() or projection_label
        snapshot = self._format_company_runtime_snapshot(
            tasks,
            heading="## Latest Runtime Snapshot (before follow-up)",
            annotations={projection_label: "final decider follow-up"},
        )
        await self._prepare_company_followup_target(
            target_task,
            reply,
            resume_source=resume_source,
            context_updates=context_updates,
            metadata_updates=metadata_updates,
        )
        if self.on_company_runtime_children and session_id and tasks:
            self.on_company_runtime_children(session_id, [t.id for t in tasks])
        result = await self.company_executor.execute(plan, tasks)
        refreshed_target = target_task
        if self.store:
            try:
                refreshed = await self.store.get_task(target_task.id)
                if refreshed is not None:
                    refreshed_target = refreshed
            except Exception:
                refreshed_target = target_task
        target_metadata = dict(getattr(refreshed_target, "metadata", {}) or {})
        close_message = str(target_metadata.get("feedback_close_user_message", "") or "").strip()
        if close_message:
            return close_message
        if bool(target_metadata.get("feedback_closed", False)):
            return "The human review has been closed by the final decider."
        if self.store and session_id:
            refreshed_snapshot = await self._load_company_runtime_snapshot(session_id)
            if refreshed_snapshot is not None:
                refreshed_plan, refreshed_tasks = refreshed_snapshot
                await self._ensure_open_final_delivery_review_checkpoints(
                    refreshed_plan,
                    refreshed_tasks,
                )
        if (
            bool(target_metadata.get("manager_board_mutation_performed", False))
            or [
                str(item).strip()
                for item in list(target_metadata.get("delegation_wait_for_work_item_ids", []) or [])
                if str(item).strip()
            ]
        ):
            return (
                f"Routed the latest user follow-up to `{projection_label}` ({projection_title}) "
                "and resumed the existing company runtime. The updated work item board is continuing through the normal runtime."
                f"\n\n{snapshot}"
            ).strip()
        return (
            str(result or "").strip()
            or f"Routed the latest user follow-up to `{projection_label}` ({projection_title}) and resumed the existing company runtime."
        )

    async def _close_company_delivery_review_task(
        self,
        task: Task,
        *,
        resolution: str,
        closed_at: str | None = None,
        checkpoint_id: str = "",
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        if not self.store:
            return
        now = str(closed_at or datetime.now().isoformat())
        task.metadata = dict(task.metadata or {})
        if metadata_updates:
            task.metadata.update(copy.deepcopy(dict(metadata_updates)))
        if checkpoint_id:
            task.metadata["human_review_checkpoint_id"] = checkpoint_id
        progress = list(task.metadata.get("progress_log", []) or [])
        progress.append(f"Delivery human review closed: {resolution}.")
        close_updates = {
            "requires_user_feedback": False,
            "human_review_closed": True,
            "human_review_closed_at": now,
            "human_review_resolution": resolution,
            "feedback_closed": True,
            "feedback_resolved": True,
            "feedback_resolution": resolution,
            "feedback_closed_at": now,
            "progress_log": progress[-50:],
        }
        task.metadata.update(close_updates)
        task.status = TaskStatus.DONE
        task.execution_lock = False
        task.execution_locked_at = None
        await self.store.save_task(task)

        work_item_id = str(linked_work_item_id_for_task(task) or "").strip()
        if not work_item_id or not hasattr(self.store, "update_delegation_work_item"):
            return

        work_item_updates = {
            **close_updates,
            "task_status": TaskStatus.DONE.value,
            "last_transition_reason": resolution,
        }
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.APPROVED,
                blocked_reason="",
                metadata_updates=work_item_updates,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
        except InvalidPhaseTransition:
            try:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates=work_item_updates,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to update closed delivery review work item metadata for {}",
                    work_item_id,
                )
        except TypeError:
            try:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    phase=Phase.APPROVED,
                    blocked_reason="",
                    metadata_updates=work_item_updates,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to approve closed delivery review work item for {}",
                    work_item_id,
                )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to approve closed delivery review work item for {}",
                work_item_id,
            )

    async def _terminalize_company_delivery_feedback_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        status: str,
        resolution: str,
        payload_updates: dict[str, Any] | None = None,
        task_metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        if not self.store:
            return
        payload = {**dict(checkpoint.payload or {}), **dict(payload_updates or {})}
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(checkpoint, "task_id", "")
            or ""
        ).strip()
        closed_at = str(
            (task_metadata_updates or {}).get("self_evolution_review_completed_at")
            or (task_metadata_updates or {}).get("feedback_superseded_at")
            or datetime.now().isoformat()
        )
        if waiting_task_id:
            try:
                waiting_task = await self.store.get_task(waiting_task_id)
            except Exception:
                waiting_task = None
            if waiting_task is not None:
                await self._close_company_delivery_review_task(
                    waiting_task,
                    resolution=resolution,
                    closed_at=closed_at,
                    checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
                    metadata_updates=task_metadata_updates,
                )
        await self._mark_company_runtime_checkpoint_status(
            checkpoint,
            status=status,
            payload_updates=payload,
        )

    async def ignore_company_delivery_feedback_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        assert self.store
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            return "This self-evolution review is no longer active."

        payload = dict(checkpoint.payload or {})
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(checkpoint, "task_id", "")
            or ""
        ).strip()
        if not waiting_task_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not ignore self-evolution because the delivery task reference is missing."
        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not ignore self-evolution because the delivery task no longer exists."

        ignored_at = datetime.now().isoformat()
        await self._terminalize_company_delivery_feedback_checkpoint(
            checkpoint,
            status="ignored",
            resolution="self_evolution_review_ignored",
            payload_updates={
                **payload,
                "feedback_ignored": True,
                "feedback_ignored_at": ignored_at,
                "feedback_resolution": "self_evolution_review_ignored",
                "feedback_reply_metadata": dict(reply_metadata or {}),
            },
            task_metadata_updates={
                "self_evolution_review_ignored": True,
                "self_evolution_review_ignored_at": ignored_at,
                "feedback_ignored": True,
                "feedback_ignored_at": ignored_at,
            },
        )
        return "Self-evolution review ignored."

    async def _ensure_open_final_delivery_review_checkpoints(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> None:
        if not self.store:
            return
        open_delivery_tasks = [
            task
            for task in tasks
            if task.status == TaskStatus.AWAITING_HUMAN
            and self._is_open_final_delivery_review_task(task)
            and not self._metadata_flag_true(dict(getattr(task, "metadata", {}) or {}).get("self_evolution_review_completed", False))
        ]
        if not open_delivery_tasks:
            return
        try:
            pending = await self.store.get_pending_checkpoints(
                project_id=self.project_id or "default",
                checkpoint_types=["company_delivery_feedback"],
            )
        except Exception:
            logger.opt(exception=True).debug("failed to inspect pending delivery feedback checkpoints")
            pending = []
        pending_task_ids = {
            str(
                getattr(checkpoint, "task_id", "")
                or dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
                or dict(getattr(checkpoint, "payload", {}) or {}).get("task_id")
                or ""
            ).strip()
            for checkpoint in pending
        }
        for task in open_delivery_tasks:
            if str(task.id or "").strip() in pending_task_ids:
                continue
            try:
                await self._save_company_feedback_followup_checkpoint(task, tasks, plan)
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to restore missing delivery feedback checkpoint for task {}",
                    task.id,
                )

    @staticmethod
    def _reply_metadata_requests_force_resume(reply_metadata: dict[str, Any] | None) -> bool:
        return bool(dict(reply_metadata or {}).get("ui_force_resume", False))

    async def _maybe_resume_existing_company_runtime(
        self,
        user_reply: str,
        session_id: str | None = None,
        *,
        force_resume: bool = False,
    ) -> str | None:
        assert self.store
        if not self.company_executor or not session_id:
            return None
        runtime_session_id = await self._company_runtime_parent_session_for_session_id(session_id)
        runtime_session_id = runtime_session_id or session_id
        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            return None
        plan, tasks = snapshot
        if not tasks:
            return None
        tasks = await self._terminalize_already_closed_delivery_review_tasks(tasks)
        work_item_runtime_tasks = [task for task in tasks if is_work_item_runtime_metadata(task.metadata)]
        if work_item_runtime_tasks and self._runtime_uses_multi_team_org(plan) and all(self._task_uses_multi_team_org(task) for task in work_item_runtime_tasks):
            live_running_tasks: list[Task] = []
            for task in tasks:
                if task.status == TaskStatus.RUNNING and await self._task_runtime_is_live(task):
                    live_running_tasks.append(task)
            waiting_tasks = [task for task in tasks if task.status in _WAITING_TASK_STATUSES]
            pending_tasks = [task for task in tasks if task.status == TaskStatus.PENDING]
            failed_tasks = [task for task in tasks if task.status == TaskStatus.FAILED]
            blocked_tasks = [task for task in tasks if task.status == TaskStatus.BLOCKED]
            no_active_runtime_work = not live_running_tasks and not waiting_tasks and not pending_tasks and not failed_tasks and not blocked_tasks
            has_closed_delivery_review = any(self._is_closed_company_delivery_review_task(task) for task in tasks)
            if not force_resume:
                if not (no_active_runtime_work and has_closed_delivery_review):
                    followup_result = await self._resume_company_runtime_via_final_decider(
                        plan=plan,
                        tasks=tasks,
                        user_reply=user_reply,
                        session_id=runtime_session_id,
                    )
                    if followup_result is not None:
                        return followup_result
            if no_active_runtime_work:
                return None
            if live_running_tasks or waiting_tasks:
                snapshot_text = self._format_company_runtime_snapshot(tasks)
                active_labels = ", ".join(
                    f"`{str(task.title or projection_id_for_task(task) or task.id)}`"
                    for task in [*live_running_tasks, *waiting_tasks][:6]
                )
                return (
                    "The latest multi-team organization run is already in progress for this session. "
                    f"Active turns: {active_labels}.\n\n{snapshot_text}"
                )
            if self.on_company_runtime_children and runtime_session_id and tasks:
                self.on_company_runtime_children(runtime_session_id, [t.id for t in tasks])
            result = await self.company_executor.execute(plan, tasks)
            snapshot_text = self._format_company_runtime_snapshot(
                tasks,
                heading="## Latest Organization Snapshot (before resume)",
            )
            return f"Resuming the existing multi-team organization run.\n\n{snapshot_text}\n\n{result}".strip()
        all_terminal = all(
            t.status in {TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED}
            for t in tasks
        )
        if all_terminal:
            return None
        snapshot_text = self._format_company_runtime_snapshot(
            tasks,
            heading="## Legacy Runtime Snapshot (read-only)",
        )
        return (
            "A legacy company runtime run was found for this session. "
            "Legacy runs are read-only and cannot be resumed under the work-item runtime.\n\n"
            f"{snapshot_text}"
        )

    @classmethod
    def _is_closed_company_delivery_review_task(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        closed = (
            cls._metadata_flag_true(metadata.get("self_evolution_review_completed", False))
            or cls._metadata_flag_true(metadata.get("feedback_closed", False))
            or cls._metadata_flag_true(metadata.get("human_review_closed", False))
            or cls._metadata_flag_true(metadata.get("feedback_superseded", False))
        )
        if not closed:
            return False
        turn_type = turn_type_for_task(task, fallback="")
        feedback_scope = str(metadata.get("feedback_scope", "") or "").strip().lower()
        return (
            turn_type in {"deliver", "delivery"}
            or feedback_scope == "final"
            or cls._metadata_flag_true(metadata.get("authoritative_output", False))
        )

    async def _terminalize_already_closed_delivery_review_tasks(self, tasks: list[Task]) -> list[Task]:
        if not self.store:
            return tasks
        refreshed_tasks: list[Task] = []
        for task in tasks:
            metadata = dict(getattr(task, "metadata", {}) or {})
            if task.status in _WAITING_TASK_STATUSES and self._is_closed_company_delivery_review_task(task):
                resolution = str(
                    metadata.get("feedback_resolution")
                    or metadata.get("human_review_resolution")
                    or (
                        "self_evolution_review_completed"
                        if self._metadata_flag_true(metadata.get("self_evolution_review_completed", False))
                        else "delivery_review_closed"
                    )
                ).strip()
                await self._close_company_delivery_review_task(
                    task,
                    resolution=resolution,
                    closed_at=str(
                        metadata.get("self_evolution_review_completed_at")
                        or metadata.get("feedback_closed_at")
                        or metadata.get("human_review_closed_at")
                        or datetime.now().isoformat()
                    ),
                    checkpoint_id=str(metadata.get("human_review_checkpoint_id", "") or "").strip(),
                )
                try:
                    reloaded = await self.store.get_task(task.id)
                except Exception:
                    reloaded = None
                refreshed_tasks.append(reloaded or task)
                continue
            refreshed_tasks.append(task)
        return refreshed_tasks

    def _ensure_attachment_store(self) -> None:
        project_id = self.project_id or "default"
        if self.attachment_store and self.attachment_store.project_id == project_id:
            return
        self.attachment_store = AttachmentStore(self.opc_home, project_id)

    def _normalize_attachment_refs(self, attachments: list[Any] | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in list(attachments or []):
            try:
                if isinstance(item, AttachmentRef):
                    normalized.append(item.to_dict())
                    continue
                if isinstance(item, dict) and item.get("attachment_id"):
                    normalized.append(AttachmentRef.from_dict(item).to_dict())
            except Exception as exc:
                logger.warning(f"Skipping invalid attachment reference: {exc}")
        return normalized

    def _is_inline_text_attachment(self, ref: AttachmentRef) -> bool:
        return can_extract_text(ref.filename, ref.mime_type)

    def _build_attachment_context(self, attachment_refs: list[dict[str, Any]] | None) -> str:
        refs = self._normalize_attachment_refs(attachment_refs)
        if not refs:
            return ""
        self._ensure_attachment_store()
        if not self.attachment_store:
            return ""

        parts = ["## Attachments"]
        remaining_budget = 5000
        hidden_count = 0

        for index, ref_dict in enumerate(refs, start=1):
            try:
                ref = AttachmentRef.from_dict(ref_dict)
            except Exception as exc:
                logger.warning(f"Failed to parse attachment ref: {exc}")
                continue

            if index > 6:
                hidden_count += 1
                continue

            try:
                abs_path = self.attachment_store.resolve_abs_path(ref)
            except Exception as exc:
                logger.warning(f"Failed to resolve attachment path for {ref.filename}: {exc}")
                abs_path = self.opc_home / ref.disk_path if ref.disk_path else None

            parts.append(f"### {ref.filename}")
            parts.append(f"- MIME type: {ref.mime_type}")
            parts.append(f"- Size: {ref.size_bytes} bytes")
            if abs_path:
                parts.append(f"- Stored path: {abs_path}")

            if self._is_inline_text_attachment(ref) and remaining_budget > 0:
                try:
                    preview = extract_attachment_text(
                        ref.filename,
                        ref.mime_type,
                        self.attachment_store.read_bytes(ref),
                        max_chars=min(remaining_budget, 1800),
                    ).strip()
                except Exception as exc:
                    parts.append(f"- Inline preview unavailable: {exc}")
                    continue
                if not preview:
                    parts.append("- Inline preview: [empty file]")
                    continue
                clipped = preview[: min(remaining_budget, 1800)]
                if len(clipped) < len(preview):
                    clipped = f"{clipped}\n...[truncated]"
                parts.append("```text")
                parts.append(clipped)
                parts.append("```")
                remaining_budget -= len(clipped)
                continue

            if ref.mime_type.startswith("image/"):
                parts.append("- Note: image attachment is stored and may be passed directly to the model when the provider path supports image input.")
            elif ref.mime_type == "application/pdf":
                parts.append("- Note: PDF attachment is stored and may be passed directly to the model when the provider path supports PDF input.")
            elif ref.mime_type.startswith("video/"):
                parts.append("- Note: video attachment is stored and may be passed directly to the model when the provider path supports video input.")
            else:
                parts.append("- Note: binary or complex document is available by path only when inline extraction is not available.")

        if hidden_count:
            parts.append(f"- Additional attachments omitted from inline context: {hidden_count}")
        return "\n".join(parts)

    def _secretary_workspace_root(self) -> str | None:
        return None

    async def _resolve_target_output_dir(self, message: str, session_id: str | None = None) -> str | None:
        contract = await self._resolve_workspace_contract(message, session_id)
        output_root = str(contract.get("output_root") or "").strip()
        return output_root or None

    def _resolve_comms_workspace_root(self, target_output_dir: str | None) -> str | None:
        """Pick the directory under which the file-based comms tree lives.

        Comms is OPC's collaboration substrate, NOT a deliverable — it
        belongs at the *workspace* root (sibling to project deliverables),
        not inside the project's specific output folder. Putting it
        inside the deliverable folder pollutes that folder with
        OpenOPC-internal state.

        Resolution order:

        1. Secretary's known workspace root (the user's "main workspace
           directory" — same place subprojects live in).
        2. The parent of `target_output_dir` (under the assumption that
           `target_output_dir` is a project subfolder).
        3. `target_output_dir` itself, as a last resort so something
           still works even if no workspace concept exists.
        4. None — caller should skip comms.
        """
        if target_output_dir:
            try:
                parent = str(Path(target_output_dir).expanduser().resolve().parent)
                if parent and parent not in {"/", "."}:
                    return parent
            except Exception:
                pass
            return target_output_dir
        return None

    def _apply_session_execution_defaults(
        self,
        decision: RouterDecision,
        session_defaults: dict[str, Any] | None,
        user_message: str,
    ) -> RouterDecision:
        defaults = dict(session_defaults or {})
        if not defaults:
            return decision

        explicit_override = self._detect_explicit_mode_override(user_message)
        previous_mode = str(defaults.get("mode") or "").strip()
        previous_profile = str(defaults.get("company_profile") or "").strip() or None
        previous_agent = str(defaults.get("preferred_agent") or "").strip() or None

        if explicit_override == ExecutionMode.SINGLE_AGENT.value:
            return decision

        if decision.mode == ExecutionMode.COMPANY_MODE:
            if not decision.company_profile and previous_profile:
                decision.company_profile = previous_profile
            if previous_agent and decision.preferred_agent is None:
                decision.preferred_agent = previous_agent
            return decision

        if (
            previous_mode == ExecutionMode.COMPANY_MODE.value
            and explicit_override != ExecutionMode.SINGLE_AGENT.value
            and self._looks_like_followup_request(user_message)
        ):
            decision.mode = ExecutionMode.COMPANY_MODE
            decision.company_profile = decision.company_profile or previous_profile
            if previous_agent and decision.preferred_agent is None:
                decision.preferred_agent = previous_agent
            suffix = " Continuing the prior company-mode session by default."
            existing_reasoning = str(getattr(decision, "reasoning", "") or "").strip()
            decision.reasoning = f"{existing_reasoning}{suffix}".strip() if existing_reasoning else suffix.strip()
        return decision

    def _resolve_external_workspace(self, task: Task) -> str:
        workspace_root = (
            str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
        )
        if workspace_root:
            target = Path(workspace_root).expanduser()
            try:
                target.mkdir(parents=True, exist_ok=True)
                return str(target.resolve())
            except Exception as e:
                logger.warning(f"Failed to prepare external workspace {workspace_root}: {e}")

        workspace = get_project_workplace(task.project_id or "default")
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace.resolve())

    def _resolved_execution_agent_name_for_task(self, task: Task) -> str:
        role_id = str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        role = self.org_engine.get_agent(role_id) if self.org_engine and role_id else None
        preferred_external = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent", "") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        )
        selected = normalize_recruitment_agent_choice(
            task.metadata.get("selected_execution_agent"),
            default=("native" if not preferred_external else preferred_external),
        )
        return selected or ("native" if not preferred_external else preferred_external) or "native"

    @staticmethod
    def _role_prompt_user_payload(payload: dict[str, Any]) -> str:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "Assessment payload:\n"
            "```json\n"
            f"{rendered}\n"
            "```\n\n"
            "Return JSON only."
        )

    @staticmethod
    def _role_prompt_external_description(system_prompt: str, payload: dict[str, Any]) -> str:
        return (
            f"{str(system_prompt or '').strip()}\n\n"
            f"{OPCEngine._role_prompt_user_payload(payload)}"
        ).strip()

    def _build_role_prompt_task(
        self,
        source_task: Task,
        *,
        prompt_kind: str,
        description: str,
        execution_agent: str,
        system_prompt: str = "",
        force_new_session: bool = True,
    ) -> Task:
        role_id = str(source_task.assigned_to or source_task.metadata.get("work_item_role_id", "") or "").strip()
        session_id = str(source_task.session_id or source_task.parent_session_id or "").strip() or None
        metadata = {
            "prompt_kind": prompt_kind,
            "source_task_id": str(source_task.id or "").strip(),
            "source_task_title": str(source_task.title or "").strip(),
            "selected_execution_agent": execution_agent,
            "workspace_root": str(source_task.metadata.get("workspace_root", "") or "").strip(),
            "comms_workspace_root": str(source_task.metadata.get("comms_workspace_root", "") or "").strip(),
            "target_output_dir": str(source_task.metadata.get("target_output_dir", "") or "").strip(),
            "_disable_live_inbox_interrupts": True,
            "attachment_refs": [],
        }
        if execution_agent != "native":
            metadata["preferred_external_agent"] = execution_agent
        if system_prompt:
            metadata["_runtime_system_prompt_override"] = str(system_prompt or "").strip()
        if force_new_session:
            metadata.pop("external_resume_session_id", None)
            metadata.pop("external_resume_session_scope_id", None)
            metadata.pop("external_resume_agent_type", None)
        else:
            source_metadata = dict(source_task.metadata or {})
            for key in (
                "work_item_runtime",
                "work_item_runtime_version",
                "execution_mode",
                "company_profile",
                "work_item_role_id",
                "work_item_projection_id",
                "work_item_turn_type",
                "delegation_role_session_id",
                "delegation_seat_id",
                "external_resume_session_id",
                "external_resume_session_scope_id",
                "external_resume_agent_type",
                "employee_assignment",
                "selected_execution_agent",
                "preferred_external_agent",
            ):
                if key in source_metadata and source_metadata.get(key) not in (None, "", [], {}):
                    metadata[key] = copy.deepcopy(source_metadata[key])
            metadata.setdefault("work_item_runtime", bool(source_metadata.get("work_item_runtime", False)))
        return Task(
            id=f"{str(source_task.id or 'task').strip() or 'task'}::{prompt_kind}::{uuid.uuid4().hex}",
            session_id=session_id,
            parent_session_id=str(source_task.parent_session_id or "").strip() or None,
            title=prompt_kind.replace("_", " ").title(),
            description=description,
            assigned_to=role_id,
            status=TaskStatus.PENDING,
            assigned_external_agent=None if execution_agent == "native" else execution_agent,
            project_id=str(source_task.project_id or self.project_id or "default"),
            tags=list(source_task.tags or []),
            context_snapshot={"skip_session_history": True},
            metadata=metadata,
            org_id=source_task.org_id,
        )

    async def _run_role_prompt_via_task_execution_agent(
        self,
        source_task: Task,
        system_prompt: str,
        payload: dict[str, Any],
        prompt_kind: str,
        force_new_session: bool = True,
    ) -> str | None:
        execution_agent = self._resolved_execution_agent_name_for_task(source_task)
        if execution_agent == "native":
            prompt_task = self._build_role_prompt_task(
                source_task,
                prompt_kind=prompt_kind,
                description=self._role_prompt_user_payload(payload),
                execution_agent="native",
                system_prompt=system_prompt,
                force_new_session=force_new_session,
            )
            result = await self._run_native_agent(prompt_task)
            return str(result.content or "").strip() if result.status == TaskStatus.DONE else None

        if not self.adapter_registry or not self.external_broker:
            return None
        adapter = self.adapter_registry.get(execution_agent)
        if adapter is None:
            return None
        prompt_task = self._build_role_prompt_task(
            source_task,
            prompt_kind=prompt_kind,
            description=self._role_prompt_external_description(system_prompt, payload),
            execution_agent=execution_agent,
            force_new_session=force_new_session,
        )
        run_adapter, _ = await self._configure_external_adapter_for_task(prompt_task, adapter)
        if force_new_session:
            adapter_config = getattr(run_adapter, "config", None)
            if adapter_config is not None:
                cloned_config = (
                    adapter_config.model_copy(deep=True)
                    if hasattr(adapter_config, "model_copy")
                    else adapter_config
                )
                if hasattr(cloned_config, "session_mode"):
                    cloned_config.session_mode = "new"
                if hasattr(cloned_config, "session_id"):
                    cloned_config.session_id = ""
                run_adapter = run_adapter.__class__(config=cloned_config)
        workspace = self._resolve_external_workspace(source_task)
        prepared_task = await self._build_external_agent_task(copy.deepcopy(prompt_task))
        result = await self.external_broker.run(
            adapter=run_adapter,
            task=prompt_task,
            workspace_path=workspace,
            prepared_task=prepared_task,
        )
        if result.status != TaskStatus.DONE:
            logger.debug(
                f"Role prompt `{prompt_kind}` failed via `{execution_agent}` for source task `{source_task.id}`: "
                f"{str(result.content or '').strip()}"
            )
            return None
        return str(result.content or "").strip()

    async def _run_task_once(self, task: Task) -> TaskResult:
        attempts: list[dict[str, Any]] = []
        candidates = self._get_external_candidates(task)
        scoped_progress = self._make_task_progress_callback(task)
        if candidates:
            self._apply_task_mode_external_timeout_defaults(task)
            workspace = self._resolve_external_workspace(task)

            for agent_name, adapter in candidates:
                run_adapter, resume_metadata = await self._configure_external_adapter_for_task(task, adapter)
                adapter_config = getattr(run_adapter, "config", None)
                session_mode = str(getattr(adapter_config, "session_mode", "") or "").strip().lower()
                run_mode = str(getattr(adapter_config, "run_mode", "batch") or "batch").strip().lower()
                supports_interactive = bool(
                    run_adapter.supports_interactive() if hasattr(run_adapter, "supports_interactive") else False
                )
                task.metadata = dict(task.metadata)
                task.metadata["__external_resume_session"] = session_mode == "resume"
                external_prompt_task = copy.deepcopy(task)
                external_prompt_task.metadata = dict(external_prompt_task.metadata)
                external_prompt_task.metadata["__external_resume_session"] = session_mode == "resume"
                external_task = await self._build_external_agent_task(external_prompt_task)
                for key in (
                    "external_resume_review_feedback_version",
                    "external_resume_review_feedback_digest",
                ):
                    value = dict(getattr(external_task, "metadata", {}) or {}).get(key)
                    if value in (None, "", [], {}):
                        continue
                    task.metadata = dict(task.metadata or {})
                    task.metadata[key] = copy.deepcopy(value)
                if run_mode == "interactive" and supports_interactive:
                    cmd, metadata = run_adapter.build_interactive_invocation(external_task, workspace_path=workspace)
                else:
                    cmd, metadata = run_adapter.build_invocation(external_task, workspace_path=workspace)
                explicit_user_selected_agent = (
                    str(task.metadata.get("router_preferred_agent", "") or "").strip() == agent_name
                    or (
                        bool(task.metadata.get("execution_agent_locked"))
                        and str(task.metadata.get("selected_execution_agent", "") or "").strip() == agent_name
                    )
                    or (
                        str(
                            dict(task.metadata.get("agent_selection", {}) or {}).get(
                                "selection_source",
                                "",
                            )
                            or ""
                        ).strip()
                        == "company_runtime_resume_checkpoint"
                        and str(task.assigned_external_agent or "").strip()
                        == agent_name
                    )
                )
                metadata = {
                    **metadata,
                    "workspace": workspace,
                    "argv": cmd,
                    "target_output_dir": task.metadata.get("target_output_dir"),
                    "explicit_user_selected_agent": explicit_user_selected_agent,
                    "external_session_continuation": session_mode == "resume",
                    **resume_metadata,
                }
                await self._emit_external_agent_audit(
                    external_task,
                    metadata,
                    workspace,
                    progress_callback=scoped_progress,
                )

                broker_run_kwargs = {
                    "adapter": run_adapter,
                    "task": task,
                    "workspace_path": workspace,
                    "on_progress": scoped_progress,
                }
                if "prepared_task" in inspect.signature(self.external_broker.run).parameters:
                    broker_run_kwargs["prepared_task"] = external_task
                result = await self.external_broker.run(**broker_run_kwargs)
                attempts.append({
                    "agent": agent_name,
                    "status": result.status.value,
                    "command": metadata.get("command", ""),
                    "model": metadata.get("model", "(cli default)"),
                    "session_mode": metadata.get("session_mode", "auto"),
                    "new_session": metadata.get("new_session", False),
                    "failure_reason": result.content if result.status != TaskStatus.DONE else "",
                    "last_activity_at": str((result.artifacts or {}).get("last_activity_at", "")),
                    "activity_count": int((result.artifacts or {}).get("activity_count", 0) or 0),
                })

                if not result.artifacts:
                    result.artifacts = metadata
                else:
                    result.artifacts = {**metadata, **result.artifacts}
                result.artifacts["external_attempts"] = attempts
                session_token = str(
                    result.artifacts.get("resume_session_id", "")
                    or result.artifacts.get("provider_session_id", "")
                    or metadata.get("resume_session_id", "")
                    or ""
                ).strip()
                if session_token and result.status == TaskStatus.DONE:
                    task.metadata = dict(task.metadata)
                    task.metadata["external_resume_session_id"] = session_token
                    task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
                    task.metadata["external_resume_agent_type"] = str(
                        getattr(run_adapter, "agent_type", "") or agent_name
                    ).strip()

                if result.status == TaskStatus.DONE:
                    return result
                if self._external_result_requires_user_review(result):
                    logger.info(
                        f"External agent {agent_name} is awaiting user review for task {task.id}, pausing execution"
                    )
                    return result
                if self._external_result_denied_by_user(result):
                    logger.info(
                        f"User denied external agent {agent_name} for task {task.id}, falling back to native agent"
                    )
                    if scoped_progress:
                        await scoped_progress(
                            f"[External agent denied] user denied {agent_name} for task={task.title}; falling back to native agent"
                        )
                    break
                if explicit_user_selected_agent:
                    logger.warning(
                        f"Explicit external agent {agent_name} failed for task {task.id}; "
                        "not trying alternate agents or native fallback"
                    )
                    if scoped_progress:
                        reason_excerpt = (result.content or "").strip().replace("\n", " ")
                        await scoped_progress(
                            f"[External agent failed] explicit {agent_name} failed for task={task.title}; "
                            f"reason={reason_excerpt or 'unknown'}"
                        )
                    return result

                logger.warning(
                    f"External agent {agent_name} failed for task {task.id}, trying next configured agent"
                )
                if scoped_progress:
                    reason_excerpt = (result.content or "").strip().replace("\n", " ")
                    await scoped_progress(
                        f"[External agent failed] {agent_name} failed for task={task.title}; "
                        f"reason={reason_excerpt or 'unknown'}; trying next configured agent"
                    )

            logger.warning("All configured external agents failed, falling back to native agent")
            if scoped_progress:
                await scoped_progress(
                    f"[External agents exhausted] task={task.title}; falling back to native agent"
                )

        native_result = await self._run_native_agent(task)
        if attempts:
            native_result.artifacts = {
                **(native_result.artifacts or {}),
                "external_attempts": attempts,
                "external_fallback_to_native": True,
            }
        return native_result

    async def _execute_task(self, task: Task) -> TaskResult:
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            attempt_token = self._active_task_run_registry.register(project_id, task.id)
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        try:
            return await self._execute_registered_task_attempt(task)
        finally:
            self._active_task_run_registry.unregister(project_id, task.id, attempt_token)

    @staticmethod
    def _company_runtime_task_has_durable_hold(task: Task | None) -> bool:
        if task is None or not is_company_runtime_task(task):
            return False
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("dispatch_hold", "") or "").strip()
            == "company_runtime_suspended"
            or str(metadata.get("company_runtime_stop_state", "") or "").strip()
            in {"suspending", "suspended"}
        )

    @staticmethod
    def _ensure_result_delivery_identity_for_commit(
        task: Task,
        result: TaskResult,
    ) -> dict[str, str]:
        """Reuse the runtime's immutable delivery id or mint one once.

        A runtime-generated canonical turn is not guaranteed to be copied back
        into mutable Task metadata.  The TaskResult artifact is therefore the
        hand-off boundary between runtime persistence and the engine mirrors.
        External/pause results without a canonical turn receive a per-result
        execution seed here. Runtime/provider *session* ids are deliberately
        excluded because those sessions survive resume and can produce several
        legitimate deliveries for the same task and retry count.

        The generated identity is written back to ``TaskResult.artifacts``
        before the task result is persisted. Replaying that durable result is
        therefore stable instead of minting a second UI row after a restart.
        """
        artifacts = dict(result.artifacts or {})
        canonical_turn_id = str(
            artifacts.get("canonical_turn_id")
            or artifacts.get("conversation_turn_id")
            or ""
        ).strip()
        persisted_delivery_id = str(artifacts.get("result_delivery_id") or "").strip()
        execution_id = str(artifacts.get("result_execution_id") or "").strip()
        if not persisted_delivery_id and not canonical_turn_id and not execution_id:
            execution_id = uuid.uuid4().hex
        identity = result_delivery_identity_payload_for_task(
            task,
            canonical_turn_id=canonical_turn_id,
            execution_id=execution_id,
            result_delivery_id=persisted_delivery_id,
        )
        result.artifacts = {
            **artifacts,
            **({"result_execution_id": execution_id} if execution_id else {}),
            **identity,
        }
        return identity

    async def _execute_registered_task_attempt(self, task: Task) -> TaskResult:
        try:
            result = await self._run_task_once(task)
        except asyncio.CancelledError:
            if self._shutting_down or is_company_runtime_task(task):
                raise
            store = self.store
            if not store or not bool(getattr(store, "is_ready", True)):
                raise
            try:
                fresh = await store.get_task(task.id)
            except AssertionError:
                logger.debug(
                    "Task cancellation cleanup skipped because store is already closed for task {}",
                    task.id,
                )
                raise
            target = fresh or task
            if is_company_runtime_task(target):
                raise
            if target.status != TaskStatus.CANCELLED:
                target.status = TaskStatus.CANCELLED
                await store.save_task(target)
            raise
        # Re-read task to check if user cancelled during execution
        fresh = await self.store.get_task(task.id)
        if fresh and fresh.status == TaskStatus.CANCELLED:
            logger.info(f"Task {task.id} was cancelled during execution, preserving CANCELLED status")
            return result
        if self._company_runtime_task_has_durable_hold(fresh):
            logger.info(
                "Discarding completed result for task {} because a durable company "
                "runtime hold won the completion race",
                task.id,
            )
            raise asyncio.CancelledError(
                "company runtime was suspended before result commit"
            )
        self._apply_runtime_state_to_task(task, result)
        result_identity = self._ensure_result_delivery_identity_for_commit(task, result)
        task.status = result.status
        task.result = {"content": result.content, "artifacts": result.artifacts}
        await self.store.save_task(task)
        if self.memory and task.session_id and self._uses_shared_role_session(task):
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            await self.memory.record_assistant_turn(
                session_id=task.session_id,
                content=result.content,
                project_id=task.project_id,
                agent_id=task.assigned_to or None,
                task_id=task.id,
                metadata={
                    "kind": "company_role_result",
                    "status": result.status.value,
                    "employee_id": str(assignment.get("employee_id", "")).strip(),
                    "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                    "child_session_id": str(task.session_id),
                    **result_identity,
                    **work_item_identity_payload_for_task(task),
                },
            )
        elif (
            self.memory
            and task.session_id
            and task.parent_session_id
            and task.session_id != task.parent_session_id
        ):
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            child_result_message = await self.memory.record_assistant_turn(
                session_id=task.session_id,
                content=result.content,
                project_id=task.project_id,
                agent_id=task.assigned_to or None,
                task_id=task.id,
                metadata={
                    "kind": "child_task_result",
                    "status": result.status.value,
                    "employee_id": str(assignment.get("employee_id", "")).strip(),
                    "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                    "child_session_id": str(task.session_id),
                    **result_identity,
                    **work_item_identity_payload_for_task(task),
                },
            )
            await self.memory.record_child_session_result(
                parent_session_id=task.parent_session_id,
                child_session_id=task.session_id,
                task=task,
                result_content=result.content,
                artifacts=result.artifacts,
                result_delivery_id=result_identity.get("result_delivery_id", ""),
                source_result_message_id=str(
                    getattr(child_result_message, "message_id", "") or ""
                ),
                canonical_turn_id=result_identity.get("canonical_turn_id", ""),
            )
        if result.status == TaskStatus.DONE:
            await self._record_task_mode_external_result_reply(task, result.content)
            if self.memory and task.metadata.get("execution_mode") != ExecutionMode.COMPANY_MODE.value:
                await self.memory.record_task_completion_async(
                    task=task,
                    result_content=result.content,
                    project=bool(task.project_id and task.project_id != "default"),
                )
        elif result.status in _REVIEW_WAITING_STATUSES:
            await self._save_task_pause_checkpoint(task, result)
        elif result.status == TaskStatus.AWAITING_PEER:
            await self._save_peer_pause_checkpoint(task, result)

        if result.status == TaskStatus.FAILED and task.retry_count < task.max_retries:
            task.retry_count += 1
            await self._attempt_capability_recovery(task, result)
            task.status = TaskStatus.PENDING
            await self.store.save_task(task)
            logger.info(f"Retrying task {task.id} (attempt {task.retry_count})")
            result = await self._run_task_once(task)
            # Re-read task to check if user cancelled during retry
            fresh = await self.store.get_task(task.id)
            if fresh and fresh.status == TaskStatus.CANCELLED:
                logger.info(f"Task {task.id} was cancelled during retry, preserving CANCELLED status")
                return result
            if self._company_runtime_task_has_durable_hold(fresh):
                logger.info(
                    "Discarding retry result for task {} because a durable company "
                    "runtime hold won the completion race",
                    task.id,
                )
                raise asyncio.CancelledError(
                    "company runtime was suspended before retry result commit"
                )
            self._apply_runtime_state_to_task(task, result)
            result_identity = self._ensure_result_delivery_identity_for_commit(task, result)
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            await self.store.save_task(task)
            if self.memory and task.session_id and self._uses_shared_role_session(task):
                assignment = dict(task.metadata.get("employee_assignment", {}) or {})
                await self.memory.record_assistant_turn(
                    session_id=task.session_id,
                    content=result.content,
                    project_id=task.project_id,
                    agent_id=task.assigned_to or None,
                    task_id=task.id,
                    metadata={
                        "kind": "company_role_result_retry",
                        "status": result.status.value,
                        "retry_count": task.retry_count,
                        "employee_id": str(assignment.get("employee_id", "")).strip(),
                        "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                        "child_session_id": str(task.session_id),
                        **result_identity,
                        **work_item_identity_payload_for_task(task),
                    },
                )
            elif (
                self.memory
                and task.session_id
                and task.parent_session_id
                and task.session_id != task.parent_session_id
            ):
                assignment = dict(task.metadata.get("employee_assignment", {}) or {})
                child_result_message = await self.memory.record_assistant_turn(
                    session_id=task.session_id,
                    content=result.content,
                    project_id=task.project_id,
                    agent_id=task.assigned_to or None,
                    task_id=task.id,
                    metadata={
                        "kind": "child_task_result_retry",
                        "status": result.status.value,
                        "retry_count": task.retry_count,
                        "employee_id": str(assignment.get("employee_id", "")).strip(),
                        "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                        "child_session_id": str(task.session_id),
                        **result_identity,
                        **work_item_identity_payload_for_task(task),
                    },
                )
                await self.memory.record_child_session_result(
                    parent_session_id=task.parent_session_id,
                    child_session_id=task.session_id,
                    task=task,
                    result_content=result.content,
                    artifacts=result.artifacts,
                    result_delivery_id=result_identity.get("result_delivery_id", ""),
                    source_result_message_id=str(
                        getattr(child_result_message, "message_id", "") or ""
                    ),
                    canonical_turn_id=result_identity.get("canonical_turn_id", ""),
                )
            if result.status == TaskStatus.DONE:
                await self._record_task_mode_external_result_reply(task, result.content)
                if self.memory and task.metadata.get("execution_mode") != ExecutionMode.COMPANY_MODE.value:
                    await self.memory.record_task_completion_async(
                        task=task,
                        result_content=result.content,
                        project=bool(task.project_id and task.project_id != "default"),
                    )
        if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            await self._supersede_stale_task_wait_checkpoints(
                task.id, reason=f"task settled as {task.status.value}"
            )
        return result

    async def _attempt_capability_recovery(self, task: Task, result: TaskResult) -> None:
        if not self.capability_manager or not self.config.capabilities.enable_recovery:
            return
        query = task.description or task.title
        if result.content:
            query = f"{query}\n\nFailure context:\n{result.content}"
        recovery_context, candidates = await self.capability_manager.build_recovery_context(query, domains=task.tags)
        if not recovery_context:
            return
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["capability_recovery"] = recovery_context
        if self.on_progress:
            await self.on_progress("[CapabilityRecovery] Attached local skill recovery context and retrying.")

    async def _execute_single_agent(self, tasks: list[Task], use_external: str | None = None) -> str:
        """Execute tasks serially with a single agent."""
        results: list[str] = []

        for task in tasks:
            if use_external and not task.assigned_external_agent:
                task.assigned_external_agent = use_external
            task.status = TaskStatus.RUNNING
            await self.store.save_task(task)
            result = await self._execute_task(task)
            results.append(result.content)

        return "\n\n".join(r for r in results if r)

    async def _execute_multi_agent(self, tasks: list[Task]) -> str:
        """Execute independent tasks in parallel.

        .. deprecated::
            New code should use company mode with company_profile="corporate".
            This method is retained for backward compatibility with checkpoints
            created before the migration.
        """
        logger.warning(
            "[deprecated] _execute_multi_agent called; "
            "new requests should use company mode with parallel profile"
        )
        assert self.task_scheduler

        async def executor(task: Task) -> None:
            await self._execute_task(task)

        tasks = await self.task_scheduler.execute_graph(tasks, executor)

        results = []
        for t in tasks:
            if t.result and t.result.get("content"):
                results.append(f"### {t.title}\n{t.result['content']}")

        return "\n\n".join(results)

    async def _execute_company_mode(self, tasks: list[Task], runtime_plan: Any) -> str:
        """Execute company mode through the multi-team runtime."""
        assert self.company_executor
        if isinstance(runtime_plan, CompanyWorkItemRuntimePlan):
            work_item_plan = runtime_plan
        else:
            spec = runtime_plan if isinstance(runtime_plan, CompanyRuntimeSpec) else None
            task_metadata = dict((tasks[0].metadata if tasks else {}) or {})
            spec_metadata = dict(getattr(spec, "metadata", {}) or {})
            profile = str(
                getattr(spec, "profile", "")
                or task_metadata.get("company_profile", "")
                or getattr(self.config.org, "company_profile", "corporate")
            ).strip() or "corporate"
            plan_payload = serialized_company_plan_from_metadata(task_metadata) or {}
            work_item_plan = (
                deserialize_company_work_item_runtime_plan(plan_payload)
                if isinstance(plan_payload, dict) and plan_payload
                else CompanyWorkItemRuntimePlan(profile=profile)
            )
            work_item_plan.metadata = {
                **dict(work_item_plan.metadata or {}),
                    **spec_metadata,
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": str(
                        getattr(spec, "original_request", "")
                        or task_metadata.get("original_message", "")
                        or ""
                    ).strip(),
            }
        return await self.company_executor.execute(work_item_plan, tasks)

    async def _run_native_agent(self, task: Task) -> TaskResult:
        """Instantiate and run a native agent for a task."""
        assert self.llm and self.memory and self.preferences and self.skills and self.org_engine and self.context_assembler

        if task.assigned_to:
            role = self.org_engine.get_role_for_work_item(task.assigned_to, task.tags)
        else:
            role = self.org_engine.get_role_for_domain(task.tags)

        agent = NativeAgent(
            role=role,
            llm=self.llm,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            event_bus=self.event_bus,
            cost_tracker=self.cost_tracker,
            config=self.config,
            communication=self.communication,
            approval_callback=self._tool_approval_callback,
            permission_policy=self.approval_engine,
        )

        scoped_progress = self._make_task_progress_callback(task)
        return await agent.execute(task, on_progress=scoped_progress)

    async def _run_meeting_turn(
        self,
        meeting: MeetingRoom,
        participant: str,
        request: dict[str, Any],
    ) -> str:
        source_task = await self.store.get_task(str(meeting.task_id)) if meeting.task_id else None
        execution_scope_ids = [
            str(item).strip()
            for item in list(request.get("execution_scope_ids", []) or [])
            if str(item).strip()
        ]
        read_only_tools = [
            "file_read",
            "file_search",
            "list_dir",
            "grep",
            "glob",
            "web_search",
            "web_fetch",
            "probe",
        ]
        temp_task = Task(
            title=f"Meeting Consultation: {meeting.topic}",
            description=str(request.get("task_brief", "") or "").strip() or f"Meeting turn for {participant}",
            assigned_to=participant,
            status=TaskStatus.PENDING,
            project_id=str(getattr(source_task, "project_id", "") or self.project_id or "default"),
            tags=list(getattr(source_task, "tags", []) or []),
            metadata=mark_projected_work_item_task({
                "execution_mode": "company_mode",
                "original_message": str(request.get("task_brief", "") or "").strip(),
                "_subagent_profile_prompt": str(request.get("system_addendum", "") or "").strip(),
                "_fork_allowed_tools": list(read_only_tools),
                "_disable_live_inbox_interrupts": True,
                "execution_task_ids": execution_scope_ids,
                "meeting_room_id": meeting.room_id,
                "meeting_consultation": True,
                "meeting_turn_mode": str(request.get("mode", "participant") or "participant"),
            }, projection_id=f"meeting::{meeting.room_id}::{participant}::round{int(request.get('round', 1) or 1)}", turn_type="plan"),
            context_snapshot={
                "meeting_turn_context": dict(request.get("meeting_context", {}) or {}),
            },
        )
        member_sessions = getattr(self.company_executor.runtime, "member_sessions", {}) if self.company_executor else {}
        session = next(
            (session for session in member_sessions.values() if getattr(session, "role_id", "") == participant),
            None,
        )
        if session is not None:
            session_payload = self.company_executor.runtime._serialize_session(session)
            temp_task.metadata["member_session_state"] = session_payload
            temp_task.context_snapshot["member_session"] = session_payload
        result = await self._run_native_agent(temp_task)
        content = str(result.content or "").strip()
        if content:
            return content
        if str(request.get("mode", "") or "") == "decision_owner":
            return '{"decision":"","action_items":[],"reasoning":"No structured owner decision was produced.","requires_human_input":true,"follow_up_questions":[]}'
        return '{"stance":"abstain","proposal":"","support_level":0.5,"vote":"abstain","reasoning":"No structured meeting response was produced.","blocking_issues":["Missing structured participant response."],"assumptions":[],"questions_for_others":[]}'

    async def _ensure_company_prompt_contract_for_external_task(self, task: Task) -> Task:
        if str(task.metadata.get("execution_mode", "") or "").strip() != "company_mode":
            return task
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or self.store is None or not hasattr(self.store, "get_delegation_work_item"):
            return task
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            work_item = None
        if work_item is None:
            return task

        metadata_updates: dict[str, Any] = {}
        work_metadata = dict(getattr(work_item, "metadata", {}) or {})
        if not has_prompt_contract(work_metadata.get("prompt_contract")):
            prompt_contract = prompt_contract_from_work_item(
                work_item,
                task_metadata=dict(task.metadata or {}),
                task_description=str(task.description or "").strip(),
            )
            metadata_updates["prompt_contract"] = prompt_contract
            if str(prompt_contract.get("source", {}).get("kind", "") or "") == "prompt_contract_blocker":
                metadata_updates["prompt_contract_blocker"] = True

        target_update_key = ""
        target_id_key = ""
        target_brief = ""
        target_title = ""
        if bool(task.metadata.get("review_execution_work_item") or work_metadata.get("review_execution_work_item")):
            target_update_key = "review_target_prompt_contract"
            target_id_key = "review_target_work_item_id"
            target_brief = str(task.metadata.get("review_target_description", "") or work_metadata.get("review_target_description", "") or "").strip()
            target_title = str(task.metadata.get("review_target_title", "") or work_metadata.get("review_target_title", "") or "").strip()
        elif bool(task.metadata.get("report_execution_work_item") or work_metadata.get("report_execution_work_item")):
            target_update_key = "report_target_prompt_contract"
            target_id_key = "report_target_work_item_id"
            target_brief = str(task.metadata.get("report_target_description", "") or work_metadata.get("report_target_description", "") or "").strip()
            target_title = str(task.metadata.get("report_target_title", "") or work_metadata.get("report_target_title", "") or "").strip()

        if target_update_key:
            target_contract = dict(work_metadata.get(target_update_key, {}) or task.metadata.get(target_update_key, {}) or {})
            if not has_prompt_contract(target_contract):
                target_work_item_id = str(task.metadata.get(target_id_key, "") or work_metadata.get(target_id_key, "") or "").strip()
                target_item = None
                if target_work_item_id:
                    try:
                        target_item = await self.store.get_delegation_work_item(target_work_item_id)
                    except Exception:
                        target_item = None
                if target_item is not None:
                    target_contract = prompt_contract_from_work_item(
                        target_item,
                        task_metadata=dict(task.metadata or {}),
                        task_description=target_brief,
                    )
                    if not has_prompt_contract(dict(getattr(target_item, "metadata", {}) or {}).get("prompt_contract")):
                        try:
                            await self.store.update_delegation_work_item(
                                target_work_item_id,
                                metadata_updates={"prompt_contract": target_contract},
                            )
                        except Exception:
                            logger.opt(exception=True).debug("Best-effort external target prompt_contract update failed")
                else:
                    target_contract = prompt_contract_from_work_item(
                        SimpleNamespace(
                            work_item_id=target_work_item_id,
                            title=target_title,
                            summary=target_brief,
                            kind="execute",
                            metadata=dict(task.metadata or {}),
                        ),
                        task_metadata=dict(task.metadata or {}),
                        task_description=target_brief,
                    )
                metadata_updates[target_update_key] = target_contract
                if target_update_key == "review_target_prompt_contract":
                    metadata_updates["prompt_contract"] = make_prompt_contract(
                        task_brief=(
                            "Review the completed child deliverable and decide whether to "
                            "approve it or request rework."
                        ),
                        target_contract=target_contract,
                        source={"kind": "review_auxiliary_work_item"},
                    )
                else:
                    metadata_updates["prompt_contract"] = make_prompt_contract(
                        task_brief=(
                            "Write a structured handoff report for the deliverable you just "
                            "completed. Do not do new execution work."
                        ),
                        target_contract=target_contract,
                        source={"kind": "report_auxiliary_work_item"},
                    )

        if metadata_updates and hasattr(self.store, "update_delegation_work_item"):
            try:
                updated = await self.store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates=metadata_updates,
                )
                if updated is not None:
                    work_metadata = dict(getattr(updated, "metadata", {}) or {})
            except Exception:
                logger.opt(exception=True).debug("Best-effort external prompt_contract update failed")
                work_metadata = {**work_metadata, **metadata_updates}
        else:
            work_metadata = {**work_metadata, **metadata_updates}

        merged_task_metadata = {**dict(task.metadata or {}), **metadata_updates}
        for key in ("prompt_contract", "review_target_prompt_contract", "report_target_prompt_contract", "prompt_contract_blocker"):
            if key in work_metadata:
                merged_task_metadata[key] = work_metadata[key]
        task.metadata = merged_task_metadata
        return task

    @staticmethod
    def _safe_external_attachment_token(value: Any, *, default: str = "item") -> str:
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
        return token[:80] if token else default

    @staticmethod
    def _safe_external_attachment_filename(filename: str) -> str:
        name = Path(str(filename or "attachment")).name
        name = name.replace("..", "").replace("/", "").replace("\\", "")
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return name[:160] if name else "attachment"

    def _attachment_store_for_ref(self, task: Task, ref: AttachmentRef) -> AttachmentStore:
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            parts = Path(ref.disk_path).parts
        except Exception:
            parts = ()
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "attachments":
            project_id = str(parts[1] or project_id).strip() or project_id
        return AttachmentStore(self.opc_home, project_id)

    def _prepare_external_attachment_context(self, task: Task) -> str:
        """Stage uploaded files into the external agent workspace and render paths.

        Native agents can pass attachment refs directly to the LLM provider.
        External CLIs only receive text prompts plus their workspace, so this
        method turns attachment refs into a concrete workspace file contract.
        """
        metadata = dict(task.metadata or {})
        existing_context = str(metadata.get("attachment_context", "") or "").strip()
        refs = self._normalize_attachment_refs(metadata.get("attachment_refs"))
        if not refs:
            return existing_context

        workspace_hint = str(
            metadata.get("_external_workspace_path")
            or metadata.get("workspace_root")
            or metadata.get("target_output_dir")
            or ""
        ).strip()
        try:
            workspace = Path(workspace_hint).expanduser().resolve() if workspace_hint else Path(self._resolve_external_workspace(task))
            workspace.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to prepare external attachment workspace: {exc}")
            return existing_context

        turn_token = self._safe_external_attachment_token(
            metadata.get("runtime_v2_current_turn_id")
            or metadata.get("current_turn_id")
            or metadata.get("conversation_turn_id")
            or task.id
            or task.session_id,
            default="turn",
        )
        dest_dir = workspace / ".opc-attachments" / turn_token
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to create external attachment directory {dest_dir}: {exc}")
            return existing_context

        parts: list[str] = [
            "## Attachments",
            "Uploaded files have been staged inside the external agent workspace. "
            "Use the Agent path below when reading them; the Store path is OpenOPC's canonical copy.",
        ]
        mounted_refs: list[dict[str, Any]] = []
        remaining_budget = 5000
        hidden_count = 0

        for index, ref_dict in enumerate(refs, start=1):
            if index > 6:
                hidden_count += 1
                continue
            try:
                ref = AttachmentRef.from_dict(ref_dict)
            except Exception as exc:
                logger.warning(f"Failed to parse attachment ref for external context: {exc}")
                continue

            store = self._attachment_store_for_ref(task, ref)
            source_path: Path | None = None
            staged_path: Path | None = None
            filename = self._safe_external_attachment_filename(ref.filename)
            name_prefix = self._safe_external_attachment_token(ref.attachment_id, default=f"att-{index}")
            dest = dest_dir / f"{index:02d}-{name_prefix}-{filename}"
            copy_error = ""

            try:
                source_path = store.resolve_abs_path(ref)
                shutil.copy2(source_path, dest)
                staged_path = dest
                mounted_ref = {
                    **ref.to_dict(),
                    "agent_path": str(staged_path),
                    "workspace_relative_path": str(staged_path.relative_to(workspace)),
                    "source_disk_path": ref.disk_path,
                }
                mounted_refs.append(mounted_ref)
            except Exception as exc:
                copy_error = str(exc)
                logger.warning(f"Failed to stage attachment {ref.filename} for external agent: {exc}")

            parts.append(f"### {ref.filename}")
            parts.append(f"- MIME type: {ref.mime_type}")
            parts.append(f"- Size: {ref.size_bytes} bytes")
            if staged_path:
                parts.append(f"- Agent path: {staged_path}")
                parts.append(f"- Workspace relative path: {staged_path.relative_to(workspace)}")
            if source_path:
                parts.append(f"- Store path: {source_path}")
            elif ref.disk_path:
                parts.append(f"- Store path: {ref.disk_path}")
            if copy_error:
                parts.append(f"- Agent path unavailable: {copy_error}")

            if can_extract_text(ref.filename, ref.mime_type) and remaining_budget > 0:
                try:
                    raw = source_path.read_bytes() if source_path and source_path.is_file() else store.read_bytes(ref)
                    preview = extract_attachment_text(
                        ref.filename,
                        ref.mime_type,
                        raw,
                        max_chars=min(remaining_budget, 1800),
                    ).strip()
                except Exception as exc:
                    parts.append(f"- Inline preview unavailable: {exc}")
                    continue
                if not preview:
                    parts.append("- Inline preview: [empty file]")
                    continue
                clipped = preview[: min(remaining_budget, 1800)]
                if len(clipped) < len(preview):
                    clipped = f"{clipped}\n...[truncated]"
                parts.append("```text")
                parts.append(clipped)
                parts.append("```")
                remaining_budget -= len(clipped)
                continue

            if ref.mime_type.startswith("image/"):
                parts.append("- Note: image attachment is available by Agent path for external CLI tools that can inspect images.")
            elif ref.mime_type == "application/pdf":
                parts.append("- Note: PDF attachment is available by Agent path for external CLI tools that can inspect documents.")
            elif ref.mime_type.startswith("video/"):
                parts.append("- Note: video attachment is available by Agent path for external CLI tools that can inspect media.")
            else:
                parts.append("- Note: binary or complex document is available by Agent path.")

        if hidden_count:
            parts.append(f"- Additional attachments omitted from inline context: {hidden_count}")

        rendered = "\n".join(parts)
        task.metadata = {
            **metadata,
            "attachment_refs": refs,
            "attachment_context": rendered,
            "external_attachment_refs": mounted_refs,
            "external_attachment_dir": str(dest_dir),
        }
        return rendered

    @staticmethod
    def _mark_external_prompt_contract(task: Task) -> Task:
        task.metadata = {
            **dict(task.metadata or {}),
            "external_prompt_contract": "description_is_full_prompt",
        }
        return task

    async def _build_external_agent_task(self, task: Task) -> Task:
        if not self.context_assembler and self.memory:
            self.context_assembler = ContextAssembler(
                memory=self.memory,
                store=self.store,
                communication=self.communication,
            )
        company_mode = str(task.metadata.get("execution_mode", "") or "").strip() == "company_mode"
        resume_mode = bool(task.metadata.get("__external_resume_session"))
        external_attachment_context = self._prepare_external_attachment_context(task)
        resume_delta = ""
        resume_metadata: dict[str, Any] = {}
        if resume_mode:
            resume_delta, resume_metadata = await self._build_external_resume_feedback_delta(task)
            if resume_metadata:
                task.metadata = {
                    **dict(task.metadata or {}),
                    **resume_metadata,
                }
            if resume_delta:
                task.metadata = {
                    **dict(task.metadata or {}),
                    "suppress_company_rework_feedback_context": True,
                }
        if company_mode:
            task = await self._ensure_company_prompt_contract_for_external_task(task)
        task_brief = str(task.description or task.title or "").strip()
        if resume_mode and not company_mode:
            if not resume_delta and not external_attachment_context:
                return self._mark_external_prompt_contract(task)
            task_copy = Task(
                id=task.id,
                session_id=task.session_id,
                parent_session_id=task.parent_session_id,
                title=task.title,
                description="\n\n".join(
                    part for part in (
                        f"## Task Brief\n{task_brief}" if task_brief else "",
                        f"## Runtime Context\n{self._demote_prompt_headings(external_attachment_context)}" if external_attachment_context else "",
                        f"## External Resume Delta\n{resume_delta}" if resume_delta else "",
                    )
                    if str(part).strip()
                ),
                assigned_to=task.assigned_to,
                status=task.status,
                priority=task.priority,
                dependencies=list(task.dependencies),
                execution_lock=task.execution_lock,
                context_snapshot=dict(task.context_snapshot),
                assigned_external_agent=task.assigned_external_agent,
                created_at=task.created_at,
                deadline=task.deadline,
                result=task.result,
                parent_id=task.parent_id,
                project_id=task.project_id,
                tags=list(task.tags),
                comments=list(task.comments),
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                metadata=dict(task.metadata),
            )
            return self._mark_external_prompt_contract(task_copy)
        role_id = task.assigned_to or task.metadata.get("work_item_role_id", "")
        layers = ExternalContextLayers()
        if self.context_assembler and hasattr(self.context_assembler, "build_external_context_layers"):
            layers = await self.context_assembler.build_external_context_layers(task, role_id=role_id)
        elif self.context_assembler and hasattr(self.context_assembler, "build_external_context"):
            legacy_ctx = await self.context_assembler.build_external_context(task, role_id=role_id)
            if company_mode:
                layers = ExternalContextLayers(company_runtime_context=legacy_ctx)
            else:
                layers = ExternalContextLayers(openopc_context=legacy_ctx)
        if external_attachment_context and external_attachment_context not in str(layers.attachments_state_context or ""):
            layers.attachments_state_context = "\n\n".join(
                part
                for part in (layers.attachments_state_context, external_attachment_context)
                if str(part or "").strip()
            )
        runtime_tool_hints = "" if (company_mode and is_report_prompt_turn(task.metadata)) else self._build_external_runtime_tool_hints(task, role_id=role_id)
        if self.skills:
            execution_mode = str(task.metadata.get("execution_mode", "") or "").strip() or None
            skills_summary = str(
                self.skills.build_skills_summary(
                    task.project_id,
                    execution_mode=execution_mode,
                    role_id=str(role_id or ""),
                    user_facing=_memory_skill_user_facing(task, str(role_id or "")),
                    final_decider_role_id=_final_decider_role_id(task),
                )
                or ""
            ).strip()
            memory_paths_context = self._build_external_memory_paths_context(
                task,
                role_id=str(role_id or ""),
                execution_mode=execution_mode,
            )
            if skills_summary:
                if company_mode:
                    layers.company_runtime_context = "\n\n".join(
                        part for part in (skills_summary, memory_paths_context, layers.company_runtime_context)
                        if str(part).strip()
                    )
                else:
                    layers.openopc_context = "\n\n".join(
                        part for part in (skills_summary, memory_paths_context, layers.openopc_context)
                        if str(part).strip()
                    )
        from opc.layer3_agent.company_runtime_contract import build_external_company_work_item_contract

        contract_text = ""
        if company_mode:
            contract_text = build_external_company_work_item_contract(task)
        has_layer_context = any(
            str(value or "").strip()
            for value in (
                layers.openopc_context,
                layers.attachments_state_context,
                layers.company_runtime_context,
                layers.prepared_mailbox_context,
                layers.recovery_context,
            )
        )
        if not task_brief and not has_layer_context and not contract_text and not runtime_tool_hints and not resume_delta:
            return self._mark_external_prompt_contract(task)
        rendered_task_brief = task_brief
        if company_mode:
            rendered_task_brief = str(layers.primary_task_brief or "").strip()
        description_parts: list[str] = []
        if contract_text:
            description_parts.append(f"## Runtime Contract (MANDATORY)\n{contract_text}")
        if layers.recovery_context:
            recovery_delta = self._demote_prompt_headings(layers.recovery_context)
            description_parts.append(f"## Recovery Delta (MANDATORY)\n{recovery_delta}")
        if rendered_task_brief:
            description_parts.append(f"## Task Brief\n{rendered_task_brief}")
        if company_mode:
            if layers.company_runtime_context:
                description_parts.append(f"## Company Runtime Context\n{layers.company_runtime_context}")
            collaboration_context = self._build_external_collaboration_context(
                layers.prepared_mailbox_context,
                runtime_tool_hints,
            )
            if collaboration_context:
                description_parts.append(f"## Collaboration Context\n{collaboration_context}")
        else:
            if layers.openopc_context:
                description_parts.append(f"## OpenOPC Context\n{layers.openopc_context}")
        if layers.attachments_state_context:
            runtime_context = self._demote_prompt_headings(layers.attachments_state_context)
            description_parts.append(f"## Runtime Context\n{runtime_context}")
        if resume_delta:
            description_parts.append(f"## External Resume Delta\n{resume_delta}")
        if runtime_tool_hints and not company_mode:
            description_parts.append(runtime_tool_hints)
        task_copy = Task(
            id=task.id,
            session_id=task.session_id,
            parent_session_id=task.parent_session_id,
            title=task.title,
            description="\n\n".join(part for part in description_parts if str(part).strip()),
            assigned_to=task.assigned_to,
            status=task.status,
            priority=task.priority,
            dependencies=list(task.dependencies),
            execution_lock=task.execution_lock,
            context_snapshot=dict(task.context_snapshot),
            assigned_external_agent=task.assigned_external_agent,
            created_at=task.created_at,
            deadline=task.deadline,
            result=task.result,
            parent_id=task.parent_id,
            project_id=task.project_id,
            tags=list(task.tags),
            comments=list(task.comments),
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            metadata=dict(task.metadata),
        )
        return self._mark_external_prompt_contract(task_copy)

    def _build_external_memory_paths_context(
        self,
        task: Task,
        *,
        role_id: str = "",
        execution_mode: str | None = None,
    ) -> str:
        current_mode = str(execution_mode or task.metadata.get("execution_mode", "") or "").strip()
        include = current_mode == "task_mode"
        if current_mode == "company_mode":
            include = _memory_skill_user_facing(task, role_id) and str(role_id or "").strip() == _final_decider_role_id(task)
        if not include:
            return ""

        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        markdown_store = self.memory.markdown_store if self.memory else MemoryManager(self.opc_home, project_id).markdown_store
        global_path = markdown_store.ensure_memory_file(None, heading="# Global Memory")
        project_path = markdown_store.ensure_memory_file(project_id, heading=f"# Project Memory ({project_id})")
        return (
            "## Memory Paths (Canonical)\n"
            f"- OPC_MEMORY_ROOT={markdown_store.global_memory_dir}\n"
            f"- OPC_GLOBAL_MEMORY_PATH={global_path}\n"
            f"- OPC_PROJECT_MEMORY_PATH={project_path}\n"
            "- Use these absolute paths for durable memory. Do not create a separate `.opc/memory` under the workplace."
        )

    @staticmethod
    def _demote_prompt_headings(text: str, *, target_level: int = 3) -> str:
        """Nest rendered prompt fragments under an outer section title."""
        target_level = max(int(target_level or 3), 1)
        prefix = "#" * target_level
        lines: list[str] = []
        for raw_line in str(text or "").strip().splitlines():
            if raw_line.startswith("## ") and not raw_line.startswith(prefix + " "):
                lines.append(f"{prefix} {raw_line[3:].strip()}")
            else:
                lines.append(raw_line)
        return "\n".join(lines).strip()

    def _build_external_collaboration_context(self, *parts: str) -> str:
        """Combine mailbox and collaboration-tool hints without extra H2 noise."""
        nested_parts = [
            self._demote_prompt_headings(part)
            for part in parts
            if str(part or "").strip()
        ]
        return "\n\n".join(part for part in nested_parts if part)

    async def _build_external_resume_feedback_delta(
        self,
        task: Task,
    ) -> tuple[str, dict[str, Any]]:
        feedback_metadata = dict(task.metadata or {})
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id and self.store and hasattr(self.store, "get_delegation_work_item"):
            try:
                work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                work_item = None
            if work_item is not None:
                feedback_metadata = {
                    **feedback_metadata,
                    **dict(getattr(work_item, "metadata", {}) or {}),
                }
        feedback = str(feedback_metadata.get("rework_feedback", "") or "").strip()
        if not feedback:
            return "", {}

        current_version = self._review_feedback_version_from_metadata(feedback_metadata)
        last_version = self._review_feedback_version_from_metadata(
            task.metadata or {},
            key="external_resume_review_feedback_version",
        )
        digest = hashlib.sha1(feedback.encode("utf-8")).hexdigest()
        last_digest = str((task.metadata or {}).get("external_resume_review_feedback_digest", "") or "").strip()
        if digest == last_digest and current_version <= last_version:
            return "", {
                "external_resume_review_feedback_version": current_version,
                "external_resume_review_feedback_digest": digest,
            }

        rendered = ""
        if self.context_assembler is not None:
            try:
                rendered = await self.context_assembler.build_rework_feedback_context(task)
            except Exception:
                rendered = ""
        if not rendered:
            rendered = self._fallback_external_resume_feedback_context(feedback_metadata, feedback)

        header = [
            "## Reviewer Delta (MANDATORY NEW CONTEXT)",
            "You are resuming the same external session.",
            "The reviewer feedback below is new and overrides stale assumptions from earlier in the thread.",
        ]
        if current_version > 0:
            header.append(f"review_feedback_version: {current_version}")
        delta = "\n".join(header).strip()
        if rendered.strip():
            delta = f"{delta}\n\n{rendered.strip()}"
        return delta, {
            "external_resume_review_feedback_version": current_version,
            "external_resume_review_feedback_digest": digest,
        }

    @staticmethod
    def _review_feedback_version_from_metadata(
        metadata: dict[str, Any] | None,
        *,
        key: str = "review_feedback_version",
    ) -> int:
        payload = dict(metadata or {})
        try:
            parsed = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
        if key != "review_feedback_version":
            return 0
        try:
            fallback = int(payload.get("review_rework_count") or 0)
        except (TypeError, ValueError):
            fallback = 0
        return max(fallback, 0)

    @staticmethod
    def _fallback_external_resume_feedback_context(
        metadata: dict[str, Any],
        feedback: str,
    ) -> str:
        reviewer_role = str(metadata.get("review_owner_role_id", "") or "").strip()
        verdict = dict(metadata.get("structured_review_verdict", {}) or {})
        rework_count = OPCEngine._review_feedback_version_from_metadata(metadata)
        lines: list[str] = [
            "## Reviewer Feedback (Rework Required)",
            "",
            "Your previous attempt was rejected. Address the points below before continuing.",
            "",
        ]
        if reviewer_role:
            lines.append(f"Reviewer: {reviewer_role}")
        if rework_count > 0:
            lines.append(f"Rework attempt: #{rework_count}")
        if reviewer_role or rework_count > 0:
            lines.append("")
        lines.append("### Reviewer's Reject Reason")
        lines.append(feedback)
        blocking = [
            str(item).strip()
            for item in list(verdict.get("blocking_issues", []) or [])
            if str(item).strip()
        ]
        followups = [
            str(item).strip()
            for item in list(verdict.get("followups", []) or [])
            if str(item).strip()
        ]
        if blocking:
            lines.append("")
            lines.append("### Blocking Issues")
            lines.extend(f"- {item}" for item in blocking[:12])
        if followups:
            lines.append("")
            lines.append("### Follow-ups")
            lines.extend(f"- {item}" for item in followups[:12])
        return "\n".join(lines).rstrip()

    def _build_external_runtime_tool_hints(self, task: Task, *, role_id: str = "") -> str:
        """Render external collaboration instructions for company-mode runs.

        OpenOPC-spawned agents talk to the rest of the company through the
        ``opc-collab`` CLI — installed on their PATH and also described by
        a skill under their isolated agent home. The CLI is the only
        transport taught here. Staying focused on the CLI keeps external
        agents on the supported collaboration path.
        """
        if not company_collaboration_enabled_for_task(task):
            return ""

        active_role = str(role_id or task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        runtime_state = {
            "manager_board_summary": dict(task.context_snapshot.get("manager_board_summary", {}) or {}),
        }
        role_cfg = None
        if self.org_engine is not None and active_role:
            try:
                role_cfg = self.org_engine.get_agent(active_role)
            except Exception:
                role_cfg = None

        _profile, allowed_tools = resolve_task_collaboration_tools(
            task,
            role=active_role,
            seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
            runtime_state=runtime_state,
            role_cfg=role_cfg,
        )
        if not allowed_tools:
            return ""

        current_work_item_id = linked_work_item_id_for_task(task)
        runtime_task_id = str(task.id or "").strip()
        primary_tools = self._primary_external_collaboration_tools(task, allowed_tools)
        lines = [
            "### Collaboration Tools",
            "Use the executable in `OPC_COLLAB_CLI` when it is set; otherwise use `opc-collab` from `PATH`.",
            "Call tools as `opc-collab <tool> --args-stdin` or `opc-collab <tool> --args-json-file <file>` with a JSON object.",
            "Avoid inline single-quoted JSON; `--args-stdin` and `--args-json-file` work consistently on Linux, macOS, and Windows.",
            "On Windows external-agent runs, do not use `--args-json` or pipe JSON into `--args-stdin`; command-line and PowerShell pipeline text can corrupt non-ASCII before it reaches `opc-collab`.",
            "For Windows collaboration calls, write the JSON object to a UTF-8 file and call `opc-collab <tool> --args-json-file <file>`.",
            "PowerShell-safe UTF-8 file write: `$enc = New-Object System.Text.UTF8Encoding $false; [System.IO.File]::WriteAllText($path, $json, $enc)`.",
            "",
            "Identity:",
            "- WorkItem ID is the collaboration identity; `$OPC_WORK_ITEM_ID` is already set for this run.",
            "- Runtime Task IDs are only execution/session carriers; never use `$OPC_TASK_ID` or `$OPC_RUNTIME_TASK_ID` as WorkItem IDs.",
        ]
        if current_work_item_id:
            lines.append("- Omit current-card IDs when a tool can infer them from `$OPC_WORK_ITEM_ID`.")
        if runtime_task_id:
            lines.append("- `$OPC_RUNTIME_TASK_ID` may appear in diagnostics only; do not copy it into collaboration arguments.")
        lines.extend([
            "",
            "Allowed tools this turn:",
        ])
        for logical_name in sorted(allowed_tools):
            lines.append(f"- `{logical_name}`")
        if primary_tools:
            contract_lines = build_external_cli_tool_contract_lines(primary_tools)
            if contract_lines:
                contract_lines[0] = "Primary argument contracts:"
                lines.append("")
                lines.extend(contract_lines)
        return "\n".join(lines)

    @staticmethod
    def _primary_external_collaboration_tools(task: Task, allowed_tools: set[str]) -> set[str]:
        """Return the tool schemas worth expanding for this specific turn."""
        allowed = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}
        turn_mode = resolve_company_turn_mode(task)
        if turn_mode == "dispatch_required":
            preferred = {"delegate_work", "modify_work_item", "delete_work_item", "manager_board_read", "inbox"}
        elif turn_mode == "review_execute":
            preferred = {"manager_board_read"}
        elif turn_mode in {"monitor_children", "synthesize_required", "deliver_required"}:
            preferred = {"manager_board_read", "modify_work_item", "delete_work_item", "inbox", "send_dm", "broadcast_issue"}
        else:
            preferred = {"inbox", "reply_message", "send_dm", "ask_peer_and_wait", "respond_meeting"}
        return allowed.intersection(preferred)

    # Checkpoint types that represent "a task is parked waiting for user input".
    # Invariant: a pending row of these types is only valid while its task is
    # actually in a waiting status; every other path must terminate them.
    _TASK_WAIT_CHECKPOINT_TYPES = ("task_user_input", "task_peer_wait")

    async def _supersede_stale_task_wait_checkpoints(self, task_id: str, *, reason: str) -> None:
        """Terminate pending task-wait checkpoints once their task moves on.

        The company runtime can carry a paused work item forward through its own
        machinery (approval-card grants, a fresh review attempt) without ever
        replying through the engine checkpoint. If the checkpoint row stays
        pending it will capture the user's next unrelated chat message and route
        it into a resume of a task that is no longer waiting.
        """
        if not task_id or not self.store:
            return
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if not callable(supersede):
            return
        try:
            superseded = await supersede(
                project_id=self.project_id or "default",
                task_id=task_id,
                checkpoint_types=list(self._TASK_WAIT_CHECKPOINT_TYPES),
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to supersede stale task-wait checkpoints for task {task_id}"
            )
            return
        if superseded:
            logger.info(
                f"Superseded {len(superseded)} stale task-wait checkpoint(s) for task {task_id} ({reason})"
            )

    @staticmethod
    def _checkpoint_awaits_approval_decision(checkpoint: ExecutionCheckpoint) -> bool:
        """Whether a parked task-wait checkpoint is waiting on a permission decision.

        Approval escalations park the task with the pending permission request
        recorded under ``payload.runtime_v2.permission_requests``. Those prompts
        are decided through their approval card, whose reply always targets the
        checkpoint explicitly; free-form chat text is never the decision.
        """
        if str(checkpoint.checkpoint_type or "").strip() != "task_user_input":
            return False
        payload = dict(checkpoint.payload or {})
        runtime_state = payload.get("runtime_v2")
        if not isinstance(runtime_state, dict):
            return False
        requests = runtime_state.get("permission_requests")
        return isinstance(requests, list) and len(requests) > 0

    async def _checkpoint_task_still_waiting(self, checkpoint: ExecutionCheckpoint) -> bool:
        """Whether a task-wait checkpoint still matches a genuinely waiting task.

        Lazily resolves orphaned rows (task finished, failed, superseded by a
        new review attempt, or deleted) as ``stale`` so historical dirty data
        self-heals the first time it is considered for a resume. Non-task-wait
        checkpoint types are always considered live here.
        """
        if str(checkpoint.checkpoint_type or "").strip() not in self._TASK_WAIT_CHECKPOINT_TYPES:
            return True
        task_id = str(
            checkpoint.task_id or dict(checkpoint.payload or {}).get("task_id") or ""
        ).strip()
        if not task_id or not self.store:
            return True
        try:
            task = await self.store.get_task(task_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Could not verify task {task_id} for checkpoint {checkpoint.checkpoint_id}; keeping it"
            )
            return True
        if task is None:
            stale_reason = f"task {task_id} no longer exists"
        elif task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            stale_reason = f"task {task_id} settled as {task.status.value}"
        else:
            # A non-terminal task status proves nothing on its own:
            # suspend/restart flows legitimately park a waiting task back at
            # PENDING or RUNNING. The linked delegation work item is the
            # authoritative signal — once its phase is terminal (a later review
            # attempt or the manager closed it) or the item is gone, no runtime
            # will ever come back to consume this checkpoint.
            stale_reason = await self._task_work_item_closed_reason(task)
        if not stale_reason:
            return True
        try:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="stale")
            logger.info(
                f"Resolved stale {checkpoint.checkpoint_type} checkpoint "
                f"{checkpoint.checkpoint_id} ({stale_reason})"
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to resolve stale checkpoint {checkpoint.checkpoint_id}"
            )
        return False

    async def _task_work_item_closed_reason(self, task: Task) -> str:
        """Non-empty reason when the task's delegation work item is closed.

        Returns "" when the task has no linked work item, the item cannot be
        loaded, or the item is still in a live phase — i.e. keep the checkpoint.
        """
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or not self.store:
            return ""
        getter = getattr(self.store, "get_delegation_work_item", None)
        if not callable(getter):
            return ""
        try:
            work_item = await getter(work_item_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Could not load work item {work_item_id} while validating a checkpoint; keeping it"
            )
            return ""
        if work_item is None:
            return f"work item {work_item_id} no longer exists"
        phase_raw = getattr(work_item, "phase", "")
        phase = str(getattr(phase_raw, "value", phase_raw) or "").strip()
        if phase in {Phase.APPROVED.value, Phase.FAILED.value, Phase.CANCELLED.value}:
            return f"work item {work_item_id} closed with phase={phase}"
        return ""

    async def _save_execution_checkpoint(self, data: dict[str, Any]) -> None:
        assert self.store
        payload = dict(data.get("payload", {}))
        if not str(payload.get("basis_hash", "") or "").strip():
            payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        checkpoint = ExecutionCheckpoint(
            project_id=data.get("project_id", self.project_id or "default"),
            session_id=data.get("session_id"),
            checkpoint_type=data.get("checkpoint_type", "generic"),
            task_id=data.get("task_id"),
            payload=payload,
        )
        await self.store.save_execution_checkpoint(checkpoint)
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if callable(supersede) and checkpoint.task_id:
            superseded_ids = await supersede(
                project_id=checkpoint.project_id,
                task_id=checkpoint.task_id,
                checkpoint_types=[checkpoint.checkpoint_type],
                basis_hash=str(payload.get("basis_hash", "") or "").strip() or None,
                exclude_checkpoint_id=checkpoint.checkpoint_id,
            )
            if superseded_ids:
                checkpoint.payload = dict(checkpoint.payload or {})
                checkpoint.payload["superseded_checkpoint_ids"] = superseded_ids
                checkpoint.updated_at = datetime.now()
                await self.store.save_execution_checkpoint(checkpoint)
        payload = dict(checkpoint.payload or {})
        runtime_session_id = str(payload.get("runtime_session_id", "") or "").strip()
        if runtime_session_id:
            await self.event_bus.publish(OPCEvent(
                event_type="runtime_event",
                payload={
                    "type": "checkpoint_saved",
                    "timestamp_ms": int(time.time() * 1000),
                    "runtime_session_id": runtime_session_id,
                    "task_id": checkpoint.task_id,
                    "session_id": checkpoint.session_id,
                    "checkpoint_type": checkpoint.checkpoint_type,
                    "execution_mode": payload.get("execution_mode", ""),
                    "review_level": payload.get("review_level", ""),
                    "review_target_role_id": payload.get("review_target_role_id", ""),
                    **work_item_identity_payload_from_metadata(
                        payload,
                        projection_id_fallback=str(payload.get("work_item_projection_id", "") or ""),
                        turn_type_fallback=str(payload.get("work_item_turn_type", "") or ""),
                    ),
                    "work_item_projection_title": payload.get("work_item_projection_title", ""),
                },
            ))

    @staticmethod
    def _checkpoint_basis_hash(payload: dict[str, Any]) -> str:
        basis = {
            "task_id": str(payload.get("task_id", "") or payload.get("waiting_task_id", "") or "").strip(),
            **work_item_identity_payload_from_metadata(
                payload,
                projection_id_fallback=str(payload.get("work_item_projection_id", "") or ""),
                turn_type_fallback=str(payload.get("work_item_turn_type", "") or ""),
            ),
            "delivery_revision": str(payload.get("delivery_revision", "") or "").strip(),
            "owner_directive_revision": str(payload.get("owner_directive_revision", "") or "").strip(),
            "latest_user_directive": str(payload.get("latest_user_directive", "") or "").strip(),
            "prompt": str(payload.get("prompt", "") or "").strip(),
            "result_content": str(payload.get("result_content", "") or "").strip(),
            "work_item_summary": str(payload.get("work_item_summary", "") or "").strip(),
            "work_item_summary_for_downstream": str(payload.get("work_item_summary_for_downstream", "") or "").strip(),
            "artifact_index": payload.get("work_item_artifact_index", []),
            "artifact_manifest": payload.get("artifact_manifest", []),
            "verification_status": payload.get("verification_status", {}),
            "verification_evidence": payload.get("verification_evidence", {}),
            "verification_verdict": str(payload.get("verification_verdict", "") or "").strip(),
            "delivery_package": payload.get("delivery_package", {}),
        }
        encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    async def _save_routing_checkpoint(
        self,
        checkpoint_type: str,
        original_message: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> None:
        await self._save_execution_checkpoint(
            {
                "project_id": self.project_id or "default",
                "session_id": session_id,
                "checkpoint_type": checkpoint_type,
                "payload": {
                    "original_message": original_message,
                    **payload,
                },
            }
        )

    def _checkpoint_execution_mode_for_task(self, task: Task) -> str:
        """Execution mode to record on a pause checkpoint.

        Work-item runtime membership is the durable signal; the
        ``execution_mode`` metadata field is volatile and has been observed to
        degrade to ``task_mode`` after a resume, which then misroutes the next
        resume away from the company state machine.
        """
        if is_work_item_runtime_metadata(task.metadata):
            return ExecutionMode.COMPANY_MODE.value
        return str(task.metadata.get("execution_mode", ExecutionMode.SINGLE_AGENT.value))

    async def _save_task_pause_checkpoint(self, task: Task, result: TaskResult) -> None:
        pause_request = dict(result.artifacts.get("pause_request", {})) if result.artifacts else {}
        runtime_payload = self._build_runtime_checkpoint_payload(task, result)
        review_level = str(
            pause_request.get("review_level")
            or ("manager" if result.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human")
        ).strip().lower()
        review_target_role_id = str(
            pause_request.get("review_target_role_id")
            or (task.metadata.get("manager_role_id", "") if review_level == "manager" else "")
            or ""
        ).strip()
        review_chain_role_ids = [
            str(item).strip()
            for item in list(
                pause_request.get("review_chain_role_ids")
                or ([review_target_role_id] if review_target_role_id else [])
            )
            if str(item).strip()
        ]
        pending_reorg_id = str(task.metadata.get("pending_reorg_proposal_id", "") or "").strip()
        if pending_reorg_id:
            await self._save_execution_checkpoint(
                {
                    "project_id": task.project_id,
                    "session_id": task.session_id,
                    "checkpoint_type": "company_reorg_pending",
                    "task_id": task.id,
                    "payload": {
                        "proposal_id": pending_reorg_id,
                        "waiting_task_id": task.id,
                        "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                        "parent_session_id": task.parent_session_id or task.metadata.get("parent_session_id"),
                        "org_version": task.metadata.get("org_version", 1),
                        "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                        "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                        "review_level": review_level,
                        "review_target_role_id": review_target_role_id,
                        "review_chain_role_ids": review_chain_role_ids,
                        **runtime_payload,
                    },
                }
            )
            return
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "task_user_input",
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": self._checkpoint_execution_mode_for_task(task),
                    "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                    "prompt": result.content,
                    "pause_request": pause_request,
                    "review_level": review_level,
                    "review_target_role_id": review_target_role_id,
                    "review_chain_role_ids": review_chain_role_ids,
                    **runtime_payload,
                },
            }
        )

    async def _save_peer_pause_checkpoint(self, task: Task, result: TaskResult) -> None:
        peer_wait = dict(task.metadata.get("peer_wait", {}))
        runtime_payload = self._build_runtime_checkpoint_payload(task, result)
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "task_peer_wait",
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": self._checkpoint_execution_mode_for_task(task),
                    "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                    "peer_wait": peer_wait,
                    "result_content": result.content,
                    **runtime_payload,
                },
            }
        )

    async def get_latest_pending_checkpoint_for_session(
        self,
        session_id: str | None = None,
    ) -> ExecutionCheckpoint | None:
        if not self.store:
            return None
        project_id = self.project_id or "default"
        requested_session_id = str(session_id or "").strip()
        # Fast path: with no live checkpoint rows in the project there is
        # nothing to surface, so skip the parent-session resolution below.
        # Snapshot builders call this once per task on every UI sync tick, and
        # that resolution loads (and JSON-parses) task rows each time.
        checkpoint_probe = getattr(self.store, "get_execution_checkpoints", None)
        if callable(checkpoint_probe):
            try:
                live_checkpoints = await checkpoint_probe(
                    project_id=project_id,
                    statuses=["pending", "resuming"],
                )
            except Exception:
                live_checkpoints = None
            if live_checkpoints is not None and len(live_checkpoints) == 0:
                return None
        company_parent_session_id = await self._company_runtime_parent_session_for_session_id(
            requested_session_id,
        )
        runtime_session_id = company_parent_session_id or requested_session_id
        active_suspend_checkpoint: ExecutionCheckpoint | None = None
        if runtime_session_id:
            active_suspend_checkpoint = await self.get_active_company_runtime_suspend_checkpoint(runtime_session_id)
        if (
            active_suspend_checkpoint is not None
            and company_parent_session_id
            and company_parent_session_id != requested_session_id
        ):
            return await self._ensure_checkpoint_runtime_v2_payload(active_suspend_checkpoint)
        checkpoint = await self.store.get_latest_pending_checkpoint(
            project_id,
            session_id=requested_session_id or None,
        )
        # Skip (and lazily resolve) orphaned task-wait checkpoints; each stale
        # row is marked resolved before re-querying, so this terminates.
        while checkpoint is not None and not await self._checkpoint_task_still_waiting(checkpoint):
            checkpoint = await self.store.get_latest_pending_checkpoint(
                project_id,
                session_id=requested_session_id or None,
            )
        deferred_suspend_checkpoint: ExecutionCheckpoint | None = None
        if checkpoint and self._checkpoint_is_user_visible(checkpoint):
            if not self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type):
                return await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
            deferred_suspend_checkpoint = checkpoint
        if not requested_session_id:
            return await self._ensure_checkpoint_runtime_v2_payload(deferred_suspend_checkpoint) if deferred_suspend_checkpoint else None

        # Company-mode gates are persisted on child work-item sessions. When the
        # user comes back through the primary session, surface the newest child
        # checkpoint so a plain "continue" resumes the runtime correctly.
        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
            return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None
        _, tasks = snapshot
        visible_session_ids = {
            str(getattr(task, "session_id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "session_id", "") or "").strip()
        }
        visible_task_ids = {
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        if not visible_session_ids and not visible_task_ids:
            selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
            return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None

        checkpoints = await self.store.get_pending_checkpoints(project_id=project_id)
        for pending in checkpoints:
            if not self._checkpoint_is_user_visible(pending):
                continue
            if not await self._checkpoint_task_still_waiting(pending):
                continue
            if self._is_company_runtime_suspend_checkpoint(pending.checkpoint_type):
                if deferred_suspend_checkpoint is None and str(pending.session_id or "").strip() == session_id:
                    deferred_suspend_checkpoint = pending
                continue
            pending_session_id = str(pending.session_id or "").strip()
            pending_task_id = str(
                pending.task_id
                or pending.payload.get("waiting_task_id")
                or pending.payload.get("task_id")
                or ""
            ).strip()
            if pending_session_id in visible_session_ids or pending_task_id in visible_task_ids:
                return await self._ensure_checkpoint_runtime_v2_payload(pending)
        selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
        return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None

    async def _company_runtime_parent_session_for_session_id(self, session_id: str | None) -> str:
        """Resolve any durable task session to its company runtime session."""
        if not self.store:
            return ""
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        try:
            get_by_session = getattr(self.store, "get_tasks_by_session_id", None)
            if callable(get_by_session):
                tasks = await get_by_session(
                    sid,
                    project_id=self.project_id or "default",
                )
                identity_index = build_company_runtime_identity_index(tasks)
            else:
                identity_index = await load_company_runtime_identity_index(
                    self.store,
                    self.project_id or "default",
                )
            identity = identity_index.resolve(task_session_id=sid)
        except Exception:
            logger.opt(exception=True).debug(
                "failed to resolve company runtime session identity"
            )
            return ""
        return str(getattr(identity, "runtime_session_id", "") or "").strip()

    @staticmethod
    def _checkpoint_is_user_visible(checkpoint: ExecutionCheckpoint) -> bool:
        payload = dict(checkpoint.payload or {})
        review_level = str(payload.get("review_level", "") or "").strip().lower()
        return review_level != "manager"

    @classmethod
    def _checkpoint_is_company_scoped(cls, checkpoint_type: str | None) -> bool:
        normalized = str(checkpoint_type or "").strip()
        return (
            normalized.startswith("company_")
            or normalized == "company_peer_wait"
            or cls._is_company_runtime_suspend_checkpoint(normalized)
        )

    @staticmethod
    def _reply_metadata_targets_checkpoint(
        reply_metadata: dict[str, Any] | None,
        checkpoint: ExecutionCheckpoint,
    ) -> bool:
        metadata = dict(reply_metadata or {})
        explicit_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        explicit_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        if not explicit_id and not explicit_type:
            return False
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
        if explicit_id and explicit_id != checkpoint_id:
            return False
        if explicit_type and explicit_type != checkpoint_type:
            return False
        return True

    @staticmethod
    def _explicit_checkpoint_reply(
        reply_metadata: dict[str, Any] | None,
    ) -> tuple[str, str]:
        metadata = dict(reply_metadata or {})
        return (
            str(metadata.get("response_to_checkpoint_id", "") or "").strip(),
            str(metadata.get("response_to_checkpoint_type", "") or "").strip(),
        )

    async def _load_execution_checkpoint_by_id(
        self,
        checkpoint_id: str,
    ) -> ExecutionCheckpoint | None:
        checkpoint_id = str(checkpoint_id or "").strip()
        if not checkpoint_id or not self.store:
            return None
        direct_getter = getattr(self.store, "get_execution_checkpoint", None)
        if callable(direct_getter):
            try:
                checkpoint = await direct_getter(checkpoint_id)
                if checkpoint is not None:
                    return checkpoint
            except TypeError:
                try:
                    checkpoint = await direct_getter(
                        checkpoint_id,
                        project_id=self.project_id or "default",
                    )
                    if checkpoint is not None:
                        return checkpoint
                except Exception:
                    logger.opt(exception=True).debug("direct checkpoint lookup failed")
            except Exception:
                logger.opt(exception=True).debug("direct checkpoint lookup failed")

        listing_getter = getattr(self.store, "get_execution_checkpoints", None)
        if not callable(listing_getter):
            return None
        try:
            checkpoints = await listing_getter(project_id=self.project_id or "default")
        except TypeError:
            checkpoints = await listing_getter(self.project_id or "default")
        for checkpoint in checkpoints:
            if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() == checkpoint_id:
                return checkpoint
        return None

    async def _checkpoint_visible_to_reply_session(
        self,
        checkpoint: ExecutionCheckpoint,
        session_id: str | None,
    ) -> bool:
        requested_session_id = str(session_id or "").strip()
        if not requested_session_id:
            return False

        checkpoint_session_id = str(getattr(checkpoint, "session_id", "") or "").strip()
        checkpoint_task_id = str(
            getattr(checkpoint, "task_id", "")
            or dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
            or dict(getattr(checkpoint, "payload", {}) or {}).get("task_id")
            or ""
        ).strip()
        if checkpoint_session_id == requested_session_id:
            return True

        runtime_session_id = await self._company_runtime_parent_session_for_session_id(
            requested_session_id,
        )
        runtime_session_id = runtime_session_id or requested_session_id
        if checkpoint_session_id == runtime_session_id:
            return True

        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            return False
        _, tasks = snapshot
        visible_session_ids = {
            str(getattr(task, "session_id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "session_id", "") or "").strip()
        }
        visible_task_ids = {
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        return (
            bool(checkpoint_session_id and checkpoint_session_id in visible_session_ids)
            or bool(checkpoint_task_id and checkpoint_task_id in visible_task_ids)
        )

    async def _maybe_resume_checkpoint(
        self,
        user_reply: str,
        session_id: str | None = None,
        reply_metadata: dict[str, Any] | None = None,
        requested_mode: str | None = None,
    ) -> str | None:
        explicit_checkpoint_id, explicit_checkpoint_type = self._explicit_checkpoint_reply(reply_metadata)
        if explicit_checkpoint_id:
            checkpoint = await self._load_execution_checkpoint_by_id(explicit_checkpoint_id)
            if not checkpoint:
                return "This request is no longer active."
            if explicit_checkpoint_type and explicit_checkpoint_type != str(checkpoint.checkpoint_type or "").strip():
                return "This request is no longer active or does not match the selected checkpoint."
            if not await self._checkpoint_visible_to_reply_session(checkpoint, session_id):
                return "This request is no longer active."
            if str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending":
                if str(getattr(checkpoint, "checkpoint_type", "") or "").strip() == "company_delivery_feedback":
                    return None
                return "This request is no longer active."
            if not await self._checkpoint_task_still_waiting(checkpoint):
                return "This request is no longer active."
        else:
            checkpoint = await self.get_latest_pending_checkpoint_for_session(session_id)
            if not checkpoint:
                return None
            if self._checkpoint_awaits_approval_decision(checkpoint):
                # A parked permission prompt is answered by its approval card
                # (the card reply carries an explicit response_to_checkpoint_id).
                # Deferred cards stay pending indefinitely, so a plain chat
                # message must not be consumed as the approval answer — let it
                # continue as a normal conversation turn instead.
                return None
        metadata_mode = str(dict(reply_metadata or {}).get("mode", "") or "").strip()
        inferred_mode = requested_mode or metadata_mode
        if not inferred_mode and self._checkpoint_is_company_scoped(checkpoint.checkpoint_type):
            inferred_mode = "company"
        normalized_requested_mode = self._normalize_requested_mode(inferred_mode or "task")
        if (
            normalized_requested_mode != "company"
            and self._checkpoint_is_company_scoped(checkpoint.checkpoint_type)
            and not self._reply_metadata_targets_checkpoint(reply_metadata, checkpoint)
            and not self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type)
        ):
            return None
        if checkpoint.checkpoint_type in {"route_clarification", "company_runtime_selection"}:
            return await self._resume_routing_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "task_user_input":
            return await self._resume_task_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type in {"task_peer_wait", "company_peer_wait"}:
            return await self._resume_peer_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "company_work_item_gate":
            return await self._resume_company_runtime_checkpoint(checkpoint, user_reply)
        if self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type):
            if self._reply_metadata_requests_force_resume(reply_metadata):
                return await self._resume_company_suspend_checkpoint(checkpoint, user_reply)
            return await self._resume_company_suspend_checkpoint_via_final_decider(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "company_delivery_feedback":
            if not explicit_checkpoint_id:
                return None
            reply_kind = str(dict(reply_metadata or {}).get("checkpoint_reply_kind", "") or "").strip().lower()
            if not str(user_reply or "").strip() and reply_kind not in {"approve", "feedback", "ignore"}:
                return "There is a pending delivery self-evolution review. Use the review card to fully agree, ignore, or send feedback."
            if reply_kind not in {"approve", "feedback", "ignore"}:
                normalized_reply = str(user_reply or "").strip().lower()
                if normalized_reply in {"ignore", "ignored", "skip"}:
                    reply_kind = "ignore"
                else:
                    reply_kind = "approve" if normalized_reply in {"approve", "approved", "i approve this delivery."} else "feedback"
            if reply_kind == "ignore":
                return await self.ignore_company_delivery_feedback_checkpoint(
                    checkpoint,
                    reply_metadata=reply_metadata,
                )
            return await self.run_company_delivery_self_evolution_checkpoint(
                checkpoint,
                action=reply_kind,
                feedback=user_reply if reply_kind == "feedback" else "",
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_staffing_selection":
            return await self._resume_staffing_selection_checkpoint(
                checkpoint,
                user_reply,
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_recruitment_confirmation":
            return await self._resume_recruitment_checkpoint(
                checkpoint,
                user_reply,
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_reorg_pending":
            return await self._resume_reorg_checkpoint(checkpoint, user_reply)
        return None

    async def _resume_routing_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        payload = checkpoint.payload
        original_message = payload.get("original_message", "")
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending request because the original message is missing."

        if checkpoint.checkpoint_type == "company_runtime_selection":
            combined = f"{original_message}\n\nCompany runtime selection: {user_reply.strip()}"
        else:
            combined = f"{original_message}\n\nAdditional information from user:\n{user_reply.strip()}"

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        message = UserMessage(
            channel="cli",
            user_id="owner",
            content=combined,
            session_id=checkpoint.session_id or payload.get("session_id") or str(uuid.uuid4()),
            project_context=self.project_id,
        )
        response = await self.message_bus.process_single(message)
        return response.content if response else "No response generated after resume."

    async def _release_work_item_human_wait(self, task: Task, *, reason: str) -> bool:
        """Push a work item parked in ``awaiting_human`` back to ``ready``.

        This is the state-machine half of resuming an answered human wait: the
        phase moves through the legal ``AWAITING_HUMAN → READY`` recovery exit
        and any stale claim is released so the company dispatcher can re-claim
        the item on its next pass. Returns True when a phase write happened.
        """
        if not self.store or not hasattr(self.store, "update_delegation_work_item"):
            return False
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            return False
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            logger.opt(exception=True).debug(
                "release_work_item_human_wait: work item load failed for task {}", task.id
            )
            return False
        if work_item is None or getattr(work_item, "phase", None) != Phase.AWAITING_HUMAN:
            return False
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        metadata_unset: list[str] = []
        if str(metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended":
            metadata_unset = ["dispatch_hold", "suspended_at", "suspend_reason", "suspended_phase"]
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.READY,
                blocked_reason="",
                metadata_updates={
                    "human_wait_released_at": datetime.now().isoformat(),
                    "human_wait_release_reason": reason,
                    "claimed_by_role_session_id": "",
                    "claimed_task_id": "",
                },
                metadata_unset=metadata_unset or None,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
        except InvalidPhaseTransition:
            logger.opt(exception=True).warning(
                "release_work_item_human_wait: phase transition rejected for work item {}",
                work_item_id,
            )
            return False
        except Exception:
            logger.opt(exception=True).warning(
                "release_work_item_human_wait: phase write failed for work item {}",
                work_item_id,
            )
            return False
        logger.info(
            "Released human wait on work item {} (task {}, reason={}): awaiting_human -> ready",
            work_item_id,
            task.id,
            reason,
        )
        return True

    async def _resume_task_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        assert self.store
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        task_id = payload.get("task_id")
        if not task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending task because the task reference is missing."

        task = await self.store.get_task(task_id)
        if not task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending task because it no longer exists."

        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["user_supplied_input"] = user_reply.strip()
        pause_request = dict(payload.get("pause_request", {}))
        if pause_request:
            task.context_snapshot["requested_user_input"] = pause_request
        self._restore_runtime_state_from_checkpoint(task, payload)
        task.status = TaskStatus.PENDING
        task.result = None
        task.metadata = dict(task.metadata)
        progress = list(task.metadata.get("progress_log", []))
        progress.append(f"Resumed with user input: {user_reply.strip()}")
        task.metadata["progress_log"] = progress
        await self.store.save_task(task)

        # Sibling ids persisted by older checkpoints can be work-item ids rather
        # than task UUIDs; unresolvable entries are skipped, but the primary
        # task must always be part of the resumed set so the resume can never
        # degenerate into executing an empty task list (which used to return an
        # empty reply and silently swallow the user's message).
        tasks: list[Task] = [task]
        for sibling_id in payload.get("task_ids", [task_id]):
            if str(sibling_id) == str(task_id):
                continue
            sibling = await self.store.get_task(sibling_id)
            if not sibling:
                logger.warning(
                    f"Checkpoint {checkpoint.checkpoint_id} references unknown sibling task {sibling_id!r}; skipping it"
                )
                continue
            if sibling.status == TaskStatus.BLOCKED:
                sibling.status = TaskStatus.PENDING
                await self.store.save_task(sibling)
            tasks.append(sibling)

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")

        raw_execution_mode = str(payload.get("execution_mode", ExecutionMode.SINGLE_AGENT.value))
        try:
            # MULTI_AGENT is a value alias of COMPANY_MODE, so normalizing to the
            # enum collapses both onto one branch instead of letting the legacy
            # multi-agent branch shadow the company-mode one.
            execution_mode = ExecutionMode(raw_execution_mode)
        except ValueError:
            execution_mode = ExecutionMode.SINGLE_AGENT
        # Work-item runtime tasks must resume through the delegation state
        # machine, never through a detached single-agent re-run: the recorded
        # execution_mode is volatile task metadata and has been observed to
        # degrade to task_mode after a first resume, which detaches the re-run
        # from the work item and leaves it parked in awaiting_human forever.
        is_work_item_task = bool(
            is_work_item_runtime_metadata(task.metadata) or linked_work_item_id_for_task(task)
        )
        if execution_mode == ExecutionMode.COMPANY_MODE or is_work_item_task:
            if is_work_item_task:
                await self._release_work_item_human_wait(task, reason="approval_resume")
            # Re-register child tasks so WSHandler can dual-route progress
            # events from child work items to the parent session channel.
            self._reregister_company_runtime_children(tasks, checkpoint_session_id=checkpoint.session_id)
            plan_data = payload.get("company_work_item_plan") or task.metadata.get("company_work_item_plan")
            if isinstance(plan_data, dict) and plan_data:
                return await self._execute_company_mode(tasks, deserialize_company_work_item_runtime_plan(plan_data))
            if is_work_item_task:
                parent_session_id = str(
                    getattr(task, "parent_session_id", "")
                    or task.metadata.get("parent_session_id", "")
                    or checkpoint.session_id
                    or ""
                ).strip()
                snapshot = await self._load_company_runtime_snapshot(parent_session_id)
                if snapshot is not None:
                    snapshot_plan, _snapshot_tasks = snapshot
                    logger.info(
                        f"Resuming company-mode checkpoint {checkpoint.checkpoint_id} via runtime "
                        f"snapshot for parent session {parent_session_id}"
                    )
                    return await self._execute_company_mode(tasks, snapshot_plan)
            logger.info(
                f"Resuming company-mode checkpoint {checkpoint.checkpoint_id} without a runtime plan; "
                f"re-running the paused task {task.id} directly"
            )
        return await self._execute_single_agent([task], task.assigned_external_agent)

    async def _resume_peer_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        assert self.store and self.communication
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        task_id = payload.get("task_id") or payload.get("waiting_task_id")
        if not task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending peer wait because the task reference is missing."
        task = await self.store.get_task(task_id)
        if not task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending peer wait because the task no longer exists."
        if task.status != TaskStatus.AWAITING_PEER:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        else:
            resolved = False
            wait = dict(task.metadata.get("peer_wait", {}))
            wait_kind = str(wait.get("kind") or "")
            if wait_kind == "meeting":
                resolved = await self.communication.resolve_task_meeting_wait(task)
            elif wait_kind == "comms_blocking" or not wait:
                # Comms-blocking (and orphaned) waits resolve from durable
                # inbox files owned by the company dispatcher's per-tick
                # unpark. Re-enter the runtime and let it converge: it
                # either releases the park or re-parks and re-checkpoints
                # consistently.
                resolved = True
            else:
                resolved = await self.communication.resolve_task_peer_wait(task)
            if not resolved:
                hint = user_reply.strip()
                if hint:
                    task.context_snapshot = dict(task.context_snapshot)
                    task.context_snapshot["peer_resume_hint"] = hint
                    self._restore_runtime_state_from_checkpoint(task, payload)
                    await self.store.save_task(task)
                return (
                    "There is still a pending peer coordination wait. "
                    "Reply again after the peer has answered, or continue execution so the peer task can respond."
                )
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        self._restore_runtime_state_from_checkpoint(task, payload)
        tasks: list[Task] = []
        for sibling_id in payload.get("task_ids", [task_id]):
            sibling = await self.store.get_task(sibling_id)
            if sibling:
                tasks.append(sibling)
        if not tasks:
            return "Peer wait resolved, but no runtime work-item tasks were available to resume."
        execution_mode = str(payload.get("execution_mode", ExecutionMode.COMPANY_MODE.value))
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            # Re-register child tasks for WSHandler dual-routing
            self._reregister_company_runtime_children(tasks, checkpoint_session_id=checkpoint.session_id)
            plan_data = payload.get("company_work_item_plan") or task.metadata.get("company_work_item_plan")
            if isinstance(plan_data, dict) and plan_data:
                return await self._execute_company_mode(tasks, deserialize_company_work_item_runtime_plan(plan_data))
        return await self._execute_single_agent([task], task.assigned_external_agent)

    async def _mark_company_runtime_checkpoint_status(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        status: str,
        payload_updates: dict[str, Any] | None = None,
        expected_statuses: set[str] | None = None,
    ) -> bool:
        if not self.store:
            return False
        payload = {**dict(checkpoint.payload or {}), **dict(payload_updates or {})}
        updated_at = datetime.now()
        expected = {
            str(item).strip()
            for item in set(expected_statuses or set())
            if str(item).strip()
        }
        compare_and_set = getattr(
            self.store,
            "compare_and_set_execution_checkpoint",
            None,
        )
        if expected and callable(compare_and_set):
            transitioned = await compare_and_set(
                checkpoint.checkpoint_id,
                expected_statuses=expected,
                status=status,
                payload=payload,
                updated_at=updated_at,
            )
            if not transitioned:
                return False
        checkpoint.payload = payload
        checkpoint.status = status
        checkpoint.updated_at = updated_at
        save_checkpoint = getattr(self.store, "save_execution_checkpoint", None)
        if callable(save_checkpoint) and not (expected and callable(compare_and_set)):
            await save_checkpoint(checkpoint)
        elif not (expected and callable(compare_and_set)):
            await self.store.resolve_execution_checkpoint(
                checkpoint.checkpoint_id,
                status=status,
            )
        return True

    async def _reset_company_executor_runtime_for_resume(
        self,
        tasks: list[Task],
        payload: dict[str, Any],
    ) -> None:
        runtime = getattr(getattr(self, "company_executor", None), "runtime", None)
        reset = getattr(runtime, "reset_for_company_runtime_resume", None)
        if callable(reset):
            await reset(tasks, payload=payload)

    def _merge_company_runtime_checkpoint_payload(
        self,
        payload: dict[str, Any],
        supplement: dict[str, Any],
    ) -> dict[str, Any]:
        merged = copy.deepcopy(payload)

        merged["task_ids"] = list(dict.fromkeys([
            *list(merged.get("task_ids", []) or []),
            *list(supplement.get("task_ids", []) or []),
        ]))
        for key, identity_key in (
            ("task_snapshots", "task_id"),
            ("active_work_items", "work_item_id"),
        ):
            indexed = {
                str(item.get(identity_key, "") or "").strip(): copy.deepcopy(item)
                for item in list(merged.get(key, []) or [])
                if isinstance(item, dict)
                and str(item.get(identity_key, "") or "").strip()
            }
            for item in list(supplement.get(key, []) or []):
                if not isinstance(item, dict):
                    continue
                identity = str(item.get(identity_key, "") or "").strip()
                if identity:
                    indexed[identity] = copy.deepcopy(item)
            merged[key] = list(indexed.values())

        for key in ("role_runtime_session_ids", "seat_state_ids"):
            merged[key] = list(dict.fromkeys([
                *list(merged.get(key, []) or []),
                *list(supplement.get(key, []) or []),
            ]))
        for key in (
            "native_runtime_resume",
            "adapter_session_state",
            "external_sessions",
            "progress_tail",
        ):
            merged[key] = {
                **dict(merged.get(key, {}) or {}),
                **copy.deepcopy(dict(supplement.get(key, {}) or {})),
            }
        merged["basis_hash"] = self._checkpoint_basis_hash(merged)
        return merged

    async def _load_company_suspend_checkpoint_runtime(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> tuple[dict[str, Any], str, CompanyWorkItemRuntimePlan, list[Task]] | None:
        assert self.store
        payload = dict(checkpoint.payload or {})
        parent_session_id = str(
            checkpoint.session_id
            or payload.get("parent_session_id")
            or payload.get("session_id")
            or ""
        ).strip()
        task_ids = [
            str(item).strip()
            for item in list(payload.get("task_ids", []) or [])
            if str(item).strip()
        ]
        tasks: list[Task] = []
        for task_id in task_ids:
            task = await self.store.get_task(task_id)
            if task:
                tasks.append(task)

        plan_data = payload.get("company_work_item_plan") or payload.get("plan") or {}
        plan = deserialize_company_work_item_runtime_plan(plan_data if isinstance(plan_data, dict) else {})
        if parent_session_id:
            snapshot = await self._load_company_runtime_snapshot(parent_session_id)
            if snapshot:
                snapshot_plan, current_tasks = snapshot
                if not tasks:
                    tasks = current_tasks
                    if not plan.projections:
                        plan = snapshot_plan
                else:
                    known_task_ids = {task.id for task in tasks}
                    held_task_ids = await self._company_suspend_resume_candidate_task_ids(
                        current_tasks
                    )
                    supplemental_tasks = [
                        task
                        for task in current_tasks
                        if task.id not in known_task_ids and task.id in held_task_ids
                    ]
                    if supplemental_tasks:
                        supplement = await self._company_runtime_checkpoint_payload(
                            checkpoint_type=str(
                                checkpoint.checkpoint_type
                                or payload.get("checkpoint_type")
                                or "company_runtime_interrupted"
                            ),
                            reason=str(payload.get("reason", "") or "runtime_resume_reconciliation"),
                            parent_session_id=parent_session_id,
                            origin_task_id=str(
                                checkpoint.task_id
                                or payload.get("origin_task_id")
                                or ""
                            ).strip()
                            or None,
                            plan=plan,
                            tasks=supplemental_tasks,
                            stop_intent_id=str(payload.get("stop_intent_id", "") or "").strip()
                            or None,
                        )
                        payload = self._merge_company_runtime_checkpoint_payload(
                            payload,
                            supplement,
                        )
                        tasks.extend(supplemental_tasks)
        if not tasks:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return None

        payload["checkpoint_id"] = checkpoint.checkpoint_id
        payload["checkpoint_type"] = checkpoint.checkpoint_type
        return payload, parent_session_id, plan, tasks

    async def _resolve_company_runtime_ui_anchor_task_id(
        self,
        *,
        parent_session_id: str,
        checkpoint_id: str,
    ) -> str:
        if not self.store:
            return ""
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(
            runtime_session_id=parent_session_id,
            checkpoint_id=checkpoint_id,
        )
        return str(getattr(identity, "ui_anchor_task_id", "") or "").strip()

    async def _restore_company_suspend_checkpoint_pending(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
        tasks: list[Task],
        resume_state: str,
        error: BaseException | str,
    ) -> None:
        """Fail a resume closed: restore the durable hold before publishing pending."""

        if not self.store:
            return
        project_id = str(self.project_id or "default").strip() or "default"
        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            current = await self._load_execution_checkpoint_by_id(
                checkpoint.checkpoint_id
            )
            if current is None or str(current.status or "").strip() == "resolved":
                return
            payload = dict(current.payload or checkpoint.payload or {})
            await self._suspend_company_runtime_tasks(
                tasks,
                reason="resume_failed",
                checkpoint_type=str(
                    current.checkpoint_type
                    or checkpoint.checkpoint_type
                    or "company_runtime_interrupted"
                ),
                stop_intent_id=str(payload.get("stop_intent_id", "") or ""),
            )
            error_text = str(error).strip() or type(error).__name__
            transitioned = await self._mark_company_runtime_checkpoint_status(
                current,
                status="pending",
                payload_updates={
                    "resume_state": resume_state,
                    "resume_failed_at": datetime.now().isoformat(),
                    "resume_error": error_text,
                },
                expected_statuses={"resuming"},
            )
            if not transitioned:
                refreshed = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if refreshed is None:
                    return
                current = refreshed
            checkpoint.status = current.status
            checkpoint.payload = dict(current.payload or {})
            checkpoint.updated_at = current.updated_at

    async def _restore_company_suspend_checkpoint_pending_after_cancellation(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
        tasks: list[Task],
        resume_state: str,
        error: BaseException,
    ) -> None:
        recovery = asyncio.create_task(
            self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state=resume_state,
                error=error,
            )
        )
        try:
            await asyncio.shield(recovery)
        except asyncio.CancelledError:
            await recovery

    async def _complete_company_suspend_checkpoint_resume(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
    ) -> None:
        """Publish one successful resume and its UI projection as one boundary."""

        if not self.store:
            return
        project_id = str(self.project_id or "default").strip() or "default"
        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            current = await self._load_execution_checkpoint_by_id(
                checkpoint.checkpoint_id
            )
            if current is None or str(current.status or "").strip() != "resuming":
                raise RuntimeError(
                    "company runtime resume was interrupted before completion"
                )
            payload = dict(current.payload or {})
            resolved_at = datetime.now()
            resolved_payload = {
                **payload,
                "resume_state": "handoff_complete",
                "resume_handoff_at": resolved_at.isoformat(),
                "resume_resolved_at": resolved_at.isoformat(),
            }
            transitioned = (
                await self.store.complete_execution_checkpoint_and_reopen_ui_anchor(
                    current.checkpoint_id,
                    project_id=project_id,
                    session_id=parent_session_id,
                    expected_status="resuming",
                    status="resolved",
                    payload=resolved_payload,
                    ui_anchor_task_id=str(
                        payload.get("ui_anchor_task_id", "") or ""
                    ).strip(),
                    updated_at=resolved_at,
                )
            )
            if not transitioned:
                raise RuntimeError(
                    "company runtime checkpoint changed during resume completion"
                )
            current.status = "resolved"
            current.payload = resolved_payload
            current.updated_at = resolved_at
            checkpoint.status = current.status
            checkpoint.payload = dict(current.payload or {})
            checkpoint.updated_at = current.updated_at

    async def _handoff_company_suspend_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        payload: dict[str, Any],
        parent_session_id: str,
        tasks: list[Task],
        resume_task_ids: set[str] | None = None,
    ) -> tuple[list[Task], CompanyExecutorDriverOwnership | None] | None:
        assert self.company_executor
        if not self.store:
            return None
        project_id = str(self.project_id or "default").strip() or "default"
        claimed = False
        driver_ownership: CompanyExecutorDriverOwnership | None = None
        try:
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                current = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if current is None or str(current.status or "").strip() != "pending":
                    return None
                checkpoint.status = current.status
                checkpoint.payload = dict(current.payload or {})
                checkpoint.updated_at = current.updated_at
                ui_anchor_task_id = await self._resolve_company_runtime_ui_anchor_task_id(
                    parent_session_id=parent_session_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="resuming",
                    payload_updates={
                        **payload,
                        "resume_state": "resuming",
                        "resume_started_at": datetime.now().isoformat(),
                        "ui_anchor_task_id": ui_anchor_task_id,
                    },
                    expected_statuses={"pending"},
                )
                if transitioned is False:
                    return None
                claimed = True
                driver_ownership = self._acquire_company_executor_driver_ownership(
                    tasks,
                    preferred_task_ids=resume_task_ids,
                )
                tasks = await self._prepare_company_runtime_tasks_for_resume(
                    tasks,
                    payload,
                    resume_task_ids=resume_task_ids,
                )
                await self._reset_company_executor_runtime_for_resume(tasks, payload)
                await self._clear_company_runtime_parent_stop_state(
                    parent_session_id,
                    payload,
                )
                if parent_session_id:
                    self._reregister_company_runtime_children(
                        tasks,
                        checkpoint_session_id=parent_session_id,
                    )
                notify_kanban_changed = getattr(
                    self.company_executor,
                    "_notify_kanban_changed",
                    None,
                )
                if callable(notify_kanban_changed):
                    # The registry attempt above belongs to this successful
                    # checkpoint handoff, and resume preparation has now
                    # cleared its durable holds.  Publish through the existing
                    # canonical snapshot path so UI control state becomes
                    # running/stoppable without introducing a second
                    # liveness signal in the WS layer.
                    await notify_kanban_changed()
        except asyncio.CancelledError as exc:
            if driver_ownership is not None:
                driver_ownership.release()
            if claimed:
                await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                    checkpoint,
                    parent_session_id=parent_session_id,
                    tasks=tasks,
                    resume_state="failed_before_handoff",
                    error=exc,
                )
            raise
        except Exception as exc:
            if driver_ownership is not None:
                driver_ownership.release()
            if claimed:
                await self._restore_company_suspend_checkpoint_pending(
                    checkpoint,
                    parent_session_id=parent_session_id,
                    tasks=tasks,
                    resume_state="failed_before_handoff",
                    error=exc,
                )
            raise
        return tasks, driver_ownership

    def _acquire_company_executor_driver_ownership(
        self,
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> CompanyExecutorDriverOwnership | None:
        task = CompanyWorkItemExecutor._driver_ownership_task(
            tasks,
            preferred_task_ids=preferred_task_ids,
        )
        if task is None:
            return None
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            attempt_token = self._active_task_run_registry.register(
                project_id,
                task.id,
            )
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        return CompanyExecutorDriverOwnership(
            registry=self._active_task_run_registry,
            project_id=project_id,
            task_id=task.id,
            attempt_token=attempt_token,
        )

    @staticmethod
    def _company_executor_driver_context(
        ownership: CompanyExecutorDriverOwnership | None,
    ):
        return ownership.bind() if ownership is not None else nullcontext()

    async def _company_suspend_resume_candidate_task_ids(
        self,
        tasks: list[Task],
        *,
        exclude_task_ids: set[str] | None = None,
    ) -> set[str]:
        if not self.store:
            return set()
        excluded = {str(item).strip() for item in set(exclude_task_ids or set()) if str(item).strip()}
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        candidate_ids: set[str] = set()
        for task in tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            if not task_id or task_id in excluded:
                continue
            metadata = dict(getattr(task, "metadata", {}) or {})
            task_is_held = any(str(metadata.get(key, "") or "").strip() for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS)
            work_item_id = linked_work_item_id_for_task(task)
            work_item_is_held = False
            work_item = None
            if work_item_id and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    if getattr(work_item, "phase", None) in DONE_PHASES:
                        continue
                    work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
                    work_item_is_held = str(work_item_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
            if (
                task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
                and (
                    work_item is None
                    or getattr(work_item, "phase", None) in DONE_PHASES
                )
            ):
                continue
            if task_is_held or work_item_is_held:
                candidate_ids.add(task_id)
        return candidate_ids

    async def _resume_remaining_company_runtime_after_final_decider(
        self,
        *,
        checkpoint: ExecutionCheckpoint,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        payload: dict[str, Any],
        parent_session_id: str,
        final_decider_task_id: str,
    ) -> tuple[bool, str | None]:
        assert self.company_executor
        target_progressed = await self._company_followup_target_progressed(
            final_decider_task_id
        )
        project_id = str(self.project_id or "default").strip() or "default"
        driver_ownership: CompanyExecutorDriverOwnership | None = None
        awaiting_final_decider_action = False
        try:
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                current = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if (
                    current is None
                    or str(current.status or "").strip() != "resuming"
                    or str(current.project_id or "default").strip() != project_id
                    or str(current.session_id or "").strip() != parent_session_id
                    or str(current.checkpoint_type or "").strip()
                    not in _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES
                ):
                    logger.info(
                        "company runtime resume: remaining handoff skipped because checkpoint {} "
                        "is no longer the resuming owner of scope {}",
                        checkpoint.checkpoint_id,
                        parent_session_id,
                    )
                    return False, None
                checkpoint.status = current.status
                checkpoint.payload = dict(current.payload or {})
                checkpoint.updated_at = current.updated_at
                refreshed_snapshot = await self._load_company_runtime_snapshot(
                    parent_session_id
                )
                if refreshed_snapshot:
                    plan, tasks = refreshed_snapshot
                resume_task_ids = await self._company_suspend_resume_candidate_task_ids(
                    tasks,
                    exclude_task_ids={final_decider_task_id},
                )
                if not resume_task_ids:
                    return True, None
                if not target_progressed:
                    awaiting_final_decider_action = True
                else:
                    # Acquire the scheduler attempt while the scope lock still
                    # owns the checkpoint.  Stop/shutdown can therefore never
                    # observe the released WorkItems without a live coroutine.
                    driver_ownership = self._acquire_company_executor_driver_ownership(
                        tasks,
                        preferred_task_ids=resume_task_ids,
                    )
                    tasks = await self._prepare_company_runtime_tasks_for_resume(
                        tasks,
                        payload,
                        resume_task_ids=resume_task_ids,
                    )
                    await self._reset_company_executor_runtime_for_resume(tasks, payload)
                    if parent_session_id:
                        self._reregister_company_runtime_children(
                            tasks,
                            checkpoint_session_id=parent_session_id,
                        )
        except BaseException:
            if driver_ownership is not None:
                driver_ownership.release()
            raise
        if awaiting_final_decider_action:
            logger.info(
                "company runtime resume: restoring checkpoint {} to pending because "
                "remaining work is held and final decider has no durable arbitration action",
                checkpoint.checkpoint_id,
            )
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="awaiting_final_decider_action",
                error="final decider returned without a durable arbitration action",
            )
            return False, None
        try:
            with self._company_executor_driver_context(driver_ownership):
                if self.on_company_runtime_children and parent_session_id and tasks:
                    self.on_company_runtime_children(
                        parent_session_id,
                        [t.id for t in tasks],
                    )
                result = await self.company_executor.execute(plan, tasks)
                return True, result
        finally:
            if driver_ownership is not None:
                driver_ownership.release()

    async def _company_followup_target_progressed(self, task_id: str) -> bool:
        if not self.store:
            return False
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        task = await self.store.get_task(task_id)
        if task is None:
            return False
        task_metadata = dict(task.metadata or {})
        if bool(task_metadata.get("manager_board_mutation_performed", False)):
            return True
        if str(task_metadata.get("manager_no_delegation_justification", "") or "").strip():
            return True
        if str(task_metadata.get("manager_dispatch_guard_unresolved", "") or "").strip():
            return True
        return False

    async def _resume_company_suspend_checkpoint_via_final_decider(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        assert self.store and self.company_executor
        loaded = await self._load_company_suspend_checkpoint_runtime(checkpoint)
        if loaded is None:
            return "Could not route the suspended company runtime because its task set could not be restored."
        payload, parent_session_id, plan, tasks = loaded
        target_task = self._company_followup_target_task(plan, tasks)
        if target_task is None:
            # Every routable final-decider card is terminal — a follow-up can
            # no longer be routed, but the run itself must still drain instead
            # of bouncing this checkpoint back to pending on every message.
            plain = await self._resume_company_suspend_checkpoint(checkpoint, user_reply)
            return (
                "The final-decider work item for this run is no longer active "
                "(failed or cancelled), so the message could not be routed to it. "
                "Resumed the remaining runtime instead; re-issue the request as a "
                f"new task if further work is needed.\n\n{plain}"
            ).strip()

        handoff = await self._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=payload,
            parent_session_id=parent_session_id,
            tasks=tasks,
            resume_task_ids={target_task.id},
        )
        if handoff is None:
            return (
                "This company runtime is already being resumed by another request."
            )
        tasks, driver_ownership = handoff
        try:
            with self._company_executor_driver_context(driver_ownership):
                followup_result = await self._resume_company_runtime_via_final_decider(
                    plan=plan,
                    tasks=tasks,
                    user_reply=user_reply,
                    session_id=parent_session_id,
                    degrade_to_plain_resume_on_missing_target=True,
                )
                if followup_result is None:
                    await self._restore_company_suspend_checkpoint_pending(
                        checkpoint,
                        parent_session_id=parent_session_id,
                        tasks=tasks,
                        resume_state="failed_during_execution",
                        error="final-decider work item was unavailable",
                    )
                    return (
                        "Could not route the suspended company runtime because the CEO/final-decider "
                        "work item was unavailable after resume handoff."
                    )
                continuation_owned, continuation_result = await self._resume_remaining_company_runtime_after_final_decider(
                    checkpoint=checkpoint,
                    plan=plan,
                    tasks=tasks,
                    payload=payload,
                    parent_session_id=parent_session_id,
                    final_decider_task_id=target_task.id,
                )
                if not continuation_owned:
                    if (
                        str(checkpoint.payload.get("resume_state", "") or "").strip()
                        == "awaiting_final_decider_action"
                    ):
                        return (
                            f"{followup_result}\n\nThe CEO/final decider has not yet recorded "
                            "a durable arbitration action. Remaining work items stay suspended "
                            "and this checkpoint remains available to continue."
                        ).strip()
                    return (
                        f"{followup_result}\n\nThe company runtime was suspended before "
                        "remaining work items were released; they remain held."
                    ).strip()
                await self._complete_company_suspend_checkpoint_resume(
                    checkpoint,
                    parent_session_id=parent_session_id,
                )
        except asyncio.CancelledError as exc:
            await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        except Exception as exc:
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        finally:
            if driver_ownership is not None:
                driver_ownership.release()
        if continuation_result:
            return f"{followup_result}\n\nResumed remaining company runtime after CEO/final-decider arbitration.\n\n{continuation_result}".strip()
        return followup_result

    async def _resume_company_suspend_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        assert self.store and self.company_executor
        loaded = await self._load_company_suspend_checkpoint_runtime(checkpoint)
        if loaded is None:
            return "Could not resume the suspended company runtime because its task set could not be restored."
        payload, parent_session_id, plan, tasks = loaded
        handoff = await self._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=payload,
            parent_session_id=parent_session_id,
            tasks=tasks,
        )
        if handoff is None:
            return "This company runtime is already being resumed by another request."
        tasks, driver_ownership = handoff
        try:
            with self._company_executor_driver_context(driver_ownership):
                result = await self.company_executor.execute(plan, tasks)
                await self._complete_company_suspend_checkpoint_resume(
                    checkpoint,
                    parent_session_id=parent_session_id,
                )
        except asyncio.CancelledError as exc:
            await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        except Exception as exc:
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        finally:
            if driver_ownership is not None:
                driver_ownership.release()
        prefix = (
            "Resuming the suspended company runtime"
            if checkpoint.checkpoint_type == "company_runtime_suspended"
            else "Resuming the interrupted company runtime"
        )
        return f"{prefix}.\n\n{result}".strip()

    async def _resume_company_runtime_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        assert self.store and self.company_executor
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        waiting_task_id = payload.get("waiting_task_id")
        if not waiting_task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because the waiting task reference is missing."

        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because the waiting task no longer exists."
        self._restore_runtime_state_from_checkpoint(waiting_task, payload)

        # Pre-register all child tasks so that any rework progress events
        # emitted below can be dual-routed to the parent session channel.
        _early_tasks: list[Task] = []
        for _tid in payload.get("task_ids", []):
            _t = await self.store.get_task(str(_tid))
            if _t:
                _early_tasks.append(_t)
        if _early_tasks:
            self._reregister_company_runtime_children(_early_tasks, checkpoint_session_id=checkpoint.session_id)

        reply_text = user_reply.strip()
        reply = reply_text.lower()
        approved_tokens = {"y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        gate_data = dict(payload.get("gate", {}))
        gate_metadata = dict(gate_data.get("metadata", {}) or {})
        gate_source = str(gate_metadata.get("source", "") or "").strip()
        if reply in approved_tokens:
            waiting_task.status = TaskStatus.DONE
            if gate_source == "gate_harness":
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata.pop("gate_harness_pending_decision", None)
                constraints = [
                    str(item).strip()
                    for item in list(gate_metadata.get("constraints", []) or [])
                    if str(item).strip()
                ]
                if constraints:
                    waiting_task.metadata["gate_harness_constraints"] = constraints
                    waiting_task.metadata["gate_harness_status"] = "passed_with_constraints"
                    waiting_task.metadata["risks"] = list(dict.fromkeys([
                        *list(waiting_task.metadata.get("risks", []) or []),
                        *constraints,
                    ]))
                else:
                    waiting_task.metadata["gate_harness_status"] = "passed"
            progress = list(waiting_task.metadata.get("progress_log", []))
            progress.append(f"Human confirmed via resume message: {reply_text}")
            waiting_task.metadata["progress_log"] = progress
            await self.store.save_task(waiting_task)
            # Emit a visible progress signal so the UI shows the resume actually
            # took effect, instead of leaving the user staring at the same gate
            # card. Without this, "approve" looks like a no-op when the runtime
            # then proceeds silently.
            resume_progress = self._make_task_progress_callback(waiting_task)
            if resume_progress:
                projection_label = projection_id_for_task(waiting_task) or waiting_task.title
                await resume_progress(
                    f"[Company:{projection_label}] human approved gate; resuming runtime"
                )
        else:
            rejection_feedback = ""
            if reply in denied_tokens:
                rejection_feedback = reply_text
            elif reply_text:
                rejection_feedback = reply_text
            else:
                return (
                    "There is a pending runtime waiting for confirmation. "
                    "Reply with `approve` / `continue` to proceed, or `deny` / `stop` to halt it."
                )
            progress = list(waiting_task.metadata.get("progress_log", []))
            progress.append(f"Human review feedback via resume message: {rejection_feedback}")
            waiting_task.metadata["progress_log"] = progress
            if gate_source == "gate_harness":
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata.pop("gate_harness_pending_decision", None)
            gate = self.company_executor._gate_from_metadata(gate_data)
            rework_projection_id = rework_projection_id_for_gate(gate) if gate else ""
            if gate and gate.on_reject == "rework" and rework_projection_id:
                task_by_projection_id: dict[str, Task] = {waiting_task.id: waiting_task}
                waiting_projection_id = projection_id_for_task(waiting_task)
                if waiting_projection_id:
                    task_by_projection_id[waiting_projection_id] = waiting_task
                for task_id in payload.get("task_ids", []):
                    task = await self.store.get_task(task_id)
                    if not task:
                        continue
                    task_by_projection_id[task.id] = task
                    projection_id = projection_id_for_task(task)
                    if projection_id:
                        task_by_projection_id[projection_id] = task
                rework_task = await self.company_executor.prepare_gate_rework(
                    waiting_task,
                    gate,
                    task_by_projection_id,
                    rejection_feedback,
                )
                if rework_task is None:
                    waiting_task.metadata = dict(waiting_task.metadata)
                    waiting_task.metadata["last_gate_review_feedback"] = rejection_feedback
                    await self._fail_task_via_phase(
                        waiting_task,
                        reason="gate_rework_restore_failed",
                    )
                    await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
                    return (
                        f"Runtime halted after human rejection for work item `{waiting_task.title}` because "
                        f"the configured rework projection `{rework_projection_id}` could not be restored."
                    )
                if rework_task is not waiting_task:
                    await self.store.save_task(rework_task)
                await self.store.save_task(waiting_task)
            else:
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata["last_gate_review_feedback"] = rejection_feedback
                await self._fail_task_via_phase(
                    waiting_task,
                    reason="human_gate_denied",
                )
                await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
                return f"Runtime halted after human denial for work item `{waiting_task.title}`."

        tasks: list[Task] = []
        for task_id in payload.get("task_ids", []):
            task = await self.store.get_task(task_id)
            if task:
                tasks.append(task)

        if not tasks:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because its task set could not be restored."

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        return (
            "This checkpoint belongs to a legacy company runtime run. "
            "Legacy runs are available for inspection only and cannot be resumed."
        )

    async def _resume_company_feedback_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        assert self.store and self.company_executor
        reply = str(user_reply or "").strip()
        if not reply:
            return "There is a pending delivery self-evolution review. Use the review card to fully agree, ignore, or send feedback."
        normalized = reply.lower()
        if normalized in {"ignore", "ignored", "skip"}:
            return await self.ignore_company_delivery_feedback_checkpoint(checkpoint)
        action = "approve" if normalized in {"approve", "approved", "i approve this delivery.", "fully agree"} else "feedback"
        return await self.run_company_delivery_self_evolution_checkpoint(
            checkpoint,
            action=action,
            feedback=reply if action == "feedback" else "",
        )

    async def _save_company_feedback_followup_checkpoint(
        self,
        task: Task,
        tasks: list[Task],
        plan: CompanyWorkItemRuntimePlan,
    ) -> None:
        if self.company_executor and hasattr(self.company_executor, "_save_feedback_checkpoint"):
            self.company_executor._active_plan = plan
            self.company_executor._active_tasks = tasks
            await self.company_executor._save_feedback_checkpoint(task)  # type: ignore[attr-defined]
            return
        result_content = ""
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        context_snapshot = dict(task.context_snapshot or {})
        output_metadata = dict(context_snapshot.get("work_item_owned_outputs", {}) or {})
        delivery_package = output_metadata.get("delivery_package") or task.metadata.get("delivery_package") or {}
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "company_delivery_feedback",
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "waiting_work_item_id": linked_work_item_id_for_task(task),
                    "task_ids": [item.id for item in tasks],
                    "feedback_scope": str(task.metadata.get("feedback_scope", "") or "work_item").strip() or "work_item",
                    "prompt": (
                        str(task.metadata.get("feedback_followup_message", "") or "").strip()
                        or "Use this card only to record full agreement, ignore, or feedback for employee self-evolution."
                    ),
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "delivery_revision": task.metadata.get("delivery_revision", ""),
                    "owner_directive_revision": task.metadata.get("owner_directive_revision", ""),
                    "latest_user_directive": str(task.metadata.get("latest_user_directive", "") or "").strip(),
                    "result_content": result_content,
                    "delivery_package": delivery_package if isinstance(delivery_package, dict) else {},
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                },
            }
        )

    async def _evaluate_company_feedback(
        self,
        delivery_task: Task,
        work_item_tasks: list[Task],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = self._fallback_company_feedback_evaluation(work_item_tasks, feedback)
        if not self.llm:
            return fallback

        employees_payload: list[dict[str, Any]] = []
        seen_employee_ids: set[str] = set()
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id or employee_id in seen_employee_ids:
                continue
            seen_employee_ids.add(employee_id)
            history = ""
            if self.memory:
                organization_id = str(getattr(getattr(self.config, "org", None), "organization_id", "") or "").strip()
                history = self.memory.employee_evolution.build_employee_delta_context(
                    employee_id,
                    project_id=task.project_id,
                    organization_id=organization_id or None,
                )
            employees_payload.append(
                {
                    "employee_id": employee_id,
                    "employee_name": assignment.get("name", ""),
                    "role_id": assignment.get("role_id") or task.assigned_to,
                    "history": history,
                }
            )

        task_payload = []
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            work_item_summary = str(task.metadata.get("work_item_summary_for_downstream", "") or "").strip()
            if not work_item_summary and task.result:
                work_item_summary = str(task.result.get("content", "") or "").strip()
            task_payload.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "projection_id": projection_id_for_task(task),
                    "work_item_projection_title": task.title,
                    "employee_id": assignment.get("employee_id", ""),
                    "employee_name": assignment.get("name", ""),
                    "role_id": assignment.get("role_id") or task.assigned_to,
                    "status": getattr(task.status, "value", str(task.status)),
                    "summary": work_item_summary,
                    "work_item_feedback": list(task.metadata.get("feedback_records", [])),
                }
            )

        prompt = {
            "project_id": delivery_task.project_id,
            "feedback_scope": feedback.get("scope", "final"),
            "user_feedback": feedback,
            "delivery_projection_id": projection_id_for_task(delivery_task),
            **work_item_identity_payload_for_task(delivery_task, fallback_turn_type=""),
            "work_item_tasks": task_payload,
            "employees": employees_payload,
        }
        try:
            raw = await self.llm.simple_chat(
                prompt=json.dumps(prompt, ensure_ascii=False),
                system=COMPANY_FEEDBACK_ATTRIBUTION_PROMPT,
                task_type="quick_tasks",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            data = json.loads(text)
            if not isinstance(data, dict):
                return fallback
            employees = data.get("employees", [])
            if not isinstance(employees, list):
                data["employees"] = []
            data.setdefault("overall_outcome", fallback["overall_outcome"])
            data.setdefault("summary", fallback["summary"])
            data.setdefault("strengths", fallback["strengths"])
            data.setdefault("weaknesses", fallback["weaknesses"])
            return data
        except Exception as exc:
            logger.debug(f"Company feedback evaluation failed: {exc}")
            return fallback

    def _fallback_company_feedback_evaluation(self, work_item_tasks: list[Task], feedback: dict[str, Any]) -> dict[str, Any]:
        label = str(feedback.get("label", "")).strip()
        if label == "fully_approved":
            overall_outcome = "success"
        elif label == "fully_rejected":
            overall_outcome = "failure"
        else:
            overall_outcome = "partial_success"
        employees: list[dict[str, Any]] = []
        seen_employee_ids: set[str] = set()
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id or employee_id in seen_employee_ids:
                continue
            seen_employee_ids.add(employee_id)
            employees.append(
                {
                    "employee_id": employee_id,
                    "outcome": overall_outcome,
                    "reason": "Fallback attribution based on the overall user feedback.",
                    "strengths": [],
                    "weaknesses": [],
                }
            )
        return {
            "overall_outcome": overall_outcome,
            "summary": str(feedback.get("raw_feedback", "")).strip(),
            "strengths": [],
            "weaknesses": [],
            "employees": employees,
        }

    def _runtime_topology_from_tasks(self, tasks: list[Task], waiting_task: Task) -> dict[str, Any]:
        for task in [waiting_task, *list(tasks or [])]:
            topology = dict(getattr(task, "metadata", {}).get("runtime_topology", {}) or {})
            if topology:
                return topology
        return {}

    @staticmethod
    def _runtime_seat_for_role(runtime_topology: dict[str, Any], role_id: str) -> dict[str, Any]:
        role = str(role_id or "").strip()
        for seat in list(runtime_topology.get("seats", []) or []):
            seat_data = dict(seat or {})
            if str(seat_data.get("role_id", "") or "").strip() == role:
                return seat_data
        return {}

    @staticmethod
    def _task_runtime_value(tasks: list[Task], key: str, default: str = "") -> str:
        for task in list(tasks or []):
            value = str(getattr(task, "metadata", {}).get(key, "") or "").strip()
            if value:
                return value
        return default

    def _self_evolution_prompt_contract(
        self,
        *,
        role_id: str,
        source: dict[str, Any],
        tasks: list[Task],
    ) -> dict[str, Any]:
        feedback = str(source.get("human_feedback", "") or "").strip()
        action = str(source.get("human_action", "") or "approve").strip()
        review_text = (
            f"Human review action: {action}."
            + (f"\nHuman feedback: {feedback}" if feedback else "\nHuman fully agreed with this delivery.")
        )
        task_brief = (
            "Run employee self-evolution for this role from the completed company delivery review.\n"
            f"{review_text}\n\n"
            "Decide whether your assigned employee should update its experience. If direct reports should also learn, "
            "delegate child WorkItems with `work_kind=\"self_evolution\"`. Do not continue the original user task, "
            "do not edit files, and do not produce a user-facing report. Final response must be strict JSON only: "
            "`{\"patches\": [...]}`."
        )
        return make_prompt_contract(
            task_brief=task_brief,
            upstream_intent_summary=str(source.get("delivery_summary", "") or "").strip(),
            manager_planning_handoff=(
                "Use the human review signal, delivery summary, work item task list, and org graph to decide "
                "which direct reports need self-evolution work."
            ),
            owned_outcome_kind="self_evolution",
            scope_key=f"self_evolution::{source.get('checkpoint_id', '')}::{role_id}",
            deliverables=[
                "Strict JSON only with top-level `patches` list.",
                "Use `patches: []` if no employee experience update is needed for this role.",
                "Use `delegate_work` with `work_kind=\"self_evolution\"` for direct reports that should reflect on their own work.",
            ],
            acceptance_criteria=[
                "No prose, markdown, file edits, or user-facing delivery content.",
                "Patch employee_id must be the employee assigned to this role's self-evolution work item.",
                "Each patch may include summary, strengths, adjustments, avoid_next_time, routing_notes, evidence_task_ids, and confidence.",
            ],
            coordination_notes=json.dumps(
                {
                    "work_item_tasks": self._self_evolution_task_payloads(tasks),
                    "org_graph": self._self_evolution_org_graph(),
                },
                ensure_ascii=False,
            ),
            source={"kind": "company_delivery_feedback_self_evolution"},
        )

    async def _create_company_self_evolution_root_work_item(
        self,
        *,
        checkpoint: ExecutionCheckpoint,
        waiting_task: Task,
        tasks: list[Task],
        plan: CompanyWorkItemRuntimePlan,
        root_role_id: str,
        organization_id: str,
        source: dict[str, Any],
        assignments: dict[str, dict[str, Any]],
    ) -> DelegationWorkItem | None:
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        all_tasks = list(tasks or [])
        if waiting_task.id not in {task.id for task in all_tasks}:
            all_tasks.append(waiting_task)
        runtime_topology = self._runtime_topology_from_tasks(all_tasks, waiting_task)
        linked_delivery_work_item: DelegationWorkItem | None = None
        linked_delivery_work_item_id = str(linked_work_item_id_for_task(waiting_task) or "").strip()
        if linked_delivery_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_delivery_work_item = await self.store.get_delegation_work_item(linked_delivery_work_item_id)
            except Exception:
                linked_delivery_work_item = None
        run_id = str(
            self._task_runtime_value(all_tasks, "delegation_run_id")
            or runtime_topology.get("run_id", "")
            or getattr(linked_delivery_work_item, "run_id", "")
            or ""
        ).strip()
        if not runtime_topology and self.org_engine and hasattr(self.org_engine, "build_runtime_delegation_topology"):
            try:
                runtime_topology = dict(self.org_engine.build_runtime_delegation_topology() or {})
            except Exception:
                runtime_topology = {}
        if run_id and runtime_topology:
            runtime_topology.setdefault("run_id", run_id)
        root_seat = self._runtime_seat_for_role(runtime_topology, root_role_id)
        if not root_seat and linked_delivery_work_item is not None:
            linked_metadata = dict(getattr(linked_delivery_work_item, "metadata", {}) or {})
            root_assignment = dict(
                assignments.get(root_role_id, {})
                or linked_metadata.get("employee_assignment", {})
                or waiting_task.metadata.get("employee_assignment", {})
                or {}
            )
            root_seat = {
                "role_id": root_role_id,
                "cell_id": str(getattr(linked_delivery_work_item, "cell_id", "") or linked_metadata.get("delegation_cell_id", "") or "").strip(),
                "team_id": str(getattr(linked_delivery_work_item, "team_id", "") or linked_metadata.get("delegation_team_id", "") or "").strip(),
                "team_instance_id": str(getattr(linked_delivery_work_item, "team_instance_id", "") or linked_metadata.get("delegation_team_instance_id", "") or "").strip(),
                "seat_id": str(getattr(linked_delivery_work_item, "seat_id", "") or linked_metadata.get("delegation_seat_id", "") or "").strip(),
                "seat_state_id": str(getattr(linked_delivery_work_item, "seat_state_id", "") or linked_metadata.get("delegation_seat_state_id", "") or "").strip(),
                "role_runtime_session_id": str(getattr(linked_delivery_work_item, "role_runtime_session_id", "") or linked_metadata.get("delegation_role_session_id", "") or "").strip(),
                "manager_role_id": str(getattr(linked_delivery_work_item, "manager_role_id", "") or linked_metadata.get("manager_role_id", "") or "").strip(),
                "manager_seat_id": str(getattr(linked_delivery_work_item, "manager_seat_id", "") or linked_metadata.get("manager_seat_id", "") or "").strip(),
                "managed_team_id": str(linked_metadata.get("managed_team_id", "") or "").strip(),
                "direct_report_role_ids": list(linked_metadata.get("direct_report_role_ids", []) or []),
                "direct_report_seat_ids": list(linked_metadata.get("direct_report_seat_ids", []) or []),
                "allowed_delegate_role_ids": list(linked_metadata.get("allowed_delegate_role_ids", []) or []),
                "contact_role_ids": list(linked_metadata.get("contact_role_ids", []) or []),
                "employee_assignment": root_assignment,
            }
        if not run_id or not root_seat:
            return None
        team_id = str(root_seat.get("team_id", "") or self._task_runtime_value(all_tasks, "delegation_team_id") or "").strip()
        team_instance_id = str(root_seat.get("team_instance_id", "") or "").strip()
        if not team_instance_id and team_id:
            team_instance_id = f"team-instance::{run_id}::{team_id}"
        role_runtime_session_id = str(root_seat.get("role_runtime_session_id", "") or "").strip()
        if not role_runtime_session_id:
            role_runtime_session_id = canonical_role_session_id(
                run_id=run_id,
                role_id=root_role_id,
                team_instance_id=team_instance_id,
            )
        seat_id = str(root_seat.get("seat_id", "") or "").strip()
        assignment = dict(assignments.get(root_role_id, {}) or root_seat.get("employee_assignment", {}) or {})
        work_item_id = f"self-evolution::{checkpoint.checkpoint_id}"
        projection_id = f"self_evolution::{checkpoint.checkpoint_id[:8]}::{root_role_id}"
        delivery_summary = str(
            waiting_task.metadata.get("work_item_summary_for_downstream", "")
            or waiting_task.metadata.get("work_item_summary", "")
            or source.get("delivery_summary", "")
            or ""
        ).strip()
        source["delivery_summary"] = delivery_summary
        prompt_contract = self._self_evolution_prompt_contract(
            role_id=root_role_id,
            source=source,
            tasks=all_tasks,
        )
        assignment_context = dict(prompt_contract.get("assignment_context", {}) or {})
        session_scope_id = task_session_scope_id(waiting_task)
        metadata = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "execution_mode": "company_mode",
            "execution_model": "multi_team_org",
            "mode": "company",
            "work_kind": "self_evolution",
            "self_evolution_work_item": True,
            "self_evolution_root": True,
            "self_evolution_checkpoint_id": checkpoint.checkpoint_id,
            "self_evolution_human_action": source.get("human_action", ""),
            "self_evolution_human_feedback": source.get("human_feedback", ""),
            "self_evolution_delivery_task_id": waiting_task.id,
            "self_evolution_delivery_projection_id": projection_id_for_task(waiting_task),
            "self_evolution_delivery_summary": delivery_summary,
            "self_evolution_patch_max_retries": 3,
            "organization_id": organization_id,
            "org_id": organization_id,
            "company_profile": str(getattr(plan, "profile", "") or waiting_task.metadata.get("company_profile", "") or "").strip(),
            "delegation_run_id": run_id,
            "delegation_cell_id": str(root_seat.get("cell_id", "") or team_id or "").strip(),
            "delegation_team_id": team_id,
            "delegation_team_instance_id": team_instance_id,
            "delegation_role_session_id": role_runtime_session_id,
            "session_scope_id": session_scope_id,
            "assigned_role_runtime_id": role_runtime_session_id,
            "manager_role_id": str(root_seat.get("manager_role_id", "") or "").strip(),
            "manager_seat_id": str(root_seat.get("manager_seat_id", "") or "").strip(),
            "managed_team_id": str(root_seat.get("managed_team_id", "") or "").strip(),
            "direct_report_role_ids": list(root_seat.get("direct_report_role_ids", []) or []),
            "direct_report_seat_ids": list(root_seat.get("direct_report_seat_ids", []) or []),
            "allowed_delegate_role_ids": list(root_seat.get("allowed_delegate_role_ids", []) or []),
            "contact_role_ids": list(root_seat.get("contact_role_ids", []) or []),
            "runtime_topology": runtime_topology,
            "employee_assignment": assignment,
            "prompt_contract": prompt_contract,
            "prompt_assignment": assignment_context,
            "brief": prompt_contract.get("task_brief", ""),
            "deliverables": list(assignment_context.get("deliverables", []) or []),
            "acceptance_criteria": list(assignment_context.get("acceptance_criteria", []) or []),
            "work_item_tasks": self._self_evolution_task_payloads(all_tasks),
            "org_graph": self._self_evolution_org_graph(),
            "workspace_root": self._task_runtime_value(all_tasks, "workspace_root"),
            "comms_workspace_root": self._task_runtime_value(all_tasks, "comms_workspace_root"),
            "comms_root": self._task_runtime_value(all_tasks, "comms_root"),
            "target_output_dir": self._task_runtime_value(all_tasks, "target_output_dir"),
            "output_root": self._task_runtime_value(all_tasks, "output_root"),
            "user_visible": False,
            "authoritative_output": False,
        }), projection_id=projection_id, turn_type="self_evolution")
        work_item = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id=run_id,
            cell_id=str(root_seat.get("cell_id", "") or team_id or "").strip(),
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=root_role_id,
            seat_id=seat_id,
            seat_state_id=str(root_seat.get("seat_state_id", "") or "").strip(),
            role_runtime_session_id=role_runtime_session_id,
            parent_work_item_id=str(linked_work_item_id_for_task(waiting_task) or "").strip() or None,
            source_role_id=str(waiting_task.assigned_to or waiting_task.metadata.get("work_item_role_id", "") or "").strip() or None,
            source_seat_id=str(waiting_task.metadata.get("delegation_seat_id", "") or "").strip() or None,
            title="Self-Evolution Review",
            summary=str(prompt_contract.get("task_brief", "") or "").strip(),
            kind="self_evolution",
            projection_id=projection_id,
            phase=Phase.READY,
            batch_id=f"self-evolution::{checkpoint.checkpoint_id}",
            manager_role_id=str(root_seat.get("manager_role_id", "") or "").strip(),
            manager_seat_id=str(root_seat.get("manager_seat_id", "") or "").strip(),
            metadata=metadata,
        )
        await self.store.save_delegation_work_item(work_item)
        return work_item

    async def _prepare_self_evolution_runtime_resume_tasks(
        self,
        *,
        tasks: list[Task],
        root_work_item: DelegationWorkItem,
    ) -> None:
        run_id = str(getattr(root_work_item, "run_id", "") or "").strip()
        if not run_id:
            return
        runtime_topology = dict((getattr(root_work_item, "metadata", {}) or {}).get("runtime_topology", {}) or {})
        for task in tasks:
            task.metadata = dict(getattr(task, "metadata", {}) or {})
            changed = False
            defaults = {
                "delegation_run_id": run_id,
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
            }
            if runtime_topology:
                defaults["runtime_topology"] = runtime_topology
            for key, value in defaults.items():
                if task.metadata.get(key) in (None, "", {}, []):
                    task.metadata[key] = value
                    changed = True
            if changed and self.store and hasattr(self.store, "save_task"):
                await self.store.save_task(task)

    async def _collect_company_self_evolution_result(
        self,
        *,
        checkpoint_id: str,
        run_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        recorded: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not self.store or not run_id or not hasattr(self.store, "list_delegation_work_items"):
            return {"recorded": recorded, "errors": errors}
        try:
            work_items = await self.store.list_delegation_work_items(run_id)
        except Exception:
            return {"recorded": recorded, "errors": [{"error": "self_evolution_result_collection_failed"}]}
        for item in list(work_items or []):
            metadata = dict(getattr(item, "metadata", {}) or {})
            if str(metadata.get("self_evolution_checkpoint_id", "") or "").strip() != str(checkpoint_id or "").strip():
                continue
            for entry in list(metadata.get("self_evolution_recorded", []) or []):
                if isinstance(entry, dict):
                    recorded.append(dict(entry))
            error = metadata.get("self_evolution_error")
            if isinstance(error, dict):
                errors.append({
                    "work_item_id": str(getattr(item, "work_item_id", "") or "").strip(),
                    **dict(error),
                })
            phase = getattr(item, "phase", None)
            if phase == Phase.FAILED and not error:
                errors.append({
                    "work_item_id": str(getattr(item, "work_item_id", "") or "").strip(),
                    "error": "self_evolution_work_item_failed",
                })
        return {"recorded": recorded, "errors": errors}

    async def run_company_delivery_self_evolution_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        action: str,
        feedback: str = "",
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        assert self.store
        if str(action or "").strip().lower() == "ignore":
            return await self.ignore_company_delivery_feedback_checkpoint(
                checkpoint,
                reply_metadata=reply_metadata,
            )
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            return "This self-evolution review is no longer active."

        payload = dict(checkpoint.payload or {})
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload.get("task_id", "") or "").strip()
        if not waiting_task_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the delivery task reference is missing."
        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the delivery task no longer exists."

        self._restore_runtime_state_from_checkpoint(waiting_task, payload)
        task_ids = [
            str(task_id).strip()
            for task_id in list(payload.get("task_ids", []) or [waiting_task_id])
            if str(task_id).strip()
        ]
        if waiting_task_id not in task_ids:
            task_ids.append(waiting_task_id)
        tasks: list[Task] = []
        seen_task_ids: set[str] = set()
        for task_id in task_ids:
            task = await self.store.get_task(task_id)
            if task and task.id not in seen_task_ids:
                tasks.append(task)
                seen_task_ids.add(task.id)
        if not tasks:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the runtime task set could not be restored."

        plan = deserialize_company_work_item_runtime_plan(payload.get("company_work_item_plan") or payload.get("plan", {}))
        organization_id = str(
            getattr(waiting_task, "org_id", "")
            or payload.get("organization_id")
            or getattr(getattr(self.config, "org", None), "organization_id", "")
            or DEFAULT_ORGANIZATION_ID
        ).strip() or DEFAULT_ORGANIZATION_ID
        root_role_id = str(
            getattr(plan, "final_decider_role_id", "")
            or plan.metadata.get("final_decider_role_id", "")
            or ""
        ).strip()
        if not root_role_id and self.org_engine:
            try:
                root_role_id = str(self.org_engine.get_final_decider_role_id(strict=False) or "").strip()
            except Exception:
                root_role_id = ""
        if not root_role_id:
            target = self._company_followup_target_task(plan, tasks)
            root_role_id = str(getattr(target, "assigned_to", "") or getattr(target, "metadata", {}).get("work_item_role_id", "") or "").strip()
        if not root_role_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because no final-decider role could be resolved."

        normalized_action = "feedback" if str(action or "").strip().lower() == "feedback" else "approve"
        feedback_text = str(feedback or "").strip()
        source = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": "company_delivery_feedback",
            "human_action": normalized_action,
            "human_feedback": feedback_text,
            "project_id": waiting_task.project_id or self.project_id or "default",
            "delivery_task_id": waiting_task.id,
            "delivery_projection_id": projection_id_for_task(waiting_task),
            "recorded_at": datetime.now().isoformat(),
        }
        assignments = self._self_evolution_assignments_by_role(tasks)
        if self.company_executor is None:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because company runtime is not available."
        root_work_item = await self._create_company_self_evolution_root_work_item(
            checkpoint=checkpoint,
            waiting_task=waiting_task,
            tasks=tasks,
            plan=plan,
            root_role_id=root_role_id,
            organization_id=organization_id,
            source=source,
            assignments=assignments,
        )
        if root_work_item is None:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the company runtime work-item state could not be restored."

        await self._close_company_delivery_review_task(
            waiting_task,
            resolution="self_evolution_started",
            closed_at=datetime.now().isoformat(),
            checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
            metadata_updates={
                "self_evolution_review_started": True,
                "self_evolution_review_started_at": datetime.now().isoformat(),
                "self_evolution_root_work_item_id": root_work_item.work_item_id,
            },
        )
        if waiting_task.id not in seen_task_ids:
            tasks.append(waiting_task)
            seen_task_ids.add(waiting_task.id)
        await self._prepare_self_evolution_runtime_resume_tasks(
            tasks=tasks,
            root_work_item=root_work_item,
        )
        await self.company_executor.execute(plan, tasks)
        result = await self._collect_company_self_evolution_result(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=str(getattr(root_work_item, "run_id", "") or "").strip(),
        )

        waiting_task.metadata = dict(waiting_task.metadata or {})
        review_record = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "action": normalized_action,
            "feedback": feedback_text,
            "completed_at": datetime.now().isoformat(),
            "recorded_count": len(result.get("recorded", [])),
            "error_count": len(result.get("errors", [])),
        }
        history = list(waiting_task.metadata.get("self_evolution_reviews", []) or [])
        history.append(review_record)
        task_metadata_updates = {
            "self_evolution_review_completed": True,
            "self_evolution_review_completed_at": review_record["completed_at"],
            "latest_self_evolution_review": review_record,
            "self_evolution_reviews": history[-20:],
        }
        await self._terminalize_company_delivery_feedback_checkpoint(
            checkpoint,
            status="resolved",
            resolution="self_evolution_review_completed",
            payload_updates={
                **payload,
                "self_evolution_review": review_record,
                "self_evolution_recorded": list(result.get("recorded", [])),
                "self_evolution_errors": list(result.get("errors", [])),
            },
            task_metadata_updates=task_metadata_updates,
        )
        recorded_count = len(result.get("recorded", []))
        if recorded_count:
            return f"Self-evolution completed. Recorded {recorded_count} employee experience update(s)."
        errors = list(result.get("errors", []))
        if errors:
            return "Self-evolution finished without writing updates because the agents did not return valid evolution patches."
        return "Self-evolution completed. No employee experience updates were needed."

    def _self_evolution_assignments_by_role(self, tasks: list[Task]) -> dict[str, dict[str, Any]]:
        assignments: dict[str, dict[str, Any]] = {}
        for task in tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "") or "").strip()
            role_id = str(
                assignment.get("role_id")
                or getattr(task, "assigned_to", "")
                or getattr(task, "metadata", {}).get("work_item_role_id", "")
                or ""
            ).strip()
            if not employee_id or not role_id:
                continue
            assignments.setdefault(role_id, {
                "employee_id": employee_id,
                "employee_name": assignment.get("name", ""),
                "template_id": assignment.get("template_id", ""),
                "role_id": role_id,
                "category": assignment.get("category", ""),
                "domains": list(assignment.get("domains", []) or []),
            })
        return assignments

    def _self_evolution_task_payloads(self, tasks: list[Task]) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for task in tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            result_content = ""
            if isinstance(task.result, dict):
                result_content = str(task.result.get("content", "") or "").strip()
            elif task.result:
                result_content = str(task.result or "").strip()
            payloads.append({
                "task_id": task.id,
                "title": task.title,
                "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                "employee_id": str(assignment.get("employee_id", "") or "").strip(),
                "projection_id": projection_id_for_task(task),
                "turn_type": turn_type_for_task(task, fallback=""),
                "status": getattr(task.status, "value", str(task.status)),
                "summary": str(task.metadata.get("work_item_summary_for_downstream", "") or result_content).strip()[:2000],
            })
        return payloads

    def _self_evolution_org_graph(self) -> dict[str, list[str]]:
        if not self.org_engine:
            return {}
        graph: dict[str, list[str]] = {}
        for agent in self.org_engine.list_agents():
            role_id = str(agent.role_id or "").strip()
            if role_id:
                graph.setdefault(role_id, [])
        for agent in self.org_engine.list_agents():
            role_id = str(agent.role_id or "").strip()
            manager = str(agent.reports_to or "").strip()
            if role_id and manager and manager in graph:
                graph.setdefault(manager, [])
                if role_id not in graph[manager]:
                    graph[manager].append(role_id)
        return graph

    def _normalize_staffing_selection(self, value: Any) -> dict[str, str]:
        if isinstance(value, dict):
            kind = str(value.get("kind") or value.get("source") or "").strip().lower()
            selected_id = str(
                value.get("id")
                or value.get("employee_id")
                or value.get("template_id")
                or ""
            ).strip()
        else:
            text = str(value or "").strip()
            if ":" in text:
                kind, selected_id = text.split(":", 1)
                kind = kind.strip().lower()
                selected_id = selected_id.strip()
            else:
                kind, selected_id = text.strip().lower(), ""
        if kind in {"emp", "employee", "existing"}:
            return {"kind": "employee", "id": selected_id}
        if kind in {"tpl", "template", "talent"}:
            return {"kind": "template", "id": selected_id}
        if kind in {"fallback", "role", "role-only", "role_only", "none", ""}:
            return {"kind": "fallback", "id": ""}
        return {"kind": kind, "id": selected_id}

    @staticmethod
    def _normalize_staffing_experience_mode(value: Any) -> str:
        return "template_only" if str(value or "").strip() == "template_only" else "with_experience"

    def _parse_cli_staffing_selection_overrides(self, reply: str) -> dict[str, dict[str, str]]:
        overrides: dict[str, dict[str, str]] = {}
        for token in re.split(r"[\s,]+", reply.strip()):
            if "=" not in token:
                continue
            raw_role_id, raw_selection = token.split("=", 1)
            role_id = raw_role_id.strip()
            if not role_id:
                continue
            selection = self._normalize_staffing_selection(raw_selection)
            if selection.get("kind") in {"employee", "template", "fallback"}:
                overrides[role_id] = selection
        return overrides

    def _staffing_role_agent_overrides(
        self,
        payload: dict[str, Any],
        reply_metadata: dict[str, Any] | None,
    ) -> dict[str, str]:
        overrides: dict[str, str] = {}
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            if not role_id:
                continue
            selected = normalize_recruitment_agent_choice(
                role.get("selected_agent"),
                default=str(role.get("default_agent", "") or "codex"),
            )
            if selected:
                overrides[role_id] = selected
        raw_role_agents = dict(reply_metadata or {}).get("recruitment_role_agents")
        if isinstance(raw_role_agents, dict):
            for raw_role_id, raw_agent in raw_role_agents.items():
                role_id = str(raw_role_id or "").strip()
                agent = normalize_recruitment_agent_choice(raw_agent)
                if role_id and agent:
                    overrides[role_id] = agent
        return overrides

    async def _resume_staffing_selection_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        assert self.store and self.talent_market
        payload = checkpoint.payload
        original_message = str(payload.get("original_message", ""))
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume staffing because the original request is missing."

        reply = user_reply.strip()
        normalized = reply.lower()
        reply_metadata = dict(reply_metadata or {})
        staffing_action = str(reply_metadata.get("staffing_action", "") or "").strip().lower()
        approved_tokens = {"1", "y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        auto_tokens = {"auto", "auto_recruit", "auto recruit", "automatic", "automatic recruitment", "recruit"}
        denied_tokens = {"2", "n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}

        decision = self._deserialize_router_decision(dict(payload.get("decision", {})))
        if payload.get("runtime_spec"):
            runtime_spec = deserialize_company_runtime_spec(dict(payload.get("runtime_spec", {})))
        else:
            runtime_spec = CompanyRuntimeSpec(
                profile=str(payload.get("company_profile", "") or getattr(decision, "company_profile", "") or CompanyProfile.CORPORATE.value),
                original_request=original_message,
                runtime_model="multi_team_org",
                work_item_driven=True,
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": original_message,
                },
            )
        session_id = str(payload.get("primary_session_id") or checkpoint.session_id or str(uuid.uuid4()))
        origin_channel = str(payload.get("origin_channel", "cli"))
        origin_chat_id = str(payload.get("origin_chat_id", ""))
        origin_thread_id = str(payload.get("origin_thread_id", ""))
        origin_task_id = str(payload.get("origin_task_id", "")).strip() or None
        attachment_refs = self._normalize_attachment_refs(payload.get("attachment_refs", []))

        if staffing_action == "auto_recruit" or normalized in auto_tokens:
            role_agent_overrides = self._staffing_role_agent_overrides(payload, reply_metadata)
            recruitment_agent = normalize_recruitment_agent_choice(
                reply_metadata.get("recruitment_agent") or payload.get("recruitment_agent"),
                default="native",
            ) or "native"
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return await self._begin_company_recruitment_loop(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_confirmation=True,
                role_agent_overrides=role_agent_overrides,
                recruitment_agent=recruitment_agent,
            )

        if normalized in denied_tokens or staffing_action == "deny":
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return "Manual staffing was cancelled. Execution will not continue."

        cli_overrides = self._parse_cli_staffing_selection_overrides(reply)
        is_approve = (
            staffing_action in {"manual_approve", "approve"}
            or normalized in approved_tokens
            or normalized.startswith("approve ")
            or bool(cli_overrides)
        )
        if not is_approve:
            return self._render_manual_staffing_summary(payload)

        role_ids = {
            str(role.get("role_id", "") or "").strip()
            for role in list(payload.get("staffing_roles", []) or [])
            if str(role.get("role_id", "") or "").strip()
        }
        selections: dict[str, dict[str, str]] = {}
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            if not role_id:
                continue
            selections[role_id] = self._normalize_staffing_selection(role.get("default_selection", {}))

        raw_metadata_selections = reply_metadata.get("staffing_selections")
        if isinstance(raw_metadata_selections, dict):
            for raw_role_id, raw_selection in raw_metadata_selections.items():
                role_id = str(raw_role_id or "").strip()
                if role_id in role_ids:
                    selections[role_id] = self._normalize_staffing_selection(raw_selection)
        for role_id, selection in cli_overrides.items():
            if role_id in role_ids:
                selections[role_id] = selection

        active_employees = self._active_staffing_employees_by_id()
        available_templates = {
            str(getattr(template, "id", "") or "").strip(): template
            for template in self.talent_market.list_available_templates()
            if str(getattr(template, "id", "") or "").strip()
        }

        staffing_overrides: dict[str, str] = {}
        staffing_experience_modes: dict[str, str] = {}
        fallback_role_ids: set[str] = set()
        hired_messages: list[str] = []
        errors: list[str] = []
        for role_id in sorted(role_ids):
            selection = selections.get(role_id, {"kind": "fallback", "id": ""})
            kind = selection.get("kind", "fallback")
            selected_id = str(selection.get("id", "") or "").strip()
            if kind == "employee":
                if selected_id not in active_employees:
                    errors.append(f"Role `{role_id}` selected unknown employee `{selected_id}`.")
                    continue
                staffing_overrides[role_id] = self._canonical_staffing_employee_id(active_employees[selected_id], selected_id)
                staffing_experience_modes[role_id] = "with_experience"
                continue
            if kind == "template":
                if selected_id not in available_templates:
                    errors.append(f"Role `{role_id}` selected unknown template `{selected_id}`.")
                    continue
                employee = self.talent_market.ensure_hire_template(
                    selected_id,
                    role_id,
                    employee_name=str(getattr(available_templates[selected_id], "name", "") or ""),
                )
                staffing_overrides[role_id] = employee.employee_id
                staffing_experience_modes[role_id] = "template_only"
                hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {role_id}")
                continue
            fallback_role_ids.add(role_id)

        if errors:
            return "Could not apply manual staffing:\n" + "\n".join(f"- {item}" for item in errors) + "\n\n" + self._render_manual_staffing_summary(payload)

        self.config.save(self.opc_home / "config")
        if self.org_engine:
            self.org_engine.reload_from_config()
        await self._mark_session_recruitment_confirmation_completed(
            session_id,
            source="manual_staffing_approved",
        )
        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        role_agent_overrides = self._staffing_role_agent_overrides(payload, reply_metadata)
        self._save_project_company_staffing_defaults(
            decision,
            company_profile=str(runtime_spec.profile or payload.get("company_profile", "") or CompanyProfile.CORPORATE.value),
            role_ids=role_ids,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        if decision.mode == ExecutionMode.COMPANY_MODE:
            result = await self._continue_company_mode_execution(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        else:
            result = await self._continue_task_mode_execution(
                decision,
                original_message,
                None,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        if not hired_messages:
            return result
        return "Approved manual staffing.\n" + "\n".join(hired_messages) + "\n\n" + result

    async def _resume_recruitment_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        assert self.store and self.company_recruiter and self.talent_market
        payload = checkpoint.payload
        original_message = str(payload.get("original_message", ""))
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume recruitment because the original request is missing."

        reply = user_reply.strip()
        normalized = reply.lower()
        reply_metadata = dict(reply_metadata or {})
        reply_kind = str(reply_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
        approved_tokens = {"1", "y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"2", "n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        decision = self._deserialize_router_decision(dict(payload.get("decision", {})))
        if payload.get("runtime_spec"):
            runtime_spec = deserialize_company_runtime_spec(dict(payload.get("runtime_spec", {})))
        else:
            profile = str(
                payload.get("company_profile")
                or payload.get("profile")
                or getattr(decision, "company_profile", "")
                or CompanyProfile.CORPORATE.value
            ).strip() or CompanyProfile.CORPORATE.value
            runtime_spec = CompanyRuntimeSpec(
                profile=profile,
                original_request=original_message,
                runtime_model="multi_team_org",
                work_item_driven=True,
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": original_message,
                },
            )
        recruitment_plan = build_recruitment_plan_from_payload(dict(payload.get("recruitment_plan", {})))
        apply_recruitment_role_agent_overrides(
            recruitment_plan,
            payload.get("recruitment_role_agents"),
        )
        apply_recruitment_role_agent_overrides(
            recruitment_plan,
            reply_metadata.get("recruitment_role_agents"),
        )
        recruitment_agent = normalize_recruitment_agent_choice(
            reply_metadata.get("recruitment_agent")
            or payload.get("recruitment_agent")
            or dict(getattr(recruitment_plan, "metadata", {}) or {}).get("recruitment_agent"),
            default="native",
        ) or "native"
        role_agent_overrides = extract_recruitment_role_agent_overrides(recruitment_plan)
        fallback_role_ids = build_fallback_role_ids(recruitment_plan)
        session_id = str(payload.get("primary_session_id") or checkpoint.session_id or str(uuid.uuid4()))
        origin_channel = str(payload.get("origin_channel", "cli"))
        origin_chat_id = str(payload.get("origin_chat_id", ""))
        origin_thread_id = str(payload.get("origin_thread_id", ""))
        origin_task_id = str(payload.get("origin_task_id", "")).strip() or None
        attachment_refs = self._normalize_attachment_refs(payload.get("attachment_refs", []))

        if reply_kind == "approve" or (reply_kind != "feedback" and normalized in approved_tokens):
            hired_messages: list[str] = []
            raw_staffing_selections = reply_metadata.get("staffing_selections")
            if isinstance(raw_staffing_selections, dict) and raw_staffing_selections:
                role_ids = {
                    str(proposal.role_id or "").strip()
                    for proposal in list(recruitment_plan.proposals or [])
                    if str(proposal.role_id or "").strip()
                }
                selections: dict[str, dict[str, str]] = {}
                proposal_by_role = {
                    str(proposal.role_id or "").strip(): proposal
                    for proposal in list(recruitment_plan.proposals or [])
                    if str(proposal.role_id or "").strip()
                }
                for role_id, proposal in proposal_by_role.items():
                    if proposal.existing_employee and proposal.existing_employee.employee_id:
                        selections[role_id] = {
                            "kind": "employee",
                            "id": proposal.existing_employee.employee_id,
                        }
                    elif proposal.candidate and proposal.candidate.template_id:
                        selections[role_id] = {
                            "kind": "template",
                            "id": proposal.candidate.template_id,
                        }
                    else:
                        selections[role_id] = {"kind": "fallback", "id": ""}
                for raw_role_id, raw_selection in raw_staffing_selections.items():
                    role_id = str(raw_role_id or "").strip()
                    if role_id in role_ids:
                        selections[role_id] = self._normalize_staffing_selection(raw_selection)

                active_employees = self._active_staffing_employees_by_id()
                available_templates = {
                    str(getattr(template, "id", "") or "").strip(): template
                    for template in self.talent_market.list_available_templates()
                    if str(getattr(template, "id", "") or "").strip()
                }
                staffing_overrides = {}
                staffing_experience_modes = {}
                fallback_role_ids = set()
                errors: list[str] = []
                for role_id in sorted(role_ids):
                    selection = selections.get(role_id, {"kind": "fallback", "id": ""})
                    kind = selection.get("kind", "fallback")
                    selected_id = str(selection.get("id", "") or "").strip()
                    if kind == "employee":
                        if selected_id not in active_employees:
                            errors.append(f"Role `{role_id}` selected unknown employee `{selected_id}`.")
                            continue
                        staffing_overrides[role_id] = self._canonical_staffing_employee_id(active_employees[selected_id], selected_id)
                        staffing_experience_modes[role_id] = "with_experience"
                        continue
                    if kind == "template":
                        if selected_id not in available_templates:
                            errors.append(f"Role `{role_id}` selected unknown template `{selected_id}`.")
                            continue
                        proposal = proposal_by_role.get(role_id)
                        use_proposed_identity = bool(
                            proposal
                            and proposal.candidate
                            and proposal.candidate.template_id == selected_id
                        )
                        employee = self.talent_market.ensure_hire_template(
                            selected_id,
                            role_id,
                            employee_name=(
                                proposal.candidate.proposed_employee_name
                                if use_proposed_identity and proposal and proposal.candidate
                                else str(getattr(available_templates[selected_id], "name", "") or "")
                            ),
                            employee_id=(
                                proposal.candidate.proposed_employee_id
                                if use_proposed_identity and proposal and proposal.candidate
                                else ""
                            ),
                        )
                        if use_proposed_identity and proposal and proposal.candidate:
                            proposal.candidate.proposed_employee_id = employee.employee_id
                            proposal.candidate.proposed_employee_name = employee.name
                        staffing_overrides[role_id] = employee.employee_id
                        staffing_experience_modes[role_id] = "template_only"
                        hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {role_id}")
                        continue
                    fallback_role_ids.add(role_id)
                if errors:
                    return (
                        "Could not apply recruitment staffing:\n"
                        + "\n".join(f"- {item}" for item in errors)
                        + "\n\n"
                        + (recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan))
                    )
            else:
                for proposal in recruitment_plan.proposals:
                    if proposal.status != "proposed_hire" or not proposal.candidate:
                        continue
                    employee = self.talent_market.ensure_hire_template(
                        proposal.candidate.template_id,
                        proposal.role_id,
                        employee_name=proposal.candidate.proposed_employee_name,
                        employee_id=proposal.candidate.proposed_employee_id,
                    )
                    proposal.candidate.proposed_employee_id = employee.employee_id
                    proposal.candidate.proposed_employee_name = employee.name
                    hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {employee.role_id}")
                staffing_overrides = build_staffing_overrides(recruitment_plan)
                staffing_experience_modes = build_staffing_experience_modes(recruitment_plan)
            self.config.save(self.opc_home / "config")
            if self.org_engine:
                self.org_engine.reload_from_config()
            await self._mark_session_recruitment_confirmation_completed(
                session_id,
                source="checkpoint_approved",
            )
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            self._save_project_company_staffing_defaults(
                decision,
                company_profile=str(runtime_spec.profile or payload.get("company_profile", "") or CompanyProfile.CORPORATE.value),
                role_ids={
                    *{
                        str(proposal.role_id or "").strip()
                        for proposal in list(recruitment_plan.proposals or [])
                        if str(proposal.role_id or "").strip()
                    },
                    *set(staffing_overrides),
                    *set(fallback_role_ids),
                    *set(role_agent_overrides),
                },
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
            )
            if decision.mode == ExecutionMode.COMPANY_MODE:
                result = await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            else:
                result = await self._continue_task_mode_execution(
                    decision,
                    original_message,
                    None,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            if not hired_messages:
                return result
            return "Approved recruitment plan.\n" + "\n".join(hired_messages) + "\n\n" + result

        if reply_kind == "deny" or (reply_kind != "feedback" and normalized in denied_tokens):
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return "Recruitment was cancelled. Execution will not continue."

        control_tokens = approved_tokens | denied_tokens
        if reply_kind != "feedback" and normalized in control_tokens:
            return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)

        feedback = build_recruitment_feedback(reply)
        if not feedback:
            return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)
        feedback_history = list(recruitment_plan.recruiter_feedback)
        feedback_history.append(feedback)
        recruitment_llm, selected_recruitment_agent = self._resolve_recruitment_llm(recruitment_agent)
        revised_plan = await self.company_recruiter.build_recruitment_plan(
            runtime_spec,
            domains=decision.domains,
            project_id=self.project_id or "default",
            recruiter_feedback=feedback_history,
            recruitment_llm=recruitment_llm,
            recruitment_agent=selected_recruitment_agent,
        )
        apply_recruitment_role_agent_overrides(
            revised_plan,
            extract_recruitment_role_agent_overrides(recruitment_plan),
        )
        try:
            current_revision = int(
                payload.get("recruitment_revision")
                or dict(getattr(recruitment_plan, "metadata", {}) or {}).get("recruitment_revision")
                or 1
            )
        except (TypeError, ValueError):
            current_revision = 1
        next_revision = current_revision + 1
        revised_plan.metadata = dict(getattr(revised_plan, "metadata", {}) or {})
        revised_plan.metadata["recruitment_revision"] = next_revision
        revised_plan.metadata["previous_checkpoint_id"] = checkpoint.checkpoint_id
        revised_plan.metadata["recruitment_agent"] = selected_recruitment_agent
        raw_prior_superseded = payload.get("superseded_checkpoint_ids", [])
        prior_superseded = [
            str(item).strip()
            for item in (raw_prior_superseded if isinstance(raw_prior_superseded, list) else [])
            if str(item).strip()
        ]
        revised_payload = {
            **payload,
            "recruitment_plan": serialize_recruitment_plan(revised_plan),
            "recruiter_feedback": list(feedback_history),
            "previous_checkpoint_id": checkpoint.checkpoint_id,
            "recruitment_revision": next_revision,
            "recruitment_role_agents": extract_recruitment_role_agent_overrides(revised_plan),
            "recruitment_agent": selected_recruitment_agent,
            "superseded_checkpoint_ids": [*prior_superseded, checkpoint.checkpoint_id],
        }
        revised_payload.pop("basis_hash", None)
        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="superseded")
        await self._save_execution_checkpoint(
            {
                "project_id": checkpoint.project_id or self.project_id or "default",
                "session_id": checkpoint.session_id or session_id,
                "checkpoint_type": "company_recruitment_confirmation",
                "task_id": checkpoint.task_id,
                "payload": revised_payload,
            }
        )
        return revised_plan.summary or self.company_recruiter.render_recruitment_summary(revised_plan)

    async def _resume_reorg_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        assert self.store and self.reorg_manager
        payload = checkpoint.payload
        proposal_id = payload.get("proposal_id", "")
        if not proposal_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending reorg because the proposal reference is missing."
        reply = user_reply.strip().lower()
        approved_tokens = {"y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        waiting_task_id = str(payload.get("waiting_task_id", "") or "").strip()
        waiting_task = await self.store.get_task(waiting_task_id) if waiting_task_id else None
        parent_session_id = str(payload.get("parent_session_id", "") or "").strip()
        if not parent_session_id and waiting_task is not None:
            parent_session_id = str(getattr(waiting_task, "parent_session_id", "") or "").strip()
        plan_data = payload.get("company_work_item_plan") or payload.get("work_item_runtime_plan") or {}
        if plan_data:
            base_plan = deserialize_company_work_item_runtime_plan(plan_data)
        else:
            profile = self.org_engine.get_company_profile() if self.org_engine else "corporate"
            if self.org_engine:
                try:
                    base_plan = self.org_engine.build_company_work_item_runtime_plan(
                        profile=profile,
                        runtime_topology=self.org_engine.build_runtime_delegation_topology(),
                        original_request=str(payload.get("original_message", "") or ""),
                    )
                except ValueError:
                    base_plan = CompanyWorkItemRuntimePlan(
                        profile=profile,
                        metadata={"execution_model": "multi_team_org", "work_item_driven": True},
                    )
            else:
                base_plan = CompanyWorkItemRuntimePlan(
                    profile=profile,
                    metadata={"execution_model": "multi_team_org", "work_item_driven": True},
                )
        if reply in approved_tokens:
            await self.reorg_manager.set_reorg_approval(proposal_id, approved=True, notes=user_reply.strip())
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            result = await self.reorg_manager.apply_reorg(proposal_id)
            if waiting_task is not None:
                waiting_task = await self.store.get_task(waiting_task.id) or waiting_task
                self._clear_pending_reorg_marker(waiting_task)
                if waiting_task.status not in {TaskStatus.CANCELLED, TaskStatus.DONE}:
                    waiting_task.status = TaskStatus.PENDING
                    waiting_task.result = None
                waiting_task.metadata = dict(waiting_task.metadata)
                progress = list(waiting_task.metadata.get("progress_log", []))
                progress.append(f"Approved runtime replan `{proposal_id}` and refreshed the runtime.")
                waiting_task.metadata["progress_log"] = progress
                await self.store.save_task(waiting_task)
            if parent_session_id and self.company_executor:
                profile = self.org_engine.get_company_profile() if self.org_engine else base_plan.profile
                if self.org_engine:
                    try:
                        current_plan = self.org_engine.build_company_work_item_runtime_plan(
                            profile=profile,
                            runtime_topology=self.org_engine.build_runtime_delegation_topology(),
                            original_request=str(payload.get("original_message", "") or ""),
                        )
                    except ValueError:
                        current_plan = base_plan
                else:
                    current_plan = base_plan
                reconciled = await self._reconcile_company_work_item_plan_state(
                    parent_session_id,
                    current_plan,
                )
                if reconciled:
                    plan, tasks = reconciled
                    resumed = await self.company_executor.execute(plan, tasks)
                    return (
                        f"Reorg `{proposal_id}` approved and applied.\n"
                        f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                        f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}\n\n"
                        f"{resumed}"
                    ).strip()
            return (
                f"Reorg `{proposal_id}` approved and applied.\n"
                f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}"
            )
        if reply in denied_tokens:
            await self.reorg_manager.set_reorg_approval(proposal_id, approved=False, notes=user_reply.strip())
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            if waiting_task is not None:
                self._clear_pending_reorg_marker(waiting_task)
                waiting_task.result = {
                    "content": f"Runtime replan `{proposal_id}` was denied.",
                    "artifacts": {},
                }
                waiting_task.metadata = dict(waiting_task.metadata)
                progress = list(waiting_task.metadata.get("progress_log", []))
                progress.append(f"Denied runtime replan `{proposal_id}`.")
                waiting_task.metadata["progress_log"] = progress
                await self._fail_task_via_phase(
                    waiting_task,
                    reason=f"reorg_denied:{proposal_id}",
                )
                return f"Reorg `{proposal_id}` was denied. The proposing work item was halted and the current runtime remains unchanged."
            return f"Reorg `{proposal_id}` was denied. The current company architecture remains unchanged."
        return (
            "There is a pending company reorg waiting for confirmation. "
            "Reply with `approve` / `continue` to apply it, or `deny` / `stop` to reject it."
        )

    async def _maybe_handle_reorg_message(self, content: str, session_id: str | None) -> str | None:
        assert self.reorg_manager and self.store
        stripped = content.strip()
        if not stripped.lower().startswith("reorg "):
            return None
        match = re.match(r"^reorg\s+(propose|approve|deny|apply|show|adjust)\b(.*)$", stripped, re.IGNORECASE | re.DOTALL)
        if not match:
            return "Unsupported reorg command. Use `reorg propose|approve|deny|apply|show|adjust`."
        action = match.group(1).lower()
        remainder = match.group(2).strip()
        project_id = self.project_id or "default"

        if action == "show":
            proposal = await self.store.get_reorg_proposal(remainder)
            if not proposal:
                return f"Unknown reorg proposal `{remainder}`."
            return self._format_reorg_summary(proposal)
        if action in {"approve", "deny"}:
            proposal = await self.approve_company_reorg(
                proposal_id=remainder,
                approved=(action == "approve"),
                notes=f"Explicit {action} via process_message.",
            )
            if action == "approve":
                result = await self.apply_company_reorg(remainder)
                return (
                    f"{self._format_reorg_summary(proposal)}\n\n"
                    f"Applied with {len(result.get('migration_summary', {}).get('migrated_task_ids', []))} migrated tasks."
                )
            return self._format_reorg_summary(proposal)
        if action == "apply":
            result = await self.apply_company_reorg(remainder)
            return (
                f"Applied reorg `{remainder}`.\n"
                f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}"
            )

        parsed = self._parse_reorg_payload(remainder)
        if parsed is None:
            return "Reorg payload must be valid JSON after the command."
        if action == "propose":
            proposal = await self.propose_company_reorg(
                summary=str(parsed.get("summary", "Runtime company reorg")),
                rationale=str(parsed.get("rationale", parsed.get("summary", ""))),
                title=str(parsed.get("title", "")),
                changeset=parsed.get("changeset", {}),
                session_id=session_id,
                task_id=parsed.get("task_id"),
                initiated_by=str(parsed.get("initiated_by", "owner")),
                source_role_id=str(parsed.get("source_role_id", "")),
                metadata={"source": "process_message"},
            )
            if proposal.user_confirmation_required:
                await self._save_reorg_checkpoint(proposal)
                return (
                    f"{self._format_reorg_summary(proposal)}\n\n"
                    "This reorg changes the company architecture and requires user confirmation. "
                    "Reply `approve` or `deny`, or use `reorg approve <proposal_id>`."
                )
            return self._format_reorg_summary(proposal)
        if action == "adjust":
            result = await self.suggest_task_adjustment(
                summary=str(parsed.get("summary", "Task adjustment")),
                source_role_id=str(parsed.get("source_role_id", "coordinator")),
                changeset=parsed.get("changeset", {}),
                session_id=session_id,
                task_id=parsed.get("task_id"),
            )
            proposal = result["proposal"]
            return (
                f"{self._format_reorg_summary(proposal)}\n\n"
                f"Auto applied: {'yes' if result.get('auto_applied') else 'no'}"
            )
        return None

    def _parse_reorg_payload(self, payload: str) -> dict[str, Any] | None:
        try:
            data = json.loads(payload)
        except Exception:
            return None
        # ``json.loads`` accepts any JSON type; callers do ``parsed.get(...)`` and crash
        # (AttributeError) on a non-dict value such as ``reorg propose 42`` or ``[1,2]``.
        return data if isinstance(data, dict) else None

    async def _save_reorg_checkpoint(self, proposal: ReorgProposal) -> None:
        await self._save_execution_checkpoint(
            {
                "project_id": proposal.project_id,
                "session_id": proposal.session_id,
                "checkpoint_type": "company_reorg_pending",
                "task_id": proposal.task_id,
                "payload": {
                    "proposal_id": proposal.proposal_id,
                    "org_version": proposal.old_org_version,
                    "runtime_topology_version": proposal.old_runtime_topology_version,
                },
            }
        )

    def _format_reorg_summary(self, proposal: ReorgProposal) -> str:
        return (
            f"Reorg proposal `{proposal.proposal_id}`\n"
            f"Status: {proposal.status.value}\n"
            f"Scope: {proposal.scope.value}\n"
            f"Risk: {proposal.risk_level.value}\n"
            f"Summary: {proposal.summary}\n"
            f"Needs user confirmation: {'yes' if proposal.user_confirmation_required else 'no'}"
        )

    async def propose_company_reorg(
        self,
        *,
        summary: str,
        changeset: ReorgChangeSet | dict[str, Any],
        rationale: str = "",
        title: str = "",
        session_id: str | None = None,
        task_id: str | None = None,
        initiated_by: str = "owner",
        source_role_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ReorgProposal:
        assert self.reorg_manager
        proposal = await self.reorg_manager.propose_reorg(
            project_id=self.project_id or "default",
            summary=summary,
            rationale=rationale,
            title=title,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            changeset=changeset,
            session_id=session_id,
            task_id=task_id,
            metadata=metadata,
        )
        return proposal

    async def approve_company_reorg(
        self,
        proposal_id: str,
        *,
        approved: bool,
        notes: str = "",
    ) -> ReorgProposal:
        assert self.reorg_manager
        return await self.reorg_manager.set_reorg_approval(proposal_id, approved=approved, notes=notes)

    async def apply_company_reorg(self, proposal_id: str) -> dict[str, Any]:
        assert self.reorg_manager
        return await self.reorg_manager.apply_reorg(proposal_id)

    async def show_company_reorg(self, proposal_id: str) -> ReorgProposal | None:
        assert self.store
        return await self.store.get_reorg_proposal(proposal_id)

    async def suggest_task_adjustment(
        self,
        *,
        summary: str,
        source_role_id: str,
        changeset: ReorgChangeSet | dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        assert self.reorg_manager
        return await self.reorg_manager.suggest_task_adjustment(
            project_id=self.project_id or "default",
            source_role_id=source_role_id,
            summary=summary,
            changeset=changeset,
            session_id=session_id,
            task_id=task_id,
        )

    # --- Public API ---

    @staticmethod
    def _normalize_requested_mode(value: Any) -> str:
        normalized = str(value or "task").strip().lower()
        if normalized == "project":
            return "task"
        if normalized == "company":
            return "company"
        return "task"

    @staticmethod
    def _is_delegate_usable(delegate: "OPCEngine") -> bool:
        """A cached delegate is only reusable while its store connection is open."""
        store = getattr(delegate, "store", None)
        return bool(store is None or getattr(store, "is_ready", True))

    async def _get_project_delegate(self, project_id: str) -> OPCEngine:
        """Return an initialized engine dedicated to ``project_id``.

        A live engine owns one store/memory/runtime context.  When callers reuse
        an initialized engine for another project, delegate instead of rebinding
        the active store under in-flight sessions.
        """
        normalized_project_id = str(project_id or "").strip() or "default"
        current_project_id = str(self.project_id or "default").strip() or "default"
        if normalized_project_id == current_project_id:
            return self
        existing = self._project_engine_delegates.get(normalized_project_id)
        if existing is not None and self._is_delegate_usable(existing):
            return existing
        if self._project_delegate_lock is None:
            self._project_delegate_lock = asyncio.Lock()
        async with self._project_delegate_lock:
            existing = self._project_engine_delegates.get(normalized_project_id)
            if existing is not None:
                if self._is_delegate_usable(existing):
                    return existing
                # Store was closed (e.g. project deleted then re-created with
                # the same id) — drop the stale delegate and build a fresh one.
                self._project_engine_delegates.pop(normalized_project_id, None)
                logger.warning(
                    f"Discarding stale project delegate for '{normalized_project_id}' (store closed)"
                )
            try:
                delegate_config = copy.deepcopy(self.config)
            except Exception:
                delegate_config = self.config
            delegate = OPCEngine(
                config=delegate_config,
                opc_home=self.opc_home,
                project_id=normalized_project_id,
                active_task_run_registry=self._active_task_run_registry,
                owns_active_task_run_registry=False,
                on_progress=self.on_progress,
                on_runtime_event=self.on_runtime_event,
                on_escalation=self.on_escalation,
            )
            delegate.on_company_runtime_children = self.on_company_runtime_children
            delegate.on_company_kanban_callback_factory = self.on_company_kanban_callback_factory
            await delegate.initialize()
            self._project_engine_delegates[normalized_project_id] = delegate
            return delegate

    async def process_message(
        self,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        mode: str = "task",
        org_id: str | None = None,
        preferred_agent: str | None = None,
        domains: list[str] | None = None,
        company_profile: str | None = None,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        message_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Process a user message and return the response.

        Args:
            mode: ``"task"`` (default), ``"company"``, or ``"org"``. Legacy
                ``"project"`` maps to task and ``"custom"`` maps to org.
            org_id: Organization ID for isolated org mode.
            preferred_agent: ``"native"``, ``"claude_code"``, ``"cursor"``, ``"codex"``, or ``"opencode"``.
            domains: Domain hints (e.g. ``["coding", "frontend"]``).
            company_profile: ``"corporate"`` for company mode or ``"custom"``
                as a legacy org-mode alias.
            attachment_refs: Optional lightweight attachment references from Office UI.
            message_metadata: Optional metadata to preserve UI message identity across
                transcript sync and chat rendering.
        """
        target_project_id = str(project_id or "").strip() or None
        current_project_id = str(self.project_id or "default").strip() or "default"
        if self._initialized and target_project_id and target_project_id != current_project_id:
            delegate = await self._get_project_delegate(target_project_id)
            return await delegate.process_message(
                content,
                project_id=target_project_id,
                session_id=session_id,
                mode=mode,
                org_id=org_id,
                preferred_agent=preferred_agent,
                domains=domains,
                company_profile=company_profile,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                message_metadata=message_metadata,
            )
        if target_project_id is not None:
            self.project_id = target_project_id
            if self.memory:
                self.memory.set_project(target_project_id)
            self._ensure_attachment_store()
        if not self._initialized:
            await self.initialize()
        await self._refresh_runtime_config_from_disk()

        requested_mode = str(mode or "task").strip().lower()
        company_profile_value = str(company_profile or "").strip().lower()
        if requested_mode in {"org", "custom"} or (
            requested_mode == "company" and company_profile_value == "custom"
        ):
            from opc.layer2_organization.custom_runtime import CustomRuntimeRunner

            return await CustomRuntimeRunner(self).process_message(
                content,
                project_id=self.project_id or target_project_id or "default",
                session_id=session_id,
                org_id=org_id,
                preferred_agent=preferred_agent,
                domains=domains,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                message_metadata=message_metadata,
            )

        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        merged_message_metadata = {
            "mode": mode,
            "org_id": org_id,
            "preferred_agent": preferred_agent,
            "domains": domains or [],
            "company_profile": company_profile,
            "origin_task_id": origin_task_id,
            "attachment_refs": attachment_refs,
        }
        if message_metadata:
            merged_message_metadata.update(dict(message_metadata))

        message = UserMessage(
            channel="cli",
            user_id="owner",
            content=content,
            attachments=attachment_refs,
            session_id=session_id or str(uuid.uuid4()),
            project_context=self.project_id,
            metadata=merged_message_metadata,
        )

        response = await self.message_bus.process_single(message)
        if response:
            return response.content
        return "No response generated."

    async def process_secretary_message(
        self,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a direct secretary message and return the structured response."""
        if project_id is not None:
            self.project_id = project_id
            if self.memory:
                self.memory.set_project(project_id)
        if not self._initialized:
            await self.initialize()
        await self._refresh_runtime_config_from_disk()
        assert self.secretary
        return await self.secretary.handle_message(
            content,
            project_id=self.project_id,
            session_id=session_id,
        )

    async def shutdown(self) -> None:
        """Clean shutdown of all subsystems."""
        self._shutting_down = True
        logger.info("Shutting down OPC Engine...")
        if self._owns_active_task_run_registry:
            await self.prepare_active_company_runtimes_for_shutdown()
        delegates = list(self._project_engine_delegates.values())
        self._project_engine_delegates.clear()
        for delegate in delegates:
            try:
                await delegate.shutdown()
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to shut down project delegate {}",
                    getattr(delegate, "project_id", None),
                )
        if self.comms_reactivation_sweeper:
            await self.comms_reactivation_sweeper.stop()
        if self.heartbeat_scheduler:
            await self.heartbeat_scheduler.stop()
        self.message_bus.stop()
        if self.channel_manager:
            await self.channel_manager.stop_all()
        if self.mcp_manager:
            await self.mcp_manager.shutdown()
        if self.store and self._owns_store:
            await self.store.close()

        if self.llm:
            stats = self.llm.stats
            logger.info(
                f"Session stats: tokens_in={stats['tokens_in']}, "
                f"tokens_out={stats['tokens_out']}, "
                f"estimated_cost=${stats['estimated_cost']:.4f}"
            )

        logger.info("OPC Engine shut down.")
