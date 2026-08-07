from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from unittest import mock
from unittest.mock import AsyncMock

from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.plugins.cli_board.services.actions import BoardActions


class _StubStore:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.checkpoints: list[ExecutionCheckpoint] = []
        self.resolved: list[tuple[str, str]] = []

    async def save_task(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def get_tasks(self, **_kw):
        return list(self.tasks.values())

    async def get_pending_checkpoints(self, **_kw):
        return [checkpoint for checkpoint in self.checkpoints if checkpoint.status == "pending"]

    async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
        self.resolved.append((checkpoint_id, status))
        for checkpoint in self.checkpoints:
            if checkpoint.checkpoint_id == checkpoint_id:
                checkpoint.status = status


class _StubMemory:
    def __init__(self) -> None:
        self.ensure_session = AsyncMock()


class _StubEngine:
    def __init__(self) -> None:
        self.store = _StubStore()
        self.memory = _StubMemory()
        self.process_message = AsyncMock(return_value="ok")


class _StubFacade:
    def __init__(self, engine: _StubEngine) -> None:
        self.project_id = "demo"
        self._engine = engine

    async def ensure_ready(self):
        return self._engine


class _StubServiceResult:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.events: list = []


class _StubSessionService:
    """In-memory stand-in for the office SessionService seam.

    Mirrors the slice of the production contract that ``BoardActions``
    relies on: ``create`` persists a session-backed placeholder Task and
    ensures its memory session, ``send`` routes execution to
    ``engine.process_message`` with the origin task's session, and ``stop``
    cancels the target task row.  Every call is recorded for assertions.
    """

    def __init__(self, engine: _StubEngine, project_id: str) -> None:
        self._engine = engine
        self._project_id = project_id
        self.create_calls: list[dict] = []
        self.send_calls: list[dict] = []
        self.stop_calls: list[dict] = []

    async def create(self, **kwargs) -> _StubServiceResult:
        self.create_calls.append(dict(kwargs))
        project_id = kwargs.get("project_id", self._project_id)
        title = str(kwargs.get("title", "") or "New Chat")
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        await self._engine.memory.ensure_session(
            session_id=session_id,
            project_id=project_id,
            title=title,
            mode="primary",
            metadata={"interface": kwargs.get("interface", "office_ui")},
        )
        await self._engine.store.save_task(
            Task(
                id=task_id,
                title=title,
                description=str(kwargs.get("description", "") or ""),
                project_id=project_id,
                session_id=session_id,
                metadata={"exec_mode": kwargs.get("exec_mode") or "task"},
            )
        )
        return _StubServiceResult({"project_id": project_id, "task_id": task_id, "session_id": session_id})

    async def send(self, **kwargs) -> _StubServiceResult:
        self.send_calls.append(dict(kwargs))
        task = await self._engine.store.get_task(str(kwargs.get("task_id", "") or ""))
        if task is None:
            raise ValueError(f"task_not_found: {kwargs.get('task_id')}")
        response = await self._engine.process_message(
            str(kwargs.get("content", "") or ""),
            project_id=kwargs.get("project_id", self._project_id),
            session_id=str(task.session_id or ""),
            mode=kwargs.get("mode", "task"),
            origin_task_id=task.id,
        )
        return _StubServiceResult(
            {
                "project_id": kwargs.get("project_id", self._project_id),
                "task_id": task.id,
                "session_id": str(task.session_id or ""),
                "response": response,
            }
        )

    async def stop(self, **kwargs) -> _StubServiceResult:
        self.stop_calls.append(dict(kwargs))
        task = await self._engine.store.get_task(str(kwargs.get("task_id", "") or ""))
        if task is None:
            raise ValueError(f"target_not_found: {kwargs.get('task_id')}")
        task.status = TaskStatus.CANCELLED
        await self._engine.store.save_task(task)
        return _StubServiceResult(
            {
                "project_id": kwargs.get("project_id", self._project_id),
                "task_id": task.id,
                "status": "cancelled",
            }
        )


class _StubOfficeServices:
    def __init__(self, engine: _StubEngine, project_id: str = "demo") -> None:
        self.session = _StubSessionService(engine, project_id)


class BoardActionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Safety net: even if a code path slipped past the factory patch it
        # must never touch the real OPC home.
        tmp = tempfile.TemporaryDirectory(prefix="opc-cli-board-actions-test-")
        self.addCleanup(tmp.cleanup)
        env_patcher = mock.patch.dict(os.environ, {"OPC_HOME": tmp.name})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _make_actions(self, engine: _StubEngine) -> tuple[BoardActions, _StubOfficeServices]:
        """Patch the OfficeServiceFactory seam so BoardActions uses our stubs.

        ``BoardActions._run_office_service`` builds a real
        ``OfficeServiceFactory`` (and with it a real engine + ui_state.db) per
        operation; the tests replace that seam with an async context manager
        yielding stub services bound to the stub engine.
        """
        services = _StubOfficeServices(engine)

        class _StubFactory:
            def __init__(self, **_kwargs) -> None:
                pass

            async def __aenter__(self) -> _StubOfficeServices:
                return services

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

        patcher = mock.patch(
            "opc.plugins.cli_board.services.actions.OfficeServiceFactory",
            _StubFactory,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return BoardActions(_StubFacade(engine), project_id="demo"), services

    async def test_create_task_creates_session_backed_placeholder(self) -> None:
        engine = _StubEngine()
        actions, services = self._make_actions(engine)

        task = await actions.create_task(title="Draft feature", description="Initial plan")

        self.assertIn(task.id, engine.store.tasks)
        stored = engine.store.tasks[task.id]
        self.assertEqual(stored.title, "Draft feature")
        self.assertEqual(stored.description, "Initial plan")
        self.assertTrue(stored.session_id)
        engine.memory.ensure_session.assert_awaited()
        self.assertEqual(len(services.session.create_calls), 1)
        call = services.session.create_calls[0]
        self.assertEqual(call["project_id"], "demo")
        self.assertEqual(call["title"], "Draft feature")
        self.assertEqual(call["description"], "Initial plan")
        self.assertEqual(call["exec_mode"], "task")
        self.assertEqual(call["interface"], "cli_board")

    async def test_send_session_message_routes_through_origin_task(self) -> None:
        engine = _StubEngine()
        task = Task(
            id="task-1",
            title="Feature task",
            description="Implement the feature",
            status=TaskStatus.PENDING,
            session_id="session-1",
            project_id="demo",
        )
        await engine.store.save_task(task)
        actions, services = self._make_actions(engine)

        response = await actions.send_session_message("task-1", "please continue")

        self.assertEqual(response, "ok")
        self.assertEqual(len(services.session.send_calls), 1)
        call = services.session.send_calls[0]
        self.assertEqual(call["project_id"], "demo")
        self.assertEqual(call["task_id"], "task-1")
        self.assertEqual(call["content"], "please continue")
        self.assertEqual(call["mode"], "task")
        engine.process_message.assert_awaited_once()
        kwargs = engine.process_message.await_args.kwargs
        self.assertEqual(kwargs["session_id"], "session-1")
        self.assertEqual(kwargs["origin_task_id"], "task-1")

    async def test_cancel_task_marks_related_tasks_and_checkpoints_cancelled(self) -> None:
        engine = _StubEngine()
        started = asyncio.Event()
        release = asyncio.Event()  # never set; the run only ends via cancellation

        async def _blocked_process_message(*_args, **_kwargs):
            started.set()
            await release.wait()
            return "unreachable"

        engine.process_message = AsyncMock(side_effect=_blocked_process_message)
        root = Task(
            id="root",
            title="Root task",
            description="Run runtime",
            status=TaskStatus.RUNNING,
            session_id="session-root",
            project_id="demo",
        )
        linked = Task(
            id="child",
            title="Child task",
            description="Background child",
            status=TaskStatus.RUNNING,
            session_id="session-child",
            project_id="demo",
            metadata={"origin_task_id": "root"},
        )
        await engine.store.save_task(root)
        await engine.store.save_task(linked)
        engine.store.checkpoints.append(
            ExecutionCheckpoint(
                checkpoint_id="cp-root",
                project_id="demo",
                session_id="session-root",
                task_id="root",
            )
        )
        actions, services = self._make_actions(engine)

        background = asyncio.create_task(actions.send_session_message("root", "keep going"))
        await asyncio.wait_for(started.wait(), timeout=5)

        await actions.cancel_task("root")
        with self.assertRaises(asyncio.CancelledError):
            await background

        self.assertEqual(engine.store.tasks["root"].status, TaskStatus.CANCELLED)
        self.assertEqual(engine.store.tasks["child"].status, TaskStatus.CANCELLED)
        self.assertEqual(engine.store.resolved, [("cp-root", "cancelled")])
        self.assertEqual(len(services.session.stop_calls), 1)
        self.assertEqual(services.session.stop_calls[0]["task_id"], "root")
