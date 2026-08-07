from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from opc.core.config import AutonomyConfig, LLMConfig, NativeSubagentProfileConfig, OPCConfig, PermissionsV2Config
from opc.core.models import PermissionResolution
from opc.core.models import PermissionScope, RiskLevel, Task, TaskResult, TaskStatus
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.subagents import SubagentManager
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.agent_runtime import create_agent_runtime_tools
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry
from opc.layer4_tools.todo import create_todo_tools
from opc.llm.provider import LLMProvider


class _StubEventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _StubLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.config = type("Cfg", (), {"max_tokens": 2048})()

    def prepare_user_message_content(self, content: str, attachment_refs=None):
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):
        return tools

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    async def chat_stream(self, messages, tools=None):
        _ = (messages, tools)
        self.calls += 1
        yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
        if self.calls == 1:
            yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "Checking tool..."}, "model": "stub"})()
            yield type("Evt", (), {"event_type": "tool_call_delta", "payload": {"index": 0, "id": "tool-1", "name": "demo_tool", "arguments": "{\"value\": \"hello\"}"}, "model": "stub"})()
        else:
            yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "done"}, "model": "stub"})()
        yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()


class _StubStore:
    def __init__(self) -> None:
        self.project_grants: list[dict[str, object]] = []
        self.global_grants: list[dict[str, object]] = []
        self.session_grants: list[dict[str, object]] = []
        self.transcript: list[dict[str, object]] = []

    async def list_runtime_permission_grants(
        self,
        *,
        runtime_session_id: str | None = None,
        project_id: str | None = None,
        scopes: list[str] | None = None,
        tool_name: str | None = None,
    ) -> list[dict[str, object]]:
        _ = tool_name
        rows: list[dict[str, object]] = []
        if scopes and "session" in scopes and runtime_session_id:
            rows.extend(self.session_grants)
        if scopes and "project" in scopes and project_id:
            rows.extend(self.project_grants)
        if scopes and "global" in scopes:
            rows.extend(self.global_grants)
        return rows

    async def get_session_transcript(self, session_id: str) -> list[dict[str, object]]:
        _ = session_id
        return list(self.transcript)


class _StubPart:
    def __init__(self, part_type: str, payload: dict[str, object]) -> None:
        self.part_type = part_type
        self.payload = payload


class _StubMessage:
    def __init__(self, role: str, *, summary_flag: bool = False) -> None:
        self.role = role
        self.summary_flag = summary_flag


class _StubMemoryManager:
    def __init__(self, store: _StubStore) -> None:
        self.store = store
        self.verification_feedback: list[dict[str, object]] = []
        self.session_updates: list[dict[str, object]] = []
        self.appended_messages: list[dict[str, object]] = []
        self.appended_parts: list[dict[str, object]] = []

    async def build_session_memory_context(self, session_id: str) -> str:
        _ = session_id
        return "## Session Memory\nRemember the plan."

    async def record_user_turn(self, *args, **kwargs) -> None:
        _ = (args, kwargs)

    async def append_session_message(self, *args, **kwargs):
        self.appended_messages.append({"args": args, "kwargs": kwargs})
        return type("Msg", (), {"message_id": "msg-1"})()

    async def append_session_part(self, *args, **kwargs) -> None:
        self.appended_parts.append({"args": args, "kwargs": kwargs})

    async def update_runtime_session_memory(self, **kwargs) -> dict[str, object]:
        self.session_updates.append(dict(kwargs))
        return {"updated": True, "summary_preview": "runtime-session-memory"}

    async def record_verification_feedback(self, **kwargs) -> dict[str, object]:
        self.verification_feedback.append(dict(kwargs))
        return {"stored": True}


class _SubagentStore:
    def __init__(self) -> None:
        self.subagent_runs: list[dict[str, object]] = []
        self.worktree_sessions: list[dict[str, object]] = []

    async def save_runtime_subagent_run(self, **kwargs) -> None:
        self.subagent_runs.append(dict(kwargs))

    async def save_runtime_worktree_session(self, **kwargs) -> None:
        self.worktree_sessions.append(dict(kwargs))


