from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opc.core.active_task_runs import ActiveTaskRunRegistry
from opc.core.company_controller import (
    CompanyControllerAttemptContext,
    CompanyRunControllerBusy,
    CompanyRunControllerLeaseLost,
)
from opc.core.config import OPCConfig
from opc.core.interaction_protocol import PreparedOwnerInteractionPublication
from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    MeetingRoom,
    Phase,
    RoleRuntimeSession,
    SeatState,
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
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
    serialize_company_work_item_plan,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_transition import (
    transition_work_item_from_task,
)
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import ToolRegistry


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


async def _seed_owned_scope(
    tmp_path: Path,
) -> tuple[OPCStore, OPCStore, OPCEngine, ActiveTaskRunRegistry, str, int]:
    db_path = tmp_path / "tasks.db"
    store1 = OPCStore(db_path)
    store2 = OPCStore(db_path)
    await store1.initialize()
    await store2.initialize(run_startup_maintenance=False)

    await store1.save_delegation_run(
        DelegationRun(
            run_id="run-1",
            project_id="project-a",
            session_id="root-session",
            execution_model="multi_team_org",
            status="running",
            lifecycle_status="active",
        )
    )
    item = DelegationWorkItem(
        work_item_id="work-item-1",
        run_id="run-1",
        cell_id="team::executor",
        role_id="executor",
        seat_id="seat::executor",
        role_runtime_session_id="role-session-1",
        title="Active work",
        kind="execute",
        projection_id="execution",
        phase=Phase.RUNNING,
        claimed_by_role_runtime_session_id="role-session-1",
        claimed_by_seat_id="seat::executor",
        metadata={
            "claimed_by_role_session_id": "role-session-1",
            "claimed_task_id": "task-1",
            "attempt_seq": 1,
        },
    )
    await store1.save_delegation_work_item(item)
    task = Task(
        id="task-1",
        title="Active work",
        session_id="role-session-1",
        parent_session_id="root-session",
        project_id="project-a",
        assigned_to="executor",
        status=TaskStatus.RUNNING,
        execution_lock=True,
        execution_locked_at=datetime.now(),
        metadata={
            "work_item_projection_id": "execution",
            "work_item_runtime": True,
            "execution_model": "multi_team_org",
            "delegation_run_id": "run-1",
            "delegation_role_session_id": "role-session-1",
            "company_work_item_plan": serialize_company_work_item_plan(
                _runtime_plan()
            ),
        },
    )
    set_linked_work_item_id(task, item.work_item_id)
    await store1.save_task(task)
    assert await store1.link_work_item_runtime_task(item.work_item_id, task.id)
    await store1.save_delegation_role_session(
        RoleRuntimeSession(
            role_session_id="role-session-1",
            run_id="run-1",
            project_id="project-a",
            role_id="executor",
            seat_id="seat::executor",
            focused_work_item_id=item.work_item_id,
            status="running",
        )
    )
    await store1.save_external_session(
        ExternalSession(
            agent_type="native",
            project_id="project-a",
            session_id="native-session-1",
            opc_session_id="role-session-1",
            task_id=task.id,
            status="working",
        )
    )

    lease = await store1.acquire_delegation_run_controller_lease(
        "run-1",
        project_id="project-a",
        root_session_id="root-session",
        owner_token="owner-a",
        lease_seconds=60,
    )
    assert lease.acquired
    generation = lease.generation
    task.metadata.update(
        {
            "company_run_controller_owner_token": "owner-a",
            "company_run_controller_lease_generation": generation,
            "claimed_work_item_attempt_seq": 1,
        }
    )
    await store1.save_task(task)
    await store1.update_delegation_work_item(
        item.work_item_id,
        metadata_updates={
            "company_run_controller_owner_token": "owner-a",
            "company_run_controller_lease_generation": generation,
        },
    )

    registry = ActiveTaskRunRegistry()
    registry.register("project-a", task.id)
    engine = OPCEngine(
        project_id="project-a",
        active_task_run_registry=registry,
        owns_active_task_run_registry=True,
    )
    engine.store = store1
    credential = {
        "run_id": "run-1",
        "project_id": "project-a",
        "root_session_id": "root-session",
        "owner_token": "owner-a",
        "generation": generation,
    }
    engine.company_executor = SimpleNamespace(
        controller_lease_credential=lambda run_id: (
            dict(credential) if run_id == "run-1" else None
        )
    )
    return store1, store2, engine, registry, "owner-a", generation


async def _expire_lease(
    store: OPCStore,
    *,
    owner_token: str,
    generation: int,
) -> None:
    renewed = await store.renew_delegation_run_controller_lease(
        "run-1",
        project_id="project-a",
        root_session_id="root-session",
        owner_token=owner_token,
        generation=generation,
        lease_seconds=1,
        heartbeat_at=datetime.now() - timedelta(seconds=2),
    )
    assert renewed


async def _commit_final_owner_handoff(
    store: OPCStore,
    *,
    checkpoint_id: str = "delivery-final-1",
) -> ExecutionCheckpoint:
    """Commit the same run/card boundary used by final delivery in production."""

    task = await store.get_task("task-1")
    assert task is not None
    context = CompanyControllerAttemptContext.from_task(
        task,
        work_item_id="work-item-1",
    )
    assert context.complete
    item = await store.get_delegation_work_item("work-item-1")
    assert item is not None
    task_preimage = company_controller_task_preimage_hash(task)
    task.status = TaskStatus.AWAITING_HUMAN
    interaction_key = f"delivery:{checkpoint_id}"
    supersession_key = "owner:project-a:root-session"
    publication = PreparedOwnerInteractionPublication(
        checkpoint=ExecutionCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id="project-a",
            session_id=task.session_id,
            checkpoint_type="company_delivery_feedback",
            task_id=task.id,
            payload={
                "waiting_task_id": task.id,
                "task_ids": [task.id],
                "feedback_scope": "final",
                "interaction": {
                    "kind": "company_delivery_feedback",
                    "domain_key": interaction_key,
                    "supersession_key": supersession_key,
                    "supersession_order": [1, 0],
                    "ownership": {
                        "waiting_task_id": task.id,
                        "waiting_session_id": task.session_id,
                        "root_session_id": "root-session",
                        "ui_anchor_session_id": "root-session",
                    },
                },
            },
        ),
        interaction_key=interaction_key,
        supersession_key=supersession_key,
        supersession_order=(1, 0),
    )
    result = await store.execute_company_controller_authoritative_command(
        context,
        operation="test_publish_final_delivery_owner_handoff",
        mutations=(
            CompanyControllerWorkItemMutation(
                work_item_id=item.work_item_id,
                expected_phases=(Phase.RUNNING,),
                expected_updated_at=item.updated_at,
                phase=Phase.AWAITING_HUMAN,
                clear_claim=True,
                transition_reason="test_final_delivery_owner_handoff",
                attempt_outcome="awaiting_owner",
            ),
        ),
        task_snapshot=task,
        task_preimage_hashes={
            task.id: task_preimage
        },
        run_mutation=CompanyControllerRunLifecycleMutation(
            expected_statuses=("running",),
            expected_lifecycle_statuses=("active",),
            status="running",
            lifecycle_status="awaiting_owner",
            metadata_updates={"awaiting_owner_review": True},
        ),
        owner_publication=publication,
    )
    assert result.applied
    assert result.owner_checkpoint is not None
    return result.owner_checkpoint


