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
        self.patched: list[tuple[str, str, tuple[str, ...]]] = []

    async def save_task(self, task: Task) -> None:
        self.tasks[task.id] = task

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def get_tasks(self, **_kw):
        return list(self.tasks.values())

    async def get_pending_checkpoints(self, **_kw):
        return [checkpoint for checkpoint in self.checkpoints if checkpoint.status == "pending"]

    async def patch_execution_checkpoint_payload(
        self,
        checkpoint_id: str,
        *,
        project_id: str,
        checkpoint_type: str,
        expected_statuses,
        payload_patch,
        status: str,
    ):
        expected = tuple(expected_statuses)
        self.patched.append((checkpoint_id, status, expected))
        for checkpoint in self.checkpoints:
            if (
                checkpoint.checkpoint_id == checkpoint_id
                and checkpoint.project_id == project_id
                and checkpoint.checkpoint_type == checkpoint_type
                and checkpoint.status in expected
            ):
                checkpoint.status = status
                return checkpoint, True
        return None, False


class _StubMemory:
    def __init__(self) -> None:
        self.ensure_session = AsyncMock()


class _StubInteractionCoordinator:
    def __init__(self, store: _StubStore) -> None:
        self.store = store

    async def close_pending_owner_checkpoint(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        status: str,
        payload_patch,
    ):
        return await self.store.patch_execution_checkpoint_payload(
            checkpoint_id,
            project_id="demo",
            checkpoint_type=checkpoint_type,
            expected_statuses=["pending"],
            payload_patch=payload_patch,
            status=status,
        )


