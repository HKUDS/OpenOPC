"""OPC Native Agent — the primary agent implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.company_tools import (
    COMPANY_ALL_COLLABORATION_TOOL_NAMES,
    MULTI_TEAM_COORDINATION_TURN_MODES,
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from opc.core.config import OPCConfig
from opc.core.models import AgentInfo, AgentStatus, ExecutionMode, Task, TaskResult, TaskStatus
from opc.core.events import EventBus
from opc.core.models import OPCEvent
from opc.core.worker_envelope import classify_worker_message
from opc.llm.provider import LLMProvider
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer3_agent.company_runtime_contract import build_company_work_item_contract
from opc.layer3_agent.runtime_v2 import NativeRuntimeV2
from opc.layer3_agent.prompt_harness import PromptHarnessBuilder
from opc.layer3_agent.prompt_harness.builder import _final_decider_role_id, _memory_skill_user_facing
from opc.layer4_tools.output_budget import clip_text
from opc.layer4_tools.registry import ToolRegistry
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.skill_library import SkillLibrary
from opc.layer6_observability.cost_tracker import CostTracker
from opc.layer3_agent.prompt_harness.sections import (
    HONEST_REPORTING_CONTRACT,
    LONG_RUNNING_SESSION_CONTRACT,
    MEMORY_TRUST_CONTRACT,
    SAFE_ACTIONS_CONTRACT,
    SUBAGENT_HARNESS_CONTRACT,
)

# ---------------------------------------------------------------------------
# Role-aware system prompt components
# ---------------------------------------------------------------------------

_CORE_HEADER = (
    "You are {role_name}, an AI agent in the OPC (One-Person Company) system.\n"
    "Role: {responsibility}\n\n"
    "You accomplish tasks by using the tools available to your role."
)

_TASK_MODE_CORE_HEADER = (
    "You are {role_name}, an OpenOPC task execution agent.\n"
    "Role: {responsibility}\n\n"
    "You accomplish standalone user tasks by using the tools available to your role."
)

_CORE_OPERATING_PRINCIPLES = """
## Core Operating Principles
- Use available context and tools before asking the user for missing information.
- Own the user's goal within the explicit scope and keep moving with the best
  evidence available.
- Be honest about uncertainty, failed attempts, unavailable tools, and
  unverified results.
- Follow the runtime safety, reporting, memory, and subagent contracts when
  actions become risky or stateful.
- Use the current tool strategy and tool schemas as the source of truth for
  choosing tools and exact arguments.
"""


_NATIVE_WORKING_CONTRACT = """
## Native Working Contract
- Use the task brief, runtime context, available tools, and explicit runtime
  addenda to choose the right working posture for this turn.
- Treat planning, execution, review, verification, and synthesis as flexible
  working postures, not as prompt profiles selected by metadata.
- Prefer concrete, evidence-backed progress over describing hypothetical work.
- Keep implementation changes scoped to the request and consistent with the
  project.

## Planning And Review Practice
- For planning, produce decision-complete steps with clear inputs, outputs,
  handoffs, risks, and validation targets.
- For review, inspect the current workspace and evidence directly. Do not
  approve, reject, or repeat old findings without checking the current state.
- When a runtime addendum requires a structured verdict, dispatch, report, or
  handoff shape, follow that addendum exactly.

## Native Self-Verification Contract
- Before final delivery, check the user's goal against the actual changes,
  artifacts, and paths you touched.
- When you change code, files, UI behavior, commands, or generated artifacts,
  prefer executable evidence: targeted tests, lint/type checks, smoke commands,
  browser checks, or direct artifact inspection.
- If you cannot run a relevant verification step, say so plainly in one
  sentence and explain the constraint.
- Include a short verification status in the final reply when you changed
  something or when the runtime asks for one.
- If verification reveals a blocking issue, fix it before finishing when
  possible. If it cannot be fixed in this turn, report the blocker honestly
  instead of presenting the work as complete.
"""

_USER_INPUT_GUIDELINES = """
## User Input Recovery
- If the latest user reply resolves the blocker, continue instead of asking again.
- If it is incomplete or ambiguous, ask only for the exact remaining gap.
- Never repeat the same broad question or ask for what the user already provided.
"""

_TASK_MODE_ORCHESTRATION = """
## Task-Mode Orchestration
- You are the user's primary task-mode execution agent for this session.
- Execute as a single full-capability agent; do not model task mode as a
  company organization, recruiting flow, employee persona, or staff assignment.
