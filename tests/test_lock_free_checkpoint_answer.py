"""Office interaction replies bypass conversation locks through durable CAS.

The historical test in this file exercised a special lock-free session_send
branch.  That branch no longer exists: every owner-facing checkpoint now uses
the single interaction_reply control plane.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.plugins.office_ui.ws_handler import WSHandler


def _checkpoint(*, status: str = "pending") -> SimpleNamespace:
    return SimpleNamespace(
        checkpoint_id="cp-tool-1",
        checkpoint_type="tool_permission",
        project_id="p1",
        task_id="worker-task",
        session_id="worker-session",
        status=status,
        created_at=datetime.now(),
        payload={
            "interaction": {
                "kind": "tool_permission",
                "prompt": "Allow shell_exec for the exact stored call?",
                "options": [
                    {"id": "approve_once", "label": "Approve once"},
                    {"id": "deny", "label": "Deny"},
                ],
                "ownership": {
                    "ui_anchor_task_id": "root-task",
                    "ui_anchor_session_id": "root-session",
                    "waiting_task_id": "worker-task",
                    "waiting_session_id": "worker-session",
                },
            },
            "tool_call": {
                "id": "call-1",
                "name": "shell_exec",
                "arguments": {"command": "git clone https://example.test/repo"},
                "fingerprint": "fp-1",
            },
            "approval": {
                "risk_level": "medium",
                "rationale": "External network action requires authorization.",
            },
        },
    )


def _handler(engine: SimpleNamespace) -> WSHandler:
    handler = object.__new__(WSHandler)
    handler._engine_for_request = AsyncMock(return_value=(engine, "p1"))
    handler._send_ack = AsyncMock()
    handler._project_accepted_interaction_reply = AsyncMock()
    handler._task_locks = {"root-task": asyncio.Lock()}
    return handler


async def _case_interaction_reply_acks_while_conversation_lock_is_held() -> None:
    events: list[str] = []
    store = SimpleNamespace(
        is_ready=lambda: True,
        get_task=AsyncMock(return_value=SimpleNamespace(session_id="root-session")),
    )

    async def submit(**kwargs):
        events.append("durable")
        assert kwargs["decision"]["option_id"] == "approve_once"
        return {
            "accepted": True,
            "deduplicated": False,
            "status": "answered",
            "checkpoint_id": "cp-tool-1",
            "checkpoint_type": "tool_permission",
        }

    engine = SimpleNamespace(store=store, submit_checkpoint_decision=submit)
    handler = _handler(engine)

    async def ack(*_args, **_kwargs):
        events.append("ack")

    async def project(**_kwargs):
        events.append("projection")

    handler._send_ack = AsyncMock(side_effect=ack)
    handler._project_accepted_interaction_reply = AsyncMock(side_effect=project)
    await handler._task_locks["root-task"].acquire()
    try:
        await asyncio.wait_for(
            handler._handle_interaction_reply(
                object(),
                {
                    "project_id": "p1",
                    "checkpoint_id": "cp-tool-1",
                    "checkpoint_type": "tool_permission",
                    "client_request_id": "request-1",
                    "requester_task_id": "root-task",
                    "requester_session_id": "root-session",
                    "decision": {"text": "Approve once", "option_id": "approve_once"},
                },
            ),
            timeout=0.25,
        )
    finally:
        handler._task_locks["root-task"].release()

    assert events == ["durable", "ack", "projection"]


async def _case_rejected_interaction_is_acked_and_never_projected() -> None:
    engine = SimpleNamespace(
        store=SimpleNamespace(is_ready=lambda: True, get_task=AsyncMock(return_value=None)),
        submit_checkpoint_decision=AsyncMock(return_value={
            "accepted": False,
            "status": "pending",
            "reason": "not_authorized",
        }),
    )
    handler = _handler(engine)
    await handler._handle_interaction_reply(
        object(),
        {
            "project_id": "p1",
            "checkpoint_id": "cp-tool-1",
            "checkpoint_type": "tool_permission",
            "client_request_id": "request-2",
            "requester_session_id": "other-session",
            "decision": {"text": "Approve once", "option_id": "approve_once"},
        },
    )
    ack = handler._send_ack.await_args.kwargs
    assert ack["ok"] is False
    assert ack["error"] == "not_authorized"
    handler._project_accepted_interaction_reply.assert_not_awaited()


async def _case_session_send_rejects_checkpoint_metadata_before_chat_side_effects() -> None:
    attachment_store = SimpleNamespace(save_from_base64=AsyncMock())
    engine = SimpleNamespace(attachment_store=attachment_store)
    handler = object.__new__(WSHandler)
    handler._engine_for_request = AsyncMock(return_value=(engine, "p1"))
    handler._send_ack = AsyncMock()

    await handler._handle_session_send(
        object(),
        {
            "project_id": "p1",
            "task_id": "root-task",
            "content": "Approve",
            "attachments": [{"filename": "must-not-save.txt", "data": "Zm9v"}],
            "metadata": {
                "response_to_checkpoint_id": "cp-tool-1",
                "response_to_checkpoint_type": "tool_permission",
            },
        },
    )

    attachment_store.save_from_base64.assert_not_awaited()
    ack = handler._send_ack.await_args.kwargs
    assert ack["ok"] is False
    assert ack["error"] == "checkpoint_reply_requires_interaction_reply"


async def _case_ordinary_session_send_routes_turn_without_interaction_submit() -> None:
    task = SimpleNamespace(
        id="root-task",
        title="Existing company chat",
        session_id="root-session",
        status="running",
    )
    store = SimpleNamespace(
        is_ready=lambda: True,
        get_task=AsyncMock(return_value=task),
        get_pending_checkpoints=AsyncMock(return_value=[_checkpoint()]),
        resolve_execution_checkpoint=AsyncMock(),
    )
    engine = SimpleNamespace(
        store=store,
        attachment_store=None,
        memory=None,
        submit_checkpoint_decision=AsyncMock(),
    )
    handler = object.__new__(WSHandler)
    handler._engine_for_request = AsyncMock(return_value=(engine, "p1"))
    handler._exec_mode = "company"
    handler._resolve_task_session_config = lambda _task: ("company", "corporate")
    handler._route_company_suspend_reply_if_pending = AsyncMock(return_value=False)
    handler._process_session_message = AsyncMock()
    handler._send_ack = AsyncMock()
    handler.broadcast = AsyncMock()
    handler.chat_store = SimpleNamespace(
        message_scope=AsyncMock(return_value=None),
        insert_message=AsyncMock(return_value={
            "message_id": "ui-ordinary-1",
            "created_at": 1.0,
        }),
    )

    scheduled: list[object] = []

    def track(_task_id, awaitable, **_kwargs):
        scheduled.append(awaitable)
        awaitable.close()
        return SimpleNamespace()

    handler._track_session = track

    await handler._handle_session_send(
        object(),
        {
            "project_id": "p1",
            "task_id": "root-task",
            "content": "Approve",
            "metadata": {"ui_message_id": "ui-ordinary-1"},
        },
    )

    assert len(scheduled) == 1
    engine.submit_checkpoint_decision.assert_not_awaited()
    store.get_pending_checkpoints.assert_not_awaited()
    store.resolve_execution_checkpoint.assert_not_awaited()


async def _case_snapshot_visibility_uses_only_public_engine_authorizer() -> None:
    checkpoint = _checkpoint()
    store = SimpleNamespace(
        get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
    )
    engine = SimpleNamespace(
        store=store,
        can_answer_checkpoint=AsyncMock(return_value=False),
    )
    handler = object.__new__(WSHandler)
    cards = await handler._visible_owner_interaction_cards(
        engine=engine,
        project_id="p1",
        requester_task_id="root-task",  # matches anchor, but auth says no
        requester_session_id="root-session",
    )
    assert cards == []
    engine.can_answer_checkpoint.assert_awaited_once_with(
        checkpoint,
        requester_task_id="root-task",
        requester_session_id="root-session",
    )


async def _case_snapshot_rebuilds_active_card_when_no_event_or_chat_projection_exists() -> None:
    checkpoint = _checkpoint(status="consuming")
    engine = SimpleNamespace(
        store=SimpleNamespace(
            get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
        ),
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)

    cards = await handler._visible_owner_interaction_cards(
        engine=engine,
        project_id="p1",
        requester_task_id="root-task",
        requester_session_id="root-session",
    )

    assert len(cards) == 1
    assert cards[0]["message_id"] == "checkpoint::cp-tool-1"
    assert cards[0]["metadata"]["checkpoint_status"] == "consuming"
    assert cards[0]["metadata"]["source"] == "execution_checkpoint"


async def _case_tool_permission_card_is_typed_and_rebuilt_from_checkpoint() -> None:
    checkpoint = _checkpoint(status="answered")
    engine = SimpleNamespace(store=SimpleNamespace())
    handler = object.__new__(WSHandler)
    card = await handler._interaction_checkpoint_ui_message(
        checkpoint,
        engine=engine,
        project_id="p1",
    )
    assert card is not None
    assert card["channel_id"] == "session:root-task"
    assert card["metadata"]["checkpoint_type"] == "tool_permission"
    assert card["metadata"]["checkpoint_status"] == "answered"
    assert [option["id"] for option in card["metadata"]["options"]] == [
        "approve_once",
        "deny",
    ]


async def _case_action_permission_uses_the_typed_approval_card() -> None:
    checkpoint = _checkpoint()
    checkpoint.checkpoint_type = "action_permission"
    checkpoint.payload["interaction"]["kind"] = "action_permission"
    checkpoint.payload["interaction"]["prompt"] = "Approve external network action?"
    checkpoint.payload["approval"].update({
        "action_kind": "external_action",
        "action_name": "publish_release",
    })
    engine = SimpleNamespace(store=SimpleNamespace())
    handler = object.__new__(WSHandler)

    card = await handler._interaction_checkpoint_ui_message(
        checkpoint,
        engine=engine,
        project_id="p1",
    )

    assert card is not None
    assert card["metadata"]["checkpoint_type"] == "action_permission"
    assert card["metadata"]["escalation_type"] == "action_permission"
    assert card["metadata"]["action_name"] == "publish_release"
    assert [option["id"] for option in card["metadata"]["options"]] == [
        "approve_once",
        "deny",
    ]


async def _case_manager_review_checkpoint_is_never_projected_to_owner_ui() -> None:
    checkpoint = _checkpoint()
    checkpoint.checkpoint_type = "company_work_item_gate"
    checkpoint.payload = {
        "review_level": "manager",
        "gate": {"type": "review", "instructions": "Internal manager review"},
    }
    engine = SimpleNamespace(
        store=SimpleNamespace(
            get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
        ),
        # Projection filtering is independent of the engine safety boundary.
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)

    cards = await handler._visible_owner_interaction_cards(
        engine=engine,
        project_id="p1",
        requester_task_id="root-task",
        requester_session_id="root-session",
    )

    assert cards == []
    engine.can_answer_checkpoint.assert_not_awaited()


async def _case_taskless_checkpoint_change_projects_from_durable_row() -> None:
    checkpoint = _checkpoint()
    checkpoint.task_id = None
    checkpoint.session_id = "root-session"
    checkpoint.payload["interaction"]["ownership"] = {
        "ui_anchor_task_id": "",
        "ui_anchor_session_id": "root-session",
        "waiting_task_id": "",
        "waiting_session_id": "root-session",
    }
    engine = SimpleNamespace(
        project_id="p1",
        store=SimpleNamespace(),
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)
    handler._enrich_runtime_progress_payload = lambda payload, **_kwargs: dict(payload)
    handler._load_execution_checkpoint_for_reply = AsyncMock(return_value=checkpoint)
    handler.broadcast = AsyncMock()

    await handler._handle_runtime_event_progress(
        {
            "type": "interaction_checkpoint_changed",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "task_id": "",
            "ui_anchor_task_id": "",
            "ui_anchor_session_id": "root-session",
        },
        engine=engine,
        project_id="p1",
    )

    engine.can_answer_checkpoint.assert_awaited_once_with(
        checkpoint,
        requester_task_id="",
        requester_session_id="root-session",
    )
    envelope = handler.broadcast.await_args.args[0]
    assert envelope["type"] == "session_message"
    assert envelope["payload"]["channel_id"] == "activity:p1"
    assert envelope["payload"]["metadata"]["interaction_requester_task_id"] == ""
    assert envelope["payload"]["metadata"]["interaction_requester_session_id"] == "root-session"


async def _case_reconnect_rebuilds_taskless_card_without_an_event() -> None:
    checkpoint = _checkpoint(status="answered")
    checkpoint.task_id = None
    checkpoint.session_id = "root-session"
    checkpoint.payload["interaction"]["ownership"] = {
        "ui_anchor_task_id": "",
        "ui_anchor_session_id": "root-session",
        "waiting_task_id": "",
        "waiting_session_id": "root-session",
    }
    engine = SimpleNamespace(
        store=SimpleNamespace(
            get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
            get_tasks=AsyncMock(return_value=[]),
        ),
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)

    cards = await handler._owner_interaction_baseline_cards(
        engine=engine,
        project_id="p1",
    )

    assert len(cards) == 1
    assert cards[0]["channel_id"] == "activity:p1"
    assert cards[0]["metadata"]["checkpoint_status"] == "answered"
    engine.can_answer_checkpoint.assert_awaited_once_with(
        checkpoint,
        requester_task_id="",
        requester_session_id="root-session",
    )


async def _case_reconnect_uses_pure_host_only_for_presentation() -> None:
    checkpoint = _checkpoint()
    checkpoint.payload["interaction"]["ownership"] = {
        "ui_anchor_task_id": "",
        "ui_anchor_session_id": "root-session",
        "root_session_id": "root-session",
        "waiting_task_id": "waiting-child",
        "waiting_session_id": "child-session",
    }
    pure_host = SimpleNamespace(
        id="pure-host",
        project_id="p1",
        session_id="root-session",
        parent_id=None,
        parent_session_id=None,
        created_at=datetime.now(),
        metadata={"exec_mode": "custom", "company_profile": "custom"},
    )
    waiting_child = SimpleNamespace(
        id="waiting-child",
        project_id="p1",
        session_id="child-session",
        parent_id="pure-host",
        parent_session_id="root-session",
        created_at=datetime.now(),
        metadata={
            "company_runtime_root_session_id": "root-session",
            "work_item_id": "work-item-1",
        },
    )
    engine = SimpleNamespace(
        store=SimpleNamespace(
            get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
            get_tasks=AsyncMock(return_value=[pure_host, waiting_child]),
        ),
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)

    cards = await handler._owner_interaction_baseline_cards(
        engine=engine,
        project_id="p1",
    )

    assert len(cards) == 1
    assert cards[0]["channel_id"] == "session:pure-host"
    assert cards[0]["metadata"]["interaction_requester_task_id"] == ""
    assert cards[0]["metadata"]["interaction_requester_session_id"] == "root-session"
    engine.can_answer_checkpoint.assert_awaited_once_with(
        checkpoint,
        requester_task_id="",
        requester_session_id="root-session",
    )


async def _case_live_rootless_card_ignores_event_child_actor() -> None:
    checkpoint = _checkpoint()
    checkpoint.payload["interaction"]["ownership"] = {
        "ui_anchor_task_id": "",
        "ui_anchor_session_id": "root-session",
        "root_session_id": "root-session",
        "waiting_task_id": "waiting-child",
        "waiting_session_id": "waiting-session",
    }
    config_task = SimpleNamespace(
        id="canonical-config",
        project_id="p1",
        session_id="config-session",
        parent_id=None,
        parent_session_id="root-session",
        created_at=datetime.now(),
        metadata={
            "exec_mode": "custom",
            "company_profile": "custom",
            "company_runtime_root_session_id": "root-session",
        },
    )
    waiting_child = SimpleNamespace(
        id="waiting-child",
        project_id="p1",
        session_id="waiting-session",
        parent_id="canonical-config",
        parent_session_id="root-session",
        created_at=datetime.now(),
        metadata={
            "company_runtime_root_session_id": "root-session",
            "work_item_id": "work-item-2",
        },
    )
    engine = SimpleNamespace(
        project_id="p1",
        store=SimpleNamespace(
            get_execution_checkpoints=AsyncMock(return_value=[checkpoint]),
            get_tasks=AsyncMock(return_value=[config_task, waiting_child]),
        ),
        can_answer_checkpoint=AsyncMock(return_value=True),
    )
    handler = object.__new__(WSHandler)
    handler._load_execution_checkpoint_for_reply = AsyncMock(return_value=checkpoint)
    handler.broadcast = AsyncMock()

    await handler._handle_runtime_event_progress(
        {
            "type": "interaction_checkpoint_changed",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "task_id": "",
            # EventBus is a refresh hint, not an actor source.
            "ui_anchor_task_id": "waiting-child",
            "ui_anchor_session_id": "waiting-session",
        },
        engine=engine,
        project_id="p1",
    )

    engine.can_answer_checkpoint.assert_awaited_once_with(
        checkpoint,
        requester_task_id="",
        requester_session_id="root-session",
    )
    card = handler.broadcast.await_args.args[0]["payload"]
    assert card["channel_id"] == "session:canonical-config"
    assert card["metadata"]["interaction_requester_task_id"] == ""
    assert card["metadata"]["interaction_requester_session_id"] == "root-session"


async def _case_collab_sync_replays_durable_owner_interactions_after_snapshot() -> None:
    engine = SimpleNamespace()
    handler = object.__new__(WSHandler)
    handler._exec_mode = "company"
    handler.agent_store = object()
    handler.chat_store = object()
    handler.event_adapter = object()
    handler._engine_for_request = AsyncMock(return_value=(engine, "p1"))
    order: list[str] = []

    class Socket:
        async def send_json(self, payload):
            assert payload["type"] == "ack"
            order.append("snapshot")

    async def replay(*_args, **kwargs):
        assert kwargs == {"engine": engine, "project_id": "p1"}
        order.append("checkpoint")

    handler._send_owner_interaction_baseline_for_client = AsyncMock(
        side_effect=replay
    )
    with patch(
        "opc.plugins.office_ui.ws_handler.build_collab_sync",
        new=AsyncMock(return_value={"messages": []}),
    ):
        await handler._handle_collab_sync(Socket(), {"project_id": "p1"})

    assert order == ["snapshot", "checkpoint"]


class OfficeInteractionReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_interaction_reply_acks_while_conversation_lock_is_held(self) -> None:
        await _case_interaction_reply_acks_while_conversation_lock_is_held()

    async def test_rejected_interaction_is_acked_and_never_projected(self) -> None:
        await _case_rejected_interaction_is_acked_and_never_projected()

    async def test_session_send_rejects_checkpoint_metadata_before_chat_side_effects(self) -> None:
        await _case_session_send_rejects_checkpoint_metadata_before_chat_side_effects()

    async def test_ordinary_session_send_routes_turn_without_interaction_submit(self) -> None:
        await _case_ordinary_session_send_routes_turn_without_interaction_submit()

    async def test_snapshot_visibility_uses_only_public_engine_authorizer(self) -> None:
        await _case_snapshot_visibility_uses_only_public_engine_authorizer()

    async def test_snapshot_rebuilds_active_card_without_event_or_chat_projection(self) -> None:
        await _case_snapshot_rebuilds_active_card_when_no_event_or_chat_projection_exists()

    async def test_tool_permission_card_is_typed_and_rebuilt_from_checkpoint(self) -> None:
        await _case_tool_permission_card_is_typed_and_rebuilt_from_checkpoint()

    async def test_action_permission_uses_the_typed_approval_card(self) -> None:
        await _case_action_permission_uses_the_typed_approval_card()

    async def test_manager_review_checkpoint_is_never_projected_to_owner_ui(self) -> None:
        await _case_manager_review_checkpoint_is_never_projected_to_owner_ui()

    async def test_taskless_checkpoint_change_projects_from_durable_row(self) -> None:
        await _case_taskless_checkpoint_change_projects_from_durable_row()

    async def test_reconnect_rebuilds_taskless_card_without_an_event(self) -> None:
        await _case_reconnect_rebuilds_taskless_card_without_an_event()

    async def test_reconnect_uses_pure_host_only_for_presentation(self) -> None:
        await _case_reconnect_uses_pure_host_only_for_presentation()

    async def test_live_rootless_card_ignores_event_child_actor(self) -> None:
        await _case_live_rootless_card_ignores_event_child_actor()

    async def test_collab_sync_replays_durable_owner_interactions_after_snapshot(self) -> None:
        await _case_collab_sync_replays_durable_owner_interactions_after_snapshot()


def test_legacy_office_approval_paths_are_removed() -> None:
    assert not hasattr(WSHandler, "_try_lock_free_parked_checkpoint_answer")
    assert not hasattr(WSHandler, "_handle_ui_escalation")
    assert not hasattr(WSHandler, "_route_company_delivery_feedback_reply_if_pending")
    assert not hasattr(WSHandler, "_supersede_pending_delivery_feedback_for_new_company_turn")
    assert not hasattr(WSHandler, "_update_or_emit_checkpoint_card_status")


def test_office_wiring_disables_durable_coordinator_presentation_callbacks() -> None:
    legacy_callback = object()
    engine = SimpleNamespace(
        project_id="p1",
        on_escalation=legacy_callback,
        interaction_coordinator=SimpleNamespace(presentation_callback=legacy_callback),
        company_executor=None,
        reorg_manager=None,
        event_bus=None,
    )
    handler = object.__new__(WSHandler)
    handler._root_engine = engine

    handler._wire_engine_callbacks(engine)

    assert engine.on_escalation is None
    assert engine.interaction_coordinator.presentation_callback is None
