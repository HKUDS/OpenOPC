"""Regression: delivery-review self-evolution pipeline (OBS-10).

Approving (or sending feedback on) the delivery review card runs employee
self-evolution as company work items. Three defects made that pipeline fail
in production while plain ``ignore`` worked:

1. Output-channel mismatch — the finalizer required the turn's FINAL chat
   text to be bare JSON, but the native runtime appends a verification
   status line, the manager dispatch guard displaces the final message with
   a justification, and models narrate around the JSON. Fix: a dedicated
   ``submit_self_evolution_patches`` tool is the authoritative channel and
   the text parser scans fenced blocks / balanced objects as fallback.
2. Failure pollution — an abandoned reflection settled ``FAILED`` inside
   the delivered run and dirtied its terminal verdict. Fix: settle
   ``CANCELLED`` and exclude ``kind=self_evolution`` from run settlement.
3. No idempotent consumption — the card stayed ``pending`` for the whole
   (potentially long) reflection run, so a duplicate approve re-entered and
   reset the live work item. Fix: CAS the card to ``consuming`` before
   spawning, plus a wall-clock deadline that cancels a stuck reflection.
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.phase import Phase
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer4_tools.collaboration import create_collaboration_tools


class PatchJsonParserTests(unittest.TestCase):
    def parse(self, text: str):
        return CompanyWorkItemExecutor._parse_self_evolution_patch_json(text)

    def test_bare_json_still_parses(self) -> None:
        parsed = self.parse('{"patches":[{"employee_id":"e1"}]}')
        self.assertEqual(parsed["patches"][0]["employee_id"], "e1")

    def test_verification_status_line_suffix(self) -> None:
        # runtime_v2 appends this line to every final text; it must not
        # break extraction of a perfectly valid JSON payload before it.
        parsed = self.parse(
            '{"patches":[]}\n\nVerification: not required because no code '
            "edits or risky runtime actions were detected."
        )
        self.assertEqual(parsed["patches"], [])

    def test_prose_wrapped_fenced_json(self) -> None:
        text = (
            "Both children failed the strict format. I will synthesize.\n\n"
            '```json\n{"patches": [{"employee_id": "ceo-1", "summary": "s"}]}\n```\n'
            "Done."
        )
        parsed = self.parse(text)
        self.assertEqual(parsed["patches"][0]["employee_id"], "ceo-1")

    def test_prose_embedded_unfenced_json(self) -> None:
        parsed = self.parse('Here is my patch: {"patches": [{"employee_id": "x"}]} recorded.')
        self.assertEqual(parsed["patches"][0]["employee_id"], "x")

    def test_justification_only_text_returns_none(self) -> None:
        parsed = self.parse(
            "The JSON patch was delivered in the previous response.\n"
            "NO_DELEGATION_JUSTIFICATION: purely local reflection."
        )
        self.assertIsNone(parsed)

    def test_prefers_object_with_patches_key(self) -> None:
        parsed = self.parse('{"a": 1} and later {"patches": []}')
        self.assertEqual(parsed["patches"], [])


class SubmitPatchesToolTests(unittest.IsolatedAsyncioTestCase):
    def _tool(self, store=None):
        tools = create_collaboration_tools(SimpleNamespace(store=store))
        return next(t for t in tools if t.name == "submit_self_evolution_patches")

    def _task(self, *, self_evo: bool = True) -> Task:
        metadata = {
            "execution_mode": "company_mode",
            "work_item_role_id": "cto",
            "employee_assignment": {"employee_id": "emp-1", "role_id": "cto"},
        }
        if self_evo:
            metadata["work_item_turn_type"] = "self_evolution"
            metadata["self_evolution_work_item"] = True
        return Task(id="t1", title="t", project_id="p", session_id="s", metadata=metadata)

    async def test_records_patches_and_autofills_employee(self) -> None:
        task = self._task()
        result = await self._tool().func(
            patches=[{"summary": "lesson"}],
            task=task,
        )
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["patch_count"], 1)
        recorded = task.metadata["self_evolution_submitted_patch"]
        self.assertEqual(recorded["patches"][0]["employee_id"], "emp-1")
        self.assertEqual(recorded["patches"][0]["summary"], "lesson")

    async def test_empty_patch_list_is_valid(self) -> None:
        task = self._task()
        result = await self._tool().func(patches=[], task=task)
        self.assertEqual(result["patch_count"], 0)
        self.assertEqual(task.metadata["self_evolution_submitted_patch"]["patches"], [])

    async def test_rejected_outside_self_evolution_turns(self) -> None:
        task = self._task(self_evo=False)
        with self.assertRaises(ValueError):
            await self._tool().func(patches=[], task=task)

    async def test_non_object_patch_rejected(self) -> None:
        task = self._task()
        with self.assertRaises(ValueError):
            await self._tool().func(patches=["not-a-dict"], task=task)


class _EvolutionSink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def apply_employee_evolution_patch(self, **kwargs):
        self.calls.append(kwargs)
        return [
            {"employee_id": patch.get("employee_id", "")}
            for patch in kwargs["patch"]["patches"]
        ]


class FinalizeSelfEvolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        self.executor.store = self.store
        self.executor.save_task = self.store.save_task
        self.executor._emit_progress = AsyncMock()
        self.executor._projection_id_for_task = lambda task: "cto::self_evolution::x"
        self.sink = _EvolutionSink()
        self.executor.memory = SimpleNamespace(employee_evolution=self.sink)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed(self, retry_count: int = 0) -> Task:
        item = DelegationWorkItem(
            work_item_id="wi-se",
            run_id="run-1",
            role_id="cto",
            kind="self_evolution",
            title="Self-Evolution Review",
            phase=Phase.RUNNING,
        )
        await self.store.save_delegation_work_item(item)
        task = Task(
            id="task-se",
            title="Self-Evolution Review",
            project_id="p",
            session_id="s",
            assigned_to="cto",
            metadata={
                "work_item_turn_type": "self_evolution",
                "self_evolution_work_item": True,
                "self_evolution_patch_retry_count": retry_count,
                "self_evolution_patch_max_retries": 3,
                "employee_assignment": {"employee_id": "emp-1", "role_id": "cto"},
            },
        )
        set_linked_work_item_id(task, "wi-se")
        await self.store.save_task(task)
        return task

    async def test_tool_submission_wins_over_prose_final_text(self) -> None:
        task = await self._seed()
        task.metadata["self_evolution_submitted_patch"] = {
            "patches": [{"employee_id": "emp-1", "summary": "tool lesson"}],
        }
        result = TaskResult(
            status=TaskStatus.DONE,
            content="Reflection complete.\n\nVerification: not required.",
        )
        outcome = await self.executor._finalize_self_evolution_work_item(task, result)
        self.assertIsNone(outcome)
        self.assertEqual(len(self.sink.calls), 1)
        self.assertEqual(
            task.metadata["self_evolution_patch"]["patches"][0]["summary"], "tool lesson"
        )
        self.assertNotIn("self_evolution_submitted_patch", task.metadata)

    async def test_text_fallback_parses_fenced_json(self) -> None:
        task = await self._seed()
        result = TaskResult(
            status=TaskStatus.DONE,
            content='Summary.\n```json\n{"patches": [{"employee_id": "emp-1"}]}\n```',
        )
        outcome = await self.executor._finalize_self_evolution_work_item(task, result)
        self.assertIsNone(outcome)
        self.assertEqual(len(self.sink.calls), 1)

    async def test_unreadable_output_retries_with_tool_instruction(self) -> None:
        task = await self._seed()
        result = TaskResult(status=TaskStatus.DONE, content="I finished reflecting, all good.")
        outcome = await self.executor._finalize_self_evolution_work_item(task, result)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, TaskStatus.PENDING)
        feedback = task.metadata["self_evolution_patch_retry_feedback"]
        self.assertIn("submit_self_evolution_patches", feedback)
        self.assertIn("I finished reflecting", feedback)

    async def test_exhausted_retries_settle_cancelled_not_failed(self) -> None:
        task = await self._seed(retry_count=2)
        result = TaskResult(status=TaskStatus.DONE, content="still prose")
        outcome = await self.executor._finalize_self_evolution_work_item(task, result)
        self.assertEqual(outcome.status, TaskStatus.CANCELLED)
        item = await self.store.get_delegation_work_item("wi-se")
        self.assertEqual(item.phase, Phase.CANCELLED)
        self.assertEqual(
            dict(item.metadata or {}).get("last_transition_reason"),
            "self_evolution_abandoned",
        )
        self.assertEqual(
            task.metadata["self_evolution_error"]["error"], "invalid_self_evolution_json"
        )


class RunSettlementIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        self.executor.store = self.store
        self.executor.checkpoint_callback = AsyncMock()
        self.executor._emit_progress = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_run(self, *, intake_phase: Phase, selfevo_phase: Phase | None) -> list[Task]:
        run = DelegationRun(run_id="run-1", project_id="p", session_id="s")
        run.status = "running"
        run.lifecycle_status = "active"
        await self.store.save_delegation_run(run)
        intake = DelegationWorkItem(
            work_item_id="wi-intake", run_id="run-1", role_id="ceo",
            kind="intake", title="Intake", phase=Phase.READY,
        )
        await self.store.save_delegation_work_item(intake)
        if intake_phase is not Phase.READY:
            await self.store.update_delegation_work_item("wi-intake", phase=intake_phase)
        if selfevo_phase is not None:
            se = DelegationWorkItem(
                work_item_id="wi-se", run_id="run-1", role_id="ceo",
                kind="self_evolution", title="Self-Evolution", phase=Phase.READY,
            )
            await self.store.save_delegation_work_item(se)
            if selfevo_phase is not Phase.READY:
                await self.store.update_delegation_work_item("wi-se", phase=selfevo_phase)
        task = Task(
            id="task-1", title="Intake", project_id="p", session_id="s",
            metadata={"delegation_run_id": "run-1", "original_request": "goal"},
        )
        await self.store.save_task(task)
        return [task]

    async def test_running_selfevo_item_does_not_block_failure_settlement(self) -> None:
        tasks = await self._seed_run(intake_phase=Phase.FAILED, selfevo_phase=Phase.RUNNING)
        await self.executor._settle_run_lifecycle_on_convergence(tasks)
        run = await self.store.get_delegation_run("run-1")
        self.assertEqual(run.lifecycle_status, "closed_failed")

    async def test_cancelled_selfevo_item_never_fails_a_delivered_run(self) -> None:
        tasks = await self._seed_run(intake_phase=Phase.RUNNING, selfevo_phase=Phase.CANCELLED)
        await self.store.update_delegation_work_item("wi-intake", phase=Phase.APPROVED)
        await self.executor._settle_run_lifecycle_on_convergence(tasks)
        run = await self.store.get_delegation_run("run-1")
        self.assertEqual(run.lifecycle_status, "active")
        self.executor.checkpoint_callback.assert_not_called()


class DeliveryFeedbackConsumingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_checkpoint(self, *, status: str = "pending") -> ExecutionCheckpoint:
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="ckpt-fb",
            project_id="p",
            session_id="s",
            checkpoint_type="company_delivery_feedback",
            task_id="task-1",
            status=status,
            payload={"session_id": "s"},
        )
        await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    async def test_concurrent_approve_claims_exactly_once(self) -> None:
        await self._seed_checkpoint()
        # Both controllers hold a pending copy of the card before either
        # acts — the DB-level CAS must let exactly one proceed. The payload
        # has no waiting_task_id, so the winner exits right after claiming,
        # which keeps the race observable without a full runtime.
        first = await self.engine._load_execution_checkpoint_by_id("ckpt-fb")
        second = await self.engine._load_execution_checkpoint_by_id("ckpt-fb")
        replies = await asyncio.gather(
            self.engine.run_company_delivery_self_evolution_checkpoint(first, action="approve"),
            self.engine.run_company_delivery_self_evolution_checkpoint(second, action="approve"),
        )
        already = [r for r in replies if r == "Self-evolution for this delivery is already running."]
        proceeded = [r for r in replies if "delivery task reference is missing" in r]
        self.assertEqual(len(already), 1, replies)
        self.assertEqual(len(proceeded), 1, replies)

    async def test_duplicate_reply_while_consuming_is_idempotent(self) -> None:
        await self._seed_checkpoint(status="consuming")
        reply = await self.engine._maybe_resume_checkpoint(
            "approve",
            session_id="s",
            reply_metadata={"response_to_checkpoint_id": "ckpt-fb"},
        )
        self.assertEqual(reply, "Self-evolution for this delivery is already running.")

    async def test_crash_hands_the_claim_back_for_retry(self) -> None:
        await self._seed_checkpoint()

        async def _boom(checkpoint, *, action, feedback=""):
            raise RuntimeError("mid-flight crash")

        self.engine._run_company_delivery_self_evolution_consumed = _boom
        loaded = await self.engine._load_execution_checkpoint_by_id("ckpt-fb")
        with self.assertRaises(RuntimeError):
            await self.engine.run_company_delivery_self_evolution_checkpoint(
                loaded, action="approve"
            )
        refreshed = await self.engine._load_execution_checkpoint_by_id("ckpt-fb")
        self.assertEqual(refreshed.status, "pending")
        self.assertIn(
            "mid-flight crash",
            str(dict(refreshed.payload or {}).get("self_evolution_consume_error", "")),
        )


class SelfEvolutionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def test_deadline_cancels_only_selfevo_items(self) -> None:
        for work_item_id, kind, phase in (
            ("wi-se-1", "self_evolution", Phase.RUNNING),
            ("wi-se-2", "self_evolution", Phase.READY),
            ("wi-se-3", "self_evolution", Phase.APPROVED),
            ("wi-exec", "execute", Phase.RUNNING),
        ):
            item = DelegationWorkItem(
                work_item_id=work_item_id, run_id="run-1", role_id="cto",
                kind=kind, title=work_item_id, phase=Phase.READY,
            )
            await self.store.save_delegation_work_item(item)
            if phase is not Phase.READY:
                await self.store.update_delegation_work_item(work_item_id, phase=Phase.RUNNING)
            if phase not in (Phase.READY, Phase.RUNNING):
                await self.store.update_delegation_work_item(work_item_id, phase=phase)

        await self.engine._settle_self_evolution_deadline("run-1")

        expectations = {
            "wi-se-1": Phase.CANCELLED,
            "wi-se-2": Phase.CANCELLED,
            "wi-se-3": Phase.APPROVED,
            "wi-exec": Phase.RUNNING,
        }
        for work_item_id, expected in expectations.items():
            item = await self.store.get_delegation_work_item(work_item_id)
            self.assertEqual(item.phase, expected, work_item_id)


if __name__ == "__main__":
    unittest.main()
