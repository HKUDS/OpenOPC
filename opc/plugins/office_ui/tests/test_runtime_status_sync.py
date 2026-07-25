"""Tests for the runtime-status reconciliation broadcast (issue #11).

The sync tick re-broadcasts the authoritative status of every task with a
live runtime so any dropped live delta converges within one interval.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from opc.plugins.office_ui.event_adapter import AgentAnimState, EventAdapter
from opc.plugins.office_ui.ws_handler import WSHandler


@dataclass
class FakeEvent:
    event_type: str
    payload: dict[str, Any]
    timestamp: float = 0.0
    event_id: str = "test"


@dataclass
class _FakeTask:
    status: str


@dataclass
class _FakeStore:
    tasks: dict[str, _FakeTask] = field(default_factory=dict)

    async def get_task(self, task_id: str) -> _FakeTask | None:
        return self.tasks.get(task_id)


class _FakeBgTask:
    """Hashable stand-in for an asyncio.Task in the bg-context registry."""

    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _fake_bg_task(done: bool = False) -> _FakeBgTask:
    return _FakeBgTask(done)


def _make_handler(tasks: dict[str, _FakeTask]) -> WSHandler:
    engine = SimpleNamespace(project_id="p1", store=_FakeStore(tasks))
    handler = WSHandler(engine, SimpleNamespace(), SimpleNamespace(), EventAdapter())
    handler._engine_for_project = AsyncMock(return_value=engine)  # type: ignore[method-assign]
    handler.broadcast = AsyncMock()  # type: ignore[method-assign]
    handler._clients = {object()}  # type: ignore[assignment]
    return handler


class RuntimeStatusSyncTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_broadcasts_authoritative_status_with_tracker_state(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "task-1", "project_id": "p1"}
        tracker = handler.event_adapter._get_tracker("agent-1")
        tracker.task_id = "task-1"
        tracker.state = AgentAnimState.REFLECTING
        tracker.current_tool = None

        await handler._runtime_status_sync_tick()

        handler.broadcast.assert_awaited_once()
        envelope = handler.broadcast.await_args.args[0]
        self.assertEqual(envelope["type"], "runtime_status_sync")
        self.assertEqual(envelope["payload"]["project_id"], "p1")
        self.assertEqual(envelope["payload"]["sessions"], [{
            "task_id": "task-1",
            "status": "running",
            "agent_status": "reflecting",
            "current_tool": None,
        }])

    async def test_tick_without_clients_or_candidates_is_silent(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})

        # No candidates at all → nothing to broadcast.
        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()

        # Candidates but no clients → still silent (snapshot covers reconnect).
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "task-1", "project_id": "p1"}
        handler._clients = set()  # type: ignore[assignment]
        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()

    async def test_departed_task_gets_one_final_tick(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="done")})
        bg = _fake_bg_task()
        handler._task_bg_context[bg] = {"task_id": "task-1", "project_id": "p1"}

        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 1)

        # Runtime finished: the task leaves the registry, one final sync fires.
        del handler._task_bg_context[bg]
        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 2)
        envelope = handler.broadcast.await_args.args[0]
        self.assertEqual(envelope["payload"]["sessions"][0]["status"], "done")

        # After the final tick the task is forgotten entirely.
        await handler._runtime_status_sync_tick()
        self.assertEqual(handler.broadcast.await_count, 2)

    async def test_company_child_inherits_root_project(self) -> None:
        handler = _make_handler({
            "root-1": _FakeTask(status="running"),
            "child-1": _FakeTask(status="running"),
        })
        handler._task_bg_context[_fake_bg_task()] = {"task_id": "root-1", "project_id": "p1"}
        handler._active_runtime_children["root-1"] = "root-1"
        handler._active_runtime_children["child-1"] = "root-1"

        await handler._runtime_status_sync_tick()

        envelope = handler.broadcast.await_args.args[0]
        task_ids = [s["task_id"] for s in envelope["payload"]["sessions"]]
        self.assertEqual(task_ids, ["child-1", "root-1"])
        self.assertEqual(envelope["payload"]["project_id"], "p1")

    async def test_done_bg_tasks_are_not_candidates(self) -> None:
        handler = _make_handler({"task-1": _FakeTask(status="running")})
        handler._task_bg_context[_fake_bg_task(done=True)] = {"task_id": "task-1", "project_id": "p1"}

        await handler._runtime_status_sync_tick()
        handler.broadcast.assert_not_awaited()


class TrackerTurnLifecycleTests(unittest.TestCase):
    """The tracker must treat tool boundaries as reasoning, and turn ends as idle."""

    def _runtime_event(self, runtime_type: str, **extra: Any) -> FakeEvent:
        return FakeEvent("runtime_event", {"type": runtime_type, "task_id": "task-1", **extra})

    def test_tool_completed_returns_to_reflecting(self) -> None:
        adapter = EventAdapter()
        adapter.translate(self._runtime_event("tool_started", tool_name="file_read", agent_id="a1"))
        adapter.translate(self._runtime_event("tool_completed", agent_id="a1"))
        tracker = adapter._get_tracker("a1")
        self.assertEqual(tracker.state, AgentAnimState.REFLECTING)
        self.assertIsNone(tracker.current_tool)

    def test_turn_end_goes_idle_and_emits_runtime_update(self) -> None:
        adapter = EventAdapter()
        adapter.translate(self._runtime_event("turn_started", agent_id="a1"))
        for terminal in ("turn_completed", "turn_failed"):
            adapter.translate(self._runtime_event("turn_started", agent_id="a1"))
            events = adapter.translate(self._runtime_event(terminal, agent_id="a1"))
            tracker = adapter._get_tracker("a1")
            self.assertEqual(tracker.state, AgentAnimState.IDLE)
            updates = [e for e in events if e["type"] == "agent_runtime_update"]
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]["data"]["status"], "idle")


if __name__ == "__main__":
    unittest.main()
