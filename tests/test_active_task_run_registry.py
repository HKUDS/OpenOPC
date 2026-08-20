from __future__ import annotations

import asyncio
from functools import wraps
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.models import (
    CompanyMemberSession,
    ExecutionCheckpoint,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.layer0_interaction.coordinator import InteractionDecisionLease
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.custom_runtime import CustomRuntimeRunner
from opc.layer2_organization.company_runtime_identity import is_company_runtime_task
from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan


def _async_test(func):
    @wraps(func)
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


def test_overlapping_attempts_remain_active_until_last_attempt_exits() -> None:
    registry = ActiveTaskRunRegistry()
    first = registry.register("project-a", "task-1")
    second = registry.register("project-a", "task-1")

    assert first != second
    assert registry.attempt_count("project-a", "task-1") == 2
    assert registry.is_active("project-a", "task-1")
    assert registry.unregister("project-a", "task-1", first)
    assert registry.is_active("project-a", "task-1")
    assert registry.unregister("project-a", "task-1", second)
    assert not registry.is_active("project-a", "task-1")


def test_registry_isolates_projects_with_equal_task_ids() -> None:
    registry = ActiveTaskRunRegistry()
    token = registry.register("project-a", "task-1")

    assert registry.active_task_ids("project-a") == {"task-1"}
    assert registry.active_task_ids("project-b") == set()
    assert not registry.is_active("project-b", "task-1")
    assert registry.unregister("project-a", "task-1", token)


def test_plain_child_task_is_not_classified_as_company_runtime_scope() -> None:
    task = Task(
        id="plain-task",
        title="Plain task",
        project_id="project-a",
        parent_session_id="parent-session",
        metadata={"mode": "task", "parent_session_id": "parent-session"},
    )

    assert not is_company_runtime_task(task)


def test_closing_admission_preserves_existing_attempts_and_rejects_new_ones() -> None:
    registry = ActiveTaskRunRegistry()
    token = registry.register("project-a", "task-1")

    registry.close_admission()

    assert registry.admission_closed
    assert registry.active_task_ids("project-a") == {"task-1"}
    assert registry.is_active("project-a", "task-1")
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "task-2")
    assert registry.unregister("project-a", "task-1", token)


def test_closed_admission_allows_only_nested_live_driver_attempts() -> None:
    registry = ActiveTaskRunRegistry()
    driver_token = registry.register("project-a", "driver-task")

    with registry.bind_driver_attempt(driver_token):
        registry.close_admission()
        nested_token = registry.register("project-a", "claimed-child")
        assert registry.is_active("project-a", "claimed-child")
        registry.unregister("project-a", "claimed-child", nested_token)

    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "new-ingress")
    registry.unregister("project-a", "driver-task", driver_token)


@_async_test
async def test_shutdown_cancels_and_joins_registered_coroutine_owner() -> None:
    registry = ActiveTaskRunRegistry()
    started = asyncio.Event()

    async def execute() -> None:
        current = asyncio.current_task()
        assert current is not None
        token = registry.register(
            "project-a",
            "task-1",
            owner_task=current,
        )
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            registry.unregister("project-a", "task-1", token)

    owner = asyncio.create_task(execute())
    await started.wait()

    await registry.cancel_and_wait_for_owner_tasks()

    assert owner.cancelled()
    assert registry.active_owner_tasks() == set()
    assert not registry.is_active("project-a", "task-1")


@_async_test
async def test_custom_child_normal_teardown_unregisters_and_is_idempotent() -> None:
    parent = OPCEngine(project_id="project-a")
    child = OPCEngine(
        project_id="project-a",
        active_task_run_registry=parent._active_task_run_registry,
        owns_active_task_run_registry=False,
    )
    runner = CustomRuntimeRunner(parent)
    ingress = parent._register_runtime_child(child)
    runner._runtime_ingress_tasks[id(child)] = ingress

    await runner._shutdown_runtime(child)
    await runner._shutdown_runtime(child)

    assert child._shutdown_complete
    assert child not in parent._runtime_child_engines
    assert asyncio.current_task() not in parent._runtime_child_ingress_tasks


