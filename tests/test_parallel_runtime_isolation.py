from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.models import CompanyMemberSession, ExternalSession, SystemMessage, Task, TaskResult, TaskStatus
from opc.core.config import ExternalAgentConfig
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer3_agent.adapters.claude_code import ClaudeCodeAdapter
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor, CompanyWorkItemRuntimePlan


class _MemoryStore:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.saved: list[Task] = []
        self.renewed: list[str] = []

    @property
    def is_ready(self) -> bool:
        return True

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def save_task(self, task: Task) -> None:
        self.tasks[task.id] = task
        self.saved.append(task)

    async def get_session_transcript(self, session_id: str) -> list[dict]:
        return []

    async def renew_task_lock(self, task_id: str) -> bool:
        self.renewed.append(task_id)
        return True


def _ui_chat_store() -> SimpleNamespace:
    return SimpleNamespace(
        create_session_channel=AsyncMock(return_value={}),
        backfill_messages=AsyncMock(return_value=[]),
        insert_message=AsyncMock(return_value={"message_id": "msg", "created_at": 1.0}),
        append_progress=AsyncMock(),
    )


def _ui_event_adapter() -> SimpleNamespace:
    return SimpleNamespace(
        translate=MagicMock(return_value=[]),
        parse_progress=MagicMock(return_value=[]),
        get_task_display_num=MagicMock(return_value=1),
        _resolve_agent_from_task=MagicMock(return_value=None),
        _resolve_role_to_agent=MagicMock(return_value=""),
        _task_display_map={},
        _task_display_counter=0,
        task_display_counter=1,
    )


def _ui_engine(project_id: str, store: _MemoryStore) -> SimpleNamespace:
    return SimpleNamespace(
        project_id=project_id,
        opc_home=Path(tempfile.gettempdir()),
        store=store,
        memory=None,
        escalation=None,
        company_executor=None,
        reorg_manager=None,
        event_bus=None,
        process_message=AsyncMock(return_value="ok"),
        get_latest_pending_checkpoint_for_session=AsyncMock(return_value=None),
        _ensure_attachment_store=MagicMock(),
    )


class _FakeWS:
    def __init__(self) -> None:
        self.sent_json: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)


class _FakeClient:
    def __init__(self) -> None:
        self.sent_str: list[str] = []

    async def send_str(self, payload: str) -> None:
        self.sent_str.append(payload)


def _async_test(func):
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


def _run_task(run_id: str) -> Task:
    return Task(
        id=f"task-{run_id}",
        title=f"Run {run_id}",
        description="simple parallel isolation task",
        project_id="proj",
        assigned_to="executor",
        status=TaskStatus.PENDING,
        metadata={
            "delegation_run_id": run_id,
            "parent_session_id": f"session-{run_id}",
            "progress_log": [],
        },
        parent_session_id=f"session-{run_id}",
        session_id=f"session-{run_id}",
    )