- Treat the `task_generalist` role id as routing and logging metadata only, not
  as a persona source.
- Prefer direct execution over narrating what you would do.
- Use `agent_spawn`, `agent_wait`, and `agent_send` only for bounded parallel
  work or context isolation when that improves the result.
"""

_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST = {
    "shell_exec",
    "file_write",
    "file_edit",
    "apply_patch",
    "python_exec",
    "web_search",
    "web_fetch",
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_snapshot",
    "browser_type",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
    "browser_close",
    "git_status",
    "git_commit",
    "git_diff",
    "agent_spawn",
    "agent_wait",
    "agent_send",
    "agent_list",
}

_PROMPT_PROFILE_COMMUNICATION = """
## Communication Contract
- Before the first meaningful tool action, briefly state the immediate plan.
- During longer work, give short progress updates when you find a root cause,
  change direction, or complete a meaningful milestone.
- Final delivery must be outcome-first and include an explicit verification
  status when the runtime asks for one.
"""

_PROMPT_PROFILE_HARNESS = """
## Runtime Harness Reminder
- The runtime may compact history, summarize older turns, and re-inject structured runtime artifacts.
- Preserve important state in task tools and artifacts rather than only in free-form prose.
- When resuming work, trust the reinjected runtime state before re-solving old steps.
"""


@dataclass
class NativePromptBundle:
    """Layered prompt payload for the native runtime."""

    profile_name: str
    stable_system_prompt: str
    runtime_policy_messages: list[dict[str, Any]] = field(default_factory=list)


class PromptProfileManager:
    """Build unified native prompts with stable static sections."""

    UNIFIED_PROFILE = "unified"

    def __init__(self, role: AgentInfo, config: OPCConfig) -> None:
        self.role = role
        self.config = config

    def resolve_profile(self, task: Task) -> str:
        _ = task
        # Compatibility/observability label only. Prompt profiles are no longer
        # selected from YAML; the native prompt is intentionally unified.
        return self.UNIFIED_PROFILE

    def build_stable_system_prompt(self, task: Task) -> tuple[str, str]:
        profile = self.resolve_profile(task)
        header = _TASK_MODE_CORE_HEADER if self._is_task_mode_task(task) else _CORE_HEADER
        parts: list[str] = [
            header.format(
                role_name=self.role.name,
                responsibility=self.role.responsibility,
            ),
            _CORE_OPERATING_PRINCIPLES,
            SAFE_ACTIONS_CONTRACT,
            HONEST_REPORTING_CONTRACT,
            MEMORY_TRUST_CONTRACT,
            SUBAGENT_HARNESS_CONTRACT,
            _NATIVE_WORKING_CONTRACT,
            _USER_INPUT_GUIDELINES,
            _PROMPT_PROFILE_COMMUNICATION,
            _PROMPT_PROFILE_HARNESS,
            LONG_RUNNING_SESSION_CONTRACT,
        ]
        return profile, "\n\n".join(part for part in parts if part)

    def build_runtime_policy_messages(self, task: Task) -> list[dict[str, Any]]:
        parts: list[str] = []

        if self._is_company_mode_task(task):
            parts.append(self._build_company_work_item_contract(task))

        if self._is_task_mode_task(task):
            parts.append(_TASK_MODE_ORCHESTRATION)
        if self.role.prompt_refs and not self._is_task_generalist_role(task):
            parts.append("## Role Operating Instructions\n" + "\n\n".join(self.role.prompt_refs))
        runtime_prompt_addendum = str(task.metadata.get("_subagent_profile_prompt", "") or "").strip()
        if runtime_prompt_addendum:
            parts.append(f"## Runtime Profile Override\n{runtime_prompt_addendum}")
        return [
            {"role": "system", "content": part}
            for part in parts
            if str(part or "").strip()
        ]

    def build_prompt_bundle(self, task: Task) -> NativePromptBundle:
        profile, stable_prompt = self.build_stable_system_prompt(task)
        return NativePromptBundle(
            profile_name=profile,
            stable_system_prompt=stable_prompt,
            runtime_policy_messages=self.build_runtime_policy_messages(task),
        )

    def build_prompt(self, task: Task) -> tuple[str, str]:
        bundle = self.build_prompt_bundle(task)
        parts = [
            bundle.stable_system_prompt,
            *[
                str(message.get("content", "") or "").strip()
                for message in bundle.runtime_policy_messages
            ],
        ]
        return bundle.profile_name, "\n\n".join(part for part in parts if part)

    @staticmethod
    def _is_task_mode_task(task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return mode in {"project", "task"} or execution_mode == ExecutionMode.TASK_MODE.value

    @staticmethod
    def _is_company_mode_task(task: Task) -> bool:
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return execution_mode == ExecutionMode.COMPANY_MODE.value

    def _is_task_generalist_role(self, task: Task) -> bool:
        role_id = str(getattr(self.role, "role_id", "") or "").strip()
        return role_id == "task_generalist" and self._is_task_mode_task(task)

    def _build_company_work_item_contract(self, task: Task) -> str:
        return build_company_work_item_contract(task)


class NativeAgent:
    """OPC Native Agent — wraps NativeRuntimeV2 with memory, skills, and preferences."""

    def __init__(
        self,
        role: AgentInfo,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        context_assembler: ContextAssembler,
        memory: MemoryManager,
        preferences: PreferenceManager,
        skills: SkillLibrary,
        event_bus: EventBus,
        cost_tracker: CostTracker | None = None,
        config: OPCConfig | None = None,
        communication: Any | None = None,
        approval_callback: Any = None,
    ) -> None:
        self.role = role
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_assembler = context_assembler
        self.memory = memory
        self.preferences = preferences
        self.skills = skills
        self.event_bus = event_bus
        self.cost_tracker = cost_tracker
        self.config = config or OPCConfig()
        self.communication = communication
        self.approval_callback = approval_callback
        self.prompt_profiles = PromptProfileManager(role, self.config)
        max_iter = self.config.system.max_agent_iterations
        comp_threshold = self.config.system.context_compression_threshold
        self.loop = NativeRuntimeV2(
            llm=llm,
            tool_registry=tool_registry,
            event_bus=event_bus,
            cost_tracker=cost_tracker,
            memory_manager=memory,
            history_compactor=getattr(memory, "history_compactor", None),
            max_iterations=max_iter,
            compression_threshold=comp_threshold,
            config=self.config,
            child_agent_factory=self._create_child_agent,
            approval_callback=approval_callback,
            prefetch_provider=self._build_runtime_prefetch_payload,
        )

    def _is_task_mode_task(self, task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        if mode in {"project", "task"}:
            return True
        return execution_mode == ExecutionMode.TASK_MODE.value

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> TaskResult:
        """Execute a task end-to-end."""
        self.role.status = AgentStatus.RUNNING
        self.role.current_task_id = task.id

        await self.event_bus.publish(OPCEvent(
            event_type="agent_status_changed",
            payload={"role_id": self.role.role_id, "status": "running", "task_id": task.id},
        ))

        is_task_mode = self._is_task_mode_task(task)
        allowed = self._resolve_allowed_tools(task)
        inbox_interrupt_provider = None
        if (
            self.communication
            and task.metadata.get("execution_mode") == ExecutionMode.COMPANY_MODE.value
            and not bool(task.metadata.get("_disable_live_inbox_interrupts", False))
        ):
            inbox_interrupt_provider = self._create_inbox_interrupt_provider()
        runtime_inbox_queue = getattr(task, "_runtime_inbox_queue", None)
        if runtime_inbox_queue is not None:
            inbox_interrupt_provider = self._create_runtime_inbox_provider(runtime_inbox_queue, task)

        try:
            system_prompt = await self._build_system_prompt(task)
            user_message = await self._build_user_message(task)
            context_messages = await self._build_context_messages(task)

            result = await self.loop.run(
                system_prompt=system_prompt,
                user_message=user_message,
                context_messages=context_messages,
                attachment_refs=list(task.metadata.get("attachment_refs", []) or []),
                task=task,
                allowed_tools=allowed,
                on_progress=on_progress,
                inbox_interrupt_provider=inbox_interrupt_provider,
            )

            return result

        except Exception as e:
            logger.error(f"Agent {self.role.role_id} failed on task {task.id}: {e}")
            return TaskResult(status=TaskStatus.FAILED, content=str(e))

        finally:
            self.role.status = AgentStatus.IDLE
            self.role.current_task_id = None
            await self.event_bus.publish(OPCEvent(
                event_type="agent_status_changed",
                payload={"role_id": self.role.role_id, "status": "idle"},
            ))

    async def _build_native_prompt_bundle(self, task: Task) -> NativePromptBundle:
        override = str(task.metadata.get("_runtime_system_prompt_override", "") or "").strip()
        if override:
            task.metadata["runtime_prompt_profile"] = "override"
            return NativePromptBundle(
                profile_name="override",
                stable_system_prompt=override,
                runtime_policy_messages=[],
            )
        bundle = self.prompt_profiles.build_prompt_bundle(task)
        task.metadata["runtime_prompt_profile"] = bundle.profile_name
        return bundle

    async def _build_system_prompt(self, task: Task) -> str:
        bundle = await self._build_native_prompt_bundle(task)
        return bundle.stable_system_prompt

    async def _build_user_message(self, task: Task) -> str:
        return self.context_assembler.build_task_brief(task)

    async def _build_context_messages(self, task: Task) -> list[dict[str, Any]]:
        fork_messages = list(task.metadata.get("_fork_context_messages", []) or [])
        if fork_messages:
            return fork_messages

        harness_output = await self._build_prompt_harness(task)
        dynamic_messages = [
            *harness_output.runtime_policy_messages,
            *harness_output.workspace_context_messages,
            *harness_output.artifact_messages,
        ]
        session_id = getattr(task, "session_id", None)
        if not session_id:
            return dynamic_messages
        context_snapshot = task.context_snapshot if isinstance(task.context_snapshot, dict) else {}
        raw_runtime_resume = context_snapshot.get("runtime_resume")
        has_runtime_resume = isinstance(raw_runtime_resume, dict) and bool(raw_runtime_resume)
        legacy_skip_session_history = raw_runtime_resume is True
        if bool(context_snapshot.get("skip_session_history", False)) or has_runtime_resume or legacy_skip_session_history:
            return dynamic_messages
        return [
            *dynamic_messages,
            *(
                await self.memory.build_session_history_tail_messages(
                    session_id,
                    include_latest_user_turn=False,
                )
            ),
        ]

    async def _build_prompt_harness(self, task: Task) -> Any:
        allowed_tools = self._resolve_allowed_tools(task)
        prompt_bundle = await self._build_native_prompt_bundle(task)
        harness = PromptHarnessBuilder(
            task=task,
            role_id=self.role.role_id,
            config=self.config,
            context_assembler=self.context_assembler,
            preferences=self.preferences,
            skills=self.skills,
            skill_refs=list(self.role.skill_refs or []),
        )
        output = await harness.build(
            system_prompt=prompt_bundle.stable_system_prompt,
            allowed_tools=allowed_tools,
            runtime_policy_messages=prompt_bundle.runtime_policy_messages,
        )
        task.metadata["prompt_harness"] = {
            "static_section_ids": list(output.static_section_ids),
            "dynamic_section_ids": list(output.dynamic_section_ids),
            "artifact_manifest": list(output.artifact_manifest),
            "artifact_hashes": dict(output.artifact_hashes),
        }
        task.metadata["_prompt_harness_boot_artifacts"] = list(output.artifact_manifest)
        return output

    def _registered_general_tool_names(self) -> set[str]:
        return {
            str(tool.name or "").strip()
            for tool in self.tool_registry.list_tools()
            if str(tool.name or "").strip()
            and str(tool.name or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    @staticmethod
    def _configured_general_tool_names(tools: list[str] | tuple[str, ...]) -> set[str]:
        return {
            str(tool or "").strip()
            for tool in list(tools or [])
            if str(tool or "").strip()
            and str(tool or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    def _resolve_allowed_tools(self, task: Task) -> list[str] | None:
        turn_mode = resolve_company_turn_mode(task, runtime_state={})
        inherited = list(task.metadata.get("_fork_allowed_tools", []) or [])
        if inherited:
            if not company_collaboration_enabled_for_task(task):
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
                ]
            else:
                _, allowed_collab = resolve_task_collaboration_tools(
                    task,
                    role=self.role.role_id,
                    seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                    runtime_state={},
                    role_cfg=self.role,
                )
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES or tool in allowed_collab
                ]
            if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
                inherited = [
                    tool for tool in inherited
                    if tool not in _MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST
                ]
            return inherited

        configured_general = self._configured_general_tool_names(list(self.role.tools or []))
        company_mode = company_collaboration_enabled_for_task(task)
        if company_mode:
            allowed = set(configured_general) if configured_general else self._registered_general_tool_names()
            _, allowed_collab = resolve_task_collaboration_tools(
                task,
                role=self.role.role_id,
                seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                runtime_state={},
                role_cfg=self.role,
            )
            allowed.update(allowed_collab)
        elif configured_general:
            allowed = set(configured_general)
        else:
            return None

        if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
            allowed.difference_update(_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST)
        return sorted(allowed)

    async def _build_runtime_prefetch_payload(
        self,
        task: Task,
        query: str,
        _messages: list[dict[str, Any]],
    ) -> dict[str, str]:
        prefetch_cfg = self.config.system.native_runtime.prefetch
        if not prefetch_cfg.enabled:
            return {}
        payload: dict[str, str] = {}
        max_chars = max(400, int(prefetch_cfg.max_chars or 4000))
        session_id = getattr(task, "session_id", None)
        include_project_knowledge = bool(task.metadata.get("include_project_knowledge", False))
        if prefetch_cfg.session_memory and session_id:
            session_memory = (await self.memory.build_session_memory_context(session_id)).strip()
            if session_memory:
                payload["session_memory"] = clip_text(
                    session_memory,
                    limit=max_chars,
                    marker="session memory prefetch truncated",
                ).text
        if prefetch_cfg.focused_memory:
            focused = (
                await self.memory.build_focused_memory_context(
                    query=query,
                    project_id=task.project_id,
                    session_id=session_id,
                    include_project_knowledge=include_project_knowledge,
                    max_chars=max_chars,
                )
            ).strip()
            if focused:
                payload["focused_memory"] = clip_text(
                    focused,
                    limit=max_chars,
                    marker="focused memory prefetch truncated",
                ).text
        if prefetch_cfg.project_memory_candidates:
            project_memory = (
                await self.memory.build_project_memory_context(
                    project_id=task.project_id,
                    include_project_knowledge=include_project_knowledge,
                )
            ).strip()
            if project_memory:
                payload["project_memory_candidates"] = clip_text(
                    project_memory,
                    limit=max_chars,
                    marker="project memory prefetch truncated",
                ).text
        harness_cfg = self.config.system.native_runtime.prompt_harness
        skills_in_prompt_harness = bool(harness_cfg.enabled and harness_cfg.artifact_messages_enabled)
        if prefetch_cfg.skills_summary and not skills_in_prompt_harness:
            execution_mode = str(task.metadata.get("execution_mode", "") or "").strip() or None
            skills_summary = str(
                self.skills.build_skills_summary(
                    task.project_id,
                    execution_mode=execution_mode,
                    role_id=self.role.role_id,
                    user_facing=_memory_skill_user_facing(task, self.role.role_id),
                    final_decider_role_id=_final_decider_role_id(task),
                    skill_refs=list(self.role.skill_refs or []),
                )
                or ""
            ).strip()
            if skills_summary:
                payload["skills_summary"] = clip_text(
                    skills_summary,
                    limit=max_chars,
                    marker="skills summary prefetch truncated",
                ).text
        return payload

    def _create_inbox_interrupt_provider(self) -> Any:
        communication = self.communication
        agent_role_id = self.role.role_id

        async def _provide(task: Task) -> list[dict[str, Any]]:
            if communication is None:
                return []
            return await communication.consume_live_inbox_messages(task, agent_id=agent_role_id)

        return _provide

    def _create_runtime_inbox_provider(self, inbox_queue: Any, task: Task) -> Any:
        async def _provide(_task: Task) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            while True:
                try:
                    message = inbox_queue.get_nowait()
                except Exception:
                    break
                if not message:
                    continue
                if isinstance(message, dict):
                    normalized = dict(message)
                    normalized.setdefault("from", str(normalized.get("from_agent", "runtime_subagent_parent") or "runtime_subagent_parent"))
                    normalized.setdefault("body", str(normalized.get("body", normalized.get("message", "")) or ""))
                    items.append(normalized)
                    continue
                items.append({"from": "runtime_subagent_parent", "body": str(message)})
            endpoint_id = str(task.metadata.get("_comms_endpoint_id", "") or "").strip()
            if endpoint_id:
                workspace_root = (
                    str(task.metadata.get("comms_workspace_root", "") or "").strip()
                    or str(task.metadata.get("workspace_root", "") or "").strip()
                    or str(task.metadata.get("target_output_dir", "") or "").strip()
                )
                if workspace_root:
                    try:
                        from opc.layer2_organization import comms as _comms

                        layout = _comms.resolve_layout(
                            workspace_root,
                            str(task.project_id or "default").strip() or "default",
                            str(task.parent_session_id or task.session_id or "default").strip() or "default",
                        )
                        unread = _comms.list_unread(layout, endpoint_id, limit=6)
                        injected_ids = {
                            str(item).strip()
                            for item in list(task.context_snapshot.get("runtime_inbox_injected_message_ids", []) or [])
                            if str(item).strip()
                        }
                        for header in unread:
                            msg_id = str(header.message_id or "").strip()
                            if msg_id and msg_id in injected_ids:
                                continue
                            _, body = _comms.read_message(header.path)
                            if body.strip():
                                items.append(classify_worker_message(
                                    {
                                        "from": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "from_agent": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "subject": str(header.subject or "").strip(),
                                        "message_id": str(header.message_id or "").strip(),
                                        "msg_id": str(header.message_id or "").strip(),
                                        "body": body.strip(),
                                        "reply_needed": bool(header.blocking),
                                        "urgency": str(header.priority or "").strip() or "normal",
                                        "transport_kind": str(header.raw_frontmatter.get("transport_kind", "") or "").strip(),
                                        "semantic_type": str(header.raw_frontmatter.get("semantic_type") or header.raw_frontmatter.get("kind") or "").strip(),
                                        "metadata": dict(header.raw_frontmatter or {}),
                                    }
                                ))
                                if msg_id:
                                    injected_ids.add(msg_id)
                        if injected_ids:
                            task.context_snapshot = dict(task.context_snapshot)
                            task.context_snapshot["runtime_inbox_injected_message_ids"] = sorted(injected_ids)[-50:]
                    except Exception:
                        pass
            return items

        return _provide

    def _create_child_agent(
        self,
        profile: str,
        allowed_tools: list[str],
        prompt_addendum: str,
        overrides: dict[str, Any] | None = None,
    ) -> "NativeAgent":
        overrides = dict(overrides or {})
        role_name = str(overrides.get("name") or f"{self.role.name} [{profile}]").strip() or f"{self.role.name} [{profile}]"
        role = AgentInfo(
            role_id=f"{self.role.role_id}:{profile}",
            name=role_name,
            responsibility=self.role.responsibility,
            status=AgentStatus.IDLE,
            current_task_id=None,
            reports_to=self.role.reports_to,
            icon=self.role.icon,
            can_spawn=list(self.role.can_spawn),
            tools=list(allowed_tools),
            preferred_external_agent=self.role.preferred_external_agent,
            prompt_refs=[*self.role.prompt_refs],
            skill_refs=[*self.role.skill_refs],
            handoff_template_ref=self.role.handoff_template_ref,
            memory_policy_ref=self.role.memory_policy_ref,
            artifact_contract_ref=self.role.artifact_contract_ref,
            runtime_policy=dict(self.role.runtime_policy),
            org_id=self.role.org_id,
            budget_monthly_cents=self.role.budget_monthly_cents,
            spent_monthly_cents=self.role.spent_monthly_cents,
            heartbeat_enabled=self.role.heartbeat_enabled,
            heartbeat_interval_sec=self.role.heartbeat_interval_sec,
            last_heartbeat_at=self.role.last_heartbeat_at,
            capabilities=self.role.capabilities,
        )
        if prompt_addendum:
            role.prompt_refs.append(prompt_addendum)
        if overrides.get("description"):
            role.prompt_refs.append(f"Subagent task summary: {str(overrides['description']).strip()}")
        if overrides.get("mode"):
            role.prompt_refs.append(f"Runtime spawn mode: {str(overrides['mode']).strip()}")

        child_llm = self.llm
        model_override = str(overrides.get("model") or "").strip()
        if model_override:
            llm_config = self.llm.config.model_copy(deep=True)
            llm_config.default_model = model_override
            child_llm = LLMProvider(llm_config, opc_home=getattr(self.llm, "opc_home", None))

        child_config = self.config
        max_iterations = overrides.get("max_iterations")
        if self.config is not None and max_iterations:
            child_config = self.config.model_copy(deep=True)
            child_config.system.max_agent_iterations = max(1, int(max_iterations))

        return NativeAgent(
            role=role,
            llm=child_llm,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            event_bus=self.event_bus,
            cost_tracker=self.cost_tracker,
            config=child_config,
            communication=self.communication,
            approval_callback=self.approval_callback,
        )