@_async_test
async def test_shutdown_skips_final_authoritative_run_still_in_active_registry(
    tmp_path: Path,
) -> None:
    store1, store2, engine, registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        final_checkpoint = await _commit_final_owner_handoff(store1)
        assert registry.is_active("project-a", "task-1")

        assert await engine.prepare_active_company_runtimes_for_shutdown() == []

        run = await store2.get_delegation_run("run-1")
        task = await store2.get_task("task-1")
        item = await store2.get_delegation_work_item("work-item-1")
        final_checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            checkpoint_types=["company_delivery_feedback"],
        )
        interrupted = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
        )

        assert run is not None and run.lifecycle_status == "awaiting_owner"
        assert [row.checkpoint_id for row in final_checkpoints] == [
            final_checkpoint.checkpoint_id
        ]
        assert interrupted == []
        assert task is not None and "dispatch_hold" not in task.metadata
        assert item is not None and "dispatch_hold" not in item.metadata
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_final_commit_winning_sqlite_barrier_makes_stale_suspend_zero_write(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    stale_suspend_ready = asyncio.Event()
    final_committed = asyncio.Event()
    try:
        snapshot = await engine._load_company_runtime_snapshot("root-session")
        assert snapshot is not None
        plan, tasks = snapshot
        stale_candidate = await engine._build_company_runtime_suspend_checkpoint(
            checkpoint_type="company_runtime_interrupted",
            reason="service_shutdown",
            parent_session_id="root-session",
            origin_task_id="task-1",
            plan=plan,
            tasks=tasks,
        )

        async def stale_suspend():
            stale_suspend_ready.set()
            await final_committed.wait()
            return await store2.suspend_company_runtime_scope_for_controller(
                run_id="run-1",
                project_id="project-a",
                root_session_id="root-session",
                owner_token=owner,
                generation=generation,
                checkpoint=stale_candidate,
                checkpoint_types=["company_runtime_interrupted"],
                task_ids=["task-1"],
            )

        suspend = asyncio.create_task(stale_suspend())
        await asyncio.wait_for(stale_suspend_ready.wait(), timeout=2)
        final_checkpoint = await _commit_final_owner_handoff(store1)
        task_after_final = await store1.get_task("task-1")
        item_after_final = await store1.get_delegation_work_item("work-item-1")
        final_committed.set()
        receipt = await suspend
        task = await store2.get_task("task-1")
        item = await store2.get_delegation_work_item("work-item-1")
        final_checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            checkpoint_types=["company_delivery_feedback"],
        )
        interrupted = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
        )

        assert receipt.outcome == "already_awaiting_owner"
        assert receipt.skipped and not receipt.applied
        assert receipt.checkpoint is None
        assert receipt.affected_task_ids == ()
        assert [row.checkpoint_id for row in final_checkpoints] == [
            final_checkpoint.checkpoint_id
        ]
        assert interrupted == []
        assert task == task_after_final
        assert item == item_after_final
        assert task is not None and "dispatch_hold" not in task.metadata
        assert item is not None and "dispatch_hold" not in item.metadata
    finally:
        final_committed.set()
        await store2.close()
        await store1.close()


@_async_test
async def test_running_run_still_suspends_under_exact_controller_lease(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        snapshot = await engine._load_company_runtime_snapshot("root-session")
        assert snapshot is not None
        plan, tasks = snapshot
        candidate = await engine._build_company_runtime_suspend_checkpoint(
            checkpoint_type="company_runtime_interrupted",
            reason="service_shutdown",
            parent_session_id="root-session",
            origin_task_id="task-1",
            plan=plan,
            tasks=tasks,
        )
        receipt = await store1.suspend_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token=owner,
            generation=generation,
            checkpoint=candidate,
            checkpoint_types=["company_runtime_interrupted"],
            task_ids=["task-1"],
        )

        task = await store2.get_task("task-1")
        item = await store2.get_delegation_work_item("work-item-1")
        assert receipt.applied and not receipt.skipped
        assert receipt.checkpoint is not None
        assert receipt.checkpoint_created
        assert receipt.affected_task_ids == ("task-1",)
        assert task is not None
        assert task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item is not None
        assert item.metadata["dispatch_hold"] == "company_runtime_suspended"
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_root_shutdown_suspends_live_custom_child_before_store_close(
    tmp_path: Path,
) -> None:
    store1, store2, root, registry, _owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    child = OPCEngine(
        project_id="project-a",
        store=store1,
        owns_store=False,
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )
    credential = {
        "run_id": "run-1",
        "project_id": "project-a",
        "root_session_id": "root-session",
        "owner_token": "owner-a",
        "generation": generation,
    }
    child.company_executor = SimpleNamespace(
        controller_lease_credential=lambda run_id: (
            dict(credential) if run_id == "run-1" else None
        )
    )
    # The Store-owning root deliberately has no authority to impersonate its
    # custom child.  Only the child credential may commit the shutdown hold.
    root.company_executor = SimpleNamespace(
        controller_lease_credential=lambda _run_id: None
    )
    ingress_started = asyncio.Event()
    cancellation_observation: dict[str, object] = {}
    close_events: list[str] = []
    original_close = store1.close

    async def observed_close() -> None:
        close_events.append("store_close")
        await original_close()

    store1.close = observed_close  # type: ignore[method-assign]

    async def live_custom_ingress() -> None:
        current = asyncio.current_task()
        assert current is not None
        ingress = root._register_runtime_child(child)
        token = registry.register(
            "project-a",
            "task-1",
            owner_task=current,
        )
        ingress_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_observation["store_ready"] = store1.is_ready
            checkpoints = await store1.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["pending"],
            )
            persisted_task = await store1.get_task("task-1")
            persisted_item = await store1.get_delegation_work_item("work-item-1")
            cancellation_observation["checkpoint_count"] = len(checkpoints)
            cancellation_observation["task_hold"] = str(
                ((persisted_task.metadata if persisted_task else {}) or {}).get(
                    "dispatch_hold",
                    "",
                )
            )
            cancellation_observation["work_item_hold"] = str(
                ((persisted_item.metadata if persisted_item else {}) or {}).get(
                    "dispatch_hold",
                    "",
                )
            )
            close_events.append("ingress_joined")
            raise
        finally:
            registry.unregister("project-a", "task-1", token)
            await child.shutdown()
            root._unregister_runtime_child(child, ingress)

    live = asyncio.create_task(live_custom_ingress())
    await ingress_started.wait()
    try:
        with pytest.raises(AssertionError, match="harness abort"):
            try:
                raise AssertionError("harness abort")
            finally:
                await root.shutdown()
        await root.shutdown()

        assert live.cancelled()
        assert cancellation_observation == {
            "store_ready": True,
            "checkpoint_count": 1,
            "task_hold": "company_runtime_suspended",
            "work_item_hold": "company_runtime_suspended",
        }
        assert close_events == ["ingress_joined", "store_close"]
        assert not store1.is_ready
        assert child._shutdown_complete
        assert child not in root._runtime_child_engines
        assert registry.active_owner_tasks() == set()

        persisted_task = await store2.get_task("task-1")
        persisted_item = await store2.get_delegation_work_item("work-item-1")
        checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        assert persisted_task is not None and persisted_item is not None
        assert persisted_task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert persisted_item.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert len(checkpoints) == 1
    finally:
        if not live.done():
            live.cancel()
            await asyncio.gather(live, return_exceptions=True)
        await store2.close()
        if store1.is_ready:
            await original_close()