class NativeRuntimeV2Tests(unittest.IsolatedAsyncioTestCase):
    def _runtime_for_unit_checks(self) -> NativeRuntimeV2:
        return NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            event_bus=_StubEventBus(),
            config=OPCConfig(),
        )

    async def test_task_mode_does_not_require_automatic_verification(self) -> None:
        runtime = self._runtime_for_unit_checks()
        task = Task(
            id="task-mode",
            session_id="sess-task",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "runtime_kind": "task_mode_agent_turn",
                "explicit_verification_requested": "false",
                "work_item_verification_required": True,
            },
        )

        self.assertFalse(runtime._verification_required(
            task=task,
            todo_state=[
                {"content": "one"},
                {"content": "two"},
                {"content": "three"},
            ],
            runtime_notes={
                "mutating_tools": ["file_write"],
                "observed_risky_tools": ["shell_exec"],
            },
        ))

    async def test_task_mode_explicit_verification_is_advisory_and_does_not_append_footer(self) -> None:
        runtime = self._runtime_for_unit_checks()
        task = Task(
            id="task-mode",
            session_id="sess-task",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "runtime_kind": "task_mode_agent_turn",
                "explicit_verification_requested": True,
            },
        )

        self.assertTrue(runtime._verification_required(
            task=task,
            todo_state=[],
            runtime_notes={},
        ))
        content, verdict = runtime._apply_verification_contract(
            "Here is the answer.",
            task=task,
            todo_state=[],
            runtime_notes={"verification": {"completed": True, "passed": False, "verdict": "ISSUES"}},
        )

        self.assertEqual(content, "Here is the answer.")
        self.assertIn("Verification:", verdict)

    async def test_task_mode_verifier_failure_is_advisory(self) -> None:
        runtime = self._runtime_for_unit_checks()
        task = Task(
            id="task-mode",
            title="Task mode explicit verification",
            description="Verify this task-mode answer.",
            session_id="sess-task",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "runtime_kind": "task_mode_agent_turn",
                "explicit_verification_requested": True,
            },
        )
        runtime_notes: dict[str, object] = {}

        class _Subagents:
            async def spawn(self, **kwargs):
                _ = kwargs
                return {
                    "success": True,
                    "result": (
                        "ISSUES: missing validation evidence\n"
                        "Check: Validation coverage\n"
                        "Command: pytest -q tests/test_runtime.py\n"
                        "Observed Output: no validation command was run\n"
                        "Result: FAIL\n"
                        "VERDICT: FAIL"
                    ),
                }

        result = await runtime._run_verification_gate(
            runtime_session_id="rt-task",
            task=task,
            subagents=_Subagents(),
            messages=[{"role": "system", "content": "system prompt"}],
            todo_state=[],
            runtime_notes=runtime_notes,
        )

        self.assertIsNone(result)
        verification = runtime_notes["verification"]
        self.assertFalse(verification["passed"])
        self.assertEqual(verification["evidence"]["verdict"], "fail")

    async def test_task_mode_thinking_is_persisted_once_with_final_turn(self) -> None:
        class _ThinkingLLM:
            def __init__(self) -> None:
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "thinking_delta", "payload": {"text": "我先检查"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "完成"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        event_bus = _StubEventBus()
        memory = _StubMemoryManager(_StubStore())
        runtime = NativeRuntimeV2(
            llm=_ThinkingLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            event_bus=event_bus,
            config=OPCConfig(),
            max_iterations=2,
        )

        result = await runtime.run(
            system_prompt="You are a runtime.",
            user_message="请处理",
            task=Task(
                id="task-mode",
                title="task-mode",
                session_id="sess-task",
                project_id="proj1",
                metadata={
                    "mode": "task",
                    "execution_mode": "task_mode",
                    "runtime_kind": "task_mode_agent_turn",
                },
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        runtime_events = [evt for evt in event_bus.events if getattr(evt, "event_type", "") == "runtime_event"]
        self.assertEqual(
            [evt.payload.get("type") for evt in runtime_events].count("thinking_delta"),
            1,
        )
        self.assertEqual(
            [evt.payload.get("type") for evt in runtime_events].count("assistant_delta"),
            1,
        )
        assistant_kwargs = memory.appended_messages[-1]["kwargs"]
        self.assertEqual(assistant_kwargs["metadata"]["runtime_thinking"], "我先检查")
        thinking_parts = [item for item in memory.appended_parts if item["args"][2] == "thinking"]
        self.assertEqual(len(thinking_parts), 1)
        self.assertEqual(thinking_parts[0]["args"][3]["text"], "我先检查")

    async def test_task_mode_final_identity_uses_conversation_turn_not_runtime_iteration(self) -> None:
        memory = _StubMemoryManager(_StubStore())
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            config=OPCConfig(),
        )
        task = Task(
            id="task-mode",
            title="task-mode",
            session_id="sess-task",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "runtime_kind": "task_mode_agent_turn",
            },
        )

        await runtime._persist_assistant_turn(
            task,
            "first answer",
            [],
            runtime_session_id="rt_reused",
            turn_id="ui-turn:first",
            conversation_turn_id="ui-turn:first",
            iteration=1,
        )
        await runtime._persist_assistant_turn(
            task,
            "second answer",
            [],
            runtime_session_id="rt_reused",
            turn_id="ui-turn:second",
            conversation_turn_id="ui-turn:second",
            iteration=1,
        )

        ui_ids = [
            item["kwargs"]["metadata"]["ui_message_id"]
            for item in memory.appended_messages
        ]
        self.assertEqual(ui_ids, [
            "runtime-v2-assistant:ui-turn:first",
            "runtime-v2-assistant:ui-turn:second",
        ])
        self.assertEqual(len(set(ui_ids)), 2)
        self.assertEqual(
            [item["kwargs"]["metadata"].get("visible_speaker") for item in memory.appended_messages],
            ["OPC", "OPC"],
        )

    async def test_company_mode_assistant_turn_is_raw_role_turn_not_task_generalist(self) -> None:
        memory = _StubMemoryManager(_StubStore())
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            config=OPCConfig(),
        )
        task = Task(
            id="company-role-task",
            title="Chao Intake",
            assigned_to="chao",
            session_id="sess-company",
            project_id="proj1",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "custom",
                "work_item_projection_id": "chao::intake",
            },
        )

        await runtime._persist_assistant_turn(
            task,
            "角色原始回复",
            [],
            runtime_session_id="rt_company",
            turn_id="ui-turn:company",
            conversation_turn_id="ui-turn:company",
            iteration=1,
        )

        metadata = memory.appended_messages[-1]["kwargs"]["metadata"]
        self.assertEqual(metadata["kind"], "runtime_v2_company_assistant")
        self.assertEqual(metadata["execution_mode"], "company_mode")
        self.assertTrue(metadata["company_runtime_raw_turn"])
        self.assertEqual(metadata["role_id"], "chao")
        # No tool calls ⇒ terminal iteration: flagged as the role's final
        # reply and given its own UI row id so the id-keyed backfill merge
        # cannot swallow it into the frozen shared-iteration row.
        self.assertTrue(metadata["company_final_turn"])
        self.assertEqual(metadata["ui_message_id"], "runtime-v2-company-assistant-final:ui-turn:company")
        self.assertEqual(
            metadata["result_delivery_id"],
            "result:task:company-role-task:turn:ui-turn:company:attempt:0",
        )
        self.assertEqual(metadata["source_task_id"], "company-role-task")
        self.assertEqual(metadata["child_session_id"], "sess-company")
        self.assertEqual(metadata["work_item_projection_id"], "chao::intake")
        self.assertNotIn("visible_speaker", metadata)

        await runtime._persist_assistant_turn(
            task,
            "中间轮叙述",
            [{"id": "call-1", "function": "file_read", "arguments": {}}],
            runtime_session_id="rt_company",
            turn_id="ui-turn:company:iter:1",
            conversation_turn_id="ui-turn:company",
            iteration=1,
        )

        intermediate_metadata = memory.appended_messages[-1]["kwargs"]["metadata"]
        self.assertEqual(intermediate_metadata["kind"], "runtime_v2_company_assistant")
        self.assertNotIn("company_final_turn", intermediate_metadata)
        self.assertNotIn("result_delivery_id", intermediate_metadata)
        self.assertEqual(
            intermediate_metadata["ui_message_id"],
            "runtime-v2-company-assistant:ui-turn:company",
        )

    async def test_task_mode_assistant_turn_with_company_defaults_stays_opc_task_reply(self) -> None:
        memory = _StubMemoryManager(_StubStore())
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            config=OPCConfig(),
        )
        task = Task(
            id="task-mode-with-company-defaults",
            title="Task Mode",
            assigned_to="task_generalist",
            session_id="sess-task",
            project_id="proj1",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "task_mode_execution",
                "task_mode_contract": "single_full_capability_main_agent",
            },
        )

        await runtime._persist_assistant_turn(
            task,
            "Task mode native answer.",
            [],
            runtime_session_id="rt_task",
            turn_id="ui-turn:task",
            conversation_turn_id="ui-turn:task",
            iteration=1,
        )

        metadata = memory.appended_messages[-1]["kwargs"]["metadata"]
        self.assertEqual(metadata["kind"], "runtime_v2_assistant")
        self.assertEqual(metadata["visible_speaker"], "OPC")
        self.assertEqual(metadata["ui_message_id"], "runtime-v2-assistant:ui-turn:task")
        self.assertNotIn("company_runtime_raw_turn", metadata)

    async def test_runtime_executes_tool_then_returns_second_turn_answer(self) -> None:
        registry = ToolRegistry()

        async def demo_tool(value: str) -> dict[str, str]:
            return {"echo": value}

        registry.register(
            ToolDefinition(
                name="demo_tool",
                description="Demo runtime tool",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                func=demo_tool,
                concurrency_safe=True,
                read_only=True,
            )
        )

        event_bus = _StubEventBus()
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            event_bus=event_bus,
            config=OPCConfig(),
            max_iterations=4,
        )

        result = await runtime.run(
            system_prompt="You are a test runtime.",
            user_message="Use the tool then finish.",
            task=Task(
                title="runtime-v2",
                description="Use runtime v2",
                session_id="sess-runtime-v2",
                project_id="proj1",
                metadata={
                    "mode": "task",
                    "work_item_projection_id": "implement_work_item",
                    "work_item_projection_title": "Implement Work Item",
                    "company_profile": "corporate",
                },
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertTrue(result.content.startswith("done"))
        self.assertIn("Verification:", result.content)
        self.assertEqual(result.artifacts["runtime_session_id"][:3], "rt_")
        runtime_events = [evt for evt in event_bus.events if getattr(evt, "event_type", "") == "runtime_event"]
        self.assertTrue(runtime_events)
        self.assertTrue(any(
            getattr(evt, "payload", {}).get("work_item_projection_id") == "implement_work_item"
            for evt in runtime_events
        ))
        self.assertTrue(any(
            getattr(evt, "payload", {}).get("type") == "prompt_prefix_state"
            for evt in runtime_events
        ))
        self.assertTrue(any(
            getattr(evt, "payload", {}).get("type") == "status_snapshot"
            for evt in runtime_events
        ))

    async def test_bootstrap_messages_restore_structured_transcript_on_resume(self) -> None:
        store = _StubStore()
        store.transcript = [
            {
                "message": _StubMessage("user"),
                "parts": [_StubPart("text", {"text": "Original request"})],
            },
            {
                "message": _StubMessage("assistant"),
                "parts": [
                    _StubPart("text", {"text": "Checking tool..."}),
                    _StubPart("tool_call", {
                        "tool_call_id": "tool-1",
                        "tool_name": "demo_tool",
                        "arguments": {"value": "hello"},
                    }),
                ],
            },
            {
                "message": _StubMessage("assistant"),
                "parts": [
                    _StubPart("tool_result", {
                        "tool_call_id": "tool-1",
                        "tool_name": "demo_tool",
                        "result": {"echo": "hello"},
                    }),
                ],
            },
        ]
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(store),
            config=OPCConfig(),
        )
        task = Task(
            title="resume",
            description="resume",
            session_id="sess-resume",
            project_id="proj1",
            context_snapshot={"runtime_resume": {"runtime_session_id": "rt_existing"}},
        )
        messages, base_prefix_len = await runtime._bootstrap_messages(
            system_prompt="System",
            user_content="Latest user reply",
            user_message="Latest user reply",
            context_messages=[
                {"role": "system", "content": "Runtime policy"},
                {"role": "system", "content": "Tool Strategy"},
            ],
            task=task,
        )
        self.assertEqual(base_prefix_len, 3)
        self.assertEqual(messages[0]["content"], "System")
        self.assertEqual(messages[1]["content"], "Runtime policy")
        self.assertEqual(messages[2]["content"], "Tool Strategy")
        self.assertEqual(messages[3]["role"], "user")
        self.assertEqual(messages[4]["role"], "assistant")
        self.assertEqual(messages[4]["tool_calls"][0]["function"]["name"], "demo_tool")
        self.assertEqual(messages[5]["role"], "tool")
        self.assertIn("echo", str(messages[5]["content"]))

    async def test_bootstrap_messages_resume_synthesizes_missing_tool_call_ids(self) -> None:
        store = _StubStore()
        store.transcript = [
            {
                "message": _StubMessage("user"),
                "parts": [_StubPart("text", {"text": "Original request"})],
            },
            {
                "message": _StubMessage("assistant"),
                "parts": [
                    _StubPart("text", {"text": "Checking tool..."}),
                    _StubPart("tool_call", {
                        "tool_call_id": "",
                        "tool_name": "demo_tool",
                        "arguments": {"value": "hello"},
                    }),
                ],
            },
            {
                "message": _StubMessage("assistant"),
                "parts": [
                    _StubPart("tool_result", {
                        "tool_call_id": "",
                        "tool_name": "demo_tool",
                        "result": {"echo": "hello"},
                    }),
                ],
            },
        ]
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(store),
            config=OPCConfig(),
        )
        task = Task(
            title="resume-missing-tool-id",
            description="resume-missing-tool-id",
            session_id="sess-resume-missing-tool-id",
            project_id="proj1",
            context_snapshot={"runtime_resume": {"runtime_session_id": "rt_existing"}},
        )

        messages, _ = await runtime._bootstrap_messages(
            system_prompt="System",
            user_content="Latest user reply",
            user_message="Latest user reply",
            context_messages=[],
            task=task,
        )

        synthesized_id = messages[2]["tool_calls"][0]["id"]
        self.assertTrue(synthesized_id)
        self.assertEqual(messages[3]["role"], "tool")
        self.assertEqual(messages[3]["tool_call_id"], synthesized_id)

    async def test_bootstrap_messages_resume_downgrades_orphan_tool_result_to_text(self) -> None:
        store = _StubStore()
        store.transcript = [
            {
                "message": _StubMessage("user"),
                "parts": [_StubPart("text", {"text": "Original request"})],
            },
            {
                "message": _StubMessage("assistant"),
                "parts": [
                    _StubPart("tool_result", {
                        "tool_call_id": "",
                        "tool_name": "demo_tool",
                        "result": {"echo": "hello"},
                    }),
                ],
            },
        ]
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(store),
            config=OPCConfig(),
        )
        task = Task(
            title="resume-orphan-tool-result",
            description="resume-orphan-tool-result",
            session_id="sess-resume-orphan-tool-result",
            project_id="proj1",
            context_snapshot={"runtime_resume": {"runtime_session_id": "rt_existing"}},
        )

        messages, _ = await runtime._bootstrap_messages(
            system_prompt="System",
            user_content="Latest user reply",
            user_message="Latest user reply",
            context_messages=[],
            task=task,
        )

        self.assertEqual([message["role"] for message in messages], ["system", "user", "assistant", "user"])
        self.assertIn("Tool result [demo_tool]", messages[2]["content"])
        self.assertNotIn("tool_call_id", messages[2])

    def test_sanitize_tool_message_sequence_downgrades_orphan_tool_output(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        sanitized = runtime._sanitize_tool_message_sequence([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "Thinking"},
            {"role": "tool", "tool_call_id": "call-1", "content": "{\"success\": true}"},
        ])

        self.assertEqual(sanitized[3]["role"], "assistant")
        self.assertIn("orphan call call-1", sanitized[3]["content"])

    async def test_agent_spawn_runtime_tool_passes_resident_flag(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        captured: dict[str, object] = {}

        class _Subagents:
            async def spawn(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, **kwargs}

        result = await runtime._handle_runtime_tool(
            subagents=_Subagents(),
            tool_name="agent_spawn",
            arguments={
                "profile": "implement",
                "prompt": "Implement the change",
                "background": True,
                "resident": True,
            },
            todo_state=[],
        )

        self.assertTrue(result["success"])
        self.assertEqual(captured["profile"], "implement")
        self.assertTrue(captured["resident"])

    async def test_agent_spawn_runtime_tool_normalizes_legacy_mode_names(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        captured: dict[str, object] = {}

        class _Subagents:
            async def spawn(self, **kwargs):
                captured.update(kwargs)
                return {"success": True, **kwargs}

        result = await runtime._handle_runtime_tool(
            subagents=_Subagents(),
            tool_name="agent_spawn",
            arguments={
                "profile": "implement",
                "prompt": "Implement the change",
                "mode": "bypassPermissions",
            },
            todo_state=[],
        )

        self.assertTrue(result["success"])
        self.assertEqual(captured["mode"], "bypass_permissions")

    async def test_verification_gate_blocks_completion_when_verifier_reports_issues(self) -> None:
        class _VerifyThenFinishLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                self.calls += 1
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                if self.calls == 1:
                    yield type("Evt", (), {
                        "event_type": "tool_call_delta",
                        "payload": {
                            "index": 0,
                            "id": "todo-1",
                            "name": "todo_write",
                            "arguments": json.dumps({
                                "todos": [
                                    {"content": "Inspect", "status": "in_progress"},
                                    {"content": "Implement", "status": "pending"},
                                    {"content": "Verify", "status": "pending"},
                                ]
                            }),
                        },
                        "model": "stub",
                    })()
                else:
                    yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "final answer"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        class _VerifierAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                _ = child_task
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=(
                        "ISSUES: missing validation evidence\n"
                        "Check: Validation coverage\n"
                        "Command: pytest -q tests/test_runtime.py\n"
                        "Observed Output: rollback validation test is missing\n"
                        "Result: FAIL\n"
                        "VERDICT: FAIL"
                    ),
                )

        with patch("opc.layer3_agent.runtime_v2.subagents.create_worktree", AsyncMock(return_value={"path": "."})), patch(
            "opc.layer3_agent.runtime_v2.subagents.cleanup_worktree",
            AsyncMock(),
        ):
            registry = ToolRegistry()
            for tool in create_todo_tools():
                registry.register(tool)
            runtime = NativeRuntimeV2(
                llm=_VerifyThenFinishLLM(),
                tool_registry=registry,
                memory_manager=_StubMemoryManager(_StubStore()),
                config=OPCConfig(),
                child_agent_factory=lambda profile, allowed_tools, prompt: _VerifierAgent(),
                max_iterations=4,
            )

            result = await runtime.run(
                system_prompt="You are a verifier-aware runtime.",
                user_message="Complete the task.",
                task=Task(
                    title="verify-runtime",
                    description="verify-runtime",
                    session_id="sess-verify",
                    project_id="proj1",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "verify_runtime",
                        "work_item_turn_type": "execute",
                        "company_profile": "corporate",
                    },
                ),
            )

        self.assertEqual(result.status, TaskStatus.AWAITING_HUMAN)
        self.assertIn("ISSUES:", result.content)
        self.assertIn("verification", result.artifacts)

    async def test_verification_failure_completes_hidden_company_turn_instead_of_blocking(self) -> None:
        """A failed verification on a non-user-visible company card (e.g. the
        hidden worker report/handoff turn) must complete as DONE rather than
        park on AWAITING_HUMAN — a hidden card can never surface an approval
        card, so blocking would deadlock the whole company run."""
        class _VerifyThenFinishLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                self.calls += 1
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                if self.calls == 1:
                    yield type("Evt", (), {
                        "event_type": "tool_call_delta",
                        "payload": {
                            "index": 0,
                            "id": "todo-1",
                            "name": "todo_write",
                            "arguments": json.dumps({
                                "todos": [
                                    {"content": "Inspect", "status": "in_progress"},
                                    {"content": "Implement", "status": "pending"},
                                    {"content": "Verify", "status": "pending"},
                                ]
                            }),
                        },
                        "model": "stub",
                    })()
                else:
                    yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "report handoff body"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        class _VerifierAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                _ = child_task
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=(
                        "ISSUES: missing validation evidence\n"
                        "Check: Validation coverage\n"
                        "Command: pytest -q tests/test_runtime.py\n"
                        "Observed Output: rollback validation test is missing\n"
                        "Result: FAIL\n"
                        "VERDICT: FAIL"
                    ),
                )

        with patch("opc.layer3_agent.runtime_v2.subagents.create_worktree", AsyncMock(return_value={"path": "."})), patch(
            "opc.layer3_agent.runtime_v2.subagents.cleanup_worktree",
            AsyncMock(),
        ):
            registry = ToolRegistry()
            for tool in create_todo_tools():
                registry.register(tool)
            runtime = NativeRuntimeV2(
                llm=_VerifyThenFinishLLM(),
                tool_registry=registry,
                memory_manager=_StubMemoryManager(_StubStore()),
                config=OPCConfig(),
                child_agent_factory=lambda profile, allowed_tools, prompt: _VerifierAgent(),
                max_iterations=4,
            )

            result = await runtime.run(
                system_prompt="You are a verifier-aware runtime.",
                user_message="Produce the handoff report.",
                task=Task(
                    title="report-handoff",
                    description="report-handoff",
                    session_id="sess-report",
                    project_id="proj1",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "report_runtime",
                        "work_item_turn_type": "report",
                        "company_profile": "corporate",
                        # Hidden worker report/handoff card: not user-visible.
                        "user_visible": False,
                        "report_execution_work_item": True,
                        "hidden_from_company_kanban": True,
                    },
                ),
            )

        # Must NOT block on a human — completes so the company run advances.
        self.assertEqual(result.status, TaskStatus.DONE)
        # The failed verdict is still recorded for audit / downstream review.
        self.assertIn("verification", result.artifacts)
        self.assertFalse(
            dict(result.artifacts.get("verification", {}) or {}).get("passed", True)
        )

    async def test_verification_gate_requires_structured_evidence_even_for_pass(self) -> None:
        class _VerifyThenFinishLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                self.calls += 1
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                if self.calls == 1:
                    yield type("Evt", (), {
                        "event_type": "tool_call_delta",
                        "payload": {
                            "index": 0,
                            "id": "todo-1",
                            "name": "todo_write",
                            "arguments": json.dumps({
                                "todos": [
                                    {"content": "Inspect", "status": "in_progress"},
                                    {"content": "Implement", "status": "pending"},
                                    {"content": "Verify", "status": "pending"},
                                ]
                            }),
                        },
                        "model": "stub",
                    })()
                else:
                    yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "final answer"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        class _VerifierAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                _ = child_task
                return TaskResult(status=TaskStatus.DONE, content="VERIFIED: looks acceptable\nVERDICT: PASS")

        with patch("opc.layer3_agent.runtime_v2.subagents.create_worktree", AsyncMock(return_value={"path": "."})), patch(
            "opc.layer3_agent.runtime_v2.subagents.cleanup_worktree",
            AsyncMock(),
        ):
            registry = ToolRegistry()
            for tool in create_todo_tools():
                registry.register(tool)
            runtime = NativeRuntimeV2(
                llm=_VerifyThenFinishLLM(),
                tool_registry=registry,
                memory_manager=_StubMemoryManager(_StubStore()),
                config=OPCConfig(),
                child_agent_factory=lambda profile, allowed_tools, prompt: _VerifierAgent(),
                max_iterations=4,
            )

            result = await runtime.run(
                system_prompt="You are a verifier-aware runtime.",
                user_message="Complete the task.",
                task=Task(
                    title="verify-runtime",
                    description="verify-runtime",
                    session_id="sess-verify",
                    project_id="proj1",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "verify_runtime",
                        "work_item_turn_type": "execute",
                        "company_profile": "corporate",
                    },
                ),
            )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.artifacts["verification_evidence"]["status"], "unavailable")
        self.assertEqual(result.artifacts["verification_evidence"]["verdict"], "partial")
        self.assertIn("verification unavailable", result.artifacts["verification_evidence"]["summary"].lower())

    def test_parse_verification_evidence_accepts_bullet_prefixed_lines(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        evidence = runtime._parse_verification_evidence(
            "- Check: smoke\n"
            "- Command: pytest -q\n"
            "- Observed Output: 12 passed\n"
            "- Result: PASS\n"
            "- VERDICT: PASS"
        )

        self.assertEqual(evidence.status, "provided")
        self.assertEqual(evidence.verdict, "pass")
        self.assertEqual(evidence.checks[0]["command"], "pytest -q")

    async def test_verification_gate_retries_missing_structure_once(self) -> None:
        class _VerifyThenFinishLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                self.calls += 1
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                if self.calls == 1:
                    yield type("Evt", (), {
                        "event_type": "tool_call_delta",
                        "payload": {
                            "index": 0,
                            "id": "todo-1",
                            "name": "todo_write",
                            "arguments": json.dumps({
                                "todos": [
                                    {"content": "Inspect", "status": "in_progress"},
                                    {"content": "Implement", "status": "pending"},
                                    {"content": "Verify", "status": "pending"},
                                ]
                            }),
                        },
                        "model": "stub",
                    })()
                else:
                    yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "final answer"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        verifier_prompts: list[str] = []

        def _child_agent_factory(profile, allowed_tools, prompt):
            _ = (profile, allowed_tools)
            verifier_prompts.append(prompt)

            class _VerifierAgent:
                async def execute(self, child_task: Task) -> TaskResult:
                    _ = child_task
                    if len(verifier_prompts) == 1:
                        return TaskResult(status=TaskStatus.DONE, content="Looks acceptable overall.")
                    return TaskResult(
                        status=TaskStatus.DONE,
                        content=(
                            "Check: smoke\n"
                            "Command: pytest -q\n"
                            "Observed Output: 1 passed\n"
                            "Result: PASS\n"
                            "VERIFIED: smoke checks passed\n"
                            "VERDICT: PASS"
                        ),
                    )

            return _VerifierAgent()

        with patch("opc.layer3_agent.runtime_v2.subagents.create_worktree", AsyncMock(return_value={"path": "."})), patch(
            "opc.layer3_agent.runtime_v2.subagents.cleanup_worktree",
            AsyncMock(),
        ):
            registry = ToolRegistry()
            for tool in create_todo_tools():
                registry.register(tool)
            runtime = NativeRuntimeV2(
                llm=_VerifyThenFinishLLM(),
                tool_registry=registry,
                memory_manager=_StubMemoryManager(_StubStore()),
                config=OPCConfig(),
                child_agent_factory=_child_agent_factory,
                max_iterations=4,
            )

            result = await runtime.run(
                system_prompt="You are a verifier-aware runtime.",
                user_message="Complete the task.",
                task=Task(
                    title="verify-runtime-repair",
                    description="verify-runtime-repair",
                    session_id="sess-verify-repair",
                    project_id="proj1",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "verify_runtime_repair",
                        "work_item_turn_type": "execute",
                        "company_profile": "corporate",
                    },
                ),
            )

        self.assertEqual(len(verifier_prompts), 2)
        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(result.artifacts["verification_evidence"]["status"], "provided")
        self.assertTrue(result.artifacts["verification"]["repair_attempted"])

    async def test_stream_tool_protocol_error_falls_back_to_non_stream_retry(self) -> None:
        class _ProtocolFallbackLLM:
            def __init__(self) -> None:
                self.config = type("Cfg", (), {"max_tokens": 2048})()
                self.stream_calls = 0
                self.chat_calls = 0

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            def is_tool_protocol_error(self, error: Exception) -> bool:
                return "no tool output found for function call" in str(error).lower()

            def sanitize_tool_call_history(self, messages):
                return list(messages)

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                self.stream_calls += 1
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                raise RuntimeError("No tool output found for function call call_demo123.")

            async def chat(self, messages, tools=None):
                _ = (messages, tools)
                self.chat_calls += 1
                return {
                    "content": "Recovered after provider tool protocol fallback.",
                    "tool_calls": [],
                    "finish_reason": "stop",
                    "model": "stub",
                    "cost": 0.0,
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        runtime = NativeRuntimeV2(
            llm=_ProtocolFallbackLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(_StubStore()),
            config=OPCConfig(),
            max_iterations=2,
        )

        result = await runtime.run(
            system_prompt="You are a fallback-capable runtime.",
            user_message="Complete the task.",
            task=Task(
                title="protocol-fallback",
                description="protocol-fallback",
                session_id="sess-protocol-fallback",
                project_id="proj1",
                metadata={"mode": "task"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("Recovered after provider tool protocol fallback.", result.content)

    @staticmethod
    def _make_provider_reject_llm(fail_times: int):
        """LLM stub whose stream fails ``fail_times`` times with an
        unclassified provider rejection (content-filter style), then answers."""

        class _ProviderRejectLLM:
            def __init__(self) -> None:
                self.config = type("Cfg", (), {"max_tokens": 2048})()
                self.stream_calls = 0
                self.seen_notice_payloads: list[list[str]] = []

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            def is_tool_protocol_error(self, error: Exception) -> bool:
                _ = error
                return False

            def sanitize_tool_call_history(self, messages):
                return list(messages)

            async def chat_stream(self, messages, tools=None):
                _ = tools
                self.stream_calls += 1
                self.seen_notice_payloads.append([
                    str(m.get("content", ""))
                    for m in messages
                    if m.get("role") == "system" and "[runtime notice]" in str(m.get("content", ""))
                ])
                if self.stream_calls <= fail_times:
                    yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                    raise RuntimeError(
                        "litellm.BadRequestError: OpenAIException - The request failed "
                        "because the input may contain sensitive information."
                    )
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                yield type("Evt", (), {
                    "event_type": "assistant_delta",
                    "payload": {"text": "Rephrased and continued."},
                    "model": "stub",
                })()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {}, "model": "stub"})()

            async def chat(self, messages, tools=None):
                raise AssertionError("non-stream fallback must not be used for unclassified errors")

        return _ProviderRejectLLM()

    async def test_unclassified_provider_error_feeds_error_back_and_recovers(self) -> None:
        llm = self._make_provider_reject_llm(fail_times=1)
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(_StubStore()),
            config=OPCConfig(),
            max_iterations=8,
        )

        result = await runtime.run(
            system_prompt="You are a resilient runtime.",
            user_message="Complete the task.",
            task=Task(
                title="provider-reject-recover",
                description="provider-reject-recover",
                session_id="sess-provider-reject-recover",
                project_id="proj1",
                metadata={"mode": "task"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("Rephrased and continued.", result.content)
        self.assertEqual(llm.stream_calls, 2)
        # The retry request must contain the provider's error text as a notice
        retry_notices = llm.seen_notice_payloads[1]
        self.assertEqual(len(retry_notices), 1)
        self.assertIn("sensitive information", retry_notices[0])
        self.assertIn("not a user action", retry_notices[0])

    async def test_unclassified_provider_error_retries_are_bounded_then_fail(self) -> None:
        llm = self._make_provider_reject_llm(fail_times=99)
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=ToolRegistry(),
            memory_manager=_StubMemoryManager(_StubStore()),
            config=OPCConfig(),
            max_iterations=20,
        )

        result = await runtime.run(
            system_prompt="You are a resilient runtime.",
            user_message="Complete the task.",
            task=Task(
                title="provider-reject-bounded",
                description="provider-reject-bounded",
                session_id="sess-provider-reject-bounded",
                project_id="proj1",
                metadata={"mode": "task"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("sensitive information", result.content)
        # 1 initial + 2 feedback retries + 1 context-reset retry = 4, never 20
        self.assertLessEqual(llm.stream_calls, 4)

    async def test_todo_write_normalizes_openopc_task_ledger_shape(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        class _Subagents:
            async def spawn(self, **kwargs):
                return {"success": True, **kwargs}

        todo_state: list[dict[str, object]] = []
        result = await runtime._handle_runtime_tool(
            subagents=_Subagents(),
            tool_name="todo_write",
            arguments={
                "todos": [
                    {"content": "Inspect runtime", "active_form": "Inspecting runtime", "status": "in_progress"},
                    {"content": "Write tests", "active_form": "Writing tests", "status": "in_progress"},
                    {"title": "Legacy step", "status": "done"},
                ]
            },
            todo_state=todo_state,
        )

        self.assertTrue(result["success"])
        self.assertEqual(todo_state[0]["status"], "in_progress")
        self.assertEqual(todo_state[0]["active_form"], "Inspecting runtime")
        self.assertNotIn("activeForm", todo_state[0])
        self.assertEqual(todo_state[1]["status"], "pending")
        self.assertEqual(todo_state[2]["status"], "completed")

    async def test_todo_write_accepts_legacy_active_form_alias(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )

        class _Subagents:
            async def spawn(self, **kwargs):
                return {"success": True, **kwargs}

        todo_state: list[dict[str, object]] = []
        result = await runtime._handle_runtime_tool(
            subagents=_Subagents(),
            tool_name="todo_write",
            arguments={
                "todos": [
                    {"content": "Inspect runtime", "activeForm": "Inspecting runtime", "status": "in_progress"},
                ]
            },
            todo_state=todo_state,
        )

        self.assertTrue(result["success"])
        self.assertEqual(todo_state[0]["active_form"], "Inspecting runtime")
        self.assertNotIn("activeForm", todo_state[0])

    def test_native_runtime_tool_schemas_use_openopc_field_names(self) -> None:
        todo_write = next(tool for tool in create_todo_tools() if tool.name == "todo_write")
        agent_spawn = next(tool for tool in create_agent_runtime_tools() if tool.name == "agent_spawn")

        todo_properties = todo_write.parameters["properties"]["todos"]["oneOf"][1]["items"]["properties"]
        agent_properties = agent_spawn.parameters["properties"]
        schema_text = json.dumps([todo_write.parameters, agent_spawn.parameters, todo_write.description, agent_spawn.description])

        self.assertIn("active_form", todo_properties)
        self.assertNotIn("activeForm", todo_properties)
        self.assertNotIn("run_in_background", agent_properties)
        legacy_style_phrase = "Claude" + " Code-style"
        legacy_compat_phrase = "Claude" + " Code-compatible"
        self.assertNotIn(legacy_style_phrase, schema_text)
        self.assertNotIn(legacy_compat_phrase, schema_text)

    async def test_runtime_restores_task_ledger_from_resume_state(self) -> None:
        class _FinalOnlyLLM:
            def __init__(self) -> None:
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            async def chat_stream(self, messages, tools=None):
                _ = (messages, tools)
                yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "done"}, "model": "stub"})()
                yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

        runtime = NativeRuntimeV2(
            llm=_FinalOnlyLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
            max_iterations=2,
        )
        task = Task(
            title="resume-ledger",
            description="resume-ledger",
            session_id="sess-ledger",
            project_id="proj1",
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt_resume",
                    "task_ledger": [
                        {"content": "Inspect", "status": "in_progress"},
                        {"content": "Implement", "status": "pending"},
                    ],
                }
            },
        )

        result = await runtime.run(
            system_prompt="You are a runtime.",
            user_message="Continue.",
            task=task,
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(len(result.artifacts["task_ledger"]), 2)
        self.assertEqual(result.artifacts["task_ledger"][0]["status"], "in_progress")

    async def test_prefetch_is_consumed_on_following_turn(self) -> None:
        registry = ToolRegistry()

        async def demo_tool(value: str) -> dict[str, str]:
            return {"echo": value}

        registry.register(
            ToolDefinition(
                name="demo_tool",
                description="Demo runtime tool",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                func=demo_tool,
                concurrency_safe=True,
                read_only=True,
            )
        )

        async def prefetch_provider(task: Task, query: str, messages: list[dict[str, object]]) -> dict[str, str]:
            _ = (task, query, messages)
            await asyncio.sleep(0)
            return {"focused_memory": "## Focused Memory\nRemember the last read result."}

        event_bus = _StubEventBus()
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            event_bus=event_bus,
            config=OPCConfig(),
            prefetch_provider=prefetch_provider,
            max_iterations=4,
        )

        result = await runtime.run(
            system_prompt="You are a prefetch runtime.",
            user_message="Use the tool and continue.",
            task=Task(
                title="prefetch-runtime",
                description="prefetch-runtime",
                session_id="sess-prefetch",
                project_id="proj1",
                metadata={"mode": "task"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("focused_memory", result.artifacts["prefetch_hits"])
        runtime_events = [evt for evt in event_bus.events if getattr(evt, "event_type", "") == "runtime_event"]
        self.assertTrue(any(getattr(evt, "payload", {}).get("type") == "prefetch_consumed" for evt in runtime_events))

    async def test_context_guard_preserves_recovery_metadata(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            event_bus=_StubEventBus(),
            config=OPCConfig(),
        )
        result = {
            "success": True,
            "result": {
                "content": "C" * 20000,
                "full_output_path": "/tmp/full-output.json",
                "truncated": True,
                "next_offset": 42,
            },
        }

        clipped = runtime._clip_tool_result_for_history("demo_tool", result)
        payload = clipped["result"]

        self.assertEqual(payload["full_output_path"], "/tmp/full-output.json")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["next_offset"], 42)
        self.assertTrue(payload["content_truncated"])
        self.assertIn("tool output truncated by context_guard", payload["content"])

    async def test_streaming_tool_start_prefires_completed_read_only_call(self) -> None:
        registry = ToolRegistry()

        async def demo_tool(value: str) -> dict[str, str]:
            return {"echo": value}

        registry.register(
            ToolDefinition(
                name="demo_tool",
                description="Demo runtime tool",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}},
                func=demo_tool,
                concurrency_safe=True,
                read_only=True,
            )
        )
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
        )
        planner = ToolPlanner(registry)
        resolver = _policy_adapter()
        executor = StreamingToolExecutor(
            registry=registry,
            planner=planner,
            permission_resolver=resolver,
        )
        early_tool_runs: dict[int, dict[str, object]] = {}

        await runtime._maybe_start_streaming_tool_calls(
            upto_index=1,
            tool_call_chunks={
                0: {"id": "tool-1", "function": "demo_tool", "arguments_chunks": ['{"value":"hello"}']},
                1: {"id": "tool-2", "function": "demo_tool", "arguments_chunks": ['{"value":"later"}']},
            },
            early_tool_runs=early_tool_runs,
            executor=executor,
            planner=planner,
            permission_resolver=resolver,
            task=Task(title="streaming-start"),
            on_progress=None,
            runtime_session_id="rt_stream_start",
        )

        self.assertIn(0, early_tool_runs)
        self.assertEqual((await early_tool_runs[0]["task"])[0]["result"]["result"]["echo"], "hello")

    def test_tool_aware_microcompact_preserves_failure_signal(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=ToolRegistry(),
            config=OPCConfig(),
        )
        content = json.dumps({
            "success": False,
            "error": "command failed",
            "result": {"stderr": "x" * 9000},
        })
        compacted = runtime._apply_tool_aware_microcompact(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "prompt"},
                {"role": "tool", "content": content},
                {"role": "assistant", "content": "older-1"},
                {"role": "assistant", "content": "older-2"},
                {"role": "assistant", "content": "older-3"},
                {"role": "assistant", "content": "older-4"},
                {"role": "assistant", "content": "older-5"},
                {"role": "assistant", "content": "older-6"},
                {"role": "assistant", "content": "older-7"},
                {"role": "assistant", "content": "older-8"},
            ],
            base_prefix_len=2,
        )

        self.assertIn("command failed", compacted[2]["content"])
        self.assertIn("[tool failure output truncated]", compacted[2]["content"])


