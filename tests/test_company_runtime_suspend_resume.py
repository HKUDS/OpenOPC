from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from opc.core.models import (
    CompanyMemberSession,
    DelegationRoleSession,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor, serialize_company_work_item_runtime_plan
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.phase import is_dispatchable
from opc.layer2_organization.work_item_identity import mark_work_item_projection
from opc.layer3_agent.company_runtime_contract import build_company_work_item_contract
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)


class CompanyRuntimeSuspendResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self) -> OPCStore:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = OPCStore(Path(tmpdir.name) / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        return store

    def _plan(self, profile: str) -> CompanyWorkItemRuntimePlan:
        return CompanyWorkItemRuntimePlan(
            profile=profile,
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

    async def _seed_runtime(
        self,
        store: OPCStore,
        *,
        profile: str = "corporate",
        parent_session_id: str = "sess-parent",
        child_session_id: str = "sess-child",
        task_id: str = "execution-task",
        work_item_id: str = "work-item-1",
        role_session_id: str = "role-runtime-1",
        external_status: str = "suspended",
        external_session_id: str = "provider-session-1",
        external_resume_session_id: str = "provider-session-1",
        external_provider_session_id: str = "provider-session-1",
    ) -> tuple[CompanyWorkItemRuntimePlan, Task]:
        plan = self._plan(profile)
        await store.save_task(
            Task(
                id=f"ui-anchor-{parent_session_id}",
                title="Company chat",
                session_id=parent_session_id,
                project_id="proj1",
                status=TaskStatus.IDLE,
                metadata={
                    "exec_mode": "company",
                    "company_profile": profile,
                },
            )
        )
        await store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="run-1",
                project_id="proj1",
                role_id="executor",
                seat_id="seat-1",
            )
        )
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id=work_item_id,
                run_id="run-1",
                role_id="executor",
                seat_id="seat-1",
                title="Execution",
                summary="Execute the project.",
                kind="execute",
                projection_id="execution",
                phase=Phase.RUNNING,
                claimed_by_role_runtime_session_id=role_session_id,
                claimed_by_seat_id="seat-1",
                metadata={"work_item_projection_id": "execution"},
            )
        )
        task = Task(
            id=task_id,
            title="Execution",
            session_id=child_session_id,
            parent_session_id=parent_session_id,
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="executor",
            assigned_external_agent="codex",
            execution_lock=True,
            metadata={
                "company_profile": profile,
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "execution",
                "delegation_run_id": "run-1",
                "delegation_role_session_id": role_session_id,
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "progress_log": ["started", "working"],
                "runtime_v2": {
                    "runtime_session_id": "native-runtime-1",
                    "resume_cursor": "cursor-1",
                },
            },
        )
        set_linked_work_item_id(task, work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item_id, task_id)
        await store.save_external_session(
            ExternalSession(
                agent_type="codex",
                project_id="proj1",
                session_id=external_session_id,
                opc_session_id=role_session_id,
                task_id=task_id,
                workspace_path="/tmp/opc-test",
                run_mode="interactive",
                status=external_status,
                metadata={
                    "resume_session_id": external_resume_session_id,
                    "provider_session_id": external_provider_session_id,
                },
            )
        )
        return plan, task

    def _engine(self, store: OPCStore) -> OPCEngine:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = store
        return engine

    class _CapturingCompanyExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[CompanyWorkItemRuntimePlan, list[Task]]] = []

        async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
            self.calls.append((plan, tasks))
            return "runtime resumed"

    async def test_suspend_checkpoint_is_profile_agnostic_and_keeps_work_item_resumable(self) -> None:
        for profile in ("corporate", "custom"):
            with self.subTest(profile=profile):
                store = await self._store()
                _, task = await self._seed_runtime(
                    store,
                    profile=profile,
                    parent_session_id=f"sess-parent-{profile}",
                    child_session_id=f"sess-child-{profile}",
                    task_id=f"execution-task-{profile}",
                    work_item_id=f"work-item-{profile}",
                    role_session_id=f"role-runtime-{profile}",
                )
                engine = self._engine(store)

                result = await engine.suspend_company_runtime(
                    origin_task_id=task.id,
                    session_id=f"sess-parent-{profile}",
                    reason="user_stop",
                )

                self.assertIsNotNone(result)
                checkpoints = await store.get_pending_checkpoints(
                    project_id="proj1",
                    session_id=f"sess-parent-{profile}",
                    checkpoint_types=["company_runtime_suspended"],
                )
                refreshed_task = await store.get_task(task.id)
                refreshed_item = await store.get_delegation_work_item(f"work-item-{profile}")

                self.assertEqual(len(checkpoints), 1)
                payload = checkpoints[0].payload
                self.assertEqual(payload["version"], 2)
                self.assertEqual(payload["company_profile"], profile)
                self.assertEqual(payload["parent_session_id"], f"sess-parent-{profile}")
                self.assertEqual(payload["task_ids"], [task.id])
                self.assertIn(task.id, payload["native_runtime_resume"])
                self.assertIn(task.id, payload["external_sessions"])
                self.assertEqual(payload["progress_tail"][task.id], ["started", "working"])
                assert refreshed_task is not None
                assert refreshed_item is not None
                self.assertNotEqual(refreshed_task.status, TaskStatus.CANCELLED)
                self.assertEqual(refreshed_task.status, TaskStatus.RUNNING)
                self.assertFalse(refreshed_task.execution_lock)
                self.assertNotEqual(refreshed_item.phase, Phase.CANCELLED)
                self.assertEqual(refreshed_item.phase, Phase.RUNNING)
                self.assertEqual(refreshed_item.claimed_by_role_runtime_session_id, "")
                self.assertEqual(refreshed_item.claimed_by_seat_id, "")
                self.assertEqual(refreshed_item.metadata.get("dispatch_hold"), "company_runtime_suspended")
                self.assertFalse(is_dispatchable(refreshed_item))

    async def test_continue_resumes_from_suspend_checkpoint_with_native_and_external_state(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["plan"] = plan
                captured["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        checkpoints = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
        )
        resumed_task = captured["tasks"][0]
        refreshed_item = await store.get_delegation_work_item("work-item-1")

        self.assertIn("Resuming the suspended company runtime", response)
        self.assertEqual(captured["plan"].profile, "corporate")
        self.assertEqual(resumed_task.status, TaskStatus.RUNNING)
        self.assertEqual(
            resumed_task.context_snapshot["runtime_resume"]["runtime_session_id"],
            "native-runtime-1",
        )
        self.assertEqual(resumed_task.metadata["external_resume_session_id"], "provider-session-1")
        self.assertEqual(resumed_task.metadata["external_resume_agent_type"], "codex")
        self.assertEqual(checkpoints, [])
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.RUNNING)
        self.assertEqual(refreshed_item.metadata.get("dispatch_hold"), "")
        self.assertEqual(refreshed_item.claimed_by_role_runtime_session_id, "")

    async def test_continue_merges_task_suspended_after_checkpoint_creation(self) -> None:
        store = await self._store()
        plan, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="startup_interrupted",
        )

        review_item = DelegationWorkItem(
            work_item_id="review-item-v3",
            run_id="run-1",
            role_id="executor",
            seat_id="seat-1",
            title="Review v3",
            summary="Review the completed report.",
            kind="review",
            projection_id="review-v3",
            phase=Phase.RUNNING,
            metadata={
                "work_item_projection_id": "review-v3",
                "dispatch_hold": "company_runtime_suspended",
                "suspend_checkpoint_type": "company_runtime_interrupted",
                "suspended_phase": Phase.RUNNING.value,
            },
        )
        await store.save_delegation_work_item(review_item)
        review_task = Task(
            id="review-task-v3",
            title="Review v3",
            session_id="sess-review-v3",
            parent_session_id="sess-parent",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="executor",
            assigned_external_agent="codex",
            metadata={
                "company_profile": "corporate",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "review-v3",
                "delegation_run_id": "run-1",
                "delegation_role_session_id": "role-runtime-1",
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "company_runtime_stop_state": "suspended",
                "company_runtime_suspend_checkpoint_type": "company_runtime_interrupted",
                "company_runtime_suspended_at": datetime.now().isoformat(),
                "dispatch_hold": "company_runtime_suspended",
                "suspended_task_status": TaskStatus.RUNNING.value,
            },
        )
        set_linked_work_item_id(review_task, review_item.work_item_id)
        await store.save_task(review_task)
        await store.link_work_item_runtime_task(review_item.work_item_id, review_task.id)

        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(
                self,
                resumed_plan: CompanyWorkItemRuntimePlan,
                tasks: list[Task],
            ) -> str:
                captured["plan"] = resumed_plan
                captured["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        resumed_ids = {item.id for item in captured["tasks"]}
        refreshed_review_item = await store.get_delegation_work_item(
            review_item.work_item_id
        )
        resolved = await store.get_execution_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )

        self.assertEqual(resumed_ids, {task.id, review_task.id})
        assert refreshed_review_item is not None
        self.assertEqual(refreshed_review_item.metadata.get("dispatch_hold"), "")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(
            set(resolved[0].payload.get("task_ids", [])),
            {task.id, review_task.id},
        )
        self.assertEqual(
            {
                snapshot.get("task_id")
                for snapshot in resolved[0].payload.get("task_snapshots", [])
            },
            {task.id, review_task.id},
        )

    async def test_runtime_refreshes_canonical_adapter_state_before_persisting(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        role_session_id = "role-runtime-1"
        stale_state = {
            "selected_execution_agent": "codex",
            "external_resume_session_id": "thread-terminal",
            "external_resume_agent_type": "codex",
            "codex": {
                "resume_session_id": "thread-terminal",
                "provider_session_id": "thread-terminal",
            },
        }
        role_session = await store.get_delegation_role_session(role_session_id)
        assert role_session is not None
        role_session.adapter_session_state = dict(stale_state)
        await store.save_delegation_role_session(role_session)

        runtime = CompanyRuntime(org_engine=None, communication=None, store=store)
        runtime.role_sessions[role_session_id] = role_session
        member = CompanyMemberSession(
            member_session_id="member-operations",
            role_session_id=role_session_id,
            role_id="executor",
            employee_id="employee-1",
            adapter_session_state=dict(stale_state),
        )
        await store.update_role_session_adapter_state(
            role_session_id,
            "codex",
            None,
        )

        await runtime._refresh_adapter_session_state_from_store(member)

        self.assertNotIn("codex", member.adapter_session_state)
        self.assertNotIn(
            "external_resume_session_id",
            member.adapter_session_state,
        )
        self.assertEqual(
            runtime.role_sessions[role_session_id].adapter_session_state,
            member.adapter_session_state,
        )

    async def test_second_stop_during_resumed_execution_restores_pending_checkpoint(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        first_stop = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        self.assertIsNotNone(first_stop)

        execution_started = asyncio.Event()

        class BlockingCompanyExecutor:
            def __init__(self) -> None:
                self._notify_kanban_changed = AsyncMock()

            async def execute(
                self,
                _plan: CompanyWorkItemRuntimePlan,
                _tasks: list[Task],
            ) -> str:
                execution_started.set()
                await asyncio.Event().wait()
                return "unreachable"

        executor = BlockingCompanyExecutor()
        engine.company_executor = executor  # type: ignore[assignment]
        resume_task = asyncio.create_task(engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        ))
        await asyncio.wait_for(execution_started.wait(), timeout=1)
        executor._notify_kanban_changed.assert_awaited_once_with()
        self.assertTrue(engine._active_task_run_registry.is_active("proj1", task.id))

        second_stop = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        self.assertIsNotNone(second_stop)
        self.assertEqual(
            second_stop["checkpoint_id"],
            first_stop["checkpoint_id"],
        )

        resume_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await resume_task

        pending = await store.get_execution_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        refreshed_task = await store.get_task(task.id)
        refreshed_item = await store.get_delegation_work_item("work-item-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].checkpoint_id, first_stop["checkpoint_id"])
        self.assertEqual(pending[0].payload.get("resume_state"), "interrupted")
        assert refreshed_task is not None
        assert refreshed_item is not None
        self.assertEqual(
            refreshed_task.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(
            refreshed_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertFalse(engine._active_task_run_registry.is_active("proj1", task.id))

    async def test_resume_attempt_pins_unlocked_auto_external_agent_without_permanent_lock(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        task.assigned_external_agent = "opencode"
        task.metadata.update({
            "delegation_seat_id": "seat-1",
            "employee_assignment": {"employee_id": "employee-executor"},
            "selected_execution_agent": "codex",
            "selected_execution_agent_source": "fallback_rules",
            "execution_agent_locked": False,
            "preferred_external_agent": "codex",
            "agent_selection": {
                "selected": "opencode",
                "selection_source": "llm",
            },
        })
        await store.save_task(task)
        engine = self._engine(store)
        engine.org_engine = SimpleNamespace()
        engine._available_external_agents = lambda: ["opencode", "codex"]
        adaptive_selector = AsyncMock(return_value=(
            "codex",
            {
                "selected": "codex",
                "selection_source": "fallback_rules",
            },
        ))
        engine._select_task_execution_agent_via_llm = adaptive_selector
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_pending_checkpoints(
                project_id="proj1",
                session_id="sess-parent",
                checkpoint_types=["company_runtime_suspended"],
            )
        )[0]
        checkpoint_identity = checkpoint.payload["task_snapshots"][0][
            "execution_identity"
        ]
        self.assertEqual(checkpoint_identity["selected_execution_agent"], "opencode")
        self.assertEqual(checkpoint_identity["assigned_external_agent"], "opencode")
        self.assertEqual(checkpoint_identity["agent_selection_source"], "llm")
        captured: dict[str, Any] = {}

        class SelectingExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                resumed = tasks[0]
                captured["selected"] = await engine._assign_task_execution_agent(
                    resumed,
                    role=SimpleNamespace(),
                )
                captured["task"] = resumed
                return "runtime resumed"

        engine.company_executor = SelectingExecutor()
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        resumed_task = captured["task"]
        self.assertEqual(captured["selected"], "opencode")
        self.assertEqual(resumed_task.assigned_external_agent, "opencode")
        self.assertEqual(resumed_task.assigned_to, "executor")
        self.assertEqual(resumed_task.metadata["delegation_seat_id"], "seat-1")
        self.assertEqual(
            resumed_task.metadata["employee_assignment"]["employee_id"],
            "employee-executor",
        )
        self.assertFalse(resumed_task.metadata["execution_agent_locked"])
        self.assertNotIn(
            "_company_runtime_resume_execution_agent_pin",
            resumed_task.metadata,
        )
        self.assertEqual(
            resumed_task.metadata["agent_selection"]["selection_source"],
            "company_runtime_resume_checkpoint",
        )
        adaptive_selector.assert_not_awaited()

        # A later ordinary dispatch has no checkpoint pin and may adapt again.
        selected_after_resume = await engine._assign_task_execution_agent(
            resumed_task,
            role=SimpleNamespace(),
        )
        self.assertEqual(selected_after_resume, "codex")
        adaptive_selector.assert_awaited_once()

    async def test_resume_attempt_pins_native_without_running_adaptive_selector(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        task.assigned_external_agent = None
        task.metadata.update({
            "selected_execution_agent": "native",
            "selected_execution_agent_source": "fallback_rules",
            "execution_agent_locked": False,
        })
        await store.save_task(task)
        engine = self._engine(store)
        engine.org_engine = SimpleNamespace()
        engine._available_external_agents = lambda: ["opencode", "codex"]
        adaptive_selector = AsyncMock(return_value=(
            "opencode",
            {"selected": "opencode", "selection_source": "fallback_rules"},
        ))
        engine._select_task_execution_agent_via_llm = adaptive_selector
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class SelectingExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["selected"] = await engine._assign_task_execution_agent(
                    tasks[0],
                    role=SimpleNamespace(),
                )
                captured["task"] = tasks[0]
                return "runtime resumed"

        engine.company_executor = SelectingExecutor()
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        self.assertIsNone(captured["selected"])
        self.assertIsNone(captured["task"].assigned_external_agent)
        self.assertEqual(
            captured["task"].metadata["selected_execution_agent"],
            "native",
        )
        adaptive_selector.assert_not_awaited()

    async def test_resume_identity_mismatch_fails_closed_and_returns_checkpoint_to_pending(self) -> None:
        mutations = {
            "role_id": lambda item: setattr(item, "role_id", "replacement-role"),
            "seat_id": lambda item: setattr(item, "seat_id", "replacement-seat"),
            "role_runtime_session_id": lambda item: setattr(
                item,
                "role_runtime_session_id",
                "replacement-role-session",
            ),
            "employee_id": lambda item: item.metadata.update({
                "employee_assignment": {"employee_id": "replacement-employee"},
            }),
        }
        for field_name, mutate in mutations.items():
            with self.subTest(field_name=field_name):
                store = await self._store()
                _, task = await self._seed_runtime(store)
                task.metadata.update({
                    "delegation_seat_id": "seat-1",
                    "employee_assignment": {"employee_id": "employee-executor"},
                    "selected_execution_agent": "codex",
                    "selected_execution_agent_source": "fallback_rules",
                    "execution_agent_locked": False,
                })
                await store.save_task(task)
                work_item = await store.get_delegation_work_item("work-item-1")
                assert work_item is not None
                work_item.metadata = {
                    **dict(work_item.metadata or {}),
                    "employee_assignment": {"employee_id": "employee-executor"},
                }
                await store.save_delegation_work_item(work_item)

                engine = self._engine(store)
                await engine.suspend_company_runtime(
                    origin_task_id=task.id,
                    session_id="sess-parent",
                    reason="user_stop",
                )
                mutate(work_item)
                await store.save_delegation_work_item(work_item)
                executor = self._CapturingCompanyExecutor()
                engine.company_executor = executor

                with self.assertRaisesRegex(RuntimeError, field_name):
                    await engine._maybe_resume_checkpoint(
                        "continue",
                        "sess-parent",
                        reply_metadata={"ui_force_resume": True},
                    )

                pending = await store.get_execution_checkpoints(
                    project_id="proj1",
                    session_id="sess-parent",
                    checkpoint_types=["company_runtime_suspended"],
                    statuses=["pending"],
                )
                resuming = await store.get_execution_checkpoints(
                    project_id="proj1",
                    session_id="sess-parent",
                    checkpoint_types=["company_runtime_suspended"],
                    statuses=["resuming"],
                )
                self.assertEqual(len(pending), 1)
                self.assertEqual(resuming, [])
                self.assertEqual(
                    pending[0].payload.get("resume_state"),
                    "failed_before_handoff",
                )
                self.assertEqual(executor.calls, [])

    async def test_resume_repairs_stale_task_projection_from_work_item_and_role_session(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        task.metadata.update({
            "delegation_seat_id": "seat-1",
            "employee_assignment": {"employee_id": "employee-executor"},
            "selected_execution_agent": "codex",
        })
        await store.save_task(task)
        work_item = await store.get_delegation_work_item("work-item-1")
        role_session = await store.get_delegation_role_session("role-runtime-1")
        assert work_item is not None and role_session is not None
        work_item.metadata = {
            **dict(work_item.metadata or {}),
            "employee_assignment": {"employee_id": "employee-executor"},
        }
        role_session.employee_id = "employee-executor"
        await store.save_delegation_work_item(work_item)
        await store.save_delegation_role_session(role_session)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )

        stale_task = await store.get_task(task.id)
        assert stale_task is not None
        stale_task.assigned_to = "stale-role"
        stale_task.metadata.update({
            "delegation_seat_id": "stale-seat",
            "delegation_role_session_id": "stale-role-session",
            "employee_assignment": {"employee_id": "stale-employee"},
        })
        await store.save_task(stale_task)
        captured: dict[str, Task] = {}

        class CapturingExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["task"] = tasks[0]
                return "resumed"

        engine.company_executor = CapturingExecutor()
        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        repaired = captured["task"]
        self.assertEqual(repaired.assigned_to, "executor")
        self.assertEqual(repaired.metadata["delegation_seat_id"], "seat-1")
        self.assertEqual(
            repaired.metadata["delegation_role_session_id"],
            "role-runtime-1",
        )
        self.assertEqual(
            repaired.metadata["employee_assignment"]["employee_id"],
            "employee-executor",
        )

    async def test_resume_rejects_role_runtime_session_identity_mismatch(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        role_session = await store.get_delegation_role_session("role-runtime-1")
        assert role_session is not None
        role_session.role_id = "replacement-role"
        await store.save_delegation_role_session(role_session)
        engine.company_executor = self._CapturingCompanyExecutor()

        with self.assertRaisesRegex(RuntimeError, "role session role_id"):
            await engine._maybe_resume_checkpoint(
                "continue",
                "sess-parent",
                reply_metadata={"ui_force_resume": True},
            )

    async def test_resume_rejects_role_runtime_session_employee_mismatch(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        task.metadata["employee_assignment"] = {"employee_id": "employee-executor"}
        await store.save_task(task)
        work_item = await store.get_delegation_work_item("work-item-1")
        role_session = await store.get_delegation_role_session("role-runtime-1")
        assert work_item is not None and role_session is not None
        work_item.metadata["employee_assignment"] = {
            "employee_id": "employee-executor",
        }
        role_session.employee_id = "employee-executor"
        await store.save_delegation_work_item(work_item)
        await store.save_delegation_role_session(role_session)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )

        role_session.employee_id = "replacement-employee"
        await store.save_delegation_role_session(role_session)
        engine.company_executor = self._CapturingCompanyExecutor()

        with self.assertRaisesRegex(RuntimeError, "role session employee_id"):
            await engine._maybe_resume_checkpoint(
                "continue",
                "sess-parent",
                reply_metadata={"ui_force_resume": True},
            )

    async def test_resume_accepts_work_item_seat_in_multi_seat_role_session(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        work_item = await store.get_delegation_work_item("work-item-1")
        role_session = await store.get_delegation_role_session("role-runtime-1")
        assert work_item is not None and role_session is not None

        work_item_seat_id = "seat::team::ceo::cto"
        home_seat_id = "seat::team::cto::cto"
        work_item.seat_id = work_item_seat_id
        work_item.role_runtime_session_id = "role-runtime-1"
        work_item.claimed_by_seat_id = work_item_seat_id
        work_item.metadata["employee_assignment"] = {
            "employee_id": "employee-executor",
        }
        role_session.seat_id = home_seat_id
        role_session.seat_ids = [home_seat_id, work_item_seat_id]
        role_session.employee_id = "employee-executor"
        task.metadata["delegation_seat_id"] = work_item_seat_id
        task.metadata["employee_assignment"] = {
            "employee_id": "employee-executor",
        }
        await store.save_delegation_work_item(work_item)
        await store.save_delegation_role_session(role_session)
        await store.save_task(task)

        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        executor = self._CapturingCompanyExecutor()
        engine.company_executor = executor

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        self.assertEqual(len(executor.calls), 1)
        resumed_task = executor.calls[0][1][0]
        self.assertEqual(
            resumed_task.metadata["delegation_seat_id"],
            work_item_seat_id,
        )
        self.assertEqual(resumed_task.assigned_to, "executor")
        self.assertEqual(resumed_task.assigned_external_agent, "codex")
        self.assertEqual(
            resumed_task.metadata["delegation_role_session_id"],
            "role-runtime-1",
        )
        self.assertEqual(
            resumed_task.metadata["employee_assignment"]["employee_id"],
            "employee-executor",
        )
        resumed_work_item = await store.get_delegation_work_item("work-item-1")
        resumed_role_session = await store.get_delegation_role_session(
            "role-runtime-1",
        )
        assert resumed_work_item is not None and resumed_role_session is not None
        self.assertEqual(resumed_work_item.seat_id, work_item_seat_id)
        self.assertEqual(
            resumed_work_item.role_runtime_session_id,
            "role-runtime-1",
        )
        self.assertEqual(resumed_role_session.seat_id, home_seat_id)
        self.assertEqual(resumed_role_session.employee_id, "employee-executor")
        self.assertCountEqual(
            resumed_role_session.seat_ids,
            [home_seat_id, work_item_seat_id],
        )

    async def test_force_native_real_work_item_path_consumes_resume_pin(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        task.assigned_external_agent = None
        task.metadata.update({
            "force_native_execution": True,
            "selected_execution_agent": "native",
            "agent_selection": {
                "selected": "native",
                "selection_source": "forced_native",
            },
        })
        await store.save_task(task)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_pending_checkpoints(
                project_id="proj1",
                session_id="sess-parent",
                checkpoint_types=["company_runtime_suspended"],
            )
        )[0]
        stored_task = await store.get_task(task.id)
        assert stored_task is not None
        resumed = (
            await engine._prepare_company_runtime_tasks_for_resume(
                [stored_task],
                checkpoint.payload,
                resume_task_ids={task.id},
            )
        )[0]
        self.assertIn(
            "_company_runtime_resume_execution_agent_pin",
            resumed.metadata,
        )

        role = SimpleNamespace(role_id="executor", preferred_external_agent="codex")

        class OrgEngine:
            @staticmethod
            def get_role_for_work_item(role_id: str, tags: list[str]) -> Any:
                return role

        engine.org_engine = OrgEngine()
        executor = CompanyWorkItemExecutor(
            org_engine=engine.org_engine,
            communication=None,
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(return_value=TaskResult(
                status=TaskStatus.FAILED,
                content="stop after selector",
            )),
            save_task=store.save_task,
            store=store,
            agent_selector=engine._assign_task_execution_agent,
        )

        result = await executor._run_work_item(resumed, {"execution": resumed})

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertNotIn(
            "_company_runtime_resume_execution_agent_pin",
            resumed.metadata,
        )
        self.assertEqual(
            resumed.metadata["agent_selection"]["selection_source"],
            "company_runtime_resume_checkpoint",
        )
        self.assertIsNone(resumed.assigned_external_agent)

    async def test_resume_does_not_overwrite_newer_durable_role_adapter_state(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        await store.update_role_session_adapter_state(
            "role-runtime-1",
            "codex",
            {"resume_session_id": "thread-before-stop", "updated_at": "2026-01-01"},
        )
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        await store.update_role_session_adapter_state(
            "role-runtime-1",
            "codex",
            {"resume_session_id": "thread-after-checkpoint", "updated_at": "2026-07-14"},
        )
        engine.company_executor = self._CapturingCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )

        state = await store.get_role_session_adapter_state(
            "role-runtime-1",
            "codex",
        )
        assert state is not None
        self.assertEqual(state["resume_session_id"], "thread-after-checkpoint")

    async def test_resume_fixed_backend_failure_never_tries_alternate_or_native(self) -> None:
        engine = OPCEngine(project_id="proj1")
        attempted_agents: list[str] = []

        class Adapter:
            def __init__(self, name: str) -> None:
                self.agent_type = name
                self.config = SimpleNamespace(
                    session_mode="auto",
                    run_mode="batch",
                )

            def supports_interactive(self) -> bool:
                return False

            def build_invocation(
                self,
                task: Task,
                workspace_path: str | None = None,
            ) -> tuple[list[str], dict[str, Any]]:
                return [self.agent_type], {
                    "agent": self.agent_type,
                    "command": self.agent_type,
                }

        codex = Adapter("codex")
        opencode = Adapter("opencode")

        class Registry:
            def get_ordered_available(self) -> list[tuple[str, Adapter]]:
                return [("opencode", opencode), ("codex", codex)]

            def get(self, name: str) -> Adapter | None:
                return {"codex": codex, "opencode": opencode}.get(name)

        class Broker:
            async def run(
                self,
                *,
                adapter: Adapter,
                task: Task,
                workspace_path: str,
                on_progress: Any = None,
                prepared_task: Task | None = None,
            ) -> TaskResult:
                attempted_agents.append(adapter.agent_type)
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content=f"{adapter.agent_type} failed",
                    artifacts={},
                )

        engine.adapter_registry = Registry()
        engine.external_broker = Broker()
        engine._resolve_external_workspace = lambda task: "/tmp/workspace"
        engine._build_external_agent_task = AsyncMock(side_effect=lambda task: task)
        engine._configure_external_adapter_for_task = AsyncMock(
            side_effect=lambda task, adapter: (adapter, {}),
        )
        engine._emit_external_agent_audit = AsyncMock()
        engine._run_native_agent = AsyncMock(return_value=TaskResult(
            status=TaskStatus.DONE,
            content="native fallback",
        ))
        task = Task(
            id="resume-fixed-backend",
            title="Resume fixed backend",
            project_id="proj1",
            assigned_external_agent="codex",
            metadata={
                "target_output_dir": "/tmp/workspace",
                "agent_selection": {
                    "selection_source": "company_runtime_resume_checkpoint",
                },
            },
        )

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(attempted_agents, ["codex"])
        engine._run_native_agent.assert_not_awaited()

    async def test_suspend_prefers_provider_token_over_newer_synthetic_monitor_row(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        await store.save_external_session(ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id="codex:proj1:execution-task",
            opc_session_id="role-runtime-1",
            task_id=task.id,
            workspace_path="/tmp/opc-test",
            run_mode="interactive",
            status="working",
            metadata={},
            updated_at=datetime.now() + timedelta(seconds=1),
        ))
        engine = self._engine(store)

        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_pending_checkpoints(
                project_id="proj1",
                session_id="sess-parent",
                checkpoint_types=["company_runtime_suspended"],
            )
        )[0]
        external = checkpoint.payload["external_sessions"][task.id]

        self.assertEqual(external["session_id"], "provider-session-1")
        self.assertEqual(external["resume_session_id"], "provider-session-1")

    async def test_text_after_stop_routes_to_final_decider_instead_of_plain_resume(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["plan"] = plan
                captured["tasks"] = tasks
                return "ceo handled follow-up"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
        )
        checkpoints = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
        )
        routed_task = captured["tasks"][0]
        refreshed_item = await store.get_delegation_work_item("work-item-1")

        self.assertEqual(response, "ceo handled follow-up")
        self.assertEqual(captured["plan"].metadata["final_decider_role_id"], "executor")
        self.assertEqual(routed_task.status, TaskStatus.PENDING)
        self.assertEqual(routed_task.context_snapshot["user_supplied_input"], "continue")
        self.assertEqual(routed_task.metadata["latest_user_directive"], "continue")
        self.assertEqual(routed_task.metadata["manager_mutation_user_input"], "continue")
        self.assertTrue(routed_task.metadata["followup_routed_to_final_decider"])
        self.assertEqual(checkpoints, [])
        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.READY)
        self.assertEqual(refreshed_item.metadata.get("dispatch_hold"), "")
        self.assertEqual(refreshed_item.metadata.get("resume_source"), "primary_session_followup")
        self.assertEqual(refreshed_item.metadata.get("resume_user_reply"), "continue")
        self.assertEqual(refreshed_item.metadata.get("latest_user_directive"), "continue")
        self.assertEqual(refreshed_item.metadata.get("manager_mutation_user_input"), "continue")
        self.assertEqual(refreshed_item.metadata.get("current_turn_mode"), "dispatch_required")
        self.assertTrue(refreshed_item.metadata.get("followup_routed_to_final_decider"))

    async def test_text_after_stop_from_child_session_routes_to_parent_final_decider(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["plan"] = plan
                captured["tasks"] = tasks
                return "ceo handled child-session follow-up"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_checkpoint(
            "把当前任务改成水下潜艇风格，CEO 自己判断 work item 变更。",
            "sess-child",
        )
        parent_checkpoints = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
        )
        child_checkpoints = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-child",
            checkpoint_types=["company_runtime_suspended"],
        )
        routed_task = captured["tasks"][0]

        self.assertEqual(response, "ceo handled child-session follow-up")
        self.assertEqual(captured["plan"].metadata["final_decider_role_id"], "executor")
        self.assertEqual(parent_checkpoints, [])
        self.assertEqual(child_checkpoints, [])
        self.assertEqual(routed_task.context_snapshot["user_supplied_input"], "把当前任务改成水下潜艇风格，CEO 自己判断 work item 变更。")
        self.assertEqual(routed_task.metadata["latest_user_directive"], "把当前任务改成水下潜艇风格，CEO 自己判断 work item 变更。")
        self.assertTrue(routed_task.metadata["followup_routed_to_final_decider"])

    async def test_text_after_stop_keeps_non_decider_work_held_until_after_ceo_arbitration(self) -> None:
        store = await self._store()
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="ceo-deliver",
                    turn_type="deliver",
                    title="CEO delivery",
                    summary="Final decision and delivery.",
                    role_id="ceo",
                ),
                WorkItemProjectionSpec(
                    projection_id="worker-execute",
                    turn_type="execute",
                    title="Worker execution",
                    summary="Build the game.",
                    role_id="engineer",
                ),
            ],
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "final_decider_role_id": "ceo",
                "top_level_role_ids": ["ceo"],
            },
        )
        serialized_plan = serialize_company_work_item_runtime_plan(plan)

        ceo_item = DelegationWorkItem(
            work_item_id="wi-ceo",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            role_runtime_session_id="role-ceo",
            title="CEO delivery",
            kind="deliver",
            projection_id="ceo-deliver",
            phase=Phase.RUNNING,
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "runtime_model": "multi_team_org"},
                projection_id="ceo-deliver",
                turn_type="deliver",
            ),
        )
        worker_item = DelegationWorkItem(
            work_item_id="wi-worker",
            run_id="run-1",
            role_id="engineer",
            seat_id="seat-engineer",
            role_runtime_session_id="role-engineer",
            title="Worker execution",
            kind="execute",
            projection_id="worker-execute",
            phase=Phase.RUNNING,
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "runtime_model": "multi_team_org"},
                projection_id="worker-execute",
                turn_type="execute",
            ),
        )
        await store.save_delegation_work_item(ceo_item)
        await store.save_delegation_work_item(worker_item)
        await store.save_delegation_role_session(DelegationRoleSession(
            role_session_id="role-ceo",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
        ))
        await store.save_delegation_role_session(DelegationRoleSession(
            role_session_id="role-engineer",
            run_id="run-1",
            role_id="engineer",
            seat_id="seat-engineer",
        ))

        common_metadata = {
            "company_profile": "corporate",
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "work_item_runtime": True,
            "delegation_run_id": "run-1",
            "company_work_item_plan": serialized_plan,
        }
        ceo_task = Task(
            id="task-ceo",
            title="CEO delivery",
            session_id="sess-ceo",
            parent_session_id="sess-parent",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {**common_metadata, "delegation_role_session_id": "role-ceo"},
                projection_id="ceo-deliver",
                turn_type="deliver",
            ),
        )
        worker_task = Task(
            id="task-worker",
            title="Worker execution",
            session_id="sess-worker",
            parent_session_id="sess-parent",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="engineer",
            assigned_external_agent="codex",
            metadata=mark_work_item_projection(
                {**common_metadata, "delegation_role_session_id": "role-engineer"},
                projection_id="worker-execute",
                turn_type="execute",
            ),
        )
        set_linked_work_item_id(ceo_task, "wi-ceo")
        set_linked_work_item_id(worker_task, "wi-worker")
        await store.save_task(ceo_task)
        await store.save_task(worker_task)
        await store.link_work_item_runtime_task("wi-ceo", ceo_task.id)
        await store.link_work_item_runtime_task("wi-worker", worker_task.id)

        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=worker_task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        execute_snapshots: list[dict[str, str]] = []

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                ceo = await store.get_delegation_work_item("wi-ceo")
                worker = await store.get_delegation_work_item("wi-worker")
                assert ceo is not None
                assert worker is not None
                execute_snapshots.append(
                    {
                        "ceo_hold": str(ceo.metadata.get("dispatch_hold", "") or ""),
                        "worker_hold": str(worker.metadata.get("dispatch_hold", "") or ""),
                    }
                )
                if len(execute_snapshots) == 1:
                    routed = await store.get_task("task-ceo")
                    assert routed is not None
                    routed.status = TaskStatus.DONE
                    routed.metadata = {
                        **dict(routed.metadata or {}),
                        "manager_board_mutation_performed": True,
                    }
                    await store.save_task(routed)
                return f"execute call {len(execute_snapshots)}"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_checkpoint(
            "改成霓虹节奏躲避游戏，CEO 自己判断修改、删除或新增 work item。",
            "sess-parent",
        )
        refreshed_ceo = await store.get_delegation_work_item("wi-ceo")
        refreshed_worker = await store.get_delegation_work_item("wi-worker")
        routed_ceo_task = await store.get_task("task-ceo")

        self.assertIn("Routed the latest user follow-up", response)
        self.assertGreaterEqual(len(execute_snapshots), 2)
        self.assertEqual(execute_snapshots[0]["ceo_hold"], "")
        self.assertEqual(execute_snapshots[0]["worker_hold"], "company_runtime_suspended")
        assert refreshed_ceo is not None
        assert refreshed_worker is not None
        assert routed_ceo_task is not None
        self.assertEqual(refreshed_worker.metadata.get("dispatch_hold"), "")
        self.assertEqual(refreshed_ceo.metadata.get("resume_source"), "primary_session_followup")
        self.assertEqual(
            routed_ceo_task.context_snapshot["user_supplied_input"],
            "改成霓虹节奏躲避游戏，CEO 自己判断修改、删除或新增 work item。",
        )

    async def test_text_after_stop_prefers_completed_manager_root_over_blocked_delivery(self) -> None:
        engine = self._engine(await self._store())
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "final_decider_role_id": "ceo",
                "top_level_role_ids": ["ceo"],
            },
        )
        intake = Task(
            id="task-intake",
            title="CEO Intake",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {"work_item_runtime": True},
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        delivery = Task(
            id="task-delivery",
            title="CEO Delivery",
            status=TaskStatus.BLOCKED,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "dependency_work_item_ids": ["wi-worker"]},
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
        )
        worker = Task(
            id="task-worker",
            title="Worker",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="engineer",
            metadata=mark_work_item_projection(
                {"work_item_runtime": True},
                projection_id="worker-execute",
                turn_type="execute",
            ),
        )

        target = engine._company_followup_target_task(plan, [delivery, worker, intake])

        assert target is not None
        self.assertEqual(target.id, "task-intake")

    async def test_followup_target_prefers_open_final_delivery_review_over_done_intake(self) -> None:
        engine = self._engine(await self._store())
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "final_decider_role_id": "ceo",
                "top_level_role_ids": ["ceo"],
            },
        )
        intake = Task(
            id="task-intake",
            title="CEO Intake",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {"work_item_runtime": True},
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        delivery = Task(
            id="task-delivery",
            title="CEO Delivery",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "execution_mode": "company_mode",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                    "feedback_scope": "final",
                },
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
        )

        target = engine._company_followup_target_task(plan, [intake, delivery])

        assert target is not None
        self.assertEqual(target.id, "task-delivery")

    async def test_final_delivery_followup_preserves_delivery_identity(self) -> None:
        store = await self._store()
        engine = self._engine(store)
        item = DelegationWorkItem(
            work_item_id="wi-delivery",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Delivery",
            kind="delivery",
            projection_id="ceo-delivery",
            phase=Phase.AWAITING_HUMAN,
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "execution_mode": "company_mode",
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                    "feedback_scope": "final",
                    "delivery_package": {"executive_summary": "old package"},
                    "final_delivery_package": {"executive_summary": "old final package"},
                },
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
        )
        await store.save_delegation_work_item(item)
        task = Task(
            id="task-delivery",
            title="CEO Delivery",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "execution_mode": "company_mode",
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                    "feedback_scope": "final",
                    "delivery_package": {"executive_summary": "old package"},
                    "final_delivery_package": {"executive_summary": "old final package"},
                    "delivery_revision": 2,
                },
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
            context_snapshot={
                "delivery_package": {"executive_summary": "old package"},
                "final_delivery_package": {"executive_summary": "old final package"},
                "work_item_owned_outputs": {
                    "delivery_package": {"executive_summary": "old package"},
                    "final_delivery_package": {"executive_summary": "old final package"},
                },
                "owner_directive_revision": 2,
            },
        )
        set_linked_work_item_id(task, "wi-delivery")
        await store.save_task(task)
        await store.link_work_item_runtime_task("wi-delivery", task.id)

        await engine._prepare_company_followup_target(task, "继续做一版 PPT")

        refreshed_task = await store.get_task(task.id)
        refreshed_item = await store.get_delegation_work_item("wi-delivery")
        assert refreshed_task is not None
        assert refreshed_item is not None
        self.assertEqual(refreshed_task.status, TaskStatus.PENDING)
        self.assertEqual(refreshed_task.metadata.get("work_item_turn_type"), "deliver")
        self.assertEqual(refreshed_task.metadata.get("work_kind"), "delivery")
        self.assertEqual(refreshed_task.metadata.get("delegation_turn_kind"), "delivery")
        self.assertEqual(refreshed_task.metadata.get("current_turn_mode"), "dispatch_required")
        self.assertEqual(refreshed_task.metadata.get("feedback_scope"), "final")
        self.assertTrue(refreshed_task.metadata.get("requires_user_feedback"))
        self.assertEqual(refreshed_task.metadata.get("delivery_revision"), 3)
        self.assertEqual(refreshed_task.metadata.get("owner_directive_revision"), 3)
        self.assertEqual(refreshed_task.metadata.get("latest_user_directive"), "继续做一版 PPT")
        self.assertNotIn("delivery_package", refreshed_task.metadata)
        self.assertNotIn("final_delivery_package", refreshed_task.metadata)
        self.assertNotIn("delivery_package", refreshed_task.context_snapshot)
        self.assertNotIn("final_delivery_package", refreshed_task.context_snapshot)
        self.assertNotIn("work_item_owned_outputs", refreshed_task.context_snapshot)
        self.assertEqual(refreshed_task.context_snapshot.get("delivery_revision"), 3)
        self.assertEqual(refreshed_task.context_snapshot.get("owner_directive_revision"), 3)
        self.assertEqual(refreshed_task.context_snapshot.get("latest_user_directive"), "继续做一版 PPT")
        self.assertEqual(refreshed_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(refreshed_item.metadata.get("work_item_turn_type"), "deliver")
        self.assertEqual(refreshed_item.metadata.get("work_kind"), "delivery")
        self.assertEqual(refreshed_item.metadata.get("delegation_turn_kind"), "delivery")
        self.assertEqual(refreshed_item.metadata.get("current_turn_mode"), "dispatch_required")
        self.assertEqual(refreshed_item.metadata.get("delivery_revision"), 3)
        self.assertEqual(refreshed_item.metadata.get("owner_directive_revision"), 3)
        self.assertNotIn("delivery_package", refreshed_item.metadata)
        self.assertNotIn("final_delivery_package", refreshed_item.metadata)

    async def test_followup_restores_missing_delivery_review_checkpoint(self) -> None:
        store = await self._store()
        engine = self._engine(store)
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "final_decider_role_id": "ceo",
                "top_level_role_ids": ["ceo"],
            },
        )
        plan_payload = serialize_company_work_item_runtime_plan(plan)
        intake_item = DelegationWorkItem(
            work_item_id="wi-intake",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Intake",
            kind="intake",
            projection_id="ceo-intake",
            phase=Phase.APPROVED,
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "runtime_model": "multi_team_org"},
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        delivery_item = DelegationWorkItem(
            work_item_id="wi-delivery",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Delivery",
            kind="delivery",
            projection_id="ceo-delivery",
            phase=Phase.AWAITING_HUMAN,
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "execution_mode": "company_mode",
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                    "feedback_scope": "final",
                },
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
        )
        await store.save_delegation_work_item(intake_item)
        await store.save_delegation_work_item(delivery_item)
        intake_task = Task(
            id="task-intake",
            title="CEO Intake",
            session_id="sess-parent",
            parent_session_id="sess-parent",
            assigned_to="ceo",
            status=TaskStatus.DONE,
            project_id="proj1",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "company_work_item_plan": plan_payload,
                },
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        delivery_task = Task(
            id="task-delivery",
            title="CEO Delivery",
            session_id="sess-parent:delivery",
            parent_session_id="sess-parent",
            assigned_to="ceo",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "execution_mode": "company_mode",
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                    "feedback_scope": "final",
                    "company_work_item_plan": plan_payload,
                },
                projection_id="ceo-delivery",
                turn_type="deliver",
            ),
        )
        set_linked_work_item_id(intake_task, "wi-intake")
        set_linked_work_item_id(delivery_task, "wi-delivery")
        await store.save_task(intake_task)
        await store.save_task(delivery_task)
        await store.link_work_item_runtime_task("wi-intake", intake_task.id)
        await store.link_work_item_runtime_task("wi-delivery", delivery_task.id)
        await store.save_execution_checkpoint(
            ExecutionCheckpoint(
                checkpoint_id="old-feedback",
                project_id="proj1",
                session_id=delivery_task.session_id,
                checkpoint_type="company_delivery_feedback",
                status="resolved",
                task_id=delivery_task.id,
                payload={"waiting_task_id": delivery_task.id},
            )
        )

        class DummyExecutor:
            async def execute(self, runtime_plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                _ = runtime_plan
                selected = await store.get_task("task-delivery")
                assert selected is not None
                selected.status = TaskStatus.AWAITING_HUMAN
                await store.save_task(selected)
                return "runtime resumed"

        engine.company_executor = DummyExecutor()

        response = await engine._maybe_resume_existing_company_runtime(
            "继续做一版 PPT",
            "sess-parent:delivery",
        )

        self.assertEqual(response, "runtime resumed")
        refreshed_intake = await store.get_task("task-intake")
        refreshed_delivery = await store.get_task("task-delivery")
        assert refreshed_intake is not None
        assert refreshed_delivery is not None
        self.assertEqual(refreshed_intake.status, TaskStatus.DONE)
        self.assertEqual(refreshed_delivery.metadata.get("latest_user_directive"), "继续做一版 PPT")
        pending = await store.get_pending_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_delivery_feedback"],
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].task_id, "task-delivery")

    async def test_followup_target_reopens_approved_manager_work_item_for_rework(self) -> None:
        store = await self._store()
        engine = self._engine(store)
        item = DelegationWorkItem(
            work_item_id="wi-intake",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Intake",
            kind="intake",
            projection_id="ceo-intake",
            phase=Phase.APPROVED,
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "runtime_model": "multi_team_org"},
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        await store.save_delegation_work_item(item)
        await store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id="role-ceo",
                run_id="run-1",
                project_id="proj1",
                role_id="ceo",
                status="running",
                focused_work_item_id="wi-intake",
            )
        )
        task = Task(
            id="task-intake",
            title="CEO Intake",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "delegation_role_session_id": "role-ceo"},
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        set_linked_work_item_id(task, "wi-intake")
        await store.save_task(task)
        await store.link_work_item_runtime_task("wi-intake", task.id)

        await engine._prepare_company_followup_target(task, "change the plan")

        refreshed_task = await store.get_task("task-intake")
        refreshed_item = await store.get_delegation_work_item("wi-intake")
        refreshed_role_session = await store.get_delegation_role_session("role-ceo")
        assert refreshed_task is not None
        assert refreshed_item is not None
        assert refreshed_role_session is not None
        self.assertEqual(refreshed_task.status, TaskStatus.PENDING)
        self.assertEqual(refreshed_task.context_snapshot["user_supplied_input"], "change the plan")
        self.assertEqual(refreshed_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(refreshed_item.metadata.get("resume_source"), "primary_session_followup")
        self.assertEqual(refreshed_item.metadata.get("current_turn_mode"), "dispatch_required")
        self.assertTrue(refreshed_item.metadata.get("followup_routed_to_final_decider"))
        self.assertEqual(refreshed_role_session.status, "idle")
        self.assertEqual(refreshed_role_session.focused_work_item_id, "")

    async def test_final_decider_followup_is_runnable_despite_unapproved_child_dependency(self) -> None:
        store = await self._store()
        engine = self._engine(store)
        ceo_item = DelegationWorkItem(
            work_item_id="wi-ceo",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Intake",
            kind="intake",
            projection_id="ceo-intake",
            phase=Phase.READY,
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "dependency_work_item_ids": ["wi-child"],
                },
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        child_item = DelegationWorkItem(
            work_item_id="wi-child",
            run_id="run-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Old child",
            kind="execute",
            projection_id="child-execute",
            phase=Phase.RUNNING,
            metadata=mark_work_item_projection(
                {"work_item_runtime": True, "runtime_model": "multi_team_org"},
                projection_id="child-execute",
                turn_type="execute",
            ),
        )
        await store.save_delegation_work_item(ceo_item)
        await store.save_delegation_work_item(child_item)
        task = Task(
            id="task-ceo",
            title="CEO Intake",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "delegation_role_session_id": "role-ceo",
                },
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        set_linked_work_item_id(task, "wi-ceo")
        await store.save_task(task)
        await store.link_work_item_runtime_task("wi-ceo", task.id)

        self.assertFalse(
            CompanyWorkItemExecutor._work_item_is_runnable(
                ceo_item,
                {"wi-ceo": ceo_item, "wi-child": child_item},
                task_by_work_item_id={"wi-ceo": task},
            )
        )

        await engine._prepare_company_followup_target(task, "change direction")
        refreshed_task = await store.get_task(task.id)
        refreshed_ceo_item = await store.get_delegation_work_item("wi-ceo")
        assert refreshed_task is not None
        assert refreshed_ceo_item is not None

        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(
                refreshed_ceo_item,
                {"wi-ceo": refreshed_ceo_item, "wi-child": child_item},
                task_by_work_item_id={"wi-ceo": refreshed_task},
            )
        )

        # The WorkItem is the source of truth for final-decider follow-up.
        # A projection refresh or stale Task row must not make the CEO item
        # wait on obsolete child dependencies before the CEO can arbitrate.
        refreshed_task.metadata = dict(refreshed_task.metadata or {})
        refreshed_task.metadata.pop("followup_routed_to_final_decider", None)
        refreshed_task.metadata.pop("current_turn_mode", None)
        await store.save_task(refreshed_task)
        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(
                refreshed_ceo_item,
                {"wi-ceo": refreshed_ceo_item, "wi-child": child_item},
                task_by_work_item_id={"wi-ceo": refreshed_task},
            )
        )

    async def test_final_decider_progress_requires_arbitration_not_claim_marker(self) -> None:
        store = await self._store()
        engine = self._engine(store)
        item = DelegationWorkItem(
            work_item_id="wi-ceo",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            title="CEO Intake",
            kind="intake",
            projection_id="ceo-intake",
            phase=Phase.READY_FOR_REWORK,
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "followup_routed_to_final_decider": True,
                    "claimed_task_id": "task-ceo",
                    "claimed_by_role_session_id": "role-ceo",
                },
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        task = Task(
            id="task-ceo",
            title="CEO Intake",
            status=TaskStatus.PENDING,
            project_id="proj1",
            assigned_to="ceo",
            metadata=mark_work_item_projection(
                {
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "delegation_run_id": "run-1",
                    "delegation_role_session_id": "role-ceo",
                },
                projection_id="ceo-intake",
                turn_type="intake",
            ),
        )
        set_linked_work_item_id(task, "wi-ceo")
        await store.save_delegation_work_item(item)
        await store.save_task(task)
        await store.link_work_item_runtime_task("wi-ceo", task.id)

        self.assertFalse(await engine._company_followup_target_progressed("task-ceo"))

        task.metadata = {
            **dict(task.metadata or {}),
            "manager_no_delegation_justification": "The existing board still matches the follow-up.",
        }
        await store.save_task(task)
        self.assertTrue(await engine._company_followup_target_progressed("task-ceo"))

        task.metadata.pop("manager_no_delegation_justification", None)
        task.metadata["manager_dispatch_guard_unresolved"] = (
            "Soft dispatch constraint exhausted; accept the top-seat output."
        )
        await store.save_task(task)
        self.assertTrue(await engine._company_followup_target_progressed("task-ceo"))

        task.metadata.pop("manager_dispatch_guard_unresolved", None)
        await store.save_task(task)
        await store.update_delegation_work_item(
            "wi-ceo",
            metadata_updates={"manager_board_mutation_performed": True},
        )
        # Dispatch outcome markers are attempt-scoped Task state. A stale
        # WorkItem copy must not make a later follow-up look progressed.
        self.assertFalse(await engine._company_followup_target_progressed("task-ceo"))

    async def test_final_decider_followup_keeps_dispatch_turn_mode_with_existing_children(self) -> None:
        runtime = CompanyRuntime(org_engine=None, communication=None, store=None)
        session = CompanyMemberSession(
            member_session_id="member-ceo",
            role_id="ceo",
            employee_id="employee-ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "managed_team_id": "team-ceo",
                "manager_board_summary": {"total_children": 1},
            },
        )
        normal_task = Task(
            id="task-normal",
            title="CEO Intake",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "current_turn_mode": "dispatch_required",
            },
        )
        followup_task = Task(
            id="task-followup",
            title="CEO Intake",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "current_turn_mode": "dispatch_required",
                "followup_routed_to_final_decider": True,
            },
            context_snapshot={"current_turn_mode": "dispatch_required"},
        )

        self.assertEqual(runtime._resolve_current_turn_mode(session, normal_task), "monitor_children")
        self.assertEqual(runtime._resolve_current_turn_mode(session, followup_task), "dispatch_required")

    def test_final_decider_followup_contract_requires_board_reconciliation(self) -> None:
        task = Task(
            id="task-followup",
            title="CEO Intake",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "current_turn_mode": "dispatch_required",
                "followup_routed_to_final_decider": True,
                "direct_report_seat_ids": ["seat::team::ceo::cto"],
                "allowed_delegate_role_ids": ["cto"],
            },
            context_snapshot={
                "user_supplied_input": "把太空船游戏改成水下潜艇探险，并废弃旧方向。",
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("User Follow-up Board Reconciliation", contract)
        self.assertIn("manager_board_read", contract)
        self.assertIn("resuming this same role session with a fresh owner directive", contract)
        self.assertIn("answer directly, close review when appropriate, inspect or revise the board", contract)
        self.assertIn("modify_work_item", contract)
        self.assertIn("delete_work_item", contract)
        self.assertNotIn("classify each existing child WorkItem", contract)
        self.assertNotIn("Delegating replacement work alone is incomplete", contract)

    def test_revised_manager_work_item_contract_requires_child_board_reconciliation(self) -> None:
        task = Task(
            id="task-revised-manager",
            title="CTO revised dispatch",
            project_id="proj1",
            assigned_to="cto",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "current_turn_mode": "dispatch_required",
                "direct_report_seat_ids": ["seat::team::cto::senior_engineer"],
                "allowed_delegate_role_ids": ["senior_engineer"],
                "manager_mutation_action": "modify",
                "manager_mutation_reason": "CEO revised the game direction after Stop.",
                "manager_mutation_user_input": "Replace the old cooking game with Neon Rails and delete obsolete work.",
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("Upstream Work Item Mutation Reconciliation", contract)
        self.assertIn("Replace the old cooking game with Neon Rails", contract)
        self.assertIn("manager_board_read", contract)
        self.assertIn("modify_work_item", contract)
        self.assertIn("delete_work_item", contract)
        self.assertIn("suspended/running children left over from before Stop", contract)

    async def test_continue_resets_stale_in_memory_runtime_sessions_before_execute(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        await store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id="role-runtime-1",
                run_id="run-1",
                project_id="proj1",
                role_id="executor",
                status="running",
                focused_work_item_id="work-item-1",
            )
        )
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        runtime = CompanyRuntime(org_engine=None, communication=None, store=store)
        runtime.member_sessions["member-1"] = CompanyMemberSession(
            member_session_id="member-1",
            role_session_id="role-runtime-1",
            role_id="executor",
            employee_id="employee-1",
            status="running",
            resident_status="running",
            current_task_id=task.id,
            focused_work_item_id="work-item-1",
        )
        runtime.role_sessions["role-runtime-1"] = DelegationRoleSession(
            role_session_id="role-runtime-1",
            run_id="run-1",
            project_id="proj1",
            role_id="executor",
            status="running",
            focused_work_item_id="work-item-1",
        )
        runtime._claimed_task_ids.add(task.id)
        runtime._claimed_work_item_ids.add("work-item-1")
        runtime.role_queues["executor"].append(f"work-item::work-item-1")
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            def __init__(self) -> None:
                self.runtime = runtime

            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                session = self.runtime.member_sessions["member-1"]
                role_session = self.runtime.role_sessions["role-runtime-1"]
                captured["member_status"] = session.status
                captured["member_focus"] = session.focused_work_item_id
                captured["role_status"] = role_session.status
                captured["role_focus"] = role_session.focused_work_item_id
                captured["claimed_tasks"] = set(self.runtime._claimed_task_ids)
                captured["claimed_work_items"] = set(self.runtime._claimed_work_item_ids)
                captured["queue"] = list(self.runtime.role_queues["executor"])
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        refreshed_role_session = await store.get_delegation_role_session("role-runtime-1")

        self.assertEqual(captured["member_status"], "idle")
        self.assertEqual(captured["member_focus"], "")
        self.assertEqual(captured["role_status"], "idle")
        self.assertEqual(captured["role_focus"], "")
        self.assertEqual(captured["claimed_tasks"], set())
        self.assertEqual(captured["claimed_work_items"], set())
        self.assertEqual(captured["queue"], [])
        assert refreshed_role_session is not None
        self.assertEqual(refreshed_role_session.status, "idle")
        self.assertEqual(refreshed_role_session.focused_work_item_id, "")

    async def test_continue_reconciles_running_work_item_with_unmet_dependency_to_waiting(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        dependency = DelegationWorkItem(
            work_item_id="dep-item",
            run_id="run-1",
            role_id="designer",
            seat_id="seat-2",
            title="Dependency",
            projection_id="dependency",
            phase=Phase.RUNNING,
            metadata={"runtime_model": "multi_team_org"},
        )
        await store.save_delegation_work_item(dependency)
        item = await store.get_delegation_work_item("work-item-1")
        assert item is not None
        item.metadata = {
            **dict(item.metadata or {}),
            "runtime_model": "multi_team_org",
            "dependency_work_item_ids": ["dep-item"],
            "dependency_classes": {"dep-item": "hard"},
        }
        await store.save_delegation_work_item(item)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        resumed_task = captured["tasks"][0]
        refreshed_item = await store.get_delegation_work_item("work-item-1")

        assert refreshed_item is not None
        self.assertEqual(refreshed_item.phase, Phase.WAITING_FOR_CHILDREN)
        self.assertEqual(refreshed_item.metadata.get("dispatch_hold"), "")
        self.assertEqual(resumed_task.status, TaskStatus.BLOCKED)

    async def test_continue_keeps_settlement_released_triage_card_running(self) -> None:
        """Stop/Resume must not re-lock a failure-triage card the frontier
        pass already released over a FAILED dependency: the failed dep is
        terminal, so after a regression no event would ever wake it again."""
        store = await self._store()
        _, task = await self._seed_runtime(store)
        dependency = DelegationWorkItem(
            work_item_id="dep-item",
            run_id="run-1",
            role_id="designer",
            seat_id="seat-2",
            title="Dependency",
            projection_id="dependency",
            phase=Phase.FAILED,
            metadata={"runtime_model": "multi_team_org"},
        )
        await store.save_delegation_work_item(dependency)
        item = await store.get_delegation_work_item("work-item-1")
        assert item is not None
        item.metadata = {
            **dict(item.metadata or {}),
            "runtime_model": "multi_team_org",
            "dependency_work_item_ids": ["dep-item"],
            "dependency_classes": {"dep-item": "hard"},
            "dependency_settlement": {
                "failed": ["dep-item"],
                "cancelled": [],
                "stuck": [],
                "settled_at": "2026-07-13T00:00:00",
            },
        }
        await store.save_delegation_work_item(item)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["tasks"] = tasks
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

    def test_resume_dependency_check_honors_settlement_for_soft_stuck_deps(self) -> None:
        """The soft-class branch must honor the settlement stamp too: a
        soft dep parked in WAITING_DEPENDENCIES (not in-progress, not
        terminal) that the frontier stamped as stuck must not re-lock the
        released card on resume."""
        stuck_dep = DelegationWorkItem(
            work_item_id="soft-dep",
            run_id="run-soft",
            role_id="w",
            seat_id="seat-w",
            title="Stuck soft dep",
            projection_id="soft-dep",
            phase=Phase.WAITING_DEPENDENCIES,
        )
        card = DelegationWorkItem(
            work_item_id="soft-card",
            run_id="run-soft",
            role_id="m",
            seat_id="seat-m",
            title="Released triage card",
            projection_id="soft-card",
            phase=Phase.RUNNING,
            metadata={
                "dependency_work_item_ids": ["soft-dep"],
                "dependency_classes": {"soft-dep": "soft"},
                "dependency_settlement": {
                    "failed": [],
                    "cancelled": [],
                    "stuck": ["soft-dep"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        by_id = {"soft-dep": stuck_dep, "soft-card": card}
        self.assertTrue(
            OPCEngine._company_runtime_dependencies_satisfied(card, by_id)
        )
        card.metadata = {
            **dict(card.metadata or {}),
            "dependency_settlement": {},
        }
        self.assertFalse(
            OPCEngine._company_runtime_dependencies_satisfied(card, by_id)
        )

    async def test_continue_does_not_use_synthetic_external_session_id_as_provider_token(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(
            store,
            external_session_id="codex:proj1:execution-task",
            external_resume_session_id="",
            external_provider_session_id="",
        )
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        resumed_task = captured["tasks"][0]

        self.assertEqual(resumed_task.assigned_external_agent, "codex")
        self.assertEqual(
            resumed_task.metadata["selected_execution_agent"],
            "codex",
        )
        self.assertNotIn("external_resume_session_id", resumed_task.metadata)
        self.assertEqual(resumed_task.metadata["external_resume_fallback"], "context_replay")

    async def test_company_runtime_checkpoint_stays_resuming_during_long_execute(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                resuming = await store.get_execution_checkpoints(
                    project_id="proj1",
                    session_id="sess-parent",
                    checkpoint_types=["company_runtime_suspended"],
                    statuses=["resuming"],
                )
                resolved = await store.get_execution_checkpoints(
                    project_id="proj1",
                    session_id="sess-parent",
                    checkpoint_types=["company_runtime_suspended"],
                    statuses=["resolved"],
                )
                captured["resuming_count_during_execute"] = len(resuming)
                captured["resolved_count_during_execute"] = len(resolved)
                captured["resume_state_during_execute"] = resuming[0].payload.get("resume_state") if resuming else ""
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        resolved = await store.get_execution_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )

        self.assertEqual(captured["resuming_count_during_execute"], 1)
        self.assertEqual(captured["resolved_count_during_execute"], 0)
        self.assertEqual(captured["resume_state_during_execute"], "resuming")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].payload.get("resume_state"), "handoff_complete")

    async def test_company_runtime_checkpoint_returns_pending_if_resume_handoff_fails(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )

        class BrokenRuntime:
            async def reset_for_company_runtime_resume(
                self,
                tasks: list[Task],
                *,
                payload: dict[str, Any] | None = None,
            ) -> None:
                raise RuntimeError("reset failed")

        class BrokenCompanyExecutor:
            runtime = BrokenRuntime()

        engine.company_executor = BrokenCompanyExecutor()

        with self.assertRaises(RuntimeError):
            await engine._maybe_resume_checkpoint(
                "continue",
                "sess-parent",
                reply_metadata={"ui_force_resume": True},
            )
        pending = await store.get_execution_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        resuming = await store.get_execution_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resuming"],
        )
        refreshed_task = await store.get_task(task.id)
        refreshed_item = await store.get_delegation_work_item("work-item-1")

        self.assertEqual(len(pending), 1)
        self.assertEqual(resuming, [])
        self.assertEqual(pending[0].payload.get("resume_state"), "failed_before_handoff")
        assert refreshed_task is not None
        assert refreshed_item is not None
        self.assertEqual(
            refreshed_task.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(
            refreshed_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(refreshed_item.claimed_by_role_runtime_session_id, "")
        self.assertEqual(refreshed_item.claimed_by_seat_id, "")

    async def test_suspend_is_parent_session_idempotent(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)

        first = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
            stop_intent_id="intent-1",
        )
        second = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
            stop_intent_id="intent-2",
        )
        checkpoints = await store.get_pending_checkpoints(
            project_id="proj1",
            session_id="sess-parent",
            checkpoint_types=["company_runtime_suspended"],
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first["checkpoint_id"], second["checkpoint_id"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(checkpoints), 1)

    async def test_continue_does_not_reuse_failed_external_session_token(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store, external_status="failed")
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="startup_recovery",
            checkpoint_type="company_runtime_interrupted",
        )
        captured: dict[str, Any] = {}

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                captured["tasks"] = tasks
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        resumed_task = captured["tasks"][0]

        self.assertNotIn("external_resume_session_id", resumed_task.metadata)
        self.assertEqual(resumed_task.metadata["external_resume_fallback"], "context_replay")

    async def test_child_human_checkpoint_takes_priority_over_parent_suspend_checkpoint(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
        )
        child_checkpoint = ExecutionCheckpoint(
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="task_user_input",
            payload={"prompt": "Need human input.", "task_id": task.id},
        )
        await store.save_execution_checkpoint(child_checkpoint)

        selected = await engine.get_latest_pending_checkpoint_for_session("sess-parent")

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.checkpoint_type, "task_user_input")
        self.assertEqual(selected.checkpoint_id, child_checkpoint.checkpoint_id)

    async def test_explicit_task_user_input_reply_resumes_selected_checkpoint_only(self) -> None:
        store = await self._store()
        plan, task = await self._seed_runtime(store)
        sibling = Task(
            id="execution-task-2",
            title="Second execution",
            session_id="sess-child-2",
            parent_session_id="sess-parent",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            metadata={
                "execution_mode": "company_mode",
                "work_item_runtime": True,
                "work_item_projection_id": "execution-2",
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
            },
        )
        await store.save_task(sibling)
        first_checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-first",
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="task_user_input",
            payload={
                "task_id": task.id,
                "session_id": task.session_id,
                "execution_mode": "company_mode",
                "task_ids": [task.id],
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "pause_request": {"reason": "Need first input"},
            },
        )
        second_checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-second",
            project_id="proj1",
            session_id=sibling.session_id,
            task_id=sibling.id,
            checkpoint_type="task_user_input",
            payload={
                "task_id": sibling.id,
                "session_id": sibling.session_id,
                "execution_mode": "company_mode",
                "task_ids": [sibling.id],
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "pause_request": {"reason": "Need second input"},
            },
        )
        await store.save_execution_checkpoint(first_checkpoint)
        await store.save_execution_checkpoint(second_checkpoint)
        engine = self._engine(store)
        executor = self._CapturingCompanyExecutor()
        engine.company_executor = executor  # type: ignore[assignment]

        result = await engine._maybe_resume_checkpoint(
            "Use the first answer",
            "sess-parent",
            reply_metadata={
                "response_to_checkpoint_id": "cp-first",
                "response_to_checkpoint_type": "task_user_input",
            },
        )

        self.assertEqual(result, "runtime resumed")
        refreshed_first = await store.get_task(task.id)
        refreshed_second = await store.get_task(sibling.id)
        assert refreshed_first is not None
        assert refreshed_second is not None
        self.assertEqual(refreshed_first.context_snapshot.get("user_supplied_input"), "Use the first answer")
        self.assertNotEqual(refreshed_second.context_snapshot.get("user_supplied_input"), "Use the first answer")
        checkpoints = await store.get_execution_checkpoints("proj1")
        statuses = {item.checkpoint_id: item.status for item in checkpoints}
        self.assertEqual(statuses["cp-first"], "resolved")
        self.assertEqual(statuses["cp-second"], "pending")
        self.assertEqual(executor.calls[0][1][0].id, task.id)

    async def test_explicit_child_checkpoint_reply_from_parent_session_resumes_child(self) -> None:
        store = await self._store()
        plan, task = await self._seed_runtime(store)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-child-input",
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="task_user_input",
            payload={
                "task_id": task.id,
                "session_id": task.session_id,
                "execution_mode": "company_mode",
                "task_ids": [task.id],
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "pause_request": {"reason": "Need child input"},
            },
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = self._engine(store)
        engine.company_executor = self._CapturingCompanyExecutor()  # type: ignore[assignment]

        result = await engine._maybe_resume_checkpoint(
            "Parent replied to child",
            "sess-parent",
            reply_metadata={
                "response_to_checkpoint_id": "cp-child-input",
                "response_to_checkpoint_type": "task_user_input",
            },
        )

        self.assertEqual(result, "runtime resumed")
        refreshed = await store.get_task(task.id)
        assert refreshed is not None
        self.assertEqual(refreshed.context_snapshot.get("user_supplied_input"), "Parent replied to child")

    async def test_explicit_resolved_checkpoint_reply_returns_inactive(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-old",
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="task_user_input",
            status="resolved",
            payload={"task_id": task.id, "session_id": task.session_id},
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = self._engine(store)
        engine.company_executor = self._CapturingCompanyExecutor()  # type: ignore[assignment]

        result = await engine._maybe_resume_checkpoint(
            "late reply",
            "sess-parent",
            reply_metadata={
                "response_to_checkpoint_id": "cp-old",
                "response_to_checkpoint_type": "task_user_input",
            },
        )

        self.assertEqual(result, "This request is no longer active.")

    async def test_explicit_resolved_delivery_feedback_reply_allows_runtime_followup(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-old-delivery",
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="company_delivery_feedback",
            status="resolved",
            payload={"task_id": task.id, "session_id": task.session_id},
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = self._engine(store)
        engine.company_executor = self._CapturingCompanyExecutor()  # type: ignore[assignment]

        result = await engine._maybe_resume_checkpoint(
            "late delivery follow-up",
            "sess-parent",
            reply_metadata={
                "response_to_checkpoint_id": "cp-old-delivery",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
        )

        self.assertIsNone(result)

    async def test_invalid_delivery_feedback_keeps_checkpoint_pending(self) -> None:
        store = await self._store()
        plan, task = await self._seed_runtime(store)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery",
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            checkpoint_type="company_delivery_feedback",
            payload={
                "waiting_task_id": task.id,
                "task_ids": [task.id],
                "feedback_scope": "final",
                "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                "prompt": "Send your next review instruction.",
            },
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = self._engine(store)
        engine.company_executor = self._CapturingCompanyExecutor()  # type: ignore[assignment]
        engine.memory = object()  # type: ignore[assignment]

        result = await engine._maybe_resume_checkpoint(
            "   ",
            task.session_id,
            reply_metadata={
                "response_to_checkpoint_id": "cp-delivery",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
        )

        self.assertIn("pending delivery self-evolution review", result or "")
        checkpoints = await store.get_execution_checkpoints("proj1")
        statuses = {item.checkpoint_id: item.status for item in checkpoints}
        self.assertEqual(statuses["cp-delivery"], "pending")

    async def test_continue_clears_parent_runtime_stop_marker(self) -> None:
        store = await self._store()
        _, task = await self._seed_runtime(store)
        parent = await store.get_task("ui-anchor-sess-parent")
        assert parent is not None
        parent.status = TaskStatus.RUNNING
        parent.metadata = {
            **dict(parent.metadata or {}),
            "company_runtime_stop_state": "suspended",
            "company_runtime_stop_intent_id": "intent-1",
            "company_runtime_stop_marked_at": "2026-04-29T11:02:40",
            "company_runtime_suspended_at": "2026-04-29T11:02:40",
        }
        await store.save_task(parent)
        engine = self._engine(store)
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="sess-parent",
            reason="user_stop",
            stop_intent_id="intent-1",
        )

        class DummyCompanyExecutor:
            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                return "runtime resumed"

        engine.company_executor = DummyCompanyExecutor()

        await engine._maybe_resume_checkpoint(
            "continue",
            "sess-parent",
            reply_metadata={"ui_force_resume": True},
        )
        refreshed_parent = await store.get_task(parent.id)

        assert refreshed_parent is not None
        self.assertNotIn("company_runtime_stop_state", refreshed_parent.metadata)
        self.assertNotIn("company_runtime_stop_marked_at", refreshed_parent.metadata)
        self.assertTrue(refreshed_parent.metadata.get("company_runtime_resume_checkpoint_id"))
        self.assertTrue(refreshed_parent.metadata.get("company_runtime_resume_requested_at"))


if __name__ == "__main__":
    unittest.main()
