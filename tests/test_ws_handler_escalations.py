from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.plugins.office_ui.ws_handler import WSHandler


def _engine() -> SimpleNamespace:
    return SimpleNamespace(
        store=SimpleNamespace(is_ready=lambda: True),
        submit_checkpoint_decision=AsyncMock(return_value={
            "accepted": True,
            "deduplicated": False,
            "status": "answered",
        }),
    )


def _handler(engines: dict[str, SimpleNamespace]) -> WSHandler:
    handler = object.__new__(WSHandler)

    async def resolve_engine(data: dict) -> tuple[SimpleNamespace, str]:
        project_id = str(data.get("project_id", "") or "").strip()
        return engines[project_id], project_id

    handler._engine_for_request = AsyncMock(side_effect=resolve_engine)
    handler._send_ack = AsyncMock()
    handler._project_accepted_interaction_reply = AsyncMock()
    return handler


def _reply(project_id: str, checkpoint_id: str, request_id: str) -> dict:
    return {
        "project_id": project_id,
        "checkpoint_id": checkpoint_id,
        "checkpoint_type": "action_permission",
        "client_request_id": request_id,
        "requester_task_id": "root-task",
        "requester_session_id": "root-session",
        "decision": {
            "text": "Allow for this session",
            "option_id": "approve_session",
        },
    }


class WSHandlerInteractionReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_checkpoint_is_submitted_independently(self) -> None:
        engine = _engine()
        handler = _handler({"project-a": engine})

        await handler._handle_interaction_reply(
            object(),
            _reply("project-a", "checkpoint-first", "request-first"),
        )

        engine.submit_checkpoint_decision.assert_awaited_once_with(
            checkpoint_id="checkpoint-first",
            checkpoint_type="action_permission",
            decision={
                "text": "Allow for this session",
                "option_id": "approve_session",
            },
            client_request_id="request-first",
            requester_task_id="root-task",
            requester_session_id="root-session",
        )
        self.assertEqual(handler._send_ack.await_count, 1)
        self.assertEqual(handler._project_accepted_interaction_reply.await_count, 1)

    async def test_project_scope_selects_one_engine_without_sibling_fanout(self) -> None:
        project_a = _engine()
        project_b = _engine()
        handler = _handler({"project-a": project_a, "project-b": project_b})

        await handler._handle_interaction_reply(
            object(),
            _reply("project-a", "checkpoint-a", "request-a"),
        )

        project_a.submit_checkpoint_decision.assert_awaited_once()
        project_b.submit_checkpoint_decision.assert_not_awaited()
        projection = handler._project_accepted_interaction_reply.await_args.kwargs
        self.assertEqual(projection["project_id"], "project-a")
        self.assertEqual(projection["checkpoint_id"], "checkpoint-a")

    def test_process_local_escalation_registry_is_gone(self) -> None:
        self.assertFalse(hasattr(WSHandler, "_remember_pending_escalation"))
        self.assertFalse(hasattr(WSHandler, "_find_pending_escalation"))
        self.assertFalse(hasattr(WSHandler, "_resolve_related_pending_escalations"))


if __name__ == "__main__":
    unittest.main()