class ToolPlannerTests(unittest.TestCase):
    def test_read_only_calls_batch_together(self) -> None:
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="file_read",
            description="read",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            concurrency_safe=True,
            read_only=True,
        ))
        registry.register(ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            concurrency_safe=False,
            read_only=False,
        ))
        planner = ToolPlanner(registry)
        batches = planner.partition([
            {"id": "1", "function": "file_read", "arguments": {}},
            {"id": "2", "function": "file_read", "arguments": {}},
            {"id": "3", "function": "shell_exec", "arguments": {}},
        ])
        self.assertEqual(len(batches), 2)
        self.assertTrue(batches[0].concurrency_safe)
        self.assertEqual(len(batches[0].calls), 2)
        self.assertFalse(batches[1].concurrency_safe)


class _ApprovalPrefsStub:
    def __init__(self, opc_home: Path | None = None) -> None:
        if opc_home is not None:
            self.opc_home = opc_home

    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs):
        _ = kwargs


class _ApprovalStoreStub:
    async def record_approval(self, **kwargs):
        _ = kwargs


class _ApprovalMemoryStub:
    def append_autonomy_event(self, event, project=False):
        _ = (event, project)


def _build_permission_policy(
    config: AutonomyConfig | None = None,
    opc_home: Path | None = None,
) -> ApprovalEngine:
    return ApprovalEngine(
        llm=object(),
        store=_ApprovalStoreStub(),
        preferences=_ApprovalPrefsStub(opc_home),
        memory=_ApprovalMemoryStub(),
        escalation=None,
        config=config or AutonomyConfig(),
    )


