"""Regression tests for the lock-free parked-checkpoint answer path.

Project-0012 forensics: a company goal turn holds the per-task session lock
for hours while its live dispatcher waits on AWAITING_HUMAN approval cards.
The card answers are session messages, so they queued behind that same lock —
a circular wait (dispatcher -> answer -> lock -> dispatcher) that left the
approval clicks undelivered forever. The fix routes a reply that explicitly
targets a pending park checkpoint through the engine's checkpoint-resume
channel without acquiring the turn lock.
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from opc.plugins.office_ui.ws_handler import WSHandler


class _ChatStoreStub:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []

    async def insert_message(self, **kwargs: Any) -> dict[str, Any]:
        self.inserted.append(kwargs)
        return {"message_id": f"msg-{len(self.inserted)}", **kwargs}


class _StoreStub:
    def __init__(self, pending: list[Any]) -> None:
        self._pending = pending

    async def get_pending_checkpoints(self, project_id: str = "default") -> list[Any]:
        return list(self._pending)


class _EngineStub:
    def __init__(self, store: Any, *, reply: str = "Input received.", error: Exception | None = None) -> None:
        self.store = store
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def process_message(self, content: str, **kwargs: Any) -> str:
        self.calls.append({"content": content, **kwargs})
        if self.error is not None:
            raise self.error
        return self.reply


def _pending_checkpoint(
    checkpoint_id: str,
    checkpoint_type: str = "task_user_input",
    *,
    task_id: str = "chat-task",
    session_id: str = "session-1",
) -> Any:
    return SimpleNamespace(
        checkpoint_id=checkpoint_id,
        checkpoint_type=checkpoint_type,
        status="pending",
        task_id=task_id,
        session_id=session_id,
        payload={"task_ids": [task_id], "waiting_task_id": task_id, "session_id": session_id},
    )


def _make_handler(engine: _EngineStub) -> WSHandler:
    handler = object.__new__(WSHandler)
    handler._task_locks = {}
    handler._task_lock_holders = {}
    handler.chat_store = _ChatStoreStub()
    handler._store_is_ready = lambda store: store is not None
    handler.broadcast = _async_noop
    handler._mark_checkpoint_card_after_engine_response = _async_none_kwargs
    return handler


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None


async def _async_none_kwargs(**kwargs: Any) -> None:
    return None


def _answer_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "task_id": "chat-task",
        "content": "Approval decision: approve_session. Re-run it and continue the task.",
        "session_id": "session-1",
        "message_metadata": {
            "response_to_checkpoint_id": "ckpt-park",
            "response_to_checkpoint_type": "task_user_input",
        },
        "user_message_id": "ui-msg-1",
        "user_message_created_at": None,
        "pid": "0012",
        "channel_id": "session:chat-task",
        "session_exec_mode": "company",
        "session_company_profile": "corporate",
        "session_org_id": "",
        "attachment_refs": None,
    }
    kwargs.update(overrides)
    return kwargs


class LockFreeCheckpointAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def _hold_lock(self, handler: WSHandler, task_id: str) -> asyncio.Task:
        lock = handler._get_task_lock(task_id)
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def _holder() -> None:
            async with lock:
                acquired.set()
                await release.wait()

        holder = asyncio.create_task(_holder())
        await acquired.wait()
        handler._task_lock_holders[task_id] = holder
        holder.release_event = release  # type: ignore[attr-defined]
        return holder

    async def test_lock_held_delivers_through_resume_channel(self) -> None:
        engine = _EngineStub(
            _StoreStub([_pending_checkpoint("ckpt-park")]),
            reply="Input received. The company runtime is live and will pick it up on its next dispatch tick.",
        )
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine, **_answer_kwargs()
            )
            self.assertTrue(handled)
            self.assertEqual(len(engine.calls), 1)
            call = engine.calls[0]
            self.assertEqual(call["mode"], "company")
            self.assertEqual(call["project_id"], "0012")
            self.assertEqual(
                call["message_metadata"]["response_to_checkpoint_id"], "ckpt-park"
            )
            # The turn lock must remain untouched — still held by the live turn.
            self.assertTrue(handler._get_task_lock("chat-task").locked())
            # The engine reply is surfaced to the session channel.
            replies = [m for m in handler.chat_store.inserted if m.get("sender") == "assistant"]
            self.assertEqual(len(replies), 1)
            self.assertIn("Input received", replies[0]["content"])
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_lock_free_session_keeps_serialized_path(self) -> None:
        engine = _EngineStub(_StoreStub([_pending_checkpoint("ckpt-park")]))
        handler = _make_handler(engine)
        handled = await handler._try_lock_free_parked_checkpoint_answer(
            engine=engine, **_answer_kwargs()
        )
        self.assertFalse(handled)
        self.assertEqual(engine.calls, [])

    async def test_unknown_or_resolved_checkpoint_declines(self) -> None:
        engine = _EngineStub(_StoreStub([]))
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine, **_answer_kwargs()
            )
            self.assertFalse(handled)
            self.assertEqual(engine.calls, [])
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_non_park_checkpoint_type_declines(self) -> None:
        engine = _EngineStub(
            _StoreStub([_pending_checkpoint("ckpt-park", "company_delivery_feedback")])
        )
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine,
                **_answer_kwargs(
                    message_metadata={
                        "response_to_checkpoint_id": "ckpt-park",
                        "response_to_checkpoint_type": "company_delivery_feedback",
                    }
                ),
            )
            self.assertFalse(handled)
            self.assertEqual(engine.calls, [])
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_lock_free_requires_exact_checkpoint_type_and_owner(self) -> None:
        engine = _EngineStub(
            _StoreStub([
                _pending_checkpoint(
                    "ckpt-park",
                    "company_work_item_gate",
                    task_id="other-task",
                    session_id="other-session",
                )
            ])
        )
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine,
                **_answer_kwargs(),
            )
            self.assertFalse(handled)
            self.assertEqual(engine.calls, [])
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_engine_failure_surfaces_error_without_queueing(self) -> None:
        engine = _EngineStub(
            _StoreStub([_pending_checkpoint("ckpt-park")]),
            error=RuntimeError("resume blew up"),
        )
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine, **_answer_kwargs()
            )
            # Handled=True: the reply must NOT fall through to the locked path,
            # which would silently queue behind the wedged turn again.
            self.assertTrue(handled)
            errors = [m for m in handler.chat_store.inserted if m.get("sender") == "system"]
            self.assertEqual(len(errors), 1)
            self.assertIn("resume blew up", errors[0]["content"])
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_stale_done_holder_lock_self_heals_and_declines(self) -> None:
        engine = _EngineStub(_StoreStub([_pending_checkpoint("ckpt-park")]))
        handler = _make_handler(engine)
        lock = handler._get_task_lock("chat-task")
        await lock.acquire()

        async def _finished() -> None:
            return None

        done_holder = asyncio.create_task(_finished())
        await done_holder
        handler._task_lock_holders["chat-task"] = done_holder
        handled = await handler._try_lock_free_parked_checkpoint_answer(
            engine=engine, **_answer_kwargs()
        )
        # _get_task_lock replaces the stale lock, so the fresh lock is free and
        # the normal serialized path is the right route.
        self.assertFalse(handled)
        self.assertEqual(engine.calls, [])

    async def test_anchor_channel_answer_for_role_task_gate_is_handled(self) -> None:
        # Project-0012 production shape: a company gate checkpoint is raised
        # by a role work-item task, but the card is answered from the run's
        # anchor chat channel. The anchor task id only appears in
        # payload["task_ids"]; exact task/session equality would reject it
        # and re-open the late-approval lock wedge.
        checkpoint = SimpleNamespace(
            checkpoint_id="ckpt-park",
            checkpoint_type="company_work_item_gate",
            status="pending",
            task_id="role-task",
            session_id="role-session",
            payload={
                "waiting_task_id": "role-task",
                "session_id": "role-session",
                "task_ids": ["role-task", "chat-task"],
            },
        )
        engine = _EngineStub(_StoreStub([checkpoint]))
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine,
                **_answer_kwargs(
                    message_metadata={
                        "response_to_checkpoint_id": "ckpt-park",
                        "response_to_checkpoint_type": "company_work_item_gate",
                    },
                ),
            )
            self.assertTrue(handled)
            self.assertEqual(len(engine.calls), 1)
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder

    async def test_legacy_checkpoint_without_linkage_is_still_handled(self) -> None:
        # Checkpoints persisted before ownership fields existed carry no
        # task/session linkage at all. They must keep the pre-scoping
        # behavior (deliver by explicit checkpoint id) instead of silently
        # falling back to the wedged serialized path.
        checkpoint = SimpleNamespace(
            checkpoint_id="ckpt-park",
            checkpoint_type="task_user_input",
            status="pending",
            payload={},
        )
        engine = _EngineStub(_StoreStub([checkpoint]))
        handler = _make_handler(engine)
        holder = await self._hold_lock(handler, "chat-task")
        try:
            handled = await handler._try_lock_free_parked_checkpoint_answer(
                engine=engine, **_answer_kwargs()
            )
            self.assertTrue(handled)
            self.assertEqual(len(engine.calls), 1)
        finally:
            holder.release_event.set()  # type: ignore[attr-defined]
            await holder


class SessionIdentityErrorSurfacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_service_error_surfaces_in_chat_instead_of_raising(self) -> None:
        # _process_session_message mostly runs as a fire-and-forget background
        # task; a ServiceError escaping it is only logged and the user's
        # message silently vanishes. The pre-lock identity resolution must
        # surface the failure as a visible chat error and stop.
        from opc.plugins.office_ui.services.models import ServiceError

        class _Store:
            async def get_task(self, task_id: str) -> Any:
                return SimpleNamespace(id=task_id, session_id="sess-1", metadata={})

        engine = _EngineStub(_Store())
        handler = _make_handler(engine)
        handler.engine = engine
        handler._session_to_task = {}
        handler._exec_mode = "company"
        handler._company_profile = "corporate"
        handler._task_preferred_agent = "native"

        async def _raise_identity_error(*args: Any, **kwargs: Any) -> Any:
            raise ServiceError(
                "company_runtime_identity_mismatch",
                "Company runtime identity could not be resolved",
                {"task_id": "chat-task"},
            )

        handler._resolve_session_runtime_config_task = _raise_identity_error

        await handler._process_session_message("chat-task", "please continue")

        self.assertEqual(engine.calls, [])
        errors = [m for m in handler.chat_store.inserted if m.get("sender") == "system"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Company runtime identity could not be resolved", errors[0]["content"])


if __name__ == "__main__":
    unittest.main()
