"""Regression tests: orphaned task-wait checkpoints must never swallow user messages.

Reproduces the production failure where a company-mode review left a pending
``task_user_input`` checkpoint behind (the runtime carried the flow forward via
approval-card grants and a fresh review attempt, never replying through the
checkpoint). The user's next chat message in the primary session was captured
by that orphan row, routed into the deprecated multi-agent resume path with an
empty task list, and answered with an empty string.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from opc.core.models import (
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExecutionMode,
    Phase,
    Task,
    TaskStatus,
)
from opc.engine import OPCEngine


class StaleCheckpointSelfHealTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = OPCEngine(opc_home=Path(self._tmp.name), project_id="default")
        await self.engine.initialize()
        self.store = self.engine.store

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _save_task(self, status: TaskStatus, session_id: str) -> Task:
        task = Task(
            title="Review #1: competitive analysis",
            session_id=session_id,
            project_id="default",
            status=status,
        )
        await self.store.save_task(task)
        return task

    async def _save_wait_checkpoint(self, task: Task, session_id: str, **payload_extra) -> ExecutionCheckpoint:
        checkpoint = ExecutionCheckpoint(
            project_id="default",
            session_id=session_id,
            checkpoint_type="task_user_input",
            task_id=task.id,
            payload={
                "task_id": task.id,
                "session_id": session_id,
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "task_ids": [task.id],
                "prompt": "Tool execution blocked by autonomy policy",
                "review_level": "human",
                **payload_extra,
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    async def _checkpoint_status(self, checkpoint_id: str) -> str:
        rows = await self.store.get_execution_checkpoints(project_id="default")
        for row in rows:
            if row.checkpoint_id == checkpoint_id:
                return str(row.status or "")
        return "<not found>"

    async def test_orphan_checkpoint_is_lazily_resolved_and_not_returned(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.DONE, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        found = await self.engine.get_latest_pending_checkpoint_for_session(session_id)

        self.assertIsNone(found)
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "stale")

    async def test_waiting_checkpoint_is_still_returned(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        found = await self.engine.get_latest_pending_checkpoint_for_session(session_id)

        self.assertIsNotNone(found)
        self.assertEqual(found.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "pending")

    async def test_user_message_falls_through_to_normal_processing(self) -> None:
        """A plain follow-up question must not be consumed by an orphan checkpoint."""
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.DONE, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        result = await self.engine._maybe_resume_checkpoint(
            "你的交付文件在哪里？", session_id=session_id
        )

        self.assertIsNone(result)  # None → caller processes it as a fresh message
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "stale")

    async def test_explicit_reply_to_dead_checkpoint_reports_inactive(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.DONE, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        result = await self.engine._maybe_resume_checkpoint(
            "approve",
            session_id=session_id,
            reply_metadata={
                "response_to_checkpoint_id": checkpoint.checkpoint_id,
                "response_to_checkpoint_type": "task_user_input",
            },
        )

        self.assertEqual(result, "This request is no longer active.")
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "stale")

    async def _link_work_item(self, task: Task, phase: Phase) -> None:
        item = DelegationWorkItem(
            work_item_id=f"review::{uuid.uuid4()}::v1",
            title="Review #1",
            phase=phase,
        )
        await self.store.save_delegation_work_item(item)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

    async def test_checkpoint_with_closed_work_item_is_stale_even_if_task_not_terminal(self) -> None:
        """Production case: the swallowed resume left the task at PENDING, but the
        review attempt's work item had long been approved — the checkpoint is dead."""
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.PENDING, session_id)
        await self._link_work_item(task, Phase.APPROVED)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        found = await self.engine.get_latest_pending_checkpoint_for_session(session_id)

        self.assertIsNone(found)
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "stale")

    async def test_checkpoint_with_open_work_item_is_kept(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.RUNNING, session_id)
        await self._link_work_item(task, Phase.RUNNING)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        found = await self.engine.get_latest_pending_checkpoint_for_session(session_id)

        self.assertIsNotNone(found)
        self.assertEqual(found.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "pending")

    async def test_task_settling_supersedes_pending_wait_checkpoints(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        await self.engine._supersede_stale_task_wait_checkpoints(task.id, reason="test settle")

        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "superseded")

    async def test_resume_with_unresolvable_siblings_never_returns_empty(self) -> None:
        """Sibling ids that are work-item ids (not task UUIDs) must not empty the task list."""
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(
            task,
            session_id,
            task_ids=["review::467f36ff::v1"],  # work-item id, unresolvable as a task
            company_work_item_plan=None,
        )

        self.engine._execute_single_agent = AsyncMock(return_value="resumed reply")
        self.engine._execute_multi_agent = AsyncMock(return_value="")
        self.engine._execute_company_mode = AsyncMock(return_value="")

        result = await self.engine._resume_task_checkpoint(checkpoint, "继续")

        self.assertEqual(result, "resumed reply")
        self.engine._execute_multi_agent.assert_not_awaited()
        self.engine._execute_company_mode.assert_not_awaited()
        (called_tasks, _agent), _ = self.engine._execute_single_agent.await_args
        self.assertEqual([t.id for t in called_tasks], [task.id])
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "resolved")

    async def test_resume_with_plan_routes_to_company_mode(self) -> None:
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(
            task,
            session_id,
            company_work_item_plan={"profile": "corporate"},
        )

        self.engine._execute_company_mode = AsyncMock(return_value="company resumed")
        self.engine._execute_multi_agent = AsyncMock(return_value="")

        result = await self.engine._resume_task_checkpoint(checkpoint, "继续")

        self.assertEqual(result, "company resumed")
        self.engine._execute_multi_agent.assert_not_awaited()

    def _approval_runtime_payload(self) -> dict:
        return {
            "runtime_session_id": "rt-1",
            "permission_requests": [
                {
                    "tool_name": "shell_exec",
                    "resolution": "ask",
                    "scope": "once",
                    "risk_level": "medium",
                    "source": "approval_engine",
                }
            ],
        }

    async def test_plain_chat_is_not_consumed_by_parked_approval_checkpoint(self) -> None:
        """A live permission prompt is decided via its approval card, never by free chat.

        Deferred approval cards stay pending indefinitely, so an implicit capture
        here would swallow every later conversation message into the approval reply.
        """
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(
            task,
            session_id,
            runtime_v2=self._approval_runtime_payload(),
        )

        result = await self.engine._maybe_resume_checkpoint(
            "顺便问一下，进度怎么样了？", session_id=session_id
        )

        self.assertIsNone(result)  # message continues as a normal turn
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "pending")

    async def test_unambiguous_decision_word_resumes_parked_approval_without_a_card(self) -> None:
        """An unambiguous decision word answers a parked approval even with no card click.

        The approval card is the normal way to answer, but if it failed to
        render (event-bus/mirroring hiccup) the task must not stay stuck
        forever: a reply that unambiguously normalizes to a decision (e.g.
        "proceed") is safe to apply the same way an explicit card click
        would be, because it can never be mistaken for ordinary chat.
        """
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(
            task,
            session_id,
            runtime_v2=self._approval_runtime_payload(),
        )

        self.engine._execute_single_agent = AsyncMock(return_value="approved and resumed")
        self.engine._execute_multi_agent = AsyncMock(return_value="")
        self.engine._execute_company_mode = AsyncMock(return_value="")

        result = await self.engine._maybe_resume_checkpoint("proceed", session_id=session_id)

        self.assertEqual(result, "approved and resumed")
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "resolved")

    async def test_plain_chat_still_answers_agent_question_checkpoint(self) -> None:
        """Waits without a permission request (agent asked the user a question)
        keep accepting typed answers."""
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(task, session_id)

        self.engine._execute_single_agent = AsyncMock(return_value="answered")
        self.engine._execute_multi_agent = AsyncMock(return_value="")
        self.engine._execute_company_mode = AsyncMock(return_value="")

        result = await self.engine._maybe_resume_checkpoint("用蓝色的方案", session_id=session_id)

        self.assertEqual(result, "answered")
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "resolved")

    async def test_explicit_reply_still_resumes_parked_approval_checkpoint(self) -> None:
        """The approval-card click path targets the checkpoint explicitly and must keep working."""
        session_id = str(uuid.uuid4())
        task = await self._save_task(TaskStatus.AWAITING_HUMAN, session_id)
        checkpoint = await self._save_wait_checkpoint(
            task,
            session_id,
            runtime_v2=self._approval_runtime_payload(),
        )

        self.engine._execute_single_agent = AsyncMock(return_value="approved and resumed")
        self.engine._execute_multi_agent = AsyncMock(return_value="")
        self.engine._execute_company_mode = AsyncMock(return_value="")

        result = await self.engine._maybe_resume_checkpoint(
            "approve",
            session_id=session_id,
            reply_metadata={
                "response_to_checkpoint_id": checkpoint.checkpoint_id,
                "response_to_checkpoint_type": "task_user_input",
            },
        )

        self.assertEqual(result, "approved and resumed")
        self.assertEqual(await self._checkpoint_status(checkpoint.checkpoint_id), "resolved")


if __name__ == "__main__":
    unittest.main()