@_async_test
async def test_normal_borrowed_custom_child_does_not_suspend_peer_scope() -> None:
    registry = ActiveTaskRunRegistry()
    parent = OPCEngine(
        project_id="project-a",
        active_task_run_registry=registry,
        owns_active_task_run_registry=True,
    )
    child = OPCEngine(
        project_id="project-a",
        store=SimpleNamespace(is_ready=True),  # type: ignore[arg-type]
        owns_store=False,
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )
    child.prepare_active_company_runtimes_for_shutdown = AsyncMock(
        side_effect=AssertionError("normal child must not scan peer scopes")
    )
    runner = CustomRuntimeRunner(parent)
    ingress = parent._register_runtime_child(child)
    runner._runtime_ingress_tasks[id(child)] = ingress
    peer_token = registry.register("project-a", "peer-scope-task")

    await runner._shutdown_runtime(child)

    child.prepare_active_company_runtimes_for_shutdown.assert_not_awaited()
    assert registry.is_active("project-a", "peer-scope-task")
    assert not registry.admission_closed
    registry.unregister("project-a", "peer-scope-task", peer_token)


@_async_test
async def test_shutdown_barrier_allows_only_reserved_handoff_to_register() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()

    with registry.bind_handoff(handoff_token):
        barrier = asyncio.create_task(
            registry.close_admission_and_wait_for_handoffs()
        )
        await asyncio.sleep(0)

        assert not barrier.done()
        attempt_token = registry.register("project-a", "task-1")

    registry.release_handoff(handoff_token)
    await asyncio.wait_for(barrier, timeout=0.1)

    # The handoff wait ends at real coroutine registration, not at the end of
    # that execution attempt.
    assert registry.is_active("project-a", "task-1")
    assert registry.pending_handoff_count == 0
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        registry.register("project-a", "late-task")
    assert registry.unregister("project-a", "task-1", attempt_token)


@_async_test
async def test_shutdown_barrier_drains_request_that_exits_before_registration() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()
    barrier = asyncio.create_task(registry.close_admission_and_wait_for_handoffs())
    await asyncio.sleep(0)

    assert not barrier.done()
    assert registry.release_handoff(handoff_token)
    await asyncio.wait_for(barrier, timeout=0.1)

    assert registry.pending_handoff_count == 0
    assert registry.active_task_ids("project-a") == set()


@_async_test
async def test_revoked_handoff_cannot_block_shutdown_or_register_late() -> None:
    registry = ActiveTaskRunRegistry()
    handoff_token = registry.reserve_handoff()

    with registry.bind_handoff(handoff_token):
        assert registry.retain_current_handoff() == handoff_token
        registry.close_admission()
        assert registry.revoke_handoff(handoff_token)
        await asyncio.wait_for(
            registry.close_admission_and_wait_for_handoffs(),
            timeout=0.1,
        )
        with pytest.raises(ActiveTaskRunAdmissionClosed):
            registry.register("project-a", "late-task")

    assert registry.pending_handoff_count == 0
    assert not registry.release_handoff(handoff_token)