@_async_test
async def test_shutdown_takeover_after_precheck_gives_stale_owner_zero_writes(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = await _seed_owned_scope(
        tmp_path
    )
    try:
        task_before = await store2.get_task("task-1")
        item_before = await store2.get_delegation_work_item("work-item-1")
        assert task_before is not None and item_before is not None

        entered_command = asyncio.Event()
        continue_command = asyncio.Event()
        original = store1.suspend_company_runtime_scope_for_controller

        async def gated_command(*args, **kwargs):
            entered_command.set()
            await continue_command.wait()
            return await original(*args, **kwargs)

        store1.suspend_company_runtime_scope_for_controller = gated_command  # type: ignore[method-assign]
        shutdown = asyncio.create_task(
            engine.prepare_active_company_runtimes_for_shutdown()
        )
        await asyncio.wait_for(entered_command.wait(), timeout=2)
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired and takeover.generation > generation
        continue_command.set()

        assert await shutdown == []
        task_after = await store2.get_task("task-1")
        item_after = await store2.get_delegation_work_item("work-item-1")
        checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=[
                "company_runtime_interrupted",
                "company_runtime_suspended",
            ],
        )
        assert task_after == task_before
        assert item_after == item_before
        assert checkpoints == []
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_owned_user_stop_uses_atomic_controller_suspend(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        first = await engine.suspend_company_runtime(
            origin_task_id="task-1",
            session_id="root-session",
            reason="user_stop",
            checkpoint_type="company_runtime_suspended",
            stop_intent_id="stop-intent-1",
        )
        second = await engine.suspend_company_runtime(
            origin_task_id="task-1",
            session_id="root-session",
            reason="user_stop",
            checkpoint_type="company_runtime_suspended",
            stop_intent_id="stop-intent-1",
        )
        task = await store2.get_task("task-1")
        item = await store2.get_delegation_work_item("work-item-1")
        checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_suspended"],
            statuses=["pending"],
        )

        assert first is not None and second is not None
        assert first["checkpoint_id"] == second["checkpoint_id"]
        assert first["task_ids"] == ["task-1"]
        assert second["task_ids"] == []
        assert not first["idempotent"] and second["idempotent"]
        assert len(checkpoints) == 1
        assert checkpoints[0].payload["stop_intent_id"] == "stop-intent-1"
        assert task is not None and item is not None
        assert task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item.claimed_by_role_runtime_session_id == ""
        assert item.claimed_by_seat_id == ""
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_user_stop_takeover_after_local_credential_gives_old_owner_zero_writes(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        task_before = await store2.get_task("task-1")
        item_before = await store2.get_delegation_work_item("work-item-1")
        entered_command = asyncio.Event()
        continue_command = asyncio.Event()
        original = store1.suspend_company_runtime_scope_for_controller

        async def gated_command(*args, **kwargs):
            entered_command.set()
            await continue_command.wait()
            return await original(*args, **kwargs)

        store1.suspend_company_runtime_scope_for_controller = gated_command  # type: ignore[method-assign]
        stopping = asyncio.create_task(
            engine.suspend_company_runtime(
                origin_task_id="task-1",
                session_id="root-session",
                reason="user_stop",
                checkpoint_type="company_runtime_suspended",
                stop_intent_id="stop-intent-race",
            )
        )
        await asyncio.wait_for(entered_command.wait(), timeout=2)
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired and takeover.generation > generation
        continue_command.set()

        with pytest.raises(CompanyRunControllerLeaseLost):
            await stopping
        assert await store2.get_task("task-1") == task_before
        assert await store2.get_delegation_work_item("work-item-1") == item_before
        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
            )
            == []
        )
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_user_stop_remote_owner_is_busy_and_non_mutating(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        task_before = await store2.get_task("task-1")
        item_before = await store2.get_delegation_work_item("work-item-1")
        engine.company_executor = SimpleNamespace(
            controller_lease_credential=lambda _run_id: None
        )

        with pytest.raises(CompanyRunControllerBusy):
            await engine.suspend_company_runtime(
                origin_task_id="task-1",
                session_id="root-session",
                reason="user_stop",
                checkpoint_type="company_runtime_suspended",
            )
        assert await store2.get_task("task-1") == task_before
        assert await store2.get_delegation_work_item("work-item-1") == item_before
        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_suspended"],
            )
            == []
        )
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_owned_shutdown_atomically_holds_scope_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        first = await engine.prepare_active_company_runtimes_for_shutdown()
        task_after_first = await store2.get_task("task-1")
        item_after_first = await store2.get_delegation_work_item("work-item-1")
        role_after_first = await store2.get_delegation_role_session("role-session-1")
        external_after_first = await store2.get_latest_external_session_for_task(
            "project-a", "task-1"
        )
        second = await engine.prepare_active_company_runtimes_for_shutdown()
        task_after_second = await store2.get_task("task-1")
        item_after_second = await store2.get_delegation_work_item("work-item-1")
        checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=[
                "company_runtime_interrupted",
                "company_runtime_suspended",
            ],
            statuses=["pending", "resuming"],
        )

        assert len(first) == 1 and len(second) == 1
        assert first[0]["checkpoint_id"] == second[0]["checkpoint_id"]
        assert first[0]["checkpoint_type"] == "company_runtime_interrupted"
        assert first[0]["task_ids"] == ["task-1"]
        assert not first[0]["idempotent"]
        assert second[0]["task_ids"] == []
        assert second[0]["idempotent"]
        assert len(checkpoints) == 1 and checkpoints[0].status == "pending"

        assert task_after_first is not None and task_after_second is not None
        assert task_after_first == task_after_second
        assert not task_after_first.execution_lock
        assert task_after_first.execution_locked_at is None
        assert task_after_first.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert (
            task_after_first.metadata["company_runtime_suspend_checkpoint_type"]
            == "company_runtime_interrupted"
        )
        assert item_after_first is not None and item_after_second is not None
        assert item_after_first == item_after_second
        assert item_after_first.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert item_after_first.claimed_by_role_runtime_session_id == ""
        assert item_after_first.claimed_by_seat_id == ""
        assert role_after_first is not None
        assert role_after_first.status == "idle"
        assert role_after_first.focused_work_item_id == ""
        assert external_after_first is not None
        assert external_after_first.status == "suspended"
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_startup_terminalizes_expired_executing_staffing_without_blocking_resume(
    tmp_path: Path,
) -> None:
    """A stopped staffing consumer and its suspended run recover independently."""

    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    recovery_coordinator: InteractionCoordinator | None = None
    try:
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="staffing-1",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_staffing_selection",
            task_id="task-1",
            payload={
                "delegation_run_id": "run-1",
                "interaction": {
                    "kind": "company_staffing_selection",
                    "domain_key": "staffing:run-1",
                    "ownership": {
                        "waiting_task_id": "task-1",
                        "waiting_session_id": "root-session",
                        "root_session_id": "root-session",
                        "ui_anchor_session_id": "root-session",
                    },
                    "execution_scope": {"company_profile": "corporate"},
                },
            },
        )
        persisted, created = await store1.create_owner_interaction_checkpoint(
            checkpoint,
            interaction_key="staffing:run-1",
        )
        assert created
        accepted = await store1.accept_execution_checkpoint_decision(
            persisted.checkpoint_id,
            project_id="project-a",
            checkpoint_type=persisted.checkpoint_type,
            request_id="staffing-decision-1",
            decision_hash="staffing-decision-hash-1",
            decision={"staffing_action": "manual_approve"},
        )
        assert accepted.acknowledged

        expired_at = datetime.now() - timedelta(seconds=2)
        claim = await store1.claim_answered_execution_checkpoint(
            persisted.checkpoint_id,
            project_id="project-a",
            checkpoint_type=persisted.checkpoint_type,
            consumer_id="stopped-engine",
            claim_id="stopped-staffing-claim",
            lease_seconds=1,
            claimed_at=expired_at,
        )
        assert claim.acquired
        started = await store1.begin_execution_checkpoint_effect(
            persisted.checkpoint_id,
            project_id="project-a",
            checkpoint_type=persisted.checkpoint_type,
            consumer_id="stopped-engine",
            claim_id="stopped-staffing-claim",
            started_at=expired_at,
        )
        assert started.acquired

        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        assert len(prepared) == 1
        assert prepared[0]["checkpoint_type"] == "company_runtime_interrupted"
        interrupted_id = prepared[0]["checkpoint_id"]
        released = await store1.release_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token=owner,
            generation=generation,
        )
        assert released
        suspended_run = await store2.get_delegation_run("run-1")
        assert suspended_run is not None
        assert suspended_run.controller_owner_token == ""

        recovery_coordinator = InteractionCoordinator(
            store=store2,
            project_id="project-a",
        )
        recovery = OPCEngine(
            project_id="project-a",
            store=store2,
            owns_store=False,
            interaction_coordinator=recovery_coordinator,
        )
        replay = AsyncMock(side_effect=AssertionError("staffing effect replayed"))
        recovery._dispatch_interaction_decision = replay  # type: ignore[method-assign]

        await recovery._recover_interaction_consumers()
        consumers = list(recovery._interaction_consumer_tasks)
        assert len(consumers) == 1
        await asyncio.gather(*consumers)
        assert recovery._interaction_consumer_failures == []
        replay.assert_not_awaited()

        terminal_staffing = await store2.get_execution_checkpoint(
            persisted.checkpoint_id,
            project_id="project-a",
            checkpoint_type=persisted.checkpoint_type,
        )
        assert terminal_staffing is not None
        assert terminal_staffing.status == "outcome_unknown"
        terminal_interaction = terminal_staffing.payload["interaction"]
        assert terminal_interaction["execution"]["state"] == "outcome_unknown"
        assert (
            terminal_interaction["completion"]["final_status"]
            == "outcome_unknown"
        )

        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="restart-owner",
            lease_seconds=60,
        )
        assert takeover.acquired
        resumed = await store2.resume_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="restart-owner",
            generation=takeover.generation,
            checkpoint_id=interrupted_id,
            checkpoint_types=["company_runtime_interrupted"],
            expected_checkpoint_statuses=["pending"],
            task_ids=prepared[0]["task_ids"],
        )
        assert resumed.applied
        assert resumed.checkpoint is not None
        assert resumed.checkpoint.status == "resuming"
        assert resumed.affected_task_ids == ("task-1",)

        resumed_task = await store2.get_task("task-1")
        resumed_item = await store2.get_delegation_work_item("work-item-1")
        assert resumed_task is not None and resumed_item is not None
        assert "dispatch_hold" not in resumed_task.metadata
        assert "dispatch_hold" not in resumed_item.metadata
        still_terminal = await store2.get_execution_checkpoint(
            persisted.checkpoint_id,
            project_id="project-a",
            checkpoint_type=persisted.checkpoint_type,
        )
        assert still_terminal is not None
        assert still_terminal.status == "outcome_unknown"
    finally:
        if recovery_coordinator is not None:
            await recovery_coordinator.shutdown()
        await store2.close()
        await store1.close()


