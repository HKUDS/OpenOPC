"""Engine-integration regression for durable owner interaction replies.

A ``task_user_input`` answer remains plain user input. A blocked ToolCall uses
its own typed ``tool_permission`` checkpoint and immutable ToolCall reference.
Both decisions enter through the same durable submit/claim boundary and wake a
live company dispatcher instead of re-entering ``_execute_company_mode``.

When no dispatcher is live the resume must fall through to the legacy
re-entry path unchanged.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from opc.core.config import AutonomyConfig
from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.work_item_links import set_linked_work_item_id


class _PreferencesStub:
    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs):
        _ = kwargs


class _StoreStub:
    async def record_approval(self, **kwargs):
        _ = kwargs


class _MemoryStub:
    def append_autonomy_event(self, event, project=False):
        _ = (event, project)


def _approval_engine() -> ApprovalEngine:
    return ApprovalEngine(
        llm=object(),
        store=_StoreStub(),
        preferences=_PreferencesStub(),
        memory=_MemoryStub(),
        config=AutonomyConfig(),
    )


class CheckpointAnswerLiveDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store
        self.engine.interaction_coordinator = InteractionCoordinator(
            store=self.store,
            project_id="p",
        )
        self.engine._initialized = True
        # Drive consumption explicitly after asserting the durable ACK.
        self.engine._schedule_interaction_consumption = lambda *_args: None
        self.engine.approval_engine = _approval_engine()
        self.executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        self.executor._live_run_dispatchers = {}
        self.executor._dispatcher_wake = asyncio.Event()
        self.engine.company_executor = self.executor
        self.engine._execute_company_mode = AsyncMock(return_value="re-entered")
        self.engine._execute_single_agent = AsyncMock(return_value="single-agent")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed(self) -> ExecutionCheckpoint:
        anchor = Task(
            id="root-task",
            title="company request",
            project_id="p",
            session_id="s",
            metadata={
                "mode": "company",
                "execution_mode": "company_mode",
            },
        )
        await self.store.save_task(anchor)
        task = Task(
            id="task-1",
            title="blocked worker",
            project_id="p",
            session_id="worker-s",
            parent_id=anchor.id,
            parent_session_id=anchor.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "delegation_run_id": "run-1",
                "execution_mode": "company_mode",
                "work_item_runtime": True,
            },
        )
        set_linked_work_item_id(task, "wi-1")
        await self.store.save_task(task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="ckpt-1",
            project_id="p",
            session_id="worker-s",
            checkpoint_type="task_user_input",
            task_id="task-1",
            status="pending",
            payload={
                "task_id": "task-1",
                "session_id": "s",
                "execution_mode": "company_mode",
                "task_ids": ["task-1"],
                "prompt": "Which deployment region should be used?",
                "pause_request": {
                    "requires_user_input": True,
                    "reason": "Need a deployment region.",
                    "questions": [
                        {
                            "id": "deployment_region",
                            "question": "Which deployment region?",
                        }
                    ],
                },
                "interaction": {
                    "kind": "task_user_input",
                    "domain_key": "task-user-input:ckpt-1",
                    "ownership": {
                        "waiting_task_id": "task-1",
                        "waiting_session_id": "worker-s",
                        "ui_anchor_task_id": "root-task",
                        "ui_anchor_session_id": "s",
                        "root_session_id": "s",
                        "company_runtime_session_id": "s",
                        "execution_parent_task_id": "root-task",
                    },
                },
            },
            created_at=datetime.now(),
        )
        persisted, _created = await self.store.publish_owner_interaction_checkpoint(
            checkpoint,
            interaction_key="task-user-input:ckpt-1",
            supersede_pending_scope=False,
        )
        return persisted

    async def test_task_input_reply_wakes_live_dispatcher_after_durable_ack(self) -> None:
        checkpoint = await self._seed()
        self.executor._live_run_dispatchers["run-1"] = 1

        receipt = await self.engine.submit_checkpoint_decision(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision={"text": "Use US East."},
            client_request_id="task-input-1",
            requester_task_id="root-task",
            requester_session_id="s",
        )
        self.assertTrue(receipt["accepted"], receipt)
        self.assertEqual(receipt["status"], "answered")
        await self.engine._consume_answered_interaction(
            checkpoint.checkpoint_id,
            checkpoint.checkpoint_type,
        )

        self.assertTrue(self.executor._dispatcher_wake.is_set())
        self.engine._execute_company_mode.assert_not_awaited()
        self.engine._execute_single_agent.assert_not_awaited()
        saved = await self.store.get_task("task-1")
        injected = str(saved.context_snapshot.get("user_supplied_input", ""))
        self.assertEqual(injected, "Use US East.")
        self.assertEqual(saved.status, TaskStatus.PENDING)
        persisted = await self.store.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="p",
            checkpoint_type="task_user_input",
        )
        self.assertEqual(persisted.status, "resolved")

    async def test_typed_tool_permission_wakes_live_exact_runtime(self) -> None:
        await self._seed()
        arguments = {"command": "pip install pandas"}
        runtime_session_id = "runtime-tool-1"
        fingerprint = self.engine._tool_call_fingerprint(
            tool_call_id="call-1",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id=runtime_session_id,
        )
        approval = {
            "action_kind": "tool",
            "action_name": "shell_exec",
            "project_id": "p",
            "session_scope_id": "s",
            "allowlist_enabled": True,
            "allowlist_patterns": ["*"],
            "candidates": ["pip install pandas"],
            "risk_level": "medium",
            "rationale": "Command is not in the low-risk allowlist.",
        }
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="tool-permission-1",
            project_id="p",
            session_id="worker-s",
            checkpoint_type="tool_permission",
            task_id="task-1",
            payload={
                "schema_version": 2,
                "interaction": {
                    "kind": "tool_permission",
                    "domain_key": "tool-permission:tool-permission-1",
                    "prompt": "Allow this exact shell command?",
                    "options": [
                        {"id": "approve_session", "label": "Approve session"},
                        {"id": "deny", "label": "Deny"},
                    ],
                    "ownership": {
                        "waiting_task_id": "task-1",
                        "waiting_session_id": "worker-s",
                        "ui_anchor_task_id": "root-task",
                        "ui_anchor_session_id": "s",
                        "root_session_id": "s",
                        "company_runtime_session_id": "s",
                        "execution_parent_task_id": "root-task",
                        "tool_runtime_session_id": runtime_session_id,
                    },
                },
                "tool_call": {
                    "id": "call-1",
                    "name": "shell_exec",
                    "arguments": arguments,
                    "runtime_session_id": runtime_session_id,
                    "fingerprint": fingerprint,
                },
                "approval": approval,
            },
        )
        checkpoint, _created = await self.store.publish_owner_interaction_checkpoint(
            checkpoint,
            interaction_key="tool-permission:tool-permission-1",
            supersede_pending_scope=False,
        )
        await self.store.save_runtime_tool_call(
            runtime_session_id=runtime_session_id,
            task_id="task-1",
            session_id="worker-s",
            message_id="assistant-1",
            tool_call_id="call-1",
            tool_name="shell_exec",
            arguments=arguments,
        )
        self.executor._live_run_dispatchers["run-1"] = 1

        receipt = await self.engine.submit_checkpoint_decision(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision={"option_id": "approve_session"},
            client_request_id="tool-permission-reply-1",
            requester_task_id="root-task",
            requester_session_id="s",
        )
        self.assertTrue(receipt["accepted"], receipt)
        await self.engine._consume_answered_interaction(
            checkpoint.checkpoint_id,
            checkpoint.checkpoint_type,
        )

        self.assertTrue(self.executor._dispatcher_wake.is_set())
        self.engine._execute_company_mode.assert_not_awaited()
        self.engine._execute_single_agent.assert_not_awaited()
        saved = await self.store.get_task("task-1")
        permit = saved.context_snapshot["runtime_resume"]["approved_tool_calls"][
            fingerprint
        ]
        self.assertEqual(permit["id"], "call-1")
        self.assertEqual(permit["function"], "shell_exec")
        self.assertEqual(permit["arguments"], arguments)
        self.assertEqual(permit["runtime_session_id"], runtime_session_id)
        self.assertNotIn("user_supplied_input", saved.context_snapshot)

        # The live runtime owns exact ToolCall completion. Settle it here to
        # stop the claim heartbeat just as persisted tool-result handling does.
        consuming = await self.store.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="p",
            checkpoint_type="tool_permission",
        )
        self.assertEqual(consuming.status, "consuming")
        claim = consuming.payload["interaction"]["claim"]
        finished = await self.engine.interaction_coordinator.finish(
            InteractionDecisionLease(
                checkpoint=consuming,
                decision={"option_id": "approve_session"},
                consumer_id=claim["consumer_id"],
                claim_id=claim["claim_id"],
            )
        )
        self.assertTrue(finished.applied)

    async def test_no_live_dispatcher_falls_through_to_reentry_path(self) -> None:
        checkpoint = await self._seed()

        reply = await self.engine._resume_task_checkpoint(checkpoint, "please continue")

        self.assertFalse(self.executor._dispatcher_wake.is_set())
        self.assertEqual(reply, "single-agent")
        self.engine._execute_single_agent.assert_awaited()
        saved = await self.store.get_task("task-1")
        self.assertEqual(
            saved.context_snapshot.get("user_supplied_input"),
            "please continue",
        )


if __name__ == "__main__":
    unittest.main()
