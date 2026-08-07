from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.layer2_organization.company_runtime_identity import (
    build_company_runtime_identity_index,
)
from opc.plugins.office_ui.services.models import ServiceError
from opc.plugins.office_ui.snapshot_builder import (
    _build_company_runtime_control_by_task,
    _primary_session_tasks_by_session_id,
)
from opc.plugins.office_ui.ws_handler import WSHandler


def _runtime_records() -> tuple[list[Task], ExecutionCheckpoint]:
    runtime_session_id = "runtime-session"
    anchor = Task(
        id="ui-anchor",
        project_id="project-a",
        session_id=runtime_session_id,
        status=TaskStatus.CANCELLED,
        metadata={
            "exec_mode": "company",
            "mode": "company",
            "company_profile": "corporate",
        },
        created_at=datetime.now() - timedelta(minutes=3),
    )
    final_decider = Task(
        id="final-decider",
        project_id="project-a",
        session_id=runtime_session_id,
        parent_session_id=runtime_session_id,
        status=TaskStatus.CANCELLED,
        linked_work_item_id="work-item-root",
        metadata={
            "mode": "company",
            "work_item_runtime": True,
            "work_item_projection_id": "root",
            "shared_role_session": True,
            "shared_role_id": "ceo",
            "company_runtime_root_session_id": runtime_session_id,
        },
        created_at=datetime.now() - timedelta(minutes=2),
    )
    child = Task(
        id="worker",
        project_id="project-a",
        session_id=f"{runtime_session_id}:role:worker",
        parent_session_id=runtime_session_id,
        status=TaskStatus.BLOCKED,
        linked_work_item_id="work-item-worker",
        metadata={
            "mode": "company",
            "work_item_runtime": True,
            "work_item_projection_id": "worker",
            "shared_role_session": True,
            "company_runtime_root_session_id": runtime_session_id,
        },
        created_at=datetime.now() - timedelta(minutes=1),
    )
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="checkpoint-1",
        project_id="project-a",
        session_id=runtime_session_id,
        checkpoint_type="company_runtime_interrupted",
        status="pending",
        task_id=final_decider.id,
        payload={"parent_session_id": runtime_session_id},
    )
    return [anchor, final_decider, child], checkpoint


def test_identity_is_session_first_and_never_selects_shared_final_decider_as_ui_anchor() -> None:
    tasks, checkpoint = _runtime_records()
    index = build_company_runtime_identity_index(tasks, [checkpoint])

    identity = index.resolve(
        task_id="final-decider",
        runtime_session_id="runtime-session",
        checkpoint_id="checkpoint-1",
    )

    assert identity is not None
    assert identity.runtime_session_id == "runtime-session"
    assert identity.ui_anchor_task_id == "ui-anchor"
    assert identity.config_source_task_id == "ui-anchor"
    assert identity.runtime_task_ids == ("ui-anchor", "final-decider", "worker")
    assert identity.pending_checkpoint_id == "checkpoint-1"
    assert identity.resumable is True
    assert index.resolve(task_session_id="runtime-session:role:worker") == identity
    assert index.resolve(task_id="worker", runtime_session_id="other-session") is None
    assert index.resolve(task_id="ui-anchor", checkpoint_id="other-checkpoint") is None


def test_config_source_uses_configured_scope_task_when_ui_anchor_has_no_config() -> None:
    tasks, _checkpoint = _runtime_records()
    tasks[0].metadata = {}
    tasks[1].metadata.update({"exec_mode": "org", "company_profile": "custom", "org_id": "studio"})

    identity = build_company_runtime_identity_index(tasks).resolve(
        task_id="ui-anchor",
    )

    assert identity is not None
    assert identity.ui_anchor_task_id == "ui-anchor"
    assert identity.config_source_task_id == "final-decider"


def test_snapshot_session_representative_uses_canonical_ui_anchor() -> None:
    tasks, _checkpoint = _runtime_records()
    primary, ordered = _primary_session_tasks_by_session_id(
        [tasks[1], tasks[0], tasks[2]],
    )

    assert ordered[0] == "runtime-session"
    assert primary["runtime-session"].id == "ui-anchor"