@_async_test
async def test_company_executor_parallel_runs_keep_context_state_isolated() -> None:
    async def execute_task(task: Task) -> TaskResult:
        task.status = TaskStatus.DONE
        task.result = {"content": f"executed {task.id}"}
        return TaskResult(status=TaskStatus.DONE, content=task.result["content"])

    executor = CompanyWorkItemExecutor(
        org_engine=SimpleNamespace(),
        communication=SimpleNamespace(
            refresh_waiting_tasks=AsyncMock(return_value=[]),
            detect_deadlocks=AsyncMock(return_value=[]),
        ),
        approval_engine=SimpleNamespace(),
        memory=None,
        execute_task=execute_task,
        save_task=AsyncMock(),
    )

    def active_run_id() -> str:
        assert executor._active_tasks
        run_ids = {
            str((task.metadata or {}).get("delegation_run_id", "") or "")
            for task in executor._active_tasks
        }
        assert len(run_ids) == 1
        return next(iter(run_ids))

    async def fake_bootstrap(tasks: list[Task]) -> None:
        run_id = str((tasks[0].metadata or {}).get("delegation_run_id", "") or "")
        executor.runtime.member_sessions = {
            f"member-{run_id}": CompanyMemberSession(
                member_session_id=f"member-{run_id}",
                role_id="executor",
                seat_id=f"seat-{run_id}",
                status="idle",
                resident_status="idle",
                metadata={"run_id": run_id},
            )
        }
        executor.runtime._claimed_task_ids = set()
        executor.runtime._claimed_work_item_ids = set()

    claimed_once: set[str] = set()

    async def fake_claim_runnable_tasks(tasks: list[Task], work_items=None):
        run_id = active_run_id()
        assert run_id == str((tasks[0].metadata or {}).get("delegation_run_id", "") or "")
        if run_id in claimed_once:
            return []
        claimed_once.add(run_id)
        task = tasks[0]
        session = next(iter(executor.runtime.member_sessions.values()))
        executor.runtime._claimed_task_ids.add(task.id)
        session.status = session.resident_status = "running"
        session.current_task_id = task.id
        return [(session, task)]

    both_running = asyncio.Event()
    running_runs: set[str] = set()
    observed_max_parallel = 0

    async def fake_run_claimed_work_item(member_session: CompanyMemberSession, task: Task, task_by_projection_id: dict):
        nonlocal observed_max_parallel
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "")
        running_runs.add(run_id)
        observed_max_parallel = max(observed_max_parallel, len(running_runs))
        if len(running_runs) == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), timeout=2)
        assert active_run_id() == run_id
        assert executor.runtime._claimed_task_ids == {task.id}
        result = await execute_task(task)
        executor.runtime._claimed_task_ids.discard(task.id)
        member_session.status = member_session.resident_status = "idle"
        member_session.current_task_id = ""
        running_runs.discard(run_id)
        return result

    executor.runtime.bootstrap = AsyncMock(side_effect=fake_bootstrap)
    executor.runtime.refresh_inbox_state = AsyncMock()
    executor.runtime.enqueue_runnable_work_items = lambda *args, **kwargs: None
    executor.runtime.enqueue_runnable_tasks = lambda *args, **kwargs: None
    executor.runtime.claim_runnable_tasks = AsyncMock(side_effect=fake_claim_runnable_tasks)
    executor._load_delegation_work_items = AsyncMock(return_value=[])
    executor._refresh_ready_work_items = AsyncMock(side_effect=lambda items, tasks=None: items)
    executor._materialize_work_item_tasks = AsyncMock(side_effect=lambda tasks, work_items: tasks)
    executor._queue_multi_team_response_tasks = AsyncMock(side_effect=lambda tasks, work_items: (tasks, work_items))
    executor._reconcile_role_serial_queues = AsyncMock(side_effect=lambda work_items: work_items)
    executor._sync_task_projection_from_work_items = lambda tasks, work_items: None
    executor._diagnose_work_item_runtime_projection_issues = AsyncMock()
    executor._rehydrate_parked_member_sessions = lambda work_items: None
    executor._schedule_kanban_notification = lambda: None
    executor._summarize_multi_team_org_results = lambda tasks: f"done:{tasks[0].metadata['delegation_run_id']}"
    executor._run_claimed_work_item = AsyncMock(side_effect=fake_run_claimed_work_item)

    task_a = _run_task("run-a")
    task_b = _run_task("run-b")
    results = await asyncio.gather(
        executor.execute(CompanyWorkItemRuntimePlan(profile="corporate"), [task_a]),
        executor.execute(CompanyWorkItemRuntimePlan(profile="corporate"), [task_b]),
    )

    assert set(results) == {"done:run-a", "done:run-b"}
    assert observed_max_parallel == 2
    assert task_a.status == TaskStatus.DONE
    assert task_b.status == TaskStatus.DONE
    assert executor.runtime._claimed_task_ids == set()


