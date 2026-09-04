from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.company_controller import (
    CompanyControllerAttemptContext,
    CompanyRunControllerLeaseLost,
)
from opc.core.interaction_protocol import (
    CompanyWorkItemGateDecisionCommand,
    OriginOwnerInteractionLease,
    PreparedOwnerInteractionPublication,
)
from opc.core.models import (
    ApprovalAction,
    CompanyMemberSession,
    DelegationRun,
    DelegationRoleSession,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import (
    CompanyControllerRunLifecycleMutation,
    CompanyControllerWorkItemMutation,
    OPCStore,
    company_controller_task_preimage_hash,
)
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    WorkItemOutputBundle,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
)
from opc.layer2_organization.gate_harness import GateHarnessDecision
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
    WorkItemGatePolicy,
)
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_identity import (
    company_work_item_gate_attempt,
    company_work_item_gate_basis_hash,
    mark_work_item_projection,
    projection_id_for_task,
)
from opc.layer2_organization.work_item_transition import transition_work_item_from_task


class CompanyControllerAuthoritativeCommitTests(unittest.IsolatedAsyncioTestCase):
    """Real-SQLite takeover tests for post-native-DONE business writes."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.store1 = OPCStore(self.db_path)
        self.store2 = OPCStore(self.db_path)
        await self.store1.initialize()
        await self.store2.initialize(run_startup_maintenance=False)
        await self.store1.save_delegation_run(
            DelegationRun(
                run_id="run-1",
                project_id="project-1",
                session_id="root-1",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        self.executor1 = self._executor(self.store1)
        self.executor2 = self._executor(self.store2)

    async def asyncTearDown(self) -> None:
        await self.store2.close()
        await self.store1.close()
        self._tmp.cleanup()

    @staticmethod
    def _executor(store: OPCStore) -> CompanyWorkItemExecutor:
        async def execute_task(task: Task) -> TaskResult:
            return TaskResult(status=task.status, content="", artifacts={})

        return CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(
                on_kanban_changed=None,
                on_work_items_created=None,
            ),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=store.save_task,
            store=store,
        )

    async def _claim(
        self,
        store: OPCStore,
        *,
        item: DelegationWorkItem,
        task: Task,
        owner_token: str,
        expected_phase: Phase,
        generation: int | None = None,
    ) -> tuple[Task, int]:
        if await store.get_delegation_work_item(item.work_item_id) is None:
            await store.save_delegation_work_item(item)
        if await store.get_task(task.id) is None:
            await store.save_task(task)
        await store.link_work_item_runtime_task(item.work_item_id, task.id)
        if generation is None:
            lease = await store.acquire_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token=owner_token,
                lease_seconds=60,
            )
            self.assertTrue(lease.acquired)
            generation = lease.generation
        claimed = await store.claim_delegation_work_item_if_dispatchable(
            item.work_item_id,
            expected_phase=expected_phase,
            role_runtime_session_id=f"role-session::{owner_token}",
            seat_id=item.seat_id or "seat-1",
            task_id=task.id,
            controller_owner_token=owner_token,
            controller_lease_generation=generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        current_task = await store.get_task(task.id)
        assert current_task is not None
        current_task.status = TaskStatus.RUNNING
        current_task.metadata.update(
            {
                "delegation_run_id": "run-1",
                "company_run_controller_owner_token": owner_token,
                "company_run_controller_lease_generation": generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await store.save_task(current_task)
        return current_task, generation

    async def _take_over(
        self,
        *,
        stale_generation: int,
        item: DelegationWorkItem,
        task: Task,
    ) -> tuple[Task, int]:
        past = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store1.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=stale_generation,
                lease_seconds=1,
                heartbeat_at=past,
            )
        )
        takeover = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertGreater(takeover.generation, stale_generation)
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
        current_task, _ = await self._claim(
            self.store2,
            item=item,
            task=task,
            owner_token="owner-b",
            expected_phase=Phase.RUNNING,
            generation=takeover.generation,
        )
        return current_task, takeover.generation

    @staticmethod
    def _runtime_item(
        work_item_id: str,
        *,
        phase: Phase = Phase.READY,
        kind: str = "execute",
        role_id: str = "analyst",
        seat_id: str = "seat::analyst",
        parent_work_item_id: str | None = None,
        metadata: dict | None = None,
    ) -> DelegationWorkItem:
        return DelegationWorkItem(
            work_item_id=work_item_id,
            run_id="run-1",
            cell_id="team::analysis",
            team_id="team::analysis",
            role_id=role_id,
            seat_id=seat_id,
            manager_role_id="manager",
            manager_seat_id="seat::manager",
            parent_work_item_id=parent_work_item_id,
            title=work_item_id,
            summary="initial summary",
            kind=kind,
            projection_id=work_item_id,
            phase=phase,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_kind": kind,
                **dict(metadata or {}),
            },
        )

    @staticmethod
    def _runtime_task(
        task_id: str,
        work_item_id: str,
        *,
        metadata: dict | None = None,
    ) -> Task:
        task = Task(
            id=task_id,
            session_id="root-1",
            parent_session_id="root-1",
            project_id="project-1",
            title=work_item_id,
            assigned_to="analyst",
            status=TaskStatus.PENDING,
            metadata=mark_work_item_projection(
                {
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::analyst",
                    "work_item_role_id": "analyst",
                    "work_kind": "execute",
                    **dict(metadata or {}),
                },
                projection_id=work_item_id,
                turn_type=str(
                    dict(metadata or {}).get(
                        "work_item_turn_type",
                        dict(metadata or {}).get("work_kind", "execute"),
                    )
                    or "execute"
                ).strip(),
            ),
        )
        if not dict(task.metadata.get("company_work_item_plan", {}) or {}):
            task.metadata["company_work_item_plan"] = (
                CompanyWorkItemRuntimePlan(
                    root_projection_id=work_item_id,
                    projections=[
                        WorkItemProjectionSpec(
                            projection_id=work_item_id,
                            turn_type=str(
                                dict(metadata or {}).get(
                                    "work_item_turn_type",
                                    dict(metadata or {}).get(
                                        "work_kind", "execute"
                                    ),
                                )
                                or "execute"
                            ).strip(),
                            role_id="analyst",
                            title=work_item_id,
                            summary="initial summary",
                            team_id="team::analysis",
                            seat_id="seat::analyst",
                            manager_role_id="manager",
                            manager_seat_id="seat::manager",
                        )
                    ],
                ).to_dict()
            )
        set_linked_work_item_id(task, work_item_id)
        return task

    async def _prepare_blocked_manager_attention_source(
        self,
        *,
        child_phase: Phase = Phase.RUNNING,
        dispatch_hold: str = "",
    ) -> tuple[
        DelegationWorkItem,
        DelegationWorkItem,
        Task,
        CompanyMemberSession,
        int,
    ]:
        child = self._runtime_item(
            "wi-attention-child",
            phase=child_phase,
            role_id="worker",
            seat_id="seat::worker",
            parent_work_item_id="wi-attention-parent",
        )
        parent = self._runtime_item(
            "wi-attention-parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            kind="deliver",
            role_id="manager",
            seat_id="seat::manager",
            metadata={
                "dependency_work_item_ids": [child.work_item_id],
                "current_turn_mode": "monitor_children",
                **(
                    {"dispatch_hold": dispatch_hold}
                    if dispatch_hold
                    else {}
                ),
            },
        )
        task = self._runtime_task(
            "task-attention-parent",
            parent.work_item_id,
            metadata={
                "work_kind": "deliver",
                "work_item_turn_type": "deliver",
                "delegation_seat_id": "seat::manager",
                "work_item_role_id": "manager",
            },
        )
        task.assigned_to = "manager"
        task.status = TaskStatus.BLOCKED
        await self.store1.save_delegation_work_item(parent)
        await self.store1.save_delegation_work_item(child)
        await self.store1.save_task(task)
        await self.store1.link_work_item_runtime_task(
            parent.work_item_id,
            task.id,
        )
        lease = await self.store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-a",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        generation = lease.generation
        parent = await self.store1.get_delegation_work_item(
            parent.work_item_id
        )
        assert parent is not None
        role_session_id = "role-session::manager"
        await self.store1.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="run-1",
                project_id="project-1",
                role_id="manager",
                seat_id="seat::manager",
                focused_work_item_id=parent.work_item_id,
                status="blocked",
            ),
            controller_owner_token="owner-a",
            controller_lease_generation=generation,
        )
        session = CompanyMemberSession(
            member_session_id="member::manager",
            role_session_id=role_session_id,
            role_id="manager",
            employee_id="manager-employee",
            seat_id="seat::manager",
            seat_state_id="seat-state::manager",
            team_id="team::analysis",
            status="blocked",
            resident_status="blocked",
            current_task_id=task.id,
            focused_work_item_id=parent.work_item_id,
            current_turn_mode="monitor_children",
            current_work_item={"work_item_id": parent.work_item_id},
            metadata={
                "role_id": "manager",
                "seat_id": "seat::manager",
                "team_id": "team::analysis",
            },
        )
        return parent, child, task, session, generation

    @staticmethod
    def _attention_message() -> dict[str, str]:
        return {
            "msg_id": "msg-attention",
            "from_agent": "worker",
            "subject": "Child needs manager attention",
            "body": "Review the child update before continuing delivery.",
        }

    @staticmethod
    def _set_executor_controller(
        executor: CompanyWorkItemExecutor,
        *,
        owner_token: str,
        generation: int,
    ) -> None:
        state = executor._run_state()
        state.controller_run_id = "run-1"
        state.controller_project_id = "project-1"
        state.controller_root_session_id = "root-1"
        state.controller_owner_token = owner_token
        state.controller_lease_generation = generation

    @staticmethod
    async def _role_mirror_rows(
        store: OPCStore,
        role_session_id: str,
    ) -> dict[str, tuple]:
        db = store._require_db()
        rows: dict[str, tuple] = {}
        for table in ("role_runtime_sessions", "delegation_role_sessions"):
            async with db.execute(
                f"""SELECT run_id, project_id, role_id, seat_id,
                           focused_work_item_id, current_work_item, status,
                           pending_work_item_ids, metadata, updated_at
                    FROM {table}
                    WHERE role_session_id = ?""",
                (role_session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                rows[table] = tuple(row)
        return rows

    async def test_attention_wake_commits_work_item_role_mirrors_and_task(
        self,
    ) -> None:
        parent, child, root_task, session, generation = (
            await self._prepare_blocked_manager_attention_source()
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation,
        )

        updated_tasks, updated_items = await self.executor1._upsert_attention_work_item(
            root_task=root_task,
            tasks=[root_task],
            work_items=[parent, child],
            session=session,
            source_message=self._attention_message(),
        )

        attention_items = [
            item
            for item in updated_items
            if dict(item.metadata or {}).get("attention_work_item") is True
        ]
        self.assertEqual(len(attention_items), 1)
        attention = attention_items[0]
        self.assertEqual(attention.phase, Phase.READY)
        self.assertEqual(attention.parent_work_item_id, parent.work_item_id)
        self.assertEqual(
            attention.metadata["company_run_controller_owner_token"],
            "owner-a",
        )
        self.assertEqual(
            attention.metadata["company_run_controller_lease_generation"],
            generation,
        )
        self.assertNotIn("queued_behind_session", attention.metadata)

        mirrors = await self._role_mirror_rows(
            self.store1,
            session.role_session_id,
        )
        self.assertEqual(
            set(mirrors),
            {"role_runtime_sessions", "delegation_role_sessions"},
        )
        for row in mirrors.values():
            self.assertEqual(row[4], "")
            self.assertEqual(json.loads(row[5]), {})
            self.assertEqual(row[6], "idle")
            self.assertNotIn(attention.work_item_id, json.loads(row[7]))
            role_metadata = json.loads(row[8])
            self.assertEqual(
                role_metadata["company_run_controller_owner_token"],
                "owner-a",
            )
            self.assertEqual(
                role_metadata["company_run_controller_lease_generation"],
                generation,
            )

        projected = await self.store1.get_runtime_task_for_work_item(
            attention.work_item_id
        )
        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertEqual(projected.status, TaskStatus.PENDING)
        self.assertEqual(projected.metadata["message_priority"], "seat_attention")
        self.assertIn(projected, updated_tasks)
        claimed = await self.store1.claim_delegation_work_item_if_dispatchable(
            attention.work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id=session.role_session_id,
            seat_id=session.seat_id,
            task_id=projected.id,
            controller_owner_token="owner-a",
            controller_lease_generation=generation,
        )
        self.assertIsNotNone(claimed)

    async def test_attention_wake_takeover_loser_has_zero_partial_side_effects(
        self,
    ) -> None:
        parent, child, root_task, session, generation1 = (
            await self._prepare_blocked_manager_attention_source()
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation1,
        )
        items_before = await self.store2.list_delegation_work_items("run-1")
        tasks_before = await self.store2.get_tasks(project_id="project-1")
        mirrors_before = await self._role_mirror_rows(
            self.store2,
            session.role_session_id,
        )
        async with self.store2._require_db().execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links ORDER BY work_item_id"
        ) as cursor:
            links_before = await cursor.fetchall()

        past = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store1.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=generation1,
                lease_seconds=1,
                heartbeat_at=past,
            )
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
            await self.executor1._upsert_attention_work_item(
                root_task=root_task,
                tasks=[root_task],
                work_items=[parent, child],
                session=session,
                source_message=self._attention_message(),
            )

        self.assertEqual(
            await self.store2.list_delegation_work_items("run-1"),
            items_before,
        )
        self.assertEqual(
            await self.store2.get_tasks(project_id="project-1"),
            tasks_before,
        )
        self.assertEqual(
            await self._role_mirror_rows(
                self.store2,
                session.role_session_id,
            ),
            mirrors_before,
        )
        async with self.store2._require_db().execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links ORDER BY work_item_id"
        ) as cursor:
            self.assertEqual(await cursor.fetchall(), links_before)
        self.assertTrue(
            await self.store2.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                generation=takeover.generation,
                lease_seconds=60,
            )
        )

        self._set_executor_controller(
            self.executor2,
            owner_token="owner-b",
            generation=takeover.generation,
        )
        _winner_tasks, winner_items = await self.executor2._upsert_attention_work_item(
            root_task=root_task,
            tasks=[root_task],
            work_items=[parent, child],
            session=session,
            source_message=self._attention_message(),
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in winner_items
                    if dict(item.metadata or {}).get("attention_work_item") is True
                ]
            ),
            1,
        )

    async def test_attention_current_target_takeover_loser_has_zero_writes(
        self,
    ) -> None:
        parent, child, root_task, session, generation1 = (
            await self._prepare_blocked_manager_attention_source(
                child_phase=Phase.APPROVED,
            )
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation1,
        )
        items_before = await self.store2.list_delegation_work_items("run-1")
        tasks_before = await self.store2.get_tasks(project_id="project-1")
        mirrors_before = await self._role_mirror_rows(
            self.store2,
            session.role_session_id,
        )

        past = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store1.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=generation1,
                lease_seconds=1,
                heartbeat_at=past,
            )
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
            await self.executor1._upsert_attention_work_item(
                root_task=root_task,
                tasks=[root_task],
                work_items=[parent, child],
                session=session,
                source_message=self._attention_message(),
            )
        self.assertEqual(
            await self.store2.list_delegation_work_items("run-1"),
            items_before,
        )
        self.assertEqual(
            await self.store2.get_tasks(project_id="project-1"),
            tasks_before,
        )
        self.assertEqual(
            await self._role_mirror_rows(
                self.store2,
                session.role_session_id,
            ),
            mirrors_before,
        )

        self._set_executor_controller(
            self.executor2,
            owner_token="owner-b",
            generation=takeover.generation,
        )
        winner_tasks, winner_items = await self.executor2._upsert_attention_work_item(
            root_task=root_task,
            tasks=[root_task],
            work_items=[parent, child],
            session=session,
            source_message=self._attention_message(),
        )
        resumed = next(
            item
            for item in winner_items
            if item.work_item_id == parent.work_item_id
        )
        self.assertEqual(resumed.phase, Phase.RUNNING)
        self.assertFalse(
            any(
                dict(item.metadata or {}).get("attention_work_item") is True
                for item in winner_items
            )
        )
        projected_parent = next(
            task
            for task in winner_tasks
            if task.id == root_task.id
        )
        self.assertEqual(projected_parent.status, TaskStatus.RUNNING)
        persisted_parent_task = await self.store2.get_task(root_task.id)
        assert persisted_parent_task is not None
        self.assertEqual(persisted_parent_task.status, TaskStatus.RUNNING)
        self.assertEqual(
            persisted_parent_task.metadata["message_priority"],
            "seat_attention",
        )
        for row in (
            await self._role_mirror_rows(
                self.store2,
                session.role_session_id,
            )
        ).values():
            self.assertEqual(row[4], "")
            self.assertEqual(json.loads(row[5]), {})
            self.assertEqual(row[6], "idle")

    async def test_attention_current_target_dispatch_hold_has_zero_writes(
        self,
    ) -> None:
        parent, child, root_task, session, generation = (
            await self._prepare_blocked_manager_attention_source(
                child_phase=Phase.APPROVED,
                dispatch_hold="run_quarantine",
            )
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation,
        )
        items_before = await self.store1.list_delegation_work_items("run-1")
        tasks_before = await self.store1.get_tasks(project_id="project-1")
        mirrors_before = await self._role_mirror_rows(
            self.store1,
            session.role_session_id,
        )

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._upsert_attention_work_item(
                root_task=root_task,
                tasks=[root_task],
                work_items=[parent, child],
                session=session,
                source_message=self._attention_message(),
            )
        self.assertEqual(
            await self.store1.list_delegation_work_items("run-1"),
            items_before,
        )
        self.assertEqual(
            await self.store1.get_tasks(project_id="project-1"),
            tasks_before,
        )
        self.assertEqual(
            await self._role_mirror_rows(
                self.store1,
                session.role_session_id,
            ),
            mirrors_before,
        )

    async def test_attention_task_materialization_rejects_stale_generation_atomically(
        self,
    ) -> None:
        parent, _child, _root_task, session, generation1 = (
            await self._prepare_blocked_manager_attention_source()
        )
        candidate = self._runtime_item(
            "wi-attention-materialize",
            kind="monitor",
            role_id="manager",
            seat_id="seat::manager",
            parent_work_item_id=parent.work_item_id,
            metadata={
                "attention_work_item": True,
                "attention_key": "seat::manager:monitor",
                "assigned_role_runtime_id": session.role_session_id,
            },
        )
        candidate.role_runtime_session_id = session.role_session_id
        persisted = await self.store1.upsert_company_attention_work_item_for_controller(
            candidate,
            project_id="project-1",
            controller_owner_token="owner-a",
            controller_lease_generation=generation1,
            expected_role_session_focus=parent.work_item_id,
        )
        self.assertIsNotNone(persisted)
        assert persisted is not None

        past = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store1.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=generation1,
                lease_seconds=1,
                heartbeat_at=past,
            )
        )
        takeover = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        task = self._runtime_task(
            "task-attention-materialize",
            persisted.work_item_id,
            metadata={
                "work_kind": "monitor",
                "work_item_turn_type": "monitor",
                "delegation_seat_id": "seat::manager",
                "work_item_role_id": "manager",
            },
        )
        task.assigned_to = "manager"
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.ensure_runtime_task_for_work_item(
                persisted,
                lambda: task,
                project_id="project-1",
                controller_owner_token="owner-a",
                controller_lease_generation=generation1,
            )
        self.assertIsNone(
            await self.store2.get_runtime_task_for_work_item(
                persisted.work_item_id
            )
        )
        self.assertTrue(
            await self.store2.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-b",
                generation=takeover.generation,
                lease_seconds=60,
            )
        )
        winner_task = await self.store2.ensure_runtime_task_for_work_item(
            persisted,
            lambda: task,
            project_id="project-1",
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        self.assertEqual(winner_task.id, task.id)
        attempt_credential_keys = (
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
            "claimed_work_item_attempt_seq",
        )
        for key in attempt_credential_keys:
            self.assertNotIn(key, winner_task.metadata)
        durable_pending = await self.store2.get_task(winner_task.id)
        assert durable_pending is not None
        for key in attempt_credential_keys:
            self.assertNotIn(key, durable_pending.metadata)

        self.assertTrue(
            self.executor2._apply_work_item_projection_to_task(
                winner_task,
                persisted,
            )
        )
        self.assertIsNone(
            await self.store2.save_unclaimed_runtime_task_projection_for_company_controller(
                winner_task,
                project_id="project-1",
                controller_owner_token="owner-b",
                controller_lease_generation=takeover.generation,
            )
        )
        durable_pending = await self.store2.get_task(winner_task.id)
        assert durable_pending is not None
        for key in attempt_credential_keys:
            self.assertNotIn(key, durable_pending.metadata)

        self._set_executor_controller(
            self.executor2,
            owner_token="owner-b",
            generation=takeover.generation,
        )
        runtime_state = self.executor2.runtime._state()
        runtime_state.controller_owner_token = "owner-b"
        runtime_state.controller_lease_generation = takeover.generation
        self.executor2.runtime.org_engine = SimpleNamespace(
            get_agent=lambda _role_id: None
        )
        member_session = self.executor2.runtime._ensure_member_session(
            winner_task
        )
        self.assertTrue(
            await self.executor2.runtime._claim_role_session_work_item(
                member_session,
                persisted,
                winner_task,
            )
        )
        self.assertEqual(
            winner_task.metadata["company_run_controller_owner_token"],
            "owner-b",
        )
        self.assertEqual(
            winner_task.metadata[
                "company_run_controller_lease_generation"
            ],
            takeover.generation,
        )
        self.assertEqual(
            winner_task.metadata["claimed_work_item_attempt_seq"],
            1,
        )
        # A projection pass can hold a clean pre-claim Task snapshot while the
        # dispatcher concurrently persists the exact claimed attempt.  The
        # stale projection is a benign loser, not a run-controller lease loss.
        stale_unclaimed_projection = copy.deepcopy(durable_pending)
        await self.store2.save_task(winner_task)
        persisted.summary = "projection refresh raced with a live claim"
        await self.executor2._sync_task_projection_from_work_items(
            [stale_unclaimed_projection],
            [persisted],
        )
        durable_after_race = await self.store2.get_task(winner_task.id)
        assert durable_after_race is not None
        self.assertEqual(
            durable_after_race.metadata["company_run_controller_owner_token"],
            "owner-b",
        )
        self.assertEqual(
            durable_after_race.metadata["claimed_work_item_attempt_seq"],
            1,
        )
        await self.executor2._sync_task_projection_from_work_items(
            [winner_task],
            [persisted],
        )
        durable_claimed = await self.store2.get_task(winner_task.id)
        assert durable_claimed is not None
        self.assertEqual(durable_claimed.status, TaskStatus.RUNNING)
        self.assertEqual(
            durable_claimed.metadata["company_run_controller_owner_token"],
            "owner-b",
        )
        self.assertEqual(
            durable_claimed.metadata["claimed_work_item_attempt_seq"],
            1,
        )

    async def test_live_controller_materializer_requires_fenced_store_api(
        self,
    ) -> None:
        _parent, child, root_task, _session, generation = (
            await self._prepare_blocked_manager_attention_source()
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation,
        )
        items_before = await self.store1.list_delegation_work_items("run-1")
        tasks_before = await self.store1.get_tasks(project_id="project-1")
        async with self.store1._require_db().execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links ORDER BY work_item_id"
        ) as cursor:
            links_before = await cursor.fetchall()

        with patch.object(
            self.store1,
            "ensure_runtime_task_for_work_item",
            None,
        ):
            with self.assertRaises(CompanyRunControllerLeaseLost):
                await self.executor1._materialize_work_item_tasks(
                    [root_task],
                    [child],
                )
        self.assertEqual(
            await self.store1.list_delegation_work_items("run-1"),
            items_before,
        )
        self.assertEqual(
            await self.store1.get_tasks(project_id="project-1"),
            tasks_before,
        )
        async with self.store1._require_db().execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links ORDER BY work_item_id"
        ) as cursor:
            self.assertEqual(await cursor.fetchall(), links_before)

    async def test_live_controller_materializes_and_syncs_ready_child_without_attempt_credential(
        self,
    ) -> None:
        parent, child, root_task, _session, generation = (
            await self._prepare_blocked_manager_attention_source(
                child_phase=Phase.READY,
            )
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=generation,
        )
        tasks = await self.executor1._materialize_work_item_tasks(
            [root_task],
            [parent, child],
        )
        child_task = next(
            task
            for task in tasks
            if getattr(task, "linked_work_item_id", "")
            == child.work_item_id
        )
        await self.executor1._sync_task_projection_from_work_items(
            tasks,
            [parent, child],
        )

        linked_child = await self.store2.get_runtime_task_for_work_item(
            child.work_item_id
        )
        self.assertIsNotNone(linked_child)
        assert linked_child is not None
        self.assertEqual(linked_child.id, child_task.id)
        self.assertEqual(linked_child.status, TaskStatus.PENDING)
        self.assertEqual(
            linked_child.metadata["derived_work_item_projection"]["phase"],
            Phase.READY.value,
        )
        for key in (
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
            "claimed_work_item_attempt_seq",
        ):
            self.assertNotIn(key, child_task.metadata)
            self.assertNotIn(key, linked_child.metadata)

    async def test_projection_sync_leaves_suspended_task_and_work_item_frozen(
        self,
    ) -> None:
        item = self._runtime_item(
            "wi-held-approved",
            phase=Phase.APPROVED,
            metadata={
                "dispatch_hold": "company_runtime_suspended",
                "attempt_seq": 1,
                "attempt_settled": True,
            },
        )
        task = self._runtime_task(
            "task-held-approved",
            item.work_item_id,
            metadata={"dispatch_hold": "company_runtime_suspended"},
        )
        task.status = TaskStatus.AWAITING_MANAGER_REVIEW
        await self.store1.save_delegation_work_item(item)
        await self.store1.save_task(task)
        await self.store1.link_work_item_runtime_task(item.work_item_id, task.id)
        before = await self.store2.get_task(task.id)
        assert before is not None

        lease = await self.store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-a",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=lease.generation,
        )

        item.summary = "approved while another resume stage still owns the hold"
        await self.executor1._sync_task_projection_from_work_items([task], [item])

        self.assertEqual(task, before)
        self.assertEqual(await self.store2.get_task(task.id), before)

    async def test_unclaimed_task_projection_takeover_and_generic_save_have_zero_writes(
        self,
    ) -> None:
        item = self._runtime_item("wi-unclaimed-takeover")
        task = self._runtime_task(
            "task-unclaimed-takeover",
            item.work_item_id,
        )
        await self.store1.save_delegation_work_item(item)
        await self.store1.save_task(task)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task(
                item.work_item_id,
                task.id,
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
        candidate = await self.store1.get_task(task.id)
        assert candidate is not None
        self.assertTrue(
            self.executor1._apply_work_item_projection_to_task(
                candidate,
                item,
            )
        )
        durable_before = await self.store2.get_task(task.id)
        assert durable_before is not None

        broad_candidate = copy.deepcopy(candidate)
        broad_candidate.description = "unfenced broad mutation"
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.save_task(broad_candidate)
        self.assertEqual(await self.store2.get_task(task.id), durable_before)

        past = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store1.renew_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=lease.generation,
                lease_seconds=1,
                heartbeat_at=past,
            )
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
            await self.store1.save_unclaimed_runtime_task_projection_for_company_controller(
                candidate,
                project_id="project-1",
                controller_owner_token="owner-a",
                controller_lease_generation=lease.generation,
            )
        self.assertEqual(await self.store2.get_task(task.id), durable_before)

    async def test_unclaimed_task_projection_claim_hold_and_identity_conflicts_have_zero_writes(
        self,
    ) -> None:
        case_items: dict[str, DelegationWorkItem] = {}
        case_tasks: dict[str, Task] = {}
        for label in ("claimed", "held", "identity", "turn"):
            item = self._runtime_item(
                f"wi-unclaimed-{label}",
                metadata=(
                    {"dispatch_hold": "run_quarantine"}
                    if label == "held"
                    else None
                ),
            )
            task = self._runtime_task(
                f"task-unclaimed-{label}",
                item.work_item_id,
            )
            await self.store1.save_delegation_work_item(item)
            await self.store1.save_task(task)
            self.assertTrue(
                await self.store1.link_work_item_runtime_task(
                    item.work_item_id,
                    task.id,
                )
            )
            case_items[label] = item
            case_tasks[label] = task

        lease = await self.store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-a",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        claimed_item = await self.store1.claim_delegation_work_item_if_dispatchable(
            case_items["claimed"].work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session::analyst",
            seat_id="seat::analyst",
            task_id=case_tasks["claimed"].id,
            controller_owner_token="owner-a",
            controller_lease_generation=lease.generation,
        )
        self.assertIsNotNone(claimed_item)
        assert claimed_item is not None
        case_items["claimed"] = claimed_item

        for label in ("claimed", "held", "identity", "turn"):
            with self.subTest(label=label):
                candidate = await self.store1.get_task(
                    case_tasks[label].id
                )
                assert candidate is not None
                self.executor1._apply_work_item_projection_to_task(
                    candidate,
                    case_items[label],
                )
                if label == "identity":
                    candidate.metadata["delegation_seat_id"] = (
                        "seat::foreign"
                    )
                if label == "turn":
                    candidate.metadata["work_item_turn_type"] = "review"
                durable_before = await self.store2.get_task(candidate.id)
                with self.assertRaises(CompanyRunControllerLeaseLost):
                    await self.store1.save_unclaimed_runtime_task_projection_for_company_controller(
                        candidate,
                        project_id="project-1",
                        controller_owner_token="owner-a",
                        controller_lease_generation=lease.generation,
                    )
                self.assertEqual(
                    await self.store2.get_task(candidate.id),
                    durable_before,
                )

    async def test_materializer_and_sync_reject_partial_attempt_credentials_without_writes(
        self,
    ) -> None:
        item = self._runtime_item("wi-partial-materialization")
        reuse_item = self._runtime_item("wi-partial-reuse")
        reuse_task = self._runtime_task(
            "task-partial-reuse",
            reuse_item.work_item_id,
            metadata={
                "company_run_controller_lease_generation": 7,
            },
        )
        await self.store1.save_delegation_work_item(item)
        await self.store1.save_delegation_work_item(reuse_item)
        await self.store1.save_task(reuse_task)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task(
                reuse_item.work_item_id,
                reuse_task.id,
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
        reuse_before = await self.store2.get_task(reuse_task.id)
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.ensure_runtime_task_for_work_item(
                reuse_item,
                lambda: self._runtime_task(
                    "unused-clean-reuse",
                    reuse_item.work_item_id,
                ),
                project_id="project-1",
                controller_owner_token="owner-a",
                controller_lease_generation=lease.generation,
            )
        self.assertEqual(
            await self.store2.get_task(reuse_task.id),
            reuse_before,
        )
        forged_candidate = self._runtime_task(
            "task-partial-materialization",
            item.work_item_id,
            metadata={
                "company_run_controller_owner_token": "owner-a",
                "company_run_controller_lease_generation": lease.generation,
            },
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.ensure_runtime_task_for_work_item(
                item,
                lambda: forged_candidate,
                project_id="project-1",
                controller_owner_token="owner-a",
                controller_lease_generation=lease.generation,
            )
        self.assertIsNone(await self.store2.get_task(forged_candidate.id))
        self.assertIsNone(
            await self.store2.get_runtime_task_for_work_item(
                item.work_item_id
            )
        )

        clean_task = self._runtime_task(
            "task-partial-sync",
            item.work_item_id,
        )
        await self.store1.ensure_runtime_task_for_work_item(
            item,
            lambda: clean_task,
            project_id="project-1",
            controller_owner_token="owner-a",
            controller_lease_generation=lease.generation,
        )
        durable_before = await self.store2.get_task(clean_task.id)
        assert durable_before is not None
        clean_task.metadata["company_run_controller_owner_token"] = (
            "owner-a"
        )
        self._set_executor_controller(
            self.executor1,
            owner_token="owner-a",
            generation=lease.generation,
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._sync_task_projection_from_work_items(
                [clean_task],
                [item],
            )
        self.assertEqual(
            await self.store2.get_task(clean_task.id),
            durable_before,
        )

        live_candidate = await self.store1.get_task(clean_task.id)
        assert live_candidate is not None
        item.summary = "projection update must not swallow Store failure"
        with patch.object(
            self.store1,
            "save_unclaimed_runtime_task_projection_for_company_controller",
            AsyncMock(side_effect=RuntimeError("injected Store failure")),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected Store failure",
            ):
                await self.executor1._sync_task_projection_from_work_items(
                    [live_candidate],
                    [item],
                )
        self.assertEqual(
            await self.store2.get_task(clean_task.id),
            durable_before,
        )

    async def test_attention_boundary_rejects_focus_or_shape_conflicts_without_writes(
        self,
    ) -> None:
        parent, _child, _root_task, session, generation = (
            await self._prepare_blocked_manager_attention_source()
        )
        candidate = self._runtime_item(
            "wi-attention-invalid",
            kind="monitor",
            role_id="manager",
            seat_id="seat::manager",
            parent_work_item_id=parent.work_item_id,
            metadata={
                "attention_work_item": True,
                "attention_key": "seat::manager:monitor",
            },
        )
        candidate.role_runtime_session_id = session.role_session_id
        items_before = await self.store1.list_delegation_work_items("run-1")
        mirrors_before = await self._role_mirror_rows(
            self.store1,
            session.role_session_id,
        )
        invalid_projection = copy.deepcopy(candidate)
        invalid_projection.metadata[
            "work_item_projection_id"
        ] = "foreign-projection"
        invalid_parent = copy.deepcopy(candidate)
        invalid_parent.parent_work_item_id = "foreign-parent"
        invalid_phase = copy.deepcopy(candidate)
        invalid_phase.phase = Phase.PAUSED
        for label, invalid_candidate in (
            ("projection", invalid_projection),
            ("parent", invalid_parent),
            ("phase", invalid_phase),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    await self.store1.upsert_company_attention_work_item_for_controller(
                        invalid_candidate,
                        project_id="project-1",
                        controller_owner_token="owner-a",
                        controller_lease_generation=generation,
                        expected_role_session_focus=parent.work_item_id,
                    )
        focus_conflict = copy.deepcopy(candidate)
        focus_conflict.parent_work_item_id = "winning-focus"
        self.assertIsNone(
            await self.store1.upsert_company_attention_work_item_for_controller(
                focus_conflict,
                project_id="project-1",
                controller_owner_token="owner-a",
                controller_lease_generation=generation,
                expected_role_session_focus="winning-focus",
            )
        )
        self.assertEqual(
            await self.store1.list_delegation_work_items("run-1"),
            items_before,
        )
        self.assertEqual(
            await self._role_mirror_rows(
                self.store1,
                session.role_session_id,
            ),
            mirrors_before,
        )

    async def _seed_company_origin(
        self,
        *,
        item: DelegationWorkItem,
        task: Task,
        checkpoint_id: str,
        claim_id: str = "origin-claim",
        consumer_id: str = "origin-consumer",
    ) -> OriginOwnerInteractionLease:
        interaction_key = f"staffing:{checkpoint_id}"
        checkpoint, created = await self.store1.create_owner_interaction_checkpoint(
            ExecutionCheckpoint(
                checkpoint_id=checkpoint_id,
                project_id="project-1",
                session_id="root-1",
                checkpoint_type="company_staffing_selection",
                task_id=task.id,
                payload={
                    "interaction": {
                        "kind": "company_staffing_selection",
                        "domain_key": interaction_key,
                        "ownership": {
                            "waiting_task_id": task.id,
                            "waiting_session_id": "root-1",
                            "root_session_id": "root-1",
                            "ui_anchor_session_id": "root-1",
                        },
                        "execution_scope": {"company_profile": "corporate"},
                    }
                },
            ),
            interaction_key=interaction_key,
        )
        self.assertTrue(created)
        decision = {"staffing_action": "manual_approve", "text": "approve"}
        accepted = await self.store1.accept_execution_checkpoint_decision(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type=checkpoint.checkpoint_type,
            request_id=f"request:{checkpoint_id}",
            decision_hash=hashlib.sha256(
                repr(sorted(decision.items())).encode("utf-8")
            ).hexdigest(),
            decision=decision,
        )
        self.assertTrue(accepted.acknowledged)
        claimed = await self.store1.claim_answered_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type=checkpoint.checkpoint_type,
            consumer_id=consumer_id,
            claim_id=claim_id,
            lease_seconds=300,
        )
        self.assertTrue(claimed.acquired)
        started = await self.store1.begin_execution_checkpoint_effect(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type=checkpoint.checkpoint_type,
            consumer_id=consumer_id,
            claim_id=claim_id,
        )
        self.assertTrue(started.acquired)
        origin = OriginOwnerInteractionLease(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            project_id="project-1",
            claim_id=claim_id,
            consumer_id=consumer_id,
        )
        origin_payload = origin.to_payload()
        item.metadata["origin_owner_interaction"] = dict(origin_payload)
        task.metadata["origin_owner_interaction"] = dict(origin_payload)
        run = await self.store1.get_delegation_run("run-1")
        assert run is not None
        run.metadata = dict(run.metadata or {})
        run.metadata["origin_owner_interaction"] = dict(origin_payload)
        await self.store1.save_delegation_run(run)
        return origin

    @staticmethod
    def _prepared_final_owner_publication(
        task: Task,
        *,
        checkpoint_id: str,
    ) -> PreparedOwnerInteractionPublication:
        interaction_key = f"delivery:{checkpoint_id}"
        supersession_key = "owner:project-1:root-1"
        return PreparedOwnerInteractionPublication(
            checkpoint=ExecutionCheckpoint(
                checkpoint_id=checkpoint_id,
                project_id="project-1",
                session_id=task.session_id,
                checkpoint_type="company_delivery_feedback",
                task_id=task.id,
                payload={
                    "waiting_task_id": task.id,
                    "task_ids": [task.id],
                    "prompt": "Review final delivery.",
                    "interaction": {
                        "kind": "company_delivery_feedback",
                        "domain_key": interaction_key,
                        "supersession_key": supersession_key,
                        "supersession_order": [1, 0],
                        "ownership": {
                            "waiting_task_id": task.id,
                            "waiting_session_id": task.session_id,
                            "root_session_id": "root-1",
                            "ui_anchor_session_id": "root-1",
                        },
                    },
                },
            ),
            interaction_key=interaction_key,
            supersession_key=supersession_key,
            supersession_order=(1, 0),
        )

    @staticmethod
    def _prepared_gate_publication_from_data(
        data: dict,
        *,
        checkpoint_id: str = "checkpoint-work-item-gate",
        prefill_decision: bool = False,
    ) -> PreparedOwnerInteractionPublication:
        payload = copy.deepcopy(dict(data.get("payload", {}) or {}))
        interaction_key = hashlib.sha256(
            json.dumps(
                {
                    "type": data.get("checkpoint_type"),
                    "task_id": data.get("task_id"),
                    "basis_hash": payload.get("basis_hash"),
                    "source_event_id": payload.get("source_event_id"),
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        supersession_key = hashlib.sha256(
            f"gate:{data.get('project_id')}:{data.get('task_id')}".encode(
                "utf-8"
            )
        ).hexdigest()
        supersession_order = (
            int(payload.get("work_item_attempt_seq", 0) or 0),
            int(payload.get("gate_attempt", 0) or 0),
        )
        interaction = {
            "kind": "company_work_item_gate",
            "prompt": str(payload.get("prompt", "") or ""),
            "domain_key": interaction_key,
            "supersession_key": supersession_key,
            "supersession_order": list(supersession_order),
            "ownership": {
                "waiting_task_id": str(data.get("task_id", "") or ""),
                "waiting_session_id": str(data.get("session_id", "") or ""),
                "root_session_id": "root-1",
                "ui_anchor_session_id": "root-1",
            },
        }
        if prefill_decision:
            interaction["decision"] = {
                "value": {"option_id": "approve"},
            }
        payload["interaction"] = interaction
        return PreparedOwnerInteractionPublication(
            checkpoint=ExecutionCheckpoint(
                checkpoint_id=checkpoint_id,
                project_id=str(data.get("project_id", "") or "default"),
                session_id=data.get("session_id"),
                checkpoint_type="company_work_item_gate",
                task_id=data.get("task_id"),
                payload=payload,
            ),
            interaction_key=interaction_key,
            supersession_key=supersession_key,
            supersession_order=supersession_order,
        )

    @staticmethod
    def _direct_gate_publication(
        task: Task,
        item: DelegationWorkItem,
        gate: WorkItemGatePolicy,
        *,
        checkpoint_id: str,
        prefill_decision: bool = False,
        additional_task_ids: tuple[str, ...] = (),
    ) -> tuple[
        CompanyControllerAttemptContext,
        Task,
        PreparedOwnerInteractionPublication,
    ]:
        context = CompanyControllerAttemptContext.from_task(
            task,
            work_item_id=item.work_item_id,
        )
        submitted = copy.deepcopy(task)
        submitted.status = TaskStatus.AWAITING_HUMAN
        gate_payload = gate.to_dict()
        gate_attempt = company_work_item_gate_attempt(
            submitted,
            dict(gate_payload.get("metadata", {}) or {}),
        )
        projection_id = projection_id_for_task(submitted)
        data = {
            "checkpoint_type": "company_work_item_gate",
            "project_id": submitted.project_id,
            "session_id": submitted.session_id,
            "task_id": submitted.id,
            "payload": {
                "waiting_task_id": submitted.id,
                "waiting_work_item_id": item.work_item_id,
                "run_id": context.run_id,
                "work_item_projection_id": projection_id,
                "work_item_turn_type": "execute",
                "work_item_attempt_seq": context.attempt_seq,
                "gate_attempt": gate_attempt,
                "gate": gate_payload,
                "prompt": gate.instructions,
                "source_event_id": (
                    f"work-item-gate:{context.run_id}:{projection_id}:"
                    f"{gate.gate_type}:{gate_attempt}"
                ),
                "basis_hash": company_work_item_gate_basis_hash(
                    submitted,
                    gate_payload,
                ),
                "task_ids": [submitted.id, *additional_task_ids],
                "company_work_item_plan": copy.deepcopy(
                    dict(submitted.metadata.get("company_work_item_plan", {}) or {})
                ),
            },
        }
        publication = (
            CompanyControllerAuthoritativeCommitTests._prepared_gate_publication_from_data(
                data,
                checkpoint_id=checkpoint_id,
                prefill_decision=prefill_decision,
            )
        )
        return context, submitted, publication

    async def _prepare_gate_source(
        self,
        *,
        suffix: str,
        kind: str = "execute",
        task_metadata: dict | None = None,
    ) -> tuple[DelegationWorkItem, Task, WorkItemGatePolicy]:
        gate = WorkItemGatePolicy(
            gate_type="human_confirmation",
            instructions="Confirm this work-item result.",
            requires_human=True,
            on_reject="halt",
            max_retries=1,
        )
        item = self._runtime_item(
            f"wi-gate-{suffix}",
            kind=kind,
        )
        task = self._runtime_task(
            f"task-gate-{suffix}",
            item.work_item_id,
            metadata={
                "work_kind": kind,
                "work_item_turn_type": kind,
                "work_item_gate": gate.to_dict(),
                **dict(task_metadata or {}),
            },
        )
        if not dict(task.metadata.get("company_work_item_plan", {}) or {}):
            task.metadata["company_work_item_plan"] = (
                CompanyWorkItemRuntimePlan(
                    root_projection_id=item.work_item_id,
                    projections=[
                        WorkItemProjectionSpec(
                            projection_id=item.work_item_id,
                            turn_type=kind,
                            role_id=item.role_id,
                            title=item.title,
                            summary=item.summary,
                            team_id=item.team_id,
                            seat_id=item.seat_id,
                            manager_role_id=item.manager_role_id,
                            manager_seat_id=item.manager_seat_id,
                            gate_policy=gate,
                        )
                    ],
                ).to_dict()
            )
        task, _generation = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        return item, task, gate

    async def test_work_item_gate_publication_uses_durable_link_and_reconciles_crash_claims(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(suffix="positive")
        role_session_id = "role-session::owner-a"
        await self.store1.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="run-1",
                project_id="project-1",
                role_id=item.role_id,
                seat_id=item.seat_id,
                focused_work_item_id=item.work_item_id,
                status="running",
            ),
            controller_owner_token="owner-a",
            controller_lease_generation=int(
                task.metadata["company_run_controller_lease_generation"]
            ),
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-positive",
        )

        checkpoint, created, persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=(
                    company_controller_task_preimage_hash(task)
                ),
                progress_messages=("Awaiting human confirmation.",),
            )
        )

        self.assertTrue(created)
        self.assertEqual(checkpoint.status, "pending")
        self.assertEqual(persisted_task.status, TaskStatus.AWAITING_HUMAN)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(durable_item.claimed_by_role_runtime_session_id, "")
        self.assertEqual(durable_item.claimed_by_seat_id, "")
        self.assertIs(durable_item.metadata.get("attempt_settled"), True)

        # Simulate a crash immediately after the atomic commit: the normal
        # runtime claim-finalizer never cleared its serial-session focus.
        before = await self.store2.get_delegation_role_session(role_session_id)
        assert before is not None
        self.assertEqual(before.focused_work_item_id, item.work_item_id)
        repaired = await reconcile_role_serial_queues(
            self.store2,
            "run-1",
            project_id="project-1",
            controller_owner_token=context.owner_token,
            controller_lease_generation=context.generation,
        )
        self.assertIn(role_session_id, repaired["cleared_focus_session_ids"])
        after = await self.store2.get_delegation_role_session(role_session_id)
        assert after is not None
        self.assertEqual(after.focused_work_item_id, "")
        self.assertEqual(after.status, "idle")

    async def test_orphan_gate_repair_rejects_unstamped_terminal_checkpoint(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="orphan-unstamped"
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-orphan-unstamped",
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        payload = copy.deepcopy(dict(checkpoint.payload or {}))
        payload.pop("company_work_item_gate_publication_provenance", None)
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET status = 'invalid', payload = ? "
            "WHERE checkpoint_id = ?",
            (json.dumps(payload, ensure_ascii=False), checkpoint.checkpoint_id),
        )
        await self.store1._db.commit()

        repaired = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )

        self.assertEqual(repaired, ())
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)

    async def test_orphan_gate_repair_accepts_exact_stamped_terminal_checkpoint(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="orphan-stamped"
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-orphan-stamped",
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET status = 'invalid' "
            "WHERE checkpoint_id = ?",
            (checkpoint.checkpoint_id,),
        )
        await self.store1._db.commit()

        repaired = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )

        self.assertEqual(len(repaired), 1)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.FAILED)
        self.assertEqual(durable_item.phase, Phase.FAILED)
        marker = dict(
            durable_task.metadata.get(
                "company_work_item_gate_continuation", {}
            )
            or {}
        )
        self.assertEqual(marker.get("kind"), "failure_settlement")
        self.assertEqual(marker.get("status"), "pending")

    async def test_orphan_gate_repair_selects_current_stamp_among_historical_invalid_rows(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="orphan-current-among-history",
            task_metadata={"gate_rework_count": 1},
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-orphan-current",
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        historical_task = copy.deepcopy(submitted)
        historical_task.metadata = dict(historical_task.metadata or {})
        historical_task.metadata["gate_rework_count"] = 0
        historical_payload = copy.deepcopy(dict(checkpoint.payload or {}))
        historical_payload["gate_attempt"] = 0
        historical_payload["source_event_id"] = str(
            historical_payload.get("source_event_id", "") or ""
        ).rsplit(":", 1)[0] + ":0"
        historical_payload["basis_hash"] = company_work_item_gate_basis_hash(
            historical_task,
            dict(historical_payload.get("gate", {}) or {}),
        )
        historical_provenance = dict(
            historical_payload[
                "company_work_item_gate_publication_provenance"
            ]
        )
        historical_provenance.update(
            {
                "checkpoint_id": "checkpoint-gate-orphan-historical",
                "gate_attempt": 0,
                "basis_hash": historical_payload["basis_hash"],
                "source_event_id": historical_payload["source_event_id"],
                "publication_id": "historical-publication",
            }
        )
        historical_provenance.pop("identity_sha256", None)
        historical_provenance["identity_sha256"] = hashlib.sha256(
            json.dumps(
                historical_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        historical_payload[
            "company_work_item_gate_publication_provenance"
        ] = historical_provenance
        recorded_at = datetime.now().isoformat()
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET status = 'invalid' "
            "WHERE checkpoint_id = ?",
            (checkpoint.checkpoint_id,),
        )
        await self.store1._db.execute(
            """INSERT INTO execution_checkpoints
               (checkpoint_id, project_id, session_id, checkpoint_type, status,
                task_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "checkpoint-gate-orphan-historical",
                "project-1",
                "root-1",
                "company_work_item_gate",
                "invalid",
                task.id,
                json.dumps(historical_payload, ensure_ascii=False),
                recorded_at,
                recorded_at,
            ),
        )
        await self.store1._db.commit()

        repaired = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )

        self.assertEqual(
            [entry.checkpoint_id for entry in repaired],
            [checkpoint.checkpoint_id],
        )

    async def test_orphan_gate_repair_ignores_unrelated_active_owner_card(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="orphan-unrelated-active-card"
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-orphan-unrelated-active",
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET status = 'invalid' "
            "WHERE checkpoint_id = ?",
            (checkpoint.checkpoint_id,),
        )
        await self.store1._db.commit()
        unrelated_key = "unrelated-active-owner-card"
        _unrelated, created = await self.store1.create_owner_interaction_checkpoint(
            ExecutionCheckpoint(
                checkpoint_id="checkpoint-unrelated-active-owner-card",
                project_id="project-1",
                session_id="root-1",
                checkpoint_type="company_staffing_selection",
                task_id=task.id,
                payload={
                    "interaction": {
                        "kind": "company_staffing_selection",
                        "domain_key": unrelated_key,
                        "ownership": {
                            "waiting_task_id": task.id,
                            "waiting_session_id": "root-1",
                            "root_session_id": "root-1",
                            "ui_anchor_session_id": "root-1",
                        },
                    }
                },
            ),
            interaction_key=unrelated_key,
        )
        self.assertTrue(created)

        repaired = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )

        self.assertEqual(
            [entry.checkpoint_id for entry in repaired],
            [checkpoint.checkpoint_id],
        )

    async def test_orphan_gate_repair_only_defers_to_exact_newer_gate_envelope(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="orphan-exact-newer-replacement"
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-orphan-old-terminal",
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        durable_task = await self.store1.get_task(task.id)
        assert durable_task is not None
        replacement_task = copy.deepcopy(durable_task)
        replacement_task.metadata = dict(replacement_task.metadata or {})
        replacement_task.metadata.update(
            {
                "gate_rework_count": 1,
                "claimed_work_item_attempt_seq": context.attempt_seq,
            }
        )
        replacement_payload = copy.deepcopy(dict(checkpoint.payload or {}))
        replacement_payload["gate_attempt"] = 1
        replacement_payload["source_event_id"] = str(
            replacement_payload.get("source_event_id", "") or ""
        ).rsplit(":", 1)[0] + ":1"
        replacement_payload["basis_hash"] = company_work_item_gate_basis_hash(
            replacement_task,
            dict(replacement_payload.get("gate", {}) or {}),
        )
        replacement_interaction = dict(
            replacement_payload.get("interaction", {}) or {}
        )
        replacement_interaction.update(
            {
                "domain_key": "newer-exact-gate-domain",
                "supersession_order": [context.attempt_seq, 1],
            }
        )
        replacement_payload["interaction"] = replacement_interaction
        replacement_provenance = dict(
            replacement_payload[
                "company_work_item_gate_publication_provenance"
            ]
        )
        replacement_provenance.update(
            {
                "checkpoint_id": "checkpoint-gate-orphan-newer-active",
                "gate_attempt": 1,
                "basis_hash": replacement_payload["basis_hash"],
                "source_event_id": replacement_payload["source_event_id"],
                "publication_id": "newer-exact-publication",
            }
        )
        replacement_provenance.pop("identity_sha256", None)
        replacement_provenance["identity_sha256"] = hashlib.sha256(
            json.dumps(
                replacement_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        replacement_payload[
            "company_work_item_gate_publication_provenance"
        ] = replacement_provenance
        recorded_at = datetime.now().isoformat()
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET status = 'invalid' "
            "WHERE checkpoint_id = ?",
            (checkpoint.checkpoint_id,),
        )
        await self.store1._db.execute(
            "UPDATE tasks SET metadata = ? WHERE id = ?",
            (
                json.dumps(replacement_task.metadata, ensure_ascii=False),
                task.id,
            ),
        )
        await self.store1._db.execute(
            """INSERT INTO execution_checkpoints
               (checkpoint_id, project_id, session_id, checkpoint_type, status,
                task_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "checkpoint-gate-orphan-newer-active",
                "project-1",
                "root-1",
                "company_work_item_gate",
                "pending",
                task.id,
                json.dumps(replacement_payload, ensure_ascii=False),
                recorded_at,
                recorded_at,
            ),
        )
        await self.store1._db.commit()
        self.assertIsNotNone(
            await self.store1.get_company_work_item_gate_publication_provenance(
                "checkpoint-gate-orphan-newer-active",
                project_id="project-1",
            )
        )
        self.assertTrue(
            await self.store1._company_work_item_gate_runtime_scope_matches(
                self.store1._db,
                payload=replacement_payload,
                source_task=replacement_task,
                run_id="run-1",
                source_projection_id=projection_id_for_task(replacement_task),
            )
        )

        blocked_by_exact_replacement = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )
        self.assertEqual(blocked_by_exact_replacement, ())

        replacement_provenance["identity_sha256"] = "tampered"
        replacement_payload[
            "company_work_item_gate_publication_provenance"
        ] = replacement_provenance
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET payload = ? "
            "WHERE checkpoint_id = ?",
            (
                json.dumps(replacement_payload, ensure_ascii=False),
                "checkpoint-gate-orphan-newer-active",
            ),
        )
        await self.store1._db.commit()

        repaired = await self.store1.reconcile_orphaned_company_work_item_gate_waits_for_controller(
            "run-1",
            project_id="project-1",
            owner_token=context.owner_token,
            generation=context.generation,
        )
        self.assertEqual(
            [entry.checkpoint_id for entry in repaired],
            [checkpoint.checkpoint_id],
        )

    async def _publish_claimed_gate(
        self,
        *,
        suffix: str,
        action: str,
        gate: WorkItemGatePolicy | None = None,
        kind: str = "execute",
        task_metadata: dict | None = None,
        additional_task_ids: tuple[str, ...] = (),
    ) -> tuple[
        CompanyControllerAttemptContext,
        CompanyWorkItemGateDecisionCommand,
        ExecutionCheckpoint,
        DelegationWorkItem,
        Task,
    ]:
        item, task, default_gate = await self._prepare_gate_source(
            suffix=suffix,
            kind=kind,
            task_metadata=task_metadata,
        )
        selected_gate = gate or default_gate
        task.metadata["work_item_gate"] = selected_gate.to_dict()
        await self.store1.save_task(task)
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            selected_gate,
            checkpoint_id=f"checkpoint-gate-decision-{suffix}",
            additional_task_ids=additional_task_ids,
        )
        checkpoint, _created, _persisted_task = (
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )
        )
        decision = {"option_id": action}
        accepted = await self.store1.accept_execution_checkpoint_decision(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
            request_id=f"request-{suffix}",
            decision_hash=hashlib.sha256(
                json.dumps(decision, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            decision=decision,
        )
        self.assertTrue(accepted.acknowledged)
        claimed = await self.store1.claim_answered_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
            consumer_id=f"consumer-{suffix}",
            claim_id=f"claim-{suffix}",
            lease_seconds=300,
        )
        self.assertTrue(claimed.acquired)
        payload = dict(checkpoint.payload or {})
        command = CompanyWorkItemGateDecisionCommand(
            checkpoint_id=checkpoint.checkpoint_id,
            project_id="project-1",
            claim_id=f"claim-{suffix}",
            consumer_id=f"consumer-{suffix}",
            run_id=str(payload["run_id"]),
            task_id=str(payload["waiting_task_id"]),
            work_item_id=str(payload["waiting_work_item_id"]),
            attempt_seq=int(payload["work_item_attempt_seq"]),
            gate_attempt=int(payload["gate_attempt"]),
            basis_hash=str(payload["basis_hash"]),
            action=action,
            feedback=action,
        )
        assert claimed.checkpoint is not None
        return context, command, claimed.checkpoint, item, submitted

    async def test_work_item_gate_decision_approve_is_atomic_and_idempotent(
        self,
    ) -> None:
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="approve",
            action="approve",
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )
        duplicate = await self.store2.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        self.assertEqual(applied.target_phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(duplicate.outcome, "duplicate")
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_task is not None and durable_item is not None
        assert durable_checkpoint is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_MANAGER_REVIEW)
        self.assertEqual(durable_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(durable_checkpoint.status, "resolved")
        completion = dict(
            durable_checkpoint.payload.get(
                "company_work_item_gate_decision_completion", {}
            )
            or {}
        )
        self.assertEqual(completion.get("claim_id"), command.claim_id)
        self.assertEqual(completion.get("basis_hash"), command.basis_hash)

    async def test_work_item_gate_decision_approve_final_delivery_marks_finalize(
        self,
    ) -> None:
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="approve-final",
            action="approve",
            kind="deliver",
            task_metadata={
                "work_kind": "deliver",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        self.assertEqual(applied.target_phase, Phase.AWAITING_HUMAN)
        self.assertEqual(applied.continuation, "finalize")
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        marker = dict(
            durable_task.metadata.get(
                "company_work_item_gate_continuation", {}
            )
            or {}
        )
        self.assertEqual(marker.get("kind"), "finalize")
        self.assertEqual(marker.get("status"), "pending")

    async def test_work_item_gate_decision_deny_halt_is_atomic(self) -> None:
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="deny-halt",
            action="deny",
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        self.assertEqual(applied.target_phase, Phase.FAILED)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_task is not None and durable_item is not None
        assert durable_checkpoint is not None
        self.assertEqual(durable_task.status, TaskStatus.FAILED)
        self.assertEqual(durable_item.phase, Phase.FAILED)
        self.assertEqual(durable_checkpoint.status, "resolved")

    async def test_work_item_gate_stale_observability_claim_mirrors_do_not_gate(
        self,
    ) -> None:
        context, command, _checkpoint, item, _task = (
            await self._publish_claimed_gate(
                suffix="stale-observability-mirrors",
                action="approve",
            )
        )
        parked_item = await self.store1.get_delegation_work_item(
            item.work_item_id
        )
        assert parked_item is not None
        parked_item.metadata = dict(parked_item.metadata or {})
        parked_item.metadata.update(
            {
                "claimed_by_role_session_id": "stale-observability-only",
                "claimed_task_id": "stale-observability-only",
            }
        )
        await self.store1.save_delegation_work_item(parked_item)

        applied = (
            await self.store1.apply_company_work_item_gate_decision_for_controller(
                context,
                command,
            )
        )

        self.assertTrue(applied.applied)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_item is not None
        self.assertEqual(durable_item.phase, Phase.AWAITING_MANAGER_REVIEW)

    async def test_work_item_gate_scope_failure_settlement_is_atomic_and_idempotent(
        self,
    ) -> None:
        context, command, _checkpoint, source_item, _source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-settlement",
                action="approve",
            )
        )
        sibling_item = self._runtime_item("wi-scope-failure-sibling")
        sibling_task = self._runtime_task(
            "task-scope-failure-sibling",
            sibling_item.work_item_id,
        )
        await self.store1.save_delegation_work_item(sibling_item)
        await self.store1.save_task(sibling_task)
        await self.store1.link_work_item_runtime_task(
            sibling_item.work_item_id,
            sibling_task.id,
        )
        invalidated = (
            await self.store1.invalidate_company_work_item_gate_decision_for_controller(
                context,
                command,
                conflict_reason="source_basis_mismatch",
            )
        )
        self.assertTrue(invalidated.applied)

        settled = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="invalid_company_work_item_gate",
        )
        after_first = await self.store2.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        self.assertTrue(settled)
        assert after_first is not None

        duplicate = await self.store2.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="invalid_company_work_item_gate",
        )
        after_duplicate = await self.store1.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )

        self.assertTrue(duplicate)
        assert after_duplicate is not None
        self.assertEqual(after_duplicate.updated_at, after_first.updated_at)
        run = await self.store1.get_delegation_run("run-1")
        durable_source = await self.store1.get_delegation_work_item(
            source_item.work_item_id
        )
        durable_sibling = await self.store1.get_delegation_work_item(
            sibling_item.work_item_id
        )
        durable_sibling_task = await self.store1.get_task(sibling_task.id)
        assert run is not None
        assert durable_source is not None and durable_sibling is not None
        assert durable_sibling_task is not None
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(durable_source.phase, Phase.FAILED)
        self.assertEqual(durable_sibling.phase, Phase.CANCELLED)
        self.assertEqual(durable_sibling_task.status, TaskStatus.CANCELLED)
        completion = dict(
            after_duplicate.payload.get(
                "company_work_item_gate_decision_completion", {}
            )
            or {}
        )
        self.assertEqual(completion.get("continuation_status"), "completed")
        failure_reviews = await self.store1.get_execution_checkpoints(
            project_id="project-1",
            checkpoint_types=["company_run_failure_review"],
        )
        self.assertEqual(len(failure_reviews), 1)
        self.assertEqual(
            failure_reviews[0].payload.get("run_id"),
            "run-1",
        )

    async def test_work_item_gate_scope_failure_settlement_survives_missing_source_task(
        self,
    ) -> None:
        context, command, _checkpoint, source_item, source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-missing-task",
                action="approve",
            )
        )
        invalidated = (
            await self.store1.invalidate_company_work_item_gate_decision_for_controller(
                context,
                command,
                conflict_reason="source_basis_mismatch",
            )
        )
        self.assertTrue(invalidated.applied)
        assert self.store1._db is not None
        await self.store1._db.execute(
            "DELETE FROM tasks WHERE id = ?",
            (source_task.id,),
        )
        await self.store1._db.commit()

        settled = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="invalid_company_work_item_gate",
        )

        self.assertTrue(settled)
        run = await self.store2.get_delegation_run("run-1")
        durable_source = await self.store2.get_delegation_work_item(
            source_item.work_item_id
        )
        assert run is not None and durable_source is not None
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(durable_source.phase, Phase.FAILED)
        failure_reviews = await self.store2.get_execution_checkpoints(
            project_id="project-1",
            checkpoint_types=["company_run_failure_review"],
        )
        self.assertEqual(len(failure_reviews), 1)

    async def test_work_item_gate_scope_failure_settlement_survives_missing_runtime_link(
        self,
    ) -> None:
        context, command, _checkpoint, source_item, source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-missing-link",
                action="approve",
            )
        )
        invalidated = (
            await self.store1.invalidate_company_work_item_gate_decision_for_controller(
                context,
                command,
                conflict_reason="source_basis_mismatch",
            )
        )
        self.assertTrue(invalidated.applied)
        assert self.store1._db is not None
        await self.store1._db.execute(
            "DELETE FROM work_item_runtime_links WHERE runtime_task_id = ?",
            (source_task.id,),
        )
        await self.store1._db.commit()

        settled = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="invalid_company_work_item_gate",
        )

        self.assertTrue(settled)
        run = await self.store2.get_delegation_run("run-1")
        durable_source = await self.store2.get_delegation_work_item(
            source_item.work_item_id
        )
        durable_task = await self.store2.get_task(source_task.id)
        assert run is not None and durable_source is not None
        assert durable_task is not None
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(durable_source.phase, Phase.FAILED)
        self.assertEqual(durable_task.status, TaskStatus.FAILED)

    async def test_work_item_gate_scope_failure_rejects_tampered_store_provenance(
        self,
    ) -> None:
        context, command, _checkpoint, source_item, _source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-tampered-provenance",
                action="approve",
            )
        )
        invalidated = (
            await self.store1.invalidate_company_work_item_gate_decision_for_controller(
                context,
                command,
                conflict_reason="source_basis_mismatch",
            )
        )
        self.assertTrue(invalidated.applied)
        durable_checkpoint = await self.store1.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_checkpoint is not None
        tampered_payload = copy.deepcopy(dict(durable_checkpoint.payload or {}))
        provenance = dict(
            tampered_payload.get(
                "company_work_item_gate_publication_provenance", {}
            )
            or {}
        )
        provenance["identity_sha256"] = "tampered"
        tampered_payload[
            "company_work_item_gate_publication_provenance"
        ] = provenance
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE execution_checkpoints SET payload = ? WHERE checkpoint_id = ?",
            (
                json.dumps(tampered_payload, ensure_ascii=False),
                command.checkpoint_id,
            ),
        )
        await self.store1._db.commit()

        settled = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="invalid_company_work_item_gate",
        )

        self.assertFalse(settled)
        run = await self.store2.get_delegation_run("run-1")
        durable_source = await self.store2.get_delegation_work_item(
            source_item.work_item_id
        )
        assert run is not None and durable_source is not None
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(durable_source.phase, Phase.FAILED)
        failure_reviews = await self.store2.get_execution_checkpoints(
            project_id="project-1",
            checkpoint_types=["company_run_failure_review"],
        )
        self.assertEqual(failure_reviews, [])

    async def test_work_item_gate_scope_failure_requires_exact_pending_marker(
        self,
    ) -> None:
        context, command, _checkpoint, _source_item, source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-marker-identity",
                action="approve",
            )
        )
        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )
        self.assertTrue(applied.applied)

        healthy_refused = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="postcommit_marker_identity_mismatch",
        )
        self.assertFalse(healthy_refused)

        durable_task = await self.store1.get_task(source_task.id)
        assert durable_task is not None
        task_metadata = copy.deepcopy(dict(durable_task.metadata or {}))
        marker = dict(
            task_metadata.get("company_work_item_gate_continuation", {}) or {}
        )
        marker.update(
            {
                "status": "completed",
                "checkpoint_id": "forged-completed-marker",
            }
        )
        task_metadata["company_work_item_gate_continuation"] = marker
        assert self.store1._db is not None
        await self.store1._db.execute(
            "UPDATE tasks SET metadata = ? WHERE id = ?",
            (json.dumps(task_metadata, ensure_ascii=False), source_task.id),
        )
        await self.store1._db.commit()

        settled = await self.store1.settle_company_work_item_gate_failure_for_controller(
            command.checkpoint_id,
            project_id="project-1",
            run_id="run-1",
            owner_token=context.owner_token,
            generation=context.generation,
            conflict_reason="postcommit_marker_identity_mismatch",
        )

        self.assertTrue(settled)
        run = await self.store2.get_delegation_run("run-1")
        assert run is not None
        self.assertEqual(run.lifecycle_status, "closed_failed")

    async def test_work_item_gate_scope_failure_review_fault_rolls_back_everything(
        self,
    ) -> None:
        context, command, _checkpoint, _source_item, _source_task = (
            await self._publish_claimed_gate(
                suffix="scope-failure-rollback",
                action="approve",
            )
        )
        sibling_item = self._runtime_item("wi-scope-failure-rollback-sibling")
        sibling_task = self._runtime_task(
            "task-scope-failure-rollback-sibling",
            sibling_item.work_item_id,
        )
        await self.store1.save_delegation_work_item(sibling_item)
        await self.store1.save_task(sibling_task)
        await self.store1.link_work_item_runtime_task(
            sibling_item.work_item_id,
            sibling_task.id,
        )
        invalidated = (
            await self.store1.invalidate_company_work_item_gate_decision_for_controller(
                context,
                command,
                conflict_reason="source_basis_mismatch",
            )
        )
        self.assertTrue(invalidated.applied)
        assert self.store1._db is not None
        await self.store1._db.execute(
            """CREATE TRIGGER fail_gate_failure_review_insert
               BEFORE INSERT ON execution_checkpoints
               WHEN NEW.checkpoint_type = 'company_run_failure_review'
               BEGIN
                   SELECT RAISE(ABORT, 'simulated failure review fault');
               END"""
        )
        await self.store1._db.commit()

        with self.assertRaises(Exception):
            await self.store1.settle_company_work_item_gate_failure_for_controller(
                command.checkpoint_id,
                project_id="project-1",
                run_id="run-1",
                owner_token=context.owner_token,
                generation=context.generation,
                conflict_reason="invalid_company_work_item_gate",
            )

        run = await self.store2.get_delegation_run("run-1")
        durable_sibling = await self.store2.get_delegation_work_item(
            sibling_item.work_item_id
        )
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            command.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert run is not None and durable_sibling is not None
        assert durable_checkpoint is not None
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(durable_sibling.phase, Phase.READY)
        self.assertNotIn(
            "company_work_item_gate_failure_settlement",
            durable_checkpoint.payload,
        )

    async def test_work_item_gate_decision_deny_rework_cap_fails_source(self) -> None:
        gate = WorkItemGatePolicy(
            gate_type="human_confirmation",
            instructions="No more than one correction.",
            requires_human=True,
            on_reject="rework",
            rework_projection_id="wi-gate-deny-cap",
            max_retries=1,
        )
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="deny-cap",
            action="deny",
            gate=gate,
            task_metadata={"gate_rework_count": 1},
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        self.assertEqual(applied.target_phase, Phase.FAILED)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.FAILED)
        self.assertEqual(durable_item.phase, Phase.FAILED)

    async def test_work_item_gate_decision_deny_self_rework_is_atomic(self) -> None:
        gate = WorkItemGatePolicy(
            gate_type="human_confirmation",
            instructions="Fix the result.",
            requires_human=True,
            on_reject="rework",
            rework_projection_id="wi-gate-deny-self",
            max_retries=2,
        )
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="deny-self",
            action="deny",
            gate=gate,
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        self.assertEqual(applied.target_phase, Phase.READY_FOR_REWORK)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.PENDING)
        self.assertEqual(durable_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(durable_item.metadata.get("gate_rework_count"), 1)
        self.assertEqual(
            durable_item.metadata.get("last_gate_review_feedback"),
            "deny",
        )

    async def test_work_item_gate_decision_deny_cross_target_is_atomic(self) -> None:
        target_item = self._runtime_item(
            "wi-gate-cross-target",
            phase=Phase.APPROVED,
            metadata={"attempt_seq": 3, "attempt_settled": True},
        )
        target_task = self._runtime_task(
            "task-gate-cross-target",
            target_item.work_item_id,
        )
        target_task.status = TaskStatus.DONE
        target_task.result = {"content": "old target output"}
        await self.store1.save_delegation_work_item(target_item)
        await self.store1.save_task(target_task)
        await self.store1.link_work_item_runtime_task(
            target_item.work_item_id,
            target_task.id,
        )
        gate = WorkItemGatePolicy(
            gate_type="human_confirmation",
            instructions="Rework the upstream target.",
            requires_human=True,
            on_reject="rework",
            rework_projection_id=target_item.projection_id,
            max_retries=2,
        )
        runtime_plan = CompanyWorkItemRuntimePlan(
            root_projection_id="wi-gate-deny-cross",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="wi-gate-deny-cross",
                    turn_type="execute",
                    role_id="analyst",
                    title="wi-gate-deny-cross",
                    summary="initial summary",
                    team_id="team::analysis",
                    seat_id="seat::analyst",
                    manager_role_id="manager",
                    manager_seat_id="seat::manager",
                    gate_policy=gate,
                ),
                WorkItemProjectionSpec(
                    projection_id=target_item.projection_id,
                    turn_type="execute",
                    role_id=target_item.role_id,
                    title=target_item.title,
                    summary=target_item.summary,
                    team_id=target_item.team_id,
                    seat_id=target_item.seat_id,
                    manager_role_id=target_item.manager_role_id,
                    manager_seat_id=target_item.manager_seat_id,
                ),
            ],
        )
        context, command, _checkpoint, source_item, source_task = (
            await self._publish_claimed_gate(
                suffix="deny-cross",
                action="deny",
                gate=gate,
                task_metadata={
                    "company_work_item_plan": runtime_plan.to_dict(),
                },
                additional_task_ids=(target_task.id,),
            )
        )

        applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            command,
        )

        self.assertTrue(applied.applied)
        durable_source_task = await self.store2.get_task(source_task.id)
        durable_source_item = await self.store2.get_delegation_work_item(
            source_item.work_item_id
        )
        durable_target_task = await self.store2.get_task(target_task.id)
        durable_target_item = await self.store2.get_delegation_work_item(
            target_item.work_item_id
        )
        assert durable_source_task is not None and durable_source_item is not None
        assert durable_target_task is not None and durable_target_item is not None
        self.assertEqual(durable_source_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(durable_target_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(durable_source_task.status, TaskStatus.PENDING)
        self.assertEqual(durable_target_task.status, TaskStatus.PENDING)
        self.assertIsNone(durable_target_task.result)
        self.assertEqual(
            durable_target_item.metadata.get("gate_review_feedback"),
            "deny",
        )

    async def test_work_item_gate_decision_concurrent_consumers_single_effect(
        self,
    ) -> None:
        context, command, _checkpoint, item, _task = await self._publish_claimed_gate(
            suffix="concurrent",
            action="approve",
        )

        first, second = await asyncio.gather(
            self.store1.apply_company_work_item_gate_decision_for_controller(
                context,
                command,
            ),
            self.store2.apply_company_work_item_gate_decision_for_controller(
                context,
                command,
            ),
        )

        self.assertEqual(
            sorted([first.outcome, second.outcome]),
            ["applied", "duplicate"],
        )
        durable_item = await self.store1.get_delegation_work_item(item.work_item_id)
        assert durable_item is not None
        self.assertEqual(durable_item.phase, Phase.AWAITING_MANAGER_REVIEW)

    async def test_work_item_gate_decision_release_then_generation_two_consumes(
        self,
    ) -> None:
        old_context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="generation-two",
            action="approve",
        )
        self.assertTrue(
            await self.store1.release_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token=old_context.owner_token,
                generation=old_context.generation,
            )
        )
        admitted = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(admitted.acquired)
        new_context = replace(
            old_context,
            owner_token="owner-b",
            generation=admitted.generation,
        )

        applied = await self.store2.apply_company_work_item_gate_decision_for_controller(
            new_context,
            command,
        )

        self.assertTrue(applied.applied)
        durable_task = await self.store1.get_task(task.id)
        durable_item = await self.store1.get_delegation_work_item(item.work_item_id)
        assert durable_task is not None and durable_item is not None
        self.assertEqual(
            durable_task.metadata.get("company_run_controller_owner_token"),
            "owner-b",
        )
        self.assertEqual(
            durable_item.metadata.get("company_run_controller_lease_generation"),
            admitted.generation,
        )

    async def test_work_item_gate_decision_controller_takeover_is_zero_write(
        self,
    ) -> None:
        old_context, command, checkpoint, item, task = await self._publish_claimed_gate(
            suffix="takeover",
            action="approve",
        )
        self.assertTrue(
            await self.store1.release_delegation_run_controller_lease(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token=old_context.owner_token,
                generation=old_context.generation,
            )
        )
        admitted = await self.store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            lease_seconds=60,
        )
        self.assertTrue(admitted.acquired)

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.apply_company_work_item_gate_decision_for_controller(
                old_context,
                command,
            )

        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_task is not None and durable_item is not None
        assert durable_checkpoint is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(durable_checkpoint.status, "consuming")

    async def test_work_item_gate_decision_basis_mismatch_is_zero_write(self) -> None:
        context, command, checkpoint, item, task = await self._publish_claimed_gate(
            suffix="basis-mismatch",
            action="approve",
        )
        stale = replace(command, basis_hash="wrong-basis")

        result = await self.store1.apply_company_work_item_gate_decision_for_controller(
            context,
            stale,
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.conflict_reason, "checkpoint_domain_mismatch")
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_task is not None and durable_item is not None
        assert durable_checkpoint is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(durable_checkpoint.status, "consuming")

    async def test_work_item_gate_decision_fault_rolls_back_all_rows(self) -> None:
        context, command, checkpoint, item, task = await self._publish_claimed_gate(
            suffix="fault-rollback",
            action="approve",
        )
        original = (
            self.store1._complete_company_work_item_gate_checkpoint_in_transaction
        )

        async def fail_after_checkpoint(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("simulated gate commit crash")

        self.store1._complete_company_work_item_gate_checkpoint_in_transaction = (
            fail_after_checkpoint
        )
        with self.assertRaisesRegex(RuntimeError, "simulated gate commit crash"):
            await self.store1.apply_company_work_item_gate_decision_for_controller(
                context,
                command,
            )

        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        durable_checkpoint = await self.store2.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="company_work_item_gate",
        )
        assert durable_task is not None and durable_item is not None
        assert durable_checkpoint is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(durable_checkpoint.status, "consuming")

    async def test_work_item_gate_decision_postcommit_notification_is_best_effort(
        self,
    ) -> None:
        context, command, _checkpoint, item, task = await self._publish_claimed_gate(
            suffix="notify-fault",
            action="approve",
        )

        with patch(
            "opc.database.store.on_phase_transition",
            new=AsyncMock(side_effect=RuntimeError("notify failed")),
        ):
            applied = await self.store1.apply_company_work_item_gate_decision_for_controller(
                context,
                command,
            )

        self.assertTrue(applied.applied)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(item.work_item_id)
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_MANAGER_REVIEW)
        self.assertEqual(durable_item.phase, Phase.AWAITING_MANAGER_REVIEW)

    async def test_work_item_gate_publication_wrong_runtime_link_is_zero_write(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(suffix="wrong-link")
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-wrong-link",
        )
        other = self._runtime_item("wi-gate-wrong-link-other")
        await self.store1.save_delegation_work_item(other)
        self.assertTrue(
            await self.store1.link_work_item_runtime_task(
                other.work_item_id,
                task.id,
                allow_replace=True,
            )
        )

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=(
                    company_controller_task_preimage_hash(task)
                ),
            )

        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.RUNNING)
        self.assertEqual(durable_item.phase, Phase.RUNNING)
        self.assertEqual(
            durable_item.claimed_by_role_runtime_session_id,
            "role-session::owner-a",
        )
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                publication.checkpoint.checkpoint_id,
                project_id="project-1",
                checkpoint_type="company_work_item_gate",
            )
        )

    async def test_work_item_gate_publication_rolls_back_pause_when_insert_fails(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(suffix="rollback")
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-rollback",
            prefill_decision=True,
        )

        with self.assertRaises(ValueError):
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=(
                    company_controller_task_preimage_hash(task)
                ),
                progress_messages=("This must roll back.",),
            )

        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.RUNNING)
        self.assertEqual(durable_item.phase, Phase.RUNNING)
        self.assertEqual(
            durable_item.claimed_by_role_runtime_session_id,
            "role-session::owner-a",
        )
        self.assertIsNot(durable_item.metadata.get("attempt_settled"), True)
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                publication.checkpoint.checkpoint_id,
                project_id="project-1",
                checkpoint_type="company_work_item_gate",
            )
        )

    async def test_work_item_gate_publication_rejects_unstamped_domain_duplicate(
        self,
    ) -> None:
        item, task, gate = await self._prepare_gate_source(
            suffix="untrusted-domain-duplicate"
        )
        context, submitted, publication = self._direct_gate_publication(
            task,
            item,
            gate,
            checkpoint_id="checkpoint-gate-authoritative-duplicate",
        )
        untrusted_checkpoint = replace(
            publication.checkpoint,
            checkpoint_id="checkpoint-gate-untrusted-duplicate",
        )
        _existing, created = await self.store1.publish_owner_interaction_checkpoint(
            untrusted_checkpoint,
            interaction_key=publication.interaction_key,
            supersession_key=publication.supersession_key,
            supersession_order=publication.supersession_order,
        )
        self.assertTrue(created)

        with self.assertRaisesRegex(
            RuntimeError,
            "authoritative publication basis",
        ):
            await self.store1.publish_company_work_item_gate_checkpoint_for_controller(
                context,
                publication,
                task_snapshot=submitted,
                expected_task_preimage_hash=company_controller_task_preimage_hash(
                    task
                ),
            )

        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.RUNNING)
        self.assertEqual(durable_item.phase, Phase.RUNNING)
        self.assertEqual(
            durable_item.claimed_by_role_runtime_session_id,
            "role-session::owner-a",
        )
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                publication.checkpoint.checkpoint_id,
                project_id="project-1",
                checkpoint_type="company_work_item_gate",
            )
        )

    async def test_nonreviewable_manager_wait_falls_back_before_write_and_postcommit_errors_are_safe(
        self,
    ) -> None:
        item, task, _gate = await self._prepare_gate_source(
            suffix="manager-fallback",
            kind="aggregate",
            task_metadata={"manager_role_id": "manager"},
        )
        executor = self.executor1
        executor.org_engine = SimpleNamespace(get_agent=lambda _role_id: None)
        executor._active_plan = CompanyWorkItemRuntimePlan.from_dict(
            dict(task.metadata["company_work_item_plan"])
        )
        executor._active_tasks = [task]

        prepare_calls = 0

        async def prepare(data: dict) -> PreparedOwnerInteractionPublication:
            nonlocal prepare_calls
            prepare_calls += 1
            return self._prepared_gate_publication_from_data(
                data,
                checkpoint_id="checkpoint-gate-manager-fallback",
            )

        original_get_task = self.store1.get_task
        get_task_calls = 0

        async def get_task_then_fail_if_reloaded(task_id: str) -> Task | None:
            nonlocal get_task_calls
            get_task_calls += 1
            if get_task_calls > 1:
                raise RuntimeError("post-commit reload must not occur")
            return await original_get_task(task_id)

        self.store1.get_task = get_task_then_fail_if_reloaded  # type: ignore[method-assign]
        executor.checkpoint_prepare_callback = prepare
        executor.checkpoint_notify_callback = AsyncMock(
            side_effect=RuntimeError("notification transport unavailable")
        )
        executor._emit_progress = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("progress transport unavailable")
        )
        decision = GateHarnessDecision(
            action="escalate",
            summary="A manager gate was requested for a non-reviewable turn.",
            blockers=["Owner confirmation is required."],
            blocker_types=["authority"],
        )

        self.assertTrue(
            await executor._commit_pre_done_gate_harness_wait(task, decision)
        )

        self.assertEqual(prepare_calls, 1)
        self.assertEqual(get_task_calls, 1)
        durable_task = await self.store2.get_task(task.id)
        durable_item = await self.store2.get_delegation_work_item(
            item.work_item_id
        )
        pending = await self.store2.get_pending_checkpoints(
            project_id="project-1",
            checkpoint_types=["company_work_item_gate"],
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(durable_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0].payload.get("review_level"),
            "human",
        )
        self.assertEqual(
            durable_task.metadata["gate_harness_review_level"],
            "human",
        )

    async def test_require_input_gate_shapes_preserve_full_envelope_in_owner_wait(
        self,
    ) -> None:
        cases = (
            {
                "name": "reviewer-mismatch",
                "reviewer_role": "different-manager",
                "on_reject": "rework",
                "target": "source",
                "max_retries": 2,
            },
            {
                "name": "halt",
                "reviewer_role": "manager",
                "on_reject": "halt",
                "target": "",
                "max_retries": 1,
            },
            {
                "name": "cross-target",
                "reviewer_role": "manager",
                "on_reject": "rework",
                "target": "different-work-item",
                "max_retries": 3,
            },
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                suffix = str(case["name"])
                item = self._runtime_item(f"wi-gate-{suffix}", kind="execute")
                target = (
                    item.projection_id
                    if case["target"] == "source"
                    else str(case["target"])
                )
                gate = WorkItemGatePolicy(
                    gate_type="approval",
                    instructions=f"Review gate for {suffix}.",
                    reviewer_role=str(case["reviewer_role"]),
                    requires_human=False,
                    on_reject=str(case["on_reject"]),
                    rework_projection_id=target or None,
                    max_retries=int(case["max_retries"]),
                    metadata={"contract_case": suffix},
                )
                task = self._runtime_task(
                    f"task-gate-{suffix}",
                    item.work_item_id,
                    metadata={
                        "work_kind": "execute",
                        "work_item_turn_type": "execute",
                        "manager_role_id": "manager",
                        "work_item_gate": gate.to_dict(),
                    },
                )
                task, _generation = await self._claim(
                    self.store1,
                    item=item,
                    task=task,
                    owner_token="owner-a",
                    expected_phase=Phase.READY,
                )
                scoped_tasks = [task]
                task_by_projection = {item.projection_id: task}
                if case["name"] == "cross-target":
                    target_item = self._runtime_item(
                        "different-work-item",
                        phase=Phase.APPROVED,
                        metadata={"attempt_seq": 1, "attempt_settled": True},
                    )
                    target_task = self._runtime_task(
                        "task-gate-cross-target-owner-wait",
                        target_item.work_item_id,
                    )
                    target_task.status = TaskStatus.DONE
                    await self.store1.save_delegation_work_item(target_item)
                    await self.store1.save_task(target_task)
                    await self.store1.link_work_item_runtime_task(
                        target_item.work_item_id,
                        target_task.id,
                    )
                    runtime_plan = CompanyWorkItemRuntimePlan.from_dict(
                        dict(task.metadata["company_work_item_plan"])
                    )
                    runtime_plan.projections.append(
                        WorkItemProjectionSpec(
                            projection_id=target_item.projection_id,
                            turn_type="execute",
                            role_id=target_item.role_id,
                            title=target_item.title,
                            summary=target_item.summary,
                            team_id=target_item.team_id,
                            seat_id=target_item.seat_id,
                            manager_role_id=target_item.manager_role_id,
                            manager_seat_id=target_item.manager_seat_id,
                        )
                    )
                    task.metadata["company_work_item_plan"] = (
                        runtime_plan.to_dict()
                    )
                    await self.store1.save_task(task)
                    scoped_tasks.append(target_task)
                    task_by_projection[target_item.projection_id] = target_task
                checkpoint_id = f"checkpoint-gate-{suffix}"

                async def prepare(
                    data: dict,
                    *,
                    checkpoint_id: str = checkpoint_id,
                ) -> PreparedOwnerInteractionPublication:
                    return self._prepared_gate_publication_from_data(
                        data,
                        checkpoint_id=checkpoint_id,
                    )

                executor = self.executor1
                executor._active_plan = CompanyWorkItemRuntimePlan.from_dict(
                    dict(task.metadata["company_work_item_plan"])
                )
                executor._active_tasks = scoped_tasks
                executor.checkpoint_prepare_callback = prepare
                executor.approval_engine = SimpleNamespace(
                    authorize_work_item_action=AsyncMock(
                        return_value=(
                            False,
                            SimpleNamespace(
                                action=ApprovalAction.REQUIRE_INPUT
                            ),
                        )
                    )
                )
                executor._emit_progress = AsyncMock()  # type: ignore[method-assign]

                await executor._apply_gate(
                    task,
                    gate,
                    task_by_projection,
                )

                durable_task = await self.store2.get_task(task.id)
                durable_item = await self.store2.get_delegation_work_item(
                    item.work_item_id
                )
                checkpoint = await self.store2.get_execution_checkpoint(
                    checkpoint_id,
                    project_id="project-1",
                    checkpoint_type="company_work_item_gate",
                )
                assert (
                    durable_task is not None
                    and durable_item is not None
                    and checkpoint is not None
                )
                self.assertEqual(
                    durable_task.status,
                    TaskStatus.AWAITING_HUMAN,
                )
                self.assertEqual(durable_item.phase, Phase.AWAITING_HUMAN)
                self.assertEqual(checkpoint.status, "pending")
                self.assertEqual(checkpoint.payload["review_level"], "human")
                checkpoint_gate = dict(checkpoint.payload["gate"])
                self.assertEqual(checkpoint_gate["type"], gate.gate_type)
                self.assertEqual(
                    checkpoint_gate["instructions"],
                    gate.instructions,
                )
                self.assertIsNone(checkpoint_gate["reviewer_role"])
                self.assertIs(checkpoint_gate["requires_human"], True)
                self.assertEqual(
                    checkpoint_gate["on_reject"],
                    gate.on_reject,
                )
                self.assertEqual(
                    checkpoint_gate["rework_projection_id"],
                    gate.rework_projection_id,
                )
                self.assertEqual(
                    checkpoint_gate["max_retries"],
                    gate.max_retries,
                )
                self.assertEqual(
                    checkpoint_gate["metadata"][
                        "manager_gate_original_reviewer_role"
                    ],
                    gate.reviewer_role,
                )
                self.assertTrue(
                    checkpoint_gate["metadata"]["manager_gate_fallback"]
                )
                self.assertEqual(
                    checkpoint.payload["basis_hash"],
                    company_work_item_gate_basis_hash(
                        durable_task,
                        checkpoint_gate,
                    ),
                )

    async def _prepare_origin_delivery_attempt(
        self,
        *,
        suffix: str,
    ) -> tuple[
        DelegationWorkItem,
        Task,
        OriginOwnerInteractionLease,
        CompanyControllerAttemptContext,
    ]:
        item = self._runtime_item(
            f"wi-origin-delivery-{suffix}",
            kind="delivery",
            role_id="ceo",
            seat_id="seat::ceo",
            metadata={
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )
        task = self._runtime_task(
            f"task-origin-delivery-{suffix}",
            item.work_item_id,
            metadata={
                "work_kind": "delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )
        origin = await self._seed_company_origin(
            item=item,
            task=task,
            checkpoint_id=f"origin-{suffix}",
        )
        task, _generation = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        task.status = TaskStatus.AWAITING_HUMAN
        transitioned = await transition_work_item_from_task(
            self.store1,
            task,
            target_status_or_phase=Phase.AWAITING_HUMAN,
            reason="test_origin_delivery_ready",
        )
        self.assertIsNotNone(transitioned)
        task = await self.store1.get_task(task.id)
        assert task is not None
        context = CompanyControllerAttemptContext.from_task(
            task,
            work_item_id=item.work_item_id,
        )
        self.assertTrue(context.complete)
        return item, task, origin, context

    async def test_takeover_fences_output_metadata_artifact_and_deliverable(self) -> None:
        item = self._runtime_item("wi-output")
        task = self._runtime_task("task-output", item.work_item_id)
        stale_task, generation1 = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, _generation2 = await self._take_over(
            stale_generation=generation1,
            item=item,
            task=task,
        )
        stale_bundle = WorkItemOutputBundle(
            work_item_updates={
                "work_item_summary": "generation one stale summary",
                "completion_report": "generation one stale completion",
                "work_item_artifact_index": [
                    {"kind": "file", "label": "stale", "value": "stale.txt"}
                ],
            },
            summary="generation one stale summary",
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._persist_work_item_owned_output_metadata(
                stale_task,
                stale_bundle,
            )
        untouched = await self.store2.get_delegation_work_item(item.work_item_id)
        assert untouched is not None
        self.assertEqual(untouched.deliverable_summary, "")
        self.assertNotIn("completion_report", untouched.metadata)
        self.assertNotIn("work_item_artifact_index", untouched.metadata)

        current_bundle = WorkItemOutputBundle(
            work_item_updates={
                "work_item_summary": "generation two summary",
                "completion_report": "generation two completion",
                "work_item_artifact_index": [
                    {"kind": "file", "label": "winner", "value": "winner.txt"}
                ],
            },
            summary="generation two summary",
        )
        await self.executor2._persist_work_item_owned_output_metadata(
            current_task,
            current_bundle,
        )
        winner = await self.store2.get_delegation_work_item(item.work_item_id)
        assert winner is not None
        self.assertEqual(winner.deliverable_summary, "generation two summary")
        self.assertEqual(winner.metadata["completion_report"], "generation two completion")
        self.assertEqual(
            winner.metadata["work_item_artifact_index"][0]["value"],
            "winner.txt",
        )

    async def test_takeover_fences_follow_up_creation_and_parent_dependencies(self) -> None:
        item = self._runtime_item("wi-parent")
        topology = {
            "seats": [
                {
                    "role_id": "engineer",
                    "seat_id": "seat::engineer",
                    "team_id": "team::engineering",
                    "team_instance_id": "team-instance::engineering",
                    "seat_state_id": "seat-state::engineer",
                }
            ]
        }
        task = self._runtime_task(
            "task-parent",
            item.work_item_id,
            metadata={
                "runtime_topology": topology,
                "delegation_seat_id": "seat::analyst",
            },
        )
        stale_task, generation1 = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, _generation2 = await self._take_over(
            stale_generation=generation1,
            item=item,
            task=task,
        )
        stale_task.metadata["runtime_topology"] = topology
        current_task.metadata["runtime_topology"] = topology
        self.executor1._active_tasks = [stale_task]
        self.executor2._active_tasks = [current_task]
        result = TaskResult(
            status=TaskStatus.DONE,
            content="Build the application.",
            artifacts={
                "follow_up_actions": [
                    {
                        "action": "delegate_followup",
                        "target_role_id": "engineer",
                        "title": "Build app",
                        "summary": "Implement and test the app.",
                        "dedupe_key": "app-build-v1",
                    }
                ]
            },
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._materialize_follow_up_work_items(stale_task, result)
        self.assertEqual(
            [
                candidate
                for candidate in await self.store2.list_delegation_work_items("run-1")
                if candidate.work_item_id != item.work_item_id
            ],
            [],
        )
        parent = await self.store2.get_delegation_work_item(item.work_item_id)
        assert parent is not None
        self.assertNotIn("dependency_work_item_ids", parent.metadata)

        created = await self.executor2._materialize_follow_up_work_items(
            current_task,
            result,
        )
        self.assertEqual(len(created), 1)
        reused = await self.executor2._materialize_follow_up_work_items(
            current_task,
            result,
        )
        self.assertEqual(reused, [])
        parent = await self.store2.get_delegation_work_item(item.work_item_id)
        assert parent is not None
        self.assertEqual(parent.metadata["dependency_work_item_ids"], created)
        follow_ups = [
            candidate
            for candidate in await self.store2.list_delegation_work_items("run-1")
            if candidate.metadata.get("follow_up_dedupe_key") == "app-build-v1"
        ]
        self.assertEqual(len(follow_ups), 1)

    async def test_follow_up_insert_conflict_cannot_link_wrong_identity(self) -> None:
        item = self._runtime_item("wi-conflict-parent")
        topology = {
            "seats": [
                {
                    "role_id": "engineer",
                    "seat_id": "seat::engineer",
                    "team_id": "team::engineering",
                    "team_instance_id": "team-instance::engineering",
                    "seat_state_id": "seat-state::engineer",
                }
            ]
        }
        task = self._runtime_task(
            "task-conflict-parent",
            item.work_item_id,
            metadata={"runtime_topology": topology},
        )
        current_task, _generation = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task.metadata["runtime_topology"] = topology
        self.executor1._active_tasks = [current_task]
        dedupe_key = "semantic-conflict"
        digest = hashlib.sha256(
            f"run-1|{item.work_item_id}|{dedupe_key}".encode("utf-8")
        ).hexdigest()[:16]
        deterministic_id = f"followup::engineer::{digest}"
        await self.store1.save_delegation_work_item(
            self._runtime_item(
                deterministic_id,
                kind="review",
                role_id="intruder",
                seat_id="seat::intruder",
                parent_work_item_id="wrong-parent",
                metadata={"follow_up_dedupe_key": "different-key"},
            )
        )
        result = TaskResult(
            status=TaskStatus.DONE,
            content="delegate",
            artifacts={
                "follow_up_actions": [
                    {
                        "action": "delegate_followup",
                        "target_role_id": "engineer",
                        "title": "Build app",
                        "dedupe_key": dedupe_key,
                    }
                ]
            },
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._materialize_follow_up_work_items(
                current_task,
                result,
            )
        parent = await self.store1.get_delegation_work_item(item.work_item_id)
        assert parent is not None
        self.assertNotIn("dependency_work_item_ids", parent.metadata)
        conflict = await self.store1.get_delegation_work_item(deterministic_id)
        assert conflict is not None
        self.assertEqual(conflict.role_id, "intruder")

    async def test_takeover_fences_report_close_parent_payload_and_review_spawn(self) -> None:
        worker = self._runtime_item(
            "wi-worker",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_owner_role_id": "manager",
                "review_owner_seat_id": "seat::manager",
            },
        )
        report_id = report_work_item_id_for_attempt(worker.work_item_id, 1)
        report = self._runtime_item(
            report_id,
            kind="report",
            parent_work_item_id=worker.work_item_id,
            metadata={
                "report_execution_work_item": True,
                "report_target_work_item_id": worker.work_item_id,
                "report_target_worker_task_id": "task-worker",
                "report_attempt": 1,
            },
        )
        await self.store1.save_delegation_work_item(worker)
        task = self._runtime_task(
            "task-report",
            report_id,
            metadata={
                "report_execution_work_item": True,
                "report_target_work_item_id": worker.work_item_id,
                "work_kind": "report",
            },
        )
        stale_task, generation1 = await self._claim(
            self.store1,
            item=report,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, _generation2 = await self._take_over(
            stale_generation=generation1,
            item=report,
            task=task,
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._apply_report_done_transition(
                stale_task,
                result=TaskResult(
                    status=TaskStatus.DONE,
                    content="generation one stale report",
                ),
            )
        report_after_stale = await self.store2.get_delegation_work_item(report_id)
        worker_after_stale = await self.store2.get_delegation_work_item(worker.work_item_id)
        assert report_after_stale is not None and worker_after_stale is not None
        self.assertEqual(report_after_stale.phase, Phase.RUNNING)
        self.assertNotIn("completion_report", worker_after_stale.metadata)
        self.assertIsNone(
            await self.store2.get_delegation_work_item(
                review_work_item_id_for_attempt(worker.work_item_id, 1)
            )
        )

        await self.executor2._apply_report_done_transition(
            current_task,
            result=TaskResult(
                status=TaskStatus.DONE,
                content="generation two authoritative report",
            ),
        )
        report_winner = await self.store2.get_delegation_work_item(report_id)
        worker_winner = await self.store2.get_delegation_work_item(worker.work_item_id)
        review = await self.store2.get_delegation_work_item(
            review_work_item_id_for_attempt(worker.work_item_id, 1)
        )
        assert report_winner is not None and worker_winner is not None
        self.assertEqual(report_winner.phase, Phase.APPROVED)
        self.assertEqual(report_winner.metadata["report_card_outcome"], "applied")
        self.assertEqual(
            worker_winner.metadata["completion_report"],
            "generation two authoritative report",
        )
        self.assertIsNotNone(review)

    async def test_takeover_fences_review_close_and_verdict_application(self) -> None:
        worker = self._runtime_item(
            "wi-review-target",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "review_owner_role_id": "manager",
                "review_owner_seat_id": "seat::manager",
            },
        )
        report_id = report_work_item_id_for_attempt(worker.work_item_id, 1)
        report = self._runtime_item(
            report_id,
            phase=Phase.APPROVED,
            kind="report",
            parent_work_item_id=worker.work_item_id,
            metadata={
                "report_target_work_item_id": worker.work_item_id,
                "report_card_outcome": "applied",
                "completion_report": "verified report",
            },
        )
        review_id = review_work_item_id_for_attempt(worker.work_item_id, 1)
        review = self._runtime_item(
            review_id,
            kind="review",
            role_id="manager",
            seat_id="seat::manager",
            parent_work_item_id=worker.work_item_id,
            metadata={
                "review_execution_work_item": True,
                "review_target_work_item_id": worker.work_item_id,
                "review_source_report_work_item_id": report_id,
                "review_attempt": 1,
                "structured_review_verdict": {
                    "label": "approve",
                    "summary": "meets requirements",
                    "blocking_issues": [],
                    "followups": [],
                },
            },
        )
        await self.store1.save_delegation_work_item(worker)
        await self.store1.save_delegation_work_item(report)
        task = self._runtime_task(
            "task-review",
            review_id,
            metadata={
                "review_execution_work_item": True,
                "review_target_work_item_id": worker.work_item_id,
                "review_source_report_work_item_id": report_id,
                "work_kind": "review",
            },
        )
        task.context_snapshot["work_item_owned_outputs"] = {
            "structured_review_verdict": review.metadata[
                "structured_review_verdict"
            ]
        }
        stale_task, generation1 = await self._claim(
            self.store1,
            item=review,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, _generation2 = await self._take_over(
            stale_generation=generation1,
            item=review,
            task=task,
        )
        current_task.context_snapshot["work_item_owned_outputs"] = {
            "structured_review_verdict": review.metadata[
                "structured_review_verdict"
            ]
        }
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._finalize_review_work_item(stale_task)
        review_after_stale = await self.store2.get_delegation_work_item(review_id)
        worker_after_stale = await self.store2.get_delegation_work_item(worker.work_item_id)
        assert review_after_stale is not None and worker_after_stale is not None
        self.assertEqual(review_after_stale.phase, Phase.RUNNING)
        self.assertEqual(worker_after_stale.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertNotIn(
            "review_resolution_applied_work_item_id",
            worker_after_stale.metadata,
        )

        await self.executor2._finalize_review_work_item(current_task)
        review_winner = await self.store2.get_delegation_work_item(review_id)
        worker_winner = await self.store2.get_delegation_work_item(worker.work_item_id)
        assert review_winner is not None and worker_winner is not None
        self.assertEqual(review_winner.phase, Phase.APPROVED)
        self.assertEqual(review_winner.metadata["review_work_item_outcome"], "approve")
        self.assertEqual(worker_winner.phase, Phase.APPROVED)
        self.assertEqual(
            worker_winner.metadata["review_resolution_applied_work_item_id"],
            review_id,
        )

    async def test_intake_delivery_spawn_is_fenced_deterministic_and_exactly_once(self) -> None:
        origin_payload = {
            "checkpoint_id": "origin-intake",
            "checkpoint_type": "company_staffing_selection",
            "project_id": "project-1",
            "claim_id": "origin-intake-claim",
            "consumer_id": "origin-intake-consumer",
        }
        intake = self._runtime_item(
            "wi-intake",
            kind="intake",
            role_id="ceo",
            seat_id="seat::ceo",
            metadata={
                "original_message": "Analyze an investment target.",
                "dependency_work_item_ids": ["wi-child"],
                "origin_owner_interaction": dict(origin_payload),
            },
        )
        task = self._runtime_task(
            "task-intake",
            intake.work_item_id,
            metadata={
                "work_kind": "intake",
                "delegation_wait_for_work_item_ids": ["wi-child"],
                "origin_owner_interaction": dict(origin_payload),
            },
        )
        stale_task, generation1 = await self._claim(
            self.store1,
            item=intake,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, generation2 = await self._take_over(
            stale_generation=generation1,
            item=intake,
            task=task,
        )
        self._set_executor_controller(
            self.executor2,
            owner_token="owner-b",
            generation=generation2,
        )

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._spawn_delivery_card_after_intake(
                task=stale_task,
                intake_work_item=intake,
                dependency_ids=["wi-child"],
            )
        self.assertEqual(
            [
                item
                for item in await self.store2.list_delegation_work_items("run-1")
                if item.kind == "delivery"
            ],
            [],
        )
        untouched = await self.store2.get_delegation_work_item(intake.work_item_id)
        assert untouched is not None
        self.assertEqual(untouched.phase, Phase.RUNNING)
        self.assertFalse(untouched.metadata.get("intake_delivery_spawned", False))

        self.assertTrue(
            await self.executor2._spawn_delivery_card_after_intake(
                task=current_task,
                intake_work_item=untouched,
                dependency_ids=["wi-child"],
            )
        )
        first_items = await self.store2.list_delegation_work_items("run-1")
        first_delivery = [item for item in first_items if item.kind == "delivery"]
        self.assertEqual(len(first_delivery), 1)
        deterministic_id = first_delivery[0].work_item_id
        self.assertTrue(deterministic_id.startswith("delivery::"))

        self.assertTrue(
            await self.executor2._spawn_delivery_card_after_intake(
                task=current_task,
                intake_work_item=untouched,
                dependency_ids=["wi-child"],
            )
        )
        deliveries = [
            item
            for item in await self.store2.list_delegation_work_items("run-1")
            if item.kind == "delivery"
        ]
        self.assertEqual([item.work_item_id for item in deliveries], [deterministic_id])
        self.assertEqual(
            deliveries[0].metadata["origin_owner_interaction"],
            origin_payload,
        )
        materialized = await self.executor2._materialize_work_item_tasks(
            [current_task],
            deliveries,
        )
        delivery_tasks = [
            candidate
            for candidate in materialized
            if candidate.id != current_task.id
        ]
        self.assertEqual(len(delivery_tasks), 1)
        self.assertEqual(
            delivery_tasks[0].metadata["origin_owner_interaction"],
            origin_payload,
        )
        closed = await self.store2.get_delegation_work_item(intake.work_item_id)
        saved_task = await self.store2.get_task(current_task.id)
        assert closed is not None and saved_task is not None
        self.assertEqual(closed.phase, Phase.APPROVED)
        self.assertEqual(
            closed.metadata["intake_delivery_work_item_id"],
            deterministic_id,
        )
        self.assertEqual(saved_task.status, TaskStatus.DONE)

    async def test_final_delivery_commit_fences_all_state_and_retries_one_pending_card(self) -> None:
        delivery = self._runtime_item(
            "wi-delivery",
            kind="delivery",
            role_id="ceo",
            seat_id="seat::ceo",
            metadata={
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )
        task = self._runtime_task(
            "task-delivery",
            delivery.work_item_id,
            metadata={
                "work_kind": "delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )
        stale_task, generation1 = await self._claim(
            self.store1,
            item=delivery,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        current_task, _generation2 = await self._take_over(
            stale_generation=generation1,
            item=delivery,
            task=task,
        )
        current_task.status = TaskStatus.AWAITING_HUMAN
        transitioned = await transition_work_item_from_task(
            self.store2,
            current_task,
            target_status_or_phase=Phase.AWAITING_HUMAN,
            reason="test_final_delivery_ready",
        )
        self.assertIsNotNone(transitioned)

        package = {
            "executive_summary": "Generation two final delivery.",
            "delivered_items": [],
        }
        for candidate in (stale_task, current_task):
            candidate.status = TaskStatus.AWAITING_HUMAN
            candidate.result = {
                "content": "Generation two final delivery.",
                "artifacts": {},
            }
            candidate.context_snapshot = dict(candidate.context_snapshot or {})
            candidate.context_snapshot["delivery_package"] = package
            candidate.context_snapshot["work_item_owned_outputs"] = {
                "delivery_package": package,
            }

        async def prepare(
            data: dict,
        ) -> PreparedOwnerInteractionPublication:
            payload = dict(data["payload"])
            domain_key = hashlib.sha256(
                f"delivery:{payload['basis_hash']}".encode("utf-8")
            ).hexdigest()
            supersession_key = hashlib.sha256(
                f"owner:{data['project_id']}:{data['task_id']}".encode("utf-8")
            ).hexdigest()
            payload["interaction"] = {
                "kind": "company_delivery_feedback",
                "prompt": payload["prompt"],
                "ownership": {
                    "waiting_task_id": data["task_id"],
                    "waiting_session_id": data["session_id"],
                    "root_session_id": "root-1",
                    "ui_anchor_session_id": "root-1",
                },
                "domain_key": domain_key,
                "supersession_key": supersession_key,
                "supersession_order": [0, 0],
            }
            return PreparedOwnerInteractionPublication(
                checkpoint=ExecutionCheckpoint(
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=payload,
                ),
                interaction_key=domain_key,
                supersession_key=supersession_key,
                supersession_order=(0, 0),
            )

        notifications: list[str] = []

        async def notify(checkpoint: ExecutionCheckpoint) -> None:
            notifications.append(checkpoint.checkpoint_id)

        for executor in (self.executor1, self.executor2):
            executor.checkpoint_prepare_callback = prepare
            executor.checkpoint_notify_callback = notify

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.executor1._commit_final_delivery_owner_handoff(
                stale_task,
                summary="stale summary",
                progress_message="stale progress",
            )
        after_stale = await self.store2.get_delegation_work_item(delivery.work_item_id)
        run_after_stale = await self.store2.get_delegation_run("run-1")
        assert after_stale is not None and run_after_stale is not None
        self.assertNotIn("delivery_package", after_stale.metadata)
        self.assertEqual(run_after_stale.lifecycle_status, "active")
        self.assertEqual(
            await self.store2.get_pending_checkpoints(
                project_id="project-1",
                checkpoint_types=["company_delivery_feedback"],
            ),
            [],
        )

        for _ in range(2):
            self.assertTrue(
                await self.executor2._commit_final_delivery_owner_handoff(
                    current_task,
                    summary="Generation two final delivery.",
                    progress_message="Awaiting owner feedback.",
                )
            )
        final_item = await self.store2.get_delegation_work_item(delivery.work_item_id)
        final_run = await self.store2.get_delegation_run("run-1")
        pending = await self.store2.get_pending_checkpoints(
            project_id="project-1",
            checkpoint_types=["company_delivery_feedback"],
        )
        assert final_item is not None and final_run is not None
        self.assertEqual(final_item.metadata["delivery_package"], package)
        self.assertEqual(final_run.lifecycle_status, "awaiting_owner")
        self.assertEqual(final_run.status, "running")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].task_id, current_task.id)
        self.assertEqual(notifications, [pending[0].checkpoint_id] * 2)

    async def test_final_delivery_atomically_resolves_origin_and_leaves_one_owner_frontier(
        self,
    ) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="success"
        )
        origin_checkpoint = await self.store1.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )
        assert origin_checkpoint is not None
        interaction = dict(origin_checkpoint.payload["interaction"])
        decision = dict(interaction["decision"]["value"])
        coordinator = InteractionCoordinator(
            store=self.store1,
            project_id="project-1",
        )
        lease = InteractionDecisionLease(
            checkpoint=origin_checkpoint,
            decision=decision,
            consumer_id=origin.consumer_id,
            claim_id=origin.claim_id,
        )
        operation_started = asyncio.Event()
        release_operation = asyncio.Event()

        async def long_company_operation() -> str:
            operation_started.set()
            await release_operation.wait()
            return "company delivered"

        outer = asyncio.create_task(
            coordinator.run_while_claimed(
                lease,
                long_company_operation(),
                begin_effect=False,
                lease_seconds=300,
            )
        )
        await operation_started.wait()
        try:
            result = await self.store1.execute_company_controller_authoritative_command(
                context,
                operation="publish_final_delivery_owner_handoff",
                mutations=(
                    CompanyControllerWorkItemMutation(
                        work_item_id=item.work_item_id,
                        expected_phases=(Phase.AWAITING_HUMAN,),
                        deliverable_summary="Final result.",
                    ),
                ),
                task_snapshot=task,
                task_preimage_hashes={
                    task.id: company_controller_task_preimage_hash(task)
                },
                run_mutation=CompanyControllerRunLifecycleMutation(
                    expected_statuses=("running",),
                    expected_lifecycle_statuses=("active",),
                    lifecycle_status="awaiting_owner",
                    metadata_updates={"awaiting_owner_review": True},
                ),
                owner_publication=self._prepared_final_owner_publication(
                    task,
                    checkpoint_id="final-success",
                ),
                origin_owner_interaction=origin,
            )
            self.assertTrue(result.applied)
            release_operation.set()
            self.assertEqual(await outer, "company delivered")
        finally:
            release_operation.set()
            if not outer.done():
                outer.cancel()
                await asyncio.gather(
                    outer,
                    return_exceptions=True,
                )
            await coordinator.stop_lease_heartbeat(lease)

        completed_origin = await self.store2.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )
        assert completed_origin is not None
        self.assertEqual(completed_origin.status, "resolved")
        completion = dict(
            completed_origin.payload["interaction"]["completion"]
        )
        self.assertEqual(completion["claim_id"], origin.claim_id)
        self.assertEqual(completion["consumer_id"], origin.consumer_id)
        active = await self.store2.get_execution_checkpoints(
            project_id="project-1",
            statuses=["pending", "answered", "consuming"],
        )
        self.assertEqual(
            [
                (checkpoint.checkpoint_id, checkpoint.checkpoint_type)
                for checkpoint in active
            ],
            [("final-success", "company_delivery_feedback")],
        )

    async def test_final_delivery_origin_claim_mismatch_rolls_back_every_effect(
        self,
    ) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="mismatch"
        )
        item_before = await self.store2.get_delegation_work_item(item.work_item_id)
        task_before = await self.store2.get_task(task.id)
        run_before = await self.store2.get_delegation_run("run-1")
        origin_before = await self.store2.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )
        wrong_origin = OriginOwnerInteractionLease(
            checkpoint_id=origin.checkpoint_id,
            checkpoint_type=origin.checkpoint_type,
            project_id=origin.project_id,
            claim_id="different-active-claim",
            consumer_id=origin.consumer_id,
        )
        result = await self.store1.execute_company_controller_authoritative_command(
            context,
            operation="publish_final_delivery_owner_handoff",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    deliverable_summary="must roll back",
                ),
            ),
            task_snapshot=task,
            task_preimage_hashes={
                task.id: company_controller_task_preimage_hash(task)
            },
            run_mutation=CompanyControllerRunLifecycleMutation(
                expected_statuses=("running",),
                expected_lifecycle_statuses=("active",),
                lifecycle_status="awaiting_owner",
                metadata_updates={"must_not_land": True},
            ),
            owner_publication=self._prepared_final_owner_publication(
                task,
                checkpoint_id="final-mismatch",
            ),
            origin_owner_interaction=wrong_origin,
        )
        self.assertFalse(result.applied)
        self.assertEqual(
            await self.store2.get_delegation_work_item(item.work_item_id),
            item_before,
        )
        self.assertEqual(await self.store2.get_task(task.id), task_before)
        self.assertEqual(await self.store2.get_delegation_run("run-1"), run_before)
        self.assertEqual(
            await self.store2.get_execution_checkpoint(
                origin.checkpoint_id,
                project_id="project-1",
                checkpoint_type=origin.checkpoint_type,
            ),
            origin_before,
        )
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                "final-mismatch",
                project_id="project-1",
                checkpoint_type="company_delivery_feedback",
            )
        )

    async def test_final_delivery_missing_task_origin_cannot_downgrade_run_to_legacy(
        self,
    ) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="missing-task-origin"
        )
        task.metadata = dict(task.metadata or {})
        task.metadata.pop("origin_owner_interaction", None)
        await self.store1.save_task(task)

        item_before = await self.store2.get_delegation_work_item(item.work_item_id)
        task_before = await self.store2.get_task(task.id)
        run_before = await self.store2.get_delegation_run("run-1")
        origin_before = await self.store2.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )

        result = await self.store1.execute_company_controller_authoritative_command(
            context,
            operation="publish_final_delivery_owner_handoff",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    deliverable_summary="must not land",
                ),
            ),
            task_snapshot=task,
            task_preimage_hashes={
                task.id: company_controller_task_preimage_hash(task)
            },
            run_mutation=CompanyControllerRunLifecycleMutation(
                expected_statuses=("running",),
                expected_lifecycle_statuses=("active",),
                lifecycle_status="awaiting_owner",
                metadata_updates={"must_not_land": True},
            ),
            owner_publication=self._prepared_final_owner_publication(
                task,
                checkpoint_id="final-missing-task-origin",
            ),
            origin_owner_interaction=None,
        )

        self.assertFalse(result.applied)
        self.assertEqual(
            await self.store2.get_delegation_work_item(item.work_item_id),
            item_before,
        )
        self.assertEqual(await self.store2.get_task(task.id), task_before)
        self.assertEqual(await self.store2.get_delegation_run("run-1"), run_before)
        self.assertEqual(
            await self.store2.get_execution_checkpoint(
                origin.checkpoint_id,
                project_id="project-1",
                checkpoint_type=origin.checkpoint_type,
            ),
            origin_before,
        )
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                "final-missing-task-origin",
                project_id="project-1",
                checkpoint_type="company_delivery_feedback",
            )
        )

    async def test_final_delivery_malformed_run_origin_fails_closed(
        self,
    ) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="malformed-run-origin"
        )
        run = await self.store1.get_delegation_run("run-1")
        assert run is not None
        run.metadata = dict(run.metadata or {})
        run.metadata["origin_owner_interaction"] = {
            "checkpoint_id": origin.checkpoint_id,
        }
        await self.store1.save_delegation_run(run)

        item_before = await self.store2.get_delegation_work_item(item.work_item_id)
        task_before = await self.store2.get_task(task.id)
        run_before = await self.store2.get_delegation_run("run-1")
        origin_before = await self.store2.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )

        result = await self.store1.execute_company_controller_authoritative_command(
            context,
            operation="publish_final_delivery_owner_handoff",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    deliverable_summary="must not land",
                ),
            ),
            task_snapshot=task,
            task_preimage_hashes={
                task.id: company_controller_task_preimage_hash(task)
            },
            run_mutation=CompanyControllerRunLifecycleMutation(
                expected_statuses=("running",),
                expected_lifecycle_statuses=("active",),
                lifecycle_status="awaiting_owner",
                metadata_updates={"must_not_land": True},
            ),
            owner_publication=self._prepared_final_owner_publication(
                task,
                checkpoint_id="final-malformed-run-origin",
            ),
            origin_owner_interaction=origin,
        )

        self.assertFalse(result.applied)
        self.assertEqual(
            await self.store2.get_delegation_work_item(item.work_item_id),
            item_before,
        )
        self.assertEqual(await self.store2.get_task(task.id), task_before)
        self.assertEqual(await self.store2.get_delegation_run("run-1"), run_before)
        self.assertEqual(
            await self.store2.get_execution_checkpoint(
                origin.checkpoint_id,
                project_id="project-1",
                checkpoint_type=origin.checkpoint_type,
            ),
            origin_before,
        )
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                "final-malformed-run-origin",
                project_id="project-1",
                checkpoint_type="company_delivery_feedback",
            )
        )

    async def test_terminal_origin_allows_recovered_company_delivery(self) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="recovery"
        )
        terminal = await self.store1.finish_execution_checkpoint_consumption(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
            claim_id=origin.claim_id,
            consumer_id=origin.consumer_id,
            final_status="outcome_unknown",
            payload_patch={"interaction_error": {"kind": "process_exit"}},
        )
        self.assertTrue(terminal.applied)

        result = await self.store1.execute_company_controller_authoritative_command(
            context,
            operation="publish_final_delivery_owner_handoff",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    deliverable_summary="Recovered final result.",
                ),
            ),
            task_snapshot=task,
            task_preimage_hashes={
                task.id: company_controller_task_preimage_hash(task)
            },
            run_mutation=CompanyControllerRunLifecycleMutation(
                expected_statuses=("running",),
                expected_lifecycle_statuses=("active",),
                lifecycle_status="awaiting_owner",
            ),
            owner_publication=self._prepared_final_owner_publication(
                task,
                checkpoint_id="final-recovery",
            ),
            origin_owner_interaction=origin,
        )
        self.assertTrue(result.applied)
        persisted_origin = await self.store2.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )
        assert persisted_origin is not None
        self.assertEqual(persisted_origin.status, "outcome_unknown")
        active = await self.store2.get_execution_checkpoints(
            project_id="project-1",
            statuses=["pending", "answered", "consuming"],
        )
        self.assertEqual(
            [checkpoint.checkpoint_id for checkpoint in active],
            ["final-recovery"],
        )

    async def test_terminal_origin_with_foreign_completion_rolls_back_delivery(
        self,
    ) -> None:
        item, task, origin, context = await self._prepare_origin_delivery_attempt(
            suffix="foreign-terminal"
        )
        terminal = await self.store1.finish_execution_checkpoint_consumption(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
            claim_id=origin.claim_id,
            consumer_id=origin.consumer_id,
            final_status="outcome_unknown",
        )
        self.assertTrue(terminal.applied)
        assert self.store1._db is not None
        persisted_origin = await self.store1.get_execution_checkpoint(
            origin.checkpoint_id,
            project_id="project-1",
            checkpoint_type=origin.checkpoint_type,
        )
        assert persisted_origin is not None
        corrupt_payload = dict(persisted_origin.payload)
        corrupt_interaction = dict(corrupt_payload["interaction"])
        corrupt_interaction["completion"] = {
            **dict(corrupt_interaction["completion"]),
            "claim_id": "foreign-terminal-claim",
        }
        corrupt_payload["interaction"] = corrupt_interaction
        await self.store1._db.execute(
            """UPDATE execution_checkpoints SET payload = ?
               WHERE checkpoint_id = ? AND project_id = ?""",
            (
                json.dumps(corrupt_payload),
                origin.checkpoint_id,
                "project-1",
            ),
        )
        await self.store1._db.commit()
        item_before = await self.store2.get_delegation_work_item(item.work_item_id)
        task_before = await self.store2.get_task(task.id)
        run_before = await self.store2.get_delegation_run("run-1")

        result = await self.store1.execute_company_controller_authoritative_command(
            context,
            operation="publish_final_delivery_owner_handoff",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    deliverable_summary="must not land",
                ),
            ),
            task_snapshot=task,
            task_preimage_hashes={
                task.id: company_controller_task_preimage_hash(task)
            },
            run_mutation=CompanyControllerRunLifecycleMutation(
                expected_statuses=("running",),
                expected_lifecycle_statuses=("active",),
                lifecycle_status="awaiting_owner",
                metadata_updates={"must_not_land": True},
            ),
            owner_publication=self._prepared_final_owner_publication(
                task,
                checkpoint_id="final-foreign-terminal",
            ),
            origin_owner_interaction=origin,
        )
        self.assertFalse(result.applied)
        self.assertEqual(
            await self.store2.get_delegation_work_item(item.work_item_id),
            item_before,
        )
        self.assertEqual(await self.store2.get_task(task.id), task_before)
        self.assertEqual(await self.store2.get_delegation_run("run-1"), run_before)
        self.assertIsNone(
            await self.store2.get_execution_checkpoint(
                "final-foreign-terminal",
                project_id="project-1",
                checkpoint_type="company_delivery_feedback",
            )
        )

    async def test_run_lifecycle_cas_rejects_takeover_loser_and_commits_winner(self) -> None:
        item = self._runtime_item("wi-run-failure")
        task = self._runtime_task("task-run-failure", item.work_item_id)
        _stale_task, generation1 = await self._claim(
            self.store1,
            item=item,
            task=task,
            owner_token="owner-a",
            expected_phase=Phase.READY,
        )
        _current_task, generation2 = await self._take_over(
            stale_generation=generation1,
            item=item,
            task=task,
        )
        before = await self.store2.get_delegation_run("run-1")
        assert before is not None
        mutation = CompanyControllerRunLifecycleMutation(
            expected_statuses=("running",),
            expected_lifecycle_statuses=("active",),
            expected_current_revision=before.current_revision,
            expected_updated_at=before.updated_at,
            status="failed",
            lifecycle_status="closed_failed",
            metadata_updates={"run_failure": {"reason": "terminal convergence"}},
        )
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store1.transition_delegation_run_lifecycle_for_controller(
                "run-1",
                project_id="project-1",
                root_session_id="root-1",
                owner_token="owner-a",
                generation=generation1,
                mutation=mutation,
            )
        after_loser = await self.store2.get_delegation_run("run-1")
        assert after_loser is not None
        self.assertEqual(after_loser.status, "running")
        self.assertEqual(after_loser.lifecycle_status, "active")
        self.assertNotIn("run_failure", after_loser.metadata)

        winner = await self.store2.transition_delegation_run_lifecycle_for_controller(
            "run-1",
            project_id="project-1",
            root_session_id="root-1",
            owner_token="owner-b",
            generation=generation2,
            mutation=mutation,
        )
        self.assertTrue(winner.applied)
        self.assertIsNotNone(winner.run)
        persisted = await self.store1.get_delegation_run("run-1")
        assert persisted is not None
        self.assertEqual(persisted.status, "failed")
        self.assertEqual(persisted.lifecycle_status, "closed_failed")
        self.assertEqual(
            persisted.metadata["run_failure"]["reason"],
            "terminal convergence",
        )


if __name__ == "__main__":
    unittest.main()
