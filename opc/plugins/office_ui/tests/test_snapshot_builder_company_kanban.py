from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    Phase,
    Task,
)
from opc.database.store import OPCStore
from opc.plugins.office_ui.snapshot_builder import (
    _build_company_runtime_control_by_task,
    _build_executor_role_work_items_for_session,
    _build_role_work_items_for_session,
    _role_aggregated_status,
    build_collab_sync,
    build_company_kanban_projection,
)


class CompanyKanbanProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_control_keeps_suspending_until_checkpoint_visible(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "company_runtime_stop_state": "suspending",
                "company_runtime_stop_marked_at": "2026-04-29T11:00:00",
            },
        )
        child_task = SimpleNamespace(
            id="child-task",
            session_id="session-root:child",
            parent_session_id="session-root",
            title="Child work item",
            status="running",
            created_at=created_at,
            metadata={
                "mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "work-item",
            },
        )
        store = MagicMock()
        store.get_execution_checkpoints = AsyncMock(return_value=[])
        engine = SimpleNamespace(
            store=store,
            _task_runtime_is_live=AsyncMock(return_value=True),
        )

        control = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, child_task],
            "proj1",
        )

        self.assertEqual(control["parent-task"]["runtime_control_state"], "suspending")
        self.assertEqual(control["child-task"]["runtime_control_state"], "suspending")
        self.assertFalse(control["child-task"]["can_stop"])

    async def test_runtime_control_ignores_stale_suspended_marker_after_resume(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "company_runtime_stop_state": "suspended",
                "company_runtime_stop_marked_at": "2026-04-29T11:00:00",
            },
        )
        child_task = SimpleNamespace(
            id="child-task",
            session_id="session-root:child",
            parent_session_id="session-root",
            title="Child work item",
            status="running",
            created_at=created_at,
            metadata={
                "mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "work-item",
            },
        )
        store = MagicMock()
        store.get_execution_checkpoints = AsyncMock(return_value=[])
        engine = SimpleNamespace(
            store=store,
            _task_runtime_is_live=AsyncMock(return_value=True),
        )

        control = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, child_task],
            "proj1",
        )

        self.assertEqual(control["child-task"]["runtime_control_state"], "running")
        self.assertTrue(control["child-task"]["can_stop"])
        engine._task_runtime_is_live.assert_awaited()

    async def test_runtime_control_running_status_requires_controller_registry_ownership(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
            },
        )
        store = MagicMock()
        store.get_execution_checkpoints = AsyncMock(return_value=[])
        engine = SimpleNamespace(
            store=store,
            _task_runtime_is_live=AsyncMock(return_value=False),
        )

        control = await _build_company_runtime_control_by_task(
            engine,
            [parent_task],
            "proj1",
        )

        self.assertEqual(control["parent-task"]["runtime_control_state"], "idle")
        self.assertFalse(control["parent-task"]["can_stop"])
        engine._task_runtime_is_live.assert_awaited_once_with(parent_task)

    async def test_registry_live_resume_checkpoint_projects_running_and_stoppable(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
            },
        )
        terminal_driver_task = SimpleNamespace(
            id="terminal-driver-task",
            session_id="driver-session",
            parent_session_id="session-root",
            title="Terminal driver envelope",
            status="done",
            created_at=created_at,
            metadata={
                "mode": "company",
                "work_item_runtime": True,
                "work_item_projection_id": "driver",
            },
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="resume-checkpoint",
            project_id="proj1",
            session_id="session-root",
            checkpoint_type="company_runtime_suspended",
            status="resuming",
            task_id="terminal-driver-task",
            payload={"parent_session_id": "session-root"},
        )
        store = MagicMock()
        store.get_execution_checkpoints = AsyncMock(return_value=[checkpoint])
        engine = SimpleNamespace(
            store=store,
            _task_runtime_is_live=AsyncMock(
                side_effect=lambda task: task.id == "terminal-driver-task"
            ),
        )

        control = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, terminal_driver_task],
            "proj1",
        )

        self.assertEqual(
            control["parent-task"]["runtime_control_state"],
            "running",
        )
        self.assertTrue(control["parent-task"]["can_stop"])
        self.assertFalse(control["parent-task"]["can_resume"])

    async def test_stop_hold_and_pending_checkpoint_override_live_resume_projection(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
            },
        )
        child_task = SimpleNamespace(
            id="child-task",
            session_id="child-session",
            parent_session_id="session-root",
            title="Live child",
            status="done",
            created_at=created_at,
            metadata={
                "mode": "company",
                "work_item_runtime": True,
                "work_item_projection_id": "child",
                "dispatch_hold": "company_runtime_suspended",
            },
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="resume-checkpoint",
            project_id="proj1",
            session_id="session-root",
            checkpoint_type="company_runtime_suspended",
            status="resuming",
            task_id="child-task",
            payload={"parent_session_id": "session-root"},
        )
        store = MagicMock()
        store.get_execution_checkpoints = AsyncMock(return_value=[checkpoint])
        engine = SimpleNamespace(
            store=store,
            _task_runtime_is_live=AsyncMock(return_value=True),
        )

        suspending = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, child_task],
            "proj1",
        )
        self.assertEqual(
            suspending["parent-task"]["runtime_control_state"],
            "suspending",
        )
        self.assertFalse(suspending["parent-task"]["can_stop"])

        checkpoint.status = "pending"
        suspended = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, child_task],
            "proj1",
        )
        self.assertEqual(
            suspended["parent-task"]["runtime_control_state"],
            "suspended",
        )
        self.assertFalse(suspended["parent-task"]["can_stop"])
        self.assertTrue(suspended["parent-task"]["can_resume"])

    async def test_runtime_control_treats_dispatch_hold_as_suspending_without_checkpoint(self) -> None:
        created_at = datetime.now(timezone.utc)
        parent_task = SimpleNamespace(
            id="parent-task",
            session_id="session-root",
            parent_session_id="",
            title="Root runtime",
            status="running",
            created_at=created_at,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
            },
        )
        child_task = SimpleNamespace(
            id="child-task",
            session_id="session-root:child",
            parent_session_id="session-root",
            title="Child work item",
            status="running",
            created_at=created_at,
            metadata={
                "mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "work-item",
                "dispatch_hold": "company_runtime_suspended",
            },
        )
        store = MagicMock()
        store.get_pending_checkpoints = AsyncMock(return_value=[])
        engine = SimpleNamespace(store=store)

        control = await _build_company_runtime_control_by_task(
            engine,
            [parent_task, child_task],
            "proj1",
        )

        self.assertEqual(control["child-task"]["runtime_control_state"], "suspending")
        self.assertFalse(control["child-task"]["can_stop"])

    async def test_shared_role_root_session_keeps_root_board_id(self) -> None:
        created_at = datetime.now(timezone.utc)
        store = MagicMock()
        store.list_open_delegation_runs = AsyncMock(return_value=[])
        engine = SimpleNamespace(
            store=store,
            org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
        )
        root_task = SimpleNamespace(
            id="root-task",
            session_id="session-root",
            parent_session_id="",
            title="app17",
            status="running",
            assigned_to="",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={"origin_task_id": "root-task"},
        )
        shared_role_task = SimpleNamespace(
            id="ceo-intake-task",
            session_id="session-root",
            parent_session_id="session-root",
            title="CEO Intake",
            status="running",
            assigned_to="ceo",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={
                "shared_role_session": True,
                "shared_role_id": "ceo",
                "company_runtime_root_session_id": "session-root",
                "work_item_projection_id": "ceo-intake",
                "work_item_turn_type": "intake",
                "origin_task_id": "root-task",
            },
        )

        tasks, columns, boards, meta = await build_company_kanban_projection(
            engine,
            project_id="proj-shared-root",
            tasks=[root_task, shared_role_task],
            event_adapter=None,
        )

        self.assertEqual(tasks, [])
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0]["board_id"], "root-task")
        self.assertEqual(meta["board_ids"], ["root-task"])
        self.assertEqual(meta["session_ids"], ["session-root"])
        self.assertEqual(
            {column["board_id"] for column in columns if column["column_id"] == "todo"},
            {"root-task"},
        )

    async def test_build_company_kanban_projection_returns_session_scoped_boards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                await store.save_delegation_run(
                    DelegationRun(
                        run_id="run-1",
                        project_id="proj1",
                        session_id="sess-1",
                        company_profile="corporate",
                        status="running",
                        lifecycle_status="active",
                    )
                )
                await store.save_delegation_run(
                    DelegationRun(
                        run_id="run-2",
                        project_id="proj1",
                        session_id="sess-2",
                        company_profile="corporate",
                        status="running",
                        lifecycle_status="active",
                    )
                )
                primary_a = SimpleNamespace(id="primary-a", session_id="sess-1", parent_session_id="", title="Session A")
                primary_b = SimpleNamespace(id="primary-b", session_id="sess-2", parent_session_id="", title="Session B")
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="root-item",
                        run_id="run-1",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        title="CEO Intake",
                        summary="Review the request and set direction.",
                        kind="intake",
                        projection_id="root-item",
                        phase=Phase.RUNNING,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="child-item",
                        run_id="run-1",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="cto",
                        seat_id="seat::team::ceo::cto",
                        parent_work_item_id="root-item",
                        title="Build prototype",
                        summary="Prototype the core translation runtime.",
                        kind="execute",
                        projection_id="child-item",
                        phase=Phase.READY,
                        manager_role_id="ceo",
                        manager_seat_id="seat::team::ceo::ceo",
                        metadata={
                            "runtime_model": "multi_team_org",
                            "dependency_work_item_ids": [],
                        },
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="root-item-2",
                        run_id="run-2",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        title="CEO Intake B",
                        summary="Review request B.",
                        kind="intake",
                        projection_id="root-item-2",
                        phase=Phase.RUNNING,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="child-item-2",
                        run_id="run-2",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="cmo",
                        seat_id="seat::team::ceo::cmo",
                        parent_work_item_id="root-item-2",
                        title="Prepare launch copy",
                        summary="Draft the launch positioning and promo angle.",
                        kind="execute",
                        projection_id="child-item-2",
                        phase=Phase.READY,
                        manager_role_id="ceo",
                        manager_seat_id="seat::team::ceo::ceo",
                        metadata={
                            "runtime_model": "multi_team_org",
                            "dependency_work_item_ids": [],
                        },
                    )
                )

                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )

                tasks, columns, boards, meta = await build_company_kanban_projection(
                    engine,
                    project_id="proj1",
                    tasks=[primary_a, primary_b],
                    event_adapter=None,
                )

                self.assertEqual(len(boards), 2)
                self.assertEqual({board["board_id"] for board in boards}, {"primary-a", "primary-b"})
                self.assertEqual(
                    {(task["task_id"], task["board_id"]) for task in tasks},
                    {("child-item", "primary-a"), ("child-item-2", "primary-b")},
                )
                self.assertEqual(
                    {column["board_id"] for column in columns if column["column_id"] == "todo"},
                    {"primary-a", "primary-b"},
                )
                # Order follows primary tasks (session order), not run order
                self.assertEqual(meta["board_ids"], ["primary-a", "primary-b"])
                self.assertEqual(set(meta["run_ids"]), {"run-1", "run-2"})
            finally:
                await store.close()

    async def test_empty_project_returns_no_boards(self) -> None:
        """A project with no primary sessions at all should return empty lists.

        Regression: previously the snapshot builder fell back to a
        project-wide board when company_boards was empty.  That board would
        be auto-selected by the frontend, then cleared by the session-driven
        effect, causing an infinite render loop (screen flicker).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )
                tasks, columns, boards, meta = await build_company_kanban_projection(
                    engine, project_id="proj1", tasks=[], event_adapter=None,
                )
                self.assertEqual(boards, [])
                self.assertEqual(columns, [])
                self.assertEqual(tasks, [])
                self.assertIsNone(meta)
            finally:
                await store.close()

    async def test_primary_session_without_delegation_run_gets_empty_board(self) -> None:
        """A primary session with no open delegation run should still get its own board."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                primary = SimpleNamespace(
                    id="primary-solo", session_id="sess-solo", parent_session_id="", title="Solo Session",
                )
                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )

                tasks, columns, boards, meta = await build_company_kanban_projection(
                    engine, project_id="proj1", tasks=[primary], event_adapter=None,
                )

                self.assertEqual(len(boards), 1)
                self.assertEqual(boards[0]["board_id"], "primary-solo")
                self.assertEqual(boards[0]["name"], "Solo Session")
                # Columns exist (standard company columns) even though no work items
                board_col_ids = {c["board_id"] for c in columns}
                self.assertIn("primary-solo", board_col_ids)
                # No tasks since there's no delegation run
                self.assertEqual(tasks, [])
                # Meta tracks the session with empty run_id
                self.assertEqual(meta["board_ids"], ["primary-solo"])
                self.assertEqual(meta["session_ids"], ["sess-solo"])
                self.assertEqual(meta["run_ids"], [""])
            finally:
                await store.close()

    async def test_projection_meta_exposes_work_items_by_session_id(self) -> None:
        """``build_company_kanban_projection`` must surface the per-run
        DelegationWorkItem list back to ``build_snapshot`` so the
        Execution Progress panel can build per-role rollups without
        re-querying the store. Locked here to prevent silent drift.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                await store.save_delegation_run(
                    DelegationRun(
                        run_id="run-1",
                        project_id="proj1",
                        session_id="sess-1",
                        company_profile="corporate",
                        status="running",
                        lifecycle_status="active",
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="root-wi",
                        run_id="run-1",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        title="Intake",
                        kind="intake",
                        projection_id="root-wi",
                        phase=Phase.RUNNING,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="child-wi",
                        run_id="run-1",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="cto",
                        seat_id="seat::team::ceo::cto",
                        parent_work_item_id="root-wi",
                        title="Build",
                        kind="execute",
                        projection_id="child-wi",
                        phase=Phase.READY,
                        manager_role_id="ceo",
                        metadata={
                            "runtime_model": "multi_team_org",
                            "dependency_work_item_ids": [],
                        },
                    )
                )
                primary = SimpleNamespace(
                    id="task-a", session_id="sess-1", parent_session_id="", title="A",
                )
                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )
                tasks, _columns, _boards, meta = await build_company_kanban_projection(
                    engine, project_id="proj1", tasks=[primary], event_adapter=None,
                )
                self.assertIn("work_items_by_session_id", meta)
                wi_by_sess = meta["work_items_by_session_id"]
                self.assertEqual(set(wi_by_sess.keys()), {"sess-1"})
                self.assertEqual(
                    {wi.work_item_id for wi in wi_by_sess["sess-1"]},
                    {"root-wi", "child-wi"},
                )
                # task_by_work_item_id is also passed back so the helper can
                # resolve linked Tasks for ``WorkItemContextView`` reads.
                self.assertIn("task_by_work_item_id", meta)
            finally:
                await store.close()

    async def test_projection_preserves_multiple_runs_for_same_session(self) -> None:
        """Follow-up owner directives append to a project's history; the UI
        board must not hide earlier run work items when a later run exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                for run_id, lifecycle in (("run-old", "delivered"), ("run-new", "active")):
                    await store.save_delegation_run(
                        DelegationRun(
                            run_id=run_id,
                            project_id="proj1",
                            session_id="sess-1",
                            company_profile="custom",
                            status="completed" if lifecycle == "delivered" else "running",
                            lifecycle_status=lifecycle,
                        )
                    )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="old-child",
                        run_id="run-old",
                        cell_id="team::lead",
                        team_id="team::lead",
                        role_id="researcher",
                        seat_id="seat::team::lead::researcher",
                        parent_work_item_id="old-root",
                        title="Original research",
                        kind="execute",
                        projection_id="old-child",
                        phase=Phase.APPROVED,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="new-child",
                        run_id="run-new",
                        cell_id="team::lead",
                        team_id="team::lead",
                        role_id="report_producer",
                        seat_id="seat::team::lead::report_producer",
                        parent_work_item_id="new-root",
                        title="Follow-up PPT",
                        kind="execute",
                        projection_id="new-child",
                        phase=Phase.READY,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                primary = SimpleNamespace(
                    id="primary-a", session_id="sess-1", parent_session_id="", title="A",
                )
                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )

                tasks, _columns, boards, meta = await build_company_kanban_projection(
                    engine, project_id="proj1", tasks=[primary], event_adapter=None,
                )

                self.assertEqual([board["board_id"] for board in boards], ["primary-a"])
                self.assertEqual(
                    {(task["task_id"], task["board_id"]) for task in tasks},
                    {("old-child", "primary-a"), ("new-child", "primary-a")},
                )
                self.assertEqual(set(meta["run_ids"]), {"run-old", "run-new"})
                self.assertEqual(
                    {wi.work_item_id for wi in meta["work_items_by_session_id"]["sess-1"]},
                    {"old-child", "new-child"},
                )
            finally:
                await store.close()

    async def test_mixed_sessions_with_and_without_runs(self) -> None:
        """One session has a delegation run, another does not — both get boards."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                await store.save_delegation_run(
                    DelegationRun(
                        run_id="run-a",
                        project_id="proj1",
                        session_id="sess-a",
                        company_profile="corporate",
                        status="running",
                        lifecycle_status="active",
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="root-wi",
                        run_id="run-a",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        title="CEO Intake",
                        summary="Review.",
                        kind="intake",
                        projection_id="root-wi",
                        phase=Phase.RUNNING,
                        metadata={"runtime_model": "multi_team_org"},
                    )
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="child-wi",
                        run_id="run-a",
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        role_id="cto",
                        seat_id="seat::team::ceo::cto",
                        parent_work_item_id="root-wi",
                        title="Build feature",
                        summary="Build it.",
                        kind="execute",
                        projection_id="child-wi",
                        phase=Phase.READY,
                        manager_role_id="ceo",
                        manager_seat_id="seat::team::ceo::ceo",
                        metadata={
                            "runtime_model": "multi_team_org",
                            "dependency_work_item_ids": [],
                        },
                    )
                )

                primary_a = SimpleNamespace(id="task-a", session_id="sess-a", parent_session_id="", title="Session A")
                primary_b = SimpleNamespace(id="task-b", session_id="sess-b", parent_session_id="", title="Session B (new)")

                engine = SimpleNamespace(
                    store=store,
                    org_engine=SimpleNamespace(get_employee=lambda employee_id: None),
                )

                tasks, columns, boards, meta = await build_company_kanban_projection(
                    engine, project_id="proj1", tasks=[primary_a, primary_b], event_adapter=None,
                )

                # Both sessions get a board
                self.assertEqual(len(boards), 2)
                self.assertEqual({b["board_id"] for b in boards}, {"task-a", "task-b"})
                # Only session A (with run) has work-item tasks
                self.assertEqual(len(tasks), 1)
                self.assertEqual(tasks[0]["board_id"], "task-a")
                self.assertEqual(tasks[0]["task_id"], "child-wi")
                self.assertEqual(tasks[0]["work_item_projection_id"], "child-wi")
                self.assertEqual(tasks[0]["work_item_turn_type"], "execute")
                # Both boards have company columns
                self.assertEqual(
                    {c["board_id"] for c in columns if c["column_id"] == "todo"},
                    {"task-a", "task-b"},
                )
            finally:
                await store.close()


class RoleWorkItemsRollupTests(unittest.TestCase):
    """Per-role DelegationWorkItem rollup that drives the chat
    "Execution Progress" panel. The UI contract is "1 row = 1 work item",
    so the helper must:

    * group by ``effective_owner(phase, item)`` (reviewer wins during
      ``in_review`` phases)
    * skip auxiliary cards (review_execution / report_execution / attention)
    * filter ``progress_log`` per ``work_item_projection_id`` so a row's
      activity feed never bleeds into another row
    * derive ``aggregated_status`` from runtime tracker + phase mix so the
      UI doesn't have to reimplement the priority table.
    """

    @staticmethod
    def _make_item(
        *,
        work_item_id: str,
        role_id: str,
        phase: Phase,
        title: str = "",
        manager_role_id: str = "",
        kind: str = "execute",
        metadata: dict | None = None,
        created_at: float = 1.0,
        updated_at: float = 1.0,
        projection_id: str | None = None,
        parent_work_item_id: str = "root-wi",
    ) -> SimpleNamespace:
        meta = dict(metadata or {})
        if projection_id and "work_item_projection_id" not in meta:
            meta["work_item_projection_id"] = projection_id
        if "work_item_turn_type" not in meta:
            meta["work_item_turn_type"] = kind
        return SimpleNamespace(
            work_item_id=work_item_id,
            projection_id=projection_id or work_item_id,
            run_id="run-1",
            cell_id="team::ceo",
            team_id="team::ceo",
            role_id=role_id,
            seat_id=f"seat::team::ceo::{role_id}",
            parent_work_item_id=parent_work_item_id,
            title=title or work_item_id,
            kind=kind,
            phase=phase,
            manager_role_id=manager_role_id,
            manager_seat_id=(
                f"seat::team::ceo::{manager_role_id}" if manager_role_id else ""
            ),
            metadata=meta,
            created_at=created_at,
            updated_at=updated_at,
        )

    def test_groups_by_effective_owner_with_reviewer_during_in_review(self) -> None:
        """When a work item is in ``awaiting_manager_review`` it should
        appear under the manager's role, not the executor's — that is the
        contract ``effective_owner`` enforces, and the UI relies on it to
        show review work in the reviewer's column.
        """
        items = [
            self._make_item(
                work_item_id="wi-engineer-running",
                role_id="engineer",
                phase=Phase.RUNNING,
                title="Engineer the new feature",
                manager_role_id="cto",
                created_at=10.0,
                updated_at=20.0,
            ),
            self._make_item(
                work_item_id="wi-engineer-review",
                role_id="engineer",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                title="Implement the AI summary",
                manager_role_id="cto",
                created_at=30.0,
                updated_at=40.0,
            ),
        ]
        rollup = _build_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"engineer", "cto"})
        engineer_ids = [wi["work_item_id"] for wi in rollup["engineer"]["work_items"]]
        cto_ids = [wi["work_item_id"] for wi in rollup["cto"]["work_items"]]
        self.assertEqual(engineer_ids, ["wi-engineer-running"])
        self.assertEqual(cto_ids, ["wi-engineer-review"])

        review_row = rollup["cto"]["work_items"][0]
        self.assertTrue(review_row["is_review_target"])
        self.assertEqual(review_row["executor_role_id"], "engineer")
        self.assertEqual(review_row["reviewer_role_id"], "cto")
        # ``kanban_column`` is the source of truth for column placement; the
        # UI derives row colour from it, so the test pins the projection.
        self.assertEqual(review_row["kanban_column"], "in-review")

    def test_executor_rollup_keeps_review_target_under_executor_role(self) -> None:
        """Execution Progress uses a display-only executor rollup so worker
        chips do not disappear while their work waits on manager review.
        """
        items = [
            self._make_item(
                work_item_id="wi-engineer-review",
                role_id="engineer",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                title="Implement the AI summary",
                manager_role_id="cto",
                created_at=30.0,
                updated_at=40.0,
            ),
        ]

        rollup = _build_executor_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"engineer"})
        row = rollup["engineer"]["work_items"][0]
        self.assertEqual(row["work_item_id"], "wi-engineer-review")
        self.assertTrue(row["is_review_target"])
        self.assertEqual(row["executor_role_id"], "engineer")
        self.assertEqual(row["reviewer_role_id"], "cto")
        self.assertEqual(row["kanban_column"], "in-review")
        self.assertEqual(rollup["engineer"]["aggregated_status"], "waiting")

    def test_executor_rollup_includes_final_delivery_for_leader(self) -> None:
        items = [
            self._make_item(
                work_item_id="wi-final-delivery",
                role_id="chief_analyst",
                phase=Phase.READY_FOR_REWORK,
                title="Final delivery",
                kind="delivery",
                parent_work_item_id="",
                metadata={
                    "work_item_turn_type": "deliver",
                    "feedback_scope": "final",
                    "authoritative_output": True,
                    "user_visible": True,
                    "requires_user_feedback": True,
                },
            ),
        ]

        rollup = _build_executor_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"chief_analyst"})
        row = rollup["chief_analyst"]["work_items"][0]
        self.assertEqual(row["work_item_id"], "wi-final-delivery")
        self.assertEqual(row["kind"], "delivery")
        self.assertEqual(rollup["chief_analyst"]["aggregated_status"], "waiting")

    def test_executor_rollup_preserves_six_executor_chips_during_review(self) -> None:
        items = [
            self._make_item(work_item_id="wi-market", role_id="market_researcher", phase=Phase.RUNNING),
            self._make_item(work_item_id="wi-sector", role_id="sector_analyst", phase=Phase.PAUSED),
            self._make_item(work_item_id="wi-scout", role_id="startup_scout", phase=Phase.APPROVED),
            self._make_item(
                work_item_id="wi-acq",
                role_id="acquisition_specialist",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                manager_role_id="coo",
            ),
            self._make_item(
                work_item_id="wi-devops",
                role_id="devops_engineer",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                manager_role_id="cto",
            ),
            self._make_item(
                work_item_id="wi-qa",
                role_id="qa_analyst",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                manager_role_id="coo",
            ),
        ]

        rollup = _build_executor_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(
            set(rollup.keys()),
            {
                "market_researcher",
                "sector_analyst",
                "startup_scout",
                "acquisition_specialist",
                "devops_engineer",
                "qa_analyst",
            },
        )
        self.assertEqual(rollup["acquisition_specialist"]["work_items"][0]["kanban_column"], "in-review")
        self.assertEqual(rollup["devops_engineer"]["work_items"][0]["reviewer_role_id"], "cto")
        self.assertEqual(rollup["qa_analyst"]["aggregated_status"], "waiting")

    def test_executor_rollup_filters_wrappers_but_keeps_deleted_business_rows(self) -> None:
        items = [
            self._make_item(
                work_item_id="wi-deleted",
                role_id="engineer",
                phase=Phase.CANCELLED,
                metadata={
                    "hidden_from_company_kanban": True,
                    "deleted_by_manager_tool": True,
                    "upstream_visibility": "hidden",
                },
            ),
            self._make_item(
                work_item_id="wi-review-wrapper",
                role_id="cto",
                phase=Phase.RUNNING,
                kind="review",
                metadata={
                    "hidden_from_company_kanban": True,
                    "review_execution_work_item": True,
                    "review_target_work_item_id": "wi-deleted",
                },
            ),
            self._make_item(
                work_item_id="wi-dispatch-wrapper",
                role_id="ceo",
                phase=Phase.CANCELLED,
                kind="dispatch",
                metadata={"hidden_from_company_kanban": True},
                parent_work_item_id="",
            ),
        ]

        rollup = _build_executor_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"engineer"})
        row = rollup["engineer"]["work_items"][0]
        self.assertEqual(row["work_item_id"], "wi-deleted")
        self.assertEqual(row["phase"], "cancelled")
        self.assertEqual(rollup["engineer"]["aggregated_status"], "failed")

    def test_skips_auxiliary_and_top_level_work_items(self) -> None:
        """Hidden / auxiliary cards (review_execution, report_execution,
        attention, hidden_from_company_kanban, non-intake top-level
        orchestration) must NOT get their own row — they are scheduling
        artefacts the user shouldn't see grouped by role. The leader's
        top-level ``intake`` is an intentional exception and is covered
        by ``test_top_level_intake_is_surfaced_for_leader``.
        """
        items = [
            # Top-level (no parent_work_item_id) with non-intake kind —
            # excluded for parity with the company kanban.
            self._make_item(
                work_item_id="root", role_id="ceo", phase=Phase.RUNNING,
                parent_work_item_id="",
            ),
            # Visible execution card — should appear.
            self._make_item(
                work_item_id="wi-real",
                role_id="engineer",
                phase=Phase.RUNNING,
            ),
            # review_execution work item — auxiliary, never a row.
            self._make_item(
                work_item_id="wi-review-exec",
                role_id="engineer",
                phase=Phase.RUNNING,
                kind="review",
                metadata={
                    "review_execution_work_item": True,
                    "review_target_work_item_id": "wi-real",
                },
            ),
            # report_execution work item — same.
            self._make_item(
                work_item_id="wi-report",
                role_id="engineer",
                phase=Phase.RUNNING,
                kind="report",
                metadata={
                    "report_execution_work_item": True,
                    "report_target_work_item_id": "wi-real",
                },
            ),
            self._make_item(
                work_item_id="wi-attn",
                role_id="engineer",
                phase=Phase.RUNNING,
                metadata={"attention_work_item": True},
            ),
            self._make_item(
                work_item_id="wi-hidden",
                role_id="engineer",
                phase=Phase.RUNNING,
                metadata={"hidden_from_company_kanban": True},
            ),
        ]

        rollup = _build_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"engineer"})
        self.assertEqual(
            [wi["work_item_id"] for wi in rollup["engineer"]["work_items"]],
            ["wi-real"],
        )

    def test_top_level_intake_is_surfaced_for_leader(self) -> None:
        """The leader's ``intake`` is parentless but must show on the role
        panel — it is the only work item the leader has during planning,
        and hiding it leaves the leader invisible on Execution Progress
        until children produce review activity.
        """
        items = [
            self._make_item(
                work_item_id="intake-root",
                role_id="ceo",
                phase=Phase.RUNNING,
                title="CEO Intake",
                kind="intake",
                parent_work_item_id="",
            ),
            self._make_item(
                work_item_id="wi-child",
                role_id="engineer",
                phase=Phase.RUNNING,
                manager_role_id="ceo",
                parent_work_item_id="intake-root",
            ),
        ]

        rollup = _build_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )

        self.assertEqual(set(rollup.keys()), {"ceo", "engineer"})
        self.assertEqual(
            [wi["work_item_id"] for wi in rollup["ceo"]["work_items"]],
            ["intake-root"],
        )

    def test_shared_role_session_pulls_progress_from_origin_task(self) -> None:
        """Leader-side intake / review turns ride on the user-facing primary
        chat: ``ws_handler`` redirects ``on_progress`` writes to the runtime
        task's ``origin_task_id``. The runtime task itself ends up with an
        empty ``task_progress`` row, so the rollup must fall back to the
        origin task's entries — otherwise the leader's intake renders as
        "No runtime activity yet" even though the LLM produced a full
        thinking/tool timeline. The fallback is time-windowed against the
        work item's ``created_at`` / ``updated_at`` so a sibling shared
        work item that also rides on the same origin task can't bleed in.
        """
        intake_running = self._make_item(
            work_item_id="intake-running",
            role_id="ceo",
            phase=Phase.RUNNING,
            title="CEO Intake",
            kind="intake",
            projection_id="corporate::intake::ceo",
            parent_work_item_id="",
            created_at=100.0,
            updated_at=200.0,
        )
        intake_done = self._make_item(
            work_item_id="intake-done",
            role_id="ceo",
            phase=Phase.APPROVED,
            title="CEO Intake (prior run)",
            kind="intake",
            projection_id="corporate::intake::ceo",
            parent_work_item_id="",
            created_at=10.0,
            updated_at=50.0,
        )
        # Both intake runtime tasks ride on the same origin (worst case for
        # bleed-through). They are explicitly marked ``shared_role_session``
        # with the user-facing ``origin_task_id``.
        task_by_work_item_id = {
            "intake-running": SimpleNamespace(
                id="leader-runtime-running",
                metadata={
                    "shared_role_session": True,
                    "origin_task_id": "user-primary",
                },
            ),
            "intake-done": SimpleNamespace(
                id="leader-runtime-done",
                metadata={
                    "shared_role_session": True,
                    "origin_task_id": "user-primary",
                },
            ),
        }
        # Origin task carries entries from both intake windows. Mid-window
        # runtime entries (no projection stamp) belong to whichever work item
        # was active at that timestamp; the time-window filter does the
        # partitioning so the projection-id filter alone doesn't have to.
        progress_by_task = {
            "leader-runtime-running": [],
            "leader-runtime-done": [],
            "user-primary": [
                # Done-intake window [10, 50]
                {"timestamp": 15.0, "type": "thinking", "summary": "done-think"},
                {"timestamp": 40.0, "type": "tool_call", "summary": "done-tool"},
                # Running-intake window [100, ∞)
                {"timestamp": 120.0, "type": "thinking", "summary": "running-think"},
                {"timestamp": 180.0, "type": "tool_call", "summary": "running-tool"},
                # Stamped sync events ride through unchanged
                {
                    "timestamp": 200.0,
                    "type": "gate_approved",
                    "summary": "intake-completed",
                    "work_item_projection_id": "corporate::intake::ceo",
                },
            ],
        }

        rollup = _build_role_work_items_for_session(
            work_items=[intake_done, intake_running],
            task_by_work_item_id=task_by_work_item_id,
            event_adapter=None,
            progress_by_task=progress_by_task,
        )

        ceo_rows = {row["work_item_id"]: row for row in rollup["ceo"]["work_items"]}
        done_log = ceo_rows["intake-done"]["progress_log"]
        running_log = ceo_rows["intake-running"]["progress_log"]
        self.assertEqual(
            sorted(entry["summary"] for entry in done_log),
            ["done-think", "done-tool"],
            "Done intake should only see entries within its [created_at, updated_at] window",
        )
        self.assertEqual(
            sorted(entry["summary"] for entry in running_log),
            ["intake-completed", "running-think", "running-tool"],
            "Running intake should see entries from its own window plus stamped sync events; "
            "no upper bound applies because the work item is still emitting events",
        )

    def test_auxiliary_review_and_report_activity_merges_into_target_row(self) -> None:
        """Hidden report/review execution work items are runtime carriers.
        They must not become rows, but their task_progress entries should
        remain visible inside the target work item's activity sections.
        """
        items = [
            self._make_item(
                work_item_id="wi-real",
                role_id="engineer",
                phase=Phase.RUNNING,
                projection_id="wi-real",
                created_at=10.0,
            ),
            self._make_item(
                work_item_id="wi-review-exec",
                role_id="cto",
                phase=Phase.RUNNING,
                kind="review",
                projection_id="wi-review-exec",
                created_at=30.0,
                metadata={
                    "review_execution_work_item": True,
                    "review_target_work_item_id": "wi-real",
                },
            ),
            self._make_item(
                work_item_id="wi-report",
                role_id="engineer",
                phase=Phase.RUNNING,
                kind="report",
                projection_id="wi-report",
                created_at=20.0,
                metadata={
                    "report_execution_work_item": True,
                    "report_target_work_item_id": "wi-real",
                },
            ),
        ]
        task_by_work_item_id = {
            "wi-real": SimpleNamespace(id="task-real", metadata={}),
            "wi-review-exec": SimpleNamespace(id="task-review", metadata={}),
            "wi-report": SimpleNamespace(id="task-report", metadata={}),
        }
        progress_by_task = {
            "task-real": [
                {
                    "timestamp": 1.0,
                    "type": "thinking",
                    "summary": "execute-think",
                    "work_item_projection_id": "wi-real",
                },
            ],
            "task-report": [
                {
                    "timestamp": 2.0,
                    "type": "tool_call",
                    "summary": "report-write",
                    "work_item_projection_id": "wi-report",
                },
            ],
            "task-review": [
                {
                    "timestamp": 3.0,
                    "type": "gate_result",
                    "summary": "review-check",
                    "work_item_projection_id": "wi-review-exec",
                },
            ],
        }

        rollup = _build_role_work_items_for_session(
            work_items=items,
            task_by_work_item_id=task_by_work_item_id,
            event_adapter=None,
            progress_by_task=progress_by_task,
        )

        self.assertEqual(set(rollup.keys()), {"engineer"})
        rows = rollup["engineer"]["work_items"]
        self.assertEqual([row["work_item_id"] for row in rows], ["wi-real"])

        row = rows[0]
        sections = row["activity_sections"]
        self.assertEqual([section["kind"] for section in sections], ["execute", "report", "review"])
        self.assertEqual(row["progress_log"][0]["summary"], "execute-think")
        summaries_by_kind = {
            section["kind"]: [entry["summary"] for entry in section["entries"]]
            for section in sections
        }
        self.assertEqual(summaries_by_kind["report"], ["report-write"])
        self.assertEqual(summaries_by_kind["review"], ["review-check"])

    def test_progress_log_filtered_by_projection_id(self) -> None:
        """A linked Task's ``progress_log`` may carry entries from multiple
        projections (rework execution + review). The row's ``progress_log``
        must surface only entries matching this work item's projection.
        """
        items = [
            self._make_item(
                work_item_id="wi-1",
                role_id="engineer",
                phase=Phase.RUNNING,
                projection_id="proj-engineer",
            ),
            self._make_item(
                work_item_id="wi-2",
                role_id="cto",
                phase=Phase.RUNNING,
                projection_id="proj-cto",
            ),
        ]
        # Single shared linked task whose progress_log carries entries for
        # both projections (mimicking how a shared role session's progress
        # gets aggregated by ``WorkItemContextView``).
        shared_task = SimpleNamespace(
            id="task-shared",
            metadata={
                "progress_log": [
                    {"timestamp": 1.0, "type": "tool_call", "summary": "engineer-step",
                     "work_item_projection_id": "proj-engineer"},
                    {"timestamp": 2.0, "type": "tool_call", "summary": "cto-step",
                     "work_item_projection_id": "proj-cto"},
                    {"timestamp": 3.0, "type": "thinking", "summary": "no-projection"},
                ],
            },
        )

        rollup = _build_role_work_items_for_session(
            work_items=items,
            task_by_work_item_id={"wi-1": shared_task, "wi-2": shared_task},
            event_adapter=None,
        )

        engineer_log = rollup["engineer"]["work_items"][0]["progress_log"]
        cto_log = rollup["cto"]["work_items"][0]["progress_log"]
        engineer_summaries = sorted(e["summary"] for e in engineer_log)
        cto_summaries = sorted(e["summary"] for e in cto_log)
        # Each row sees its own projection's entry plus the
        # projection-less one (kept for backwards compatibility — older
        # entries didn't stamp projection id).
        self.assertEqual(engineer_summaries, ["engineer-step", "no-projection"])
        self.assertEqual(cto_summaries, ["cto-step", "no-projection"])

    def test_work_items_sorted_chronologically_by_created_at(self) -> None:
        """The UI shows turns left-to-right in the order they first happened.
        Rows must be sorted by ``created_at`` ASC so a late ``updated_at``
        bump can never reorder the row history.
        """
        items = [
            self._make_item(
                work_item_id="wi-late",
                role_id="engineer",
                phase=Phase.APPROVED,
                created_at=300.0,
                updated_at=400.0,
            ),
            self._make_item(
                work_item_id="wi-early",
                role_id="engineer",
                phase=Phase.RUNNING,
                created_at=100.0,
                updated_at=999.0,  # looks "fresh", but came first
            ),
        ]
        rollup = _build_role_work_items_for_session(
            work_items=items, task_by_work_item_id={}, event_adapter=None,
        )
        self.assertEqual(
            [wi["work_item_id"] for wi in rollup["engineer"]["work_items"]],
            ["wi-early", "wi-late"],
        )

    def test_aggregated_status_table(self) -> None:
        """Lock the aggregation rules — the UI colour table is keyed off
        these strings. Drift here = silent UI regression.
        """
        # All approved → done
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=["approved", "approved"]),
            "done",
        )
        # Mixed terminal with failed → failed
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=["approved", "failed"]),
            "failed",
        )
        # Tracker live overrides anything non-terminal
        self.assertEqual(
            _role_aggregated_status(runtime_status="tool_active", work_item_phases=["queued"]),
            "active",
        )
        # Tracker live overrides a previously approved phase when a final
        # delivery role session is actively resumed for the next owner turn.
        self.assertEqual(
            _role_aggregated_status(runtime_status="tool_active", work_item_phases=["approved"]),
            "active",
        )
        # Phase-driven active when tracker is idle
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=["running", "queued"]),
            "active",
        )
        # In-review or todo → waiting
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=["awaiting_manager_review"]),
            "waiting",
        )
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=["queued"]),
            "waiting",
        )
        # No work items → pending
        self.assertEqual(
            _role_aggregated_status(runtime_status="idle", work_item_phases=[]),
            "pending",
        )

    def test_uses_work_item_updated_at_not_channel_timestamp(self) -> None:
        """Regression: the old session-driven panel pulled ``updated_at``
        from the latest channel message, so any stray write made finished
        turns flash "1s ago". The role rollup must read directly from
        ``WorkItem.updated_at`` (which only moves on phase transition).
        """
        item = self._make_item(
            work_item_id="wi-stable",
            role_id="engineer",
            phase=Phase.APPROVED,
            created_at=100.0,
            updated_at=150.0,
        )
        rollup = _build_role_work_items_for_session(
            work_items=[item], task_by_work_item_id={}, event_adapter=None,
        )
        row = rollup["engineer"]["work_items"][0]
        self.assertEqual(row["created_at"], 100.0)
        self.assertEqual(row["updated_at"], 150.0)


class CollabSyncCompanyModeTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: build_collab_sync must not emit a stale project-wide board
    in company/custom mode when no sessions exist.  That board caused a render
    loop (screen flicker) because it was auto-selected but the parent's
    session-driven effect had nothing to sync it with."""

    async def test_empty_company_mode_project_returns_no_boards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            chat_store = MagicMock()
            chat_store.get_channels = AsyncMock(return_value=[])
            chat_store.get_messages = AsyncMock(return_value=[])
            chat_store.get_session_channels = AsyncMock(return_value=[])
            chat_store.get_channel_stats = AsyncMock(return_value={})
            chat_store.get_progress_many = AsyncMock(return_value={})
            chat_store.delete_channel = AsyncMock(return_value=None)
            chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
            chat_store.delete_progress = AsyncMock(return_value=None)
            try:
                engine = MagicMock()
                engine.store = store
                engine.project_id = "proj-empty"
                engine.llm = None
                agent_store = MagicMock()
                result = await build_collab_sync(
                    engine, agent_store, chat_store, exec_mode="company",
                )
                self.assertEqual(result.get("boards", []), [])
                self.assertEqual(result.get("columns", []), [])
            finally:
                await store.close()

    async def test_empty_task_mode_project_still_has_project_board(self) -> None:
        """Non-company mode should still get its project-wide board."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            chat_store = MagicMock()
            chat_store.get_channels = AsyncMock(return_value=[])
            chat_store.get_messages = AsyncMock(return_value=[])
            chat_store.get_session_channels = AsyncMock(return_value=[])
            chat_store.get_channel_stats = AsyncMock(return_value={})
            chat_store.get_progress_many = AsyncMock(return_value={})
            chat_store.delete_channel = AsyncMock(return_value=None)
            chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
            chat_store.delete_progress = AsyncMock(return_value=None)
            try:
                engine = MagicMock()
                engine.store = store
                engine.project_id = "proj-task-mode"
                engine.llm = None
                agent_store = MagicMock()
                result = await build_collab_sync(
                    engine, agent_store, chat_store, exec_mode="task",
                )
                self.assertEqual(len(result.get("boards", [])), 1)
                self.assertEqual(result["boards"][0]["board_id"], "proj-task-mode")
            finally:
                await store.close()

    async def test_collab_sync_projects_external_team_summary_in_its_own_scope(self) -> None:
        """Regression: Team summary loading must live in build_collab_sync.

        It was previously initialized inside reconcile_sessions, leaving the
        collab-sync session projection with an undefined local variable.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            task = Task(
                id="team-task",
                title="Team task",
                project_id="proj-team",
                session_id="team-session",
                metadata={
                    "mode": "task",
                    "selected_execution_agent": "jiuwenswarm",
                    "execution_unit_kind": "opaque_external_team",
                },
            )
            await store.save_task(task)
            summary = {
                "external_invocation_id": "inv-team",
                "mode": "team_active",
                "counts": {"members": 2, "active_members": 1},
            }
            await store.save_external_session(
                ExternalSession(
                    agent_type="jiuwenswarm",
                    project_id="proj-team",
                    session_id="provider-team",
                    opc_session_id="team-session",
                    task_id="team-task",
                    metadata={
                        "execution_unit_kind": "opaque_external_team",
                        "external_invocation_id": "inv-team",
                        "external_team_summary": summary,
                    },
                )
            )

            created_at = task.created_at.timestamp()
            channel = {
                "channel_id": "session:team-task",
                "type": "session",
                "name": "Team task",
                "office_id": None,
                "participants": ["user"],
                "created_at": created_at,
            }
            chat_store = MagicMock()
            chat_store.get_channels = AsyncMock(return_value=[])
            chat_store.get_messages = AsyncMock(return_value=[])
            chat_store.get_session_channels = AsyncMock(return_value=[])
            chat_store.get_channel_stats = AsyncMock(return_value={})
            chat_store.get_progress_many = AsyncMock(return_value={})
            chat_store.create_session_channel = AsyncMock(return_value=channel)
            chat_store.delete_channel = AsyncMock(return_value=None)
            chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
            chat_store.delete_progress = AsyncMock(return_value=None)

            engine = MagicMock()
            engine.store = store
            engine.project_id = "proj-team"
            engine.llm = None
            engine.config.autonomy.native_approval_level = "auto"
            engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=None)
            engine._task_runtime_is_live = AsyncMock(return_value=False)
            try:
                with patch(
                    "opc.plugins.office_ui.snapshot_builder.reconcile_sessions",
                    new=AsyncMock(return_value=0),
                ):
                    result = await build_collab_sync(
                        engine,
                        MagicMock(),
                        chat_store,
                        exec_mode="task",
                    )
                self.assertEqual(
                    result["sessions"][0]["external_team_summary"],
                    summary,
                )
            finally:
                await store.close()

    async def test_org_mode_session_does_not_round_trip_as_task(self) -> None:
        created_at = datetime.now(timezone.utc)
        org_task = SimpleNamespace(
            id="org-session-task",
            session_id="org-session",
            parent_session_id="",
            title="Org session",
            status="pending",
            assigned_to="",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                "preferred_agent": "codex",
            },
        )
        engine = MagicMock()
        engine.store = MagicMock()
        engine.store.get_tasks = AsyncMock(return_value=[org_task])
        engine.project_id = "proj-org"
        engine.llm = None

        chat_store = MagicMock()
        chat_store.get_channels = AsyncMock(return_value=[])
        chat_store.get_messages = AsyncMock(return_value=[])
        chat_store.get_session_channels = AsyncMock(return_value=[])
        chat_store.get_channel_stats = AsyncMock(return_value={})
        chat_store.get_progress_many = AsyncMock(return_value={})
        chat_store.delete_channel = AsyncMock(return_value=None)
        chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
        chat_store.delete_progress = AsyncMock(return_value=None)
        chat_store.create_session_channel = AsyncMock(
            side_effect=lambda task_id, title, project_id=None: {
                "channel_id": f"session:{task_id}",
                "type": "session",
                "name": title,
                "office_id": None,
                "participants": ["user"],
                "created_at": created_at.timestamp(),
            }
        )

        with (
            patch(
                "opc.plugins.office_ui.snapshot_builder.reconcile_sessions",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "opc.plugins.office_ui.snapshot_builder.build_company_kanban_projection",
                new=AsyncMock(return_value=([], [], [], None)),
            ),
        ):
            result = await build_collab_sync(
                engine,
                MagicMock(),
                chat_store,
                exec_mode="org",
            )

        session = result["sessions"][0]
        self.assertEqual(session["exec_mode"], "org")
        self.assertEqual(session["company_profile"], "custom")
        self.assertEqual(session["preferred_agent"], "codex")

    async def test_build_collab_sync_does_not_promote_shared_role_session_without_ui_anchor(self) -> None:
        created_at = datetime.now(timezone.utc)
        shared_session_id = "root-session:role:cto"
        pending_task = SimpleNamespace(
            id="task-pending",
            session_id=shared_session_id,
            parent_session_id="",
            title="Implement feature",
            status="pending",
            assigned_to="cto",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={
                "shared_role_session": True,
                "work_item_role_id": "cto",
            },
        )
        running_task = SimpleNamespace(
            id="task-running",
            session_id=shared_session_id,
            parent_session_id="",
            title="Implement feature",
            status="running",
            assigned_to="cto",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={
                "shared_role_session": True,
                "work_item_role_id": "cto",
                "authoritative_output": True,
            },
        )
        engine = MagicMock()
        engine.store = MagicMock()
        engine.store.get_tasks = AsyncMock(return_value=[pending_task, running_task])
        engine.store.get_execution_checkpoints = AsyncMock(return_value=[])
        engine.project_id = "proj-shared"
        engine.llm = None

        chat_store = MagicMock()
        chat_store.get_channels = AsyncMock(return_value=[])
        chat_store.get_messages = AsyncMock(return_value=[])
        chat_store.get_session_channels = AsyncMock(return_value=[])
        chat_store.get_channel_stats = AsyncMock(return_value={})
        chat_store.get_progress_many = AsyncMock(return_value={
            "root-task": [],
            "ceo-intake-task": [
                {"timestamp": 10.0, "type": "work_item_started", "detail": "starting CEO Intake"},
            ],
        })
        chat_store.delete_channel = AsyncMock(return_value=None)
        chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
        chat_store.delete_progress = AsyncMock(return_value=None)
        chat_store.create_session_channel = AsyncMock(
            side_effect=lambda task_id, title, project_id=None: {
                "channel_id": f"session:{task_id}",
                "type": "session",
                "name": title,
                "office_id": None,
                "participants": ["user"],
                "created_at": created_at.timestamp(),
            }
        )

        projection = (
            [],
            [{
                "column_id": "todo",
                "board_id": "board-1",
                "name": "Todo",
                "position": 0,
                "wip_limit": None,
            }],
            [{
                "board_id": "board-1",
                "name": "Board 1",
                "description": "",
                "prefix": "OPC",
                "color": "#4f46e5",
                "next_task_num": 1,
                "created_at": created_at.timestamp(),
                "updated_at": created_at.timestamp(),
            }],
            None,
        )
        with (
            patch(
                "opc.plugins.office_ui.snapshot_builder.reconcile_sessions",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "opc.plugins.office_ui.snapshot_builder.build_company_kanban_projection",
                new=AsyncMock(return_value=projection),
            ),
        ):
            result = await build_collab_sync(
                engine,
                MagicMock(),
                chat_store,
                exec_mode="company",
            )

        sessions = result.get("sessions", [])
        self.assertEqual(sessions, [])

    async def test_build_collab_sync_keeps_root_session_visible_when_final_decider_shares_session(self) -> None:
        created_at = datetime.now(timezone.utc)
        shared_session_id = "root-session"
        root_task = SimpleNamespace(
            id="root-task",
            session_id=shared_session_id,
            parent_session_id="",
            title="app17",
            status="running",
            assigned_to="",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={"origin_task_id": "root-task"},
        )
        shared_role_task = SimpleNamespace(
            id="ceo-intake-task",
            session_id=shared_session_id,
            parent_session_id=shared_session_id,
            title="CEO Intake",
            status="running",
            assigned_to="ceo",
            priority=None,
            tags=[],
            created_at=created_at,
            metadata={
                "shared_role_session": True,
                "shared_role_id": "ceo",
                "company_runtime_root_session_id": shared_session_id,
                "work_item_projection_id": "ceo-intake",
                "origin_task_id": "root-task",
                "selected_execution_agent": "codex",
            },
        )
        engine = MagicMock()
        engine.store = MagicMock()
        engine.store.get_tasks = AsyncMock(return_value=[root_task, shared_role_task])
        engine.project_id = "proj-shared-root"
        engine.llm = None

        chat_store = MagicMock()
        chat_store.get_channels = AsyncMock(return_value=[])
        chat_store.get_messages = AsyncMock(return_value=[])
        chat_store.get_session_channels = AsyncMock(return_value=[])
        chat_store.get_channel_stats = AsyncMock(return_value={})
        chat_store.get_progress_many = AsyncMock(return_value={
            "root-task": [],
            "ceo-intake-task": [
                {"timestamp": 10.0, "type": "work_item_started", "detail": "starting CEO Intake"},
            ],
        })
        chat_store.delete_channel = AsyncMock(return_value=None)
        chat_store.delete_activity_messages_for_task = AsyncMock(return_value=None)
        chat_store.delete_progress = AsyncMock(return_value=None)
        chat_store.create_session_channel = AsyncMock(
            side_effect=lambda task_id, title, project_id=None: {
                "channel_id": f"session:{task_id}",
                "type": "session",
                "name": title,
                "office_id": None,
                "participants": ["user"],
                "created_at": created_at.timestamp(),
            }
        )

        projection = (
            [],
            [{
                "column_id": "todo",
                "board_id": "root-task",
                "name": "Todo",
                "position": 0,
                "wip_limit": None,
            }],
            [{
                "board_id": "root-task",
                "name": "app17",
                "description": "",
                "prefix": "OPC",
                "color": "#4f46e5",
                "next_task_num": 1,
                "created_at": created_at.timestamp(),
                "updated_at": created_at.timestamp(),
            }],
            None,
        )
        with (
            patch(
                "opc.plugins.office_ui.snapshot_builder.reconcile_sessions",
                new=AsyncMock(return_value=0),
            ),
            patch(
                "opc.plugins.office_ui.snapshot_builder.build_company_kanban_projection",
                new=AsyncMock(return_value=projection),
            ),
        ):
            result = await build_collab_sync(
                engine,
                MagicMock(),
                chat_store,
                exec_mode="company",
            )

        sessions = result.get("sessions", [])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["task_id"], "root-task")
        self.assertEqual(sessions[0]["origin_task_id"], "root-task")
        self.assertEqual(sessions[0]["work_item_role_id"], "ceo")
        self.assertEqual(sessions[0]["work_item_role_name"], "CEO")
        self.assertEqual(sessions[0]["selected_execution_agent"], "codex")
        self.assertEqual(sessions[0]["assignee_ids"], ["ceo"])
        self.assertEqual(sessions[0]["progress_log"][0]["detail"], "starting CEO Intake")
        self.assertEqual(sessions[0]["work_item_log"][0]["role_name"], "CEO")
        self.assertEqual(sessions[0]["work_item_log"][0]["execution_turn_id"], "ceo-intake-task")
        self.assertEqual(result["boards"][0]["board_id"], "root-task")