class _StubEngine:
    def __init__(self) -> None:
        self.store = _StubStore()
        self.memory = _StubMemory()
        self.interaction_coordinator = _StubInteractionCoordinator(self.store)
        self.process_message = AsyncMock(return_value="ok")
        self.submissions: list[dict] = []

    async def can_answer_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        requester_task_id: str = "",
        requester_session_id: str | None = None,
        requester_actor: dict | None = None,
    ) -> bool:
        ownership = dict(
            dict((checkpoint.payload or {}).get("interaction", {}) or {}).get(
                "ownership", {}
            )
            or {}
        )
        expected_task_id = str(
            ownership.get("ui_anchor_task_id") or checkpoint.task_id or ""
        )
        expected_session_id = str(
            ownership.get("ui_anchor_session_id")
            or ownership.get("root_session_id")
            or checkpoint.session_id
            or ""
        )
        if expected_task_id or expected_session_id:
            return bool(
                requester_task_id == expected_task_id
                and str(requester_session_id or "") == expected_session_id
            )
        return bool(
            not expected_task_id
            and not expected_session_id
            and dict(requester_actor or {}).get("capability") == "board-capability"
        )

    @staticmethod
    def issue_project_owner_actor(*, interface: str) -> dict:
        return {
            "kind": "project_owner",
            "project_id": "demo",
            "interface": interface,
            "capability": "board-capability",
        }

    @staticmethod
    def build_compatibility_checkpoint_decision(
        checkpoint: ExecutionCheckpoint,
        message: str,
        _metadata,
    ) -> dict:
        token = str(message or "").strip().lower()
        if checkpoint.checkpoint_type in {"tool_permission", "action_permission"}:
            return {
                "option_id": "approve_once" if token == "approve" else token,
                "text": message,
            }
        return {"option_id": token, "text": message}

    async def submit_checkpoint_decision(self, **kwargs):
        self.submissions.append(dict(kwargs))
        checkpoint = next(
            (
                candidate
                for candidate in self.store.checkpoints
                if candidate.checkpoint_id == kwargs.get("checkpoint_id")
                and candidate.checkpoint_type == kwargs.get("checkpoint_type")
            ),
            None,
        )
        if checkpoint is None:
            return {"accepted": False, "reason": "checkpoint_not_found"}
        if checkpoint.status != "pending":
            return {"accepted": False, "reason": "invalid_state"}
        checkpoint.status = "answered"
        return {
            "accepted": True,
            "reason": "",
            "status": "answered",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
        }


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

    async def test_approve_checkpoint_bypasses_held_task_lock(self) -> None:
        engine = _StubEngine()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _blocked_process_message(*_args, **_kwargs):
            started.set()
            await release.wait()
            return "ok"

        engine.process_message = AsyncMock(side_effect=_blocked_process_message)
        task = Task(
            id="root",
            title="Root task",
            status=TaskStatus.RUNNING,
            session_id="session-root",
            project_id="demo",
        )
        await engine.store.save_task(task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-tool",
            project_id="demo",
            session_id="session-root",
            task_id="root",
            checkpoint_type="company_work_item_gate",
            payload={
                "interaction": {
                    "kind": "company_work_item_gate",
                    "options": [
                        {"id": "approve", "label": "Approve"},
                        {"id": "deny", "label": "Deny"},
                    ],
                    "ownership": {
                        "ui_anchor_task_id": "root",
                        "ui_anchor_session_id": "session-root",
                        "waiting_task_id": "root",
                    },
                }
            },
        )
        engine.store.checkpoints.append(checkpoint)
        actions, services = self._make_actions(engine)

        running = asyncio.create_task(actions.send_session_message("root", "run"))
        await asyncio.wait_for(started.wait(), timeout=5)
        response = await asyncio.wait_for(
            actions.approve_checkpoint("root", approved=True),
            timeout=1,
        )

        self.assertEqual(response, "Checkpoint response accepted.")
        self.assertEqual(checkpoint.status, "answered")
        self.assertEqual(len(engine.submissions), 1)
        self.assertEqual(
            engine.submissions[0]["decision"]["option_id"],
            "approve",
        )
        self.assertEqual(len(services.session.send_calls), 1)

        release.set()
        self.assertEqual(await running, "ok")

    async def test_work_item_checkpoint_uses_durable_ui_anchor_actor(self) -> None:
        engine = _StubEngine()
        await engine.store.save_task(
            Task(
                id="root",
                title="Company root",
                session_id="session-root",
                project_id="demo",
            )
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-gate",
            project_id="demo",
            session_id="session-child",
            task_id="child",
            checkpoint_type="company_work_item_gate",
            payload={
                "interaction": {
                    "kind": "company_work_item_gate",
                    "ownership": {
                        "work_item_id": "work-item-1",
                        "ui_anchor_task_id": "root",
                        "ui_anchor_session_id": "session-root",
                        "waiting_task_id": "child",
                    },
                }
            },
        )
        engine.store.checkpoints.append(checkpoint)
        actions, services = self._make_actions(engine)

        await actions.approve_checkpoint("work-item-1", approved=True)

        self.assertEqual(checkpoint.status, "answered")
        self.assertEqual(engine.submissions[0]["requester_task_id"], "root")
        self.assertEqual(
            engine.submissions[0]["requester_session_id"],
            "session-root",
        )
        self.assertEqual(services.session.send_calls, [])

    async def test_root_session_actor_never_falls_back_to_waiting_child(self) -> None:
        engine = _StubEngine()
        await engine.store.save_task(
            Task(
                id="waiting-child",
                title="Native child",
                session_id="session-child",
                parent_session_id="session-root",
                project_id="demo",
            )
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-root-session-owner",
            project_id="demo",
            session_id="session-child",
            task_id="waiting-child",
            checkpoint_type="tool_permission",
            payload={
                "interaction": {
                    "kind": "tool_permission",
                    "ownership": {
                        "ui_anchor_task_id": "",
                        "ui_anchor_session_id": "session-root",
                        "root_session_id": "session-root",
                        "waiting_task_id": "waiting-child",
                    },
                }
            },
        )
        engine.store.checkpoints.append(checkpoint)
        engine.can_answer_checkpoint = AsyncMock(return_value=True)
        actions, _services = self._make_actions(engine)

        await actions.approve_checkpoint("waiting-child", approved=False)

        engine.can_answer_checkpoint.assert_awaited_once_with(
            checkpoint,
            requester_task_id="",
            requester_session_id="session-root",
            requester_actor=None,
        )
        self.assertEqual(engine.submissions[0]["requester_task_id"], "")
        self.assertEqual(
            engine.submissions[0]["requester_session_id"],
            "session-root",
        )

    async def test_cli_service_cannot_approve_exact_permission_but_can_deny(self) -> None:
        engine = _StubEngine()
        await engine.store.save_task(
            Task(
                id="root",
                title="Root task",
                session_id="session-root",
                project_id="demo",
            )
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-exact-cli",
            project_id="demo",
            session_id="session-root",
            task_id="root",
            checkpoint_type="tool_permission",
            payload={
                "interaction": {
                    "kind": "tool_permission",
                    "prompt": "  \nif True:\n    print('<tag>')\n\n" + "x" * 4_000,
                    "options": [
                        {"id": "approve_once", "label": "Approve once"},
                        {"id": "deny", "label": "Deny"},
                    ],
                    "ownership": {
                        "ui_anchor_task_id": "root",
                        "ui_anchor_session_id": "session-root",
                        "waiting_task_id": "root",
                    },
                }
            },
        )
        engine.store.checkpoints.append(checkpoint)
        actions, _services = self._make_actions(engine)

        with self.assertRaises(PermissionError):
            await actions.approve_checkpoint("root", approved=True)
        self.assertEqual(engine.submissions, [])
        self.assertEqual(checkpoint.status, "pending")

        response = await actions.approve_checkpoint("root", approved=False)
        self.assertEqual(response, "Checkpoint response accepted.")
        self.assertEqual(
            engine.submissions[0]["decision"]["option_id"],
            "deny",
        )

    async def test_cleanup_pending_cas_preserves_concurrent_answer(self) -> None:
        engine = _StubEngine()
        task = Task(
            id="root",
            title="Root task",
            session_id="session-root",
            project_id="demo",
        )
        await engine.store.save_task(task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-race",
            project_id="demo",
            session_id="session-root",
            task_id="root",
            checkpoint_type="tool_permission",
        )
        engine.store.checkpoints.append(checkpoint)
        actions, _services = self._make_actions(engine)
        entered_cas = asyncio.Event()
        release_cas = asyncio.Event()
        real_close = engine.interaction_coordinator.close_pending_owner_checkpoint

        async def _blocked_close(*args, **kwargs):
            entered_cas.set()
            await release_cas.wait()
            return await real_close(*args, **kwargs)

        engine.interaction_coordinator.close_pending_owner_checkpoint = _blocked_close
        cleanup = asyncio.create_task(
            actions._resolve_related_checkpoints("root", status="cancelled")
        )
        await asyncio.wait_for(entered_cas.wait(), timeout=5)
        checkpoint.status = "answered"
        release_cas.set()
        await cleanup

        self.assertEqual(checkpoint.status, "answered")
        self.assertEqual(
            engine.store.patched,
            [("cp-race", "cancelled", ("pending",))],
        )

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
        self.assertEqual(
            engine.store.patched,
            [("cp-root", "cancelled", ("pending",))],
        )
        self.assertEqual(len(services.session.stop_calls), 1)
        self.assertEqual(services.session.stop_calls[0]["task_id"], "root")