@_async_test
async def test_engine_turns_closed_admission_into_infrastructure_cancellation() -> None:
    registry = ActiveTaskRunRegistry()
    registry.close_admission()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine._run_task_once = AsyncMock()
    task = Task(
        id="late-task",
        title="Late task",
        project_id="project-a",
        status=TaskStatus.PENDING,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine._run_task_once.assert_not_awaited()
    assert registry.active_task_ids("project-a") == set()


@_async_test
async def test_task_liveness_uses_registry_only() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine.store = SimpleNamespace(get_latest_external_session_for_task=AsyncMock())
    task = Task(
        id="task-1",
        title="Live task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    assert not await engine._task_runtime_is_live(task)
    engine.store.get_latest_external_session_for_task.assert_not_awaited()

    token = registry.register("project-a", task.id)
    assert await engine._task_runtime_is_live(task)
    registry.unregister("project-a", task.id, token)


@_async_test
async def test_project_delegate_receives_controller_registry() -> None:
    registry = ActiveTaskRunRegistry()
    root = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    root._initialized = True
    captured: dict[str, object] = {}

    class FakeDelegate:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.store = None

        async def initialize(self) -> None:
            return None

    with patch("opc.engine.OPCEngine", FakeDelegate):
        delegate = await root._get_project_delegate("project-b")

    assert delegate is root._project_engine_delegates["project-b"]
    assert captured["active_task_run_registry"] is registry
    assert captured["owns_active_task_run_registry"] is False


@_async_test
async def test_shutdown_cancellation_does_not_write_business_cancelled() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine._shutting_down = True
    engine.store = SimpleNamespace(
        is_ready=True,
        get_task=AsyncMock(),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(side_effect=asyncio.CancelledError)
    task = Task(
        id="task-1",
        title="Interrupted task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.get_task.assert_not_awaited()
    engine.store.save_task.assert_not_awaited()
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_company_cancellation_never_synthesizes_hold_without_checkpoint() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.store = SimpleNamespace(
        is_ready=True,
        get_task=AsyncMock(),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(side_effect=asyncio.CancelledError)
    task = Task(
        id="company-task",
        title="Company task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.get_task.assert_not_awaited()
    engine.store.save_task.assert_not_awaited()
    assert "company_runtime_suspended_at" not in task.metadata
    assert "last_stop_reason" not in task.metadata


@_async_test
async def test_suspended_checkpoint_discards_racing_task_completion() -> None:
    engine = OPCEngine(project_id="project-a")
    engine._shutting_down = False
    task = Task(
        id="company-task",
        title="Company task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )
    suspended = Task(
        id=task.id,
        title=task.title,
        project_id=task.project_id,
        status=TaskStatus.BLOCKED,
        metadata={
            "work_item_runtime": True,
            "dispatch_hold": "company_runtime_suspended",
            "company_runtime_stop_state": "suspended",
        },
    )
    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=suspended),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.DONE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()

    with pytest.raises(asyncio.CancelledError):
        await engine._execute_task(task)

    engine.store.save_task.assert_not_awaited()
    engine._apply_runtime_state_to_task.assert_not_called()


@_async_test
async def test_attempt_stays_active_until_result_persistence_finishes() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    save_started = asyncio.Event()
    allow_save = asyncio.Event()

    async def blocked_save(_task: Task) -> None:
        save_started.set()
        await allow_save.wait()

    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=None),
        save_task=blocked_save,
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.IDLE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()
    task = Task(
        id="persisting-task",
        title="Persisting task",
        project_id="project-a",
        status=TaskStatus.RUNNING,
    )

    execution = asyncio.create_task(engine._execute_task(task))
    await save_started.wait()
    assert registry.is_active("project-a", task.id)
    allow_save.set()
    await execution
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_claimed_work_item_ownership_covers_post_execution_finalize_gap() -> None:
    registry = ActiveTaskRunRegistry()
    engine = OPCEngine(project_id="project-a", active_task_run_registry=registry)
    engine.store = SimpleNamespace(
        get_task=AsyncMock(return_value=None),
        save_task=AsyncMock(),
    )
    engine._run_task_once = AsyncMock(
        return_value=TaskResult(status=TaskStatus.IDLE, content="done", artifacts={})
    )
    engine._apply_runtime_state_to_task = MagicMock()
    task = Task(
        id="finalizing-work-item",
        title="Finalizing work item",
        project_id="project-a",
        parent_session_id="runtime-session",
        status=TaskStatus.RUNNING,
        metadata={"work_item_runtime": True},
    )
    inner_finished = asyncio.Event()
    allow_finalize = asyncio.Event()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry

    async def run_claimed(*_args: object, **_kwargs: object) -> TaskResult:
        result = await engine._execute_task(task)
        inner_finished.set()
        await allow_finalize.wait()
        return result

    executor._run_claimed_work_item = run_claimed
    owned = executor._create_claimed_work_item_task(
        CompanyMemberSession(
            role_id="executor",
            seat_id="seat::executor",
            member_session_id="role-session",
        ),
        task,
        {},
    )
    await inner_finished.wait()

    assert registry.attempt_count("project-a", task.id) == 1
    allow_finalize.set()
    await owned
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_work_item_claim_and_spawn_share_stop_scope_lock() -> None:
    registry = ActiveTaskRunRegistry()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry
    claim_entered = asyncio.Event()
    allow_claim = asyncio.Event()
    allow_child_exit = asyncio.Event()
    stop_acquired = asyncio.Event()
    task = Task(
        id="claimed-task",
        title="Claimed task",
        project_id="project-a",
        session_id="role-session",
        parent_session_id="runtime-session",
        metadata={"work_item_runtime": True},
    )
    member_session = CompanyMemberSession(
        role_id="executor",
        seat_id="seat::executor",
        member_session_id="role-session",
    )

    async def claim_runnable_tasks(
        _tasks: list[Task],
        *,
        work_items: list[object],
        **_kwargs: object,
    ) -> list[tuple[CompanyMemberSession, Task]]:
        del work_items
        claim_entered.set()
        await allow_claim.wait()
        return [(member_session, task)]

    async def run_claimed(*_args: object, **_kwargs: object) -> None:
        await allow_child_exit.wait()

    executor.runtime = SimpleNamespace(
        claim_runnable_tasks=claim_runnable_tasks,
    )
    executor._run_claimed_work_item = run_claimed
    active: dict[asyncio.Task, tuple[CompanyMemberSession, Task]] = {}
    scheduled = asyncio.create_task(
        executor._claim_and_create_work_item_tasks([task], [], active)
    )
    await claim_entered.wait()

    async def stop_scope() -> None:
        async with registry.scope_lock("project-a", "runtime-session"):
            assert registry.is_active("project-a", task.id)
            stop_acquired.set()

    stopping = asyncio.create_task(stop_scope())
    await asyncio.sleep(0)
    assert not stop_acquired.is_set()

    allow_claim.set()
    await scheduled
    await stopping
    assert len(active) == 1

    allow_child_exit.set()
    await asyncio.gather(*active)
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_company_executor_driver_ownership_covers_idle_scheduler_window() -> None:
    registry = ActiveTaskRunRegistry()
    entered = asyncio.Event()
    allow_exit = asyncio.Event()
    executor = object.__new__(CompanyWorkItemExecutor)
    executor.active_task_run_registry = registry

    async def idle_scheduler(
        _plan: CompanyWorkItemRuntimePlan,
        _tasks: list[Task],
    ) -> str:
        entered.set()
        await allow_exit.wait()
        return "done"

    executor._execute_multi_team_org = idle_scheduler
    task = Task(
        id="driver-task",
        title="Driver task",
        project_id="project-a",
        parent_session_id="runtime-session",
        metadata={"work_item_runtime": True},
    )
    execution = asyncio.create_task(
        executor.execute(CompanyWorkItemRuntimePlan(), [task])
    )
    await entered.wait()

    assert registry.is_active("project-a", task.id)
    allow_exit.set()
    assert await execution == "done"
    assert not registry.is_active("project-a", task.id)


@_async_test
async def test_borrowed_engine_shutdown_keeps_controller_registry_open() -> None:
    registry = ActiveTaskRunRegistry()
    root_token = registry.register("project-a", "root-attempt")
    borrowed = OPCEngine(
        project_id="project-a",
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )

    await borrowed.shutdown()

    assert not registry.admission_closed
    assert registry.is_active("project-a", "root-attempt")
    next_token = registry.register("project-a", "next-attempt")
    registry.unregister("project-a", "next-attempt", next_token)
    registry.unregister("project-a", "root-attempt", root_token)


@_async_test
async def test_shutdown_preparation_includes_project_delegates() -> None:
    engine = OPCEngine(project_id="project-a")
    delegate_prepare = AsyncMock(
        return_value=[{"session_id": "delegate-session", "checkpoint_id": "checkpoint-1"}]
    )
    engine._project_engine_delegates["project-b"] = SimpleNamespace(
        prepare_active_company_runtimes_for_shutdown=delegate_prepare,
    )

    prepared = await engine.prepare_active_company_runtimes_for_shutdown()

    assert prepared == [{"session_id": "delegate-session", "checkpoint_id": "checkpoint-1"}]
    delegate_prepare.assert_awaited_once()


@_async_test
async def test_engine_shutdown_prepares_before_closing_subsystems() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(return_value=[])

    await engine.shutdown()

    engine.prepare_active_company_runtimes_for_shutdown.assert_awaited_once()


@_async_test
async def test_engine_shutdown_does_not_close_store_when_durable_prepare_fails() -> None:
    engine = OPCEngine(project_id="project-a")
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(
        side_effect=RuntimeError("checkpoint failed")
    )
    engine.store = SimpleNamespace(close=AsyncMock())
    engine.message_bus.stop = MagicMock()

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        await engine.shutdown()

    engine.message_bus.stop.assert_not_called()
    engine.store.close.assert_not_awaited()


@_async_test
async def test_nested_delegate_init_gap_is_joined_before_each_owned_store_closes(
    tmp_path,
) -> None:
    from opc.database.store import OPCStore

    registry = ActiveTaskRunRegistry()
    root_store = OPCStore(tmp_path / "root.db")
    delegate_store = OPCStore(tmp_path / "delegate.db")
    await root_store.initialize()
    await delegate_store.initialize()
    root = OPCEngine(
        project_id="project-root",
        store=root_store,
        owns_store=True,
        active_task_run_registry=registry,
        owns_active_task_run_registry=True,
    )
    delegate = OPCEngine(
        project_id="project-delegate",
        store=delegate_store,
        owns_store=True,
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )
    child = OPCEngine(
        project_id="project-delegate",
        store=delegate_store,
        owns_store=False,
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )
    root._project_engine_delegates["project-delegate"] = delegate
    events: list[str] = []
    original_delegate_close = delegate_store.close
    original_root_close = root_store.close

    async def delegate_close() -> None:
        events.append("delegate_store_close")
        await original_delegate_close()

    async def root_close() -> None:
        events.append("root_store_close")
        await original_root_close()

    delegate_store.close = delegate_close  # type: ignore[method-assign]
    root_store.close = root_close  # type: ignore[method-assign]
    entered_gap = asyncio.Event()

    async def initialize_gap() -> None:
        ingress = delegate._register_runtime_child(child)
        entered_gap.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert delegate_store.is_ready
            events.append("delegate_ingress_joined")
            raise
        finally:
            await child.shutdown()
            delegate._unregister_runtime_child(child, ingress)

    ingress_task = asyncio.create_task(initialize_gap())
    await entered_gap.wait()
    try:
        await root.shutdown()
        await root.shutdown()

        assert ingress_task.cancelled()
        assert delegate._runtime_child_engines == set()
        assert delegate._runtime_child_ingress_tasks == set()
        assert root._project_engine_delegates == {}
        assert events == [
            "delegate_ingress_joined",
            "delegate_store_close",
            "root_store_close",
        ]
        assert not delegate_store.is_ready
        assert not root_store.is_ready
    finally:
        if not ingress_task.done():
            ingress_task.cancel()
            await asyncio.gather(ingress_task, return_exceptions=True)
        if delegate_store.is_ready:
            await original_delegate_close()
        if root_store.is_ready:
            await original_root_close()


@_async_test
async def test_delegate_child_cleanup_failure_keeps_all_stores_open_for_retry() -> None:
    registry = ActiveTaskRunRegistry()

    class FakeStore:
        def __init__(self) -> None:
            self.is_ready = True
            self.close_count = 0

        async def close(self) -> None:
            self.close_count += 1
            self.is_ready = False

    root_store = FakeStore()
    delegate_store = FakeStore()
    root = OPCEngine(
        project_id="project-root",
        store=root_store,  # type: ignore[arg-type]
        owns_store=True,
        active_task_run_registry=registry,
        owns_active_task_run_registry=True,
    )
    delegate = OPCEngine(
        project_id="project-delegate",
        store=delegate_store,  # type: ignore[arg-type]
        owns_store=True,
        active_task_run_registry=registry,
        owns_active_task_run_registry=False,
    )
    class FailingChild:
        def __init__(self) -> None:
            self.prepare_active_company_runtimes_for_shutdown = AsyncMock(
                return_value=[]
            )
            self.shutdown = AsyncMock(
                side_effect=[RuntimeError("child cleanup failed"), None]
            )

    child = FailingChild()
    delegate._runtime_child_engines.add(child)  # type: ignore[arg-type]
    root._project_engine_delegates["project-delegate"] = delegate

    with pytest.raises(RuntimeError, match="child cleanup failed"):
        await root.shutdown()

    assert root_store.is_ready and delegate_store.is_ready
    assert not root._shutdown_complete and not delegate._shutdown_complete
    assert root._project_engine_delegates["project-delegate"] is delegate
    assert child in delegate._runtime_child_engines
    assert root_store.close_count == delegate_store.close_count == 0

    await root.shutdown()

    assert root._shutdown_complete and delegate._shutdown_complete
    assert root._project_engine_delegates == {}
    assert delegate._runtime_child_engines == set()
    assert root_store.close_count == delegate_store.close_count == 1


@_async_test
async def test_interaction_cleanup_failure_fails_closed_then_shutdown_retries() -> None:
    engine = OPCEngine(project_id="project-a")

    class FakeStore:
        is_ready = True
        close_count = 0

        async def close(self) -> None:
            self.close_count += 1
            self.is_ready = False

    store = FakeStore()
    engine.store = store  # type: ignore[assignment]
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(return_value=[])
    started = asyncio.Event()

    async def consumer() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("durable release failed") from exc

    task = asyncio.create_task(consumer())
    engine._interaction_consumer_tasks.add(task)
    task.add_done_callback(engine._interaction_consumer_finished)
    await started.wait()

    with pytest.raises(RuntimeError, match="durable release failed"):
        await engine.shutdown()

    assert store.is_ready
    assert store.close_count == 0
    assert not engine._shutdown_complete

    await engine.shutdown()

    assert engine._shutdown_complete
    assert store.close_count == 1
    assert not store.is_ready


@_async_test
async def test_shutdown_joins_exact_tool_operation_before_store_close() -> None:
    engine = OPCEngine(project_id="project-a")
    task = Task(id="worker", title="Worker", project_id="project-a")
    cleanup_observation: list[object] = []

    class FakeStore:
        is_ready = True

        async def claim_runtime_tool_continuation(self, **_kwargs):
            return SimpleNamespace(
                acquired=True,
                outcome="acquired",
                claim_id="continuation-claim",
                request_generation=1,
            )

        async def begin_runtime_tool_continuation(self, **_kwargs):
            return SimpleNamespace(acquired=True, outcome="started")

        async def get_task(self, _task_id):
            return task

        async def renew_runtime_tool_continuation(self, **_kwargs):
            return SimpleNamespace(acquired=True, outcome="renewed")

        async def finish_runtime_tool_continuation(self, **_kwargs):
            return SimpleNamespace(outcome="finished", request_generation=1)

        async def close(self) -> None:
            cleanup_observation.append("store_close")
            self.is_ready = False

    store = FakeStore()
    engine.store = store  # type: ignore[assignment]
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(return_value=[])
    run_started = asyncio.Event()

    async def run_once(_task: Task) -> str:
        run_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_observation.extend([store.is_ready, "operation_joined"])

    lease = InteractionDecisionLease(
        checkpoint=ExecutionCheckpoint(
            checkpoint_id="permission",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="tool_permission",
            task_id=task.id,
        ),
        decision={"option_id": "approve_once"},
        consumer_id="consumer",
        claim_id="interaction-claim",
    )
    consumer = asyncio.create_task(
        engine._run_runtime_tool_continuation_singleflight(
            task=task,
            runtime_session_id="runtime-1",
            interaction_lease=lease,
            run_once=run_once,
        )
    )
    engine._interaction_consumer_tasks.add(consumer)
    consumer.add_done_callback(engine._interaction_consumer_finished)
    await run_started.wait()

    await engine.shutdown()

    assert consumer.cancelled()
    assert cleanup_observation == [True, "operation_joined", "store_close"]
    assert not any(
        not pending.done()
        and (
            pending.get_name().startswith("tool-continuation:")
            or pending is consumer
        )
        for pending in asyncio.all_tasks()
    )


@_async_test
async def test_delegate_construction_gap_cannot_publish_after_root_shutdown() -> None:
    registry = ActiveTaskRunRegistry()
    root = OPCEngine(
        project_id="project-root",
        active_task_run_registry=registry,
        owns_active_task_run_registry=True,
    )
    initialize_entered = asyncio.Event()
    allow_initialize = asyncio.Event()
    delegate_closed = asyncio.Event()
    created: list[object] = []

    class FakeDelegate:
        def __init__(self, **_kwargs: object) -> None:
            self.store = SimpleNamespace(is_ready=True)
            self._shutdown_complete = False
            created.append(self)

        async def initialize(self) -> None:
            initialize_entered.set()
            await allow_initialize.wait()

        async def shutdown(self) -> None:
            self.store.is_ready = False
            self._shutdown_complete = True
            delegate_closed.set()

        async def prepare_active_company_runtimes_for_shutdown(self, **_kwargs):
            return []

    with patch("opc.engine.OPCEngine", FakeDelegate):
        construction = asyncio.create_task(root._get_project_delegate("project-b"))
        await initialize_entered.wait()
        shutdown = asyncio.create_task(root.shutdown())
        await shutdown

    assert construction.cancelled()
    assert delegate_closed.is_set()
    assert created and not created[0].store.is_ready
    assert root._project_engine_delegates == {}
    assert root._project_delegate_candidates == {}
    assert root._project_delegate_construction_tasks == set()
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        await root._get_project_delegate("project-c")


@_async_test
async def test_same_project_message_ingress_rejected_during_and_after_shutdown() -> None:
    engine = OPCEngine(project_id="project-a")
    handler = AsyncMock(return_value=SimpleNamespace(content="unexpected"))
    engine.message_bus.set_handler(handler)
    engine._initialized = True
    engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(return_value=[])
    close_entered = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingStore:
        is_ready = True

        async def close(self) -> None:
            close_entered.set()
            await allow_close.wait()
            self.is_ready = False

    engine.store = BlockingStore()  # type: ignore[assignment]
    shutting_down = asyncio.create_task(engine.shutdown())
    await close_entered.wait()

    with pytest.raises(ActiveTaskRunAdmissionClosed):
        await engine.process_message("late", project_id="project-a")
    with pytest.raises(ActiveTaskRunAdmissionClosed):
        await engine._get_project_delegate("project-a")
    handler.assert_not_awaited()

    allow_close.set()
    await shutting_down

    with pytest.raises(ActiveTaskRunAdmissionClosed):
        await engine.process_message("after", project_id="project-a")
    handler.assert_not_awaited()
