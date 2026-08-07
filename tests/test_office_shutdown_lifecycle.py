from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.active_task_runs import ActiveTaskRunRegistry
from opc.core.models import ExecutionCheckpoint, Task
from opc.plugins.office_ui.ws_handler import WSHandler


def _make_handler(root_engine: SimpleNamespace | None = None, **overrides: object) -> WSHandler:
    """Build a real WSHandler over stub dependencies.

    Constructed through ``__init__`` (rather than ``__new__`` plus a
    hand-copied attribute list) so every attribute ``shutdown()`` touches is
    initialized by the constructor itself; each test only overrides what its
    scenario controls. ``__init__`` is side-effect free for stub inputs: it
    assigns state, builds Dispatcher/OfficeServices holders, and wires engine
    callbacks via defensive setattr/getattr.
    """
    engine = root_engine if root_engine is not None else SimpleNamespace()
    if not hasattr(engine, "project_id"):
        engine.project_id = "default"
    handler = WSHandler(
        engine=engine,
        agent_store=SimpleNamespace(),
        chat_store=None,
        event_adapter=SimpleNamespace(),
    )
    for name, value in overrides.items():
        setattr(handler, name, value)
    return handler


def test_ws_shutdown_checkpoints_before_cancelling_and_awaiting_sessions() -> None:
    async def scenario() -> None:
        events: list[str] = []
        started = asyncio.Event()

        async def execution() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("execution_finally")

        async def prepare() -> list[dict]:
            events.append("checkpoint")
            assert not execution_task.done()
            return []

        execution_task = asyncio.create_task(execution())
        await started.wait()

        handler = _make_handler(
            SimpleNamespace(prepare_active_company_runtimes_for_shutdown=prepare),
            _background_tasks={execution_task},
            _task_bg_context={execution_task: {"task_id": "runtime-task"}},
            _task_bg_map={"runtime-task": {execution_task}},
        )

        await handler.shutdown(timeout=1.0)

        assert events == ["checkpoint", "execution_finally"]
        assert execution_task.done()

    asyncio.run(scenario())


def test_ws_shutdown_checkpoint_failure_does_not_cancel_execution_or_close_the_gap() -> None:
    async def scenario() -> None:
        released = asyncio.Event()

        async def execution() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                released.set()

        execution_task = asyncio.create_task(execution())
        await asyncio.sleep(0)

        handler = _make_handler(
            SimpleNamespace(
                prepare_active_company_runtimes_for_shutdown=AsyncMock(
                    side_effect=RuntimeError("checkpoint unavailable")
                ),
            ),
            _background_tasks={execution_task},
            _task_bg_context={execution_task: {"task_id": "runtime-task"}},
            _task_bg_map={"runtime-task": {execution_task}},
        )

        try:
            await handler.shutdown(timeout=1.0)
        except RuntimeError as exc:
            assert str(exc) == "checkpoint unavailable"
        else:
            raise AssertionError("shutdown must fail closed when checkpointing fails")

        assert not released.is_set()
        assert not execution_task.done()
        execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)

    asyncio.run(scenario())


def test_ws_shutdown_rejects_background_work_scheduled_by_late_ingress() -> None:
    async def scenario() -> None:
        entered = False

        async def late_work() -> None:
            nonlocal entered
            entered = True

        handler = _make_handler(_shutting_down=True)
        task = handler._track(late_work())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert entered is False
        assert task not in handler._background_tasks

    asyncio.run(scenario())


def test_ws_shutdown_drains_queued_duplicate_handoff_before_checkpointing() -> None:
    async def scenario() -> None:
        registry = ActiveTaskRunRegistry()
        runtime_lock = asyncio.Lock()
        execution_registered = asyncio.Event()
        prepare_called = asyncio.Event()
        execution_released = asyncio.Event()

        async def prepare() -> list[dict]:
            assert registry.is_active("project-a", "runtime-task")
            assert registry.pending_handoff_count == 0
            prepare_called.set()
            return []

        root_engine = SimpleNamespace(
            _active_task_run_registry=registry,
            prepare_active_company_runtimes_for_shutdown=prepare,
        )
        handler = _make_handler(root_engine)

        async def execution() -> None:
            async with runtime_lock:
                attempt_token = registry.register("project-a", "runtime-task")
                execution_registered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    registry.unregister("project-a", "runtime-task", attempt_token)
                    execution_released.set()

        async def queued_duplicate() -> None:
            async with runtime_lock:
                attempt_token = registry.register("project-a", "runtime-task")
                try:
                    await asyncio.Event().wait()
                finally:
                    registry.unregister("project-a", "runtime-task", attempt_token)

        first_handoff = registry.reserve_handoff()
        with registry.bind_handoff(first_handoff):
            first = handler._track_session(
                "runtime-task",
                execution(),
                project_id="project-a",
                engine=root_engine,
            )
        registry.release_handoff(first_handoff)
        await execution_registered.wait()

        second_handoff = registry.reserve_handoff()
        with registry.bind_handoff(second_handoff):
            second = handler._track_session(
                "runtime-task",
                queued_duplicate(),
                project_id="project-a",
                engine=root_engine,
            )
        registry.release_handoff(second_handoff)
        await asyncio.sleep(0)
        assert registry.pending_handoff_count == 1

        await asyncio.wait_for(handler.shutdown(timeout=1.0), timeout=1.0)

        assert prepare_called.is_set()
        assert execution_released.is_set()
        assert first.cancelled()
        assert second.cancelled()
        assert registry.pending_handoff_count == 0
        assert not registry.is_active("project-a", "runtime-task")

    asyncio.run(scenario())


