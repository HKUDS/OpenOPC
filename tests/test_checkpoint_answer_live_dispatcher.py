"""Engine-integration regression for OBS-4 + OBS-7.

A ``task_user_input`` answer for a run whose dispatcher is live must:
  1. apply an explicit approval decision through the approval engine
     (OBS-7 — the chat route was input-only, so the blocked tool
     re-escalated until the attempt ledger killed the card), and
  2. deliver the input in place and wake the live dispatcher instead of
     re-entering ``_execute_company_mode`` (OBS-4 — re-entry reset live
     claim registries and the ledger stamped in-flight cards interrupted).

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
        escalation=None,
        config=AutonomyConfig(),
    )


class CheckpointAnswerLiveDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store
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
        task = Task(
            id="task-1",
            title="blocked worker",
            project_id="p",
            session_id="s",
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
            session_id="s",
            checkpoint_type="task_user_input",
            task_id="task-1",
            status="pending",
            payload={
                "task_id": "task-1",
                "session_id": "s",
                "execution_mode": "company_mode",
                "task_ids": ["task-1"],
                "prompt": "Tool execution blocked by autonomy policy",
                "pause_request": {
                    "requires_user_input": True,
                    "permission_context": {
                        "tool_name": "shell_exec",
                        "candidate": "pip install pandas",
                        "resolution": "ask",
                    },
                },
            },
            created_at=datetime.now(),
        )
        await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    async def test_live_dispatcher_gets_wake_and_approval_applies(self) -> None:
        checkpoint = await self._seed()
        self.executor._live_run_dispatchers["run-1"] = 1

        reply = await self.engine._resume_task_checkpoint(checkpoint, "approve_session")

        self.assertIn("live", reply)
        self.assertTrue(self.executor._dispatcher_wake.is_set())
        self.engine._execute_company_mode.assert_not_awaited()
        self.engine._execute_single_agent.assert_not_awaited()
        saved = await self.store.get_task("task-1")
        injected = str(saved.context_snapshot.get("user_supplied_input", ""))
        self.assertIn("Approval decision applied", injected)
        self.assertIn("shell_exec", injected)
        self.assertEqual(saved.status, TaskStatus.PENDING)

    async def test_runtime_v2_park_shape_applies_approval_via_permission_requests(self) -> None:
        """Company runtime parks carry the blocked call in permission_requests
        (empty pause_request). The decision bridge must fall back to that shape
        or a late approval resumes without recording any allowlist grant and
        the identical command re-parks forever (project-0012 treadmill)."""
        checkpoint = await self._seed()
        payload = dict(checkpoint.payload)
        payload["pause_request"] = {}
        payload["permission_requests"] = [
            {
                "tool_name": "shell_exec",
                "tool_args": {"command": "pip install pandas"},
                "resolution": "ask",
                "scope": "once",
                "risk_level": "medium",
                "rationale": "Command is not in the low-risk allowlist.",
                "source": "approval_engine",
            }
        ]
        checkpoint.payload = payload
        await self.store.save_execution_checkpoint(checkpoint)
        self.executor._live_run_dispatchers["run-1"] = 1

        reply = await self.engine._resume_task_checkpoint(checkpoint, "approve_session")

        self.assertIn("live", reply)
        saved = await self.store.get_task("task-1")
        injected = str(saved.context_snapshot.get("user_supplied_input", ""))
        self.assertIn("Approval decision applied", injected)
        self.assertIn("shell_exec", injected)

    async def test_permission_requests_artifact_preserves_tool_args(self) -> None:
        """The runtime park artifact must persist the blocked call's arguments;
        they are the only source of the command text for late allowlist grants."""
        from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2

        class _Decision:
            resolution = type("R", (), {"value": "ask"})()
            scope = type("S", (), {"value": "once"})()
            risk_level = type("L", (), {"value": "medium"})()
            rationale = "blocked"
            source = "approval_engine"

        runtime = object.__new__(NativeRuntimeV2)
        requests = NativeRuntimeV2._permission_requests_from_results(
            runtime,
            [
                {
                    "permission_decision": _Decision(),
                    "tool_call": {
                        "function": "shell_exec",
                        "arguments": {"command": "curl -sI https://example.com"},
                    },
                }
            ],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["tool_name"], "shell_exec")
        self.assertEqual(
            requests[0]["tool_args"], {"command": "curl -sI https://example.com"}
        )

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
