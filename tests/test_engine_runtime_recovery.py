from __future__ import annotations

import os
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opc.core.active_task_runs import ActiveTaskRunRegistry
from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.core.models import (
    CompanyMemberSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
    serialize_company_work_item_plan,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id


def _async_test(func):
    @wraps(func)
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


def _runtime_plan() -> CompanyWorkItemRuntimePlan:
    return CompanyWorkItemRuntimePlan(
        profile="corporate",
        projections=[
            WorkItemProjectionSpec(
                projection_id="execution",
                turn_type="execute",
                title="Execution",
                summary="Execute the work.",
                role_id="executor",
            )
        ],
        metadata={"execution_model": "multi_team_org"},
    )


def _runtime_task(*, task_id: str, status: TaskStatus, metadata: dict | None = None) -> Task:
    return Task(
        id=task_id,
        title="Execution",
        session_id=f"role-session-{task_id}",
        parent_session_id="root-session",
        project_id="project-a",
        status=status,
        metadata={
            "work_item_projection_id": "execution",
            "work_item_runtime": True,
            "execution_model": "multi_team_org",
            "company_work_item_plan": serialize_company_work_item_plan(_runtime_plan()),
            **dict(metadata or {}),
        },
    )


def _ui_anchor(*, status: TaskStatus = TaskStatus.IDLE) -> Task:
    return Task(
        id="ui-anchor",
        title="Company chat",
        session_id="root-session",
        project_id="project-a",
        status=status,
        metadata={"exec_mode": "company", "company_profile": "corporate"},
    )


@pytest.mark.parametrize("pid", [None, os.getpid(), 99999999])
@_async_test
async def test_startup_creates_exactly_one_checkpoint_without_persisted_liveness(
    tmp_path: Path,
    pid: int | None,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        task = _runtime_task(task_id="running-task", status=TaskStatus.RUNNING)
        await store.save_task(task)
        await store.save_external_session(
            ExternalSession(
                agent_type="codex",
                project_id="project-a",
                session_id="codex:project-a:running-task",
                opc_session_id=str(task.session_id or ""),
                task_id=task.id,
                workspace_path=str(tmp_path),
                run_mode="interactive",
                status="working",
                metadata={
                    **({"pid": pid} if pid is not None else {}),
                    "status_heartbeat_seconds": 30,
                },
                updated_at=datetime.now(),
            )
        )
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine.company_executor = SimpleNamespace(execute=AsyncMock())

        assert await engine._reconcile_interrupted_project_tasks() == 1
        assert await engine._reconcile_interrupted_project_tasks() == 0
        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending", "resuming"],
        )

        assert len(checkpoints) == 1
        engine.company_executor.execute.assert_not_awaited()
    finally:
        await store.close()


@_async_test
async def test_startup_terminalizes_only_interrupted_generic_auxiliary_runtimes(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        interrupted: list[tuple[Task, str]] = []
        for kind in ("role_prompt", "meeting_turn"):
            runtime_id = f"runtime-{kind}"
            task = Task(
                id=f"source::{kind}::attempt-1",
                session_id=f"private-{kind}-session",
                parent_session_id="source-session",
                parent_id="source",
                title=f"Private {kind}",
                project_id="project-a",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                execution_lock=True,
                execution_locked_at=datetime.now(),
                context_snapshot={"skip_session_history": True},
                metadata={
                    "runtime_auxiliary_task": True,
                    "runtime_auxiliary_kind": kind,
                    "runtime_auxiliary_source_task_id": "source",
                    "runtime_v2": {
                        "runtime_session_id": runtime_id,
                        "status": "running",
                        "audit_value": f"keep-{kind}",
                    },
                },
            )
            await store.save_task(task)
            await store.save_runtime_session(
                runtime_session_id=runtime_id,
                project_id="project-a",
                session_id=task.session_id,
                task_id=task.id,
                status="running",
                metadata={
                    "status": "running",
                    "audit_value": f"keep-{kind}",
                },
            )
            await store.save_runtime_transcript_entry(
                runtime_session_id=runtime_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id=f"message-{kind}",
                role="assistant",
                content=f"partial {kind} answer",
                metadata={"audit_value": f"keep-{kind}"},
            )
            interrupted.append((task, runtime_id))

        terminal = Task(
            id="source::role_prompt::already-terminal",
            session_id="private-terminal-session",
            parent_session_id="source-session",
            parent_id="source",
            title="Completed private role prompt",
            project_id="project-a",
            assigned_to="executor",
            status=TaskStatus.DONE,
            metadata={
                "runtime_auxiliary_task": True,
                "runtime_auxiliary_kind": "role_prompt",
                "runtime_auxiliary_source_task_id": "source",
                "runtime_v2": {
                    "runtime_session_id": "runtime-terminal",
                    "status": "completed",
                },
            },
        )
        await store.save_task(terminal)
        await store.save_runtime_session(
            runtime_session_id="runtime-terminal",
            project_id="project-a",
            session_id=terminal.session_id,
            task_id=terminal.id,
            status="completed",
            metadata={"status": "completed", "audit_value": "keep-terminal"},
        )

        engine = OPCEngine(project_id="project-a")
        engine.store = store
        assert await engine._reconcile_interrupted_project_tasks() == 2
        assert await engine._reconcile_interrupted_project_tasks() == 0

        for original, runtime_id in interrupted:
            durable = await store.get_task(original.id)
            runtime = await store.get_runtime_session(runtime_id)
            transcript = await store.list_runtime_transcript_entries(runtime_id)
            assert durable is not None and durable.status == TaskStatus.FAILED
            assert not durable.execution_lock
            assert durable.execution_locked_at is None
            assert durable.metadata["runtime_auxiliary_terminal_reason"] == (
                "startup_interrupted"
            )
            assert durable.metadata["runtime_v2"]["status"] == "failed"
            assert durable.metadata["runtime_v2"]["audit_value"] == (
                f"keep-{durable.metadata['runtime_auxiliary_kind']}"
            )
            assert runtime is not None and runtime["status"] == "failed"
            assert runtime["metadata"]["runtime_auxiliary_terminal_reason"] == (
                "startup_interrupted"
            )
            assert runtime["metadata"]["audit_value"] == (
                f"keep-{durable.metadata['runtime_auxiliary_kind']}"
            )
            assert [entry["content"] for entry in transcript] == [
                f"partial {durable.metadata['runtime_auxiliary_kind']} answer"
            ]

        terminal_after = await store.get_task(terminal.id)
        terminal_runtime = await store.get_runtime_session("runtime-terminal")
        assert terminal_after is not None and terminal_after.status == TaskStatus.DONE
        assert "runtime_auxiliary_terminal_reason" not in terminal_after.metadata
        assert terminal_runtime is not None
        assert terminal_runtime["status"] == "completed"
        assert terminal_runtime["metadata"]["audit_value"] == "keep-terminal"
        assert await store.get_pending_checkpoints(project_id="project-a") == []
    finally:
        await store.close()


@_async_test
async def test_startup_normalizes_duplicate_active_scope_checkpoints(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        task = _runtime_task(
            task_id="duplicate-checkpoint-task",
            status=TaskStatus.RUNNING,
        )
        await store.save_task(task)
        older = ExecutionCheckpoint(
            checkpoint_id="checkpoint-older",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id=task.id,
            payload={"reason": "older"},
        )
        winner = ExecutionCheckpoint(
            checkpoint_id="checkpoint-winner",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_runtime_suspended",
            status="resuming",
            task_id=task.id,
            payload={"reason": "winner"},
        )
        winner.updated_at = older.updated_at + timedelta(microseconds=1)
        await store.save_execution_checkpoint(older)
        await store.save_execution_checkpoint(winner)

        engine = OPCEngine(project_id="project-a")
        engine.store = store
        await engine._reconcile_interrupted_project_tasks()

        active = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=[
                "company_runtime_interrupted",
                "company_runtime_suspended",
            ],
            statuses=["pending", "resuming"],
        )
        assert [checkpoint.checkpoint_id for checkpoint in active] == [
            winner.checkpoint_id
        ]
        assert active[0].status == "pending"

        await store.resolve_execution_checkpoint(winner.checkpoint_id)
        assert await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=[
                "company_runtime_interrupted",
                "company_runtime_suspended",
            ],
            statuses=["pending", "resuming"],
        ) == []
        all_rows = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
        )
        by_id = {checkpoint.checkpoint_id: checkpoint for checkpoint in all_rows}
        assert by_id[older.checkpoint_id].status == "superseded"
        assert by_id[winner.checkpoint_id].status == "resolved"
    finally:
        await store.close()