def test_ws_shutdown_fails_closed_while_execution_cleanup_is_still_running() -> None:
    async def scenario() -> None:
        cancellation_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def execution() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_started.set()
                await allow_cleanup.wait()

        execution_task = asyncio.create_task(execution())
        await asyncio.sleep(0)

        handler = _make_handler(
            SimpleNamespace(
                prepare_active_company_runtimes_for_shutdown=AsyncMock(return_value=[]),
            ),
            _background_tasks={execution_task},
            _task_bg_context={
                execution_task: {"task_id": "runtime-task", "execution_handoff": True}
            },
            _task_bg_map={"runtime-task": {execution_task}},
        )

        try:
            await handler.shutdown(timeout=0.01)
        except RuntimeError as exc:
            assert "execution task(s)" in str(exc)
        else:
            raise AssertionError("shutdown must not close resources before execution cleanup")

        assert cancellation_started.is_set()
        assert not execution_task.done()
        allow_cleanup.set()
        await execution_task

    asyncio.run(scenario())


def test_ws_shutdown_cancels_execution_before_waiting_for_client_close() -> None:
    async def scenario() -> None:
        execution_released = asyncio.Event()
        close_entered = asyncio.Event()
        allow_close = asyncio.Event()

        async def execution() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                execution_released.set()

        class BlockingWebSocket:
            async def close(self, **_kwargs: object) -> None:
                close_entered.set()
                await allow_close.wait()

        execution_task = asyncio.create_task(execution())
        await asyncio.sleep(0)
        client = BlockingWebSocket()
        handler = _make_handler(
            SimpleNamespace(
                prepare_active_company_runtimes_for_shutdown=AsyncMock(return_value=[]),
            ),
            _clients={client},
            _background_tasks={execution_task},
            _task_bg_context={execution_task: {"task_id": "runtime-task"}},
            _task_bg_map={"runtime-task": {execution_task}},
        )

        shutdown_task = asyncio.create_task(handler.shutdown(timeout=1.0))
        # close_entered only fires from inside shutdown's client-close loop.
        # Bound the wait so a shutdown crash before that loop fails the test
        # instead of wedging the whole suite.
        await asyncio.wait_for(close_entered.wait(), timeout=5.0)

        assert execution_released.is_set()
        assert execution_task.done()
        allow_close.set()
        await shutdown_task

    asyncio.run(scenario())


def test_duplicate_resume_does_not_leave_shutdown_handoff_barrier_queued() -> None:
    async def scenario() -> None:
        registry = ActiveTaskRunRegistry()
        execution_started = asyncio.Event()
        execution_released = asyncio.Event()

        async def prepare() -> list[dict]:
            assert registry.is_active("project-a", "runtime-task")
            return []

        root_engine = SimpleNamespace(
            project_id="project-a",
            _active_task_run_registry=registry,
            prepare_active_company_runtimes_for_shutdown=prepare,
        )
        handler = _make_handler(root_engine)

        checkpoint = ExecutionCheckpoint(
            checkpoint_id="checkpoint-1",
            project_id="project-a",
            session_id="runtime-session",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
        )
        task = Task(
            id="ui-task",
            title="Company chat",
            project_id="project-a",
            session_id="runtime-session",
            metadata={"exec_mode": "company"},
        )
        target = {
            "runtime_session_id": "runtime-session",
            "checkpoint": checkpoint,
        }
        handler._resolve_company_runtime_target = AsyncMock(return_value=target)

        async def fake_resume(**_kwargs: object) -> None:
            attempt = registry.register("project-a", "runtime-task")
            execution_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                registry.unregister("project-a", "runtime-task", attempt)
                execution_released.set()

        handler._process_company_suspend_reply = fake_resume

        async def route_once() -> bool:
            handoff = registry.reserve_handoff()
            try:
                with registry.bind_handoff(handoff):
                    return await handler._route_company_suspend_reply_if_pending(
                        task_id=task.id,
                        content="continue",
                        session_id=task.session_id,
                        task=task,
                        attachment_refs=None,
                        message_metadata=None,
                        user_message_id=None,
                        user_message_created_at=None,
                        run_engine=root_engine,
                        run_project_id="project-a",
                    )
            finally:
                registry.release_handoff(handoff)

        assert await route_once() is True
        await execution_started.wait()
        assert await route_once() is True
        assert registry.pending_handoff_count == 0
        assert len(handler._background_tasks) == 1

        await asyncio.wait_for(handler.shutdown(timeout=1.0), timeout=1.0)

        assert execution_released.is_set()
        assert registry.pending_handoff_count == 0

    asyncio.run(scenario())