def test_runtime_without_ui_anchor_never_promotes_shared_work_item() -> None:
    tasks, checkpoint = _runtime_records()
    shared_final = tasks[1]
    shared_final.parent_session_id = None
    index = build_company_runtime_identity_index([shared_final, tasks[2]], [checkpoint])

    identity = index.resolve(runtime_session_id="runtime-session")

    assert identity is not None
    assert identity.ui_anchor_task_id == ""
    assert identity.config_source_task_id == "final-decider"
    primary, _ordered = _primary_session_tasks_by_session_id(
        [shared_final, tasks[2]],
    )
    assert "runtime-session" not in primary


def test_snapshot_projects_checkpoint_control_to_cancelled_anchor_without_task_resume_identity() -> None:
    tasks, checkpoint = _runtime_records()

    class Store:
        async def get_execution_checkpoints(self, **_kwargs):
            return [checkpoint]

    engine = SimpleNamespace(store=Store())
    control = asyncio.run(_build_company_runtime_control_by_task(engine, tasks, "project-a"))

    assert control["ui-anchor"]["runtime_control_state"] == "suspended"
    assert control["ui-anchor"]["can_resume"] is True
    assert control["ui-anchor"]["resume_parent_session_id"] == "runtime-session"
    assert control["ui-anchor"]["pending_runtime_checkpoint_id"] == "checkpoint-1"
    assert "resume_parent_task_id" not in control["ui-anchor"]


def test_service_error_transport_fields_cannot_be_overridden() -> None:
    handler = WSHandler.__new__(WSHandler)
    sent: list[dict] = []

    async def send_ack(_ws, ok=True, **payload):
        sent.append({"ok": ok, **payload})

    handler._send_ack = send_ack
    error = ServiceError(
        "actual_code",
        "actual message",
        {"ok": True, "code": "wrong", "error": "wrong", "detail": "kept"},
    )

    asyncio.run(handler._send_service_error(object(), error, action="test_action"))

    assert sent == [{
        "ok": False,
        "detail": "kept",
        "error": "actual message",
        "code": "actual_code",
        "action": "test_action",
    }]


def test_removed_recovery_action_receives_normal_unknown_message_ack() -> None:
    handler = WSHandler.__new__(WSHandler)
    handler._shutting_down = False
    handler._active_message_tasks = set()
    handler._send_ack = AsyncMock()
    ws = object()

    asyncio.run(handler._route_message(ws, json.dumps({"type": "recovery_action"})))

    handler._send_ack.assert_awaited_once_with(
        ws,
        ok=False,
        error="unknown_message_type",
        action="recovery_action",
    )


def test_work_item_chat_resume_uses_canonical_ui_anchor_as_engine_origin() -> None:
    async def scenario() -> None:
        tasks, checkpoint = _runtime_records()
        handler = WSHandler.__new__(WSHandler)
        handler._exec_mode = "task"
        handler._company_profile = "corporate"
        handler._shutting_down = False
        handler._active_runtime_children = {}
        handler._session_to_task = {}
        handler._task_bg_context = {}
        handler._company_suspend_reply_locks = {"runtime-session": asyncio.Lock()}
        handler.chat_store = None
        handler._set_company_runtime_control = AsyncMock()
        handler._normalize_session_exec_mode = MagicMock(return_value="task")
        handler._normalize_session_company_profile = MagicMock(return_value="corporate")
        handler._resolve_task_session_config = MagicMock(
            return_value=("company", "corporate")
        )
        handler._resolve_task_org_id = MagicMock(return_value="")
        handler._extract_checkpoint_metadata = AsyncMock(return_value=None)
        handler._sync_task_transcript_messages = AsyncMock()
        handler.on_kanban_changed = AsyncMock()
        handler._flush_progress = AsyncMock()
        run_engine = SimpleNamespace(
            project_id="project-a",
            process_message=AsyncMock(return_value="resumed"),
        )
        target = {
            "ui_anchor_task_id": "ui-anchor",
            "config_task": tasks[1],
        }

        await handler._process_company_suspend_reply(
            ui_task_id="final-decider",
            runtime_session_id="runtime-session",
            content="continue",
            attachment_refs=None,
            message_metadata={"ui_force_resume": True},
            user_message_id=None,
            user_message_created_at=None,
            run_engine=run_engine,
            run_project_id="project-a",
            target=target,
            checkpoint=checkpoint,
            lock=handler._company_suspend_reply_locks["runtime-session"],
        )

        call = run_engine.process_message.await_args
        assert call.kwargs["session_id"] == "runtime-session"
        assert call.kwargs["origin_task_id"] == "ui-anchor"
        assert handler._session_to_task["runtime-session"] == "ui-anchor"
        handler._set_company_runtime_control.assert_awaited_once_with(
            target,
            state="resuming",
            checkpoint_id="checkpoint-1",
        )
        handler.on_kanban_changed.assert_awaited_once_with(engine=run_engine)

    asyncio.run(scenario())