@_async_test
async def test_legacy_failed_interruption_is_migrated_but_business_failure_is_not(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        legacy = _runtime_task(
            task_id="legacy-interrupted",
            status=TaskStatus.FAILED,
            metadata={
                "interrupted_recovery": {
                    "previous_status": "running",
                    "reason": "process disappeared",
                }
            },
        )
        legacy.result = {
            "content": "interrupted",
            "artifacts": {
                "interrupted": True,
                "interrupted_previous_status": "running",
            },
        }
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="legacy-work-item",
                run_id="legacy-run",
                cell_id="team::executor",
                role_id="executor",
                seat_id="seat::executor",
                title="Legacy work",
                kind="execute",
                projection_id="execution",
                phase=Phase.PAUSED,
            )
        )
        set_linked_work_item_id(legacy, "legacy-work-item")
        await store.save_task(legacy)
        await store.link_work_item_runtime_task("legacy-work-item", legacy.id)

        # Use a separate scope so the genuine failure cannot be included in
        # the legacy task's scope checkpoint.
        genuine = _runtime_task(
            task_id="business-failure",
            status=TaskStatus.FAILED,
            metadata={"interrupted_recovery": {"previous_status": "running"}},
        )
        genuine.parent_session_id = "failed-root-session"
        genuine.result = {
            "content": "real failure",
            "artifacts": {"interrupted": True, "interrupted_previous_status": "running"},
        }
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="failed-work-item",
                run_id="failed-run",
                cell_id="team::executor",
                role_id="executor",
                seat_id="seat::executor",
                title="Failed work",
                kind="execute",
                projection_id="execution",
                phase=Phase.FAILED,
            )
        )
        set_linked_work_item_id(genuine, "failed-work-item")
        await store.save_task(genuine)
        await store.link_work_item_runtime_task("failed-work-item", genuine.id)

        engine = OPCEngine(project_id="project-a")
        engine.store = store
        await engine._reconcile_interrupted_project_tasks()

        migrated = await store.get_task(legacy.id)
        untouched = await store.get_task(genuine.id)
        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending", "resuming"],
        )
        failed_checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="failed-root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending", "resuming"],
        )

        assert migrated is not None
        assert migrated.status != TaskStatus.FAILED
        assert "interrupted_recovery" not in migrated.metadata
        assert not (migrated.result or {}).get("artifacts", {}).get("interrupted")
        assert checkpoints[0].payload["legacy_interrupted_recovery"][0]["task_id"] == legacy.id
        assert untouched is not None and untouched.status == TaskStatus.FAILED
        assert failed_checkpoints == []
    finally:
        await store.close()


