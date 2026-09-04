from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from opc.core.models import Task, TaskStatus
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.plugins.office_ui.event_adapter import EventAdapter
from opc.plugins.office_ui.snapshot_builder import (
    _resolve_task_selected_execution_agent as resolve_snapshot_selected_execution_agent,
)
from opc.plugins.office_ui.ws_handler import WSHandler


class WSHandlerProgressParsingTests(unittest.TestCase):
    def test_tool_progress_preserves_full_detail_and_unicode(self) -> None:
        detail = json.dumps(
            {
                "todos": '[{"id":"1","title":"梳理需求","status":"in_progress"}]',
                "note": "x" * 180,
            },
            ensure_ascii=False,
        )

        entry = WSHandler._parse_progress_entry(f"[Tool: todo_write] {detail}")

        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "tool_call")
        self.assertEqual(entry["summary"], "todo_write")
        self.assertEqual(entry["detail"], detail)
        self.assertIn("梳理需求", entry["detail"])
        self.assertGreater(len(entry["detail"]), 100)

    def test_external_codex_thinking_progress_maps_to_thinking_entry(self) -> None:
        entry = WSHandler._parse_progress_entry("[External:codex:thinking] 我先检查 UI 渲染链路。")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "thinking")
        self.assertEqual(entry["summary"], "我先检查 UI 渲染链路。")
        self.assertEqual(entry["detail"], "我先检查 UI 渲染链路。")

    def test_external_jiuwen_snapshot_maps_to_thinking_entry(self) -> None:
        entry = WSHandler._parse_progress_entry(
            "[External:jiuwenswarm:thinking_snapshot] 正在搜索并核对论文来源。",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "thinking")
        self.assertEqual(entry["summary"], "正在搜索并核对论文来源。")
        self.assertEqual(entry["detail"], "正在搜索并核对论文来源。")

    def test_external_codex_tool_progress_maps_to_tool_call(self) -> None:
        detail = "$ git status --short\n\n M opc/plugins/office_ui/ws_handler.py"
        entry = WSHandler._parse_progress_entry(f"[External:codex:tool] {detail}")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "tool_call")
        self.assertEqual(entry["summary"], "git status --short")
        self.assertEqual(entry["detail"], detail)

    def test_external_cursor_tool_progress_preserves_search_detail(self) -> None:
        detail = (
            "web search: Cursor pricing plans\n"
            "- Cursor · Pricing — https://www.cursor.com/pricing"
        )
        entry = WSHandler._parse_progress_entry(f"[External:cursor:tool] {detail}")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "tool_call")
        self.assertEqual(entry["summary"], "web search: Cursor pricing plans")
        self.assertEqual(entry["detail"], detail)

    def test_delegation_progress_preserves_external_agent_name(self) -> None:
        entry = WSHandler._parse_progress_entry("[Delegating to opencode] task=hello")

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "handoff")
        self.assertEqual(entry["summary"], "Delegating to opencode")

    def test_plain_final_reply_is_not_parsed_as_thinking_progress(self) -> None:
        entry = WSHandler._parse_progress_entry(
            "I've completed the implementation and validated the happy path.",
        )

        self.assertIsNone(entry)

    def test_company_assistant_delta_becomes_assistant_progress_entry(self) -> None:
        # Company mode surfaces the role's narration/final reply in its
        # progress transcript (parity with external agents' [External:*:result]).
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "assistant_delta",
                "text": "final answer token",
                "execution_mode": "company_mode",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "assistant")
        self.assertEqual(entry["summary"], "final answer token")
        self.assertEqual(entry["detail"], "final answer token")

    def test_task_mode_assistant_delta_is_not_recorded_as_progress_entry(self) -> None:
        # Task mode already streams the reply as the draft; a progress entry
        # would duplicate it.
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "assistant_delta",
                "text": "final answer token",
                "execution_mode": "task_mode",
            },
        )

        self.assertIsNone(entry)

    def test_company_mode_hides_runtime_bookkeeping_events(self) -> None:
        for runtime_type in (
            "turn_started",
            "turn_completed",
            "status_snapshot",
            "context_usage",
            "cost_update",
            "member_inbox_updated",
        ):
            entry = WSHandler._runtime_event_to_progress_entry(
                {
                    "type": runtime_type,
                    "current_tool": "shell_exec",
                    "turn_cost_usd": 0.0123,
                },
            )
            self.assertIsNone(entry, runtime_type)

    def test_runtime_member_claimed_work_item_maps_to_started_entry(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "member_claimed_work_item",
                "work_item_projection_id": "engineering_execution",
                "message_priority": "manager",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "work_item_started")
        self.assertEqual(entry["summary"], "Work item resumed")
        self.assertEqual(entry["detail"], "Claimed from manager queue.")
        self.assertEqual(entry["work_item_projection_id"], "engineering_execution")
        self.assertEqual(entry["work_item_projection_title"], "Engineering Execution")
        self.assertNotIn("projection_id", entry)
        self.assertNotIn("legacy_title", entry)
        self.assertTrue(entry["is_company_runtime"])

    def test_runtime_context_warning_prefers_token_count_over_remaining_pct(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "context_warning",
                "context_tokens": 3200,
                "context_window": 8000,
                "context_remaining_pct": 70,
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "status_change")
        self.assertEqual(entry["summary"], "Context usage high")
        self.assertEqual(entry["detail"], "3200/8000 tokens | 40% used")

    def test_company_mode_tool_completed_stays_tool_call_with_stable_item_id(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "tool_completed",
                "turn_id": "rt-1:2",
                "tool_call_id": "call-1",
                "tool_name": "web_search",
                "result_summary": "3 results",
                "work_item_projection_id": "engineering_execution",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "tool_call")
        self.assertEqual(entry["summary"], "web_search")
        self.assertEqual(entry["detail"], "3 results")
        self.assertEqual(entry["item_id"], "rt-1:2:tool:call-1")
        self.assertEqual(entry["stream_id"], "rt-1:2:tool:call-1")
        self.assertEqual(entry["tool_call_id"], "call-1")
        self.assertTrue(entry["is_company_runtime"])

    def test_thinking_delta_preserves_fragment_whitespace(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "thinking_delta",
                "turn_id": "rt-1:1",
                "item_id": "rt-1:1:thinking",
                "seq": 2,
                "text": " wants to analyze",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["detail"], " wants to analyze")
        self.assertEqual(entry["summary"], "wants to analyze")

    def test_thinking_delta_whitespace_only_fragment_is_skipped(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "thinking_delta",
                "turn_id": "rt-1:1",
                "item_id": "rt-1:1:thinking",
                "seq": 3,
                "text": "  \n ",
            },
        )

        self.assertIsNone(entry)

    def test_company_mode_thinking_summary_previews_content(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "thinking_delta",
                "turn_id": "rt-1:1",
                "item_id": "rt-1:1:thinking",
                "seq": 1,
                "text": "先梳理竞品清单，再对比功能矩阵。",
                "work_item_projection_id": "engineering_execution",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "thinking")
        self.assertEqual(entry["summary"], "先梳理竞品清单，再对比功能矩阵。")
        self.assertEqual(entry["detail"], "先梳理竞品清单，再对比功能矩阵。")

    def test_task_mode_low_value_runtime_events_are_hidden(self) -> None:
        for runtime_type in (
            "turn_started",
            "turn_completed",
            "status_snapshot",
            "context_usage",
            "cost_update",
            "durable_memory_extracted",
            "tool_batch_started",
            "tool_batch_completed",
            "permission_predicted",
            "session_memory_updated",
            "unknown_new_runtime_noise",
        ):
            entry = WSHandler._runtime_event_to_progress_entry(
                {
                    "type": runtime_type,
                    "execution_mode": "task_mode",
                    "runtime_kind": "task_mode_agent_turn",
                },
            )
            self.assertIsNone(entry)

    def test_task_mode_legacy_projection_is_not_company_runtime(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "thinking_delta",
                "execution_mode": "task_mode",
                "work_item_projection_id": "task_mode_execution",
                "work_item_turn_type": "execute",
                "turn_id": "rt-1:1",
                "item_id": "rt-1:1:thinking",
                "seq": 1,
                "text": "我先检查。",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "thinking")
        self.assertEqual(entry["summary"], "我先检查。")
        self.assertEqual(entry["detail"], "我先检查。")
        self.assertEqual(entry["turn_id"], "rt-1:1")
        self.assertEqual(entry["item_id"], "rt-1:1:thinking")
        self.assertEqual(entry["seq"], 1)
        self.assertNotIn("work_item_projection_id", entry)
        self.assertNotIn("is_company_runtime", entry)

    def test_task_mode_tool_progress_uses_stable_tool_item_id(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "tool_completed",
                "execution_mode": "task_mode",
                "turn_id": "rt-1:2",
                "tool_call_id": "call-1",
                "tool_name": "web_search",
                "result_summary": "ok",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "tool_call")
        self.assertEqual(entry["item_id"], "rt-1:2:tool:call-1")
        self.assertEqual(entry["stream_id"], "rt-1:2:tool:call-1")
        self.assertEqual(entry["tool_call_id"], "call-1")

    def test_task_mode_permission_progress_uses_stable_permission_item_id(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "permission_resolved",
                "execution_mode": "task_mode",
                "turn_id": "rt-1:2",
                "tool_call_id": "call-1",
                "tool_name": "shell_exec",
                "resolution": "allow",
                "permission_group_key": "tool:shell_exec/python:domain:example.com",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "autonomy")
        self.assertEqual(entry["item_id"], "rt-1:2:permission:call-1")
        self.assertEqual(entry["permission_group_key"], "tool:shell_exec/python:domain:example.com")

    def test_task_mode_checkpoint_saved_maps_to_needs_input(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "checkpoint_saved",
                "execution_mode": "task_mode",
                "checkpoint_type": "task_user_input",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "needs_input")
        self.assertEqual(entry["summary"], "Needs input")
        self.assertEqual(entry["detail"], "task_user_input")

    def test_runtime_worker_notification_maps_error_to_work_item_failed_entry(self) -> None:
        entry = WSHandler._runtime_event_to_progress_entry(
            {
                "type": "worker_notification",
                "worker_type": "native_subagent",
                "notification_kind": "error",
                "summary": "The worker failed.",
            },
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["type"], "work_item_failed")
        self.assertEqual(entry["summary"], "native subagent: error")
        self.assertEqual(entry["detail"], "The worker failed.")