def _policy_adapter(
    config: AutonomyConfig | None = None,
    opc_home: Path | None = None,
) -> RuntimePermissionAdapter:
    return RuntimePermissionAdapter(_build_permission_policy(config, opc_home))


class PermissionAdapterTests(unittest.TestCase):
    def test_approve_session_maps_to_session_scope(self) -> None:
        adapter = RuntimePermissionAdapter()
        decision = adapter.decision_from_result(
            "shell_exec",
            {"command": "git status"},
            {"approval": {"human_reply": "approve_session"}, "success": True},
        )
        self.assertEqual(decision.scope, PermissionScope.SESSION)

    def test_dangerous_shell_pattern_requires_prompt(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        )
        decision = policy.predict(tool, {"command": "rm -rf build"})
        self.assertEqual(decision.resolution, PermissionResolution.ASK)
        self.assertEqual(decision.risk_level, RiskLevel.CRITICAL)

    def test_denied_path_blocks_preflight(self) -> None:
        policy = _build_permission_policy(
            AutonomyConfig(permissions_v2=PermissionsV2Config(denied_paths=["D:/forbidden"]))
        )
        tool = ToolDefinition(
            name="file_write",
            description="write",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            concurrency_safe=False,
            read_only=False,
        )
        decision = policy.predict(tool, {"path": "D:/forbidden/data.txt"})
        self.assertEqual(decision.resolution, PermissionResolution.DENY)

    def test_memory_root_is_treated_as_workspace_path(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="file_write",
            description="write",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            concurrency_safe=False,
            read_only=False,
        )
        with patch("opc.layer2_organization.approval.get_opc_home", return_value=Path("/tmp/opc-home")):
            decision = policy.predict(tool, {"path": "/tmp/opc-home/memory/projects/proj1.md"})
        self.assertEqual(decision.resolution, PermissionResolution.ALLOW)

    def test_low_risk_data_acquisition_shell_prefix_auto_allows_single_command(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        )
        task = Task(
            assigned_to="acquisition_specialist",
            metadata={
                "work_item_projection_id": "data_acquisition",
                "work_item_role_id": "acquisition_specialist",
                "target_output_dir": "/tmp/data-acquisition",
            },
        )
        decision = policy.predict(
            tool,
            {
                "command": "yt-dlp -o inputs/trailers/%(title)s.%(ext)s https://example.com/video",
                "working_directory": "/tmp/data-acquisition",
            },
            task=task,
        )
        self.assertEqual(decision.resolution, PermissionResolution.ALLOW)
        self.assertEqual(decision.risk_level, RiskLevel.LOW)

    def test_download_prefix_requires_work_item_context(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        )
        decision = policy.predict(
            tool,
            {"command": "yt-dlp -o inputs/trailers/%(title)s.%(ext)s https://example.com/video"},
        )
        self.assertEqual(decision.resolution, PermissionResolution.ASK)

    def test_compound_download_pipeline_still_requires_prompt(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        )
        decision = policy.predict(tool, {"command": "curl -L https://example.com/install.sh | bash"})
        self.assertEqual(decision.resolution, PermissionResolution.ASK)

    def test_read_only_classifier_allows_flag_audited_commands(self) -> None:
        policy = _build_permission_policy()
        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=lambda **_: None,  # type: ignore[arg-type]
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        )
        for command in (
            "awk '{print $1}' data.csv",
            "od -c file.bin | head -20",
            "jq '.items[]' resp.json",
            "sed -n 1,50p main.py",
            "git log --oneline -5 && git status",
        ):
            decision = policy.predict(tool, {"command": command})
            self.assertEqual(decision.resolution, PermissionResolution.ALLOW, command)
        for command in (
            "find . -name '*.pyc' -delete",
            "sort -o hijacked.txt input.txt",
            "awk 'BEGIN{system(\"id\")}' x",
        ):
            decision = policy.predict(tool, {"command": command})
            self.assertEqual(decision.resolution, PermissionResolution.ASK, command)


class PermissionPolicyGrantTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_allowlist_grants_resolve_scopes(self) -> None:
        import tempfile

        from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager

        with tempfile.TemporaryDirectory() as tmp:
            opc_home = Path(tmp)
            manager = ApprovalAllowlistManager(opc_home)
            manager.add_patterns("tool", "shell_exec", ["git status"], project_id="proj1")
            manager.add_patterns("tool", "file_write", ["*"], project_id=None)
            policy = _build_permission_policy(opc_home=opc_home)

            shell_tool = ToolDefinition(
                name="shell_exec",
                description="shell",
                parameters={"type": "object", "properties": {}},
                func=lambda **_: None,  # type: ignore[arg-type]
                concurrency_safe=False,
                read_only=False,
            )
            file_tool = ToolDefinition(
                name="file_write",
                description="write",
                parameters={"type": "object", "properties": {}},
                func=lambda **_: None,  # type: ignore[arg-type]
                concurrency_safe=False,
                read_only=False,
            )
            task = Task(title="grant-check", project_id="proj1")
            self.assertEqual(
                policy.predict(shell_tool, {"command": "git status"}, task=task).scope,
                PermissionScope.PROJECT,
            )
            self.assertEqual(
                policy.predict(file_tool, {"path": "any.txt"}, task=task).scope,
                PermissionScope.GLOBAL,
            )


class StreamingToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_emits_permission_requested_for_ask_flow(self) -> None:
        registry = ToolRegistry()

        async def shell_tool(command: str) -> dict[str, str]:
            return {"stdout": command}

        registry.register(ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=shell_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        events: list[tuple[str, dict[str, object]]] = []
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=_policy_adapter(),
            emit_event=lambda event_type, payload: _async_append(events, event_type, payload),
        )
        results = await executor.execute([
            {"id": "call-1", "function": "shell_exec", "arguments": {"command": "git commit -m test"}},
        ])
        self.assertTrue(results[0]["result"]["success"])
        event_types = [item[0] for item in events]
        self.assertIn("permission_predicted", event_types)
        self.assertIn("permission_requested", event_types)
        self.assertIn("permission_resolved", event_types)
        completed_payload = next(payload for name, payload in events if name == "tool_completed")
        self.assertIn("started_at_ms", completed_payload)
        self.assertIn("completed_at_ms", completed_payload)
        self.assertIn("elapsed_ms", completed_payload)
        self.assertIn("result_summary", completed_payload)

    async def test_executor_short_circuits_denied_preflight(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def write_tool(path: str) -> dict[str, str]:
            executed.append(path)
            return {"path": path}

        registry.register(ToolDefinition(
            name="file_write",
            description="write",
            parameters={"type": "object", "properties": {}},
            func=write_tool,
            concurrency_safe=False,
            read_only=False,
        ))
        events: list[tuple[str, dict[str, object]]] = []
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=_policy_adapter(AutonomyConfig(permissions_v2=PermissionsV2Config(deny_tools=["file_write"]))),
            emit_event=lambda event_type, payload: _async_append(events, event_type, payload),
        )
        results = await executor.execute([
            {"id": "call-1", "function": "file_write", "arguments": {"path": "blocked.txt"}},
        ])
        self.assertFalse(results[0]["result"]["success"])
        self.assertEqual(executed, [])
        self.assertTrue(any(name == "permission_requested" for name, _ in events))

    async def test_executor_retries_shell_with_escalated_sandbox(self) -> None:
        registry = ToolRegistry()
        observed_modes: list[str] = []

        async def shell_tool(command: str, task: Task | None = None) -> dict[str, object]:
            _ = command
            context = dict((getattr(task, "metadata", {}) or {}).get("_execution_context", {}) or {})
            sandbox = dict(context.get("sandbox", {}) or {})
            mode = str(sandbox.get("mode", "") or "off")
            observed_modes.append(mode)
            if mode != "elevated":
                return {
                    "stdout": "",
                    "stderr": "sandbox unavailable",
                    "exit_code": 1,
                    "timed_out": False,
                    "sandbox": {
                        "platform": "linux",
                        "requested_mode": mode,
                        "effective_mode": mode,
                        "available": False,
                        "fallback_used": False,
                    },
                }
            return {
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "sandbox": {
                    "platform": "linux",
                    "requested_mode": mode,
                    "effective_mode": mode,
                    "available": True,
                    "fallback_used": False,
                },
            }

        registry.register(ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {}},
            func=shell_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
        )
        task = Task(id="sandbox-task", session_id="sandbox-session", project_id="proj1")
        task.metadata["_execution_context"] = {
            "sandbox": {
                "platform": "linux",
                "mode": "workspace-write",
                "allow_network": True,
            }
        }
        events: list[tuple[str, dict[str, object]]] = []
        hook_bus = runtime._build_tool_hook_bus(
            runtime_session_id="rt_sandbox",
            task=task,
            permission_resolver=_policy_adapter(),
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=_policy_adapter(),
            hook_bus=hook_bus,
            emit_event=lambda event_type, payload: _async_append(events, event_type, payload),
        )

        results = await executor.execute([
            {"id": "call-1", "function": "shell_exec", "arguments": {"command": "echo hi"}},
        ], task=task)

        self.assertTrue(results[0]["result"]["success"])
        self.assertEqual(observed_modes, ["workspace-write", "elevated"])
        event_types = [name for name, _ in events]
        self.assertIn("sandbox_retry_requested", event_types)
        self.assertIn("sandbox_retry_completed", event_types)


class SubagentManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_subagent_receives_message_and_wait_returns_result(self) -> None:
        async def execute_with_message(child_task: Task) -> TaskResult:
            queue = getattr(child_task, "_runtime_inbox_queue")
            message = await queue.get()
            return TaskResult(status=TaskStatus.DONE, content=f"received:{message}")

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                return await execute_with_message(child_task)

        manager = SubagentManager(
            parent_task=Task(id="parent", session_id="sess-parent", project_id="proj1"),
            config=OPCConfig(),
            child_agent_factory=lambda profile, allowed_tools, prompt: _ChildAgent(),
            store=_SubagentStore(),
            runtime_session_id="rt_1",
        )

        spawned = await manager.spawn(
            profile="general",
            prompt="Do the work",
            background=True,
            isolation="shared",
        )
        self.assertTrue(spawned["success"])
        agent_id = spawned["agent_id"]
        sent = await manager.send(agent_id, "follow this")
        waited = await manager.wait(agent_id, timeout_seconds=3)

        self.assertTrue(sent["success"])
        self.assertTrue(waited["success"])
        self.assertIn("follow this", waited["result"])
        self.assertIn("message_class", waited["result"])

    async def test_resident_background_subagent_goes_idle_and_resumes_same_id(self) -> None:
        prompts: list[str] = []
        event_bus = _StubEventBus()

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                prompts.append(child_task.description)
                await asyncio.sleep(0)
                return TaskResult(status=TaskStatus.DONE, content=f"done:{child_task.description}")

        manager = SubagentManager(
            parent_task=Task(id="parent", session_id="sess-parent", project_id="proj1"),
            config=OPCConfig(),
            child_agent_factory=lambda profile, allowed_tools, prompt: _ChildAgent(),
            event_bus=event_bus,
            store=_SubagentStore(),
            runtime_session_id="rt_resident",
        )

        spawned = await manager.spawn(
            profile="general",
            prompt="first turn",
            background=True,
            isolation="shared",
            resident=True,
            name="helper",
        )

        self.assertTrue(spawned["success"])
        agent_id = spawned["agent_id"]

        first_wait = await manager.wait(agent_id, timeout_seconds=3)
        self.assertTrue(first_wait["success"])
        self.assertTrue(first_wait["resident"])
        self.assertEqual(first_wait["resident_status"], "idle")
        self.assertEqual(first_wait["agent_id"], agent_id)

        sent = await manager.send(agent_id, "second turn")
        self.assertTrue(sent["success"])

        for _ in range(40):
            if len(prompts) >= 2:
                break
            await asyncio.sleep(0)

        second_wait = await manager.wait("helper", timeout_seconds=3)
        self.assertTrue(second_wait["success"])
        self.assertEqual(second_wait["agent_id"], agent_id)
        self.assertEqual(second_wait["resident_status"], "idle")
        self.assertEqual(prompts[:2], ["first turn", "second turn"])
        runtime_event_types = [
            getattr(evt, "payload", {}).get("type")
            for evt in event_bus.events
            if getattr(evt, "event_type", "") == "runtime_event"
        ]
        self.assertIn("worker_notification", runtime_event_types)

    async def test_nonresident_subagent_rejects_follow_up_after_completion(self) -> None:
        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                _ = child_task
                return TaskResult(status=TaskStatus.DONE, content="done")

        manager = SubagentManager(
            parent_task=Task(id="parent", session_id="sess-parent", project_id="proj1"),
            config=OPCConfig(),
            child_agent_factory=lambda profile, allowed_tools, prompt: _ChildAgent(),
            store=_SubagentStore(),
            runtime_session_id="rt_terminal",
        )

        spawned = await manager.spawn(
            profile="general",
            prompt="one shot",
            background=True,
            isolation="shared",
        )
        waited = await manager.wait(spawned["agent_id"], timeout_seconds=3)
        sent = await manager.send(spawned["agent_id"], "follow up")

        self.assertTrue(waited["success"])
        self.assertFalse(sent["success"])
        self.assertIn("already completed", sent["error"])

    async def test_worktree_lifecycle_is_recorded(self) -> None:
        store = _SubagentStore()

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                _ = child_task
                return TaskResult(status=TaskStatus.DONE, content="done")

        manager = SubagentManager(
            parent_task=Task(id="parent", session_id="sess-parent", project_id="proj1"),
            config=OPCConfig(),
            child_agent_factory=lambda profile, allowed_tools, prompt: _ChildAgent(),
            store=store,
            runtime_session_id="rt_2",
        )

        with patch("opc.layer3_agent.runtime_v2.subagents.create_worktree", AsyncMock(return_value={
            "path": "D:/wt-demo",
            "mode": "copy",
        })), patch("opc.layer3_agent.runtime_v2.subagents.cleanup_worktree", AsyncMock()) as cleanup:
            spawned = await manager.spawn(
                profile="implement",
                prompt="Write code",
                background=True,
                isolation="worktree",
            )
            waited = await manager.wait(spawned["agent_id"], timeout_seconds=3)

        self.assertTrue(spawned["success"])
        self.assertTrue(waited["success"])
        self.assertEqual(store.worktree_sessions[0]["status"], "active")
        self.assertEqual(store.worktree_sessions[-1]["status"], "closed")
        cleanup.assert_awaited()

    async def test_plan_mode_uses_profile_defaults_and_name_alias(self) -> None:
        store = _SubagentStore()
        config = OPCConfig()
        config.agents.native_subagents["implement"] = NativeSubagentProfileConfig(
            model="openai/gpt-5.4",
            max_iterations=12,
            default_isolation="worktree",
        )
        captured: dict[str, object] = {}

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                captured["task_title"] = child_task.title
                captured["task_metadata"] = dict(child_task.metadata)
                return TaskResult(status=TaskStatus.DONE, content="planned")

        def _factory(profile, allowed_tools, prompt, overrides):
            captured["profile"] = profile
            captured["allowed_tools"] = list(allowed_tools)
            captured["prompt"] = prompt
            captured["overrides"] = dict(overrides)
            return _ChildAgent()

        manager = SubagentManager(
            parent_task=Task(id="parent", session_id="sess-parent", project_id="proj1"),
            config=config,
            child_agent_factory=_factory,
            store=store,
            runtime_session_id="rt_3",
        )

        spawned = await manager.spawn(
            profile="implement",
            prompt="Draft an implementation plan",
            mode="plan",
            name="planner",
            description="Planning worker",
        )
        waited = await manager.wait("planner", timeout_seconds=3)

        self.assertTrue(spawned["success"])
        self.assertEqual(spawned["name"], "planner")
        self.assertTrue(waited["success"])
        self.assertEqual(waited["name"], "planner")
        self.assertEqual(captured["profile"], "implement")
        self.assertNotIn("file_write", captured["allowed_tools"])
        self.assertEqual(captured["overrides"]["model"], "openai/gpt-5.4")
        self.assertEqual(captured["task_title"], "planner")

    async def test_verify_subagent_skips_recursive_verification_and_does_not_inherit_parent_tool_whitelist(self) -> None:
        store = _SubagentStore()
        captured: dict[str, object] = {}

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                captured["task_metadata"] = dict(child_task.metadata)
                return TaskResult(status=TaskStatus.DONE, content="verified")

        def _factory(profile, allowed_tools, prompt, overrides):
            _ = (prompt, overrides)
            captured["profile"] = profile
            captured["allowed_tools"] = list(allowed_tools)
            return _ChildAgent()

        manager = SubagentManager(
            parent_task=Task(
                id="parent",
                session_id="sess-parent",
                project_id="proj1",
                metadata={
                    "_fork_allowed_tools": ["read_inbox", "send_dm"],
                    "work_item_verification_required": True,
                },
            ),
            config=OPCConfig(),
            child_agent_factory=_factory,
            store=store,
            runtime_session_id="rt_verify",
        )

        spawned = await manager.spawn(
            profile="verify",
            prompt="Verify the parent work",
            background=True,
            isolation="shared",
        )
        waited = await manager.wait(spawned["agent_id"], timeout_seconds=3)

        self.assertTrue(spawned["success"])
        self.assertTrue(waited["success"])
        self.assertEqual(captured["profile"], "verify")
        self.assertIn("shell_exec", captured["allowed_tools"])
        self.assertIn("file_read", captured["allowed_tools"])
        self.assertNotIn("read_inbox", captured["allowed_tools"])
        self.assertTrue(captured["task_metadata"]["skip_verification"])
        self.assertFalse(captured["task_metadata"]["work_item_verification_required"])
        self.assertNotIn("_fork_allowed_tools", captured["task_metadata"])


class ProviderFingerprintTests(unittest.TestCase):
    def test_cache_fingerprint_is_stable(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-5.4"))
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        tools = [{"name": "demo", "parameters": {"type": "object", "properties": {}}}]
        a = provider.build_cache_fingerprint(messages=messages, tools=tools)
        b = provider.build_cache_fingerprint(messages=messages, tools=tools)
        self.assertEqual(a, b)


async def _async_append(
    events: list[tuple[str, dict[str, object]]],
    event_type: str,
    payload: dict[str, object],
) -> None:
    events.append((event_type, payload))


if __name__ == "__main__":
    unittest.main()