def test_company_suspend_reply_routes_selected_org_to_engine() -> None:
    async def scenario() -> None:
        _tasks, checkpoint = _runtime_records()
        handler = WSHandler.__new__(WSHandler)
        handler._exec_mode = "task"
        handler._company_profile = "corporate"
        handler._shutting_down = False
        handler._active_runtime_children = {}
        handler._session_to_task = {}
        handler._task_bg_context = {}
        handler._company_suspend_reply_locks = {"runtime-session": asyncio.Lock()}
        handler.chat_store = None
        handler._set_company_runtime_control = AsyncMock()
        handler._normalize_session_exec_mode = MagicMock(return_value="task")
        handler._normalize_session_company_profile = MagicMock(return_value="corporate")
        handler._resolve_task_session_config = MagicMock(return_value=("org", "custom"))
        handler._resolve_task_org_id = MagicMock(return_value="selected-org")
        handler._extract_checkpoint_metadata = AsyncMock(return_value=None)
        handler._sync_task_transcript_messages = AsyncMock()
        handler.on_kanban_changed = AsyncMock()
        handler._flush_progress = AsyncMock()
        run_engine = SimpleNamespace(
            project_id="project-a",
            process_message=AsyncMock(return_value="resumed"),
        )
        target = {
            "ui_anchor_task_id": "ui-anchor",
            "config_task": SimpleNamespace(
                metadata={"exec_mode": "org", "company_profile": "custom"},
                org_id="selected-org",
            ),
        }

        await handler._process_company_suspend_reply(
            ui_task_id="final-decider",
            runtime_session_id="runtime-session",
            content="continue",
            attachment_refs=None,
            message_metadata={"ui_force_resume": True},
            user_message_id=None,
            user_message_created_at=None,
            run_engine=run_engine,
            run_project_id="project-a",
            target=target,
            checkpoint=checkpoint,
            lock=handler._company_suspend_reply_locks["runtime-session"],
        )

        call = run_engine.process_message.await_args
        assert call.kwargs["org_id"] == "selected-org"

    asyncio.run(scenario())


def test_delivery_feedback_reply_fails_closed_without_durable_parent_task() -> None:
    async def scenario() -> None:
        handler = WSHandler.__new__(WSHandler)
        handler._exec_mode = "task"
        handler._company_profile = "corporate"
        handler._shutting_down = False
        handler._active_runtime_children = {}
        handler._session_to_task = {}
        handler._task_bg_context = {}
        handler._company_delivery_feedback_reply_locks = {}
        handler.chat_store = None
        handler._store_is_ready = MagicMock(return_value=True)
        handler._normalize_session_exec_mode = MagicMock(return_value="task")
        handler._normalize_session_company_profile = MagicMock(return_value="corporate")
        handler._resolve_task_session_config = MagicMock(return_value=("org", "custom"))
        handler._resolve_task_org_id = MagicMock(return_value="")
        handler._chat_store_is_ready = MagicMock(return_value=False)
        handler._flush_progress = AsyncMock()
        run_engine = SimpleNamespace(
            project_id="project-a",
            store=SimpleNamespace(get_task=AsyncMock(return_value=None)),
            run_company_delivery_self_evolution_checkpoint=AsyncMock(return_value="ran"),
        )

        await handler._process_company_delivery_feedback_reply(
            parent_task_id="delivery-task",
            parent_session_id="delivery-session",
            reply_channel_id="session:delivery-task",
            content="approved",
            attachment_refs=None,
            message_metadata=None,
            user_message_id=None,
            user_message_created_at=None,
            run_engine=run_engine,
            run_project_id="project-a",
            checkpoint=SimpleNamespace(checkpoint_id="cp-delivery", payload={}),
            waiting_task_id="delivery-task",
            lock=asyncio.Lock(),
        )

        run_engine.run_company_delivery_self_evolution_checkpoint.assert_not_awaited()
        handler._resolve_task_org_id.assert_not_called()

    asyncio.run(scenario())