@_async_test
async def test_legacy_migration_does_not_change_task_before_checkpoint_commit(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        legacy = _runtime_task(
            task_id="legacy-crash-window",
            status=TaskStatus.FAILED,
            metadata={"interrupted_recovery": {"previous_status": "running"}},
        )
        await store.save_task(legacy)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine._save_company_runtime_suspend_checkpoint = AsyncMock(
            side_effect=RuntimeError("checkpoint write failed")
        )

        with pytest.raises(RuntimeError, match="checkpoint write failed"):
            await engine._reconcile_interrupted_project_tasks()

        persisted = await store.get_task(legacy.id)
        assert persisted is not None and persisted.status == TaskStatus.FAILED
        assert persisted.metadata["interrupted_recovery"]["previous_status"] == "running"
    finally:
        await store.close()


@_async_test
async def test_initialize_fails_closed_when_startup_checkpoint_write_fails(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_task(
            _runtime_task(task_id="startup-write-failure", status=TaskStatus.RUNNING)
        )
        engine = OPCEngine(
            opc_home=tmp_path / "opc-home",
            project_id="project-a",
            store=store,
            owns_store=False,
        )
        engine._save_company_runtime_suspend_checkpoint = AsyncMock(
            side_effect=RuntimeError("checkpoint write failed")
        )

        with pytest.raises(RuntimeError, match="checkpoint write failed"):
            await engine.initialize()

        assert not engine._initialized
    finally:
        await store.close()


@_async_test
async def test_shutdown_preparation_reuses_existing_checkpoint(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    registry = ActiveTaskRunRegistry()
    try:
        task = _runtime_task(task_id="active-task", status=TaskStatus.RUNNING)
        work_item = DelegationWorkItem(
            work_item_id="active-work-item",
            run_id="active-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Active work",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id="runtime-session",
            claimed_by_seat_id="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-session",
                "claimed_task_id": task.id,
            },
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        token = registry.register("project-a", task.id)
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=registry,
            owns_active_task_run_registry=True,
        )
        engine.store = store

        first = await engine.prepare_active_company_runtimes_for_shutdown()
        task_after_first = await store.get_task(task.id)
        work_item_after_first = await store.get_delegation_work_item(work_item.work_item_id)
        second = await engine.prepare_active_company_runtimes_for_shutdown()
        task_after_second = await store.get_task(task.id)
        work_item_after_second = await store.get_delegation_work_item(work_item.work_item_id)
        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending", "resuming"],
        )

        assert len(checkpoints) == 1
        assert first[0]["checkpoint_id"] == second[0]["checkpoint_id"]
        assert not first[0]["idempotent"]
        assert second[0]["idempotent"]
        assert second[0]["task_ids"] == []
        assert task_after_first is not None and task_after_second is not None
        assert task_after_second.metadata == task_after_first.metadata
        assert work_item_after_first is not None and work_item_after_second is not None
        assert work_item_after_second.metadata == work_item_after_first.metadata
        assert engine._shutting_down
        registry.unregister("project-a", task.id, token)
    finally:
        await store.close()


@_async_test
async def test_shutdown_captures_post_execution_pre_finalize_work_item(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    registry = ActiveTaskRunRegistry()
    try:
        await store.save_task(_ui_anchor())
        task = _runtime_task(task_id="finalize-gap-task", status=TaskStatus.DONE)
        work_item = DelegationWorkItem(
            work_item_id="finalize-gap-item",
            run_id="finalize-gap-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Finalize gap",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        token = registry.register("project-a", task.id)
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=registry,
            owns_active_task_run_registry=True,
        )
        engine.store = store

        prepared = await engine.prepare_active_company_runtimes_for_shutdown()

        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        persisted_task = await store.get_task(task.id)
        persisted_item = await store.get_delegation_work_item(work_item.work_item_id)
        assert len(prepared) == 1
        assert len(checkpoints) == 1
        assert persisted_task is not None and persisted_task.status == TaskStatus.DONE
        assert persisted_task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert persisted_item is not None and persisted_item.phase == Phase.RUNNING
        assert persisted_item.metadata["dispatch_hold"] == "company_runtime_suspended"
        registry.unregister("project-a", task.id, token)
    finally:
        await store.close()


@_async_test
async def test_shutdown_admission_close_cannot_orphan_claim_before_child_spawn(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    registry = ActiveTaskRunRegistry()
    driver_ownership = None
    child_execution = None
    try:
        await store.save_task(_ui_anchor())
        task = _runtime_task(task_id="claim-spawn-gap-task", status=TaskStatus.PENDING)
        work_item = DelegationWorkItem(
            work_item_id="claim-spawn-gap-item",
            run_id="claim-spawn-gap-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Claim/spawn gap",
            kind="execute",
            projection_id="execution",
            phase=Phase.READY,
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=registry,
            owns_active_task_run_registry=True,
        )
        engine.store = store
        executor = object.__new__(CompanyWorkItemExecutor)
        executor.active_task_run_registry = registry
        child_started = asyncio.Event()
        hold_child = asyncio.Event()

        async def run_claimed(*_args: object, **_kwargs: object):
            child_started.set()
            await hold_child.wait()

        executor._run_claimed_work_item = run_claimed
        driver_ownership = executor.acquire_driver_ownership([task])
        assert driver_ownership is not None
        with driver_ownership.bind():
            claimed = await store.claim_delegation_work_item_if_dispatchable(
                work_item.work_item_id,
                expected_phase=Phase.READY,
                role_runtime_session_id="role-session",
                seat_id="seat::executor",
                task_id=task.id,
            )
            assert claimed is not None
            registry.close_admission()
            child_execution = executor._create_claimed_work_item_task(
                CompanyMemberSession(
                    member_session_id="member-session",
                    role_session_id="role-session",
                    role_id="executor",
                    seat_id="seat::executor",
                ),
                task,
                {},
            )
            await child_started.wait()
            prepared = await engine.prepare_active_company_runtimes_for_shutdown()

        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        item_after = await store.get_delegation_work_item(work_item.work_item_id)
        assert len(prepared) == 1
        assert len(checkpoints) == 1
        assert item_after is not None
        assert item_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item_after.claimed_by_role_runtime_session_id == ""
    finally:
        if child_execution is not None:
            child_execution.cancel()
            await asyncio.gather(child_execution, return_exceptions=True)
        if driver_ownership is not None:
            driver_ownership.release()
        await store.close()


@_async_test
async def test_startup_uses_nonterminal_work_item_over_terminal_task_projection(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_task(_ui_anchor())
        stale_task = _runtime_task(task_id="stale-terminal-task", status=TaskStatus.DONE)
        terminal_task = _runtime_task(task_id="terminal-pair-task", status=TaskStatus.DONE)
        stale_item = DelegationWorkItem(
            work_item_id="stale-nonterminal-item",
            run_id="terminal-projection-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Still running",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
        )
        terminal_item = DelegationWorkItem(
            work_item_id="terminal-item",
            run_id="terminal-projection-run",
            cell_id="team::reviewer",
            role_id="reviewer",
            seat_id="seat::reviewer",
            title="Already approved",
            kind="review",
            projection_id="review",
            phase=Phase.APPROVED,
        )
        for task, item in (
            (stale_task, stale_item),
            (terminal_task, terminal_item),
        ):
            await store.save_delegation_work_item(item)
            set_linked_work_item_id(task, item.work_item_id)
            await store.save_task(task)
            await store.link_work_item_runtime_task(item.work_item_id, task.id)
        engine = OPCEngine(project_id="project-a")
        engine.store = store

        updated = await engine._reconcile_interrupted_project_tasks()

        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        stale_after = await store.get_task(stale_task.id)
        stale_item_after = await store.get_delegation_work_item(stale_item.work_item_id)
        terminal_after = await store.get_task(terminal_task.id)
        terminal_item_after = await store.get_delegation_work_item(terminal_item.work_item_id)
        assert updated >= 1
        assert len(checkpoints) == 1
        assert stale_after is not None and stale_after.status == TaskStatus.DONE
        assert stale_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert stale_item_after is not None
        assert stale_item_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert terminal_after is not None and terminal_after.status == TaskStatus.DONE
        assert "dispatch_hold" not in terminal_after.metadata
        assert terminal_item_after is not None and terminal_item_after.phase == Phase.APPROVED
        assert "dispatch_hold" not in terminal_item_after.metadata
    finally:
        await store.close()


@_async_test
async def test_startup_interrupts_ready_work_despite_stale_human_task_projection(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_task(_ui_anchor())
        task = _runtime_task(
            task_id="pre-claim-task",
            status=TaskStatus.AWAITING_HUMAN,
        )
        work_item = DelegationWorkItem(
            work_item_id="pre-claim-item",
            run_id="pre-claim-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Ready before first claim",
            kind="execute",
            projection_id="execution",
            phase=Phase.READY,
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine.company_executor = SimpleNamespace(execute=AsyncMock())

        updated = await engine._reconcile_interrupted_project_tasks()

        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        task_after = await store.get_task(task.id)
        item_after = await store.get_delegation_work_item(work_item.work_item_id)
        assert updated >= 1
        assert len(checkpoints) == 1
        assert task_after is not None
        assert task_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item_after is not None and item_after.phase == Phase.READY
        assert item_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        engine.company_executor.execute.assert_not_awaited()
    finally:
        await store.close()


@_async_test
async def test_startup_preserves_stable_human_work_item_despite_stale_task_projection(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_task(_ui_anchor())
        task = _runtime_task(
            task_id="stable-human-task",
            status=TaskStatus.PENDING,
        )
        work_item = DelegationWorkItem(
            work_item_id="stable-human-item",
            run_id="stable-human-run",
            cell_id="team::reviewer",
            role_id="reviewer",
            seat_id="seat::reviewer",
            title="Awaiting human",
            kind="review",
            projection_id="execution",
            phase=Phase.AWAITING_HUMAN,
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        engine = OPCEngine(project_id="project-a")
        engine.store = store

        updated = await engine._reconcile_interrupted_project_tasks()
        first_task_after = await store.get_task(task.id)
        assert first_task_after is not None
        first_marker = dict(
            first_task_after.metadata.get(
                "startup_reconcile_preserved_waiting_state",
                {},
            )
            or {}
        )
        repeated = await engine._reconcile_interrupted_project_tasks()

        interrupted = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending", "resuming"],
        )
        task_after = await store.get_task(task.id)
        item_after = await store.get_delegation_work_item(work_item.work_item_id)
        assert updated >= 1
        assert repeated == 0
        assert interrupted == []
        assert task_after is not None
        assert task_after.status == TaskStatus.PENDING
        assert task_after.metadata["startup_reconcile_preserved_waiting_state"] == first_marker
        assert "dispatch_hold" not in task_after.metadata
        assert item_after is not None and item_after.phase == Phase.AWAITING_HUMAN
        assert "dispatch_hold" not in item_after.metadata
    finally:
        await store.close()


@_async_test
async def test_resume_candidates_use_nonterminal_work_item_over_terminal_task(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        task = _runtime_task(
            task_id="terminal-resume-projection",
            status=TaskStatus.DONE,
            metadata={
                "dispatch_hold": "company_runtime_suspended",
                "company_runtime_stop_state": "suspended",
                "suspended_task_status": TaskStatus.DONE.value,
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="terminal-resume-item",
            run_id="terminal-resume-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Resume authoritative item",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
            metadata={
                "dispatch_hold": "company_runtime_suspended",
                "suspended_phase": Phase.RUNNING.value,
            },
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        engine = OPCEngine(project_id="project-a")
        engine.store = store

        candidates = await engine._company_suspend_resume_candidate_task_ids([task])
        resumed = await engine._prepare_company_runtime_tasks_for_resume(
            [task],
            {
                "checkpoint_id": "resume-checkpoint",
                "checkpoint_type": "company_runtime_interrupted",
                "active_work_items": [
                    {
                        "work_item_id": work_item.work_item_id,
                        "phase": Phase.RUNNING.value,
                    }
                ],
                "task_snapshots": [
                    {
                        "task_id": task.id,
                        "status": TaskStatus.DONE.value,
                        "assigned_to": "executor",
                        "assigned_external_agent": "",
                        "selected_execution_agent": "native",
                        "work_item_id": work_item.work_item_id,
                        "work_item": {
                            "work_item_id": work_item.work_item_id,
                            "phase": Phase.RUNNING.value,
                            "role_id": "executor",
                            "seat_id": "seat::executor",
                            "role_runtime_session_id": "",
                            "metadata": {},
                        },
                    }
                ],
            },
            resume_task_ids={task.id},
        )

        persisted_item = await store.get_delegation_work_item(work_item.work_item_id)
        assert candidates == {task.id}
        assert resumed[0].status == TaskStatus.RUNNING
        assert "dispatch_hold" not in resumed[0].metadata
        assert persisted_item is not None and persisted_item.phase == Phase.RUNNING
        assert not str(persisted_item.metadata.get("dispatch_hold", "") or "").strip()
    finally:
        await store.close()


@_async_test
async def test_user_stop_and_shutdown_share_exactly_one_scope_checkpoint(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    registry = ActiveTaskRunRegistry()
    try:
        task = _runtime_task(task_id="concurrent-task", status=TaskStatus.RUNNING)
        work_item = DelegationWorkItem(
            work_item_id="concurrent-work-item",
            run_id="concurrent-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Concurrent work",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
        )
        await store.save_task(_ui_anchor())
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        token = registry.register("project-a", task.id)
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=registry,
            owns_active_task_run_registry=True,
        )
        engine.store = store

        stopped, prepared = await asyncio.gather(
            engine.suspend_company_runtime(
                origin_task_id=task.id,
                session_id="root-session",
                reason="user_stop",
            ),
            engine.prepare_active_company_runtimes_for_shutdown(),
        )
        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=[
                "company_runtime_suspended",
                "company_runtime_interrupted",
            ],
            statuses=["pending", "resuming"],
        )

        assert stopped is not None
        assert len(prepared) == 1
        assert len(checkpoints) == 1
        assert stopped["checkpoint_id"] == prepared[0]["checkpoint_id"]
        assert {stopped["idempotent"], prepared[0]["idempotent"]} == {False, True}
        registry.unregister("project-a", task.id, token)
    finally:
        await store.close()


@_async_test
async def test_atomic_work_item_claim_rejects_durable_hold_and_post_commit_stop(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        held = DelegationWorkItem(
            work_item_id="held-claim-item",
            run_id="claim-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Held claim",
            kind="execute",
            projection_id="held",
            phase=Phase.READY,
            metadata={"dispatch_hold": "company_runtime_suspended"},
        )
        await store.save_delegation_work_item(held)
        rejected = await store.claim_delegation_work_item_if_dispatchable(
            held.work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session",
            seat_id="seat::executor",
            task_id="runtime-task",
        )
        held_after = await store.get_delegation_work_item(held.work_item_id)
        assert rejected is None
        assert held_after is not None
        assert held_after.claimed_by_role_runtime_session_id == ""

        revised = DelegationWorkItem(
            work_item_id="revised-claim-item",
            run_id="claim-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Revised claim",
            kind="execute",
            projection_id="revised",
            phase=Phase.READY,
            metadata={"manager_mutation_revision": 2},
        )
        await store.save_delegation_work_item(revised)
        stale_revision = await store.claim_delegation_work_item_if_dispatchable(
            revised.work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session",
            seat_id="seat::executor",
            task_id="runtime-task",
            work_item_revision=1,
        )
        revised_after = await store.get_delegation_work_item(revised.work_item_id)
        assert stale_revision is None
        assert revised_after is not None
        assert revised_after.phase == Phase.READY
        assert revised_after.claimed_by_role_runtime_session_id == ""

        raced = DelegationWorkItem(
            work_item_id="post-commit-stop-item",
            run_id="claim-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Post-commit stop",
            kind="execute",
            projection_id="raced",
            phase=Phase.READY,
        )
        await store.save_delegation_work_item(raced)
        original_get = store.get_delegation_work_item
        stop_injected = False

        async def get_after_stop(work_item_id: str):
            nonlocal stop_injected
            if work_item_id == raced.work_item_id and not stop_injected:
                stop_injected = True
                await store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates={
                        "dispatch_hold": "company_runtime_suspended",
                        "claimed_by_role_session_id": "",
                        "claimed_task_id": "",
                    },
                    claimed_by_role_runtime_session_id="",
                    claimed_by_seat_id="",
                )
            return await original_get(work_item_id)

        store.get_delegation_work_item = get_after_stop  # type: ignore[method-assign]
        post_commit = await store.claim_delegation_work_item_if_dispatchable(
            raced.work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session",
            seat_id="seat::executor",
            task_id="runtime-task",
        )
        raced_after = await original_get(raced.work_item_id)
        assert post_commit is None
        assert raced_after is not None
        assert raced_after.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert raced_after.claimed_by_role_runtime_session_id == ""
    finally:
        await store.close()


@_async_test
async def test_shared_final_decider_without_parent_uses_runtime_identity_scope(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        anchor = _ui_anchor(status=TaskStatus.RUNNING)
        shared_final = Task(
            id="shared-final",
            title="Final decision",
            session_id="root-session",
            parent_session_id=None,
            project_id="project-a",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "execution",
                "shared_role_session": True,
                "company_runtime_root_session_id": "root-session",
                "company_work_item_plan": serialize_company_work_item_plan(
                    _runtime_plan()
                ),
            },
        )
        await store.save_task(anchor)
        await store.save_task(shared_final)
        engine = OPCEngine(project_id="project-a")
        engine.store = store

        snapshot = await engine._load_company_runtime_snapshot("root-session")
        reconciled = await engine._reconcile_interrupted_project_tasks()
        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )

        assert snapshot is not None
        assert [task.id for task in snapshot[1]] == [shared_final.id]
        assert reconciled == 2
        assert len(checkpoints) == 1
        refreshed_anchor = await store.get_task(anchor.id)
        assert refreshed_anchor is not None
        assert refreshed_anchor.status == TaskStatus.IDLE
    finally:
        await store.close()


@_async_test
async def test_successful_checkpoint_handoff_reopens_cancelled_ui_anchor(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        anchor = Task(
            id="ui-anchor",
            title="Company chat",
            session_id="root-session",
            project_id="project-a",
            status=TaskStatus.CANCELLED,
            metadata={},
        )
        runtime_task = _runtime_task(task_id="runtime-task", status=TaskStatus.BLOCKED)
        await store.save_task(anchor)
        await store.save_task(runtime_task)
        checkpoint = ExecutionCheckpoint(
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_runtime_interrupted",
            task_id=runtime_task.id,
            payload={"task_ids": [runtime_task.id], "parent_session_id": "root-session"},
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine.company_executor = SimpleNamespace(
            _notify_kanban_changed=AsyncMock(),
        )
        engine._prepare_company_runtime_tasks_for_resume = AsyncMock(
            return_value=[runtime_task]
        )
        engine._reset_company_executor_runtime_for_resume = AsyncMock()
        engine._clear_company_runtime_parent_stop_state = AsyncMock()

        handed_off = await engine._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=dict(checkpoint.payload),
            parent_session_id="root-session",
            tasks=[runtime_task],
        )

        still_cancelled = await store.get_task(anchor.id)
        resuming = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["resuming"],
        )
        assert handed_off is not None
        handed_off_tasks, driver_ownership = handed_off
        assert handed_off_tasks == [runtime_task]
        assert driver_ownership is not None
        engine.company_executor._notify_kanban_changed.assert_awaited_once_with()
        driver_ownership.release()
        assert still_cancelled is not None
        assert still_cancelled.status == TaskStatus.CANCELLED
        assert len(resuming) == 1

        await engine._complete_company_suspend_checkpoint_resume(
            checkpoint,
            parent_session_id="root-session",
        )

        reopened = await store.get_task(anchor.id)
        persisted_checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["resolved"],
        )
        assert reopened is not None and reopened.status == TaskStatus.IDLE
        assert len(persisted_checkpoints) == 1
    finally:
        await store.close()


@_async_test
async def test_failed_checkpoint_handoff_does_not_publish_running_snapshot(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        runtime_task = _runtime_task(
            task_id="runtime-task",
            status=TaskStatus.BLOCKED,
        )
        await store.save_task(runtime_task)
        checkpoint = ExecutionCheckpoint(
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_runtime_interrupted",
            task_id=runtime_task.id,
            payload={
                "task_ids": [runtime_task.id],
                "parent_session_id": "root-session",
            },
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine.company_executor = SimpleNamespace(
            _notify_kanban_changed=AsyncMock(),
        )
        engine._prepare_company_runtime_tasks_for_resume = AsyncMock(
            side_effect=RuntimeError("prepare failed"),
        )

        with pytest.raises(RuntimeError, match="prepare failed"):
            await engine._handoff_company_suspend_checkpoint(
                checkpoint,
                payload=dict(checkpoint.payload),
                parent_session_id="root-session",
                tasks=[runtime_task],
            )

        engine.company_executor._notify_kanban_changed.assert_not_awaited()
        assert not engine._active_task_run_registry.is_active(
            "project-a",
            runtime_task.id,
        )
        pending = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        assert len(pending) == 1
        assert pending[0].payload.get("resume_state") == "failed_before_handoff"
    finally:
        await store.close()


@_async_test
async def test_executor_failure_restores_pending_checkpoint_and_durable_holds(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        anchor = _ui_anchor(status=TaskStatus.CANCELLED)
        task = _runtime_task(task_id="failing-task", status=TaskStatus.RUNNING)
        work_item = DelegationWorkItem(
            work_item_id="failing-work-item",
            run_id="failing-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Failing work",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id="role-runtime",
            claimed_by_seat_id="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-session",
                "claimed_task_id": task.id,
            },
        )
        await store.save_task(anchor)
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        suspended = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="root-session",
            reason="user_stop",
        )
        assert suspended is not None
        checkpoint = (
            await store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        engine.company_executor = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("executor failed"))
        )

        with pytest.raises(RuntimeError, match="executor failed"):
            await engine._resume_company_suspend_checkpoint(checkpoint, "continue")

        pending = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        refreshed_task = await store.get_task(task.id)
        refreshed_item = await store.get_delegation_work_item(work_item.work_item_id)
        refreshed_anchor = await store.get_task(anchor.id)
        assert len(pending) == 1
        assert pending[0].payload["resume_state"] == "failed_during_execution"
        assert refreshed_task is not None
        assert refreshed_task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert refreshed_task.metadata["company_runtime_stop_state"] == "suspended"
        assert refreshed_item is not None
        assert refreshed_item.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert refreshed_item.claimed_by_role_runtime_session_id == ""
        assert refreshed_item.claimed_by_seat_id == ""
        assert refreshed_anchor is not None
        assert refreshed_anchor.status == TaskStatus.CANCELLED
    finally:
        await store.close()


@_async_test
async def test_concurrent_resume_requests_execute_checkpoint_once(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_task(_ui_anchor())
        task = _runtime_task(task_id="single-resume-task", status=TaskStatus.RUNNING)
        await store.save_task(task)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="root-session",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        execute_started = asyncio.Event()
        allow_execute = asyncio.Event()
        calls = 0

        async def execute(_plan, _tasks):
            nonlocal calls
            calls += 1
            execute_started.set()
            await allow_execute.wait()
            return "resumed once"

        engine.company_executor = SimpleNamespace(execute=execute)
        first = asyncio.create_task(
            engine._resume_company_suspend_checkpoint(checkpoint, "continue")
        )
        await execute_started.wait()
        persisted = (
            await store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["resuming"],
            )
        )[0]

        second = await engine._resume_company_suspend_checkpoint(
            persisted,
            "continue",
        )
        allow_execute.set()
        first_result = await first

        assert calls == 1
        assert "already being resumed" in second
        assert "resumed once" in first_result
        resolved = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )
        assert len(resolved) == 1
    finally:
        await store.close()


@_async_test
async def test_separate_controllers_atomically_claim_checkpoint_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "tasks.db"
    first_store = OPCStore(db_path)
    second_store = OPCStore(db_path)
    await first_store.initialize()
    try:
        await first_store.save_task(_ui_anchor())
        task = _runtime_task(
            task_id="cross-controller-resume-task",
            status=TaskStatus.RUNNING,
        )
        work_item = DelegationWorkItem(
            work_item_id="cross-controller-work-item",
            run_id="cross-controller-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Cross-controller work",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
        )
        await first_store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await first_store.save_task(task)
        await first_store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        setup_engine = OPCEngine(project_id="project-a")
        setup_engine.store = first_store
        await setup_engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="root-session",
            reason="user_stop",
        )
        await second_store.initialize()

        first_checkpoint = (
            await first_store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        second_checkpoint = (
            await second_store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        first_engine = OPCEngine(project_id="project-a")
        second_engine = OPCEngine(project_id="project-a")
        first_engine.store = first_store
        second_engine.store = second_store
        calls = 0

        async def execute(_plan, _tasks):
            nonlocal calls
            calls += 1
            return "claimed"

        first_engine.company_executor = SimpleNamespace(execute=execute)
        second_engine.company_executor = SimpleNamespace(execute=execute)

        # Hold both controllers after their independent pending reads so the
        # conditional UPDATE, rather than scheduling luck, decides ownership.
        both_read = asyncio.Event()
        read_count = 0

        async def resolve_anchor(**_kwargs):
            nonlocal read_count
            read_count += 1
            if read_count == 2:
                both_read.set()
            await both_read.wait()
            return "ui-anchor"

        first_engine._resolve_company_runtime_ui_anchor_task_id = resolve_anchor
        second_engine._resolve_company_runtime_ui_anchor_task_id = resolve_anchor

        results = await asyncio.gather(
            first_engine._resume_company_suspend_checkpoint(
                first_checkpoint,
                "continue",
            ),
            second_engine._resume_company_suspend_checkpoint(
                second_checkpoint,
                "continue",
            ),
        )

        assert calls == 1
        assert sum("already being resumed" in result for result in results) == 1
        resolved = await first_store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )
        assert len(resolved) == 1
    finally:
        await second_store.close()
        await first_store.close()


@_async_test
async def test_remote_live_controller_blocks_resume_before_any_durable_mutation(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "projects" / "project-a"
    project_dir.mkdir(parents=True)
    db_path = project_dir / "tasks.db"
    owner_store = OPCStore(db_path)
    remote_store = OPCStore(db_path)
    await owner_store.initialize()
    await remote_store.initialize(run_startup_maintenance=False)
    owner_lease = None
    try:
        await owner_store.save_delegation_run(
            DelegationRun(
                run_id="live-run",
                project_id="project-a",
                session_id="root-session",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        task = _runtime_task(
            task_id="live-owner-task",
            status=TaskStatus.BLOCKED,
            metadata={
                "delegation_run_id": "live-run",
                "dispatch_hold": "company_runtime_suspended",
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="live-owner-item",
            run_id="live-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Live owner work",
            kind="execute",
            projection_id="execution",
            phase=Phase.RUNNING,
            metadata={"dispatch_hold": "company_runtime_suspended"},
        )
        set_linked_work_item_id(task, work_item.work_item_id)
        anchor = _ui_anchor(status=TaskStatus.CANCELLED)
        await owner_store.save_task(anchor)
        await owner_store.save_delegation_work_item(work_item)
        await owner_store.save_task(task)
        assert await owner_store.link_work_item_runtime_task(
            work_item.work_item_id,
            task.id,
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="live-owner-checkpoint",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_runtime_suspended",
            status="pending",
            task_id=task.id,
            payload={
                "task_ids": [task.id],
                "parent_session_id": "root-session",
                "company_work_item_plan": serialize_company_work_item_plan(
                    _runtime_plan()
                ),
                "resume_state": "pending",
            },
        )
        await owner_store.save_execution_checkpoint(checkpoint)
        owner_lease = await owner_store.acquire_delegation_run_controller_lease(
            "live-run",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="healthy-owner",
            lease_seconds=60,
        )
        assert owner_lease.acquired

        remote_executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        remote_executor.store = remote_store
        remote_executor._active_controller_leases = {}
        remote_executor.execute = AsyncMock()
        remote_executor._notify_kanban_changed = AsyncMock()
        engine = OPCEngine(project_id="project-a")
        engine.store = remote_store
        engine.company_executor = remote_executor
        engine._resolve_company_runtime_ui_anchor_task_id = AsyncMock()
        engine._prepare_company_runtime_tasks_for_resume = AsyncMock()
        engine._reset_company_executor_runtime_for_resume = AsyncMock()
        engine._clear_company_runtime_parent_stop_state = AsyncMock()

        before_checkpoint = await remote_store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        before_task = await remote_store.get_task(task.id)
        before_item = await remote_store.get_delegation_work_item(work_item.work_item_id)
        before_anchor = await remote_store.get_task(anchor.id)

        result = await engine._resume_company_suspend_checkpoint(
            before_checkpoint[0],
            "continue",
        )

        after_checkpoint = await remote_store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        assert "live controller" in result
        assert after_checkpoint == before_checkpoint
        assert await remote_store.get_task(task.id) == before_task
        assert (
            await remote_store.get_delegation_work_item(work_item.work_item_id)
            == before_item
        )
        assert await remote_store.get_task(anchor.id) == before_anchor
        remote_executor.execute.assert_not_awaited()
        remote_executor._notify_kanban_changed.assert_not_awaited()
        engine._resolve_company_runtime_ui_anchor_task_id.assert_not_awaited()
        engine._prepare_company_runtime_tasks_for_resume.assert_not_awaited()
        engine._reset_company_executor_runtime_for_resume.assert_not_awaited()
        engine._clear_company_runtime_parent_stop_state.assert_not_awaited()
        assert remote_executor._active_controller_leases == {}
    finally:
        if owner_lease is not None and owner_lease.acquired:
            await owner_store.release_delegation_run_controller_lease(
                "live-run",
                project_id="project-a",
                root_session_id="root-session",
                owner_token="healthy-owner",
                generation=owner_lease.generation,
            )
        await remote_store.close()
        await owner_store.close()


@_async_test
async def test_resume_lease_loss_does_not_restore_checkpoint_as_stale_owner() -> None:
    task = _runtime_task(task_id="lease-lost-task", status=TaskStatus.BLOCKED)
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="lease-lost-checkpoint",
        project_id="project-a",
        session_id="root-session",
        checkpoint_type="company_runtime_suspended",
        status="pending",
        task_id=task.id,
        payload={"task_ids": [task.id]},
    )
    admission = object()
    release_driver = Mock()
    driver_ownership = SimpleNamespace(
        controller_admission=admission,
        bind=lambda: nullcontext(),
        release=release_driver,
    )
    engine = OPCEngine(project_id="project-a")
    engine.store = object()
    engine.company_executor = object()
    engine._load_company_suspend_checkpoint_runtime = AsyncMock(
        return_value=(
            dict(checkpoint.payload),
            "root-session",
            _runtime_plan(),
            [task],
        )
    )
    engine._handoff_company_suspend_checkpoint = AsyncMock(
        return_value=([task], driver_ownership),
    )
    engine._execute_with_company_controller_admission = AsyncMock(
        side_effect=CompanyRunControllerLeaseLost("generation was fenced"),
    )
    engine._complete_company_suspend_checkpoint_resume = AsyncMock()
    engine._restore_company_suspend_checkpoint_pending_after_cancellation = AsyncMock()
    engine._restore_company_suspend_checkpoint_pending = AsyncMock()
    engine._release_company_controller_admission = AsyncMock()

    with pytest.raises(CompanyRunControllerLeaseLost, match="generation was fenced"):
        await engine._resume_company_suspend_checkpoint(checkpoint, "continue")

    engine._restore_company_suspend_checkpoint_pending_after_cancellation.assert_not_awaited()
    engine._restore_company_suspend_checkpoint_pending.assert_not_awaited()
    engine._complete_company_suspend_checkpoint_resume.assert_not_awaited()
    engine._release_company_controller_admission.assert_awaited_once_with(admission)
    release_driver.assert_called_once_with()


@_async_test
async def test_runtime_without_ui_anchor_resumes_without_promoting_work_item(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        task = _runtime_task(task_id="headless-runtime-task", status=TaskStatus.RUNNING)
        await store.save_task(task)
        engine = OPCEngine(project_id="project-a")
        engine.store = store
        await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="root-session",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        engine.company_executor = SimpleNamespace(
            execute=AsyncMock(return_value="headless resumed")
        )

        result = await engine._resume_company_suspend_checkpoint(
            checkpoint,
            "continue",
        )

        resolved = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )
        refreshed = await store.get_task(task.id)
        assert "headless resumed" in result
        assert len(resolved) == 1
        assert resolved[0].payload.get("ui_anchor_task_id") == ""
        assert refreshed is not None
        assert refreshed.status != TaskStatus.IDLE
    finally:
        await store.close()


@_async_test
async def test_canonical_suspend_wins_real_executor_result_commit_race(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        task = _runtime_task(task_id="racing-task", status=TaskStatus.PENDING)
        task.assigned_to = "executor"
        task.metadata.update({
            "runtime_model": "multi_team_org",
            "work_item_role_id": "executor",
            "work_item_execution_strategy": "native",
        })
        work_item = DelegationWorkItem(
            work_item_id="racing-work-item",
            run_id="racing-run",
            cell_id="team::executor",
            role_id="executor",
            seat_id="seat::executor",
            title="Racing work",
            kind="execute",
            projection_id="execution",
            phase=Phase.READY,
        )
        await store.save_delegation_work_item(work_item)
        set_linked_work_item_id(task, work_item.work_item_id)
        await store.save_task(task)
        await store.link_work_item_runtime_task(work_item.work_item_id, task.id)

        run_started = asyncio.Event()
        allow_result = asyncio.Event()

        async def run_once(_task: Task):
            run_started.set()
            await allow_result.wait()
            return SimpleNamespace(
                status=TaskStatus.DONE,
                content="must not commit",
                artifacts={},
            )

        class OrgEngine:
            @staticmethod
            def get_role_for_work_item(_role_id, _tags):
                return SimpleNamespace(
                    role_id="executor",
                    preferred_external_agent=None,
                    default_agent=None,
                    tools=[],
                )

        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine._run_task_once = run_once
        executor = CompanyWorkItemExecutor(
            org_engine=OrgEngine(),
            communication=None,
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=engine._execute_task,
            save_task=store.save_task,
            store=store,
        )

        execution = asyncio.create_task(
            executor._run_work_item(task, {"execution": task})
        )
        await run_started.wait()
        suspended = await engine.suspend_company_runtime(
            origin_task_id=task.id,
            session_id="root-session",
            reason="service_shutdown",
            checkpoint_type="company_runtime_interrupted",
        )
        allow_result.set()
        with pytest.raises(asyncio.CancelledError):
            await execution

        checkpoints = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        refreshed_task = await store.get_task(task.id)
        refreshed_item = await store.get_delegation_work_item(work_item.work_item_id)
        assert suspended is not None
        assert len(checkpoints) == 1
        assert refreshed_task is not None
        assert refreshed_task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert refreshed_task.metadata["company_runtime_suspend_checkpoint_type"] == "company_runtime_interrupted"
        assert refreshed_task.result is None
        assert refreshed_item is not None
        assert refreshed_item.phase not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
        assert refreshed_item.metadata["dispatch_hold"] == "company_runtime_suspended"
    finally:
        await store.close()


@pytest.mark.parametrize("interrupt_kind", ["stop", "shutdown"])
@_async_test
async def test_stop_or_shutdown_cannot_release_remaining_final_decider_work(
    tmp_path: Path,
    interrupt_kind: str,
) -> None:
    store = OPCStore(tmp_path / f"tasks-{interrupt_kind}.db")
    await store.initialize()
    registry = ActiveTaskRunRegistry()
    token = ""
    try:
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="final",
                    turn_type="deliver",
                    title="Final decision",
                    summary="Arbitrate the follow-up.",
                    role_id="ceo",
                ),
                WorkItemProjectionSpec(
                    projection_id="worker",
                    turn_type="execute",
                    title="Worker",
                    summary="Continue implementation.",
                    role_id="engineer",
                ),
            ],
            metadata={
                "execution_model": "multi_team_org",
                "final_decider_role_id": "ceo",
            },
        )
        serialized_plan = serialize_company_work_item_plan(plan)
        await store.save_task(_ui_anchor())
        final_task = Task(
            id="final-task",
            title="Final decision",
            session_id="final-session",
            parent_session_id="root-session",
            project_id="project-a",
            assigned_to="ceo",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "company_work_item_plan": serialized_plan,
            },
        )
        worker_task = Task(
            id="worker-task",
            title="Worker",
            session_id="worker-session",
            parent_session_id="root-session",
            project_id="project-a",
            assigned_to="engineer",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "worker",
                "company_work_item_plan": serialized_plan,
            },
        )
        for task, work_item_id, projection, role in (
            (final_task, "final-item", "final", "ceo"),
            (worker_task, "worker-item", "worker", "engineer"),
        ):
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id=work_item_id,
                    run_id="final-run",
                    cell_id=f"team::{role}",
                    role_id=role,
                    seat_id=f"seat::{role}",
                    title=task.title,
                    kind="deliver" if role == "ceo" else "execute",
                    projection_id=projection,
                    phase=Phase.RUNNING,
                )
            )
            set_linked_work_item_id(task, work_item_id)
            await store.save_task(task)
            await store.link_work_item_runtime_task(work_item_id, task.id)

        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=registry,
            owns_active_task_run_registry=True,
        )
        engine.store = store
        engine.company_executor = SimpleNamespace(execute=AsyncMock())
        await engine.suspend_company_runtime(
            origin_task_id=worker_task.id,
            session_id="root-session",
            reason="user_stop",
        )
        checkpoint = (
            await store.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
                statuses=["pending"],
            )
        )[0]
        loaded = await engine._load_company_suspend_checkpoint_runtime(checkpoint)
        assert loaded is not None
        payload, parent_session_id, loaded_plan, tasks = loaded
        handoff = await engine._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=payload,
            parent_session_id=parent_session_id,
            tasks=tasks,
            resume_task_ids={final_task.id},
        )
        assert handoff is not None
        resumed_tasks, initial_driver_ownership = handoff
        if initial_driver_ownership is not None:
            initial_driver_ownership.release()

        progress_checked = asyncio.Event()
        allow_remaining_handoff = asyncio.Event()

        async def final_progressed(_task_id: str) -> bool:
            progress_checked.set()
            await allow_remaining_handoff.wait()
            return True

        engine._company_followup_target_progressed = final_progressed
        continuation = asyncio.create_task(
            engine._resume_remaining_company_runtime_after_final_decider(
                checkpoint=checkpoint,
                plan=loaded_plan,
                tasks=resumed_tasks,
                payload=payload,
                parent_session_id=parent_session_id,
                final_decider_task_id=final_task.id,
            )
        )
        await progress_checked.wait()
        if interrupt_kind == "stop":
            await engine.suspend_company_runtime(
                origin_task_id=final_task.id,
                session_id="root-session",
                reason="user_stop",
            )
        else:
            token = registry.register("project-a", final_task.id)
            await engine.prepare_active_company_runtimes_for_shutdown()
        allow_remaining_handoff.set()

        continuation_owned, continuation_result = await continuation
        assert not continuation_owned
        assert continuation_result is None
        engine.company_executor.execute.assert_not_awaited()
        pending = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        assert len(pending) == 1
        for task_id, work_item_id in (
            (final_task.id, "final-item"),
            (worker_task.id, "worker-item"),
        ):
            refreshed_task = await store.get_task(task_id)
            refreshed_item = await store.get_delegation_work_item(work_item_id)
            assert refreshed_task is not None
            assert refreshed_item is not None
            assert refreshed_task.metadata["dispatch_hold"] == "company_runtime_suspended"
            assert refreshed_item.metadata["dispatch_hold"] == "company_runtime_suspended"
    finally:
        if token:
            registry.unregister("project-a", final_task.id, token)
        await store.close()