@_async_test
async def test_initialized_engine_delegates_cross_project_message_without_mutating_root() -> None:
    engine = OPCEngine(project_id="project-a")
    engine._initialized = True
    engine._refresh_runtime_config_from_disk = AsyncMock()
    engine.message_bus.process_single = AsyncMock(
        return_value=SystemMessage(
            channel="cli",
            user_id="owner",
            session_id="same-project",
            content="same",
        )
    )
    delegate = SimpleNamespace(process_message=AsyncMock(return_value="delegated"))
    engine._get_project_delegate = AsyncMock(return_value=delegate)

    result = await engine.process_message(
        "hello",
        project_id="project-b",
        session_id="session-b",
        preferred_agent="claude_code",
    )

    assert result == "delegated"
    assert engine.project_id == "project-a"
    engine._get_project_delegate.assert_awaited_once_with("project-b")
    delegate.process_message.assert_awaited_once()


@_async_test
async def test_project_store_rejects_cross_project_runtime_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "projects" / "project-a" / "tasks.db"
        store = OPCStore(db_path)
        await store.initialize()
        try:
            try:
                await store.save_task(Task(id="task-b", title="wrong project", project_id="project-b"))
            except RuntimeError as exc:
                assert "cross-project" in str(exc)
            else:
                raise AssertionError("cross-project task write was accepted")

            try:
                await store.save_external_session(
                    ExternalSession(agent_type="codex", project_id="project-b", task_id="task-b")
                )
            except RuntimeError as exc:
                assert "cross-project" in str(exc)
            else:
                raise AssertionError("cross-project external session write was accepted")
        finally:
            await store.close()