@_async_test
async def test_shutdown_atomically_converges_final_delivery_runtime_projections(
    tmp_path: Path,
) -> None:
    """A stopped process must leave no run-scoped ``running`` projection."""

    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    store3: OPCStore | None = None
    try:
        task = await store1.get_task("task-1")
        role = await store1.get_delegation_role_session("role-session-1")
        assert task is not None and role is not None
        preserved_resume_state = {
            "resume_cursor": 17,
            "working_memory": ["preserve the final-delivery evidence"],
        }
        preserved_current_work_item = {
            "work_item_id": "work-item-1",
            "task_id": "task-1",
            "status": "running",
            "summary": "preserve this delivery context",
        }
        task.metadata["member_session_state"] = {
            "member_session_id": "member-session-1",
            "role_session_id": "role-session-1",
            "status": "running",
            "resident_status": "running",
            "current_task_id": "task-1",
            "focused_work_item_id": "work-item-1",
            "resume_state": dict(preserved_resume_state),
            "current_work_item": dict(preserved_current_work_item),
            "manager_digest": {
                "resident_status": "running",
                "current_work_item": dict(preserved_current_work_item),
            },
            "metadata": {"durable_member_marker": "keep"},
        }
        task.metadata["runtime_v2"] = {
            "runtime_session_id": "rt-final-delivery",
            "status": "running",
            "resume_cursor": 17,
            "artifact_manifest": [{"path": "delivery/report.md"}],
        }
        await store1.save_task(task)

        role.current_work_item = dict(preserved_current_work_item)
        role.manager_digest = {
            "resident_status": "running",
            "current_work_item": dict(preserved_current_work_item),
        }
        role.resume_state = dict(preserved_resume_state)
        await store1.save_delegation_role_session(
            role,
            controller_owner_token=owner,
            controller_lease_generation=generation,
        )
        await store1.save_seat_state(
            SeatState(
                seat_state_id="seat-state-1",
                team_instance_id="team-instance-1",
                run_id="run-1",
                project_id="project-a",
                team_id="team-1",
                seat_id="seat::executor",
                role_id="executor",
                member_session_id="member-session-1",
                role_runtime_session_id="role-session-1",
                status="running",
                resident_status="running",
                current_task_id="task-1",
                current_work_item_id="work-item-1",
                resume_state=dict(preserved_resume_state),
                current_work_item=dict(preserved_current_work_item),
                manager_digest={
                    "resident_status": "running",
                    "current_work_item": dict(preserved_current_work_item),
                },
                metadata={"durable_seat_marker": "keep"},
            ),
            controller_owner_token=owner,
            controller_lease_generation=generation,
        )
        runtime_rows = (
            (
                "member-session-1",
                "role-session-1",
                "task-1",
                {
                    "member_session_id": "member-session-1",
                    "role_session_id": "role-session-1",
                    "status": "running",
                    "resident_status": "running",
                    "current_task_id": "task-1",
                    "focused_work_item_id": "work-item-1",
                    "resume_state": dict(preserved_resume_state),
                    "current_work_item": dict(preserved_current_work_item),
                    "manager_digest": {
                        "resident_status": "running",
                        "current_work_item": dict(preserved_current_work_item),
                    },
                    "metadata": {"durable_member_marker": "keep"},
                    "company_run_id": "run-1",
                },
            ),
            (
                "rt-final-delivery",
                "root-session:delivery::work-item-1",
                "task-1",
                {
                    "resume_cursor": 17,
                    "artifact_manifest": [{"path": "delivery/report.md"}],
                },
            ),
            (
                "rt-ceo-pre-delivery",
                "root-session:delivery::work-item-1",
                "task-1::ceo_pre_delivery_assessment::attempt-1",
                {
                    "resume_cursor": 3,
                    "artifact_manifest": [{"kind": "assessment"}],
                },
            ),
        )
        for runtime_session_id, session_id, task_id, metadata in runtime_rows:
            await store1.save_runtime_session(
                runtime_session_id=runtime_session_id,
                project_id="project-a",
                session_id=session_id,
                task_id=task_id,
                status="running",
                metadata=metadata,
                controller_run_id="run-1",
                controller_owner_token=owner,
                controller_lease_generation=generation,
            )
            await store1.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task_id,
                session_id=session_id,
                role="assistant",
                content=f"transcript::{runtime_session_id}",
                metadata={"durable_transcript_marker": "keep"},
            )
        await store1.save_runtime_session(
            runtime_session_id="rt-unrelated",
            project_id="project-a",
            session_id="other-root",
            task_id="other-task",
            status="running",
            metadata={"unrelated": True},
        )

        transcripts_before = {
            runtime_session_id: await store2.list_runtime_transcript_entries(
                runtime_session_id
            )
            for runtime_session_id, *_ in runtime_rows
        }
        first = await engine.prepare_active_company_runtimes_for_shutdown()
        first_projection_rows = {
            runtime_session_id: await store2.get_runtime_session(
                runtime_session_id
            )
            for runtime_session_id, *_ in runtime_rows
        }
        first_seat = await store2.get_seat_state("seat-state-1")
        first_task = await store2.get_task("task-1")
        first_roles = await store2.list_role_runtime_sessions("run-1")
        second = await engine.prepare_active_company_runtimes_for_shutdown()

        assert first[0]["task_ids"] == ["task-1"]
        assert second[0]["task_ids"] == []
        assert first[0]["checkpoint_id"] == second[0]["checkpoint_id"]
        assert first_task is not None and first_seat is not None
        member_state = first_task.metadata["member_session_state"]
        assert member_state["status"] == "idle"
        assert member_state["resident_status"] == "idle"
        assert member_state["current_task_id"] == ""
        assert member_state["focused_work_item_id"] == ""
        assert member_state["resume_state"] == preserved_resume_state
        assert member_state["current_work_item"]["summary"] == (
            "preserve this delivery context"
        )
        assert member_state["current_work_item"]["status"] == "suspended"
        assert first_task.metadata["runtime_v2"]["status"] == "suspended"
        assert first_task.metadata["runtime_v2"]["resume_cursor"] == 17
        assert first_seat.status == first_seat.resident_status == "idle"
        assert first_seat.current_task_id == ""
        assert first_seat.current_work_item_id == ""
        assert first_seat.resume_state == preserved_resume_state
        assert first_seat.current_work_item["summary"] == (
            "preserve this delivery context"
        )
        assert first_seat.current_work_item["status"] == "suspended"
        assert first_roles and all(
            role_projection.status == "idle"
            and role_projection.focused_work_item_id == ""
            for role_projection in first_roles
        )
        assert first_roles[0].resume_state == preserved_resume_state
        assert first_roles[0].current_work_item["summary"] == (
            "preserve this delivery context"
        )
        assert first_projection_rows["member-session-1"]["status"] == "idle"
        assert first_projection_rows["rt-final-delivery"]["status"] == "suspended"
        assert first_projection_rows["rt-ceo-pre-delivery"]["status"] == "suspended"
        for runtime_session_id, *_ in runtime_rows:
            projection = first_projection_rows[runtime_session_id]
            assert projection is not None
            assert projection["metadata"].get("resume_cursor") in {None, 3, 17}
            assert (
                await store2.list_runtime_transcript_entries(runtime_session_id)
                == transcripts_before[runtime_session_id]
            )
            assert await store2.get_runtime_session(runtime_session_id) == projection
        with pytest.raises(CompanyRunControllerLeaseLost):
            await store1.save_runtime_session(
                runtime_session_id="rt-ceo-pre-delivery",
                project_id="project-a",
                session_id="root-session:delivery::work-item-1",
                task_id="task-1::ceo_pre_delivery_assessment::attempt-1",
                status="running",
                metadata={"late_prompt_tail": True},
                controller_run_id="run-1",
                controller_owner_token=owner,
                controller_lease_generation=generation,
            )
        assert (
            await store2.get_runtime_session("rt-ceo-pre-delivery")
            == first_projection_rows["rt-ceo-pre-delivery"]
        )
        unrelated = await store2.get_runtime_session("rt-unrelated")
        assert unrelated is not None and unrelated["status"] == "running"

        assert store2._db is not None
        for table in (
            "seat_states",
            "role_runtime_sessions",
            "delegation_role_sessions",
        ):
            async with store2._db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ? AND status = 'running'",
                ("run-1",),
            ) as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with store2._db.execute(
            """SELECT COUNT(*) FROM runtime_sessions
               WHERE status = 'running' AND runtime_session_id != 'rt-unrelated'"""
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0

        # Reopen through a fresh Store to prove bootstrap/restart cannot
        # rediscover a stale live projection.
        await store2.close()
        store3 = OPCStore(tmp_path / "tasks.db")
        await store3.initialize(run_startup_maintenance=False)
        restarted_task = await store3.get_task("task-1")
        restarted_seat = await store3.get_seat_state("seat-state-1")
        assert restarted_task is not None and restarted_seat is not None
        assert restarted_task.metadata["member_session_state"]["status"] == "idle"
        assert restarted_seat.status == "idle"
        assert (
            await store3.get_runtime_session("rt-ceo-pre-delivery")
        )["status"] == "suspended"
    finally:
        if store3 is not None:
            await store3.close()
        elif store2.is_ready:
            await store2.close()
        await store1.close()


@_async_test
async def test_engine_shutdown_joins_live_final_and_pre_delivery_native_runtimes(
    tmp_path: Path,
) -> None:
    """The Store closes only after both native cancellation cleanups finish."""

    store1, store2, engine, registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    owner_task: asyncio.Task[None] | None = None

    class BlockingLLM:
        def __init__(self) -> None:
            self.config = SimpleNamespace(max_tokens=2048)
            self.entered = asyncio.Event()

        def prepare_user_message_content(self, content, attachment_refs=None):
            _ = attachment_refs
            return content

        def get_tool_definitions(self, tools):
            return tools

        def is_context_overflow_error(self, error: Exception) -> bool:
            _ = error
            return False

        async def chat_stream(self, messages, tools=None):
            _ = (messages, tools)
            self.entered.set()
            await asyncio.Event().wait()
            if False:  # pragma: no cover - make this an async generator
                yield None

    class RuntimeMemory:
        def __init__(self, store: OPCStore) -> None:
            self.store = store

        async def build_session_memory_context(self, session_id: str) -> str:
            _ = session_id
            return ""

        async def record_user_turn(self, **kwargs):
            return SimpleNamespace(message_id=f"message::{kwargs['session_id']}")

        async def append_session_message(self, **kwargs):
            return SimpleNamespace(message_id=f"message::{kwargs['session_id']}")

        async def append_session_part(self, *args, **kwargs) -> None:
            _ = (args, kwargs)

    try:
        durable_task = await store1.get_task("task-1")
        assert durable_task is not None
        durable_task.metadata.update(
            {
                "company_run_controller_owner_token": owner,
                "company_run_controller_lease_generation": generation,
            }
        )
        final_task = durable_task
        final_task.metadata["runtime_v2"] = {
            "runtime_session_id": "rt-live-final-delivery"
        }
        final_task.context_snapshot["runtime_resume"] = {
            "runtime_session_id": "rt-live-final-delivery"
        }
        await store1.save_task(final_task)
        pre_delivery_task = engine._build_role_prompt_task(
            durable_task,
            prompt_kind="ceo_pre_delivery_assessment",
            description="Assess the final package.",
            execution_agent="native",
            force_new_session=True,
        )
        pre_delivery_task.id = (
            "task-1::ceo_pre_delivery_assessment::live-attempt"
        )
        pre_delivery_task.metadata["runtime_v2"] = {
            "runtime_session_id": "rt-live-pre-delivery"
        }
        pre_delivery_task.context_snapshot["runtime_resume"] = {
            "runtime_session_id": "rt-live-pre-delivery"
        }
        pre_delivery_task.status = TaskStatus.RUNNING
        await store1.save_task(pre_delivery_task)
        final_llm = BlockingLLM()
        pre_delivery_llm = BlockingLLM()
        memory = RuntimeMemory(store1)
        final_runtime = NativeRuntimeV2(
            llm=final_llm,  # type: ignore[arg-type]
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            config=OPCConfig(),
        )
        pre_delivery_runtime = NativeRuntimeV2(
            llm=pre_delivery_llm,  # type: ignore[arg-type]
            tool_registry=ToolRegistry(),
            memory_manager=memory,
            config=OPCConfig(),
        )
        cancellation_observation: dict[str, object] = {}

        async def live_delivery_owner() -> None:
            current = asyncio.current_task()
            assert current is not None
            attempt = registry.register(
                "project-a",
                "task-1",
                owner_task=current,
            )
            try:
                await asyncio.gather(
                    final_runtime.run(
                        system_prompt="Final delivery runtime.",
                        user_message="Prepare final delivery.",
                        task=final_task,
                    ),
                    pre_delivery_runtime.run(
                        system_prompt="CEO assessment runtime.",
                        user_message="Assess before delivery.",
                        task=pre_delivery_task,
                    ),
                )
            except asyncio.CancelledError:
                cancellation_observation["store_ready"] = store1.is_ready
                cancellation_observation["runtime_statuses"] = {
                    runtime_id: (
                        await store1.get_runtime_session(runtime_id)
                    )["status"]
                    for runtime_id in (
                        "rt-live-final-delivery",
                        "rt-live-pre-delivery",
                    )
                }
                cancellation_observation["checkpoint_count"] = len(
                    await store1.get_execution_checkpoints(
                        project_id="project-a",
                        session_id="root-session",
                        checkpoint_types=["company_runtime_interrupted"],
                        statuses=["pending"],
                    )
                )
                raise
            finally:
                registry.unregister("project-a", "task-1", attempt)

        owner_task = asyncio.create_task(live_delivery_owner())
        await asyncio.wait_for(final_llm.entered.wait(), timeout=2)
        await asyncio.wait_for(pre_delivery_llm.entered.wait(), timeout=2)
        assert await store1.get_runtime_session("rt-live-final-delivery") is not None
        assert await store1.get_runtime_session("rt-live-pre-delivery") is not None
        await engine.shutdown()
        await engine.shutdown()

        assert owner_task.cancelled()
        assert cancellation_observation == {
            "store_ready": True,
            "runtime_statuses": {
                "rt-live-final-delivery": "suspended",
                "rt-live-pre-delivery": "suspended",
            },
            "checkpoint_count": 1,
        }
        assert not store1.is_ready
        for runtime_id in (
            "rt-live-final-delivery",
            "rt-live-pre-delivery",
        ):
            runtime_projection = await store2.get_runtime_session(runtime_id)
            assert runtime_projection is not None
            assert runtime_projection["status"] == "suspended"
            assert len(
                await store2.list_runtime_transcript_entries(runtime_id)
            ) == 1
    finally:
        if owner_task is not None and not owner_task.done():
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
        await store2.close()
        if store1.is_ready:
            await store1.close()


@_async_test
async def test_native_role_prompt_auxiliary_task_is_durable_private_and_terminal(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    captured: dict[str, Task] = {}
    runtime_id = "rt-role-prompt-success"
    try:
        source = await store1.get_task("task-1")
        assert source is not None

        async def run_native(auxiliary: Task) -> TaskResult:
            captured["task"] = auxiliary
            durable = await store1.get_task(auxiliary.id)
            assert durable is not None
            assert durable.status == TaskStatus.RUNNING
            assert durable.parent_id == source.id
            auxiliary.metadata["runtime_v2"] = {
                "runtime_session_id": runtime_id,
                "status": "completed",
            }
            await store1.save_runtime_session(
                runtime_session_id=runtime_id,
                project_id="project-a",
                session_id=auxiliary.session_id,
                task_id=auxiliary.id,
                status="completed",
                metadata={"status": "completed"},
                controller_run_id="run-1",
                controller_owner_token=owner,
                controller_lease_generation=generation,
            )
            return TaskResult(
                status=TaskStatus.DONE,
                content='{"deliverable":true}',
                artifacts={"runtime_session_id": runtime_id},
            )

        engine._run_native_agent = run_native  # type: ignore[method-assign]
        response = await engine._run_role_prompt_via_task_execution_agent(
            source,
            "Return JSON only.",
            {"package": "ready"},
            "ceo_pre_delivery_assessment",
            True,
        )
        assert response == '{"deliverable":true}'
        auxiliary = captured["task"]
        assert auxiliary.session_id is not None
        assert auxiliary.session_id.startswith(
            f"{source.session_id}:aux:ceo_pre_delivery_assessment:"
        )
        assert auxiliary.parent_session_id == source.session_id
        assert auxiliary.metadata["_fork_allowed_tools"] == [
            "__opc_runtime_auxiliary_no_tools__"
        ]

        durable = await store2.get_task(auxiliary.id)
        runtime = await store2.get_runtime_session(runtime_id)
        assert durable is not None and durable.status == TaskStatus.DONE
        assert runtime is not None and runtime["status"] == "completed"
        assert durable.metadata["runtime_v2"]["runtime_session_id"] == runtime_id
        assert durable.metadata["runtime_v2"]["status"] == "completed"
        assert durable.context_snapshot["runtime_resume"][
            "runtime_session_id"
        ] == runtime_id
        assert durable.result["artifacts"]["runtime_session_id"] == runtime_id
        assert "work_item_projection_id" not in durable.metadata
        assert store2._db is not None
        async with store2._db.execute(
            "SELECT COUNT(*) FROM work_item_runtime_links WHERE runtime_task_id = ?",
            (auxiliary.id,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_native_auxiliary_exception_terminalizes_task_and_runtime(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    runtime_id = "rt-role-prompt-failure"
    captured: dict[str, Task] = {}
    try:
        source = await store1.get_task("task-1")
        assert source is not None
        auxiliary = engine._build_role_prompt_task(
            source,
            prompt_kind="ceo_pre_delivery_assessment",
            description="Assess.",
            execution_agent="native",
            force_new_session=True,
        )

        async def fail_after_runtime_started(task: Task) -> TaskResult:
            captured["task"] = task
            task.metadata["runtime_v2"] = {
                "runtime_session_id": runtime_id,
                "status": "running",
            }
            await store1.save_runtime_session(
                runtime_session_id=runtime_id,
                project_id="project-a",
                session_id=task.session_id,
                task_id=task.id,
                status="running",
                metadata={"status": "running"},
                controller_run_id="run-1",
                controller_owner_token=owner,
                controller_lease_generation=generation,
            )
            raise RuntimeError("provider finalizer failed")

        engine._run_native_agent = fail_after_runtime_started  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="provider finalizer failed"):
            await engine._run_native_runtime_auxiliary_task(auxiliary)

        durable = await store2.get_task(captured["task"].id)
        runtime = await store2.get_runtime_session(runtime_id)
        assert durable is not None and durable.status == TaskStatus.FAILED
        assert runtime is not None and runtime["status"] == "failed"
        assert durable.metadata["runtime_v2"]["runtime_session_id"] == runtime_id
        assert durable.metadata["runtime_v2"]["status"] == "failed"
        assert durable.context_snapshot["runtime_resume"][
            "runtime_session_id"
        ] == runtime_id
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_company_meeting_turn_uses_durable_unlinked_auxiliary_task(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    runtime_id = "rt-meeting-turn-success"
    captured: dict[str, Task] = {}
    try:
        engine.company_executor = None

        async def run_native(auxiliary: Task) -> TaskResult:
            captured["task"] = auxiliary
            durable = await store1.get_task(auxiliary.id)
            assert durable is not None and durable.status == TaskStatus.RUNNING
            assert "work_item_projection_id" not in durable.metadata
            assert "work_item_turn_type" not in durable.metadata
            auxiliary.metadata["runtime_v2"] = {
                "runtime_session_id": runtime_id,
                "status": "completed",
            }
            await store1.save_runtime_session(
                runtime_session_id=runtime_id,
                project_id="project-a",
                session_id=auxiliary.session_id,
                task_id=auxiliary.id,
                status="completed",
                metadata={"status": "completed"},
                controller_run_id="run-1",
                controller_owner_token=owner,
                controller_lease_generation=generation,
            )
            return TaskResult(
                status=TaskStatus.DONE,
                content='{"stance":"agree","proposal":"SQLite"}',
                artifacts={"runtime_session_id": runtime_id},
            )

        engine._run_native_agent = run_native  # type: ignore[method-assign]
        content = await engine._run_meeting_turn(
            MeetingRoom(
                room_id="meeting-room-1",
                task_id="task-1",
                topic="Storage",
            ),
            "executor",
            {
                "task_brief": "Choose storage.",
                "mode": "participant",
                "round": 1,
            },
        )
        assert content == '{"stance":"agree","proposal":"SQLite"}'
        auxiliary = captured["task"]
        assert auxiliary.id.startswith("task-1::meeting_turn::")
        assert auxiliary.parent_id == "task-1"
        assert auxiliary.metadata["runtime_auxiliary_projection_id"] == (
            "meeting::meeting-room-1::executor::round1"
        )
        assert auxiliary.metadata["runtime_auxiliary_kind"] == "meeting_turn"
        assert auxiliary.metadata["runtime_auxiliary_source_attempt_seq"] == 1
        assert auxiliary.metadata["company_run_controller_owner_token"] == owner
        durable = await store2.get_task(auxiliary.id)
        assert durable is not None and durable.status == TaskStatus.DONE
        assert store2._db is not None
        async with store2._db.execute(
            "SELECT COUNT(*) FROM work_item_runtime_links WHERE runtime_task_id = ?",
            (auxiliary.id,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_controller_takeover_terminalizes_prior_generation_auxiliary(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    runtime_id = "rt-role-prompt-stale-owner"
    try:
        source = await store1.get_task("task-1")
        assert source is not None
        auxiliary = engine._build_role_prompt_task(
            source,
            prompt_kind="ceo_pre_delivery_assessment",
            description="Assess.",
            execution_agent="native",
        )
        auxiliary.status = TaskStatus.RUNNING
        auxiliary.metadata["runtime_v2"] = {
            "runtime_session_id": runtime_id,
            "status": "running",
        }
        auxiliary.context_snapshot["runtime_resume"] = {
            "runtime_session_id": runtime_id
        }
        await store1.save_task(auxiliary)
        await store1.save_runtime_session(
            runtime_session_id=runtime_id,
            project_id="project-a",
            session_id=auxiliary.session_id,
            task_id=auxiliary.id,
            status="running",
            metadata={"status": "running"},
            controller_run_id="run-1",
            controller_owner_token=owner,
            controller_lease_generation=generation,
        )

        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired and takeover.generation > generation
        assert await store2.settle_stale_delegation_run_claims_for_controller(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
        ) == 1

        durable = await store2.get_task(auxiliary.id)
        runtime = await store2.get_runtime_session(runtime_id)
        assert durable is not None and durable.status == TaskStatus.CANCELLED
        assert durable.metadata["runtime_auxiliary_terminal_reason"] == (
            "controller_takeover"
        )
        assert runtime is not None and runtime["status"] == "failed"
        stale_tail = auxiliary
        stale_tail.status = TaskStatus.DONE
        with pytest.raises(CompanyRunControllerLeaseLost):
            await store1.save_task(stale_tail)
        assert (await store2.get_task(auxiliary.id)).status == TaskStatus.CANCELLED
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_plain_task_insert_does_not_create_empty_runtime_views(
    tmp_path: Path,
) -> None:
    store = OPCStore(tmp_path / "plain.db")
    await store.initialize()
    try:
        task = Task(
            id="plain-root",
            title="Plain root",
            project_id="project-a",
            context_snapshot={"root_context": "kept"},
            metadata={"root_identity": "kept"},
        )
        await store.save_task(task)
        durable = await store.get_task(task.id)
        assert durable is not None
        assert durable.context_snapshot == {"root_context": "kept"}
        assert durable.metadata == {"root_identity": "kept"}
    finally:
        await store.close()


@_async_test
async def test_shutdown_checkpoint_insert_failure_rolls_back_all_holds(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        task_before = await store1.get_task("task-1")
        assert task_before is not None
        task_before.metadata["member_session_state"] = {
            "member_session_id": "member-session-rollback",
            "status": "running",
            "resident_status": "running",
            "current_task_id": "task-1",
            "focused_work_item_id": "work-item-1",
            "resume_state": {"cursor": 9},
        }
        await store1.save_task(task_before)
        await store1.save_seat_state(
            SeatState(
                seat_state_id="seat-state-rollback",
                team_instance_id="team-instance-1",
                run_id="run-1",
                project_id="project-a",
                team_id="team-1",
                seat_id="seat::executor",
                role_id="executor",
                status="running",
                resident_status="running",
                current_task_id="task-1",
                current_work_item_id="work-item-1",
                resume_state={"cursor": 9},
            ),
            controller_owner_token=owner,
            controller_lease_generation=generation,
        )
        await store1.save_runtime_session(
            runtime_session_id="rt-rollback-pre-delivery",
            project_id="project-a",
            session_id="root-session:delivery",
            task_id="task-1::ceo_pre_delivery_assessment::rollback",
            status="running",
            metadata={"resume_cursor": 9},
            controller_run_id="run-1",
            controller_owner_token=owner,
            controller_lease_generation=generation,
        )
        task_before = await store2.get_task("task-1")
        item_before = await store2.get_delegation_work_item("work-item-1")
        seat_before = await store2.get_seat_state("seat-state-rollback")
        runtime_before = await store2.get_runtime_session(
            "rt-rollback-pre-delivery"
        )
        role_before = await store2.get_delegation_role_session("role-session-1")
        assert store1._db is not None
        await store1._db.execute(
            """CREATE TRIGGER reject_shutdown_checkpoint
               BEFORE INSERT ON execution_checkpoints
               WHEN NEW.checkpoint_type = 'company_runtime_interrupted'
               BEGIN
                   SELECT RAISE(ABORT, 'injected checkpoint failure');
               END"""
        )
        await store1._db.commit()

        with pytest.raises(Exception, match="injected checkpoint failure"):
            await engine.prepare_active_company_runtimes_for_shutdown()

        task_after = await store2.get_task("task-1")
        item_after = await store2.get_delegation_work_item("work-item-1")
        checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
        )
        assert task_after == task_before
        assert item_after == item_before
        assert await store2.get_seat_state("seat-state-rollback") == seat_before
        assert (
            await store2.get_runtime_session("rt-rollback-pre-delivery")
            == runtime_before
        )
        assert (
            await store2.get_delegation_role_session("role-session-1")
            == role_before
        )
        assert checkpoints == []
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_remote_owner_shutdown_skips_scope_without_mutation(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        task_before = await store2.get_task("task-1")
        item_before = await store2.get_delegation_work_item("work-item-1")
        engine.company_executor = SimpleNamespace(
            controller_lease_credential=lambda _run_id: None
        )

        assert await engine.prepare_active_company_runtimes_for_shutdown() == []
        assert await store2.get_task("task-1") == task_before
        assert await store2.get_delegation_work_item("work-item-1") == item_before
        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
            )
            == []
        )
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_shutdown_hold_fences_same_generation_tail_writes(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, _owner, _generation = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        stale_task = await store1.get_task("task-1")
        assert stale_task is not None
        context = CompanyControllerAttemptContext.from_task(
            stale_task,
            work_item_id="work-item-1",
        )
        assert context.complete

        assert len(await engine.prepare_active_company_runtimes_for_shutdown()) == 1
        held_task = await store2.get_task("task-1")
        held_item = await store2.get_delegation_work_item("work-item-1")
        held_checkpoints = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
        )
        assert held_task is not None and held_item is not None
        assert len(held_checkpoints) == 1

        stale_task.status = TaskStatus.FAILED
        stale_task.result = {"late_generation_one_result": True}
        with pytest.raises(CompanyRunControllerLeaseLost):
            await store1.save_task(stale_task)
        with pytest.raises(CompanyRunControllerLeaseLost):
            await transition_work_item_from_task(
                store1,
                stale_task,
                target_status_or_phase=Phase.FAILED,
                reason="late_shutdown_tail",
                summary="must not land",
                require_work_item=True,
            )
        with pytest.raises(CompanyRunControllerLeaseLost):
            await store1.execute_company_controller_authoritative_command(
                context,
                operation="late_shutdown_tail",
                mutations=(
                    CompanyControllerWorkItemMutation(
                        work_item_id="work-item-1",
                        expected_phases=(Phase.RUNNING,),
                        summary="must not land",
                    ),
                ),
            )

        assert await store2.get_task("task-1") == held_task
        assert await store2.get_delegation_work_item("work-item-1") == held_item
        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
            )
            == held_checkpoints
        )
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_new_generation_atomically_resumes_without_inheriting_old_attempt(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation1 = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        checkpoint_id = prepared[0]["checkpoint_id"]
        held_task = await store2.get_task("task-1")
        held_item = await store2.get_delegation_work_item("work-item-1")
        assert held_task is not None and held_item is not None
        assert held_task.metadata["company_run_controller_owner_token"] == owner
        assert held_task.metadata["claimed_work_item_attempt_seq"] == 1

        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation1,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired and takeover.generation > generation1

        stale = await store1.resume_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token=owner,
            generation=generation1,
            checkpoint_id=checkpoint_id,
            checkpoint_types=["company_runtime_interrupted"],
            expected_checkpoint_statuses=["pending"],
            task_ids=["task-1"],
        )
        remote = await store1.resume_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-c",
            generation=takeover.generation,
            checkpoint_id=checkpoint_id,
            checkpoint_types=["company_runtime_interrupted"],
            expected_checkpoint_statuses=["pending"],
            task_ids=["task-1"],
        )
        assert stale.outcome == "stale"
        assert remote.outcome == "stale"
        assert await store2.get_task("task-1") == held_task
        assert await store2.get_delegation_work_item("work-item-1") == held_item

        resumed = await store2.resume_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            checkpoint_id=checkpoint_id,
            checkpoint_types=["company_runtime_interrupted"],
            expected_checkpoint_statuses=["pending"],
            task_ids=["task-1"],
            checkpoint_payload_updates={
                "resume_state": "resuming",
                "resume_started_by": "owner-b",
            },
        )
        assert resumed.applied
        assert resumed.affected_task_ids == ("task-1",)
        assert resumed.checkpoint is not None
        assert resumed.checkpoint.status == "resuming"
        assert resumed.checkpoint.payload["resume_started_by"] == "owner-b"

        task_after = await store2.get_task("task-1")
        item_after = await store2.get_delegation_work_item("work-item-1")
        assert task_after is not None and item_after is not None
        for key in (
            "dispatch_hold",
            "company_runtime_stop_state",
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
            "claimed_work_item_attempt_seq",
        ):
            assert key not in task_after.metadata
        assert "dispatch_hold" not in item_after.metadata
        assert item_after.claimed_by_role_runtime_session_id == ""
        assert item_after.claimed_by_seat_id == ""

        # Only the ordinary claim CAS creates generation two's new attempt.
        claimed = await store2.claim_delegation_work_item_if_dispatchable(
            "work-item-1",
            expected_phase=item_after.phase,
            role_runtime_session_id="role-session-2",
            seat_id="seat::executor",
            task_id="task-1",
            controller_owner_token="owner-b",
            controller_lease_generation=takeover.generation,
        )
        assert claimed is not None
        assert int(claimed.metadata["attempt_seq"]) == 2
        still_unstamped = await store2.get_task("task-1")
        assert still_unstamped is not None
        assert "company_run_controller_owner_token" not in still_unstamped.metadata
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_resume_checkpoint_failure_rolls_back_released_holds(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation1 = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        checkpoint_id = prepared[0]["checkpoint_id"]
        held_task = await store2.get_task("task-1")
        held_item = await store2.get_delegation_work_item("work-item-1")
        held_checkpoint = (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["pending"],
            )
        )[0]
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation1,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired
        assert store1._db is not None
        await store1._db.execute(
            """CREATE TRIGGER reject_resume_checkpoint
               BEFORE UPDATE ON execution_checkpoints
               WHEN NEW.status = 'resuming'
               BEGIN
                   SELECT RAISE(ABORT, 'injected resume checkpoint failure');
               END"""
        )
        await store1._db.commit()

        with pytest.raises(Exception, match="injected resume checkpoint failure"):
            await store1.resume_company_runtime_scope_for_controller(
                run_id="run-1",
                project_id="project-a",
                root_session_id="root-session",
                owner_token="owner-b",
                generation=takeover.generation,
                checkpoint_id=checkpoint_id,
                checkpoint_types=["company_runtime_interrupted"],
                expected_checkpoint_statuses=["pending"],
                task_ids=["task-1"],
            )

        assert await store2.get_task("task-1") == held_task
        assert await store2.get_delegation_work_item("work-item-1") == held_item
        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["pending"],
            )
        ) == [held_checkpoint]
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_failed_typed_handoff_atomically_restores_pending_holds(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation1 = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        checkpoint_id = prepared[0]["checkpoint_id"]
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation1,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=ActiveTaskRunRegistry(),
            owns_active_task_run_registry=True,
        )
        engine.store = store1
        admission = SimpleNamespace(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            released=False,
        )
        release = AsyncMock()
        engine.company_executor = SimpleNamespace(
            acquire_controller_admission=AsyncMock(return_value=admission),
            release_controller_admission=release,
            _notify_kanban_changed=AsyncMock(),
        )
        engine._prepare_company_runtime_tasks_for_resume = AsyncMock(
            side_effect=RuntimeError("injected projection failure")
        )
        checkpoint = (
            await store1.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["pending"],
            )
        )[0]
        task = await store1.get_task("task-1")
        assert task is not None and checkpoint.checkpoint_id == checkpoint_id

        with pytest.raises(RuntimeError, match="injected projection failure"):
            await engine._handoff_company_suspend_checkpoint(
                checkpoint,
                payload=dict(checkpoint.payload),
                parent_session_id="root-session",
                tasks=[task],
            )

        pending = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        held_task = await store2.get_task("task-1")
        held_item = await store2.get_delegation_work_item("work-item-1")
        assert len(pending) == 1
        assert pending[0].checkpoint_id == checkpoint_id
        assert pending[0].payload["resume_state"] == "failed_before_handoff"
        assert held_task is not None and held_item is not None
        assert held_task.metadata["dispatch_hold"] == "company_runtime_suspended"
        assert held_item.metadata["dispatch_hold"] == "company_runtime_suspended"
        release.assert_awaited_once_with(admission)
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_takeover_after_atomic_resume_fences_task_projection_tail(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation1 = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        checkpoint_id = prepared[0]["checkpoint_id"]
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation1,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired
        engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=ActiveTaskRunRegistry(),
            owns_active_task_run_registry=True,
        )
        engine.store = store1
        admission = SimpleNamespace(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            released=False,
        )
        engine.company_executor = SimpleNamespace(
            acquire_controller_admission=AsyncMock(return_value=admission),
            release_controller_admission=AsyncMock(),
            _notify_kanban_changed=AsyncMock(),
        )
        projection_entered = asyncio.Event()
        continue_projection = asyncio.Event()
        original_projection = (
            store1.save_company_runtime_resume_projection_for_controller
        )

        async def gated_projection(*args, **kwargs):
            projection_entered.set()
            await continue_projection.wait()
            return await original_projection(*args, **kwargs)

        store1.save_company_runtime_resume_projection_for_controller = (  # type: ignore[method-assign]
            gated_projection
        )
        checkpoint = (
            await store1.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["pending"],
            )
        )[0]
        task = await store1.get_task("task-1")
        assert task is not None and checkpoint.checkpoint_id == checkpoint_id
        handoff = asyncio.create_task(
            engine._handoff_company_suspend_checkpoint(
                checkpoint,
                payload=dict(checkpoint.payload),
                parent_session_id="root-session",
                tasks=[task],
            )
        )
        await asyncio.wait_for(projection_entered.wait(), timeout=2)
        task_at_barrier = await store2.get_task("task-1")
        item_at_barrier = await store2.get_delegation_work_item("work-item-1")
        assert task_at_barrier is not None and item_at_barrier is not None
        assert "dispatch_hold" not in task_at_barrier.metadata
        assert "dispatch_hold" not in item_at_barrier.metadata

        await _expire_lease(
            store2,
            owner_token="owner-b",
            generation=takeover.generation,
        )
        next_takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-c",
            lease_seconds=60,
        )
        assert next_takeover.acquired
        continue_projection.set()

        with pytest.raises(CompanyRunControllerLeaseLost):
            await handoff
        assert await store2.get_task("task-1") == task_at_barrier
        assert await store2.get_delegation_work_item("work-item-1") == item_at_barrier
        resuming = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["resuming"],
        )
        assert len(resuming) == 1 and resuming[0].checkpoint_id == checkpoint_id
        engine.company_executor._notify_kanban_changed.assert_not_awaited()

        # The higher generation can reclaim the orphaned ``resuming`` card
        # without waiting for a process restart or inventing an old attempt.
        store1.save_company_runtime_resume_projection_for_controller = (  # type: ignore[method-assign]
            original_projection
        )
        next_admission = SimpleNamespace(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-c",
            generation=next_takeover.generation,
            released=False,
        )
        next_engine = OPCEngine(
            project_id="project-a",
            active_task_run_registry=ActiveTaskRunRegistry(),
            owns_active_task_run_registry=True,
        )
        next_engine.store = store1
        next_engine.company_executor = SimpleNamespace(
            acquire_controller_admission=AsyncMock(return_value=next_admission),
            release_controller_admission=AsyncMock(),
            _notify_kanban_changed=AsyncMock(),
        )
        current_task = await store1.get_task("task-1")
        assert current_task is not None
        reclaimed = await next_engine._handoff_company_suspend_checkpoint(
            resuming[0],
            payload=dict(resuming[0].payload),
            parent_session_id="root-session",
            tasks=[current_task],
        )
        assert reclaimed is not None
        _reclaimed_tasks, reclaimed_driver = reclaimed
        assert reclaimed_driver is not None
        reclaimed_driver.release()
        refreshed_checkpoint = (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["resuming"],
            )
        )[0]
        assert (
            refreshed_checkpoint.payload["resume_controller_lease_generation"]
            == next_takeover.generation
        )
        refreshed_task = await store2.get_task("task-1")
        assert refreshed_task is not None
        assert "company_run_controller_owner_token" not in refreshed_task.metadata
        assert "claimed_work_item_attempt_seq" not in refreshed_task.metadata
    finally:
        await store2.close()
        await store1.close()


