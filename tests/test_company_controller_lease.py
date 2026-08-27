from __future__ import annotations

import asyncio
import copy
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from opc.core.company_controller import (
    CompanyControllerAttemptSuperseded,
    CompanyRunControllerLeaseLost,
)
from opc.core.models import (
    CompanyMemberSession,
    DelegationRun,
    DelegationWorkItem,
    Phase,
    RoleRuntimeSession,
    SeatState,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_transition import (
    transition_work_item_from_task,
)


class CompanyRunControllerLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.store1 = OPCStore(self.db_path)
        self.store2 = OPCStore(self.db_path)
        await self.store1.initialize()
        await self.store2.initialize(run_startup_maintenance=False)

    async def asyncTearDown(self) -> None:
        await self.store2.close()
        await self.store1.close()
        self._tmp.cleanup()

    async def _seed_run(self, run_id: str, root_session_id: str) -> None:
        await self.store1.save_delegation_run(
            DelegationRun(
                run_id=run_id,
                project_id="project-1",
                session_id=root_session_id,
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )

    async def _seed_claimed_attempt(
        self,
        *,
        owner_token: str,
        run_id: str = "run-1",
        root_session_id: str = "root-1",
    ) -> tuple[Task, int]:
        await self._seed_run(run_id, root_session_id)
        item = DelegationWorkItem(
            work_item_id="wi-1",
            run_id=run_id,
            role_id="executor",
            seat_id="seat-1",
            title="Execution",
            summary="Produce a result.",
            projection_id="execution",
            phase=Phase.READY,
        )
        await self.store1.save_delegation_work_item(item)
        task = Task(
            id="task-1",
            session_id=root_session_id,
            project_id="project-1",
            title="Execution",
            assigned_to="executor",
            metadata={"delegation_run_id": run_id},
        )
        await self.store1.save_task(task)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task("wi-1", task.id)
        )
        set_linked_work_item_id(task, "wi-1")
        lease = await self.store1.acquire_delegation_run_controller_lease(
            run_id,
            project_id="project-1",
            root_session_id=root_session_id,
            owner_token=owner_token,
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        claimed = await self.store1.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session-1",
            seat_id="seat-1",
            task_id=task.id,
            controller_owner_token=owner_token,
            controller_lease_generation=lease.generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        attempt_seq = int(claimed.metadata.get("attempt_seq", 0) or 0)
        task.metadata.update(
            {
                "company_run_controller_owner_token": owner_token,
                "company_run_controller_lease_generation": lease.generation,
                "claimed_work_item_attempt_seq": attempt_seq,
            }
        )
        task.status = TaskStatus.RUNNING
        await self.store1.save_task(task)
        return task, lease.generation

    async def _expire_lease(
        self,
        store: OPCStore,
        *,
        owner_token: str,
        generation: int,
        remaining_seconds: float = -1.0,
    ) -> None:
        # Renewal accepts an explicit heartbeat timestamp.  Moving that
        # timestamp into the past deterministically expires a test lease
        # without reaching around the Store's CAS API.
        heartbeat_at = datetime.now() - timedelta(
            seconds=max(0.0, 1.0 - remaining_seconds)
        )
        renewed = await store.renew_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token=owner_token,
            generation=generation,
            lease_seconds=1,
            heartbeat_at=heartbeat_at,
        )
        self.assertTrue(renewed)

    async def _claim_next_attempt_same_controller(
        self,
        task: Task,
        *,
        owner_token: str,
        generation: int,
    ) -> DelegationWorkItem:
        reopened = await self.store1.update_delegation_work_item(
            "wi-1",
            phase=Phase.READY,
        )
        self.assertIsNotNone(reopened)
        claimed = await self.store1.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session-1",
            seat_id="seat-1",
            task_id=task.id,
            controller_owner_token=owner_token,
            controller_lease_generation=generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertGreater(
            int(claimed.metadata.get("attempt_seq", 0) or 0),
            int(task.metadata.get("claimed_work_item_attempt_seq", 0) or 0),
        )
        return claimed

    async def _seed_waiting_parent(
        self,
        *,
        run_id: str = "run-1",
        root_session_id: str = "root-1",
        dependency_work_item_id: str = "wi-1",
    ) -> Task:
        parent = DelegationWorkItem(
            work_item_id="wi-parent",
            run_id=run_id,
            role_id="manager",
            seat_id="seat-manager",
            title="Integrate child",
            summary="Waiting for the child result.",
            projection_id="integration",
            phase=Phase.WAITING_DEPENDENCIES,
            metadata={
                "dependency_work_item_ids": [dependency_work_item_id],
                "waiting_on_work_item_ids": [dependency_work_item_id],
            },
        )
        await self.store1.save_delegation_work_item(parent)
        task = Task(
            id="task-parent",
            session_id=root_session_id,
            project_id="project-1",
            title="Integrate child",
            assigned_to="manager",
            status=TaskStatus.BLOCKED,
            metadata={"delegation_run_id": run_id},
        )
        await self.store1.save_task(task)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task(parent.work_item_id, task.id)
        )
        return task

    async def test_live_lease_is_run_scoped_busy_and_release_is_exact(self) -> None:
        await self._seed_run("run-1", "root-1")
        await self._seed_run("run-2", "root-2")
        first = await self.store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-a",
            lease_seconds=30,
        )
        self.assertTrue(first.acquired)
        busy = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=30,
        )
        self.assertEqual(busy.outcome, "busy")
        independent = await self.store2.acquire_delegation_run_controller_lease(
            "run-2",
            project_id="project-1",
            root_session_id="root-2",
            owner_token="owner-b",
            lease_seconds=30,
        )
        self.assertTrue(independent.acquired)
        self.assertFalse(
            await self.store2.release_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                generation=first.generation,
            )
        )
        self.assertTrue(
            await self.store1.release_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=first.generation,
            )
        )
        acquired_after_release = (
            await self.store2.acquire_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                lease_seconds=30,
            )
        )
        self.assertTrue(acquired_after_release.acquired)
        self.assertGreater(acquired_after_release.generation, first.generation)

    async def test_real_run_claim_cannot_omit_controller_credential(self) -> None:
        await self._seed_run("run-1", "root-1")
        await self.store1.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="wi-1",
                run_id="run-1",
                role_id="executor",
                seat_id="seat-1",
                title="Execution",
                phase=Phase.READY,
            )
        )
        claimed = await self.store1.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session-1",
            seat_id="seat-1",
            task_id="task-1",
        )
        self.assertIsNone(claimed)
        untouched = await self.store1.get_delegation_work_item("wi-1")
        assert untouched is not None
        self.assertEqual(untouched.phase, Phase.READY)
        self.assertEqual(untouched.claimed_by_role_runtime_session_id, "")
        self.assertNotIn("attempt_seq", untouched.metadata)

    async def test_production_admission_fails_closed_without_durable_run(self) -> None:
        project_dir = Path(self._tmp.name) / "projects" / "project-1"
        project_dir.mkdir(parents=True)
        production_store = OPCStore(project_dir / "tasks.db")
        await production_store.initialize()
        try:
            executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
            executor.store = production_store
            executor._active_controller_leases = {}
            for metadata, expected in (
                (
                    {"execution_model": "multi_team_org"},
                    "run_id=<missing>",
                ),
                (
                    {
                        "execution_model": "multi_team_org",
                        "delegation_run_id": "missing-run",
                    },
                    "missing its DelegationRun row",
                ),
            ):
                with self.subTest(metadata=metadata):
                    task = Task(
                        id="task-missing-run",
                        session_id="role-session",
                        parent_session_id="root-1",
                        project_id="project-1",
                        title="Execution",
                        metadata=metadata,
                    )
                    with self.assertRaisesRegex(RuntimeError, expected):
                        await executor.acquire_controller_admission([task])
                    self.assertEqual(executor._active_controller_leases, {})
        finally:
            await production_store.close()

    async def test_scope_admission_setup_failure_releases_exact_lease(self) -> None:
        await self._seed_run("run-admission-fault", "root-admission-fault")
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store1
        executor._active_controller_leases = {}

        with patch.object(
            self.store1,
            "settle_stale_delegation_run_claims_for_controller",
            AsyncMock(side_effect=RuntimeError("simulated settlement fault")),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated settlement fault"):
                await executor.acquire_controller_admission_for_scope(
                    run_id="run-admission-fault",
                    project_id="project-1",
                    root_session_id="root-admission-fault",
                )

        durable_run = await self.store2.get_delegation_run(
            "run-admission-fault"
        )
        assert durable_run is not None
        self.assertEqual(durable_run.controller_owner_token, "")
        self.assertIsNone(durable_run.controller_lease_expires_at)
        self.assertEqual(executor._active_controller_leases, {})

    async def test_expired_takeover_fences_old_phase_task_and_result(self) -> None:
        stale_task, generation1 = await self._seed_claimed_attempt(
            owner_token="owner-a"
        )
        stale_task.result = {"owner": "generation-1"}
        await self._expire_lease(
            self.store1,
            owner_token="owner-a",
            generation=generation1,
        )
        takeover = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertGreater(takeover.generation, generation1)
        self.assertEqual(
            await self.store2.settle_stale_delegation_run_claims_for_controller(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                generation=takeover.generation,
            ),
            1,
        )
        claimed2 = await self.store2.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.RUNNING,
            role_runtime_session_id="role-session-2",
            seat_id="seat-1",
            task_id="task-1",
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        assert claimed2 is not None
        generation2_task = await self.store2.get_task("task-1")
        assert generation2_task is not None
        generation2_task.metadata.update(
            {
                "company_run_controller_owner_token": "owner-b",
                "company_run_controller_lease_generation": takeover.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed2.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        generation2_task.result = {"owner": "generation-2"}
        await transition_work_item_from_task(
            self.store2,
            generation2_task,
            target_status_or_phase=Phase.AWAITING_MANAGER_REVIEW,
            reason="generation_2_result",
            summary="generation two won",
            require_work_item=True,
        )

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await transition_work_item_from_task(
                self.store1,
                stale_task,
                target_status_or_phase=Phase.FAILED,
                reason="late_generation_1_failure",
                summary="must not land",
                require_work_item=True,
            )
        stale_task.status = TaskStatus.FAILED
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(stale_task)

        final_item = await self.store2.get_delegation_work_item("wi-1")
        final_task = await self.store2.get_task("task-1")
        assert final_item is not None and final_task is not None
        self.assertEqual(final_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(final_item.summary, "generation two won")
        self.assertEqual(int(final_item.metadata.get("attempt_seq", 0)), 2)
        self.assertTrue(bool(final_item.metadata.get("attempt_settled")))
        self.assertEqual(
            final_item.metadata.get("attempt_outcome"),
            Phase.AWAITING_MANAGER_REVIEW.value,
        )
        self.assertEqual(final_task.status, TaskStatus.AWAITING_MANAGER_REVIEW)
        self.assertEqual(final_task.result, {"owner": "generation-2"})

    async def test_live_controller_types_old_attempt_task_save_as_superseded(
        self,
    ) -> None:
        stale_task, generation = await self._seed_claimed_attempt(
            owner_token="owner-a"
        )
        await self._claim_next_attempt_same_controller(
            stale_task,
            owner_token="owner-a",
            generation=generation,
        )
        persisted_before = await self.store1.get_task(stale_task.id)
        self.assertIsNotNone(persisted_before)

        stale_task.status = TaskStatus.FAILED
        stale_task.result = {"stale_attempt": True}
        with self.assertRaises(CompanyControllerAttemptSuperseded):
            await self.store1.save_task(stale_task)

        self.assertEqual(
            await self.store1.get_task(stale_task.id),
            persisted_before,
        )
        self.assertTrue(
            await self.store1.delegation_run_controller_lease_is_current(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=generation,
            )
        )

    async def test_attempt_supersession_classifier_fails_closed_on_bad_fences(
        self,
    ) -> None:
        stale_task, generation = await self._seed_claimed_attempt(
            owner_token="owner-a"
        )

        malformed = copy.deepcopy(stale_task)
        malformed.metadata["claimed_work_item_attempt_seq"] = 0
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(malformed)

        await self._claim_next_attempt_same_controller(
            stale_task,
            owner_token="owner-a",
            generation=generation,
        )
        drifted_link = copy.deepcopy(stale_task)
        set_linked_work_item_id(drifted_link, "wi-other")
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(drifted_link)

        await self.store1.update_delegation_work_item(
            "wi-1",
            metadata_updates={"claimed_task_id": ""},
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(stale_task)
        await self.store1.update_delegation_work_item(
            "wi-1",
            metadata_updates={"claimed_task_id": stale_task.id},
        )

        await self.store1.update_delegation_work_item(
            "wi-1",
            metadata_updates={"dispatch_hold": "company_runtime_suspended"},
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(stale_task)

        await self.store1.update_delegation_work_item(
            "wi-1",
            metadata_unset=["dispatch_hold"],
        )
        assert self.store1._db is not None
        await self.store1._db.execute(
            "DELETE FROM work_item_runtime_links WHERE runtime_task_id = ?",
            (stale_task.id,),
        )
        await self.store1._db.commit()
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(stale_task)

    async def test_dispatcher_harvest_keeps_sibling_for_superseded_attempt(
        self,
    ) -> None:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        handled: list[BaseException] = []

        async def handle_exception(
            _member_session: CompanyMemberSession,
            _task: Task,
            exc: BaseException,
        ) -> None:
            handled.append(exc)

        executor._handle_claimed_work_item_exception = handle_exception  # type: ignore[method-assign]
        member = CompanyMemberSession(
            member_session_id="member-a",
            role_id="executor",
            employee_id="employee-a",
        )
        sibling_member = CompanyMemberSession(
            member_session_id="member-b",
            role_id="qa",
            employee_id="employee-b",
        )
        stale_task = Task(id="task-a", title="Attempt A")
        sibling_task = Task(id="task-b", title="Sibling B")
        release_sibling = asyncio.Event()

        async def stale_tail() -> None:
            raise CompanyControllerAttemptSuperseded("attempt 1 < attempt 2")

        async def live_sibling() -> None:
            await release_sibling.wait()

        completed = asyncio.create_task(stale_tail())
        sibling = asyncio.create_task(live_sibling())
        cancelled = asyncio.create_task(live_sibling())
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        await asyncio.sleep(0)
        active = {
            completed: (member, stale_task),
            cancelled: (member, stale_task),
            sibling: (sibling_member, sibling_task),
        }
        try:
            await executor._harvest_completed_work_item_tasks(active)
            self.assertNotIn(completed, active)
            self.assertNotIn(cancelled, active)
            self.assertIn(sibling, active)
            self.assertFalse(sibling.cancelled())
            self.assertFalse(sibling.done())
            self.assertEqual(handled, [])
        finally:
            release_sibling.set()
            await sibling

    async def test_dispatcher_harvest_escalates_true_lease_loss(self) -> None:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        member = CompanyMemberSession(
            member_session_id="member-a",
            role_id="executor",
            employee_id="employee-a",
        )
        task = Task(id="task-a", title="Attempt A")

        async def stale_controller() -> None:
            raise CompanyRunControllerLeaseLost("generation was replaced")

        completed = asyncio.create_task(stale_controller())
        await asyncio.sleep(0)
        active = {completed: (member, task)}
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await executor._harvest_completed_work_item_tasks(active)

    async def test_takeover_fences_role_seat_and_runtime_session_tail(self) -> None:
        task, generation1 = await self._seed_claimed_attempt(owner_token="owner-a")
        controller_metadata = {
            "company_run_id": "run-1",
            "company_run_controller_owner_token": "owner-a",
            "company_run_controller_lease_generation": generation1,
        }
        stale_role = RoleRuntimeSession(
            role_session_id="role-session-1",
            run_id="run-1",
            project_id="project-1",
            team_instance_id="team-instance-1",
            team_id="team-1",
            role_id="executor",
            seat_id="seat-1",
            seat_state_id="seat-state-1",
            focused_work_item_id="wi-1",
            status="running",
            metadata=dict(controller_metadata),
        )
        stale_seat = SeatState(
            seat_state_id="seat-state-1",
            team_instance_id="team-instance-1",
            run_id="run-1",
            project_id="project-1",
            team_id="team-1",
            seat_id="seat-1",
            role_id="executor",
            role_runtime_session_id=stale_role.role_session_id,
            status="running",
            resident_status="running",
            current_task_id=task.id,
            current_work_item_id="wi-1",
            metadata=dict(controller_metadata),
        )
        await self.store1.save_delegation_role_session(
            stale_role,
            controller_owner_token="owner-a",
            controller_lease_generation=generation1,
        )
        await self.store1.save_seat_state(
            stale_seat,
            controller_owner_token="owner-a",
            controller_lease_generation=generation1,
        )
        await self.store1.save_runtime_session(
            runtime_session_id="member-session-1",
            project_id="project-1",
            session_id="role-session-1",
            task_id=task.id,
            status="running",
            metadata={
                **controller_metadata,
                "focused_work_item_id": "wi-1",
                "current_task_id": task.id,
                "status": "running",
                "resident_status": "running",
            },
            controller_run_id="run-1",
            controller_owner_token="owner-a",
            controller_lease_generation=generation1,
        )

        await self._expire_lease(
            self.store1,
            owner_token="owner-a",
            generation=generation1,
        )
        takeover = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertEqual(
            await self.store2.settle_stale_delegation_run_claims_for_controller(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                generation=takeover.generation,
            ),
            1,
        )

        winner_role = await self.store2.get_delegation_role_session(
            stale_role.role_session_id
        )
        winner_seat = await self.store2.get_seat_state(stale_seat.seat_state_id)
        winner_runtime = await self.store2.get_runtime_session("member-session-1")
        role_runtime_rows = await self.store2.list_role_runtime_sessions("run-1")
        delegation_role_rows = await self.store2.list_delegation_role_sessions(
            "run-1"
        )
        assert winner_role is not None and winner_seat is not None
        assert winner_runtime is not None
        self.assertEqual(len(role_runtime_rows), 1)
        self.assertEqual(len(delegation_role_rows), 1)
        self.assertEqual(role_runtime_rows[0], delegation_role_rows[0])
        self.assertEqual(winner_role.status, "idle")
        self.assertEqual(winner_role.focused_work_item_id, "")
        self.assertEqual(winner_seat.status, "idle")
        self.assertEqual(winner_seat.current_work_item_id, "")
        self.assertEqual(winner_runtime["status"], "idle")
        for projection_metadata in (
            winner_role.metadata,
            winner_seat.metadata,
            winner_runtime["metadata"],
        ):
            self.assertEqual(
                projection_metadata["company_run_controller_owner_token"],
                "owner-b",
            )
            self.assertEqual(
                int(
                    projection_metadata[
                        "company_run_controller_lease_generation"
                    ]
                ),
                takeover.generation,
            )

        stale_role.status = "running"
        stale_role.focused_work_item_id = "wi-1"
        stale_seat.status = "running"
        stale_seat.resident_status = "running"
        stale_seat.current_task_id = task.id
        stale_seat.current_work_item_id = "wi-1"
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_delegation_role_session(
                stale_role,
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        uncredentialed_role = RoleRuntimeSession(
            role_session_id=winner_role.role_session_id,
            run_id="run-1",
            project_id="project-1",
            role_id="executor",
            focused_work_item_id="wi-1",
            status="running",
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_delegation_role_session(uncredentialed_role)
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_seat_state(
                stale_seat,
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        uncredentialed_seat = SeatState(
            seat_state_id=winner_seat.seat_state_id,
            team_instance_id="team-instance-1",
            run_id="run-1",
            project_id="project-1",
            team_id="team-1",
            seat_id="seat-1",
            role_id="executor",
            status="running",
            current_work_item_id="wi-1",
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_seat_state(uncredentialed_seat)
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_runtime_session(
                runtime_session_id="member-session-1",
                project_id="project-1",
                session_id="role-session-1",
                task_id=task.id,
                status="running",
                metadata={
                    **controller_metadata,
                    "focused_work_item_id": "wi-1",
                },
                controller_run_id="run-1",
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_runtime_session(
                runtime_session_id="member-session-1",
                project_id="project-1",
                session_id="role-session-1",
                task_id=task.id,
                status="running",
                metadata={"focused_work_item_id": "wi-1"},
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.enqueue_pending_work_item(
                stale_role.role_session_id,
                "wi-stale-tail",
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.dequeue_pending_work_item(
                stale_role.role_session_id,
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.update_role_session_adapter_state(
                stale_role.role_session_id,
                "codex",
                {"resume_session_id": "stale-provider-session"},
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        winner_item_before_queue_marker = await self.store2.get_delegation_work_item(
            "wi-1"
        )
        assert winner_item_before_queue_marker is not None
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.update_work_item_serial_queue_marker_for_controller(
                "wi-1",
                run_id="run-1",
                project_id="project-1",
                queued_behind_session=stale_role.role_session_id,
                expected_queued_behind_session="",
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        self.assertEqual(
            await self.store2.get_delegation_work_item("wi-1"),
            winner_item_before_queue_marker,
        )
        self.assertEqual(
            await self.store2.get_delegation_role_session(stale_role.role_session_id),
            winner_role,
        )
        self.assertEqual(
            await self.store2.get_seat_state(stale_seat.seat_state_id),
            winner_seat,
        )
        self.assertEqual(
            await self.store2.get_runtime_session("member-session-1"),
            winner_runtime,
        )
        self.assertTrue(
            await self.store2.enqueue_pending_work_item(
                winner_role.role_session_id,
                "wi-generation-2-queued",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        )
        self.assertEqual(
            await self.store2.dequeue_pending_work_item(
                winner_role.role_session_id,
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            ),
            "wi-generation-2-queued",
        )
        self.assertTrue(
            await self.store2.update_role_session_adapter_state(
                winner_role.role_session_id,
                "codex",
                {"resume_session_id": "generation-2-provider-session"},
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        )
        self.assertTrue(
            await self.store2.update_work_item_serial_queue_marker_for_controller(
                "wi-1",
                run_id="run-1",
                project_id="project-1",
                queued_behind_session=winner_role.role_session_id,
                expected_queued_behind_session="",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        )
        queued_item = await self.store2.get_delegation_work_item("wi-1")
        assert queued_item is not None
        self.assertEqual(
            queued_item.metadata.get("queued_behind_session"),
            winner_role.role_session_id,
        )
        self.assertTrue(
            await self.store2.update_work_item_serial_queue_marker_for_controller(
                "wi-1",
                run_id="run-1",
                project_id="project-1",
                queued_behind_session=None,
                expected_queued_behind_session=winner_role.role_session_id,
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        )
        # Shutdown installs the durable run hold before it releases the
        # controller lease.  Even the still-current generation must be unable
        # to land a role/runtime/queue tail in that interval.
        await self.store2.update_delegation_work_item(
            "wi-1",
            metadata_updates={"dispatch_hold": "company_runtime_suspended"},
        )
        held_role = await self.store2.get_delegation_role_session(
            winner_role.role_session_id
        )
        held_seat = await self.store2.get_seat_state(winner_seat.seat_state_id)
        held_runtime = await self.store2.get_runtime_session("member-session-1")
        held_item = await self.store2.get_delegation_work_item("wi-1")
        assert held_role is not None and held_seat is not None
        assert held_runtime is not None and held_item is not None
        expected_held_role = copy.deepcopy(held_role)
        expected_held_seat = copy.deepcopy(held_seat)
        held_role.status = "running"
        held_role.focused_work_item_id = "wi-1"
        held_seat.status = "running"
        held_seat.current_work_item_id = "wi-1"
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store2.save_delegation_role_session(
                held_role,
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store2.save_seat_state(
                held_seat,
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store2.enqueue_pending_work_item(
                winner_role.role_session_id,
                "wi-1",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store2.update_work_item_serial_queue_marker_for_controller(
                "wi-1",
                run_id="run-1",
                project_id="project-1",
                queued_behind_session=winner_role.role_session_id,
                expected_queued_behind_session="",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store2.save_runtime_session(
                runtime_session_id="member-session-1",
                project_id="project-1",
                session_id="role-session-held-tail",
                task_id=task.id,
                status="running",
                metadata={"company_run_id": "run-1"},
                controller_run_id="run-1",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        self.assertEqual(
            await self.store2.get_delegation_role_session(
                winner_role.role_session_id
            ),
            expected_held_role,
        )
        self.assertEqual(
            await self.store2.get_seat_state(winner_seat.seat_state_id),
            expected_held_seat,
        )
        self.assertEqual(
            await self.store2.get_runtime_session("member-session-1"),
            held_runtime,
        )
        self.assertEqual(
            await self.store2.get_delegation_work_item("wi-1"),
            held_item,
        )
        await self.store2.update_delegation_work_item(
            "wi-1",
            metadata_unset=["dispatch_hold"],
        )
        winner_role = await self.store2.get_delegation_role_session(
            stale_role.role_session_id
        )
        assert winner_role is not None

        claimed2 = await self.store2.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.RUNNING,
            role_runtime_session_id=stale_role.role_session_id,
            seat_id="seat-1",
            task_id=task.id,
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        assert claimed2 is not None
        winner_role.status = "running"
        winner_role.focused_work_item_id = "wi-1"
        winner_role.metadata.update(
            {
                "company_run_controller_owner_token": "owner-b",
                "company_run_controller_lease_generation": takeover.generation,
            }
        )
        await self.store2.save_delegation_role_session(
            winner_role,
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        winner_seat.status = "running"
        winner_seat.resident_status = "running"
        winner_seat.current_task_id = task.id
        winner_seat.current_work_item_id = "wi-1"
        await self.store2.save_seat_state(
            winner_seat,
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        await self.store2.save_runtime_session(
            runtime_session_id="member-session-1",
            project_id="project-1",
            session_id="role-session-2",
            task_id=task.id,
            status="running",
            metadata={
                "company_run_id": "run-1",
                "company_run_controller_owner_token": "owner-b",
                "company_run_controller_lease_generation": takeover.generation,
                "focused_work_item_id": "wi-1",
            },
            controller_run_id="run-1",
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        task2 = await self.store2.get_task(task.id)
        assert task2 is not None
        task2.metadata.update(
            {
                "company_run_controller_owner_token": "owner-b",
                "company_run_controller_lease_generation": takeover.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed2.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await transition_work_item_from_task(
            self.store2,
            task2,
            target_status_or_phase=Phase.AWAITING_MANAGER_REVIEW,
            reason="generation_2_completed",
            require_work_item=True,
        )
        winner_role.status = "idle"
        winner_role.focused_work_item_id = ""
        await self.store2.save_delegation_role_session(
            winner_role,
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        winner_seat.status = "idle"
        winner_seat.resident_status = "idle"
        winner_seat.current_task_id = ""
        winner_seat.current_work_item_id = ""
        await self.store2.save_seat_state(
            winner_seat,
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        await self.store2.save_runtime_session(
            runtime_session_id="member-session-1",
            project_id="project-1",
            session_id="role-session-2",
            task_id=task.id,
            status="idle",
            metadata={
                "company_run_id": "run-1",
                "company_run_controller_owner_token": "owner-b",
                "company_run_controller_lease_generation": takeover.generation,
                "focused_work_item_id": "",
            },
            controller_run_id="run-1",
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        completed_role = await self.store2.get_delegation_role_session(
            winner_role.role_session_id
        )
        completed_item = await self.store2.get_delegation_work_item("wi-1")
        completed_seat = await self.store2.get_seat_state(winner_seat.seat_state_id)
        completed_runtime = await self.store2.get_runtime_session("member-session-1")
        completed_role_runtime_rows = await self.store2.list_role_runtime_sessions(
            "run-1"
        )
        completed_delegation_role_rows = (
            await self.store2.list_delegation_role_sessions("run-1")
        )
        assert completed_role is not None and completed_item is not None
        assert completed_seat is not None and completed_runtime is not None
        self.assertEqual(
            completed_role_runtime_rows,
            completed_delegation_role_rows,
        )
        self.assertEqual(completed_role.status, "idle")
        self.assertEqual(completed_role.focused_work_item_id, "")
        self.assertEqual(completed_seat.status, "idle")
        self.assertEqual(completed_seat.current_work_item_id, "")
        self.assertEqual(completed_runtime["status"], "idle")
        self.assertEqual(completed_item.phase, Phase.AWAITING_MANAGER_REVIEW)

    async def test_stale_resume_reset_leaves_controller_local_state_unchanged(
        self,
    ) -> None:
        task, generation1 = await self._seed_claimed_attempt(owner_token="owner-a")
        role_session_id = "role-runtime::run-1::executor"
        task.metadata = {
            **dict(task.metadata or {}),
            "delegation_role_session_id": role_session_id,
        }
        role_session = RoleRuntimeSession(
            role_session_id=role_session_id,
            run_id="run-1",
            project_id="project-1",
            role_id="executor",
            focused_work_item_id="wi-1",
            status="running",
            metadata={
                "company_run_controller_owner_token": "owner-a",
                "company_run_controller_lease_generation": generation1,
            },
        )
        await self.store1.save_delegation_role_session(
            role_session,
            controller_owner_token="owner-a",
            controller_lease_generation=generation1,
        )
        runtime = CompanyRuntime(
            org_engine=None,
            communication=None,
            store=self.store1,
        )
        state = runtime.create_state()
        state.controller_owner_token = "owner-a"
        state.controller_lease_generation = generation1
        state.role_sessions[role_session_id] = role_session
        member = state.member_sessions.setdefault(
            "member-1",
            CompanyMemberSession(
                member_session_id="member-1",
                role_session_id=role_session_id,
                role_id="executor",
                status="running",
                resident_status="running",
                current_task_id=task.id,
                focused_work_item_id="wi-1",
                current_work_item={"work_item_id": "wi-1"},
            ),
        )
        state.claimed_task_ids.add(task.id)
        state.claimed_work_item_ids.add("wi-1")
        state.role_queues["executor"].append("work-item::wi-1")
        state_token = runtime.use_state(state)
        try:
            await self._expire_lease(
                self.store1,
                owner_token="owner-a",
                generation=generation1,
            )
            takeover = await self.store2.acquire_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                lease_seconds=60,
            )
            self.assertTrue(takeover.acquired)
            with self.assertRaises(CompanyRunControllerLeaseLost):
                await runtime.reset_for_company_runtime_resume(
                    [task],
                    payload={
                        "checkpoint_id": "checkpoint-stale-resume",
                        "role_runtime_session_ids": [role_session_id],
                    },
                )
            self.assertEqual(member.status, "running")
            self.assertEqual(member.focused_work_item_id, "wi-1")
            self.assertIn(task.id, state.claimed_task_ids)
            self.assertIn("wi-1", state.claimed_work_item_ids)
            self.assertEqual(
                list(state.role_queues["executor"]),
                ["work-item::wi-1"],
            )
            persisted_role = await self.store2.get_delegation_role_session(
                role_session_id
            )
            assert persisted_role is not None
            self.assertEqual(persisted_role.status, "running")
            self.assertEqual(persisted_role.focused_work_item_id, "wi-1")
        finally:
            runtime.reset_state(state_token)

    async def test_partial_resume_persists_real_runtime_for_unheld_role_only(
        self,
    ) -> None:
        await self._seed_run("run-1", "root-1")
        ceo_item = DelegationWorkItem(
            work_item_id="wi-ceo",
            run_id="run-1",
            role_id="ceo",
            seat_id="seat-ceo",
            seat_state_id="seat-state-ceo",
            role_runtime_session_id="role-ceo",
            title="CEO arbitration",
            phase=Phase.READY,
        )
        worker_item = DelegationWorkItem(
            work_item_id="wi-worker",
            run_id="run-1",
            role_id="engineer",
            seat_id="seat-worker",
            seat_state_id="seat-state-worker",
            role_runtime_session_id="role-worker",
            title="Worker execution",
            phase=Phase.READY,
        )
        await self.store1.save_delegation_work_item(ceo_item)
        await self.store1.save_delegation_work_item(worker_item)
        ceo_task = Task(
            id="task-ceo",
            session_id="root-1",
            project_id="project-1",
            title="CEO arbitration",
            assigned_to="ceo",
            metadata={
                "delegation_run_id": "run-1",
                "delegation_role_session_id": "role-ceo",
            },
        )
        worker_task = Task(
            id="task-worker",
            session_id="root-1",
            project_id="project-1",
            title="Worker execution",
            assigned_to="engineer",
            metadata={
                "delegation_run_id": "run-1",
                "delegation_role_session_id": "role-worker",
            },
        )
        await self.store1.save_task(ceo_task)
        await self.store1.save_task(worker_task)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task("wi-ceo", ceo_task.id)
        )
        self.assertTrue(
            await self.store1.link_work_item_runtime_task(
                "wi-worker", worker_task.id
            )
        )
        lease = await self.store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-a",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        credential = {
            "controller_owner_token": "owner-a",
            "controller_lease_generation": lease.generation,
        }
        ceo_role = RoleRuntimeSession(
            role_session_id="role-ceo",
            run_id="run-1",
            project_id="project-1",
            role_id="ceo",
            seat_id="seat-ceo",
            seat_state_id="seat-state-ceo",
            status="idle",
        )
        worker_role = RoleRuntimeSession(
            role_session_id="role-worker",
            run_id="run-1",
            project_id="project-1",
            role_id="engineer",
            seat_id="seat-worker",
            seat_state_id="seat-state-worker",
            status="idle",
        )
        await self.store1.save_delegation_role_session(ceo_role, **credential)
        await self.store1.save_delegation_role_session(worker_role, **credential)
        await self.store1.save_seat_state(
            SeatState(
                seat_state_id="seat-state-ceo",
                run_id="run-1",
                project_id="project-1",
                team_id="team-ceo",
                seat_id="seat-ceo",
                role_id="ceo",
                role_runtime_session_id="role-ceo",
                status="idle",
            ),
            **credential,
        )
        await self.store1.update_delegation_work_item(
            "wi-worker",
            metadata_updates={"dispatch_hold": "company_runtime_suspended"},
        )

        class EmptyCommunication:
            @staticmethod
            async def read_inbox(**_kwargs: object) -> list[dict[str, object]]:
                return []

        runtime = CompanyRuntime(
            org_engine=None,
            communication=EmptyCommunication(),
            store=self.store1,
            save_runtime_session=self.store1.save_runtime_session,
        )
        state = runtime.create_state()
        state.controller_owner_token = "owner-a"
        state.controller_lease_generation = lease.generation
        state.role_sessions["role-ceo"] = ceo_role
        state.member_sessions["member-ceo"] = CompanyMemberSession(
            member_session_id="member-ceo",
            role_session_id="role-ceo",
            role_id="ceo",
            seat_id="seat-ceo",
            seat_state_id="seat-state-ceo",
            status="idle",
            resident_status="idle",
            metadata={"session_scope_id": "root-1"},
        )
        state_token = runtime.use_state(state)
        try:
            # This is the real CompanyRuntime persistence path used on the
            # first partial-resume dispatcher tick, not a Dummy executor.
            await runtime.refresh_inbox_state([ceo_task])
        finally:
            runtime.reset_state(state_token)

        persisted_ceo_role = await self.store1.get_delegation_role_session(
            "role-ceo"
        )
        persisted_ceo_seat = await self.store1.get_seat_state("seat-state-ceo")
        persisted_ceo_runtime = await self.store1.get_runtime_session(
            "member-ceo"
        )
        persisted_worker = await self.store1.get_delegation_work_item(
            "wi-worker"
        )
        assert persisted_ceo_role is not None and persisted_ceo_seat is not None
        assert persisted_ceo_runtime is not None and persisted_worker is not None
        self.assertEqual(
            persisted_ceo_role.metadata[
                "company_run_controller_lease_generation"
            ],
            lease.generation,
        )
        self.assertEqual(persisted_ceo_runtime["status"], "idle")
        self.assertEqual(
            persisted_worker.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        worker_role.status = "running"
        worker_role.focused_work_item_id = "wi-worker"
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_delegation_role_session(
                worker_role,
                **credential,
            )

    async def test_controller_child_approval_atomically_releases_parent_frontier(
        self,
    ) -> None:
        child_task, _generation = await self._seed_claimed_attempt(
            owner_token="owner-a"
        )
        await self._seed_waiting_parent()

        self.assertTrue(
            await transition_work_item_from_task(
                self.store1,
                child_task,
                target_status_or_phase=Phase.APPROVED,
                reason="child_approved",
                summary="Child result accepted.",
                require_work_item=True,
            )
        )

        child = await self.store1.get_delegation_work_item("wi-1")
        parent = await self.store1.get_delegation_work_item("wi-parent")
        parent_task = await self.store1.get_task("task-parent")
        assert child is not None and parent is not None and parent_task is not None
        self.assertEqual(child.phase, Phase.APPROVED)
        self.assertEqual(parent.phase, Phase.READY)
        self.assertEqual(parent.metadata.get("waiting_on_work_item_ids"), [])
        self.assertEqual(parent.claimed_by_role_runtime_session_id, "")
        self.assertEqual(parent.claimed_by_seat_id, "")
        self.assertEqual(parent_task.status, TaskStatus.PENDING)

    async def test_stale_generation_cannot_release_parent_frontier_after_takeover(
        self,
    ) -> None:
        child_task, generation1 = await self._seed_claimed_attempt(
            owner_token="owner-a"
        )
        await self._seed_waiting_parent()
        parent_before = await self.store2.get_delegation_work_item("wi-parent")
        parent_task_before = await self.store2.get_task("task-parent")
        assert parent_before is not None and parent_task_before is not None

        entered_frontier = asyncio.Event()
        continue_frontier = asyncio.Event()
        original_update = (
            self.store1.update_delegation_work_item_dependency_frontier_for_controller
        )

        async def gated_frontier_update(*args, **kwargs):
            entered_frontier.set()
            await continue_frontier.wait()
            return await original_update(*args, **kwargs)

        self.store1.update_delegation_work_item_dependency_frontier_for_controller = (  # type: ignore[method-assign]
            gated_frontier_update
        )
        stale_transition = asyncio.create_task(
            transition_work_item_from_task(
                self.store1,
                child_task,
                target_status_or_phase=Phase.APPROVED,
                reason="generation_1_child_approved",
                summary="The child commit wins before takeover.",
                require_work_item=True,
            )
        )
        await asyncio.wait_for(entered_frontier.wait(), timeout=2)

        await self._expire_lease(
            self.store1,
            owner_token="owner-a",
            generation=generation1,
        )
        takeover = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertGreater(takeover.generation, generation1)
        continue_frontier.set()
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await stale_transition

        child_after = await self.store2.get_delegation_work_item("wi-1")
        parent_after = await self.store2.get_delegation_work_item("wi-parent")
        parent_task_after = await self.store2.get_task("task-parent")
        assert child_after is not None
        self.assertEqual(child_after.phase, Phase.APPROVED)
        self.assertEqual(parent_after, parent_before)
        self.assertEqual(parent_task_after, parent_task_before)

    async def test_transition_transaction_serializes_expiry_takeover(self) -> None:
        task, generation1 = await self._seed_claimed_attempt(owner_token="owner-a")
        task.result = {"owner": "generation-1-committed"}
        await self._expire_lease(
            self.store1,
            owner_token="owner-a",
            generation=generation1,
            remaining_seconds=0.5,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        original_fence = self.store1._company_controller_task_fence_matches

        async def gated_fence(db, controller_task, *, checked_at):
            matched = await original_fence(
                db,
                controller_task,
                checked_at=checked_at,
            )
            if matched:
                entered.set()
                await release.wait()
            return matched

        self.store1._company_controller_task_fence_matches = gated_fence  # type: ignore[method-assign]
        transition = asyncio.create_task(
            transition_work_item_from_task(
                self.store1,
                task,
                target_status_or_phase=Phase.AWAITING_MANAGER_REVIEW,
                reason="atomic_generation_1_commit",
                summary="committed before takeover",
                require_work_item=True,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.sleep(0.6)

        def acquire_in_other_thread():
            async def acquire_with_isolated_store():
                takeover_store = OPCStore(self.db_path)
                await takeover_store.initialize(run_startup_maintenance=False)
                assert takeover_store._db is not None
                takeover_store._db._conn.execute("PRAGMA busy_timeout=2000")
                try:
                    return await takeover_store.acquire_delegation_run_controller_lease(
                        "run-1",
                        project_id="project-1",
                        root_session_id="root-1",
                        owner_token="owner-b",
                        lease_seconds=60,
                    )
                finally:
                    await takeover_store.close()

            return asyncio.run(acquire_with_isolated_store())

        takeover_results: list[object] = []
        takeover_errors: list[BaseException] = []

        def takeover_target() -> None:
            try:
                takeover_results.append(acquire_in_other_thread())
            except BaseException as exc:  # surfaced on the test event loop
                takeover_errors.append(exc)

        takeover_thread = threading.Thread(target=takeover_target, daemon=True)
        takeover_thread.start()
        await asyncio.sleep(0.05)
        self.assertTrue(takeover_thread.is_alive())
        release.set()
        self.assertTrue(await transition)
        deadline = asyncio.get_running_loop().time() + 3
        while takeover_thread.is_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        takeover_thread.join(timeout=0)
        self.assertFalse(takeover_thread.is_alive())
        if takeover_errors:
            raise takeover_errors[0]
        self.assertEqual(len(takeover_results), 1)
        takeover = takeover_results[0]
        self.assertTrue(takeover.acquired)
        self.assertGreater(takeover.generation, generation1)

        final_item = await self.store2.get_delegation_work_item("wi-1")
        final_task = await self.store2.get_task("task-1")
        assert final_item is not None and final_task is not None
        self.assertEqual(final_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(final_item.summary, "committed before takeover")
        self.assertEqual(final_task.result, {"owner": "generation-1-committed"})


if __name__ == "__main__":
    unittest.main()
