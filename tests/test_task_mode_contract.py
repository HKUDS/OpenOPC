from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.config import OPCConfig
from opc.core.models import AgentInfo, AgentStatus, ExecutionMode, Task, TaskResult, TaskStatus
from opc.layer2_organization.org_engine import TASK_MODE_GENERAL_ROLE_ID
from opc.layer3_agent.native_agent import NativeAgent
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry


class _StubContextAssembler:
    async def build_system_context(self, task: Task, role_id: str) -> str:
        _ = (task, role_id)
        return ""

    def build_task_brief(self, task: Task) -> str:
        return task.description or task.title


class _StubPreferences:
    def build_preference_context(self, project_id: str) -> str:
        _ = project_id
        return ""

    def summarize_autonomy_preferences(self, project_id: str) -> str:
        _ = project_id
        return ""


class _StubSkills:
    def build_skills_summary(
        self,
        project_id: str | None = None,
        *,
        execution_mode: str | None = None,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
        skill_refs: list[str] | None = None,
    ) -> str:
        _ = (project_id, execution_mode, role_id, user_facing, final_decider_role_id, skill_refs)
        return ""


class _StubMemory:
    history_compactor = None

    async def build_session_history_tail_messages(
        self,
        session_id: str,
        *,
        include_latest_user_turn: bool = True,
    ) -> list[dict[str, str]]:
        _ = (session_id, include_latest_user_turn)
        return []


class _TrackingMemory(_StubMemory):
    def __init__(self) -> None:
        self.history_calls = 0

    async def build_session_history_tail_messages(
        self,
        session_id: str,
        *,
        include_latest_user_turn: bool = True,
    ) -> list[dict[str, str]]:
        self.history_calls += 1
        return [{"role": "assistant", "content": f"history:{session_id}:{include_latest_user_turn}"}]


class _StubConfig:
    max_tokens = 1024


class _StubLLM:
    def __init__(self) -> None:
        self.config = _StubConfig()
        self.seen_tool_names: list[list[str]] = []

    def get_tool_definitions(self, tools):
        return tools

    def get_context_window(self):
        return 8192

    def count_input_tokens(self, messages, tools=None):
        _ = (messages, tools)
        return 100

    def is_context_overflow_error(self, error):
        _ = error
        return False

    def prepare_user_message_content(self, user_message: str, attachment_refs=None):
        _ = attachment_refs
        return user_message

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        _ = (prompt, system, task_type)
        return "summary"

    async def chat(self, messages, tools=None):
        _ = messages
        self.seen_tool_names.append([tool.get("name", "") for tool in tools or []])
        return {"content": "done", "tool_calls": [], "usage": {}}


def _make_role(
    tools: list[str] | None = None,
    *,
    role_id: str = TASK_MODE_GENERAL_ROLE_ID,
    can_spawn: list[str] | None = None,
    role_type: str = "worker",
    prompt_refs: list[str] | None = None,
) -> AgentInfo:
    return AgentInfo(
        role_id=role_id,
        name="Task Generalist",
        responsibility="Primary session agent for task mode.",
        status=AgentStatus.IDLE,
        can_spawn=list(can_spawn or []),
        tools=list(tools or []),
        prompt_refs=list(prompt_refs or []),
        runtime_policy={"role_type": role_type},
    )


def _make_native_agent(role: AgentInfo) -> NativeAgent:
    return _make_native_agent_with_memory(role, _StubMemory())


def _make_native_agent_with_memory(
    role: AgentInfo,
    memory: _StubMemory,
    tool_registry: ToolRegistry | None = None,
) -> NativeAgent:
    return NativeAgent(
        role=role,
        llm=_StubLLM(),
        tool_registry=tool_registry or ToolRegistry(),
        context_assembler=_StubContextAssembler(),
        memory=memory,
        preferences=_StubPreferences(),
        skills=_StubSkills(),
        event_bus=SimpleNamespace(publish=AsyncMock()),
        config=OPCConfig(),
    )


async def _noop_tool(**kwargs):
    _ = kwargs
    return {"ok": True}