@_async_test
async def test_stale_completion_cannot_resolve_checkpoint_or_reopen_ui_anchor(
    tmp_path: Path,
) -> None:
    store1, store2, engine, _registry, owner, generation1 = (
        await _seed_owned_scope(tmp_path)
    )
    try:
        prepared = await engine.prepare_active_company_runtimes_for_shutdown()
        checkpoint_id = prepared[0]["checkpoint_id"]
        await _expire_lease(
            store1,
            owner_token=owner,
            generation=generation1,
        )
        takeover = await store2.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            lease_seconds=60,
        )
        assert takeover.acquired
        anchor = Task(
            id="ui-anchor",
            title="Company chat",
            session_id="root-session",
            project_id="project-a",
            status=TaskStatus.CANCELLED,
        )
        await store2.save_task(anchor)
        resumed = await store2.resume_company_runtime_scope_for_controller(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            checkpoint_id=checkpoint_id,
            checkpoint_types=["company_runtime_interrupted"],
            expected_checkpoint_statuses=["pending"],
            task_ids=["task-1"],
            checkpoint_payload_updates={"ui_anchor_task_id": anchor.id},
        )
        assert resumed.applied and resumed.checkpoint is not None
        stale_admission = SimpleNamespace(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            released=False,
        )
        await _expire_lease(
            store2,
            owner_token="owner-b",
            generation=takeover.generation,
        )
        next_takeover = await store1.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-c",
            lease_seconds=60,
        )
        assert next_takeover.acquired
        checkpoint_before = (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["resuming"],
            )
        )[0]
        anchor_before = await store2.get_task(anchor.id)
        stale_anchor = await store1.get_task(anchor.id)
        assert stale_anchor is not None
        stale_anchor.metadata = {
            **dict(stale_anchor.metadata or {}),
            "stale_resume_projection": True,
        }
        ui_projection = await store1.save_company_runtime_ui_anchor_for_controller(
            stale_anchor,
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-b",
            generation=takeover.generation,
            checkpoint_id=checkpoint_id,
            checkpoint_types=["company_runtime_interrupted"],
        )
        assert ui_projection.outcome == "stale"
        assert await store2.get_task(anchor.id) == anchor_before

        with pytest.raises(CompanyRunControllerLeaseLost):
            await engine._complete_company_suspend_checkpoint_resume(
                resumed.checkpoint,
                parent_session_id="root-session",
                controller_admission=stale_admission,
            )

        assert (
            await store2.get_execution_checkpoints(
                project_id="project-a",
                session_id="root-session",
                checkpoint_types=["company_runtime_interrupted"],
                statuses=["resuming"],
            )
        ) == [checkpoint_before]
        assert await store2.get_task(anchor.id) == anchor_before

        current_admission = SimpleNamespace(
            run_id="run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="owner-c",
            generation=next_takeover.generation,
            released=False,
        )
        await engine._complete_company_suspend_checkpoint_resume(
            checkpoint_before,
            parent_session_id="root-session",
            controller_admission=current_admission,
        )
        resolved = await store2.get_execution_checkpoints(
            project_id="project-a",
            session_id="root-session",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["resolved"],
        )
        reopened = await store2.get_task(anchor.id)
        assert len(resolved) == 1
        assert reopened is not None and reopened.status == TaskStatus.IDLE
    finally:
        await store2.close()
        await store1.close()