def test_delivery_feedback_reply_proceeds_with_durable_parent_task() -> None:
    async def scenario() -> None:
        handler = WSHandler.__new__(WSHandler)
        handler._exec_mode = "task"
        handler._company_profile = "corporate"
        handler._shutting_down = False
        handler._active_runtime_children = {}
        handler._session_to_task = {}
        handler._task_bg_context = {}
        handler._company_delivery_feedback_reply_locks = {}
        handler.chat_store = None
        handler._store_is_ready = MagicMock(return_value=True)
        handler._normalize_session_exec_mode = MagicMock(return_value="task")
        handler._normalize_session_company_profile = MagicMock(return_value="corporate")
        handler._resolve_task_session_config = MagicMock(return_value=("org", "custom"))
        handler._resolve_task_org_id = MagicMock(return_value="selected-org")
        handler._chat_store_is_ready = MagicMock(return_value=False)
        handler._flush_progress = AsyncMock()
        parent_task = SimpleNamespace(id="delivery-task")
        run_engine = SimpleNamespace(
            project_id="project-a",
            store=SimpleNamespace(get_task=AsyncMock(return_value=parent_task)),
            run_company_delivery_self_evolution_checkpoint=AsyncMock(return_value="ran"),
        )

        await handler._process_company_delivery_feedback_reply(
            parent_task_id="delivery-task",
            parent_session_id="delivery-session",
            reply_channel_id="session:delivery-task",
            content="approved",
            attachment_refs=None,
            message_metadata=None,
            user_message_id=None,
            user_message_created_at=None,
            run_engine=run_engine,
            run_project_id="project-a",
            checkpoint=SimpleNamespace(checkpoint_id="cp-delivery", payload={}),
            waiting_task_id="delivery-task",
            lock=asyncio.Lock(),
        )

        call = run_engine.run_company_delivery_self_evolution_checkpoint.await_args
        assert call is not None
        assert call.kwargs["action"] == "approve"
        handler._resolve_task_org_id.assert_called_once_with(parent_task)

    asyncio.run(scenario())


def test_suspend_reply_missing_custom_org_id_fails_closed_before_engine_call() -> None:
    async def scenario() -> None:
        _tasks, checkpoint = _runtime_records()
        handler = WSHandler.__new__(WSHandler)
        handler._exec_mode = "task"
        handler._company_profile = "corporate"
        handler._shutting_down = False
        handler.chat_store = None
        handler._company_suspend_reply_locks = {
            "runtime-session": asyncio.Lock(),
        }
        handler._task_bg_context = {}
        handler._active_runtime_children = {}
        handler._session_to_task = {}
        handler._set_company_runtime_control = AsyncMock()
        handler._normalize_session_exec_mode = MagicMock(return_value="task")
        handler._normalize_session_company_profile = MagicMock(return_value="corporate")
        handler._resolve_task_session_config = MagicMock(return_value=("org", "custom"))
        handler._resolve_task_org_id = MagicMock(return_value="")
        handler._extract_checkpoint_metadata = AsyncMock(return_value=None)
        handler._sync_task_transcript_messages = AsyncMock()
        handler.on_kanban_changed = AsyncMock()
        handler._flush_progress = AsyncMock()
        run_engine = SimpleNamespace(
            project_id="project-a",
            process_message=AsyncMock(),
        )
        config_task = SimpleNamespace(
            metadata={"exec_mode": "org", "company_profile": "custom"},
            org_id=None,
        )
        target = {
            "ui_anchor_task_id": "ui-anchor",
            "config_task": config_task,
        }

        await handler._process_company_suspend_reply(
            ui_task_id="final-decider",
            runtime_session_id="runtime-session",
            content="continue",
            attachment_refs=None,
            message_metadata=None,
            user_message_id=None,
            user_message_created_at=None,
            run_engine=run_engine,
            run_project_id="project-a",
            target=target,
            checkpoint=checkpoint,
            lock=handler._company_suspend_reply_locks["runtime-session"],
        )

        run_engine.process_message.assert_not_awaited()
        handler._set_company_runtime_control.assert_not_awaited()

    asyncio.run(scenario())


def test_delivery_feedback_rejects_missing_canonical_identity_without_first_task_fallback() -> None:
    async def scenario() -> None:
        tasks, checkpoint = _runtime_records()

        class Store:
            is_ready = True

            async def get_tasks(self, **_kwargs):
                # A shared final-decider deliberately precedes the UI anchor;
                # legacy first-match routing selected the wrong Task here.
                return [tasks[1], tasks[0], tasks[2]]

        handler = WSHandler.__new__(WSHandler)
        handler._resolve_company_runtime_target = AsyncMock(return_value=None)
        engine = SimpleNamespace(store=Store())

        target = await handler._company_delivery_feedback_parent_target(
            task_id="final-decider",
            waiting_task_id="worker",
            waiting_task=tasks[2],
            checkpoint=checkpoint,
            payload={"parent_session_id": "runtime-session"},
            engine=engine,
        )

        assert target == {"parent_task_id": "", "parent_session_id": ""}

    asyncio.run(scenario())