def _registry_with_tools(names: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(ToolDefinition(name=name, description=name, parameters={}, func=_noop_tool))
    return registry


class TaskModeNativeAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_messages_skip_session_history_with_explicit_flag(self) -> None:
        memory = _TrackingMemory()
        agent = _make_native_agent_with_memory(_make_role(), memory)
        task = Task(
            title="Internal assessment",
            description="Return JSON only.",
            session_id="sess-internal",
            project_id="proj1",
            metadata={"mode": "task", "execution_mode": ExecutionMode.TASK_MODE.value},
            context_snapshot={"skip_session_history": True},
        )

        context_messages = await agent._build_context_messages(task)

        self.assertEqual(memory.history_calls, 0)
        self.assertFalse(any("history:sess-internal" in str(message.get("content", "")) for message in context_messages))

    async def test_legacy_boolean_runtime_resume_skips_history_without_crashing(self) -> None:
        memory = _TrackingMemory()
        agent = _make_native_agent_with_memory(_make_role(), memory)
        task = Task(
            title="Legacy internal assessment",
            description="Return JSON only.",
            session_id="sess-legacy",
            project_id="proj1",
            metadata={"mode": "task", "execution_mode": ExecutionMode.TASK_MODE.value},
            context_snapshot={"runtime_resume": True},
        )

        context_messages = await agent._build_context_messages(task)

        self.assertEqual(memory.history_calls, 0)
        self.assertFalse(any("history:sess-legacy" in str(message.get("content", "")) for message in context_messages))

    async def test_task_mode_prompt_uses_unified_capability_contract(self) -> None:
        agent = _make_native_agent(_make_role(prompt_refs=["Legacy task-mode persona text."]))
        task = Task(
            title="Inspect OpenReview assignments",
            description="Open the site and inspect my review assignments.",
            project_id="proj1",
            metadata={"mode": "task", "execution_mode": ExecutionMode.TASK_MODE.value},
        )

        prompt = await agent._build_system_prompt(task)
        context_messages = await agent._build_context_messages(task)
        context_prompt = "\n\n".join(str(item.get("content", "")) for item in context_messages)

        self.assertEqual(task.metadata["runtime_prompt_profile"], "unified")
        self.assertIn("You are Task Generalist, an OpenOPC task execution agent.", prompt)
        self.assertNotIn("One-Person Company system", prompt)
        self.assertIn("## Core Operating Principles", prompt)
        self.assertNotIn("## Core Beliefs", prompt)
        self.assertIn("## Native Working Contract", prompt)
        self.assertIn("not as prompt profiles selected by metadata", prompt)
        self.assertIn("## Native Self-Verification Contract", prompt)
        self.assertNotIn("## Task-Mode Orchestration", prompt)
        self.assertIn("## Task-Mode Orchestration", context_prompt)
        self.assertIn("primary task-mode execution agent", context_prompt)
        self.assertIn("recruiting flow, employee persona, or staff assignment", context_prompt)
        self.assertIn("agent_spawn", context_prompt)
        self.assertIn("## Runtime Artifact", context_prompt)
        self.assertIn("Tool Strategy", context_prompt)
        self.assertIn("## Task-Mode Orchestration", str(context_messages[0].get("content", "")))
        self.assertIn("Tool Strategy", str(context_messages[-1].get("content", "")))
        self.assertIn("Include a short verification status", prompt)
        self.assertNotIn("## Role Operating Instructions", prompt)
        self.assertNotIn("Legacy task-mode persona text.", context_prompt)
        self.assertNotIn("## Planning Principles", prompt)
        self.assertNotIn("## Plan Profile", prompt)

    async def test_plan_subagent_uses_unified_prompt_without_task_mode_orchestration(self) -> None:
        agent = _make_native_agent(_make_role(["file_read", "todo_write"]))
        task = Task(
            title="Draft the migration plan",
            description="Inspect the runtime modules and propose a migration sequence.",
            project_id="proj1",
            metadata={"_subagent_mode": "plan"},
        )

        prompt = await agent._build_system_prompt(task)
        context_messages = await agent._build_context_messages(task)
        context_prompt = "\n\n".join(str(item.get("content", "")) for item in context_messages)

        self.assertEqual(task.metadata["runtime_prompt_profile"], "unified")
        self.assertIn("## Native Working Contract", prompt)
        self.assertIn("For planning, produce decision-complete steps", prompt)
        self.assertNotIn("## Task Tracking", prompt)
        self.assertIn("Tool Strategy", context_prompt)
        self.assertIn("Use the task ledger", context_prompt)
        self.assertNotIn("## Plan Profile", prompt)
        self.assertNotIn("## Planning Principles", prompt)
        self.assertNotIn("## Task-Mode Orchestration", prompt)

    async def test_company_review_work_item_uses_company_contract_and_verify_profile(self) -> None:
        agent = _make_native_agent(_make_role(["file_read", "file_search", "list_dir"]))
        task = Task(
            title="QA Review",
            description="Review the execution output with fresh eyes.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_title": "QA Review",
                "work_item_turn_type": "review",
                "work_item_orchestration_profile": "company_review_fresh_eyes",
            },
        )

        prompt = await agent._build_system_prompt(task)
        context_messages = await agent._build_context_messages(task)
        context_prompt = "\n\n".join(str(item.get("content", "")) for item in context_messages)

        self.assertEqual(task.metadata["runtime_prompt_profile"], "unified")
        self.assertIn("## Native Working Contract", prompt)
        self.assertNotIn("## Company Work Item Contract", prompt)
        self.assertIn("## Company Work Item Contract", context_prompt)
        self.assertIn("Work item turn type: `review`", context_prompt)
        self.assertIn("## Company Work Item Turn: Review", context_prompt)
        # Fix 4 reshaped the inline example from the compact
        # ``"review_verdict":"approve|reject"`` alternation into two
        # explicit Approve / Reject examples. Assert both shapes show up.
        self.assertIn('"review_verdict":"approve"', context_prompt)
        self.assertIn('"review_verdict":"reject"', context_prompt)
        self.assertIn("blocking_issues", context_prompt)
        self.assertNotIn("## Task-Mode Orchestration", prompt)

    async def test_task_mode_strips_inherited_company_collaboration_tools(self) -> None:
        agent = _make_native_agent(_make_role())
        task = Task(
            title="Task-mode child",
            description="Continue the task without company collaboration.",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": ExecutionMode.TASK_MODE.value,
                "_fork_allowed_tools": ["file_read", "send_dm"],
            },
        )

        allowed = agent._resolve_allowed_tools(task)
        context_messages = await agent._build_context_messages(task)
        context_prompt = "\n\n".join(str(item.get("content", "")) for item in context_messages)

        self.assertEqual(allowed, ["file_read"])
        self.assertNotIn("company collaboration tools", context_prompt)

    async def test_company_execute_work_item_does_not_expose_manager_tools(self) -> None:
        agent = _make_native_agent(_make_role(["file_write", "file_edit"]))
        task = Task(
            title="Engineering Execution",
            description="Implement the approved scope.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "engineering_execution",
                "work_item_turn_type": "execute",
            },
        )

        allowed = agent._resolve_allowed_tools(task)

        self.assertNotIn("delegate_work", allowed)
        self.assertNotIn("manager_board_read", allowed)
        self.assertNotIn("propose_runtime_replan", allowed)

    async def test_company_empty_role_tools_allow_all_registered_general_tools(self) -> None:
        registry = _registry_with_tools([
            "file_read",
            "shell_exec",
            "request_user_input",
            "delegate_work",
            "manager_board_read",
            "send_dm",
            "inbox",
        ])
        agent = _make_native_agent_with_memory(
            _make_role([], role_id="analyst"),
            _StubMemory(),
            registry,
        )
        task = Task(
            title="Analysis",
            description="Analyze with native tools.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "analysis_execution",
                "work_item_turn_type": "execute",
            },
        )

        allowed = agent._resolve_allowed_tools(task)

        self.assertIn("file_read", allowed)
        self.assertIn("shell_exec", allowed)
        self.assertIn("request_user_input", allowed)
        self.assertIn("send_dm", allowed)
        self.assertIn("inbox", allowed)
        self.assertNotIn("delegate_work", allowed)
        self.assertNotIn("manager_board_read", allowed)

    async def test_company_explicit_role_tools_restrict_general_tools_only(self) -> None:
        registry = _registry_with_tools([
            "file_read",
            "shell_exec",
            "request_user_input",
            "send_dm",
            "inbox",
        ])
        agent = _make_native_agent_with_memory(
            _make_role(["file_read"], role_id="analyst"),
            _StubMemory(),
            registry,
        )
        task = Task(
            title="Analysis",
            description="Analyze with native tools.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "analysis_execution",
                "work_item_turn_type": "execute",
            },
        )

        allowed = agent._resolve_allowed_tools(task)

        self.assertIn("file_read", allowed)
        self.assertIn("send_dm", allowed)
        self.assertIn("inbox", allowed)
        self.assertNotIn("shell_exec", allowed)
        self.assertNotIn("request_user_input", allowed)

    async def test_company_manager_work_item_exposes_delegate_and_manager_board_but_not_replan(self) -> None:
        agent = _make_native_agent(_make_role(["file_read"], role_id="cto"))
        task = Task(
            title="CTO Delegation",
            description="Break the work into subordinate tasks.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "cto_delegation",
                "work_item_turn_type": "plan",
                "managed_team_id": "team::cto",
                "delegation_seat_id": "seat::team::ceo::cto",
            },
        )

        allowed = agent._resolve_allowed_tools(task)
        context_messages = await agent._build_context_messages(task)
        context_prompt = "\n\n".join(str(item.get("content", "")) for item in context_messages)

        self.assertIn("delegate_work", allowed)
        self.assertIn("manager_board_read", allowed)
        self.assertNotIn("manager_board_update", allowed)
        self.assertNotIn("manager_board_release", allowed)
        self.assertNotIn("manager_board_rollup", allowed)
        self.assertNotIn("propose_runtime_replan", allowed)
        self.assertNotIn("read_inbox", allowed)
        self.assertIn("company collaboration tools", context_prompt)

    async def test_multi_team_manager_turn_strips_execution_heavy_tools(self) -> None:
        agent = _make_native_agent(
            _make_role(["file_read", "shell_exec", "file_write", "web_search"], role_id="ceo", can_spawn=["cto"])
        )
        task = Task(
            title="CEO Intake",
            description="Coordinate the top-level delegation.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "ceo_intake",
                "work_item_turn_type": "intake",
                "current_turn_mode": "dispatch_required",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "managed_team_id": "team::ceo",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::ceo",
                            "team_id": "team::ceo",
                            "role_id": "ceo",
                            "managed_team_id": "team::ceo",
                        },
                        {
                            "seat_id": "seat::team::ceo::cto",
                            "team_id": "team::ceo",
                            "role_id": "cto",
                            "manager_role_id": "ceo",
                        },
                    ]
                },
            },
        )

        allowed = agent._resolve_allowed_tools(task)

        self.assertIn("file_read", allowed)
        self.assertIn("delegate_work", allowed)
        self.assertIn("manager_board_read", allowed)
        self.assertNotIn("shell_exec", allowed)
        self.assertNotIn("file_write", allowed)
        self.assertNotIn("web_search", allowed)
        self.assertNotIn("agent_spawn", allowed)
        self.assertNotIn("agent_wait", allowed)
        self.assertNotIn("agent_send", allowed)

    async def test_company_coordinator_work_item_hides_legacy_replan_and_routing_tools_in_multi_team(self) -> None:
        agent = _make_native_agent(
            _make_role(["file_read"], role_id="ceo", can_spawn=["cto", "cmo"], role_type="coordinator")
        )
        task = Task(
            title="CEO Intake",
            description="Coordinate the top-level delegation.",
            project_id="proj1",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "ceo_intake",
                "work_item_turn_type": "intake",
                "current_turn_mode": "dispatch_required",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "managed_team_id": "team::ceo",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::ceo",
                            "team_id": "team::ceo",
                            "role_id": "ceo",
                            "managed_team_id": "team::ceo",
                        },
                        {
                            "seat_id": "seat::team::ceo::cto",
                            "team_id": "team::ceo",
                            "role_id": "cto",
                            "manager_role_id": "ceo",
                        },
                    ]
                },
            },
        )

        allowed = agent._resolve_allowed_tools(task)

        self.assertIn("delegate_work", allowed)
        self.assertNotIn("route_work", allowed)
        self.assertNotIn("propose_task_adjustment", allowed)
        self.assertNotIn("propose_runtime_replan", allowed)
        self.assertNotIn("find_and_ask_expert", allowed)
        self.assertNotIn("ask_peer_and_wait", allowed)
        self.assertNotIn("read_inbox", allowed)


if __name__ == "__main__":
    unittest.main()
