"""Regression: failure-path run closure and post-failure input routing (OBS-5).

A run whose intake/delivery failed used to stay running/active forever: no
closure signal, no card, and a new message was routed into re-executing the
dead run (dropping the user's content). The fix closes the run at dispatcher
convergence, emits a ``company_run_failure_review`` card, and the card's
resume handler never swallows ordinary messages — content-bearing replies
fall through so normal routing starts a fresh run.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.phase import Phase


class RunFailureSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        self.executor.store = self.store
        self.captured_checkpoints: list[dict] = []

        async def _capture(payload: dict) -> None:
            self.captured_checkpoints.append(payload)

        self.executor.checkpoint_callback = _capture
        self.executor._emit_progress = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_run(self, *, fail_intake: bool) -> list[Task]:
        run = DelegationRun(run_id="run-1", project_id="p", session_id="s")
        run.status = "running"
        run.lifecycle_status = "active"
        await self.store.save_delegation_run(run)
        intake = DelegationWorkItem(
            work_item_id="wi-intake",
            run_id="run-1",
            role_id="ceo",
            kind="intake",
            title="CEO Intake",
            phase=Phase.READY,
        )
        execute = DelegationWorkItem(
            work_item_id="wi-exec",
            run_id="run-1",
            role_id="cto",
            kind="execute",
            title="survey",
            phase=Phase.READY,
        )
        await self.store.save_delegation_work_item(intake)
        await self.store.save_delegation_work_item(execute)
        if fail_intake:
            await self.store.update_delegation_work_item("wi-intake", phase=Phase.FAILED)
            await self.store.update_delegation_work_item("wi-exec", phase=Phase.FAILED)
        else:
            await self.store.update_delegation_work_item("wi-intake", phase=Phase.RUNNING)
            await self.store.update_delegation_work_item("wi-intake", phase=Phase.APPROVED)
            await self.store.update_delegation_work_item("wi-exec", phase=Phase.RUNNING)
            await self.store.update_delegation_work_item("wi-exec", phase=Phase.APPROVED)
        task = Task(
            id="task-1",
            title="CEO Intake",
            project_id="p",
            session_id="s",
            metadata={
                "delegation_run_id": "run-1",
                "original_request": "Research multi-agent architectures",
            },
        )
        await self.store.save_task(task)
        return [task]

    async def test_terminal_failure_closes_run_and_emits_card(self) -> None:
        tasks = await self._seed_run(fail_intake=True)

        await self.executor._settle_run_lifecycle_on_convergence(tasks)

        run = await self.store.get_delegation_run("run-1")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertTrue(run.metadata.get("run_failure", {}).get("failed_items"))
        self.assertEqual(len(self.captured_checkpoints), 1)
        card = self.captured_checkpoints[0]
        self.assertEqual(card["checkpoint_type"], "company_run_failure_review")
        self.assertEqual(card["payload"]["run_id"], "run-1")
        self.assertEqual(card["payload"]["original_request"], "Research multi-agent architectures")

    async def test_successful_convergence_leaves_run_untouched(self) -> None:
        tasks = await self._seed_run(fail_intake=False)

        await self.executor._settle_run_lifecycle_on_convergence(tasks)

        run = await self.store.get_delegation_run("run-1")
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(self.captured_checkpoints, [])

    async def test_settlement_is_idempotent(self) -> None:
        tasks = await self._seed_run(fail_intake=True)
        await self.executor._settle_run_lifecycle_on_convergence(tasks)
        await self.executor._settle_run_lifecycle_on_convergence(tasks)
        self.assertEqual(len(self.captured_checkpoints), 1)


class FailureReviewCheckpointReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="ckpt-fail-1",
            project_id="p",
            session_id="s",
            checkpoint_type="company_run_failure_review",
            task_id="task-1",
            status="pending",
            payload={
                "run_id": "run-1",
                "session_id": "s",
                "prompt": "run closed",
            },
            created_at=datetime.now(),
        )
        await self.store.save_execution_checkpoint(checkpoint)
        self.checkpoint = checkpoint

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _checkpoint_status(self) -> str:
        loaded = await self.engine._load_execution_checkpoint_by_id("ckpt-fail-1")
        return str(getattr(loaded, "status", "") or "")

    async def test_untargeted_message_is_never_swallowed(self) -> None:
        reply = await self.engine._maybe_resume_checkpoint(
            "Please run a fresh research task for me",
            session_id="s",
        )
        self.assertIsNone(reply)
        self.assertEqual(await self._checkpoint_status(), "pending")

    async def test_dismiss_resolves_with_acknowledgement(self) -> None:
        reply = await self.engine._maybe_resume_checkpoint(
            "dismiss",
            session_id="s",
            reply_metadata={"response_to_checkpoint_id": "ckpt-fail-1"},
        )
        self.assertEqual(reply, "Company run closure acknowledged.")
        self.assertEqual(await self._checkpoint_status(), "resolved")

    async def test_content_reply_resolves_and_falls_through(self) -> None:
        reply = await self.engine._maybe_resume_checkpoint(
            "Redo the research, focused on open-source frameworks this time",
            session_id="s",
            reply_metadata={"response_to_checkpoint_id": "ckpt-fail-1"},
        )
        self.assertIsNone(reply)
        self.assertEqual(await self._checkpoint_status(), "resolved")


if __name__ == "__main__":
    unittest.main()