@_async_test
async def test_final_decider_without_arbitration_keeps_multi_item_checkpoint_pending(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="final",
                    turn_type="deliver",
                    title="Final decision",
                    summary="Arbitrate the follow-up.",
                    role_id="ceo",
                ),
                WorkItemProjectionSpec(
                    projection_id="worker",
                    turn_type="execute",
                    title="Worker",
                    summary="Continue implementation.",
                    role_id="engineer",
                ),
            ],
            metadata={
                "execution_model": "multi_team_org",
                "final_decider_role_id": "ceo",
            },
        )
        serialized_plan = serialize_company_work_item_plan(plan)
        await store.save_task(_ui_anchor())
        final_task = Task(
            id="final-task-no-arbitration",
            title="Final decision",
            session_id="final-session-no-arbitration",
            parent_session_id="root-session",
            project_id="project-a",
            assigned_to="ceo",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "company_work_item_plan": serialized_plan,
            },
        )
        worker_task = Task(
            id="worker-task-still-held",
            title="Worker",
            session_id="worker-session-still-held",
            parent_session_id="root-session",
            project_id="project-a",
            assigned_to="engineer",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "worker",
                "company_work_item_plan": serialized_plan,
            },
        )
        for task, work_item_id, projection, role in (
            (final_task, "final-item-no-arbitration", "final", "ceo"),
            (worker_task, "worker-item-still-held", "worker", "engineer"),
        ):
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id=work_item_id,
                    run_id="no-arbitration-run",
                    cell_id=f"team::{role}",
                    role_id=role,
                    seat_id=f"seat::{role}",
                    title=task.title,
                    kind="deliver" if role == "ceo" else "execute",
                    projection_id=projection,
                    phase=Phase.RUNNING,
                )
            )
            set_linked_work_item_id(task, work_item_id)
            await store.save_task(task)
            await store.link_work_item_runtime_task(work_item_id, task.id)

        engine = OPCEngine(project_id="project-a")
        engine.store = store
        engine.company_executor = SimpleNamespace(execute=AsyncMock())
        engine._resume_company_runtime_via_final_decider = AsyncMock(
            return_value="Final-decider turn returned without arbitration."
        )
        suspended = await engine.suspend_company_runtime(
            origin_task_id=worker_task.id,
            session_id="root-session",
            reason="user_stop",
        )
        assert suspended is not None
        active = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        assert len(active) == 1
        checkpoint = active[0]

        response = await engine._resume_company_suspend_checkpoint_via_final_decider(
            checkpoint,
            "Continue and arbitrate the remaining work.",
        )

        pending = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )
        resolved = await store.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["resolved"],
        )
        assert len(pending) == 1
        assert pending[0].checkpoint_id == checkpoint.checkpoint_id
        assert pending[0].payload["resume_state"] == "awaiting_final_decider_action"
        assert resolved == []
        assert "durable arbitration action" in response
        assert "Resumed remaining company runtime" not in response
        engine.company_executor.execute.assert_not_awaited()
        for task_id, work_item_id in (
            (final_task.id, "final-item-no-arbitration"),
            (worker_task.id, "worker-item-still-held"),
        ):
            refreshed_task = await store.get_task(task_id)
            refreshed_item = await store.get_delegation_work_item(work_item_id)
            assert refreshed_task is not None
            assert refreshed_item is not None
            assert refreshed_task.metadata["dispatch_hold"] == "company_runtime_suspended"
            assert refreshed_item.metadata["dispatch_hold"] == "company_runtime_suspended"
    finally:
        await store.close()


@_async_test
async def test_startup_does_not_replay_resolved_checkpoint_into_ui_anchor(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        anchor = Task(
            id="crash-gap-anchor",
            title="Company chat",
            session_id="crash-gap-session",
            project_id="project-a",
            status=TaskStatus.CANCELLED,
            metadata={},
        )
        await store.save_task(anchor)
        checkpoint = ExecutionCheckpoint(
            project_id="project-a",
            session_id="crash-gap-session",
            checkpoint_type="company_runtime_interrupted",
            task_id="runtime-task",
            status="resolved",
            payload={
                "resume_state": "handoff_complete",
                "ui_anchor_task_id": anchor.id,
            },
        )
        await store.save_execution_checkpoint(checkpoint)
        engine = OPCEngine(project_id="project-a")
        engine.store = store

        assert await engine._reconcile_interrupted_project_tasks() == 0
        still_cancelled = await store.get_task(anchor.id)
        assert still_cancelled is not None and still_cancelled.status == TaskStatus.CANCELLED
    finally:
        await store.close()