@_async_test
async def test_project_store_purges_cross_project_shadow_rows_before_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "projects" / "project-b" / "tasks.db"
        store = OPCStore(db_path)
        await store.initialize()
        await store.close()

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO tasks (id, title, status, created_at, project_id, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "shadow-a",
                    "CEO Intake",
                    "cancelled",
                    "2026-01-01T00:00:00",
                    "project-a",
                    '{"work_item_runtime": true, "work_item_runtime_version": 1, '
                    '"work_item_projection_id": "corporate::intake::ceo"}',
                ),
            )
            conn.execute(
                """INSERT INTO external_sessions
                   (session_key, agent_type, project_id, session_id, task_id, status, metadata, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "codex:project-a:shadow-a",
                    "codex",
                    "project-a",
                    "codex:project-a:shadow-a",
                    "shadow-a",
                    "working",
                    "{}",
                    "2026-01-01T00:00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        reopened = OPCStore(db_path)
        await reopened.initialize()
        try:
            assert await reopened.get_task("shadow-a") is None
            async with reopened._db.execute(
                "SELECT COUNT(*) FROM external_sessions WHERE project_id != 'project-b'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == 0
        finally:
            await reopened.close()


@_async_test
async def test_ui_project_switch_uses_delegate_without_cancelling_background_tasks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = OPCEngine(opc_home=Path(tmp) / ".opc", project_id="project-a")
        root._initialized = True
        (root.opc_home / "projects" / "project-b").mkdir(parents=True, exist_ok=True)
        delegate = SimpleNamespace(
            project_id="project-b",
            opc_home=root.opc_home,
            store=SimpleNamespace(),
            memory=None,
            escalation=None,
            _ensure_attachment_store=MagicMock(),
        )
        root._get_project_delegate = AsyncMock(return_value=delegate)  # type: ignore[method-assign]

        from opc.plugins.office_ui.ws_handler import WSHandler

        chat_store = SimpleNamespace(
            ensure_activity_channel=AsyncMock(),
            ensure_secretary_channel=AsyncMock(),
        )
        handler = WSHandler(root, MagicMock(), chat_store, MagicMock())
        handler._send_ack = AsyncMock()
        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        bg = asyncio.create_task(asyncio.sleep(30))
        handler._background_tasks.add(bg)

        try:
            with patch("opc.plugins.office_ui.ws_handler.build_snapshot", new=AsyncMock(return_value={})), \
                 patch("opc.plugins.office_ui.ws_handler.build_project_index_sync", new=AsyncMock(return_value={})), \
                 patch("opc.plugins.office_ui.ws_handler.build_collab_sync", new=AsyncMock(return_value={})):
                await handler._handle_switch_project(ws, {"project_id": "project-b", "switch_seq": "seq-1"})
                await asyncio.sleep(0)

            root._get_project_delegate.assert_awaited_once_with("project-b")
            # Project switch deliberately rebinds the handler to the delegate.
            assert handler.engine is delegate
            assert handler._client_project_ids[ws] == "project-b"
            assert handler._client_switch_seq[ws] == "seq-1"
            sent_types = [call.args[0]["type"] for call in ws.send_json.await_args_list]
            assert sent_types[0] == "project_switched"
            assert "project_index_push" in sent_types
            assert "collab_sync_push" not in sent_types
            handler._send_ack.assert_awaited_once_with(ws, ok=True, project_id="project-b", switch_seq="seq-1")
            assert bg in handler._background_tasks
            assert not bg.cancelled()
        finally:
            bg.cancel()
            try:
                await bg
            except asyncio.CancelledError:
                pass


@_async_test
async def test_ui_project_switch_snapshot_failure_preserves_current_project_mapping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = OPCEngine(opc_home=Path(tmp) / ".opc", project_id="project-a")
        root._initialized = True
        (root.opc_home / "projects" / "project-b").mkdir(parents=True, exist_ok=True)
        delegate = SimpleNamespace(
            project_id="project-b",
            opc_home=root.opc_home,
            store=SimpleNamespace(),
            memory=None,
            escalation=None,
            _ensure_attachment_store=MagicMock(),
        )
        root._get_project_delegate = AsyncMock(return_value=delegate)  # type: ignore[method-assign]

        from opc.plugins.office_ui.ws_handler import WSHandler

        chat_store = SimpleNamespace(
            ensure_activity_channel=AsyncMock(),
            ensure_secretary_channel=AsyncMock(),
        )
        handler = WSHandler(root, MagicMock(), chat_store, MagicMock())
        handler._send_ack = AsyncMock()
        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        handler._client_project_ids[ws] = "project-a"
        handler._client_switch_seq[ws] = "seq-0"

        with patch(
            "opc.plugins.office_ui.ws_handler.build_snapshot",
            new=AsyncMock(side_effect=RuntimeError("snapshot boom")),
        ), patch("opc.plugins.office_ui.ws_handler.build_project_index_sync", new=AsyncMock(return_value={})), \
            patch("opc.plugins.office_ui.ws_handler.build_collab_sync", new=AsyncMock(return_value={})):
            await handler._handle_switch_project(ws, {"project_id": "project-b", "switch_seq": "seq-1"})
            await asyncio.sleep(0)

        root._get_project_delegate.assert_awaited_once_with("project-b")
        assert handler._client_project_ids[ws] == "project-b"
        assert handler._client_switch_seq[ws] == "seq-1"
        sent_types = [call.args[0]["type"] for call in ws.send_json.await_args_list]
        assert sent_types[0] == "project_switched"
        assert "project_index_push" in sent_types
        assert "collab_sync_push" not in sent_types
        chat_store.ensure_activity_channel.assert_awaited_once_with(project_id="project-b")
        chat_store.ensure_secretary_channel.assert_awaited_once_with(project_id="project-b")
        handler._send_ack.assert_awaited_once_with(ws, ok=True, project_id="project-b", switch_seq="seq-1")


@_async_test
async def test_ui_project_switch_cancels_stale_project_index_task() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = OPCEngine(opc_home=Path(tmp) / ".opc", project_id="project-a")
        root._initialized = True
        for project_id in ("project-b", "project-c"):
            (root.opc_home / "projects" / project_id).mkdir(parents=True, exist_ok=True)
        delegates = {
            project_id: SimpleNamespace(
                project_id=project_id,
                opc_home=root.opc_home,
                store=SimpleNamespace(),
                memory=None,
                escalation=None,
                _ensure_attachment_store=MagicMock(),
            )
            for project_id in ("project-b", "project-c")
        }
        root._get_project_delegate = AsyncMock(side_effect=lambda project_id: delegates[project_id])  # type: ignore[method-assign]

        from opc.plugins.office_ui.ws_handler import WSHandler

        chat_store = SimpleNamespace(
            ensure_activity_channel=AsyncMock(),
            ensure_secretary_channel=AsyncMock(),
        )
        handler = WSHandler(root, MagicMock(), chat_store, MagicMock())
        handler._send_ack = AsyncMock()
        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        stale_started = asyncio.Event()
        stale_cancelled = asyncio.Event()

        async def fake_project_index(engine, *_args, **_kwargs):
            if engine.project_id == "project-b":
                stale_started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    stale_cancelled.set()
                    raise
            return {}

        with patch("opc.plugins.office_ui.ws_handler.build_project_index_sync", new=fake_project_index), \
             patch("opc.plugins.office_ui.ws_handler.build_snapshot", new=AsyncMock(return_value={})):
            await handler._handle_switch_project(ws, {"project_id": "project-b", "switch_seq": "seq-1"})
            await asyncio.wait_for(stale_started.wait(), timeout=1.0)
            await handler._handle_switch_project(ws, {"project_id": "project-c", "switch_seq": "seq-2"})
            await asyncio.wait_for(stale_cancelled.wait(), timeout=1.0)
            await asyncio.sleep(0)

        index_payloads = [
            call.args[0]["payload"]
            for call in ws.send_json.await_args_list
            if call.args[0]["type"] == "project_index_push"
        ]
        assert [payload["project_id"] for payload in index_payloads] == ["project-c"]
        assert handler._client_project_ids[ws] == "project-c"
        assert handler._client_switch_seq[ws] == "seq-2"


@_async_test
async def test_session_processing_keeps_captured_project_engine_after_ui_switch() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_a = Task(
        id="task-a",
        title="A",
        project_id="project-a",
        session_id="session-a",
        metadata={"exec_mode": "task", "preferred_agent": "native"},
    )
    task_b = Task(
        id="task-b",
        title="B",
        project_id="project-b",
        session_id="session-b",
        metadata={"exec_mode": "task", "preferred_agent": "native"},
    )
    store_a = _MemoryStore([task_a])
    store_b = _MemoryStore([task_b])
    engine_a = _ui_engine("project-a", store_a)
    engine_b = _ui_engine("project-b", store_b)
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    handler.broadcast = AsyncMock()

    async def process_on_a(*args, **kwargs):
        handler.engine = engine_b
        return "ok-a"

    engine_a.process_message.side_effect = process_on_a
    await handler._process_session_message(
        "task-a",
        "continue",
        session_id="session-a",
        run_engine=engine_a,
        run_project_id="project-a",
    )

    engine_a.process_message.assert_awaited_once()
    assert engine_a.process_message.await_args.kwargs["project_id"] == "project-a"
    engine_b.process_message.assert_not_awaited()
    assert store_a.tasks["task-a"].status == TaskStatus.IDLE
    assert store_b.tasks["task-b"].status == TaskStatus.PENDING
    chat_store.create_session_channel.assert_awaited_with("task-a", "A", project_id="project-a")


@_async_test
async def test_project_bound_progress_callback_uses_origin_project_after_ui_switch() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_a = Task(id="task-a", title="A", project_id="project-a", session_id="session-a")
    task_b = Task(id="task-b", title="B", project_id="project-b", session_id="session-b")
    engine_a = _ui_engine("project-a", _MemoryStore([task_a]))
    engine_b = _ui_engine("project-b", _MemoryStore([task_b]))
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    handler.broadcast = AsyncMock()
    handler._wire_engine_callbacks(engine_b)
    handler.engine = engine_b

    await engine_a.on_progress("[External status] started pid=123", task_id="task-a")

    chat_store.insert_message.assert_awaited_once()
    assert chat_store.insert_message.await_args.kwargs["project_id"] == "project-a"
    assert chat_store.insert_message.await_args.kwargs["channel_id"] == "session:task-a"


@_async_test
async def test_runtime_agent_message_mirror_uses_origin_project_after_ui_switch() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_a = Task(id="task-a", title="A", project_id="project-a", session_id="session-a")
    task_b = Task(id="task-b", title="B", project_id="project-b", session_id="session-b")
    engine_a = _ui_engine("project-a", _MemoryStore([task_a]))
    engine_b = _ui_engine("project-b", _MemoryStore([task_b]))
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    handler.broadcast = AsyncMock()
    handler.engine = engine_b

    await handler.on_opc_event(
        SimpleNamespace(
            event_type="agent_message_sent",
            payload={"from": "executor", "task_id": "task-a", "body": "A update"},
        ),
        runtime_engine=engine_a,
        project_id="project-a",
    )

    chat_store.insert_message.assert_awaited_once()
    assert chat_store.insert_message.await_args.kwargs["project_id"] == "project-a"
    assert chat_store.insert_message.await_args.kwargs["channel_id"] == "session:task-a"
    assert handler.broadcast.await_args_list[-1].args[0]["payload"]["project_id"] == "project-a"


@_async_test
async def test_runtime_escalation_mirror_uses_origin_project_after_ui_switch() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_a = Task(id="task-a", title="A", project_id="project-a", session_id="session-a")
    task_b = Task(id="task-b", title="B", project_id="project-b", session_id="session-b")
    engine_a = _ui_engine("project-a", _MemoryStore([task_a]))
    engine_b = _ui_engine("project-b", _MemoryStore([task_b]))
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    handler.broadcast = AsyncMock()
    handler.engine = engine_b

    await handler.on_opc_event(
        SimpleNamespace(
            event_type="escalation_created",
            payload={
                "escalation_id": "esc-a",
                "task_id": "task-a",
                "message": "Approve A?",
                "options": [{"id": "approve_once", "label": "Approve once"}],
            },
        ),
        runtime_engine=engine_a,
        project_id="project-a",
    )

    chat_store.insert_message.assert_awaited_once()
    assert chat_store.insert_message.await_args.kwargs["project_id"] == "project-a"
    assert chat_store.insert_message.await_args.kwargs["channel_id"] == "session:task-a"


@_async_test
async def test_delegate_event_bus_forwards_all_events_once_with_project_context() -> None:
    from opc.core.events import EventBus
    from opc.core.models import OPCEvent
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_a = Task(id="task-a", title="A", project_id="project-a", session_id="session-a")
    task_b = Task(id="task-b", title="B", project_id="project-b", session_id="session-b")
    engine_a = _ui_engine("project-a", _MemoryStore([task_a]))
    engine_b = _ui_engine("project-b", _MemoryStore([task_b]))
    engine_b.event_bus = EventBus()

    async def inherited_runtime_forwarder(event):
        raise AssertionError(f"typed runtime forwarder should have been removed: {event}")

    engine_b._forward_runtime_event = inherited_runtime_forwarder
    engine_b.event_bus.subscribe("runtime_event", inherited_runtime_forwarder)
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())

    handler._wire_engine_callbacks(engine_b)
    assert inherited_runtime_forwarder not in engine_b.event_bus._listeners["runtime_event"]

    await engine_b.event_bus.publish(OPCEvent(
        event_type="child_session_created",
        payload={"task_id": "task-b", "title": "B", "session_id": "session-b"},
    ))

    chat_store.create_session_channel.assert_awaited_with("task-b", "B", project_id="project-b")


@_async_test
async def test_session_detail_routes_by_request_project_id() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    task_b = Task(
        id="task-b",
        title="Project B Session",
        project_id="project-b",
        session_id="session-b",
    )
    engine_a = _ui_engine("project-a", _MemoryStore([]))
    engine_b = _ui_engine("project-b", _MemoryStore([task_b]))
    chat_store = SimpleNamespace(
        create_session_channel=AsyncMock(return_value={"channel_id": "session:task-b"}),
        backfill_messages=AsyncMock(return_value=[]),
        get_channel_messages_page_info=AsyncMock(return_value={
            "messages": [],
            "has_more": False,
            "total_count": 0,
        }),
        get_channel_messages=AsyncMock(return_value=[]),
    )
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    handler._engine_for_project = AsyncMock(
        side_effect=lambda project_id: engine_b if project_id == "project-b" else engine_a,
    )
    ws = _FakeWS()

    await handler._handle_session_detail(
        ws,
        {
            "type": "session_detail",
            "project_id": "project-b",
            "task_id": "task-b",
            "limit": 50,
        },
    )

    payload = ws.sent_json[-1]["payload"]
    assert payload["ok"] is True
    assert payload["action"] == "session_detail"
    assert payload["project_id"] == "project-b"
    assert payload["task_id"] == "task-b"
    chat_store.create_session_channel.assert_awaited_once_with(
        "task-b",
        "Project B Session",
        project_id="project-b",
    )
    assert chat_store.get_channel_messages_page_info.await_args.kwargs["project_id"] == "project-b"
    assert chat_store.get_channel_messages_page_info.await_args.kwargs["detail_level"] == "summary"


@_async_test
async def test_project_scoped_ws_request_without_project_id_is_rejected() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    engine = _ui_engine("project-a", _MemoryStore([]))
    chat_store = _ui_chat_store()
    handler = WSHandler(engine, MagicMock(), chat_store, _ui_event_adapter())
    handler._send_ack = AsyncMock()
    ws = _FakeWS()

    await handler._route_message(
        ws,
        json.dumps({"type": "create_session", "title": "Missing project"}),
    )

    handler._send_ack.assert_awaited_once_with(
        ws,
        ok=False,
        error="project_id required for project-scoped request",
        action="create_session",
    )
    assert engine.store.tasks == {}


@_async_test
async def test_kanban_create_task_routes_by_request_project_id() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    engine_a = _ui_engine("project-a", _MemoryStore([]))
    engine_b = _ui_engine("project-b", _MemoryStore([]))
    chat_store = _ui_chat_store()
    handler = WSHandler(engine_a, MagicMock(), chat_store, _ui_event_adapter())
    engine_a._get_project_delegate = AsyncMock(return_value=engine_b)
    handler._engine_for_project = AsyncMock(
        side_effect=lambda project_id: engine_b if project_id == "project-b" else engine_a,
    )
    handler.broadcast = AsyncMock()
    ws = _FakeWS()

    await handler._handle_kanban_create_task(
        ws,
        {
            "project_id": "project-b",
            "task_id": "task-b",
            "title": "B task",
            "description": "belongs to B",
            "board_id": "project-b",
            "column_id": "todo",
        },
    )

    assert "task-b" not in engine_a.store.tasks
    assert engine_b.store.tasks["task-b"].project_id == "project-b"
    chat_store.create_session_channel.assert_awaited_once_with(
        "task-b",
        "B task",
        project_id="project-b",
    )
    payloads = [call.args[0]["payload"] for call in handler.broadcast.await_args_list]
    assert payloads
    assert all(payload.get("project_id") == "project-b" for payload in payloads)


@_async_test
async def test_project_scoped_broadcast_preserves_runtime_project_id() -> None:
    from opc.plugins.office_ui.ws_handler import WSHandler

    engine_a = _ui_engine("project-a", _MemoryStore([]))
    handler = WSHandler(engine_a, MagicMock(), _ui_chat_store(), _ui_event_adapter())
    client = _FakeClient()
    handler._clients.add(client)
    handler._client_project_ids[client] = "project-b"

    await handler.broadcast({
        "type": "session_created",
        "project_id": "project-b",
        "payload": {
            "task_id": "task-b",
            "channel_id": "session:task-b",
            "title": "B",
            "status": "pending",
            "created_at": 1.0,
        },
    })

    envelope = json.loads(client.sent_str[-1])
    assert envelope["type"] == "session_created"
    assert envelope["payload"]["project_id"] == "project-b"


@_async_test
async def test_explicit_external_agent_failure_does_not_try_other_agents_or_native() -> None:
    engine = OPCEngine()
    attempted_agents: list[str] = []
    native_called = False

    class _Adapter:
        def __init__(self, name: str) -> None:
            self.name = name
            self.agent_type = name

        def build_invocation(self, task: Task, workspace_path: str | None = None):
            return [self.name], {"agent": self.name, "command": self.name}

    class _Broker:
        async def run(self, adapter, task, workspace_path, on_progress=None, prepared_task=None):
            attempted_agents.append(adapter.name)
            return TaskResult(status=TaskStatus.FAILED, content=f"{adapter.name} failed", artifacts={})

    async def run_native_agent(task: Task) -> TaskResult:
        nonlocal native_called
        native_called = True
        return TaskResult(status=TaskStatus.DONE, content="native")

    engine.external_broker = _Broker()
    engine._get_external_candidates = lambda task: [("claude_code", _Adapter("claude_code")), ("codex", _Adapter("codex"))]  # type: ignore[method-assign]
    engine._resolve_external_workspace = lambda task: "/tmp/out"  # type: ignore[method-assign]
    engine._build_external_agent_task = AsyncMock(side_effect=lambda task: task)
    engine._run_native_agent = run_native_agent  # type: ignore[method-assign]

    task = Task(
        id="explicit-claude",
        title="Explicit Claude",
        project_id="proj",
        assigned_external_agent="claude_code",
        metadata={
            "router_preferred_agent": "claude_code",
            "target_output_dir": "/tmp/out",
        },
    )

    result = await engine._run_task_once(task)

    assert result.status == TaskStatus.FAILED
    assert attempted_agents == ["claude_code"]
    assert native_called is False


def test_explicit_agent_overrides_company_role_agent_defaults() -> None:
    engine = OPCEngine(project_id="proj")
    engine.org_engine = SimpleNamespace(
        get_employee=lambda employee_id: None,
        ensure_fallback_employee_for_role=lambda role_id, persist=False: None,
        get_default_employee_for_role=lambda role_id: None,
        list_employees=lambda role_id=None: [],
        get_agent=lambda role_id: SimpleNamespace(preferred_external_agent="codex"),
    )

    topology = {
        "seats": [
            {
                "seat_id": "seat-ceo",
                "role_id": "ceo",
            }
        ]
    }
    decision = SimpleNamespace(preferred_agent="claude_code")

    enriched = engine._enrich_runtime_delegation_topology(
        runtime_topology=topology,
        decision=decision,
        project_id="proj",
    )

    seat = enriched["seats"][0]
    assert seat["preferred_external_agent"] == "claude_code"
    assert seat["selected_execution_agent"] == "claude_code"
    assert seat["execution_agent_locked"] is True
    assert seat["selected_execution_agent_source"] == "explicit_user_agent"


@_async_test
async def test_task_mode_external_resume_requires_explicit_session_token() -> None:
    engine = OPCEngine(project_id="proj")
    engine.store = SimpleNamespace(get_external_session=AsyncMock(return_value=None))
    task = Task(
        id="first-turn",
        title="First turn",
        project_id="proj",
        session_id="session-new",
        assigned_external_agent="claude_code",
        metadata={
            "mode": "task",
            "task_mode_contract": "single_full_capability_main_agent",
        },
    )
    adapter = ClaudeCodeAdapter(config=ExternalAgentConfig(command="claude"))

    run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

    assert run_adapter.config.session_mode != "resume"
    assert resume_metadata == {}
    engine.store.get_external_session.assert_any_await(
        "claude_code",
        "proj",
        opc_session_id="session-new",
    )