class WSHandlerRuntimeEventRoutingTests(unittest.IsolatedAsyncioTestCase):
    class _MemoryChatStore:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def insert_message(
            self,
            *,
            channel_id: str,
            sender: str,
            sender_name: str,
            content: str,
            metadata: dict | None = None,
            project_id: str = "default",
            **_kw,
        ) -> dict:
            message = {
                "channel_id": channel_id,
                "sender": sender,
                "sender_name": sender_name,
                "content": content,
                "metadata": metadata or {},
                "project_id": project_id,
            }
            self.messages.append(message)
            return message

        async def get_channel_messages(
            self,
            channel_id: str,
            *,
            limit: int = 100,
            project_id: str = "default",
        ) -> list[dict]:
            matched = [
                message
                for message in self.messages
                if message["channel_id"] == channel_id and message["project_id"] == project_id
            ]
            return matched[-limit:]

    def _make_handler_for_task(self, task: Task) -> WSHandler:
        engine = MagicMock()
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=task))
        engine.project_id = "test-project"
        engine.escalation = None
        return WSHandler(engine, MagicMock(), MagicMock(), MagicMock())

    async def test_external_team_telemetry_uses_separate_display_only_channel(self) -> None:
        engine = MagicMock()
        engine.project_id = "project-a"
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=None))
        event_adapter = MagicMock()
        handler = WSHandler(engine, MagicMock(), MagicMock(), event_adapter)
        handler.broadcast = AsyncMock()

        await handler.on_progress(
            "",
            external_team_event={
                "schema_version": 1,
                "event_id": "event-a",
                "provider": "jiuwenswarm",
                "project_id": "wrong-project",
                "task_id": "task-a",
                "external_invocation_id": "invocation-a",
                "sequence": 1,
                "occurred_at": "2026-09-03T10:00:00",
                "kind": "runtime_ready",
            },
        )

        handler.broadcast.assert_awaited_once()
        envelope = handler.broadcast.await_args.args[0]
        self.assertEqual(envelope["type"], "external_team_activity_event")
        self.assertEqual(envelope["payload"]["project_id"], "project-a")
        event_adapter.parse_progress.assert_not_called()

    async def test_forwarded_delegation_progress_uses_sanitized_summary(self) -> None:
        engine = MagicMock()
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=None))
        engine.project_id = "test-project"
        engine.escalation = None
        chat_store = self._MemoryChatStore()
        handler = WSHandler(engine, MagicMock(), chat_store, EventAdapter())
        handler.broadcast = AsyncMock()
        handler._active_runtime_children["child-task"] = "parent-task"

        await handler.on_progress(
            "[Delegating to codex] task=COO Intake | cmd=codex exec <prompt:16-chars>",
            task_id="child-task",
            agent_role_id="coo",
            agent_name="COO",
        )

        messages = await chat_store.get_channel_messages(
            "session:parent-task",
            limit=20,
            project_id="test-project",
        )
        forwarded = [message for message in messages if "Delegating to codex" in message["content"]]
        self.assertEqual(len(forwarded), 1)
        self.assertNotIn("FULL ROLE PROMPT", forwarded[0]["content"])
        self.assertEqual(forwarded[0]["metadata"]["detail_visibility"], "summary")
        self.assertEqual(forwarded[0]["metadata"]["forwarded_from"], "child-task")

    async def test_task_mode_runtime_visual_event_maps_to_origin_task(self) -> None:
        execution = Task(
            id="execution-task",
            project_id="test-project",
            session_id="session-1",
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "origin_task_id": "ui-task",
            },
        )
        handler = self._make_handler_for_task(execution)

        event = await handler._canonicalize_runtime_visual_event({
            "type": "assistant_delta",
            "data": {
                "task_id": "execution-task",
                "runtime_session_id": "rt-1",
                "iteration": 1,
                "text": "hello",
            },
        })

        data = event["data"]
        self.assertEqual(data["task_id"], "ui-task")
        self.assertEqual(data["runtime_task_id"], "execution-task")
        self.assertEqual(data["execution_turn_id"], "execution-task")
        self.assertEqual(data["turn_id"], "rt-1:1")

    async def test_assistant_delta_is_chunked_and_flushed_before_turn_completed(self) -> None:
        execution = Task(
            id="execution-task",
            project_id="test-project",
            session_id="session-1",
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "origin_task_id": "ui-task",
            },
        )
        handler = self._make_handler_for_task(execution)
        handler.broadcast = AsyncMock()

        await handler._broadcast_runtime_visual_event({
            "type": "assistant_delta",
            "data": {
                "task_id": "ui-task",
                "runtime_task_id": "execution-task",
                "execution_turn_id": "execution-task",
                "turn_id": "rt-1:1",
                "text": "hello",
            },
        })
        handler.broadcast.assert_not_awaited()

        await handler._broadcast_runtime_visual_event({
            "type": "turn_completed",
            "data": {
                "task_id": "ui-task",
                "runtime_task_id": "execution-task",
                "execution_turn_id": "execution-task",
                "turn_id": "rt-1:1",
            },
        })

        self.assertEqual(handler.broadcast.await_count, 2)
        delta_payload = handler.broadcast.await_args_list[0].args[0]["payload"]
        self.assertEqual(delta_payload["type"], "assistant_delta")
        self.assertEqual(delta_payload["data"]["text"], "hello")
        completed_payload = handler.broadcast.await_args_list[1].args[0]["payload"]
        self.assertEqual(completed_payload["type"], "turn_completed")

    async def test_task_mode_session_config_overrides_stale_agent_selection(self) -> None:
        task = Task(
            id="ui-task",
            project_id="test-project",
            session_id="session-1",
            metadata={
                "mode": "task",
                "exec_mode": "task",
                "execution_mode": "task_mode",
                "preferred_agent": "native",
                "selected_execution_agent": "native",
                "agent_selection": {
                    "selected": "native",
                    "selection_source": "forced_native",
                },
            },
        )
        engine = MagicMock()
        engine.store = SimpleNamespace(save_task=AsyncMock())
        engine.memory = None
        engine.project_id = "test-project"
        engine.escalation = None
        handler = WSHandler(engine, MagicMock(), MagicMock(), MagicMock())

        await handler._persist_session_config(
            task,
            exec_mode="task",
            company_profile="corporate",
            preferred_agent="codex",
        )

        self.assertEqual(task.metadata["preferred_agent"], "codex")
        self.assertEqual(task.metadata["selected_execution_agent"], "codex")
        self.assertEqual(task.metadata["preferred_external_agent"], "codex")
        self.assertFalse(task.metadata["force_native_execution"])
        self.assertEqual(task.metadata["agent_selection"]["selected"], "codex")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "session_config")
        self.assertEqual(handler._resolve_task_selected_execution_agent(task), "codex")

    async def test_task_mode_snapshot_prefers_explicit_config_over_stale_agent_selection(self) -> None:
        task = Task(
            id="ui-task",
            project_id="test-project",
            session_id="session-1",
            metadata={
                "mode": "task",
                "exec_mode": "task",
                "execution_mode": "task_mode",
                "preferred_agent": "codex",
                "selected_execution_agent": "codex",
                "agent_selection": {
                    "selected": "native",
                    "selection_source": "forced_native",
                },
            },
        )

        self.assertEqual(resolve_snapshot_selected_execution_agent(task), "codex")

    async def test_member_claimed_work_item_broadcasts_parent_work_item_progress(self) -> None:
        engine = MagicMock()
        engine.project_id = "test-project"
        engine.store = None
        engine.escalation = None

        handler = WSHandler(
            engine,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        broadcasts: list[dict] = []
        handler.broadcast = AsyncMock(side_effect=lambda msg: broadcasts.append(msg))
        handler._active_runtime_children["projection-task-1"] = "parent-task-1"

        await handler._handle_runtime_event_progress(
            {
                "type": "member_claimed_work_item",
                "task_id": "projection-task-1",
                "work_item_projection_id": "engineering_execution",
                "message_priority": "manager",
            }
        )

        work_item_payloads = [
            item["payload"]
            for item in broadcasts
            if item.get("type") == "work_item_progress"
        ]
        self.assertEqual(len(work_item_payloads), 1)
        self.assertEqual(work_item_payloads[0]["task_id"], "parent-task-1")
        obsolete_entry_key = "work_item_" + "task_id"
        self.assertNotIn(obsolete_entry_key, work_item_payloads[0])
        self.assertEqual(work_item_payloads[0]["entry"]["type"], "work_item_started")
        self.assertNotIn(obsolete_entry_key, work_item_payloads[0]["entry"])
        self.assertEqual(work_item_payloads[0]["entry"]["execution_turn_id"], "projection-task-1")
        self.assertEqual(work_item_payloads[0]["entry"]["work_item_projection_id"], "engineering_execution")
        self.assertEqual(work_item_payloads[0]["entry"]["work_item_projection_title"], "Engineering Execution")
        self.assertNotIn("projection_id", work_item_payloads[0]["entry"])
        self.assertNotIn("legacy_title", work_item_payloads[0]["entry"])

    async def test_member_claimed_work_item_enriches_work_item_role_name(self) -> None:
        engine = MagicMock()
        engine.project_id = "test-project"
        engine.store = None
        engine.escalation = None
        role = MagicMock()
        role.name = "CEO"
        engine.org_engine.get_agent.return_value = role

        handler = WSHandler(
            engine,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        broadcasts: list[dict] = []
        handler.broadcast = AsyncMock(side_effect=lambda msg: broadcasts.append(msg))
        handler._active_runtime_children["projection-task-1"] = "parent-task-1"

        await handler._handle_runtime_event_progress(
            {
                "type": "member_claimed_work_item",
                "task_id": "projection-task-1",
                "role_id": "ceo",
                "work_item_projection_id": "5ce42865-4a7d-4f27-8a3b-9c0a0f3a0c1a",
                "message_priority": "manager",
            }
        )

        work_item_payload = next(
            item["payload"]
            for item in broadcasts
            if item.get("type") == "work_item_progress"
        )
        entry = work_item_payload["entry"]
        self.assertEqual(entry["role_name"], "CEO")
        self.assertEqual(entry["work_item_projection_title"], "CEO")
        self.assertNotIn("legacy_title", entry)

    async def test_company_progress_callback_includes_role_identity(self) -> None:
        role = MagicMock()
        role.role_id = "ceo"
        role.name = "CEO"
        org_engine = MagicMock()
        org_engine.get_agent.return_value = role
        progress_callback = AsyncMock()
        executor = CompanyWorkItemExecutor(
            org_engine=org_engine,
            communication=None,
            approval_engine=None,
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            progress_callback=progress_callback,
        )
        task = Task(
            id="projection-task-1",
            title="CEO Intake",
            assigned_to="ceo",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "5ce42865-4a7d-4f27-8a3b-9c0a0f3a0c1a",
                "work_item_role_id": "ceo",
            },
        )
        executor._active_tasks = [task]

        await executor._emit_progress(
            "[Company:5ce42865-4a7d-4f27-8a3b-9c0a0f3a0c1a] starting CEO Intake",
            task_id=task.id,
        )

        _, kwargs = progress_callback.await_args
        self.assertEqual(kwargs["task_id"], task.id)
        self.assertEqual(kwargs["agent_role_id"], "ceo")
        self.assertEqual(kwargs["agent_name"], "CEO")

    async def test_worker_notification_broadcasts_dedicated_event_and_session_message(self) -> None:
        engine = MagicMock()
        engine.project_id = "test-project"
        engine.store = None
        engine.escalation = None
        chat_store = MagicMock()
        chat_store.create_session_channel = AsyncMock()
        chat_store.insert_message = AsyncMock(return_value={
            "message_id": "msg-1",
            "channel_id": "session:task-1",
            "sender": "system",
            "sender_name": "Native Subagent",
            "content": "ready",
            "created_at": 123.0,
            "reply_to_id": None,
            "mentions": [],
            "metadata": {},
        })
        event_adapter = MagicMock()
        event_adapter.translate.return_value = []

        handler = WSHandler(
            engine,
            MagicMock(),
            chat_store,
            event_adapter,
        )
        broadcasts: list[dict] = []
        handler.broadcast = AsyncMock(side_effect=lambda msg: broadcasts.append(msg))

        await handler.on_opc_event(MagicMock(
            event_type="runtime_event",
            payload={
                "type": "worker_notification",
                "task_id": "task-1",
                "worker_id": "na_123",
                "worker_type": "native_subagent",
                "notification_kind": "idle",
                "summary": "ready",
            },
        ))

        broadcast_types = [item.get("type") for item in broadcasts]
        self.assertIn("worker_notification", broadcast_types)
        self.assertIn("session_message", broadcast_types)
        self.assertIn("session_progress", broadcast_types)
        chat_store.create_session_channel.assert_awaited()
        chat_store.insert_message.assert_awaited()


if __name__ == "__main__":
    unittest.main()