def test_delivery_feedback_route_consumes_missing_identity_instead_of_running_work_item() -> None:
    async def scenario() -> None:
        handler = WSHandler.__new__(WSHandler)
        handler.chat_store = None
        handler._company_delivery_feedback_reply_locks = {}
        handler._load_execution_checkpoint_for_reply = AsyncMock(return_value=SimpleNamespace(
            checkpoint_id="feedback-1",
            checkpoint_type="company_delivery_feedback",
            status="pending",
            task_id="worker",
            session_id="runtime-session",
            payload={"waiting_task_id": "worker", "parent_session_id": "runtime-session"},
        ))
        handler._company_delivery_feedback_parent_target = AsyncMock(return_value={
            "parent_task_id": "",
            "parent_session_id": "",
        })
        handler._track_session = MagicMock()
        engine = SimpleNamespace(store=SimpleNamespace(is_ready=True))

        handled = await handler._route_company_delivery_feedback_reply_if_pending(
            task_id="worker",
            content="looks good",
            session_id="runtime-session:worker",
            task=SimpleNamespace(id="worker"),
            attachment_refs=None,
            message_metadata={
                "response_to_checkpoint_id": "feedback-1",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
            user_message_id="message-1",
            user_message_created_at=None,
            run_engine=engine,
            run_project_id="project-a",
            reply_channel_id="session:worker",
        )

        assert handled is True
        handler._track_session.assert_not_called()

    asyncio.run(scenario())


def test_suspend_reply_identity_mismatch_and_resuming_checkpoint_fail_closed() -> None:
    async def scenario() -> None:
        tasks, checkpoint = _runtime_records()
        handler = WSHandler.__new__(WSHandler)
        handler.chat_store = None
        handler._company_stop_finalize_tasks = {}
        handler._company_suspend_reply_locks = {}
        handler._track = MagicMock()
        engine = SimpleNamespace(project_id="project-a")
        target = {
            "runtime_session_id": "runtime-session",
            "checkpoint": checkpoint,
        }

        handler._resolve_company_runtime_target = AsyncMock(
            side_effect=[target, None],
        )
        mismatched = await handler._route_company_suspend_reply_if_pending(
            task_id="worker",
            content="continue",
            session_id="runtime-session:role:worker",
            task=tasks[2],
            attachment_refs=None,
            message_metadata={
                "response_to_checkpoint_id": "wrong-checkpoint",
                "response_to_checkpoint_type": "company_runtime_interrupted",
            },
            user_message_id=None,
            user_message_created_at=None,
            run_engine=engine,
            run_project_id="project-a",
        )
        assert mismatched is True
        handler._track.assert_not_called()

        checkpoint.status = "resuming"
        handler._resolve_company_runtime_target = AsyncMock(return_value=target)
        resuming = await handler._route_company_suspend_reply_if_pending(
            task_id="worker",
            content="continue again",
            session_id="runtime-session:role:worker",
            task=tasks[2],
            attachment_refs=None,
            message_metadata=None,
            user_message_id=None,
            user_message_created_at=None,
            run_engine=engine,
            run_project_id="project-a",
        )
        assert resuming is True
        handler._track.assert_not_called()

    asyncio.run(scenario())


def test_escalation_control_uses_durable_anchor_not_transient_progress_maps() -> None:
    async def scenario() -> None:
        tasks, checkpoint = _runtime_records()

        class Store:
            async def get_task(self, task_id: str):
                return next((task for task in tasks if task.id == task_id), None)

            async def get_tasks(self, **_kwargs):
                return tasks

            async def get_execution_checkpoints(self, **_kwargs):
                return [checkpoint]

        handler = WSHandler.__new__(WSHandler)
        handler.engine = SimpleNamespace(project_id="project-a", store=Store())
        handler._active_runtime_children = {"worker": "wrong-parent"}
        handler._session_to_task = {"runtime-session": "wrong-parent"}
        handler._ui_task_aliases = {}

        resolved = await handler._resolve_escalation_session_task_id("worker")

        assert resolved == "ui-anchor"

    asyncio.run(scenario())
