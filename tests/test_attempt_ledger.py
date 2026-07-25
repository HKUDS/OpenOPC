"""Attempt-ledger settlement tests (issue #10 root fix).

Covers the structural anti-loop mechanism:
- the claim CAS opens a durable attempt (attempt_seq / attempt_settled)
- transition_work_item settles the attempt on every turn-boundary phase write
- crashed / interrupted streaks accumulate and the dispatcher refuses
  over-limit cards (is_dispatchable + _work_item_is_runnable)
- the per-tick reconcile pass back-fills dead attempts and terminalizes
  over-limit cards with a visible blocked_reason
- resume availability gate: a work item pinned to a disabled external agent
  fails closed at resume-prep instead of crash-looping through dispatch
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import (
    DelegationRoleSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    serialize_company_work_item_runtime_plan,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.phase import (
    ATTEMPT_CRASH_STREAK_LIMIT,
    ATTEMPT_INTERRUPTED_STREAK_LIMIT,
    attempt_ledger_dispatch_block_reason,
    has_open_attempt,
    is_dispatchable,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_transition import (
    settle_open_attempt_as_interrupted,
    transition_work_item,
)


class AttemptLedgerStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self) -> OPCStore:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = OPCStore(Path(tmpdir.name) / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        return store

    async def _seed_item(
        self,
        store: OPCStore,
        *,
        work_item_id: str = "wi-1",
        phase: Phase = Phase.READY,
        metadata: dict | None = None,
    ) -> DelegationWorkItem:
        item = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id="run-1",
            role_id="executor",
            seat_id="seat-1",
            title="Execution",
            summary="Do the work.",
            kind="execute",
            projection_id="execution",
            phase=phase,
            metadata=dict(metadata or {}),
        )
        await store.save_delegation_work_item(item)
        return item

    async def _claim(self, store: OPCStore, work_item_id: str, phase: Phase) -> DelegationWorkItem:
        claimed = await store.claim_delegation_work_item_if_dispatchable(
            work_item_id,
            expected_phase=phase,
            role_runtime_session_id="role-sess-1",
            seat_id="seat-1",
            task_id="task-1",
        )
        assert claimed is not None, "claim CAS unexpectedly failed"
        return claimed

    async def test_claim_cas_opens_attempt(self) -> None:
        store = await self._store()
        await self._seed_item(store)
        claimed = await self._claim(store, "wi-1", Phase.READY)
        metadata = dict(claimed.metadata or {})
        self.assertEqual(int(metadata.get("attempt_seq")), 1)
        self.assertFalse(bool(metadata.get("attempt_settled")))
        self.assertTrue(str(metadata.get("attempt_started_at", "")).strip())
        self.assertTrue(has_open_attempt(metadata))

        # Settle + release, then re-claim the orphaned RUNNING card: seq += 1.
        await transition_work_item(
            store,
            "wi-1",
            target_phase=Phase.READY,
            reason="recovery",
            release_claim=True,
        )
        settled = await store.get_delegation_work_item("wi-1")
        assert settled is not None
        self.assertTrue(bool(settled.metadata.get("attempt_settled")))
        # transition folded the claim clear into the same write, but the claim
        # mirror metadata keys are only dropped on terminal phases — clear them
        # the way the resume/suspend paths do before re-claiming.
        await store.update_delegation_work_item(
            "wi-1",
            metadata_updates={"claimed_by_role_session_id": "", "claimed_task_id": ""},
        )
        reclaimed = await self._claim(store, "wi-1", Phase.READY)
        self.assertEqual(int(reclaimed.metadata.get("attempt_seq")), 2)
        self.assertFalse(bool(reclaimed.metadata.get("attempt_settled")))

    async def test_transition_settles_attempt_clean(self) -> None:
        store = await self._store()
        await self._seed_item(store)
        await self._claim(store, "wi-1", Phase.READY)
        await transition_work_item(
            store,
            "wi-1",
            target_phase=Phase.AWAITING_MANAGER_REVIEW,
            reason="turn_done",
        )
        item = await store.get_delegation_work_item("wi-1")
        assert item is not None
        metadata = dict(item.metadata or {})
        self.assertTrue(bool(metadata.get("attempt_settled")))
        self.assertEqual(metadata.get("attempt_outcome"), Phase.AWAITING_MANAGER_REVIEW.value)
        self.assertEqual(int(metadata.get("attempt_crash_streak")), 0)
        self.assertEqual(int(metadata.get("attempt_interrupted_streak")), 0)
        self.assertFalse(has_open_attempt(metadata))

    async def test_crash_streak_accumulates_and_blocks_dispatch(self) -> None:
        store = await self._store()
        await self._seed_item(store)
        phase = Phase.READY
        for round_index in range(ATTEMPT_CRASH_STREAK_LIMIT):
            await self._claim(store, "wi-1", phase)
            # Crash + recovery-exit back to READY (RUNNING → READY is the
            # legal crash-recovery edge) with outcome=crashed.
            await transition_work_item(
                store,
                "wi-1",
                target_phase=Phase.READY,
                reason="crash_recovery",
                release_claim=True,
                attempt_outcome="crashed",
            )
            await store.update_delegation_work_item(
                "wi-1",
                metadata_updates={"claimed_by_role_session_id": "", "claimed_task_id": ""},
            )
            item = await store.get_delegation_work_item("wi-1")
            assert item is not None
            self.assertEqual(
                int(item.metadata.get("attempt_crash_streak")), round_index + 1
            )
            phase = item.phase

        item = await store.get_delegation_work_item("wi-1")
        assert item is not None
        self.assertTrue(attempt_ledger_dispatch_block_reason(item.metadata))
        self.assertFalse(is_dispatchable(item))
        # A clean settle resets the streak and dispatch reopens.
        await store.update_delegation_work_item(
            "wi-1",
            metadata_updates={
                "attempt_crash_streak": 0,
            },
        )
        item = await store.get_delegation_work_item("wi-1")
        assert item is not None
        self.assertTrue(is_dispatchable(item))

    async def test_settle_interrupted_is_idempotent_and_blocks_at_limit(self) -> None:
        store = await self._store()
        await self._seed_item(store)
        for round_index in range(ATTEMPT_INTERRUPTED_STREAK_LIMIT):
            claimed = await self._claim(
                store, "wi-1", Phase.READY if round_index == 0 else Phase.RUNNING
            )
            settled = await settle_open_attempt_as_interrupted(store, claimed)
            self.assertTrue(settled)
            # Second settle on the same attempt is a no-op.
            self.assertFalse(await settle_open_attempt_as_interrupted(store, claimed))
            item = await store.get_delegation_work_item("wi-1")
            assert item is not None
            self.assertEqual(
                int(item.metadata.get("attempt_interrupted_streak")), round_index + 1
            )
            # Free the claim like the suspend/startup sweeps do.
            await store.update_delegation_work_item(
                "wi-1",
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
                metadata_updates={"claimed_by_role_session_id": "", "claimed_task_id": ""},
            )
        item = await store.get_delegation_work_item("wi-1")
        assert item is not None
        self.assertTrue(attempt_ledger_dispatch_block_reason(item.metadata))
        self.assertFalse(is_dispatchable(item))


class AttemptLedgerReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def _executor(self) -> tuple[CompanyWorkItemExecutor, OPCStore]:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        root = Path(tmpdir.name)
        store = OPCStore(root / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        config = OPCConfig()
        config.org.company_profile = "custom"
        config.org.final_decider_role_id = "executor"
        config.org.roles = [
            RoleConfig(id="executor", name="Executor", responsibility="Do work.", reports_to="owner"),
        ]
        org_engine = OrgEngine(config, root)
        communication = CommunicationManager(store, EventBus(), llm=None, org_engine=org_engine)
        executor = CompanyWorkItemExecutor(
            org_engine=org_engine,
            communication=communication,
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=store.save_task,
            store=store,
            llm=None,
        )
        return executor, store

    async def test_reconcile_backfills_dead_attempt_and_terminalizes(self) -> None:
        executor, store = await self._executor()
        # A card whose owner died: open attempt, no claim, one interruption
        # short of the limit — the reconcile pass must settle (streak hits the
        # limit) and terminalize with a visible blocked_reason.
        item = DelegationWorkItem(
            work_item_id="wi-dead",
            run_id="run-1",
            role_id="executor",
            seat_id="seat-1",
            title="Doomed",
            summary="Crash loops forever.",
            kind="execute",
            projection_id="doomed",
            phase=Phase.RUNNING,
            metadata={
                "attempt_seq": ATTEMPT_INTERRUPTED_STREAK_LIMIT,
                "attempt_settled": False,
                "attempt_interrupted_streak": ATTEMPT_INTERRUPTED_STREAK_LIMIT - 1,
            },
        )
        await store.save_delegation_work_item(item)

        reconciled = await executor._reconcile_attempt_ledger([item], {})

        refreshed = await store.get_delegation_work_item("wi-dead")
        assert refreshed is not None
        self.assertEqual(refreshed.phase, Phase.FAILED)
        self.assertTrue(bool(refreshed.metadata.get("attempt_settled")))
        self.assertEqual(refreshed.metadata.get("attempt_outcome"), "interrupted")
        self.assertEqual(
            int(refreshed.metadata.get("attempt_interrupted_streak")),
            ATTEMPT_INTERRUPTED_STREAK_LIMIT,
        )
        self.assertIn("attempt ledger", str(refreshed.blocked_reason or ""))
        self.assertEqual(len(reconciled), 1)

    async def test_reconcile_leaves_live_and_healthy_items_alone(self) -> None:
        executor, store = await self._executor()
        healthy = DelegationWorkItem(
            work_item_id="wi-healthy",
            run_id="run-1",
            role_id="executor",
            seat_id="seat-1",
            title="Healthy",
            summary="Fine.",
            kind="execute",
            projection_id="healthy",
            phase=Phase.READY,
            metadata={},
        )
        await store.save_delegation_work_item(healthy)
        # Live item: open attempt but currently claimed by this runtime.
        live = DelegationWorkItem(
            work_item_id="wi-live",
            run_id="run-1",
            role_id="executor",
            seat_id="seat-1",
            title="Live",
            summary="Running now.",
            kind="execute",
            projection_id="live",
            phase=Phase.RUNNING,
            metadata={"attempt_seq": 1, "attempt_settled": False},
        )
        await store.save_delegation_work_item(live)
        executor.runtime._claimed_work_item_ids.add("wi-live")

        await executor._reconcile_attempt_ledger([healthy, live], {})

        refreshed_live = await store.get_delegation_work_item("wi-live")
        assert refreshed_live is not None
        self.assertEqual(refreshed_live.phase, Phase.RUNNING)
        self.assertFalse(bool(refreshed_live.metadata.get("attempt_settled")))
        refreshed_healthy = await store.get_delegation_work_item("wi-healthy")
        assert refreshed_healthy is not None
        self.assertEqual(refreshed_healthy.phase, Phase.READY)
        self.assertNotIn("attempt_settled", dict(refreshed_healthy.metadata or {}))


class ResumeAvailabilityGateTests(unittest.IsolatedAsyncioTestCase):
    """Fix-1: a resume pin to a disabled external agent fails closed."""

    async def _store(self) -> OPCStore:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = OPCStore(Path(tmpdir.name) / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        return store

    def _plan(self) -> CompanyWorkItemRuntimePlan:
        return CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="execution",
                    turn_type="execute",
                    title="Execution",
                    summary="Produce the main execution output.",
                    role_id="executor",
                )
            ],
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "final_decider_role_id": "executor",
                "top_level_role_ids": ["executor"],
            },
        )

    async def _seed(self, store: OPCStore, *, external_agent: str) -> Task:
        plan = self._plan()
        await store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id="role-runtime-1",
                run_id="run-1",
                project_id="proj1",
                role_id="executor",
                seat_id="seat-1",
            )
        )
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="work-item-1",
                run_id="run-1",
                role_id="executor",
                seat_id="seat-1",
                title="Execution",
                summary="Execute the project.",
                kind="execute",
                projection_id="execution",
                phase=Phase.RUNNING,
                claimed_by_role_runtime_session_id="role-runtime-1",
                claimed_by_seat_id="seat-1",
                metadata={"work_item_projection_id": "execution"},
            )
        )
        task = Task(
            id="execution-task",
            title="Execution",
            session_id="sess-child",
            parent_session_id="sess-parent",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="executor",
            assigned_external_agent=external_agent,
            execution_lock=True,
            metadata={
                "company_profile": "corporate",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "execution",
                "delegation_run_id": "run-1",
                "delegation_role_session_id": "role-runtime-1",
                "selected_execution_agent": external_agent,
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
            },
        )
        set_linked_work_item_id(task, "work-item-1")
        await store.save_task(task)
        await store.link_work_item_runtime_task("work-item-1", "execution-task")
        return task

    def _engine(self, store: OPCStore, *, available: list[str]) -> OPCEngine:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = store
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: list(available),
        )
        return engine

    async def test_resume_fails_closed_when_pinned_agent_unavailable(self) -> None:
        store = await self._store()
        task = await self._seed(store, external_agent="codex")
        engine = self._engine(store, available=["opencode"])  # codex disabled
        suspended = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        self.assertIsNotNone(suspended)

        executed: dict[str, list[Task]] = {}

        class DummyCompanyExecutor:
            async def execute(self, _plan, tasks: list[Task]) -> str:
                executed["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        self.assertIsNotNone(response)
        refreshed_item = await store.get_delegation_work_item("work-item-1")
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.FAILED)
        self.assertIn("codex", str(refreshed_item.blocked_reason or ""))
        refreshed_task = await store.get_task(task.id)
        assert refreshed_task is not None
        self.assertEqual(refreshed_task.status, TaskStatus.FAILED)
        self.assertEqual(
            refreshed_task.metadata.get("resume_unavailable_external_agent"),
            "codex",
        )
        # The runtime still executed (the rest of the org resumes normally).
        self.assertIn("tasks", executed)

    async def test_plain_message_after_gate_failure_converges_without_revival(self) -> None:
        """A plain text follow-up (final-decider routing path) on a run whose
        decider card failed terminally must drain the checkpoint and must not
        clobber the FAILED task back to PENDING (InvalidPhaseTransition crash
        found by the issue #10 end-to-end reproduction)."""
        store = await self._store()
        task = await self._seed(store, external_agent="codex")
        engine = self._engine(store, available=["opencode"])  # codex disabled
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )

        class DummyCompanyExecutor:
            async def execute(self, _plan, tasks: list[Task]) -> str:
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()
        # First resume: gate fails the codex-pinned decider card closed.
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        refreshed_item = await store.get_delegation_work_item("work-item-1")
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.FAILED)
        # Re-suspend cannot happen (run is terminal) — but if a pending
        # suspend checkpoint still exists, a plain message must converge
        # instead of raising InvalidPhaseTransition. Route a plain message
        # through the checkpoint machinery when present, else assert the
        # terminal state simply holds.
        response = await engine._maybe_resume_checkpoint("重跑", "sess-parent")
        self.assertNotIsInstance(response, Exception)
        refreshed_item = await store.get_delegation_work_item("work-item-1")
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.FAILED)
        refreshed_task = await store.get_task(task.id)
        assert refreshed_task is not None
        self.assertEqual(
            refreshed_task.status,
            TaskStatus.FAILED,
            "follow-up routing must not clobber the FAILED task projection",
        )
        remaining = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
        )
        self.assertEqual(
            [c.checkpoint_type for c in remaining],
            [],
            "suspend checkpoint must drain instead of bouncing back to pending",
        )

    async def test_resume_proceeds_when_pinned_agent_available(self) -> None:
        store = await self._store()
        task = await self._seed(store, external_agent="codex")
        engine = self._engine(store, available=["codex", "opencode"])
        suspended = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        self.assertIsNotNone(suspended)

        class DummyCompanyExecutor:
            async def execute(self, _plan, tasks: list[Task]) -> str:
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        refreshed_item = await store.get_delegation_work_item("work-item-1")
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.RUNNING)
        refreshed_task = await store.get_task(task.id)
        assert refreshed_task is not None
        self.assertEqual(refreshed_task.status, TaskStatus.RUNNING)
        self.assertEqual(
            refreshed_task.metadata.get("_company_runtime_resume_execution_agent_pin", {}).get(
                "selected_execution_agent"
            ),
            "codex",
        )


if __name__ == "__main__":
    unittest.main()
