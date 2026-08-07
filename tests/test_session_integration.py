"""Integration tests for Session architecture (Phase 1-3).

Tests the complete data flow:
  ws_handler → engine session (memory) → chat_store → broadcast → frontend mappers
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import time
import unittest
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from opc.core.attachment_store import AttachmentRef, AttachmentStore
from opc.core.models import DelegationRun, ExecutionCheckpoint, Task, TaskStatus
from opc.database.store import _SQLiteConnectionAdapter
from opc.layer2_organization import comms as file_comms
from opc.plugins.office_ui.event_adapter import EventAdapter
from opc.plugins.office_ui.chat_store import ChatStore
from opc.plugins.office_ui.services.models import ServiceError


# ═══════════════════════════════════════════════════════════════════════
# Stubs / Mocks
# ═══════════════════════════════════════════════════════════════════════

class StubStore:
    """In-memory task store for testing."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._checkpoints: dict[str, ExecutionCheckpoint] = {}
        self.deleted_session_data: list[tuple[str, str | None]] = []
        self.hard_deleted: list[tuple[str, str | None]] = []

    async def save_task(self, task: Task) -> None:
        self._tasks[task.id] = task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def get_tasks(self, **_kw: Any) -> list[Task]:
        return list(self._tasks.values())

    async def list_tasks(self, **_kw: Any) -> list[Task]:
        return list(self._tasks.values())

    async def delete_session_data(self, task_id: str, session_id: str | None = None) -> None:
        self.deleted_session_data.append((task_id, session_id))

    async def hard_delete_task(self, task_id: str, session_id: str | None = None) -> None:
        self.hard_deleted.append((task_id, session_id))
        self._tasks.pop(task_id, None)

    async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint

    async def get_execution_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        checkpoints = [
            checkpoint
            for checkpoint in self._checkpoints.values()
            if checkpoint.project_id == project_id
        ]
        if session_id:
            checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint.session_id == session_id]
        if checkpoint_types:
            allowed_types = {str(item) for item in checkpoint_types}
            checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint.checkpoint_type in allowed_types]
        if statuses:
            allowed_statuses = {str(item) for item in statuses}
            checkpoints = [checkpoint for checkpoint in checkpoints if checkpoint.status in allowed_statuses]
        return sorted(checkpoints, key=lambda checkpoint: checkpoint.updated_at, reverse=True)

    async def get_pending_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        return await self.get_execution_checkpoints(
            project_id=project_id,
            session_id=session_id,
            checkpoint_types=checkpoint_types,
            statuses=["pending"],
        )

    async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            return
        checkpoint.status = status
        checkpoint.updated_at = datetime.now()


@dataclass
class StubSessionRecord:
    session_id: str = ""
    project_id: str = "default"
    parent_session_id: str | None = None
    title: str = ""
    mode: str = "primary"
    metadata: dict = field(default_factory=dict)
    summary: str = ""
    updated_at: datetime = field(default_factory=datetime.now)


class StubSessionStore:
    """In-memory session store for testing memory_manager operations."""

    def __init__(self) -> None:
        self._sessions: dict[str, StubSessionRecord] = {}
        self._links: list[Any] = []

    async def get_session(self, session_id: str) -> StubSessionRecord | None:
        return self._sessions.get(session_id)

    async def save_session(self, record: Any) -> None:
        self._sessions[record.session_id] = record

    async def save_session_link(self, link: Any) -> None:
        self._links.append(link)


class StubMemory:
    """Stub for engine.memory (MemoryManager)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.title_updates: list[tuple[str, str]] = []

    async def ensure_session(
        self,
        session_id: str,
        project_id: str | None = None,
        *,
        title: str = "",
        mode: str = "primary",
        parent_session_id: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        if session_id not in self.sessions:
            self.sessions[session_id] = {
                "session_id": session_id,
                "project_id": project_id,
                "title": title,
                "mode": mode,
                "parent_session_id": parent_session_id,
                "metadata": dict(metadata or {}),
            }
        else:
            # Mimic the real behavior: only set title if not already set
            existing = self.sessions[session_id]
            if title and not existing.get("title"):
                existing["title"] = title
            if metadata:
                existing_meta = dict(existing.get("metadata", {}) or {})
                existing_meta.update(metadata)
                existing["metadata"] = existing_meta
        return self.sessions[session_id]

    async def update_session_title(self, session_id: str, title: str) -> None:
        self.title_updates.append((session_id, title))
        if session_id in self.sessions:
            self.sessions[session_id]["title"] = title

    async def record_user_turn(self, session_id: str, content: str, **kw: Any) -> None:
        pass


class StubEventBus:
    """Stub EventBus that records published events."""

    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.published.append(event)


def _make_engine(store: StubStore | None = None, memory: StubMemory | None = None) -> MagicMock:
    """Create a stub engine with controllable store/memory."""
    engine = MagicMock()
    engine.store = store or StubStore()
    engine.memory = memory or StubMemory()
    engine.project_id = "test-project"
    engine.on_progress = AsyncMock()
    engine.process_message = AsyncMock(return_value="Test response")
    engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=None)
    return engine


async def _make_chat_store() -> ChatStore:
    """Create an in-memory ChatStore for testing."""
    db = _SQLiteConnectionAdapter(":memory:")
    cs = ChatStore(db)  # type: ignore[arg-type]
    await cs.initialize()
    return cs


class TestEscalationEngineIds(unittest.IsolatedAsyncioTestCase):
    """Escalation ids should be unique per prompt."""

    async def test_escalation_ids_are_unique_for_same_task(self) -> None:
        from opc.layer2_organization.escalation import EscalationEngine

        event_bus = StubEventBus()

        async def _reply(_message: str, _options: list[dict[str, str]]) -> str:
            return "approve_once"

        engine = EscalationEngine(event_bus, user_reply_callback=_reply)
        task = Task(id="task-1", title="Approval target", project_id="test-project")

        await engine.escalate_decision(
            task,
            "Approve tool 'file_read'?",
            [{"id": "approve_once", "label": "Approve once"}],
        )
        await engine.escalate_decision(
            task,
            "Approve tool 'file_write'?",
            [{"id": "approve_once", "label": "Approve once"}],
        )

        created = [evt for evt in event_bus.published if evt.event_type == "escalation_created"]
        self.assertEqual(len(created), 2)
        escalation_ids = [str(evt.payload.get("escalation_id", "")) for evt in created]
        self.assertEqual(len(set(escalation_ids)), 2)
        self.assertTrue(all(escalation_id.startswith("esc_task-1_") for escalation_id in escalation_ids))

    async def test_escalation_decision_timeout_has_no_implicit_approval_default(self) -> None:
        from opc.layer2_organization.escalation import EscalationEngine

        event_bus = StubEventBus()

        async def _reply(_message: str, _options: list[dict[str, str]]) -> str:
            await asyncio.sleep(1)
            return "approve_once"

        engine = EscalationEngine(event_bus, timeout_seconds=0, user_reply_callback=_reply)
        task = Task(id="task-timeout", title="Approval target", project_id="test-project")

        reply = await engine.escalate_decision(
            task,
            "Approve tool 'file_read'?",
            [{"id": "approve_once", "label": "Approve once"}],
        )

        self.assertIsNone(reply)
        timeout_events = [evt for evt in event_bus.published if evt.event_type == "escalation_timeout"]
        self.assertEqual(len(timeout_events), 1)
        self.assertIsNone(timeout_events[0].payload.get("default_action"))


# ═══════════════════════════════════════════════════════════════════════
# Test 1: EventAdapter — display counter & child_session_created
# ═══════════════════════════════════════════════════════════════════════

class TestEventAdapterDisplayCounter(unittest.IsolatedAsyncioTestCase):
    """Verify display counter management and child_session_created translation."""

    def setUp(self) -> None:
        self.adapter = EventAdapter()

    def _make_event(self, event_type: str, payload: dict) -> Any:
        evt = MagicMock()
        evt.event_type = event_type
        evt.payload = payload
        return evt

    def test_task_created_populates_display_map(self) -> None:
        """task_created event should increment counter and populate map."""
        evt = self._make_event("task_created", {"task_id": "t1", "title": "Task 1"})
        results = self.adapter.translate(evt)

        self.assertEqual(self.adapter.task_display_counter, 1)
        self.assertEqual(self.adapter.get_task_display_num("t1"), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "task_routed")

    def test_multiple_tasks_sequential_numbering(self) -> None:
        """Multiple task_created events should have sequential display numbers."""
        for i, tid in enumerate(["t1", "t2", "t3"], 1):
            evt = self._make_event("task_created", {"task_id": tid, "title": f"Task {i}"})
            self.adapter.translate(evt)

        self.assertEqual(self.adapter.task_display_counter, 3)
        self.assertEqual(self.adapter.get_task_display_num("t1"), 1)
        self.assertEqual(self.adapter.get_task_display_num("t2"), 2)
        self.assertEqual(self.adapter.get_task_display_num("t3"), 3)

    def test_get_task_display_num_unknown_falls_back(self) -> None:
        """Unknown task_id should fall back to current counter value."""
        self.adapter._task_display_counter = 5
        self.assertEqual(self.adapter.get_task_display_num("unknown"), 5)

    def test_child_session_created_translation(self) -> None:
        """child_session_created event should be translated to a visual event."""
        evt = self._make_event("child_session_created", {
            "session_id": "s1",
            "parent_session_id": "ps1",
            "task_id": "t1",
            "title": "Sub-task 1",
            "agent_id": "agent-codex",
        })
        results = self.adapter.translate(evt)

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["type"], "child_session_created")
        self.assertEqual(r["data"]["session_id"], "s1")
        self.assertEqual(r["data"]["parent_session_id"], "ps1")
        self.assertEqual(r["data"]["task_id"], "t1")
        self.assertEqual(r["data"]["agent_id"], "agent-codex")

    def test_no_double_counting_task_created_then_child_session(self) -> None:
        """task_created followed by child_session_created should NOT double-count.

        Regression test for Bug5: child_session_created handler used to
        increment _task_display_counter, causing +2 per child task.
        """
        # Step 1: engine publishes task_created (from task_graph.create_task_graph)
        evt1 = self._make_event("task_created", {"task_id": "child-1", "title": "Sub 1"})
        self.adapter.translate(evt1)
        self.assertEqual(self.adapter.task_display_counter, 1)

        # Step 2: engine publishes child_session_created (from _schedule_tasks)
        evt2 = self._make_event("child_session_created", {
            "session_id": "cs1",
            "parent_session_id": "ps1",
            "task_id": "child-1",
            "title": "Sub 1",
            "agent_id": "codex",
        })
        self.adapter.translate(evt2)
        # Counter should still be 1, NOT 2
        self.assertEqual(self.adapter.task_display_counter, 1)
        # Map lookup should return 1
        self.assertEqual(self.adapter.get_task_display_num("child-1"), 1)


# ═══════════════════════════════════════════════════════════════════════
# Test 2: WSHandler — _handle_create_session
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerCreateSession(unittest.IsolatedAsyncioTestCase):
    """Test creating a new chat session."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_create_session_creates_engine_session(self) -> None:
        """_handle_create_session should create an engine session via memory.ensure_session."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "My Chat"})

        # Engine session should be created
        self.assertEqual(len(self.memory.sessions), 1)
        session = list(self.memory.sessions.values())[0]
        self.assertEqual(session["title"], "My Chat")
        self.assertEqual(session["mode"], "primary")

    async def test_create_session_creates_task(self) -> None:
        """_handle_create_session should create a Task with session_id."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "My Chat"})

        tasks = list(self.store._tasks.values())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertIsNotNone(task.session_id)
        self.assertEqual(task.title, "My Chat")

    async def test_create_session_broadcasts_correctly(self) -> None:
        """Should broadcast both board_task_created and session_created."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Test Session"})

        types = [b["type"] for b in self.broadcasts]
        self.assertIn("board_task_created", types)
        self.assertIn("session_created", types)

        # session_created should have session_id
        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertIn("session_id", session_msg["payload"])
        self.assertIsNotNone(session_msg["payload"]["session_id"])

    async def test_create_session_creates_chat_store_channel(self) -> None:
        """Should create a chat_store session channel."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Chat Title"})

        channels = await self.chat_store.get_session_channels(project_id="test-project")
        self.assertEqual(len(channels), 1)
        ch = channels[0]
        self.assertEqual(ch["name"], "Chat Title")
        self.assertEqual(ch["type"], "session")

    async def test_create_session_display_counter_increments(self) -> None:
        """Display counter should increment for each user-created session."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Session 1"})
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Session 2"})

        self.assertEqual(self.adapter.task_display_counter, 2)

        # Verify display_ids in board_task_created broadcasts
        board_msgs = [b for b in self.broadcasts if b["type"] == "board_task_created"]
        display_ids = [b["payload"]["display_id"] for b in board_msgs]
        self.assertEqual(display_ids, ["OPC-1", "OPC-2"])

    async def test_create_session_sends_ack_with_ids(self) -> None:
        """Should send ack with task_id, channel_id, session_id."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Ack Test"})

        self.handler._send_ack.assert_called_once()
        call_kwargs = self.handler._send_ack.call_args
        # _send_ack(ws, ok=True, task_id=..., channel_id=..., session_id=...)
        self.assertTrue(call_kwargs[1].get("ok", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else True))

    async def test_create_session_persists_session_specific_mode(self) -> None:
        """create_session should persist per-session mode/profile to task, memory, broadcast, and ack."""
        ws = MagicMock()
        await self.handler._handle_create_session(ws, {
            "project_id": "test-project",
            "title": "Company Session",
            "exec_mode": "company",
            "company_profile": "corporate",
            "preferred_agent": "codex",
        })

        tasks = list(self.store._tasks.values())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task.metadata.get("exec_mode"), "company")
        self.assertEqual(task.metadata.get("company_profile"), "corporate")
        self.assertEqual(task.metadata.get("preferred_agent"), "codex")

        self.assertEqual(len(self.memory.sessions), 1)
        session = next(iter(self.memory.sessions.values()))
        self.assertEqual(session["metadata"].get("exec_mode"), "company")
        self.assertEqual(session["metadata"].get("company_profile"), "corporate")
        self.assertEqual(session["metadata"].get("preferred_agent"), "codex")

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(session_msg["payload"]["exec_mode"], "company")
        self.assertEqual(session_msg["payload"]["company_profile"], "corporate")
        self.assertEqual(session_msg["payload"]["preferred_agent"], "codex")

        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertEqual(ack_kwargs["exec_mode"], "company")
        self.assertEqual(ack_kwargs["company_profile"], "corporate")
        self.assertEqual(ack_kwargs["preferred_agent"], "codex")

    async def test_create_session_company_mode_ignores_stale_custom_profile(self) -> None:
        ws = MagicMock()

        await self.handler._handle_create_session(ws, {
            "project_id": "test-project",
            "title": "Company Session",
            "exec_mode": "company",
            "company_profile": "custom",
            "org_id": "lab",
        })

        task = next(iter(self.store._tasks.values()))
        self.assertEqual(task.metadata.get("exec_mode"), "company")
        self.assertEqual(task.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", task.metadata)
        self.assertNotIn("organization_id", task.metadata)

        session = next(iter(self.memory.sessions.values()))
        self.assertEqual(session["metadata"].get("exec_mode"), "company")
        self.assertEqual(session["metadata"].get("company_profile"), "corporate")
        self.assertNotIn("org_id", session["metadata"])

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(session_msg["payload"]["exec_mode"], "company")
        self.assertEqual(session_msg["payload"]["company_profile"], "corporate")
        self.assertEqual(session_msg["payload"]["org_id"], "")

        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertEqual(ack_kwargs["exec_mode"], "company")
        self.assertEqual(ack_kwargs["company_profile"], "corporate")
        self.assertEqual(ack_kwargs["org_id"], "")

    async def test_create_session_persists_custom_org_id(self) -> None:
        """org-mode sessions should carry the selected saved organization id."""
        ws = MagicMock()
        self.handler._load_active_org_config_into_engine = MagicMock(return_value=True)
        self.handler._set_active_saved_org_name = AsyncMock()

        await self.handler._handle_create_session(ws, {
            "project_id": "test-project",
            "title": "Custom Org Session",
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
        })

        task = next(iter(self.store._tasks.values()))
        self.assertEqual(task.metadata.get("exec_mode"), "org")
        self.assertEqual(task.metadata.get("company_profile"), "custom")
        self.assertEqual(task.metadata.get("org_id"), "lab")
        self.assertEqual(task.metadata.get("organization_id"), "lab")

        session = next(iter(self.memory.sessions.values()))
        self.assertEqual(session["metadata"].get("org_id"), "lab")

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(session_msg["payload"]["exec_mode"], "org")
        self.assertEqual(session_msg["payload"]["company_profile"], "custom")
        self.assertEqual(session_msg["payload"]["org_id"], "lab")
        self.handler._set_active_saved_org_name.assert_awaited_once_with("lab")

        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertEqual(ack_kwargs["exec_mode"], "org")
        self.assertEqual(ack_kwargs["company_profile"], "custom")
        self.assertEqual(ack_kwargs["org_id"], "lab")


# ═══════════════════════════════════════════════════════════════════════
# Test 3: WSHandler — _handle_session_send with auto-title
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerSessionSend(unittest.IsolatedAsyncioTestCase):
    """Test sending a message in a session context."""

    @staticmethod
    def _discard_session_dispatch(_task_id: str, coro: Any, **_kwargs: Any) -> None:
        coro.close()

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.test_root = Path.cwd() / ".test_ws_handler_session"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.engine.opc_home = self.test_root
        self.engine.attachment_store = MagicMock()
        self.engine.attachment_store.save_from_base64 = AsyncMock(return_value=AttachmentRef(
            attachment_id="att-note",
            filename="note.txt",
            mime_type="text/plain",
            size_bytes=21,
            disk_path="projects/test-project/attachments/att-note/note.txt",
        ))
        self.engine._ensure_attachment_store = MagicMock()
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

        # Pre-create a session task
        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        task = Task(
            id=self.task_id,
            title="New Chat",
            project_id="test-project",
            session_id=self.session_id,
        )
        await self.store.save_task(task)
        self.memory.sessions[self.session_id] = {
            "session_id": self.session_id,
            "title": "New Chat",
            "mode": "primary",
        }

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()
        shutil.rmtree(self.test_root, ignore_errors=True)

    async def test_extract_checkpoint_metadata_uses_engine_session_lookup(self) -> None:
        checkpoint = SimpleNamespace(
            checkpoint_type="company_delivery_feedback",
            checkpoint_id="cp-delivery-1",
            payload={
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Delivery",
                "company_profile": "corporate",
                "feedback_scope": "final",
                "waiting_task_id": "delivery-task-1",
                "prompt": "Please review the final delivery.",
                "delivery_package": {
                    "executive_summary": "Final site is ready.",
                    "artifact_manifest": [{"kind": "file", "label": "App", "value": "index.html"}],
                },
                "result_content": "Generated the requested website.",
            },
        )
        self.engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=checkpoint)

        metadata = await self.handler._extract_checkpoint_metadata(
            self.task_id,
            session_id=self.session_id,
        )

        self.engine.get_latest_pending_checkpoint_for_session.assert_awaited_once_with(self.session_id)
        self.assertEqual(metadata["checkpoint_type"], "company_delivery_feedback")
        self.assertEqual(metadata["checkpoint_id"], "cp-delivery-1")
        self.assertEqual(metadata["waiting_task_id"], "delivery-task-1")
        self.assertEqual(metadata["feedback_scope"], "final")
        self.assertEqual(
            metadata["options"],
            [
                {"id": "approve", "label": "Fully Agree / 完全同意"},
                {"id": "ignore", "label": "Ignore / 忽略"},
                {"id": "feedback", "label": "Feedback / 反馈"},
            ],
        )
        self.assertEqual(metadata["summary"], "Final site is ready.")

    async def test_extract_checkpoint_metadata_maps_staffing_selection_checkpoint(self) -> None:
        checkpoint = SimpleNamespace(
            checkpoint_type="company_staffing_selection",
            checkpoint_id="cp-staffing-1",
            payload={
                "company_profile": "corporate",
                "staffing_roles": [
                    {
                        "role_id": "senior_engineer",
                        "role_label": "Senior Engineer",
                        "default_selection": {
                            "kind": "employee",
                            "id": "senior-existing",
                        },
                    }
                ],
                "staffing_pool": {
                    "employees": [
                        {
                            "employee_id": "senior-existing",
                            "employee_name": "Existing Engineer",
                            "role_id": "senior_engineer",
                        }
                    ],
                    "templates": [
                        {
                            "template_id": "engineering-frontend-developer",
                            "template_name": "Frontend Developer",
                        }
                    ],
                },
            },
        )
        self.engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=checkpoint)

        metadata = await self.handler._extract_checkpoint_metadata(
            self.task_id,
            session_id=self.session_id,
        )

        self.assertEqual(metadata["checkpoint_type"], "company_staffing_selection")
        self.assertEqual(metadata["checkpoint_id"], "cp-staffing-1")
        self.assertEqual(metadata["staffing_roles"][0]["role_id"], "senior_engineer")
        self.assertEqual(
            metadata["staffing_pool"]["templates"][0]["template_id"],
            "engineering-frontend-developer",
        )

    async def test_extract_checkpoint_metadata_maps_task_user_input_checkpoint(self) -> None:
        checkpoint = SimpleNamespace(
            checkpoint_type="task_user_input",
            checkpoint_id="cp-user-input-1",
            payload={
                "task_id": self.task_id,
                "prompt": "Need the target deployment region before continuing.",
                "pause_request": {
                    "reason": "Missing deployment region.",
                    "questions": ["Which region should we deploy to?"],
                    "required_fields": ["deployment_region"],
                    "context_note": "Current config has no default region.",
                    "resume_hint": "Reply with a region like us-east-1.",
                },
            },
        )
        task = await self.store.get_task(self.task_id)
        task.metadata = {
            "work_item_projection_id": "engineering_execution",
        }
        task.title = "Engineering Execution"
        await self.store.save_task(task)
        self.engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=checkpoint)

        metadata = await self.handler._extract_checkpoint_metadata(
            self.task_id,
            session_id=self.session_id,
        )

        self.assertEqual(metadata["checkpoint_type"], "task_user_input")
        self.assertEqual(metadata["checkpoint_id"], "cp-user-input-1")
        self.assertEqual(metadata["work_item_projection_id"], "engineering_execution")
        self.assertEqual(metadata["work_item_projection_title"], "Engineering Execution")
        self.assertEqual(metadata["questions"], ["Which region should we deploy to?"])
        self.assertEqual(metadata["input_questions"][0]["id"], "question_1")
        self.assertEqual(metadata["input_questions"][0]["question"], "Which region should we deploy to?")
        self.assertTrue(metadata["input_questions"][0]["allow_freeform"])
        self.assertEqual(metadata["required_fields"], ["deployment_region"])
        self.assertEqual(metadata["resume_hint"], "Reply with a region like us-east-1.")

    async def test_snapshot_checkpoint_meta_restores_delivery_feedback_checkpoint(self) -> None:
        from opc.plugins.office_ui.snapshot_builder import _build_snapshot_checkpoint_meta

        checkpoint = SimpleNamespace(
            checkpoint_type="company_delivery_feedback",
            checkpoint_id="cp-delivery-snapshot",
            payload={
                "work_item_projection_id": "ceo_delivery",
                "work_item_turn_type": "deliver",
                "work_item_projection_title": "CEO Delivery",
                "feedback_scope": "final",
                "prompt": "Please review.",
                "delivery_package": {"executive_summary": "Ready for review."},
                "runtime_session_id": "runtime-1",
            },
        )
        self.engine.get_latest_pending_checkpoint_for_session = AsyncMock(return_value=checkpoint)
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.status = TaskStatus.AWAITING_HUMAN
        await self.store.save_task(task)

        metadata = await _build_snapshot_checkpoint_meta(self.engine, task)

        self.assertEqual(metadata["checkpoint_type"], "company_delivery_feedback")
        self.assertEqual(metadata["checkpoint_id"], "cp-delivery-snapshot")
        self.assertEqual(metadata["work_item_projection_id"], "ceo_delivery")
        self.assertEqual(metadata["work_item_turn_type"], "deliver")
        self.assertEqual(metadata["summary"], "Ready for review.")
        self.assertEqual(metadata["runtime_session_id"], "runtime-1")

    async def test_session_send_inserts_user_message(self) -> None:
        """User message should be inserted into chat_store and broadcast."""
        ws = MagicMock()

        # Close the scheduled coroutine instead of running it.
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Hello world",
            "metadata": {"ui_message_id": "ui-message-1"},
        })

        # Check chat_store message
        channel_id = f"session:{self.task_id}"
        cursor = await self.chat_store._db.execute(
            "SELECT content, sender, metadata FROM messages WHERE channel_id = ?",
            (channel_id,),
        )
        rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Hello world")
        self.assertEqual(rows[0][1], "user")
        metadata = json.loads(rows[0][2])
        self.assertEqual(metadata.get("ui_message_id"), "ui-message-1")

    async def test_session_send_duplicate_delivery_is_deduplicated(self) -> None:
        """A re-delivered send with the same client ui_message_id (WS pending-queue
        flush after a reconnect) must not create a second row or a second turn."""
        ws = MagicMock()
        dispatched: list = []

        def _record_dispatch(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            dispatched.append(_task_id)
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_record_dispatch)

        payload = {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "你的交付文件在哪里？",
            "metadata": {"ui_message_id": "ui-dup-1"},
        }
        await self.handler._handle_session_send(ws, dict(payload))
        await self.handler._handle_session_send(ws, dict(payload))

        channel_id = f"session:{self.task_id}"
        cursor = await self.chat_store._db.execute(
            "SELECT message_id FROM messages WHERE channel_id = ? AND sender = 'user'",
            (channel_id,),
        )
        rows = await cursor.fetchall()
        self.assertEqual(len(rows), 1)
        # The row is persisted under the client id so later copies are detectable.
        self.assertEqual(rows[0][0], "ui-dup-1")
        self.assertEqual(len(dispatched), 1)

        dedup_acks = [
            call.kwargs
            for call in self.handler._send_ack.await_args_list
            if call.kwargs.get("deduplicated")
        ]
        self.assertEqual(len(dedup_acks), 1)
        self.assertEqual(dedup_acks[0].get("message_id"), "ui-dup-1")

    async def test_session_send_same_text_new_id_is_a_new_turn(self) -> None:
        """Deliberately re-asking the same question (fresh ui_message_id) still works."""
        ws = MagicMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        for ui_id in ("ui-ask-1", "ui-ask-2"):
            await self.handler._handle_session_send(ws, {
                "project_id": "test-project",
                "task_id": self.task_id,
                "content": "进度怎么样了？",
                "metadata": {"ui_message_id": ui_id},
            })

        channel_id = f"session:{self.task_id}"
        cursor = await self.chat_store._db.execute(
            "SELECT message_id FROM messages WHERE channel_id = ? AND sender = 'user' ORDER BY timestamp",
            (channel_id,),
        )
        rows = await cursor.fetchall()
        self.assertEqual([row[0] for row in rows], ["ui-ask-1", "ui-ask-2"])

    async def test_session_send_auto_titles(self) -> None:
        """First message should auto-generate title from content."""
        ws = MagicMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Build a REST API for user management",
        })

        # Task title should be updated
        task = await self.store.get_task(self.task_id)
        self.assertNotEqual(task.title, "New Chat")
        self.assertIn("Build a REST API", task.title)

        # Engine session title should also be updated via update_session_title
        self.assertTrue(len(self.memory.title_updates) > 0)
        sid, new_title = self.memory.title_updates[-1]
        self.assertEqual(sid, self.session_id)
        self.assertIn("Build a REST API", new_title)

        # Broadcast should include session_title_updated
        title_updates = [b for b in self.broadcasts if b["type"] == "session_title_updated"]
        self.assertEqual(len(title_updates), 1)

    async def test_auto_title_not_triggered_for_non_default(self) -> None:
        """If title is not 'New Chat', auto-titling should NOT happen."""
        ws = MagicMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        # Change task title to something custom
        task = await self.store.get_task(self.task_id)
        task.title = "My Custom Title"
        await self.store.save_task(task)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "This should not change the title",
        })

        # Title should remain unchanged
        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.title, "My Custom Title")

        # No title update broadcast
        title_updates = [b for b in self.broadcasts if b["type"] == "session_title_updated"]
        self.assertEqual(len(title_updates), 0)

    async def test_auto_title_truncates_long_english_content_to_ten_words(self) -> None:
        """Auto-title should truncate English content after 10 words."""
        ws = MagicMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        long_msg = "one two three four five six seven eight nine ten eleven twelve"
        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": long_msg,
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.title, "one two three four five six seven eight nine ten...")

    async def test_auto_title_truncates_chinese_content_to_ten_chars(self) -> None:
        """Auto-title should truncate CJK content after 10 characters."""
        ws = MagicMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "请你帮我设计实现一个后端管理系统",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.title, "请你帮我设计实现一个...")

    async def test_session_send_passes_session_id_to_engine(self) -> None:
        """_process_session_message should pass session_id to engine.process_message."""
        ws = MagicMock()

        # Instead of mocking _track, let _process_session_message run directly
        await self.handler._process_session_message(
            self.task_id, "test content", session_id=self.session_id
        )

        # Verify engine.process_message was called with session_id
        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args
        self.assertEqual(call_kwargs.kwargs.get("session_id"), self.session_id)

    async def test_session_send_passes_ui_identity_to_engine(self) -> None:
        await self.handler._process_session_message(
            self.task_id,
            "test content",
            session_id=self.session_id,
            user_message_id="ui-msg-1",
            user_message_created_at=123.5,
        )

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["message_metadata"]["ui_message_id"], "ui-msg-1")
        self.assertEqual(call_kwargs["message_metadata"]["ui_created_at"], 123.5)

    async def test_session_send_uses_session_specific_exec_mode(self) -> None:
        """session_send should use the task's persisted mode/profile, not the global defaults."""
        task = await self.store.get_task(self.task_id)
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "preferred_agent": "codex",
        }
        await self.store.save_task(task)
        self.handler._exec_mode = "task"
        self.handler._company_profile = "corporate"

        await self.handler._process_session_message(
            self.task_id,
            "use company routing",
            session_id=self.session_id,
        )

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "company")
        self.assertEqual(call_kwargs["company_profile"], "corporate")
        self.assertIsNone(call_kwargs["preferred_agent"])

    async def test_company_session_message_broadcasts_runtime_control_running_then_idle(self) -> None:
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)
        self.broadcasts.clear()

        await self.handler._process_session_message(
            self.task_id,
            "use company routing",
            session_id=self.session_id,
        )

        runtime_events = [
            item["payload"]
            for item in self.broadcasts
            if item.get("type") == "session_runtime_control"
        ]
        self.assertGreaterEqual(len(runtime_events), 2)
        self.assertEqual(runtime_events[0]["runtime_control_state"], "running")
        self.assertTrue(runtime_events[0]["can_stop"])
        self.assertFalse(runtime_events[0]["can_resume"])
        self.assertIn(self.task_id, runtime_events[0]["task_ids"])
        self.assertEqual(runtime_events[-1]["runtime_control_state"], "idle")
        self.assertFalse(runtime_events[-1]["can_stop"])
        self.assertFalse(runtime_events[-1]["can_resume"])
        self.assertIn(self.task_id, runtime_events[-1]["task_ids"])

    async def test_company_session_send_bypasses_dispatcher_for_plain_input(self) -> None:
        """Company chat text should reach the engine/CEO path without dispatcher keyword arbitration."""
        ws = MagicMock()
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.handler._dispatch_session_message = AsyncMock()
        self.handler._process_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "检查一下UI",
        })

        self.handler._dispatch_session_message.assert_not_called()
        self.handler._process_session_message.assert_called_once()
        call = self.handler._process_session_message.call_args
        self.assertEqual(call.args[:2], (self.task_id, "检查一下UI"))
        self.assertEqual(call.kwargs["session_id"], self.session_id)
        self.assertEqual(call.kwargs["run_engine"], self.engine)
        self.assertEqual(call.kwargs["run_project_id"], "test-project")

    async def test_company_plain_input_supersedes_unanswered_delivery_review_card(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task-stale",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "execution_mode": "company_mode",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-stale",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)

        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Please review the previous delivery.",
            project_id="test-project",
            metadata={
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "company_delivery_feedback",
                "summary": "Previous delivery is ready.",
            },
        )
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "第二个需求：重新做一个更简洁的版本",
        })

        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_called_once()
        self.assertEqual(self.store._checkpoints[checkpoint.checkpoint_id].status, "superseded")
        refreshed_waiting_task = await self.store.get_task(waiting_task.id)
        self.assertEqual(refreshed_waiting_task.status, TaskStatus.DONE)
        self.assertFalse(refreshed_waiting_task.metadata["requires_user_feedback"])
        self.assertTrue(refreshed_waiting_task.metadata["feedback_superseded"])
        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            (checkpoint_message["message_id"],),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0])
        self.assertEqual(metadata.get("checkpoint_status"), "superseded")
        self.assertEqual(metadata.get("checkpoint_resolution_reason"), "new_company_turn_started")

    async def test_superseded_delivery_feedback_card_reply_is_consumed(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-superseded",
            project_id="test-project",
            session_id=self.session_id,
            task_id=self.task_id,
            checkpoint_type="company_delivery_feedback",
            status="superseded",
            payload={
                "waiting_task_id": self.task_id,
                "task_ids": [self.task_id],
                "feedback_scope": "final",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)
        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Please review the previous delivery.",
            project_id="test-project",
            metadata={
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "company_delivery_feedback",
                "summary": "Previous delivery is ready.",
            },
        )
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "I fully agree with this delivery.",
            "metadata": {
                "response_to_checkpoint_id": "cp-delivery-superseded",
                "response_to_checkpoint_type": "company_delivery_feedback",
                "checkpoint_reply_kind": "approve",
                "self_evolution_trigger": True,
            },
        })

        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_not_called()
        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            (checkpoint_message["message_id"],),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0])
        self.assertEqual(metadata.get("checkpoint_status"), "superseded")
        cursor = await self.chat_store._db.execute(
            "SELECT content FROM messages WHERE sender = 'assistant' ORDER BY timestamp DESC LIMIT 1",
        )
        helper_row = await cursor.fetchone()
        self.assertIsNotNone(helper_row)
        self.assertIn("superseded by a newer company turn", helper_row[0])

    async def test_ignore_delivery_feedback_card_terminalizes_without_self_evolution(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task-ignore",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "execution_mode": "company_mode",
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-ignore",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)

        async def _ignore_checkpoint(cp: ExecutionCheckpoint, *, reply_metadata: dict[str, Any] | None = None) -> str:
            cp.status = "ignored"
            cp.payload = {
                **dict(cp.payload or {}),
                "feedback_ignored": True,
                "feedback_reply_metadata": dict(reply_metadata or {}),
            }
            await self.store.save_execution_checkpoint(cp)
            task = await self.store.get_task(waiting_task.id)
            assert task is not None
            task.status = TaskStatus.DONE
            task.metadata = {
                **dict(task.metadata or {}),
                "requires_user_feedback": False,
                "human_review_closed": True,
                "feedback_closed": True,
                "feedback_resolved": True,
                "feedback_resolution": "self_evolution_review_ignored",
                "self_evolution_review_ignored": True,
            }
            await self.store.save_task(task)
            return "Self-evolution review ignored."

        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Please review the previous delivery.",
            project_id="test-project",
            metadata={
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "company_delivery_feedback",
                "summary": "Previous delivery is ready.",
            },
        )
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.engine.ignore_company_delivery_feedback_checkpoint = AsyncMock(side_effect=_ignore_checkpoint)
        self.engine.run_company_delivery_self_evolution_checkpoint = AsyncMock()
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Ignore this self-evolution review.",
            "metadata": {
                "response_to_checkpoint_id": "cp-delivery-ignore",
                "response_to_checkpoint_type": "company_delivery_feedback",
                "checkpoint_reply_kind": "ignore",
            },
        })

        self.engine.run_company_delivery_self_evolution_checkpoint.assert_not_called()
        self.engine.ignore_company_delivery_feedback_checkpoint.assert_awaited_once()
        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_not_called()
        self.assertEqual(self.store._checkpoints[checkpoint.checkpoint_id].status, "ignored")
        refreshed_waiting_task = await self.store.get_task(waiting_task.id)
        assert refreshed_waiting_task is not None
        self.assertEqual(refreshed_waiting_task.status, TaskStatus.DONE)
        self.assertFalse(refreshed_waiting_task.metadata["requires_user_feedback"])
        self.assertTrue(refreshed_waiting_task.metadata["feedback_closed"])
        self.assertTrue(refreshed_waiting_task.metadata["self_evolution_review_ignored"])
        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            (checkpoint_message["message_id"],),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0])
        self.assertEqual(metadata.get("checkpoint_status"), "ignored")
        self.assertEqual(metadata.get("checkpoint_reply_kind"), "ignore")
        self.assertEqual(metadata.get("checkpoint_resolution_reason"), "ignored_by_user")
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = 'user' AND content = ?",
            ("Ignore this self-evolution review.",),
        )
        user_ignore_count = await cursor.fetchone()
        self.assertEqual(user_ignore_count[0], 0)
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE json_extract(metadata, '$.kind') = 'company_self_evolution_result'",
        )
        result_count = await cursor.fetchone()
        self.assertEqual(result_count[0], 0)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Ignore this self-evolution review.",
            "metadata": {
                "response_to_checkpoint_id": "cp-delivery-ignore",
                "response_to_checkpoint_type": "company_delivery_feedback",
                "checkpoint_reply_kind": "ignore",
            },
        })

        self.engine.ignore_company_delivery_feedback_checkpoint.assert_awaited_once()
        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_not_called()
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = 'user' AND content = ?",
            ("Ignore this self-evolution review.",),
        )
        user_ignore_count = await cursor.fetchone()
        self.assertEqual(user_ignore_count[0], 0)
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = 'assistant' AND content LIKE ?",
            ("This delivery self-evolution review is no longer active.%",),
        )
        stale_helper_count = await cursor.fetchone()
        self.assertEqual(stale_helper_count[0], 0)

    async def test_ignore_delivery_feedback_synthetic_only_card_persists_terminal_card(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task-ignore-synthetic",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "execution_mode": "company_mode",
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-ignore-synthetic",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
                "prompt": "Review this delivery for self-evolution.",
                "work_item_projection_title": "Final delivery",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)

        async def _ignore_checkpoint(cp: ExecutionCheckpoint, *, reply_metadata: dict[str, Any] | None = None) -> str:
            cp.status = "ignored"
            cp.payload = {
                **dict(cp.payload or {}),
                "feedback_ignored": True,
                "feedback_reply_metadata": dict(reply_metadata or {}),
            }
            await self.store.save_execution_checkpoint(cp)
            task = await self.store.get_task(waiting_task.id)
            assert task is not None
            task.status = TaskStatus.DONE
            task.metadata = {
                **dict(task.metadata or {}),
                "requires_user_feedback": False,
                "human_review_closed": True,
                "feedback_closed": True,
                "feedback_resolved": True,
                "feedback_resolution": "self_evolution_review_ignored",
                "self_evolution_review_ignored": True,
            }
            await self.store.save_task(task)
            return "Self-evolution review ignored."

        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.engine.ignore_company_delivery_feedback_checkpoint = AsyncMock(side_effect=_ignore_checkpoint)
        self.engine.run_company_delivery_self_evolution_checkpoint = AsyncMock()
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Ignore this self-evolution review.",
            "metadata": {
                "response_to_checkpoint_id": checkpoint.checkpoint_id,
                "response_to_checkpoint_type": "company_delivery_feedback",
                "checkpoint_reply_kind": "ignore",
            },
        })

        self.engine.run_company_delivery_self_evolution_checkpoint.assert_not_called()
        self.engine.ignore_company_delivery_feedback_checkpoint.assert_awaited_once()
        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_not_called()
        cursor = await self.chat_store._db.execute(
            "SELECT channel_id, sender, sender_name, content, timestamp, metadata "
            "FROM messages WHERE message_id = ? AND project_id = ?",
            (f"checkpoint::{checkpoint.checkpoint_id}", "test-project"),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], f"session:{self.task_id}")
        self.assertEqual(row[1], "assistant")
        self.assertEqual(row[2], "Final delivery")
        self.assertLess(abs(float(row[4]) - checkpoint.created_at.timestamp()), 1.0)
        metadata = json.loads(row[5])
        self.assertEqual(metadata.get("checkpoint_id"), checkpoint.checkpoint_id)
        self.assertEqual(metadata.get("checkpoint_type"), "company_delivery_feedback")
        self.assertEqual(metadata.get("checkpoint_status"), "ignored")
        self.assertEqual(metadata.get("checkpoint_reply_kind"), "ignore")
        self.assertEqual(metadata.get("checkpoint_resolution_reason"), "ignored_by_user")
        self.assertEqual(metadata.get("waiting_task_id"), waiting_task.id)
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = 'user' AND content = ?",
            ("Ignore this self-evolution review.",),
        )
        user_ignore_count = await cursor.fetchone()
        self.assertEqual(user_ignore_count[0], 0)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Ignore this self-evolution review.",
            "metadata": {
                "response_to_checkpoint_id": checkpoint.checkpoint_id,
                "response_to_checkpoint_type": "company_delivery_feedback",
                "checkpoint_reply_kind": "ignore",
            },
        })
        cursor = await self.chat_store._db.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ? AND project_id = ?",
            (f"checkpoint::{checkpoint.checkpoint_id}", "test-project"),
        )
        terminal_count = await cursor.fetchone()
        self.assertEqual(terminal_count[0], 1)
        self.engine.ignore_company_delivery_feedback_checkpoint.assert_awaited_once()

    async def test_new_company_turn_supersedes_synthetic_only_delivery_feedback_card(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task-supersede-synthetic",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "execution_mode": "company_mode",
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-supersede-synthetic",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
                "prompt": "Review this delivery for self-evolution.",
                "work_item_projection_title": "Final delivery",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "第二个需求：继续看黄金价格",
        })

        self.handler._process_company_delivery_feedback_reply.assert_not_called()
        self.handler._process_session_message.assert_called_once()
        self.assertEqual(self.store._checkpoints[checkpoint.checkpoint_id].status, "superseded")
        cursor = await self.chat_store._db.execute(
            "SELECT channel_id, timestamp, metadata FROM messages WHERE message_id = ? AND project_id = ?",
            (f"checkpoint::{checkpoint.checkpoint_id}", "test-project"),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], f"session:{self.task_id}")
        self.assertLess(abs(float(row[1]) - checkpoint.created_at.timestamp()), 1.0)
        metadata = json.loads(row[2])
        self.assertEqual(metadata.get("checkpoint_status"), "superseded")
        self.assertEqual(metadata.get("checkpoint_resolution_reason"), "new_company_turn_started")

    async def test_company_delivery_feedback_reply_routes_to_checkpoint_parent_runtime(self) -> None:
        ws = MagicMock()
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.status = TaskStatus.FAILED
        parent_task.metadata = {
            "exec_mode": "custom",
            "company_profile": "custom",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "custom",
                "company_profile": "custom",
                "execution_mode": "company_mode",
                "work_item_projection_id": "chief::delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-route",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)
        self.handler._process_session_message = AsyncMock()
        self.handler._process_company_delivery_feedback_reply = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "没有ppt介绍。生成一个ppt来说明，用codex的image2",
            "metadata": {
                "response_to_checkpoint_id": "cp-delivery-route",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
        })

        self.handler._process_session_message.assert_not_called()
        self.handler._process_company_delivery_feedback_reply.assert_called_once()
        call = self.handler._process_company_delivery_feedback_reply.call_args.kwargs
        self.assertEqual(call["parent_task_id"], self.task_id)
        self.assertEqual(call["parent_session_id"], self.session_id)
        self.assertEqual(call["reply_channel_id"], f"session:{self.task_id}")
        self.assertEqual(call["waiting_task_id"], waiting_task.id)
        self.assertEqual(call["checkpoint"].checkpoint_id, "cp-delivery-route")
        self.assertEqual(
            call["message_metadata"]["response_to_checkpoint_id"],
            "cp-delivery-route",
        )

    async def test_company_delivery_feedback_reply_records_self_evolution_result_without_resync(self) -> None:
        parent_task = await self.store.get_task(self.task_id)
        assert parent_task is not None
        parent_task.metadata = {
            "exec_mode": "custom",
            "company_profile": "custom",
            "execution_mode": "company_mode",
        }
        await self.store.save_task(parent_task)
        waiting_task = Task(
            id="delivery-task-sync",
            title="Final delivery",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery-work-item",
            parent_session_id=self.session_id,
            status=TaskStatus.AWAITING_HUMAN,
            metadata={
                "exec_mode": "custom",
                "company_profile": "custom",
                "execution_mode": "company_mode",
                "work_item_projection_id": "chief::delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_task(waiting_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-sync",
            project_id="test-project",
            session_id=waiting_task.session_id,
            task_id=waiting_task.id,
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [waiting_task.id],
                "feedback_scope": "final",
            },
        )
        self.handler._mark_checkpoint_card_after_engine_response = AsyncMock(return_value=None)
        self.handler._extract_checkpoint_metadata = AsyncMock(return_value=None)
        self.handler._sync_task_transcript_messages = AsyncMock(return_value=0)
        self.handler._flush_progress = AsyncMock()
        self.engine.run_company_delivery_self_evolution_checkpoint = AsyncMock(return_value="Self-evolution completed.")

        await self.handler._process_company_delivery_feedback_reply(
            parent_task_id=self.task_id,
            parent_session_id=self.session_id,
            reply_channel_id=f"session:{self.task_id}",
            content="继续做图片版 PPT",
            attachment_refs=None,
            message_metadata={
                "response_to_checkpoint_id": "cp-delivery-sync",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
            user_message_id="user-msg-sync",
            user_message_created_at=123.0,
            run_engine=self.engine,
            run_project_id="test-project",
            checkpoint=checkpoint,
            waiting_task_id=waiting_task.id,
            lock=asyncio.Lock(),
        )

        self.engine.process_message.assert_not_awaited()
        self.engine.run_company_delivery_self_evolution_checkpoint.assert_awaited_once()
        self.handler._sync_task_transcript_messages.assert_not_awaited()
        self.handler._extract_checkpoint_metadata.assert_not_awaited()
        messages = await self.chat_store.get_channel_messages(f"session:{self.task_id}", project_id="test-project")
        result_message = next(message for message in messages if message["content"] == "Self-evolution completed.")
        self.assertEqual(result_message["metadata"]["kind"], "company_self_evolution_result")
        self.assertEqual(result_message["metadata"]["response_to_checkpoint_id"], "cp-delivery-sync")
        self.assertNotIn("checkpoint_type", result_message["metadata"])
        self.assertNotIn("checkpoint_id", result_message["metadata"])

    async def test_checkpoint_card_update_falls_back_across_channels(self) -> None:
        other_channel_id = "session:delivery-task"
        await self.chat_store.insert_message(
            channel_id=other_channel_id,
            sender="assistant",
            sender_name="OPC",
            content="Please review the delivery.",
            project_id="test-project",
            metadata={
                "checkpoint_type": "company_delivery_feedback",
                "checkpoint_id": "cp-delivery-global",
            },
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-global",
            project_id="test-project",
            session_id=f"{self.session_id}:delivery",
            task_id="delivery-task",
            checkpoint_type="company_delivery_feedback",
            status="resolved",
            payload={"waiting_task_id": "delivery-task"},
        )
        self.engine._load_execution_checkpoint_by_id = AsyncMock(return_value=checkpoint)

        updated = await self.handler._mark_checkpoint_card_after_engine_response(
            channel_id=f"session:{self.task_id}",
            project_id="test-project",
            engine=self.engine,
            message_metadata={
                "response_to_checkpoint_id": "cp-delivery-global",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
            response_message_id="user-msg-1",
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["channel_id"], other_channel_id)
        self.assertEqual(updated["metadata"]["checkpoint_status"], "responded")
        self.assertEqual(updated["metadata"]["checkpoint_response_message_id"], "user-msg-1")

    async def test_task_session_send_still_uses_dispatcher(self) -> None:
        """Task-mode sessions keep the existing Dispatcher path."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()
        self.handler._process_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "检查一下UI",
        })

        self.handler._dispatch_session_message.assert_called_once()
        self.handler._process_session_message.assert_not_called()

    async def test_done_task_session_send_allows_followup(self) -> None:
        """A completed task-mode chat keeps accepting follow-up turns.

        Regression: the second message used to be rejected with
        ``session_ended`` once the task reached DONE, even though the engine's
        task-mode pipeline reuses the same primary task for follow-ups.
        """
        ws = MagicMock()
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.status = TaskStatus.DONE
        await self.store.save_task(task)
        self.handler._dispatch_session_message = AsyncMock()
        self.handler._process_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "继续基于这个结果优化。",
        })

        ended = [
            call for call in self.handler._send_ack.await_args_list
            if call.kwargs.get("error") == "session_ended"
        ]
        self.assertEqual(ended, [])
        self.handler._dispatch_session_message.assert_called_once()

    async def test_cancelled_task_session_send_is_terminal(self) -> None:
        """A cancelled/deleted session stays closed to further input."""
        ws = MagicMock()
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.status = TaskStatus.CANCELLED
        await self.store.save_task(task)
        self.handler._dispatch_session_message = AsyncMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "继续。",
        })

        ended = [
            call for call in self.handler._send_ack.await_args_list
            if call.kwargs.get("error") == "session_ended"
        ]
        self.assertEqual(len(ended), 1)
        self.handler._dispatch_session_message.assert_not_called()

    async def _seed_cancelled_markerless_company_anchor(self) -> ExecutionCheckpoint:
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        anchor.status = TaskStatus.CANCELLED
        anchor.metadata = {}
        await self.store.save_task(anchor)
        await self.store.save_task(Task(
            id="shared-final-decider",
            title="Final decision",
            project_id="test-project",
            session_id=self.session_id,
            parent_session_id=self.session_id,
            linked_work_item_id="work-item-final",
            status=TaskStatus.BLOCKED,
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "shared_role_session": True,
            },
        ))
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-markerless-anchor",
            project_id="test-project",
            session_id=self.session_id,
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id="shared-final-decider",
            payload={"parent_session_id": self.session_id},
        )
        await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    async def test_cancelled_markerless_company_anchor_text_routes_by_checkpoint_identity(self) -> None:
        checkpoint = await self._seed_cancelled_markerless_company_anchor()
        self.handler._process_company_suspend_reply = AsyncMock()
        fake_bg = object()

        def _close_and_track(_task_id: str, coro: Any, **_kwargs: Any) -> object:
            coro.close()
            self.handler._task_bg_context[fake_bg] = {}
            return fake_bg

        self.handler._track_session = MagicMock(side_effect=_close_and_track)
        ws = MagicMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "继续恢复这个 runtime。",
        })

        self.handler._process_company_suspend_reply.assert_called_once()
        routed = self.handler._process_company_suspend_reply.call_args.kwargs
        self.assertEqual(routed["ui_task_id"], self.task_id)
        self.assertEqual(routed["runtime_session_id"], self.session_id)
        self.assertEqual(routed["checkpoint"].checkpoint_id, checkpoint.checkpoint_id)
        self.handler._track_session.assert_called_once()
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        self.assertEqual(anchor.status, TaskStatus.CANCELLED)

    async def test_cancelled_company_anchor_with_resuming_checkpoint_is_not_ended_early(self) -> None:
        checkpoint = await self._seed_cancelled_markerless_company_anchor()
        checkpoint.status = "resuming"
        await self.store.save_execution_checkpoint(checkpoint)
        self.handler._process_company_suspend_reply = AsyncMock()
        self.handler._process_session_message = AsyncMock()
        ws = MagicMock()

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "不要重复恢复。",
        })

        ended = [
            call for call in self.handler._send_ack.await_args_list
            if call.kwargs.get("error") == "session_ended"
        ]
        self.assertEqual(ended, [])
        self.handler._process_company_suspend_reply.assert_not_called()
        self.handler._process_session_message.assert_not_called()

    async def test_cancelled_markerless_company_anchor_button_routes_by_checkpoint_identity(self) -> None:
        checkpoint = await self._seed_cancelled_markerless_company_anchor()
        self.handler._process_company_suspend_reply = AsyncMock()
        fake_bg = object()

        def _close_and_track(_task_id: str, coro: Any, **_kwargs: Any) -> object:
            coro.close()
            self.handler._task_bg_context[fake_bg] = {}
            return fake_bg

        self.handler._track_session = MagicMock(side_effect=_close_and_track)
        ws = MagicMock()

        await self.handler._handle_session_resume(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "runtime_session_id": self.session_id,
            "checkpoint_id": checkpoint.checkpoint_id,
        })

        self.handler._process_company_suspend_reply.assert_called_once()
        routed = self.handler._process_company_suspend_reply.call_args.kwargs
        self.assertEqual(routed["ui_task_id"], self.task_id)
        self.assertEqual(routed["runtime_session_id"], self.session_id)
        self.assertEqual(routed["checkpoint"].checkpoint_id, checkpoint.checkpoint_id)
        self.handler._send_ack.assert_awaited_with(
            ws,
            ok=True,
            runtime_session_id=self.session_id,
            checkpoint_id=checkpoint.checkpoint_id,
        )
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        self.assertEqual(anchor.status, TaskStatus.CANCELLED)

    async def test_done_company_session_send_is_reopened_for_followup(self) -> None:
        """Completed company chats can continue in the same CEO/company context."""
        ws = MagicMock()
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.status = TaskStatus.DONE
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)
        self.engine.get_active_company_runtime_suspend_checkpoint = AsyncMock(return_value=None)
        self.handler._process_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "继续基于这个结果优化。",
        })

        ended = [
            call for call in self.handler._send_ack.await_args_list
            if call.kwargs.get("error") == "session_ended"
        ]
        self.assertEqual(ended, [])
        self.handler._process_session_message.assert_called_once()

    async def test_process_session_message_reopens_done_company_shell_task(self) -> None:
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.status = TaskStatus.DONE
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)

        await self.handler._process_session_message(
            self.task_id,
            "继续优化。",
            session_id=self.session_id,
        )

        refreshed = await self.store.get_task(self.task_id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, TaskStatus.IDLE)
        self.assertIn("company_session_reopened_at", refreshed.metadata)
        self.engine.process_message.assert_called_once()

    async def test_process_session_message_uses_durable_org_for_role_task(self) -> None:
        runtime_session_id = "runtime-org-session"
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        anchor.session_id = runtime_session_id
        anchor.metadata = {
            "exec_mode": "org",
            "mode": "company",
            "company_profile": "custom",
            "org_id": "wrong-active-org",
            "organization_id": "wrong-active-org",
        }
        anchor.org_id = "vc-investment-firm"
        await self.store.save_task(anchor)

        role_task = Task(
            id="role-task-without-org-id",
            title="Sector Analyst",
            project_id="test-project",
            session_id=f"{runtime_session_id}:role:sector-analyst",
            parent_session_id=runtime_session_id,
            org_id="vc-investment-firm",
            metadata={
                "exec_mode": "org",
                "mode": "company",
                "company_profile": "custom",
                "org_id": "wrong-active-org",
                "organization_id": "wrong-active-org",
                "shared_role_session": True,
                "shared_role_id": "sector_analyst",
                "company_runtime_root_session_id": runtime_session_id,
            },
        )
        await self.store.save_task(role_task)
        self.handler.services_context.get_active_saved_org_name = AsyncMock(
            return_value="wrong-active-org"
        )

        await self.handler._process_session_message(
            role_task.id,
            "approve",
            session_id=role_task.session_id,
        )

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "vc-investment-firm")

    async def test_process_session_message_fails_closed_when_runtime_identity_is_missing(self) -> None:
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.metadata = {
            "mode": "company",
            "company_profile": "custom",
        }
        await self.store.save_task(task)
        self.handler._resolve_company_runtime_target = AsyncMock(return_value=None)
        self.handler.services_context.get_active_saved_org_name = AsyncMock(
            return_value="wrong-active-org"
        )

        # Fail closed without raising: this coroutine usually runs as a
        # fire-and-forget background task where an escaping ServiceError is
        # only logged and the user's message silently vanishes. The rejection
        # must instead surface as a visible chat error.
        await self.handler._process_session_message(
            self.task_id,
            "approve",
            session_id=self.session_id,
        )

        self.engine.process_message.assert_not_called()
        self.handler.services_context.get_active_saved_org_name.assert_not_awaited()
        errors = [
            msg["payload"].get("content", "")
            for msg in self.broadcasts
            if msg.get("type") == "session_message"
            and str(msg.get("payload", {}).get("sender", "")) == "system"
        ]
        self.assertTrue(
            any("Company runtime identity could not be resolved" in text for text in errors),
            errors,
        )

    async def test_process_session_message_rejects_runtime_org_without_durable_org_id(self) -> None:
        runtime_session_id = "runtime-org-missing-id-session"
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        anchor.session_id = runtime_session_id
        anchor.metadata = {
            "exec_mode": "org",
            "mode": "company",
            "company_profile": "custom",
        }
        await self.store.save_task(anchor)

        role_task = Task(
            id="role-task-missing-org-id",
            title="Sector Analyst",
            project_id="test-project",
            session_id=f"{runtime_session_id}:role:sector-analyst",
            parent_session_id=runtime_session_id,
            metadata={
                "exec_mode": "org",
                "mode": "company",
                "company_profile": "custom",
                "shared_role_session": True,
                "shared_role_id": "sector_analyst",
                "company_runtime_root_session_id": runtime_session_id,
            },
        )
        await self.store.save_task(role_task)
        self.handler.services_context.get_active_saved_org_name = AsyncMock(
            return_value="wrong-active-org"
        )

        # Same fail-closed-without-raising contract as the identity-mismatch
        # case above: reject visibly instead of raising out of a background
        # task.
        await self.handler._process_session_message(
            role_task.id,
            "approve",
            session_id=role_task.session_id,
        )

        self.engine.process_message.assert_not_called()
        self.handler.services_context.get_active_saved_org_name.assert_not_awaited()
        errors = [
            msg["payload"].get("content", "")
            for msg in self.broadcasts
            if msg.get("type") == "session_message"
            and str(msg.get("payload", {}).get("sender", "")) == "system"
        ]
        self.assertTrue(
            any("org_id_required" in text for text in errors),
            errors,
        )

    async def test_lock_free_process_session_message_uses_durable_org_for_role_task(self) -> None:
        runtime_session_id = "runtime-org-lock-free-session"
        anchor = await self.store.get_task(self.task_id)
        assert anchor is not None
        anchor.session_id = runtime_session_id
        anchor.metadata = {
            "exec_mode": "org",
            "mode": "company",
            "company_profile": "custom",
            "org_id": "vc-investment-firm",
        }
        await self.store.save_task(anchor)

        role_task = Task(
            id="role-task-lock-free-without-org-id",
            title="Sector Analyst",
            project_id="test-project",
            session_id=f"{runtime_session_id}:role:sector-analyst",
            parent_session_id=runtime_session_id,
            metadata={
                "exec_mode": "org",
                "mode": "company",
                "company_profile": "custom",
                "shared_role_session": True,
                "shared_role_id": "sector_analyst",
                "company_runtime_root_session_id": runtime_session_id,
            },
        )
        await self.store.save_task(role_task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="org-lock-free-gate",
            project_id="test-project",
            session_id=role_task.session_id,
            checkpoint_type="company_work_item_gate",
            status="pending",
            task_id=role_task.id,
            payload={
                "waiting_task_id": role_task.id,
                "task_ids": [role_task.id],
                "session_id": role_task.session_id,
            },
        )
        await self.store.save_execution_checkpoint(checkpoint)
        self.engine._load_execution_checkpoint_by_id = AsyncMock(return_value=checkpoint)
        self.handler.services_context.get_active_saved_org_name = AsyncMock(
            return_value="wrong-active-org"
        )

        lock = self.handler._get_task_lock(role_task.id)
        await lock.acquire()
        try:
            await self.handler._process_session_message(
                role_task.id,
                "approve",
                session_id=role_task.session_id,
                message_metadata={
                    "response_to_checkpoint_id": checkpoint.checkpoint_id,
                    "response_to_checkpoint_type": checkpoint.checkpoint_type,
                },
            )
        finally:
            lock.release()

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "vc-investment-firm")

    async def test_session_send_reuses_task_session_without_is_ready_flag(self) -> None:
        """Session replies should reuse the task session even for simple stub stores."""
        ws = MagicMock()
        self.assertFalse(hasattr(self.store, "is_ready"))

        self.handler._dispatch_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "approve",
        })

        self.handler._dispatch_session_message.assert_called_once_with(
            self.task_id,
            "approve",
            session_id=self.session_id,
            attachment_refs=None,
            message_metadata=None,
            user_message_id=ANY,
            user_message_created_at=ANY,
            run_engine=self.engine,
            run_project_id="test-project",
        )

    async def test_checkpoint_reply_metadata_forwarded_for_non_escalation_cards(self) -> None:
        """Non-escalation checkpoint replies must keep the selected card id/type."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        user_input_answers = {
            "deployment_region": {
                "question_id": "deployment_region",
                "selected_option_id": "a",
                "selected_label": "US East",
                "freeform_text": "Use the existing AWS account.",
                "answer_text": "US East; Use the existing AWS account.",
            }
        }
        cases = [
            ("task_user_input", "cp-task-input", {"user_input_answers": user_input_answers}),
            ("company_work_item_gate", "cp-work-item-gate", {}),
            ("company_recruitment_confirmation", "cp-recruitment", {"checkpoint_reply_kind": "feedback"}),
        ]
        for checkpoint_type, checkpoint_id, extra_metadata in cases:
            with self.subTest(checkpoint_type=checkpoint_type):
                content = f"reply to {checkpoint_type}"
                metadata = {
                    "response_to_checkpoint_id": checkpoint_id,
                    "response_to_checkpoint_type": checkpoint_type,
                    **extra_metadata,
                }
                await self.handler._handle_session_send(ws, {
                    "project_id": "test-project",
                    "task_id": self.task_id,
                    "content": content,
                    "metadata": metadata,
                })

                call_kwargs = self.handler._dispatch_session_message.call_args.kwargs
                self.assertEqual(call_kwargs["message_metadata"], metadata)

                cursor = await self.chat_store._db.execute(
                    "SELECT metadata FROM messages WHERE sender = 'user' AND content = ? ORDER BY timestamp DESC LIMIT 1",
                    (content,),
                )
                row = await cursor.fetchone()
                self.assertIsNotNone(row)
                stored_metadata = json.loads(row[0]) if row and row[0] else {}
                self.assertEqual(stored_metadata.get("response_to_checkpoint_id"), checkpoint_id)
                self.assertEqual(stored_metadata.get("response_to_checkpoint_type"), checkpoint_type)
                if checkpoint_type == "company_recruitment_confirmation":
                    self.assertEqual(stored_metadata.get("checkpoint_reply_kind"), "feedback")
                if checkpoint_type == "task_user_input":
                    self.assertEqual(
                        stored_metadata.get("user_input_answers"),
                        user_input_answers,
                    )

    async def test_session_send_after_company_stop_bypasses_parent_dispatch_lock(self) -> None:
        """Plain text after Stop should go straight to the suspend checkpoint path."""
        ws = MagicMock()
        self.chat_store = await _make_chat_store()
        self.handler.chat_store = self.chat_store

        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)

        await self.store.save_execution_checkpoint(ExecutionCheckpoint(
            checkpoint_id="cp-suspended",
            project_id="test-project",
            session_id=self.session_id,
            checkpoint_type="company_runtime_suspended",
            status="pending",
            payload={"parent_session_id": self.session_id},
        ))
        self.handler._process_company_suspend_reply = AsyncMock()
        fake_bg = object()

        def _close_and_track(_task_id: str, coro: Any, **_kwargs: Any) -> object:
            coro.close()
            self.handler._task_bg_context[fake_bg] = {}
            return fake_bg

        self.handler._track_session = MagicMock(side_effect=_close_and_track)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "改成 Sapphire Tide Runner，并让 CEO 自己修改/删除/新增 work item。",
        })

        self.handler._track_session.assert_called_once()
        self.handler._process_company_suspend_reply.assert_called_once()
        call = self.handler._process_company_suspend_reply.call_args.kwargs
        self.assertEqual(call["ui_task_id"], self.task_id)
        self.assertEqual(call["runtime_session_id"], self.session_id)
        self.assertEqual(call["checkpoint"].checkpoint_id, "cp-suspended")
        self.assertEqual(call["content"], "改成 Sapphire Tide Runner，并让 CEO 自己修改/删除/新增 work item。")

    async def test_rejected_company_resume_refreshes_optimistic_runtime_control(self) -> None:
        task = await self.store.get_task(self.task_id)
        assert task is not None
        task.metadata = {"exec_mode": "company", "company_profile": "corporate"}
        await self.store.save_task(task)
        self.handler._refresh_runtime_control_for_client = AsyncMock()
        ws = MagicMock()

        await self.handler._handle_session_resume(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "runtime_session_id": self.session_id,
        })

        self.handler._send_ack.assert_awaited_once_with(
            ws,
            ok=False,
            error="missing_checkpoint_id",
        )
        self.handler._refresh_runtime_control_for_client.assert_awaited_once_with(
            ws,
            engine=self.engine,
            project_id="test-project",
        )

    async def test_session_send_persists_attachment_refs_and_dispatches_them(self) -> None:
        """Uploaded session attachments should be stored and forwarded into engine execution."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Please review this note",
            "attachments": [{
                "filename": "note.txt",
                "data": "aGVsbG8gZnJvbSBhdHRhY2htZW50",
                "mime_type": "text/plain",
            }],
        })

        self.engine._ensure_attachment_store.assert_called_once()
        self.engine.attachment_store.save_from_base64.assert_awaited_once_with(
            "note.txt",
            "aGVsbG8gZnJvbSBhdHRhY2htZW50",
            mime_type="text/plain",
        )
        self.handler._dispatch_session_message.assert_called_once()
        attachment_refs = self.handler._dispatch_session_message.call_args.kwargs["attachment_refs"]
        self.assertEqual(len(attachment_refs), 1)
        self.assertEqual(attachment_refs[0]["filename"], "note.txt")
        self.assertIn("projects/test-project/attachments/", attachment_refs[0]["disk_path"])

        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE sender = 'user' ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0]) if row and row[0] else {}
        stored_refs = metadata.get("attachment_refs", [])
        self.assertEqual(len(stored_refs), 1)
        self.assertEqual(stored_refs[0]["filename"], "note.txt")

    async def test_session_send_warns_and_aborts_when_attachment_only_upload_fails(self) -> None:
        """Attachment-only sends should stop when every attachment upload fails."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()
        self.engine.attachment_store.save_from_base64 = AsyncMock(
            side_effect=ValueError("Unsupported file type: upload (application/octet-stream)")
        )

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "content": "Sent with attachments",
            "attachments": [{
                "filename": "",
                "data": "aGVsbG8=",
                "mime_type": "image/png",
            }],
        })

        self.handler._dispatch_session_message.assert_not_called()

        cursor = await self.chat_store._db.execute(
            "SELECT sender, content FROM messages ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "assistant")
        self.assertIn("not sent to the model", row[1])
        self.assertIn("upload", row[1])


class TestAttachmentStore(unittest.IsolatedAsyncioTestCase):
    """AttachmentStore should preserve UI mime info for multimodal uploads."""

    async def asyncSetUp(self) -> None:
        self.test_root = Path.cwd() / ".test_attachment_store"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.store = AttachmentStore(self.test_root, "test-project")

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    async def test_save_from_base64_uses_mime_type_to_add_extension(self) -> None:
        payload = base64.b64encode(b"fake-image-bytes").decode("ascii")

        ref = await self.store.save_from_base64(
            "",
            payload,
            mime_type="image/png",
        )

        self.assertEqual(ref.mime_type, "image/png")
        self.assertEqual(ref.filename, "upload.png")
        self.assertTrue((self.test_root / ref.disk_path).is_file())

    async def test_save_from_base64_accepts_data_urls(self) -> None:
        payload = "data:image/png;base64," + base64.b64encode(b"fake-image-bytes").decode("ascii")

        ref = await self.store.save_from_base64("clipboard", payload)

        self.assertEqual(ref.mime_type, "image/png")
        self.assertEqual(ref.filename, "clipboard.png")

    async def test_save_from_base64_accepts_video_uploads(self) -> None:
        payload = base64.b64encode(b"fake-video-bytes").decode("ascii")

        ref = await self.store.save_from_base64(
            "",
            payload,
            mime_type="video/mp4",
        )

        self.assertEqual(ref.mime_type, "video/mp4")
        self.assertEqual(ref.filename, "upload.mp4")
        self.assertTrue((self.test_root / ref.disk_path).is_file())


class TestWSHandlerProjectSwitch(unittest.IsolatedAsyncioTestCase):
    """Project switching should also retarget attachment storage."""

    @staticmethod
    def _discard_session_dispatch(_task_id: str, coro: Any, **_kwargs: Any) -> None:
        coro.close()

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler

        self.test_root = Path.cwd() / ".test_ws_handler_switch"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.engine = _make_engine()
        self.engine.opc_home = self.test_root
        self.engine.project_id = "alpha"
        self.beta_engine = _make_engine()
        self.beta_engine.opc_home = self.test_root
        self.beta_engine.project_id = "beta"
        self.workplace_root = self.test_root / "workplaces"
        self._workplace_patcher = patch(
            "opc.plugins.office_ui.ws_handler.get_project_workplace",
            side_effect=lambda project_id: self.workplace_root / project_id,
        )
        self._workplace_patcher.start()
        self.engine.attachment_store = AttachmentStore(self.engine.opc_home, "alpha")
        self.beta_engine.attachment_store = AttachmentStore(self.beta_engine.opc_home, "beta")

        def _ensure_attachment_store() -> None:
            self.beta_engine.attachment_store = AttachmentStore(
                self.beta_engine.opc_home,
                self.beta_engine.project_id or "default",
            )

        self.beta_engine._ensure_attachment_store = MagicMock(side_effect=_ensure_attachment_store)
        self.engine._get_project_delegate = AsyncMock(return_value=self.beta_engine)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.handler.broadcast = AsyncMock()
        self.handler._send_ack = AsyncMock()
        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        await self.engine.store.save_task(Task(
            id=self.task_id,
            title="Project Switch Session",
            project_id="alpha",
            session_id=self.session_id,
        ))
        await self.chat_store.create_session_channel(
            self.task_id,
            "Project Switch Session",
            project_id="alpha",
        )

    async def asyncTearDown(self) -> None:
        self._workplace_patcher.stop()
        await self.chat_store._db.close()
        shutil.rmtree(self.test_root, ignore_errors=True)

    @patch("opc.plugins.office_ui.ws_handler.build_collab_sync", new_callable=AsyncMock)
    @patch("opc.plugins.office_ui.ws_handler.build_snapshot", new_callable=AsyncMock)
    async def test_switch_project_refreshes_attachment_store(
        self,
        build_snapshot_mock: AsyncMock,
        build_collab_sync_mock: AsyncMock,
    ) -> None:
        """Attachment file resolution must follow the newly active project."""
        build_snapshot_mock.return_value = {}
        build_collab_sync_mock.return_value = {}

        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        (self.engine.opc_home / "projects" / "beta").mkdir(parents=True, exist_ok=True)

        await self.handler._handle_switch_project(ws, {"project_id": "beta"})

        self.engine._get_project_delegate.assert_awaited_once_with("beta")
        self.assertEqual(self.engine.project_id, "alpha")
        self.assertEqual(self.beta_engine.attachment_store.project_id, "beta")
        self.assertEqual(
            self.beta_engine.attachment_store.base_dir,
            self.beta_engine.opc_home / "projects" / "beta" / "attachments",
        )
        self.beta_engine._ensure_attachment_store.assert_called_once()

    async def test_create_project_ack_returns_updated_project_list(self) -> None:
        """Creating a project should immediately return the refreshed project list."""
        await self.handler._handle_create_project(MagicMock(), {"project_id": "new_04"})

        self.handler._send_ack.assert_awaited()
        _, kwargs = self.handler._send_ack.await_args
        self.assertTrue(kwargs["ok"])
        self.assertEqual(kwargs["project_id"], "new_04")
        self.assertEqual(kwargs["active_project_id"], "alpha")
        project_ids = [item["id"] for item in kwargs["projects"]]
        self.assertIn("default", project_ids)
        self.assertIn("new_04", project_ids)
        self.assertTrue((self.engine.opc_home / "projects" / "new_04").is_dir())
        self.assertTrue((self.engine.opc_home / "memory" / "projects" / "new_04.md").is_file())
        self.assertTrue((self.workplace_root / "new_04").is_dir())

    async def test_create_project_rejects_duplicate_artifacts(self) -> None:
        """Creating a project should fail if any canonical project artifact exists."""
        (self.engine.opc_home / "memory" / "projects").mkdir(parents=True, exist_ok=True)
        (self.engine.opc_home / "memory" / "projects" / "dupe.md").write_text("# Project Memory (dupe)\n")

        await self.handler._handle_create_project(MagicMock(), {"project_id": "dupe"})

        self.handler._send_ack.assert_awaited()
        _, kwargs = self.handler._send_ack.await_args
        self.assertFalse(kwargs["ok"])
        self.assertIn("already exists", kwargs["error"])

    async def test_stale_human_escalation_reply_does_not_dispatch(self) -> None:
        """Expired approval clicks should not be re-routed as ordinary chat messages."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        await self.handler._handle_session_send(ws, {
            "project_id": "alpha",
            "task_id": self.task_id,
            "content": "Always allow for this project",
            "metadata": {
                "response_to_checkpoint_id": "esc-stale",
                "response_to_checkpoint_type": "human_escalation",
                "response_to_escalation_id": "esc-stale",
            },
        })

        self.handler._dispatch_session_message.assert_not_called()
        self.handler._track_session.assert_not_called()

        cursor = await self.chat_store._db.execute(
            "SELECT sender, content FROM messages ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "assistant")
        self.assertIn("no longer active", row[1])

    async def test_stale_human_escalation_metadata_stripped_for_normal_message(self) -> None:
        """If stale approval metadata lingers, normal chat should still go through cleanly."""
        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()

        def _close_coro(_task_id: str, coro: Any, **_kwargs: Any) -> None:
            coro.close()

        self.handler._track_session = MagicMock(side_effect=_close_coro)

        await self.handler._handle_session_send(ws, {
            "project_id": "alpha",
            "task_id": self.task_id,
            "content": "Please continue with a status update",
            "metadata": {
                "response_to_checkpoint_id": "esc-stale",
                "response_to_checkpoint_type": "human_escalation",
                "response_to_escalation_id": "esc-stale",
            },
        })

        self.handler._dispatch_session_message.assert_called_once_with(
            self.task_id,
            "Please continue with a status update",
            session_id=self.session_id,
            attachment_refs=None,
            message_metadata=None,
            user_message_id=ANY,
            user_message_created_at=ANY,
            run_engine=self.engine,
            run_project_id="alpha",
        )

        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE sender = 'user' ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0]) if row and row[0] else {}
        self.assertEqual(metadata, {})

    async def test_session_send_targets_exact_pending_escalation_by_id(self) -> None:
        """Explicit escalation metadata should resolve only the selected approval card."""
        first = self.handler._remember_pending_escalation({
            "escalation_id": "esc-first",
            "task_id": self.task_id,
            "source_task_id": self.task_id,
            "message": "Approve tool 'file_read'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
            "escalation_type": "decision_needed",
        })
        second = self.handler._remember_pending_escalation({
            "escalation_id": "esc-second",
            "task_id": self.task_id,
            "source_task_id": self.task_id,
            "message": "Approve tool 'file_search'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
            "escalation_type": "decision_needed",
        })

        try:
            ws = MagicMock()
            await self.handler._handle_session_send(ws, {
                "project_id": "alpha",
                "task_id": self.task_id,
                "content": "Approve once",
                "metadata": {
                    "response_to_checkpoint_id": "esc-first",
                    "response_to_checkpoint_type": "human_escalation",
                    "response_to_escalation_id": "esc-first",
                },
            })

            self.assertTrue(first["future"].done())
            self.assertEqual(first["future"].result(), "approve_once")
            self.assertFalse(second["future"].done())

            cursor = await self.chat_store._db.execute(
                "SELECT metadata FROM messages WHERE sender = 'user' ORDER BY timestamp DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            self.assertIsNotNone(row)
            metadata = json.loads(row[0]) if row[0] else {}
            self.assertEqual(metadata.get("response_to_checkpoint_id"), "esc-first")
            self.assertEqual(metadata.get("response_to_escalation_id"), "esc-first")
        finally:
            if not second["future"].done():
                second["future"].cancel()
            self.handler._pending_escalations.clear()
            self.handler._pending_escalation_order.clear()

    async def test_reused_escalation_id_gets_fresh_future(self) -> None:
        """If an escalation id is ever reused, the handler should reopen the pending state."""
        first = self.handler._remember_pending_escalation({
            "escalation_id": "esc-reused",
            "task_id": self.task_id,
            "source_task_id": self.task_id,
            "message": "Approve tool 'file_read'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
            "escalation_type": "decision_needed",
        })
        first["future"].set_result("approve_once")

        refreshed = self.handler._remember_pending_escalation({
            "escalation_id": "esc-reused",
            "task_id": self.task_id,
            "source_task_id": self.task_id,
            "message": "Approve tool 'file_write'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
            "escalation_type": "decision_needed",
        })

        try:
            self.assertEqual(self.handler._pending_escalation_order.count("esc-reused"), 1)
            self.assertIsNot(first["future"], refreshed["future"])
            self.assertFalse(refreshed["future"].done())
            self.assertEqual(refreshed["message"], "Approve tool 'file_write'?")
        finally:
            refreshed_future = refreshed.get("future")
            if refreshed_future and not refreshed_future.done():
                refreshed_future.cancel()
            self.handler._pending_escalations.clear()
            self.handler._pending_escalation_order.clear()

    async def test_explicit_non_escalation_checkpoint_reply_bypasses_pending_escalation(self) -> None:
        """Recruitment/reorg replies should not be swallowed by an unrelated pending escalation."""
        pending = self.handler._remember_pending_escalation({
            "escalation_id": "esc-tool-approval",
            "task_id": self.task_id,
            "source_task_id": self.task_id,
            "message": "Approve tool 'file_read'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
            "escalation_type": "decision_needed",
        })

        try:
            ws = MagicMock()
            self.handler._dispatch_session_message = AsyncMock()
            self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

            await self.handler._handle_session_send(ws, {
                "project_id": "alpha",
                "task_id": self.task_id,
                "content": "approve",
                "metadata": {
                    "response_to_checkpoint_id": "recruitment-1",
                    "response_to_checkpoint_type": "company_recruitment_confirmation",
                    "checkpoint_reply_kind": "approve",
                    "recruitment_role_agents": {
                        "executor": "codex",
                        "reviewer": "native",
                    },
                },
            })

            self.assertFalse(pending["future"].done())
            self.handler._dispatch_session_message.assert_called_once_with(
                self.task_id,
                "approve",
                session_id=self.session_id,
                attachment_refs=None,
                message_metadata={
                    "response_to_checkpoint_id": "recruitment-1",
                    "response_to_checkpoint_type": "company_recruitment_confirmation",
                    "checkpoint_reply_kind": "approve",
                    "recruitment_role_agents": {
                        "executor": "codex",
                        "reviewer": "native",
                    },
                },
                user_message_id=ANY,
                user_message_created_at=ANY,
                run_engine=self.engine,
                run_project_id="alpha",
            )

            cursor = await self.chat_store._db.execute(
                "SELECT metadata FROM messages WHERE sender = 'user' ORDER BY timestamp DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            self.assertIsNotNone(row)
            metadata = json.loads(row[0]) if row[0] else {}
            self.assertEqual(metadata.get("response_to_checkpoint_id"), "recruitment-1")
            self.assertEqual(metadata.get("response_to_checkpoint_type"), "company_recruitment_confirmation")
            self.assertEqual(metadata.get("checkpoint_reply_kind"), "approve")
            self.assertEqual(
                metadata.get("recruitment_role_agents"),
                {
                    "executor": "codex",
                    "reviewer": "native",
                },
            )
            self.assertIsNone(metadata.get("response_to_escalation_id"))
        finally:
            if not pending["future"].done():
                pending["future"].cancel()
            self.handler._pending_escalations.clear()
            self.handler._pending_escalation_order.clear()

    async def test_checkpoint_reply_marks_original_card_with_submitted_metadata(self) -> None:
        """Checkpoint cards reflect explicit card choices as soon as the reply is sent."""
        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Recruitment Plan",
            metadata={
                "checkpoint_id": "recruitment-2",
                "checkpoint_type": "company_recruitment_confirmation",
                "summary": "Pending staffing approval",
                "proposals": [
                    {
                        "role_id": "executor",
                        "status": "proposed_hire",
                        "rationale": "Need a backend specialist.",
                        "role_labels": ["execute"],
                        "default_agent": "native",
                        "selected_agent": "native",
                    }
                ],
                "staffing_roles": [
                    {
                        "role_id": "executor",
                        "role_label": "Executor",
                        "default_agent": "native",
                        "selected_agent": "native",
                    }
                ],
            },
            project_id="alpha",
        )

        ws = MagicMock()
        self.handler._dispatch_session_message = AsyncMock()
        self.handler._track_session = MagicMock(side_effect=self._discard_session_dispatch)

        await self.handler._handle_session_send(ws, {
            "project_id": "alpha",
            "task_id": self.task_id,
            "content": "approve",
            "metadata": {
                "response_to_checkpoint_id": "recruitment-2",
                "response_to_checkpoint_type": "company_recruitment_confirmation",
                "checkpoint_reply_kind": "approve",
                "recruitment_role_agents": {
                    "executor": "codex",
                },
            },
        })

        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            (checkpoint_message["message_id"],),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0]) if row and row[0] else {}
        self.assertEqual(metadata.get("checkpoint_status"), "responded")
        self.assertTrue(metadata.get("checkpoint_response_message_id"))
        self.assertEqual(metadata.get("checkpoint_id"), "recruitment-2")
        self.assertEqual(metadata.get("checkpoint_reply_kind"), "approve")
        self.assertEqual(metadata.get("recruitment_role_agents"), {"executor": "codex"})
        self.assertEqual(metadata.get("proposals", [{}])[0].get("selected_agent"), "codex")
        self.assertEqual(metadata.get("staffing_roles", [{}])[0].get("selected_agent"), "codex")

        session_id = self.session_id

        class ResolvedCheckpointStore:
            async def get_execution_checkpoints(self, project_id: str = "default") -> list[ExecutionCheckpoint]:
                return [
                    ExecutionCheckpoint(
                        checkpoint_id="recruitment-2",
                        project_id=project_id,
                        session_id=session_id,
                        checkpoint_type="company_recruitment_confirmation",
                        status="resolved",
                    )
                ]

        updated = await self.handler._mark_checkpoint_card_after_engine_response(
            channel_id=f"session:{self.task_id}",
            project_id="alpha",
            engine=SimpleNamespace(store=ResolvedCheckpointStore()),
            message_metadata={
                "response_to_checkpoint_id": "recruitment-2",
                "response_to_checkpoint_type": "company_recruitment_confirmation",
                "checkpoint_reply_kind": "approve",
                "recruitment_role_agents": {
                    "executor": "codex",
                },
            },
            response_message_id="user-response-1",
        )
        self.assertIsNotNone(updated)
        assert updated is not None
        updated_metadata = updated["metadata"]
        self.assertEqual(updated_metadata.get("checkpoint_status"), "responded")
        self.assertEqual(updated_metadata.get("checkpoint_response_message_id"), "user-response-1")
        self.assertIn("checkpoint_responded_at", updated_metadata)
        self.assertEqual(updated_metadata.get("checkpoint_reply_kind"), "approve")
        self.assertEqual(updated_metadata.get("recruitment_role_agents"), {"executor": "codex"})
        self.assertEqual(updated_metadata.get("proposals", [{}])[0].get("selected_agent"), "codex")
        self.assertEqual(updated_metadata.get("staffing_roles", [{}])[0].get("selected_agent"), "codex")

    async def test_superseded_checkpoint_status_is_terminal_in_chat_store(self) -> None:
        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Pending recruitment",
            metadata={
                "checkpoint_id": "recruitment-old",
                "checkpoint_type": "company_recruitment_confirmation",
                "summary": "Pending staffing approval",
            },
            project_id="alpha",
        )

        updated = await self.chat_store.update_checkpoint_status(
            "recruitment-old",
            channel_id=f"session:{self.task_id}",
            checkpoint_type="company_recruitment_confirmation",
            status="superseded",
            response_metadata={"checkpoint_reply_kind": "feedback"},
            project_id="alpha",
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["message_id"], checkpoint_message["message_id"])
        self.assertEqual(updated["metadata"].get("checkpoint_status"), "superseded")
        self.assertEqual(updated["metadata"].get("checkpoint_reply_kind"), "feedback")
        unresolved = await self.chat_store.get_unresolved_checkpoint_messages(
            f"session:{self.task_id}",
            project_id="alpha",
            checkpoint_type="company_recruitment_confirmation",
        )
        self.assertEqual(unresolved, [])

    async def test_ignored_checkpoint_status_is_terminal_in_chat_store(self) -> None:
        checkpoint_message = await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Pending delivery review",
            metadata={
                "checkpoint_id": "delivery-ignore",
                "checkpoint_type": "company_delivery_feedback",
                "summary": "Pending self-evolution review",
            },
            project_id="alpha",
        )

        updated = await self.chat_store.update_checkpoint_status(
            "delivery-ignore",
            channel_id=f"session:{self.task_id}",
            checkpoint_type="company_delivery_feedback",
            status="ignored",
            response_metadata={"checkpoint_reply_kind": "ignore"},
            project_id="alpha",
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["message_id"], checkpoint_message["message_id"])
        self.assertEqual(updated["metadata"].get("checkpoint_status"), "ignored")
        self.assertEqual(updated["metadata"].get("checkpoint_reply_kind"), "ignore")
        unresolved = await self.chat_store.get_unresolved_checkpoint_messages(
            f"session:{self.task_id}",
            project_id="alpha",
            checkpoint_type="company_delivery_feedback",
        )
        self.assertEqual(unresolved, [])

    async def test_session_send_empty_content_ignored(self) -> None:
        """Empty content should be silently ignored."""
        ws = MagicMock()
        self.handler._track = MagicMock()

        await self.handler._handle_session_send(ws, {
            "task_id": self.task_id,
            "content": "",
        })

        # No message should be inserted
        cursor = await self.chat_store._db.execute("SELECT COUNT(*) FROM messages")
        count = (await cursor.fetchone())[0]
        self.assertEqual(count, 0)

    async def test_session_send_missing_task_ignored(self) -> None:
        """Missing task_id should be silently ignored."""
        ws = MagicMock()
        self.handler._track = MagicMock()

        await self.handler._handle_session_send(ws, {
            "content": "Hello",
        })

        # No message should be inserted
        cursor = await self.chat_store._db.execute("SELECT COUNT(*) FROM messages")
        count = (await cursor.fetchone())[0]
        self.assertEqual(count, 0)

    async def test_track_session_keeps_existing_background_tasks(self) -> None:
        """A newer session task should not implicitly cancel an older running one."""
        gate = asyncio.Event()

        async def _wait_forever() -> None:
            await gate.wait()

        first = self.handler._track_session(self.task_id, _wait_forever())
        second = self.handler._track_session(self.task_id, _wait_forever())
        await asyncio.sleep(0)

        self.assertFalse(first.cancelled())
        self.assertFalse(second.cancelled())
        self.assertEqual(len(self.handler._task_bg_map[self.task_id]), 2)

        self.handler._cancel_session_tasks(self.task_id)
        with self.assertRaises(asyncio.CancelledError):
            await first
        with self.assertRaises(asyncio.CancelledError):
            await second


# ═══════════════════════════════════════════════════════════════════════
# Test 4: WSHandler — _handle_session_update_title
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerSessionUpdateTitle(unittest.IsolatedAsyncioTestCase):
    """Test manual title update flow."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

        # Pre-create a session
        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        task = Task(
            id=self.task_id,
            title="Old Title",
            session_id=self.session_id,
        )
        await self.store.save_task(task)
        self.memory.sessions[self.session_id] = {
            "session_id": self.session_id,
            "title": "Old Title",
        }
        await self.chat_store.create_session_channel(
            self.task_id,
            "Old Title",
            project_id="test-project",
        )

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_title_update_propagates_to_all_stores(self) -> None:
        """Title update should propagate to task store, memory, and chat_store."""
        ws = MagicMock()
        await self.handler._handle_session_update_title(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "title": "New Title",
        })

        # 1. Task store
        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.title, "New Title")

        # 2. Memory (via update_session_title, NOT ensure_session)
        self.assertTrue(len(self.memory.title_updates) > 0)
        sid, new_title = self.memory.title_updates[-1]
        self.assertEqual(sid, self.session_id)
        self.assertEqual(new_title, "New Title")

        # 3. Chat store channel
        channel_id = f"session:{self.task_id}"
        cursor = await self.chat_store._db.execute(
            "SELECT name FROM channels WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        self.assertEqual(row[0], "New Title")

        # 4. Broadcast
        title_updates = [b for b in self.broadcasts if b["type"] == "session_title_updated"]
        self.assertEqual(len(title_updates), 1)
        self.assertEqual(title_updates[0]["payload"]["title"], "New Title")

    async def test_title_update_empty_title_ignored(self) -> None:
        """Empty title should be silently ignored."""
        ws = MagicMock()
        await self.handler._handle_session_update_title(ws, {
            "task_id": self.task_id,
            "title": "",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.title, "Old Title")

    async def test_title_update_missing_task_id_ignored(self) -> None:
        """Missing task_id should be silently ignored."""
        ws = MagicMock()
        await self.handler._handle_session_update_title(ws, {
            "title": "Orphan Title",
        })

        self.assertEqual(len(self.broadcasts), 0)


# ═══════════════════════════════════════════════════════════════════════
# Test 4b: WSHandler — _handle_session_update_config
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerSessionUpdateConfig(unittest.IsolatedAsyncioTestCase):
    """Test manual session mode/profile update flow."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        task = Task(
            id=self.task_id,
            title="Configurable Session",
            session_id=self.session_id,
            project_id="test-project",
            metadata={
                "exec_mode": "task",
                "company_profile": "corporate",
            },
        )
        await self.store.save_task(task)
        self.memory.sessions[self.session_id] = {
            "session_id": self.session_id,
            "title": "Configurable Session",
            "mode": "primary",
            "metadata": {
                "exec_mode": "task",
                "company_profile": "corporate",
            },
        }

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_update_config_persists_and_broadcasts(self) -> None:
        ws = MagicMock()
        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "company",
            "company_profile": "corporate",
            "preferred_agent": "codex",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.metadata.get("exec_mode"), "company")
        self.assertEqual(task.metadata.get("company_profile"), "corporate")
        self.assertEqual(task.metadata.get("preferred_agent"), "codex")

        session = self.memory.sessions[self.session_id]
        self.assertEqual(session["metadata"].get("exec_mode"), "company")
        self.assertEqual(session["metadata"].get("company_profile"), "corporate")
        self.assertEqual(session["metadata"].get("preferred_agent"), "codex")

        updates = [b for b in self.broadcasts if b["type"] == "session_updated"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["payload"]["task_id"], self.task_id)
        self.assertEqual(updates[0]["payload"]["exec_mode"], "company")
        self.assertEqual(updates[0]["payload"]["company_profile"], "corporate")
        self.assertEqual(updates[0]["payload"]["preferred_agent"], "codex")

        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertTrue(ack_kwargs["ok"])
        self.assertEqual(ack_kwargs["task_id"], self.task_id)
        self.assertEqual(ack_kwargs["exec_mode"], "company")
        self.assertEqual(ack_kwargs["company_profile"], "corporate")
        self.assertEqual(ack_kwargs["preferred_agent"], "codex")

    async def test_update_config_persists_custom_org_id(self) -> None:
        ws = MagicMock()
        self.handler._load_active_org_config_into_engine = MagicMock(return_value=True)
        self.handler._set_active_saved_org_name = AsyncMock()

        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.metadata.get("exec_mode"), "org")
        self.assertEqual(task.metadata.get("company_profile"), "custom")
        self.assertEqual(task.metadata.get("org_id"), "lab")
        self.assertEqual(task.metadata.get("organization_id"), "lab")

        session = self.memory.sessions[self.session_id]
        self.assertEqual(session["metadata"].get("org_id"), "lab")

        updates = [b for b in self.broadcasts if b["type"] == "session_updated"]
        self.assertEqual(updates[-1]["payload"]["org_id"], "lab")
        self.handler._set_active_saved_org_name.assert_awaited_once_with("lab")

        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertTrue(ack_kwargs["ok"])
        self.assertEqual(ack_kwargs["task_id"], self.task_id)
        self.assertEqual(ack_kwargs["exec_mode"], "org")
        self.assertEqual(ack_kwargs["company_profile"], "custom")
        self.assertEqual(ack_kwargs["org_id"], "lab")

    async def test_update_config_company_mode_ignores_stale_custom_profile(self) -> None:
        ws = MagicMock()

        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "company",
            "company_profile": "custom",
            "org_id": "lab",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.metadata.get("exec_mode"), "company")
        self.assertEqual(task.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", task.metadata)
        self.assertNotIn("organization_id", task.metadata)

        update = next(b for b in self.broadcasts if b["type"] == "session_updated")
        self.assertEqual(update["payload"]["exec_mode"], "company")
        self.assertEqual(update["payload"]["company_profile"], "corporate")
        self.assertEqual(update["payload"]["org_id"], "")

    async def test_update_config_task_mode_clears_stale_custom_identity_without_profile(self) -> None:
        task = await self.store.get_task(self.task_id)
        task.metadata.update({
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
            "organization_id": "lab",
        })
        await self.store.save_task(task)
        self.memory.sessions[self.session_id]["metadata"].update({
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
            "organization_id": "lab",
        })

        ws = MagicMock()
        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "task",
            "preferred_agent": "codex",
        })

        updated = await self.store.get_task(self.task_id)
        self.assertEqual(updated.metadata.get("exec_mode"), "task")
        self.assertEqual(updated.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", updated.metadata)
        self.assertNotIn("organization_id", updated.metadata)
        self.assertEqual(updated.metadata.get("selected_execution_agent"), "codex")

        session = self.memory.sessions[self.session_id]
        self.assertEqual(session["metadata"].get("exec_mode"), "task")
        self.assertEqual(session["metadata"].get("company_profile"), "corporate")
        self.assertEqual(session["metadata"].get("org_id"), "")
        self.assertEqual(session["metadata"].get("organization_id"), "")

        update = next(b for b in self.broadcasts if b["type"] == "session_updated")
        self.assertEqual(update["payload"]["exec_mode"], "task")
        self.assertEqual(update["payload"]["company_profile"], "corporate")
        self.assertEqual(update["payload"]["org_id"], "")

    async def test_update_config_clears_task_mode_markers_before_detail_refresh(self) -> None:
        task = await self.store.get_task(self.task_id)
        task.metadata.update({
            "mode": "task",
            "execution_mode": "task_mode",
            "task_mode_contract": "single_full_capability_main_agent",
            "force_native_execution": True,
            "preferred_external_agent": None,
            "agent_selection": {"selected": "native"},
        })
        await self.store.save_task(task)
        await self.chat_store.create_session_channel(
            self.task_id,
            "Configurable Session",
            project_id="test-project",
        )

        ws = MagicMock()
        self.handler._load_active_org_config_into_engine = MagicMock(return_value=True)
        self.handler._set_active_saved_org_name = AsyncMock()
        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
            "preferred_agent": "codex",
        })

        updated = await self.store.get_task(self.task_id)
        self.assertEqual(updated.metadata.get("exec_mode"), "org")
        self.assertEqual(updated.metadata.get("company_profile"), "custom")
        self.assertEqual(updated.metadata.get("org_id"), "lab")
        self.assertNotIn("mode", updated.metadata)
        self.assertNotIn("execution_mode", updated.metadata)
        self.assertNotIn("task_mode_contract", updated.metadata)
        self.assertNotIn("force_native_execution", updated.metadata)
        self.assertNotIn("preferred_external_agent", updated.metadata)
        self.assertNotIn("agent_selection", updated.metadata)
        session = self.memory.sessions[self.session_id]
        self.assertEqual(session["metadata"].get("mode"), "org")
        self.assertEqual(session["metadata"].get("execution_mode"), "company_mode")
        self.assertEqual(session["metadata"].get("task_mode_contract"), "")
        self.assertEqual(session["metadata"].get("selected_execution_agent"), "")

        self.handler._send_ack.reset_mock()
        await self.handler._handle_session_detail(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
        })
        detail_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertTrue(detail_kwargs["ok"])
        self.assertEqual(detail_kwargs["session_state"]["exec_mode"], "org")
        self.assertEqual(detail_kwargs["session_state"]["company_profile"], "custom")

    async def test_update_config_rejects_after_session_has_messages(self) -> None:
        await self.chat_store.insert_message(
            channel_id=f"session:{self.task_id}",
            sender="user",
            sender_name="You",
            content="Start this chat",
            project_id="test-project",
        )

        ws = MagicMock()
        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "company",
            "company_profile": "corporate",
            "preferred_agent": "codex",
        })

        task = await self.store.get_task(self.task_id)
        self.assertEqual(task.metadata.get("exec_mode"), "task")
        self.assertEqual(task.metadata.get("company_profile"), "corporate")
        self.assertNotEqual(task.metadata.get("preferred_agent"), "codex")

        self.assertEqual([b for b in self.broadcasts if b["type"] == "session_updated"], [])
        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertFalse(ack_kwargs["ok"])
        self.assertEqual(ack_kwargs["error"], "session_config_locked")
        self.assertEqual(ack_kwargs["reason"], "message_history")
        self.assertEqual(ack_kwargs["exec_mode"], "task")

    async def test_update_config_rejects_after_session_started_without_messages(self) -> None:
        task = await self.store.get_task(self.task_id)
        task.status = TaskStatus.RUNNING
        await self.store.save_task(task)

        ws = MagicMock()
        await self.handler._handle_session_update_config(ws, {
            "project_id": "test-project",
            "task_id": self.task_id,
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "lab",
        })

        updated = await self.store.get_task(self.task_id)
        self.assertEqual(updated.metadata.get("exec_mode"), "task")
        self.assertEqual(updated.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", updated.metadata)

        self.assertEqual([b for b in self.broadcasts if b["type"] == "session_updated"], [])
        ack_kwargs = self.handler._send_ack.call_args.kwargs
        self.assertFalse(ack_kwargs["ok"])
        self.assertEqual(ack_kwargs["error"], "session_config_locked")
        self.assertEqual(ack_kwargs["reason"], "status:running")


# ═══════════════════════════════════════════════════════════════════════
# Test 5: WSHandler — _handle_session_delete
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerSessionDelete(unittest.IsolatedAsyncioTestCase):
    """Test session deletion."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

        self.task_id = str(uuid.uuid4())
        task = Task(id=self.task_id, title="To Delete")
        await self.store.save_task(task)

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_delete_removes_task_row(self) -> None:
        """Session deletion should hard-delete the task row."""
        ws = MagicMock()
        await self.handler._handle_session_delete(ws, {"project_id": "test-project", "task_id": self.task_id})

        task = await self.store.get_task(self.task_id)
        self.assertIsNone(task)

    async def test_delete_broadcasts_session_deleted(self) -> None:
        """Should broadcast session_deleted event."""
        ws = MagicMock()
        await self.handler._handle_session_delete(ws, {"project_id": "test-project", "task_id": self.task_id})

        types = [b["type"] for b in self.broadcasts]
        self.assertIn("session_deleted", types)

    async def test_delete_missing_task_id_ignored(self) -> None:
        """Missing task_id should not crash or broadcast."""
        ws = MagicMock()
        await self.handler._handle_session_delete(ws, {})

        self.assertEqual(len(self.broadcasts), 0)

    async def test_delete_removes_orphan_sidebar_chat(self) -> None:
        """Deleting a UI-only session channel should be idempotent."""
        project_id = "test-project"
        orphan_task_id = "orphan-task"
        await self.chat_store.create_channel(
            channel_type="session",
            name="Old Chat",
            participants=["user"],
            channel_id=f"session:{orphan_task_id}",
            project_id=project_id,
        )
        await self.chat_store.insert_message(
            f"session:{orphan_task_id}",
            "user",
            "User",
            "stale transcript",
            project_id=project_id,
        )
        await self.chat_store.append_progress(
            orphan_task_id,
            [{"message": "stale progress"}],
            project_id=project_id,
        )

        ws = MagicMock()
        await self.handler._handle_session_delete(ws, {"project_id": project_id, "task_id": orphan_task_id})

        channels = await self.chat_store.get_channels(project_id)
        messages = await self.chat_store.get_messages(project_id)
        progress = await self.chat_store.get_progress(orphan_task_id, project_id=project_id)
        self.assertFalse(any(ch["channel_id"] == f"session:{orphan_task_id}" for ch in channels))
        self.assertFalse(any(msg["channel_id"] == f"session:{orphan_task_id}" for msg in messages))
        self.assertEqual(progress, [])
        self.assertIn("session_deleted", [b["type"] for b in self.broadcasts])

    async def test_delete_removes_comms_directory(self) -> None:
        """Session deletion should rmtree `.opc-comms/<project>/<session>/`."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            project_id = "test-project"
            session_id = "sess-delete"
            layout = file_comms.resolve_layout(workspace_root, project_id, session_id)
            file_comms.ensure_layout(layout, roles=["ceo", "cto"])
            (layout.role_new_dir("ceo") / "hello.md").write_text(
                "---\nfrom_role: cto\nto_role: ceo\n---\nbody\n", encoding="utf-8"
            )
            self.assertTrue(layout.root.is_dir())

            # Replace the pre-saved task (from asyncSetUp) with one that
            # carries workspace/session metadata pointing at the fixture.
            task_id = str(uuid.uuid4())
            self.task_id = task_id
            task = Task(
                id=task_id,
                title="Has Comms",
                session_id=session_id,
                project_id=project_id,
                metadata={"comms_workspace_root": str(workspace_root)},
            )
            await self.store.save_task(task)

            ws = MagicMock()
            await self.handler._handle_session_delete(ws, {"project_id": project_id, "task_id": task_id})

            self.assertFalse(
                layout.root.exists(),
                f"comms dir should be removed: {layout.root}",
            )


class TestWSHandlerSessionStop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict[str, Any]] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_session_stop_preserves_transcript_and_progress(self) -> None:
        ws = MagicMock()
        parent = Task(
            id="stop-parent",
            title="Stop Parent",
            session_id="sess-stop-parent",
            project_id="test-project",
            status=TaskStatus.RUNNING,
        )
        child = Task(
            id="stop-child",
            title="Stop Child",
            session_id="sess-stop-child",
            parent_session_id="sess-stop-parent",
            project_id="test-project",
            status=TaskStatus.RUNNING,
        )
        await self.store.save_task(parent)
        await self.store.save_task(child)
        await self.chat_store.create_session_channel("stop-parent", "Stop Parent", project_id="test-project")
        await self.chat_store.append_progress("stop-parent", [{"type": "thinking", "summary": "Analyzing", "timestamp": time.time()}], project_id="test-project")

        await self.handler._handle_session_stop(ws, {"project_id": "test-project", "task_id": "stop-parent"})

        updated_parent = await self.store.get_task("stop-parent")
        updated_child = await self.store.get_task("stop-child")
        self.assertIsNotNone(updated_parent)
        self.assertIsNotNone(updated_child)
        self.assertEqual(updated_parent.status, TaskStatus.IDLE)
        self.assertEqual(updated_parent.metadata.get("last_stop_reason"), "user_stop")
        self.assertEqual(updated_child.status, TaskStatus.CANCELLED)
        self.assertEqual(self.store.deleted_session_data, [])
        self.assertEqual(len(await self.chat_store.get_progress("stop-parent", project_id="test-project")), 1)

        status_updates = [msg for msg in self.broadcasts if msg.get("type") == "board_task_status_changed"]
        self.assertTrue(any(msg["payload"]["task_id"] == "stop-parent" and msg["payload"]["status"] == "idle" for msg in status_updates))
        self.assertTrue(any(msg["payload"]["task_id"] == "stop-child" and msg["payload"]["status"] == "cancelled" for msg in status_updates))

    async def test_company_identity_failure_is_rejected_without_task_tree_cancel(self) -> None:
        ws = MagicMock()
        task = Task(
            id="company-stop-mismatch",
            title="Company runtime",
            session_id="company-stop-session",
            project_id="test-project",
            status=TaskStatus.RUNNING,
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
        await self.store.save_task(task)
        self.handler._resolve_company_runtime_target = AsyncMock(return_value=None)
        self.handler._cancel_task_tree = AsyncMock()

        await self.handler._handle_session_stop(
            ws,
            {"project_id": "test-project", "task_id": task.id},
        )

        self.handler._cancel_task_tree.assert_not_awaited()
        self.handler._send_ack.assert_awaited_once_with(
            ws,
            ok=False,
            error="company_runtime_identity_mismatch",
            project_id="test-project",
            task_id=task.id,
        )
        persisted = await self.store.get_task(task.id)
        assert persisted is not None
        self.assertEqual(persisted.status, TaskStatus.RUNNING)


# ═══════════════════════════════════════════════════════════════════════
# Test 6: WSHandler — on_opc_event child_session_created
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerChildSessionEvent(unittest.IsolatedAsyncioTestCase):
    """Test the child_session_created event flow through on_opc_event."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    def _make_opc_event(self, event_type: str, payload: dict) -> Any:
        evt = MagicMock()
        evt.event_type = event_type
        evt.payload = payload
        return evt

    async def test_child_session_creates_chat_channel(self) -> None:
        """child_session_created should create a chat_store session channel."""
        # First populate the display map via task_created
        evt0 = self._make_opc_event("task_created", {"task_id": "child-1", "title": "Sub 1"})
        await self.handler.on_opc_event(evt0)

        evt = self._make_opc_event("child_session_created", {
            "session_id": "cs-1",
            "parent_session_id": "ps-1",
            "task_id": "child-1",
            "title": "Sub-task 1",
            "agent_id": "codex",
        })
        await self.handler.on_opc_event(evt)

        channels = await self.chat_store.get_session_channels(project_id="test-project")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channel_id"], "session:child-1")
        self.assertEqual(channels[0]["name"], "Sub-task 1")

    async def test_child_session_broadcasts_board_and_session(self) -> None:
        """Should broadcast child_session_created and session_created for child sessions."""
        # Populate display map
        evt0 = self._make_opc_event("task_created", {"task_id": "child-2", "title": "Sub 2"})
        await self.handler.on_opc_event(evt0)

        self.broadcasts.clear()  # clear the event broadcast from task_created

        evt = self._make_opc_event("child_session_created", {
            "session_id": "cs-2",
            "parent_session_id": "ps-2",
            "task_id": "child-2",
            "title": "Sub-task 2",
            "agent_id": "codex",
        })
        await self.handler.on_opc_event(evt)

        types = [b["type"] for b in self.broadcasts]
        self.assertIn("child_session_created", types)  # visual event
        self.assertIn("session_created", types)
        self.assertNotIn("board_task_created", types)

    async def test_child_session_does_not_increment_display_map(self) -> None:
        """Child session materialization should not re-increment the board counter."""
        # Create 3 tasks via task_created
        for i, tid in enumerate(["t1", "t2", "t3"]):
            evt = self._make_opc_event("task_created", {"task_id": tid, "title": f"T{i}"})
            await self.handler.on_opc_event(evt)

        self.broadcasts.clear()

        # child_session_created for t2 should use display number 2
        evt = self._make_opc_event("child_session_created", {
            "session_id": "cs", "parent_session_id": "ps",
            "task_id": "t2", "title": "Sub", "agent_id": "a",
        })
        await self.handler.on_opc_event(evt)

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(session_msg["payload"]["task_id"], "t2")
        self.assertEqual(session_msg["payload"]["runtime_task_id"], "t2")
        # Counter should still be 3 (not incremented further).
        self.assertEqual(self.adapter.task_display_counter, 3)

    async def test_child_session_includes_parent_session_id(self) -> None:
        """session_created broadcast should include parent_session_id."""
        evt0 = self._make_opc_event("task_created", {"task_id": "c1", "title": "C1"})
        await self.handler.on_opc_event(evt0)
        self.broadcasts.clear()

        evt = self._make_opc_event("child_session_created", {
            "session_id": "child-sess-1",
            "parent_session_id": "parent-sess-1",
            "task_id": "c1", "title": "Child", "agent_id": "a",
        })
        await self.handler.on_opc_event(evt)

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(session_msg["payload"]["session_id"], "child-sess-1")
        self.assertEqual(session_msg["payload"]["parent_session_id"], "parent-sess-1")
        self.assertEqual(session_msg["payload"]["runtime_task_id"], "c1")
        self.assertEqual(session_msg["payload"]["execution_turn_id"], "c1")

    async def test_child_session_broadcasts_selected_execution_agent(self) -> None:
        """session_created/child_session_created should carry the locked execution agent."""
        task = Task(
            id="child-agent-1",
            title="Child Agent Task",
            project_id="test-project",
            assigned_to="executor",
            assigned_external_agent="codex",
            metadata={
                "work_item_projection_id": "build_api",
                "work_item_role_id": "executor",
                "work_item_role_name": "Executor",
                "selected_execution_agent": "codex",
                "employee_assignment": {"name": "API Engineer"},
            },
        )
        await self.store.save_task(task)

        evt0 = self._make_opc_event("task_created", {"task_id": "child-agent-1", "title": "Child Agent Task"})
        await self.handler.on_opc_event(evt0)
        self.broadcasts.clear()

        evt = self._make_opc_event("child_session_created", {
            "session_id": "child-agent-session",
            "parent_session_id": "parent-agent-session",
            "task_id": "child-agent-1",
            "title": "Child Agent Task",
            "agent_id": "executor",
        })
        await self.handler.on_opc_event(evt)

        child_msg = next(b for b in self.broadcasts if b["type"] == "child_session_created")
        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(child_msg["payload"]["selected_execution_agent"], "codex")
        self.assertEqual(session_msg["payload"]["selected_execution_agent"], "codex")
        self.assertEqual(child_msg["payload"]["runtime_task_id"], "child-agent-1")
        self.assertEqual(session_msg["payload"]["runtime_task_id"], "child-agent-1")
        self.assertEqual(child_msg["payload"]["execution_turn_id"], "child-agent-1")
        self.assertEqual(session_msg["payload"]["execution_turn_id"], "child-agent-1")
        self.assertFalse(any(b["type"] == "board_task_created" for b in self.broadcasts))

    async def test_member_session_started_materializes_runtime_child_session(self) -> None:
        """Runtime member bootstrap events should still create a visible child session."""
        task = Task(
            id="projection-task-1",
            title="CEO Intake",
            project_id="test-project",
            session_id="projection-session-1",
            parent_session_id="parent-session-1",
            assigned_to="ceo",
            status=TaskStatus.PENDING,
            metadata={
                "work_item_projection_id": "ceo_intake",
                "work_item_role_id": "ceo",
                "work_item_role_name": "CEO",
                "selected_execution_agent": "codex",
                "employee_assignment": {"name": "Chief Executive Officer"},
                "origin_task_id": "root-task-1",
            },
        )
        await self.store.save_task(task)
        self.handler._session_to_task["parent-session-1"] = "root-task-1"

        evt = self._make_opc_event("runtime_event", {
            "type": "member_session_started",
            "task_id": "projection-task-1",
            "role_id": "ceo",
            "employee_id": "emp-ceo",
        })
        await self.handler.on_opc_event(evt)

        channels = await self.chat_store.get_session_channels(project_id="test-project")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channel_id"], "session:projection-task-1")

        types = [broadcast["type"] for broadcast in self.broadcasts]
        self.assertIn("child_session_created", types)
        self.assertIn("session_created", types)
        self.assertNotIn("board_task_created", types)

        child_msg = next(b for b in self.broadcasts if b["type"] == "child_session_created")
        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertEqual(child_msg["payload"]["task_id"], "projection-task-1")
        self.assertEqual(child_msg["payload"]["runtime_task_id"], "projection-task-1")
        self.assertEqual(child_msg["payload"]["execution_turn_id"], "projection-task-1")
        self.assertEqual(child_msg["payload"]["parent_session_id"], "parent-session-1")
        self.assertEqual(session_msg["payload"]["session_id"], "projection-session-1")
        self.assertEqual(session_msg["payload"]["runtime_task_id"], "projection-task-1")
        self.assertEqual(session_msg["payload"]["execution_turn_id"], "projection-task-1")
        self.assertEqual(session_msg["payload"]["origin_task_id"], "root-task-1")
        self.assertEqual(session_msg["payload"]["preferred_agent"], "native")
        self.assertEqual(session_msg["payload"]["selected_execution_agent"], "codex")

    async def test_runtime_visibility_materialization_is_not_rebroadcast_once_channel_exists(self) -> None:
        """Follow-up runtime events should not recreate the same child session."""
        task = Task(
            id="projection-task-repeat",
            title="CTO Review",
            project_id="test-project",
            session_id="projection-session-repeat",
            parent_session_id="parent-session-repeat",
            assigned_to="cto",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "cto_review",
                "work_item_role_id": "cto",
                "work_item_role_name": "CTO",
            },
        )
        await self.store.save_task(task)

        started_evt = self._make_opc_event("runtime_event", {
            "type": "member_session_started",
            "task_id": "projection-task-repeat",
            "role_id": "cto",
        })
        await self.handler.on_opc_event(started_evt)
        first_broadcast_count = len(self.broadcasts)

        followup_evt = self._make_opc_event("runtime_event", {
            "type": "member_claimed_work_item",
            "task_id": "projection-task-repeat",
            "work_item_projection_id": "cto_review",
            "message_priority": "manager",
        })
        await self.handler.on_opc_event(followup_evt)

        new_broadcasts = self.broadcasts[first_broadcast_count:]
        followup_types = [broadcast["type"] for broadcast in new_broadcasts]
        self.assertNotIn("child_session_created", followup_types)
        self.assertNotIn("board_task_created", followup_types)
        self.assertNotIn("session_created", followup_types)
        self.assertIn("session_progress", followup_types)


# ═══════════════════════════════════════════════════════════════════════
# Test 7b: WSHandler — session_detail
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerSessionDetail(unittest.IsolatedAsyncioTestCase):
    """Test full session-detail payloads used by the child-context viewer."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler

        class TranscriptStore(StubStore):
            def __init__(self) -> None:
                super().__init__()
                self._transcripts: dict[str, list[dict[str, Any]]] = {}

            async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
                return self._transcripts.get(session_id, [])

        self.store = TranscriptStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_session_detail_returns_full_transcript_and_context(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="child-task-1",
            title="Child task",
            project_id="test-project",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
        )
        task.metadata = {
            "handoff_context": "## Context\n\nFull handoff body",
        }
        await self.store.save_task(task)

        self.store._transcripts["child-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="msg-user-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "## Global Intent Summary\n\nBuild the feature"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="msg-agent-1",
                    role="assistant",
                    agent_id="agent-reviewer",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "## Review Notes\n\nLooks good."},
                    ),
                ],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "child-task-1"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "session_detail")
        self.assertEqual(payload["task_id"], "child-task-1")
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(payload["handoff_context"], "## Context\n\nFull handoff body")
        self.assertEqual(payload["messages"][0]["content"], "## Global Intent Summary\n\nBuild the feature")
        self.assertEqual(payload["messages"][1]["sender"], "agent-reviewer")

    async def test_session_detail_reconciles_legacy_inactive_approval_card(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        task = Task(
            id="legacy-approval-task",
            title="Legacy approval session",
            project_id="test-project",
            session_id="legacy-approval-session",
        )
        await self.store.save_task(task)
        await self.chat_store.create_session_channel(
            "legacy-approval-task",
            "Legacy approval session",
            project_id="test-project",
        )
        await self.chat_store.insert_message(
            channel_id="session:legacy-approval-task",
            sender="assistant",
            sender_name="OPC",
            content="Approve external_agent 'opencode'?",
            metadata={
                "checkpoint_type": "human_escalation",
                "checkpoint_id": "esc-legacy-stuck",
                "escalation_id": "esc-legacy-stuck",
                "escalation_type": "decision_needed",
                "prompt": "Approve external_agent 'opencode'?",
                "summary": "Approve external_agent 'opencode'?",
                "options": [{"id": "approve_once", "label": "Approve once"}],
                "default_action": "approve_once",
            },
            message_id="escalation::esc-legacy-stuck",
            project_id="test-project",
        )

        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": "legacy-approval-task"},
        )

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        card = next(
            message
            for message in payload["messages"]
            if message["metadata"].get("checkpoint_id") == "esc-legacy-stuck"
        )
        self.assertEqual(card["metadata"].get("checkpoint_status"), "stale")
        self.assertEqual(
            card["metadata"].get("checkpoint_resolution_reason"),
            "session_detail_reconcile_inactive_escalation",
        )

    async def test_session_detail_reconciles_resolved_staffing_card(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        task = Task(
            id="staffing-task",
            title="Staffing session",
            project_id="test-project",
            session_id="staffing-session",
        )
        task.metadata = {
            "exec_mode": "org",
            "company_profile": "custom",
        }
        await self.store.save_task(task)
        await self.chat_store.create_session_channel(
            "staffing-task",
            "Staffing session",
            project_id="test-project",
        )
        await self.chat_store.insert_message(
            channel_id="session:staffing-task",
            sender="assistant",
            sender_name="OPC",
            content="Select staff manually, or run automatic recruitment.",
            metadata={
                "checkpoint_type": "company_staffing_selection",
                "checkpoint_id": "staffing-checkpoint-1",
            },
            message_id="staffing-card-1",
            project_id="test-project",
        )
        self.store.get_execution_checkpoints = AsyncMock(return_value=[
            ExecutionCheckpoint(
                checkpoint_id="staffing-checkpoint-1",
                project_id="test-project",
                session_id="staffing-session",
                checkpoint_type="company_staffing_selection",
                status="resolved",
            )
        ])
        self.handler.broadcast = AsyncMock()

        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": "staffing-task"},
        )

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        card = next(
            message
            for message in payload["messages"]
            if message["metadata"].get("checkpoint_id") == "staffing-checkpoint-1"
        )
        self.assertEqual(card["metadata"].get("checkpoint_status"), "resolved")
        self.assertEqual(
            card["metadata"].get("checkpoint_resolution_source"),
            "execution_checkpoint_lifecycle",
        )

    async def test_session_detail_uses_controller_registry_for_runtime_control_state(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        task = Task(
            id="custom-running-task",
            title="Custom running session",
            project_id="test-project",
            session_id="custom-running-session",
            status=TaskStatus.RUNNING,
        )
        task.metadata = {
            "exec_mode": "org",
            "company_profile": "custom",
        }
        await self.store.save_task(task)
        self.store.get_execution_checkpoints = AsyncMock(return_value=[])

        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": "custom-running-task"},
        )

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session_state"].get("runtime_control_state"), "idle")
        self.assertFalse(payload["session_state"].get("can_stop"))

        self.engine._task_runtime_is_live = AsyncMock(return_value=True)
        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": "custom-running-task"},
        )
        live_payload = ws.send_json.await_args.args[0]["payload"]
        self.assertEqual(live_payload["session_state"].get("runtime_control_state"), "running")
        self.assertTrue(live_payload["session_state"].get("can_stop"))

    async def test_session_detail_prefers_task_description_for_role_prompt_context(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="role-task-1",
            title="CEO Intake",
            description=(
                "## Global Intent Summary\n"
                "Ship the feature.\n\n"
                "## Your Responsibility\n"
                "Own the CEO intake work item."
            ),
            project_id="test-project",
            session_id="root-session-1",
            parent_session_id="root-session-1",
        )
        await self.store.save_task(task)

        self.store._transcripts["root-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="root-user-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "Original user request"},
                    ),
                ],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "role-task-1"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["handoff_context"],
            "## Global Intent Summary\nShip the feature.\n\n## Your Responsibility\nOwn the CEO intake work item.",
        )
        self.assertEqual(payload["messages"][0]["content"], "Original user request")

    async def test_session_detail_returns_chat_store_deduped_messages_after_sync(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()

        task = Task(
            id="dup-task-1",
            title="Dup task",
            project_id="test-project",
            session_id="dup-session-1",
        )
        await self.store.save_task(task)

        manual_user = await self.chat_store.insert_message(
            channel_id="session:dup-task-1",
            sender="user",
            sender_name="You",
            content="Need a rollout plan",
            message_id="manual-user-1",
            project_id="test-project",
        )
        created_at = datetime.fromtimestamp(manual_user["created_at"])

        self.store._transcripts["dup-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="engine-user-1",
                    role="user",
                    agent_id="",
                    created_at=created_at + timedelta(minutes=5),
                    summary_flag=False,
                    metadata={
                        "ui_message_id": "manual-user-1",
                        "ui_created_at": manual_user["created_at"],
                    },
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "Need a rollout plan"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="engine-assistant-1",
                    role="assistant",
                    agent_id="agent-reviewer",
                    created_at=created_at + timedelta(seconds=1),
                    summary_flag=False,
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "Here is the rollout plan."},
                    ),
                ],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "dup-task-1"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(
            [message["message_id"] for message in payload["messages"]],
            ["manual-user-1", "engine-assistant-1"],
        )
        self.assertEqual(payload["messages"][0]["content"], "Need a rollout plan")
        self.assertEqual(payload["messages"][1]["content"], "Here is the rollout plan.")

    async def test_session_detail_hides_runtime_internal_turns_and_duplicate_reply(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="runtime-task-1",
            title="Runtime task",
            project_id="test-project",
            session_id="runtime-session-1",
        )
        await self.store.save_task(task)

        final_reply = (
            "I can help with many executable tasks, including research, implementation, "
            "verification, and concise delivery summaries for the user."
        )
        self.store._transcripts["runtime-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="top-user-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "What tasks can you help with?"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-user-1",
                    role="user",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={
                            "text": "## Task\nWhat tasks can you help with?\n\n## Work Item Runtime\nTask Mode Execution\n\n## Current Role\ntask_generalist",
                        },
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-assistant-1",
                    role="assistant",
                    agent_id="task_generalist",
                    created_at=base_time + timedelta(seconds=2),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_assistant"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": f"**Top level answer**: {final_reply}"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="top-reply-1",
                    role="assistant",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=3),
                    summary_flag=False,
                    metadata={"kind": "top_level_reply"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": final_reply},
                    ),
                ],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "runtime-task-1"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message_count"], 2)
        self.assertEqual(
            [message["content"] for message in payload["messages"]],
            ["What tasks can you help with?", final_reply],
        )
        self.assertEqual(payload["messages"][1]["sender_name"], "OPC")

    async def test_session_detail_full_includes_runtime_context_and_runtime_reply(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="runtime-task-full-1",
            title="Runtime task",
            project_id="test-project",
            session_id="runtime-session-full-1",
        )
        await self.store.save_task(task)

        final_reply = "I can help with many executable tasks."
        self.store._transcripts["runtime-session-full-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="top-user-full-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "What tasks can you help with?"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-user-full-1",
                    role="user",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={
                            "text": "## Task\nWhat tasks can you help with?\n\n## Work Item Runtime\nTask Mode Execution\n\n## Current Role\ntask_generalist",
                        },
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-assistant-full-1",
                    role="assistant",
                    agent_id="task_generalist",
                    created_at=base_time + timedelta(seconds=2),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_assistant"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": final_reply},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="top-reply-full-1",
                    role="assistant",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=3),
                    summary_flag=False,
                    metadata={"kind": "top_level_reply"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": final_reply},
                    ),
                ],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "runtime-task-full-1", "detail_level": "full"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertEqual(payload["detail_level"], "full")
        self.assertEqual(payload["message_count"], 3)
        self.assertEqual(payload["messages"][1]["sender"], "system")
        self.assertEqual(payload["messages"][1]["metadata"]["transcript_kind"], "runtime_v2_user_turn")
        self.assertIn("Execution Context", payload["messages"][1]["content"])
        self.assertEqual(payload["messages"][2]["content"], final_reply)
        self.assertEqual(payload["messages"][2]["sender_name"], "OPC")
        self.assertEqual(payload["messages"][2]["metadata"]["transcript_kind"], "runtime_v2_assistant")
        self.assertEqual(payload["messages"][2]["metadata"]["detail_visibility"], "summary")

    async def test_company_session_detail_hides_native_raw_turn_in_summary(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="company-runtime-task-1",
            title="Company runtime task",
            project_id="test-project",
            session_id="company-runtime-session-1",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "custom",
                "work_item_projection_id": "chao::intake",
            },
        )
        await self.store.save_task(task)

        self.store._transcripts["company-runtime-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="company-user-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "分析黄金"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="company-raw-1",
                    role="assistant",
                    agent_id="chao",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={
                        "kind": "runtime_v2_company_assistant",
                        "execution_mode": "company_mode",
                    },
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "我先组织研究员处理。"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="company-role-1",
                    role="assistant",
                    agent_id="chao",
                    created_at=base_time + timedelta(seconds=2),
                    summary_flag=False,
                    metadata={"kind": "company_role_result"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "已经派发给研究员，等待下游结果。"})],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "company-runtime-task-1"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertEqual(
            [message["content"] for message in payload["messages"]],
            ["分析黄金", "已经派发给研究员，等待下游结果。"],
        )

        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": "company-runtime-task-1", "detail_level": "full"},
        )
        full_payload = ws.send_json.await_args.args[0]["payload"]
        raw_message = next(
            message
            for message in full_payload["messages"]
            if message["metadata"]["transcript_kind"] == "runtime_v2_company_assistant"
        )
        self.assertEqual(raw_message["sender_name"], "Chao")
        self.assertNotEqual(raw_message["sender_name"], "Task Generalist")

    async def test_sync_task_transcript_messages_filters_runtime_internal_entries(self) -> None:
        base_time = datetime.now()

        task = Task(
            id="sync-runtime-task-1",
            title="Runtime sync task",
            project_id="test-project",
            session_id="sync-runtime-session-1",
        )
        await self.store.save_task(task)

        final_reply = "I can help with many executable tasks."
        self.store._transcripts["sync-runtime-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="top-user-sync-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": "What tasks can you help with?"},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-user-sync-1",
                    role="user",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={
                            "text": "## Task\nWhat tasks can you help with?\n\n## Work Item Runtime\nTask Mode Execution\n\n## Current Role\ntask_generalist",
                        },
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-assistant-sync-1",
                    role="assistant",
                    agent_id="task_generalist",
                    created_at=base_time + timedelta(seconds=2),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_assistant"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": final_reply},
                    ),
                ],
            },
            {
                "message": SimpleNamespace(
                    message_id="top-reply-sync-1",
                    role="assistant",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=3),
                    summary_flag=False,
                    metadata={"kind": "top_level_reply"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={"text": final_reply},
                    ),
                ],
            },
        ]

        synced = await self.handler._sync_task_transcript_messages("sync-runtime-task-1", broadcast=False)
        self.assertEqual(synced, 2)

        messages = await self.chat_store.get_messages(project_id="test-project", limit=10)
        self.assertEqual(
            [message["content"] for message in messages],
            ["What tasks can you help with?", final_reply],
        )

        synced_again = await self.handler._sync_task_transcript_messages("sync-runtime-task-1", broadcast=False)
        self.assertEqual(synced_again, 0)

    async def test_sync_task_transcript_messages_updates_existing_delivery_checkpoint_metadata(self) -> None:
        base_time = datetime.now()

        task = Task(
            id="sync-delivery-task-1",
            title="Delivery sync task",
            project_id="test-project",
            session_id="sync-delivery-session-1",
        )
        await self.store.save_task(task)

        self.store._transcripts["sync-delivery-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="delivery-user-sync-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "Need a delivery"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="delivery-assistant-sync-1",
                    role="assistant",
                    agent_id="chief_analyst",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_assistant"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "Current delivery result."})],
            },
        ]

        synced = await self.handler._sync_task_transcript_messages("sync-delivery-task-1", broadcast=False)
        self.assertEqual(synced, 2)

        checkpoint_meta = {
            "checkpoint_type": "company_delivery_feedback",
            "checkpoint_id": "cp-current-round",
            "waiting_task_id": "sync-delivery-task-1",
            "task_id": "sync-delivery-task-1",
            "delivery_revision": 3,
            "summary": "Current delivery result.",
        }
        updated = await self.handler._sync_task_transcript_messages(
            "sync-delivery-task-1",
            broadcast=False,
            latest_assistant_metadata=checkpoint_meta,
        )

        self.assertEqual(updated, 1)
        messages = await self.chat_store.get_messages(project_id="test-project", limit=10)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["metadata"]["checkpoint_id"], "cp-current-round")
        self.assertEqual(messages[-1]["metadata"]["delivery_revision"], 3)

    async def test_sync_task_transcript_messages_creates_checkpoint_card_when_no_host_message(self) -> None:
        task = Task(
            id="sync-delivery-task-no-host",
            title="Delivery sync task",
            project_id="test-project",
            session_id="sync-delivery-session-no-host",
        )
        await self.store.save_task(task)
        self.store._transcripts["sync-delivery-session-no-host"] = []

        synced = await self.handler._sync_task_transcript_messages(
            "sync-delivery-task-no-host",
            broadcast=False,
            latest_assistant_metadata={
                "checkpoint_type": "company_delivery_feedback",
                "checkpoint_id": "cp-synthetic",
                "waiting_task_id": "sync-delivery-task-no-host",
                "task_id": "sync-delivery-task-no-host",
                "summary": "Review the current delivery.",
                "prompt": "Please review.",
            },
        )

        self.assertEqual(synced, 1)
        messages = await self.chat_store.get_messages(project_id="test-project", limit=10)
        self.assertEqual(messages[-1]["message_id"], "checkpoint::cp-synthetic")
        self.assertEqual(messages[-1]["metadata"]["checkpoint_id"], "cp-synthetic")

    async def test_sync_task_transcript_messages_full_keeps_runtime_context(self) -> None:
        base_time = datetime.now()

        task = Task(
            id="sync-runtime-task-full-1",
            title="Runtime sync task",
            project_id="test-project",
            session_id="sync-runtime-session-full-1",
        )
        await self.store.save_task(task)

        self.store._transcripts["sync-runtime-session-full-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="top-user-sync-full-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_user_turn"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "Need a delivery plan"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="runtime-user-sync-full-1",
                    role="user",
                    agent_id="",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                    metadata={"kind": "runtime_v2_user_turn"},
                ),
                "parts": [
                    SimpleNamespace(
                        part_type="text",
                        payload={
                            "text": "## Task\nNeed a delivery plan\n\n## Work Item Runtime\nTask Mode Execution\n\n## Current Role\ntask_generalist",
                        },
                    ),
                ],
            },
        ]

        synced = await self.handler._sync_task_transcript_messages(
            "sync-runtime-task-full-1",
            broadcast=False,
            detail_level="full",
        )
        self.assertEqual(synced, 2)

        messages = await self.chat_store.get_messages(project_id="test-project", limit=10)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["metadata"]["transcript_kind"], "runtime_v2_user_turn")
        self.assertEqual(messages[1]["metadata"]["detail_visibility"], "full")

    async def test_session_detail_paginates_without_full_sync(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        self.handler._sync_task_transcript_messages = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="paged-task-1",
            title="Paged task",
            project_id="test-project",
            session_id="paged-session-1",
        )
        await self.store.save_task(task)

        self.store._transcripts["paged-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="msg-1",
                    role="user",
                    agent_id="",
                    created_at=base_time,
                    summary_flag=False,
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "oldest"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="msg-2",
                    role="assistant",
                    agent_id="agent-reviewer",
                    created_at=base_time + timedelta(seconds=1),
                    summary_flag=False,
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "middle"})],
            },
            {
                "message": SimpleNamespace(
                    message_id="msg-3",
                    role="assistant",
                    agent_id="agent-reviewer",
                    created_at=base_time + timedelta(seconds=2),
                    summary_flag=False,
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "newest"})],
            },
        ]

        await self.handler._handle_session_detail(ws, {"project_id": "test-project", "task_id": "paged-task-1", "limit": 2})

        first_payload = ws.send_json.await_args_list[0].args[0]["payload"]
        self.assertEqual(first_payload["message_count"], 3)
        self.assertEqual(first_payload["loaded_count"], 2)
        self.assertTrue(first_payload["has_more"])
        self.assertEqual(
            [message["content"] for message in first_payload["messages"]],
            ["middle", "newest"],
        )
        self.handler._sync_task_transcript_messages.assert_not_awaited()

        oldest_loaded = first_payload["messages"][0]
        await self.handler._handle_session_detail(
            ws,
            {
                "project_id": "test-project",
                "task_id": "paged-task-1",
                "limit": 2,
                "before_created_at": oldest_loaded["created_at"],
                "before_message_id": oldest_loaded["message_id"],
            },
        )

        second_payload = ws.send_json.await_args_list[1].args[0]["payload"]
        self.assertEqual(second_payload["message_count"], 3)
        self.assertEqual(second_payload["loaded_count"], 1)
        self.assertFalse(second_payload["has_more"])
        self.assertEqual(
            [message["content"] for message in second_payload["messages"]],
            ["oldest"],
        )

    async def test_session_detail_pages_transcript_and_ui_only_messages_together(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()
        base_time = datetime.now()

        task = Task(
            id="mixed-page-task-1",
            title="Mixed source page",
            project_id="test-project",
            session_id="mixed-page-session-1",
        )
        await self.store.save_task(task)
        self.store._transcripts["mixed-page-session-1"] = [
            {
                "message": SimpleNamespace(
                    message_id="transcript-old",
                    role="assistant",
                    agent_id="agent-reviewer",
                    created_at=base_time,
                    summary_flag=False,
                    metadata={"kind": "top_level_reply"},
                ),
                "parts": [SimpleNamespace(part_type="text", payload={"text": "Persisted reply"})],
            },
        ]
        channel_id = "session:mixed-page-task-1"
        await self.chat_store.create_session_channel(
            task.id,
            task.title,
            project_id="test-project",
        )
        await self.chat_store.insert_message(
            channel_id,
            "system",
            "OPC",
            "Approval required",
            metadata={
                "source": "ui",
                "detail_visibility": "summary",
                "kind": "ui_only_notice",
            },
            message_id="ui-only-mid",
            project_id="test-project",
            created_at=base_time.timestamp() + 1,
        )
        await self.chat_store.insert_message(
            channel_id,
            "system",
            "OPC",
            "Legacy execution notice",
            metadata={"source": "ui", "detail_visibility": "summary"},
            message_id="ui-only-new",
            project_id="test-project",
            created_at=base_time.timestamp() + 2,
        )

        await self.handler._handle_session_detail(
            ws,
            {"project_id": "test-project", "task_id": task.id, "limit": 2},
        )

        first_payload = ws.send_json.await_args_list[0].args[0]["payload"]
        self.assertEqual(first_payload["message_count"], 3)
        self.assertEqual(first_payload["loaded_count"], 2)
        self.assertTrue(first_payload["has_more"])
        self.assertEqual(
            [message["message_id"] for message in first_payload["messages"]],
            ["ui-only-mid", "ui-only-new"],
        )

        oldest_loaded = first_payload["messages"][0]
        await self.handler._handle_session_detail(
            ws,
            {
                "project_id": "test-project",
                "task_id": task.id,
                "limit": 2,
                "before_created_at": oldest_loaded["created_at"],
                "before_message_id": oldest_loaded["message_id"],
            },
        )

        second_payload = ws.send_json.await_args_list[1].args[0]["payload"]
        self.assertEqual(second_payload["message_count"], 3)
        self.assertEqual(second_payload["loaded_count"], 1)
        self.assertFalse(second_payload["has_more"])
        self.assertEqual(
            [message["message_id"] for message in second_payload["messages"]],
            ["transcript-old"],
        )

    async def test_session_detail_returns_silently_when_shutdown_closes_chat_db(self) -> None:
        ws = MagicMock()
        ws.send_json = AsyncMock()

        task = Task(
            id="shutdown-task-1",
            title="Shutdown task",
            project_id="test-project",
            session_id="shutdown-session-1",
        )
        await self.store.save_task(task)

        self.handler._shutting_down = True
        await self.chat_store._db.close()

        await self.handler._handle_session_detail(ws, {"task_id": "shutdown-task-1"})

        ws.send_json.assert_not_awaited()


class TestWSHandlerShutdown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler

        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)

    async def asyncTearDown(self) -> None:
        if self.chat_store._db is not None:
            try:
                await self.chat_store._db.close()
            except Exception:
                pass

    async def test_send_ack_ignores_closed_websocket(self) -> None:
        ws = MagicMock()
        ws.closed = False
        ws.closing = False
        ws.send_json = AsyncMock(side_effect=ConnectionResetError("Cannot write to closing transport"))

        await self.handler._send_ack(ws, ok=True, action="noop")

        ws.send_json.assert_awaited_once()

    async def test_shutdown_closes_clients_and_cancels_non_handoff_messages(self) -> None:
        ws = MagicMock()
        ws.closed = False
        ws.closing = False
        self.handler._root_engine.prepare_active_company_runtimes_for_shutdown = AsyncMock(
            return_value=[]
        )

        async def _close(*_args: Any, **_kwargs: Any) -> None:
            ws.closed = True

        ws.close = AsyncMock(side_effect=_close)
        self.handler._clients.add(ws)

        async def _active_message() -> None:
            await asyncio.Event().wait()

        active_task = asyncio.create_task(_active_message())
        self.handler._active_message_tasks.add(active_task)

        await self.handler.shutdown(timeout=0.5)

        ws.close.assert_awaited_once()
        self.assertTrue(active_task.cancelled())
        self.assertTrue(self.handler._shutting_down)


# ═══════════════════════════════════════════════════════════════════════
# Test 7: WSHandler — _handle_kanban_create_task
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerKanbanCreateTask(unittest.IsolatedAsyncioTestCase):
    """Test kanban task creation with session integration."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_kanban_task_creates_session(self) -> None:
        """Kanban task creation should also create an engine session."""
        ws = MagicMock()
        await self.handler._handle_kanban_create_task(ws, {
            "project_id": "test-project",
            "title": "Fix login bug",
            "board_id": "b1",
            "column_id": "todo",
        })

        # Engine session should exist
        self.assertEqual(len(self.memory.sessions), 1)
        session = list(self.memory.sessions.values())[0]
        self.assertEqual(session["title"], "Fix login bug")

        # Task should have session_id
        tasks = list(self.store._tasks.values())
        self.assertEqual(len(tasks), 1)
        self.assertIsNotNone(tasks[0].session_id)

        # Session_id in task should match session_id in memory
        self.assertEqual(tasks[0].session_id, session["session_id"])

    async def test_kanban_task_session_created_has_session_id(self) -> None:
        """session_created broadcast should include session_id."""
        ws = MagicMock()
        await self.handler._handle_kanban_create_task(ws, {
            "project_id": "test-project",
            "title": "Task with session",
        })

        session_msg = next(b for b in self.broadcasts if b["type"] == "session_created")
        self.assertIn("session_id", session_msg["payload"])
        self.assertIsNotNone(session_msg["payload"]["session_id"])


class TestWSHandlerKanbanSwitchView(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.agent_store.get_all = AsyncMock(return_value=[])
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    @patch("opc.plugins.office_ui.snapshot_builder.build_company_kanban_projection", new_callable=AsyncMock)
    async def test_company_switch_view_keeps_review_column_when_projection_has_no_cards(
        self,
        build_company_projection_mock: AsyncMock,
    ) -> None:
        build_company_projection_mock.return_value = (
            [],
            [
                {"column_id": "todo", "board_id": "test-project", "name": "To do", "sort_order": 0, "is_terminal": False, "color": "#6b7280"},
                {"column_id": "in-progress", "board_id": "test-project", "name": "In progress", "sort_order": 1, "is_terminal": False, "color": "#eab308"},
                {"column_id": "in-review", "board_id": "test-project", "name": "In review", "sort_order": 2, "is_terminal": False, "color": "#f59e0b"},
                {"column_id": "done", "board_id": "test-project", "name": "Done", "sort_order": 3, "is_terminal": True, "color": "#22c55e"},
            ],
            [
                {"board_id": "test-project", "name": "Session Board", "prefix": "OPC", "color": "#4f46e5", "next_task_num": 1, "created_at": 0.0, "updated_at": 0.0},
            ],
            {"run_id": "run-1"},
        )
        ws = MagicMock()
        ws.send_json = AsyncMock()

        await self.handler._handle_kanban_switch_view(ws, {"project_id": "test-project", "level": "global"})

        payload = ws.send_json.await_args.args[0]["payload"]
        self.assertEqual([column["name"] for column in payload["columns"]], ["To do", "In progress", "In review", "Done"])
        self.assertEqual(payload["tasks"], [])


# ═══════════════════════════════════════════════════════════════════════
# Test 8: Memory Manager — ensure_session vs update_session_title
# ═══════════════════════════════════════════════════════════════════════

class TestMemoryManagerTitleOperations(unittest.IsolatedAsyncioTestCase):
    """Test the critical distinction between ensure_session and update_session_title.

    Regression test for Bug4: ensure_session guards with `if title and not existing.title:`
    which prevents title updates on sessions that already have a title.
    """

    async def test_ensure_session_does_not_overwrite_existing_title(self) -> None:
        """ensure_session should NOT overwrite a non-empty existing title.

        This is the bug that was discovered: a session created with title="New Chat"
        could never be renamed via ensure_session.
        """
        from opc.layer5_memory.memory_manager import MemoryManager

        store = StubSessionStore()
        mm = MemoryManager.__new__(MemoryManager)
        mm.store = store
        mm.project_id = "default"

        # Create session with title
        record = StubSessionRecord(session_id="s1", title="New Chat")
        await store.save_session(record)

        # Try to update title via ensure_session — should NOT work
        await mm.ensure_session("s1", title="Better Title")

        session = await store.get_session("s1")
        # Title should still be "New Chat" (ensure_session doesn't overwrite)
        self.assertEqual(session.title, "New Chat")

    async def test_update_session_title_always_overwrites(self) -> None:
        """update_session_title should unconditionally overwrite the title."""
        from opc.layer5_memory.memory_manager import MemoryManager

        store = StubSessionStore()
        mm = MemoryManager.__new__(MemoryManager)
        mm.store = store
        mm.project_id = "default"

        # Create session with title
        record = StubSessionRecord(session_id="s1", title="New Chat")
        await store.save_session(record)

        # Update via update_session_title — should work
        await mm.update_session_title("s1", "Better Title")

        session = await store.get_session("s1")
        self.assertEqual(session.title, "Better Title")

    async def test_update_session_title_nonexistent_session(self) -> None:
        """update_session_title on nonexistent session should not crash."""
        from opc.layer5_memory.memory_manager import MemoryManager

        store = StubSessionStore()
        mm = MemoryManager.__new__(MemoryManager)
        mm.store = store
        mm.project_id = "default"

        # Should not raise
        await mm.update_session_title("nonexistent", "Title")

    async def test_update_session_title_no_store(self) -> None:
        """update_session_title without store should not crash."""
        from opc.layer5_memory.memory_manager import MemoryManager

        mm = MemoryManager.__new__(MemoryManager)
        mm.store = None

        # Should not raise
        await mm.update_session_title("s1", "Title")


# ═══════════════════════════════════════════════════════════════════════
# Test 9: Frontend data mappers — collabSync
# ═══════════════════════════════════════════════════════════════════════

class TestCollabSyncMappers(unittest.TestCase):
    """Test that backend payloads map correctly to frontend types."""

    def test_map_backend_task_with_session_id(self) -> None:
        """Verify snapshot_builder includes session_id in task output."""
        from opc.plugins.office_ui.snapshot_builder import task_to_kanban

        task = Task(
            id="t1",
            title="Test",
            session_id="sess-123",
            project_id="p1",
        )
        result = task_to_kanban(task, display_num=1)

        self.assertEqual(result["session_id"], "sess-123")
        self.assertEqual(result["chat_channel_id"], "session:t1")

    def test_map_backend_task_without_session_id(self) -> None:
        """Task without session_id should still have chat_channel_id fallback."""
        from opc.plugins.office_ui.snapshot_builder import task_to_kanban

        task = Task(id="t2", title="Legacy Task", project_id="p1")
        result = task_to_kanban(task, display_num=2)

        # session_id should be None
        self.assertIsNone(result["session_id"])
        # chat_channel_id should fall back to session:{task_id}
        self.assertEqual(result["chat_channel_id"], "session:t2")

    def test_map_backend_task_includes_work_item_role_identity(self) -> None:
        """Task payload should expose both work-item role id and role name for UI rendering."""
        from opc.plugins.office_ui.snapshot_builder import task_to_kanban

        task = Task(
            id="t3",
            title="Content Execution",
            session_id="sess-role",
            project_id="p1",
            assigned_to="content_specialist",
            metadata={
                "work_item_role_id": "content_specialist",
                "work_item_role_name": "Content Specialist",
                "employee_assignment": {"name": "Book Co-Author", "employee_id": "content-specialist-1"},
            },
        )
        result = task_to_kanban(task, display_num=3)

        self.assertEqual(result["work_item_role_id"], "content_specialist")
        self.assertEqual(result["work_item_role_name"], "Content Specialist")
        self.assertEqual(result["employee_assignment"]["name"], "Book Co-Author")

    def test_map_backend_task_includes_selected_execution_agent(self) -> None:
        """Task payload should expose the effective execution agent chosen for this work item."""
        from opc.plugins.office_ui.snapshot_builder import task_to_kanban

        task = Task(
            id="t4",
            title="Implementation",
            session_id="sess-agent",
            project_id="p1",
            assigned_external_agent="codex",
            metadata={
                "selected_execution_agent": "codex",
                "agent_selection": {"selected": "codex"},
            },
        )
        result = task_to_kanban(task, display_num=4)

        self.assertEqual(result["selected_execution_agent"], "codex")


# ═══════════════════════════════════════════════════════════════════════
# Test 10: End-to-end — Create session → send message → engine responds
# ═══════════════════════════════════════════════════════════════════════

class TestEndToEndSessionFlow(unittest.IsolatedAsyncioTestCase):
    """End-to-end test: create session → send message → verify full data flow."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._send_ack = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_full_flow_create_and_send(self) -> None:
        """Full flow: create session → send message → engine processes with session_id."""
        ws = MagicMock()

        # Step 1: Create session
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "New Chat"})

        # Extract task_id from ack
        ack_call = self.handler._send_ack.call_args
        task_id = ack_call.kwargs.get("task_id") or ack_call[1].get("task_id")
        session_id = ack_call.kwargs.get("session_id") or ack_call[1].get("session_id")
        self.assertIsNotNone(task_id)
        self.assertIsNotNone(session_id)

        # Step 2: Send message (directly call _process_session_message to test engine call)
        self.broadcasts.clear()
        await self.handler._process_session_message(
            task_id, "Build a user dashboard", session_id=session_id
        )

        # Step 3: Verify engine was called with correct session_id
        self.engine.process_message.assert_called_once()
        call_args = self.engine.process_message.call_args
        self.assertEqual(call_args[0][0], "Build a user dashboard")
        self.assertEqual(call_args.kwargs["session_id"], session_id)

        # Step 4: Verify the session returned to idle after the engine call.
        status_msgs = [b for b in self.broadcasts if b["type"] == "board_task_status_changed"]
        self.assertTrue(
            any(msg["payload"]["task_id"] == task_id and msg["payload"]["status"] == "idle" for msg in status_msgs)
        )

    async def test_full_flow_multiple_sessions_isolated(self) -> None:
        """Multiple sessions should have independent session_ids."""
        ws = MagicMock()

        # Create two sessions
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Session A"})
        await self.handler._handle_create_session(ws, {"project_id": "test-project", "title": "Session B"})

        # Both should have unique session_ids in memory
        self.assertEqual(len(self.memory.sessions), 2)
        session_ids = list(self.memory.sessions.keys())
        self.assertNotEqual(session_ids[0], session_ids[1])

        # Both should have unique tasks
        self.assertEqual(len(self.store._tasks), 2)
        tasks = list(self.store._tasks.values())
        self.assertNotEqual(tasks[0].session_id, tasks[1].session_id)


class TestOfficeServiceExecutionIdentity(unittest.IsolatedAsyncioTestCase):
    """Service execution paths should honor the task/session identity."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
        from opc.plugins.office_ui.services.runtime import RuntimeService
        from opc.plugins.office_ui.services.session import SessionService

        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.context = OfficeServiceContext(
            engine=self.engine,
            agent_store=MagicMock(),
            chat_store=self.chat_store,
            event_adapter=self.adapter,
            mode_state=ModeState(exec_mode="task", company_profile="corporate", task_preferred_agent="native"),
        )
        self.session_service = SessionService(self.context)
        self.runtime_service = RuntimeService(self.context, self.session_service)

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_continue_uses_scope_config_for_markerless_cancelled_ui_anchor(self) -> None:
        anchor = Task(
            id="service-ui-anchor",
            title="Company chat",
            session_id="service-runtime-session",
            project_id="test-project",
            status=TaskStatus.CANCELLED,
            metadata={},
        )
        final_decider = Task(
            id="service-final-decider",
            title="Final decision",
            session_id="service-runtime-session",
            parent_session_id="service-runtime-session",
            project_id="test-project",
            status=TaskStatus.BLOCKED,
            linked_work_item_id="service-work-item",
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "shared_role_session": True,
            },
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="service-runtime-checkpoint",
            project_id="test-project",
            session_id="service-runtime-session",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id=final_decider.id,
            payload={"parent_session_id": "service-runtime-session"},
        )
        await self.store.save_task(anchor)
        await self.store.save_task(final_decider)
        await self.store.save_execution_checkpoint(checkpoint)

        await self.session_service.continue_run(
            project_id="test-project",
            task_id=anchor.id,
            runtime_session_id="service-runtime-session",
            checkpoint_id=checkpoint.checkpoint_id,
            content="continue",
        )

        call = self.engine.process_message.await_args
        self.assertEqual(call.kwargs["mode"], "company")
        self.assertEqual(call.kwargs["session_id"], "service-runtime-session")
        self.assertEqual(call.kwargs["origin_task_id"], anchor.id)
        self.assertEqual(
            call.kwargs["message_metadata"]["response_to_checkpoint_id"],
            checkpoint.checkpoint_id,
        )
        persisted_anchor = await self.store.get_task(anchor.id)
        assert persisted_anchor is not None
        self.assertEqual(persisted_anchor.status, TaskStatus.CANCELLED)

    async def test_company_continue_preserves_requested_work_item_as_ui_channel(self) -> None:
        anchor = Task(
            id="service-channel-anchor",
            title="Company chat",
            session_id="service-channel-runtime",
            project_id="test-project",
            status=TaskStatus.CANCELLED,
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
        work_item = Task(
            id="service-channel-work-item",
            title="Shared final decision",
            session_id="service-channel-runtime",
            parent_session_id="service-channel-runtime",
            project_id="test-project",
            status=TaskStatus.BLOCKED,
            linked_work_item_id="service-channel-wi",
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "shared_role_session": True,
            },
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="service-channel-checkpoint",
            project_id="test-project",
            session_id="service-channel-runtime",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id=work_item.id,
            payload={"parent_session_id": "service-channel-runtime"},
        )
        await self.store.save_task(anchor)
        await self.store.save_task(work_item)
        await self.store.save_execution_checkpoint(checkpoint)

        result = await self.session_service.continue_run(
            project_id="test-project",
            task_id=work_item.id,
            runtime_session_id="service-channel-runtime",
            checkpoint_id=checkpoint.checkpoint_id,
            content="continue",
        )

        call = self.engine.process_message.await_args
        self.assertEqual(result.payload["task_id"], work_item.id)
        self.assertEqual(call.kwargs["session_id"], "service-channel-runtime")
        self.assertEqual(call.kwargs["origin_task_id"], anchor.id)

    async def test_company_identity_failure_never_falls_back_to_task_mode_control(self) -> None:
        from opc.plugins.office_ui.services.models import ServiceError

        task = Task(
            id="service-company-control",
            title="Company control",
            session_id="service-company-session",
            project_id="test-project",
            status=TaskStatus.RUNNING,
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
        await self.store.save_task(task)
        mismatch = ServiceError(
            "company_runtime_identity_mismatch",
            "identity mismatch",
        )
        self.session_service._resolve_company_runtime_target = AsyncMock(
            side_effect=mismatch,
        )

        with self.assertRaises(ServiceError) as stop_error:
            await self.session_service.stop(
                project_id="test-project",
                task_id=task.id,
            )
        self.assertEqual(stop_error.exception.code, "company_runtime_identity_mismatch")

        with self.assertRaises(ServiceError) as continue_error:
            await self.session_service.continue_run(
                project_id="test-project",
                task_id=task.id,
            )
        self.assertEqual(continue_error.exception.code, "company_runtime_identity_mismatch")
        persisted = await self.store.get_task(task.id)
        assert persisted is not None
        self.assertEqual(persisted.status, TaskStatus.RUNNING)

    async def test_continue_rejects_runtime_custom_org_without_durable_org_id(self) -> None:
        task = Task(
            id="service-continue-org-missing-id",
            title="Broken custom runtime",
            session_id="service-continue-org-runtime",
            project_id="test-project",
            metadata={"exec_mode": "org", "company_profile": "custom"},
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="service-continue-org-checkpoint",
            project_id="test-project",
            session_id=task.session_id,
            checkpoint_type="company_runtime_suspended",
            status="pending",
            task_id=task.id,
        )
        await self.store.save_task(task)
        self.session_service._resolve_company_runtime_target = AsyncMock(return_value={
            "runtime_session_id": task.session_id,
            "ui_anchor_task_id": task.id,
            "config_task": task,
            "checkpoint": checkpoint,
            "affected_task_ids": [task.id],
        })

        with self.assertRaises(ServiceError) as raised:
            await self.session_service.continue_run(
                project_id="test-project",
                task_id=task.id,
                content="continue",
            )

        self.assertEqual(raised.exception.code, "org_id_required")
        self.engine.process_message.assert_not_called()

    async def test_session_send_from_work_item_uses_runtime_checkpoint_identity(self) -> None:
        anchor = Task(
            id="service-send-anchor",
            title="Company chat",
            session_id="service-send-runtime",
            project_id="test-project",
            status=TaskStatus.CANCELLED,
            metadata={},
        )
        final_decider = Task(
            id="service-send-final",
            title="Final decider",
            session_id="service-send-runtime",
            parent_session_id="service-send-runtime",
            project_id="test-project",
            linked_work_item_id="service-send-final-wi",
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "final",
                "shared_role_session": True,
            },
        )
        worker = Task(
            id="service-send-worker",
            title="Worker",
            session_id="service-send-runtime:role:worker",
            parent_session_id="service-send-runtime",
            project_id="test-project",
            linked_work_item_id="service-send-worker-wi",
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "worker",
            },
        )
        original_worker_metadata = dict(worker.metadata)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="service-send-checkpoint",
            project_id="test-project",
            session_id="service-send-runtime",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id=final_decider.id,
            payload={"parent_session_id": "service-send-runtime"},
        )
        for task in (anchor, final_decider, worker):
            await self.store.save_task(task)
        await self.store.save_execution_checkpoint(checkpoint)

        result = await self.session_service.send(
            project_id="test-project",
            task_id=worker.id,
            content="revise and continue",
        )

        call = self.engine.process_message.await_args
        self.assertEqual(result.payload["task_id"], worker.id)
        self.assertEqual(result.payload["session_id"], "service-send-runtime")
        self.assertEqual(call.kwargs["session_id"], "service-send-runtime")
        self.assertEqual(call.kwargs["origin_task_id"], anchor.id)
        self.assertEqual(call.kwargs["mode"], "company")
        self.assertEqual(call.kwargs["message_metadata"], {
            "response_to_checkpoint_id": checkpoint.checkpoint_id,
            "response_to_checkpoint_type": checkpoint.checkpoint_type,
        })
        persisted_worker = await self.store.get_task(worker.id)
        assert persisted_worker is not None
        self.assertEqual(persisted_worker.metadata, original_worker_metadata)

    async def test_session_send_rejects_resuming_checkpoint_without_engine_fallback(self) -> None:
        anchor = Task(
            id="service-resuming-anchor",
            title="Company chat",
            session_id="service-resuming-runtime",
            project_id="test-project",
            status=TaskStatus.CANCELLED,
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="service-resuming-checkpoint",
            project_id="test-project",
            session_id="service-resuming-runtime",
            checkpoint_type="company_runtime_suspended",
            status="resuming",
            task_id=anchor.id,
            payload={"parent_session_id": "service-resuming-runtime"},
        )
        await self.store.save_task(anchor)
        await self.store.save_execution_checkpoint(checkpoint)

        with self.assertRaises(ServiceError) as raised:
            await self.session_service.send(
                project_id="test-project",
                task_id=anchor.id,
                content="continue twice",
            )

        self.assertEqual(
            raised.exception.code,
            "company_runtime_checkpoint_not_pending",
        )
        self.engine.process_message.assert_not_called()

    async def test_session_send_rejects_cancelled_task_mode_session(self) -> None:
        task = Task(
            id="service-cancelled-task-mode",
            title="Cancelled task chat",
            session_id="service-cancelled-task-session",
            project_id="test-project",
            status=TaskStatus.CANCELLED,
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "origin_task_id": "service-cancelled-task-mode",
            },
        )
        await self.store.save_task(task)

        with self.assertRaises(ServiceError) as raised:
            await self.session_service.send(
                project_id="test-project",
                task_id=task.id,
                content="must stay cancelled",
            )

        self.assertEqual(raised.exception.code, "session_ended")
        self.engine.process_message.assert_not_called()

    async def test_session_send_company_identity_mismatch_fails_closed(self) -> None:
        task = Task(
            id="service-send-mismatch",
            title="Company chat",
            session_id="service-send-mismatch-runtime",
            project_id="test-project",
            metadata={"exec_mode": "company", "company_profile": "corporate"},
        )
        await self.store.save_task(task)
        self.session_service._resolve_company_runtime_target = AsyncMock(
            side_effect=ServiceError(
                "company_runtime_identity_mismatch",
                "identity mismatch",
            ),
        )

        with self.assertRaises(ServiceError) as raised:
            await self.session_service.send(
                project_id="test-project",
                task_id=task.id,
                content="do not fall back",
            )

        self.assertEqual(
            raised.exception.code,
            "company_runtime_identity_mismatch",
        )
        self.engine.process_message.assert_not_called()

    async def test_session_send_prefers_persisted_org_identity_over_call_defaults(self) -> None:
        task = Task(
            id="task-org-send",
            title="Org Send",
            session_id="session-org-send",
            project_id="test-project",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
                "preferred_agent": "codex",
            },
        )
        await self.store.save_task(task)

        await self.session_service.send(
            project_id="test-project",
            task_id=task.id,
            content="run org",
            mode="company",
            company_profile="corporate",
            preferred_agent="codex",
        )

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "quantum_harbor")
        self.assertIsNone(call_kwargs["preferred_agent"])
        updated = await self.store.get_task(task.id)
        self.assertEqual(updated.metadata.get("exec_mode"), "org")
        self.assertEqual(updated.metadata.get("company_profile"), "custom")
        self.assertEqual(updated.metadata.get("org_id"), "quantum_harbor")
        self.assertEqual(updated.org_id, "quantum_harbor")

    async def test_session_send_uses_requested_company_for_unconfigured_task(self) -> None:
        task = Task(
            id="task-company-send",
            title="Company Send",
            session_id="session-company-send",
            project_id="test-project",
            metadata={},
        )
        await self.store.save_task(task)

        await self.session_service.send(
            project_id="test-project",
            task_id=task.id,
            content="run company",
            mode="company",
            company_profile="corporate",
            preferred_agent="codex",
        )

        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "company")
        self.assertEqual(call_kwargs["company_profile"], "corporate")
        self.assertIsNone(call_kwargs["org_id"])
        self.assertIsNone(call_kwargs["preferred_agent"])
        updated = await self.store.get_task(task.id)
        self.assertEqual(updated.metadata.get("exec_mode"), "company")
        self.assertEqual(updated.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", updated.metadata)
        self.assertIsNone(updated.org_id)

    async def test_session_send_canonicalizes_stale_company_identity_before_run(self) -> None:
        task = Task(
            id="task-company-stale-org",
            title="Company Stale Org",
            session_id="session-company-stale-org",
            project_id="test-project",
            metadata={
                "exec_mode": "company",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
            },
            org_id="quantum_harbor",
        )
        await self.store.save_task(task)

        await self.session_service.send(
            project_id="test-project",
            task_id=task.id,
            content="run company",
            mode="org",
            company_profile="custom",
        )

        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "company")
        self.assertEqual(call_kwargs["company_profile"], "corporate")
        self.assertIsNone(call_kwargs["org_id"])
        updated = await self.store.get_task(task.id)
        self.assertEqual(updated.metadata.get("exec_mode"), "company")
        self.assertEqual(updated.metadata.get("company_profile"), "corporate")
        self.assertNotIn("org_id", updated.metadata)
        self.assertIsNone(updated.org_id)

    async def test_session_send_rejects_custom_org_without_org_id(self) -> None:
        from opc.plugins.office_ui.services.models import ServiceError

        task = Task(
            id="task-org-missing-id",
            title="Broken Org",
            session_id="session-org-missing-id",
            project_id="test-project",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
            },
        )
        await self.store.save_task(task)

        with self.assertRaises(ServiceError) as ctx:
            await self.session_service.send(
                project_id="test-project",
                task_id=task.id,
                content="run org",
            )
        self.assertEqual(ctx.exception.code, "org_id_required")
        self.engine.process_message.assert_not_called()

    async def test_runtime_run_task_prefers_task_org_identity_over_global_mode(self) -> None:
        self.context.mode_state.exec_mode = "company"
        self.context.mode_state.company_profile = "corporate"
        task = Task(
            id="task-org-runtime",
            title="Org Runtime",
            description="Use saved org",
            session_id="session-org-runtime",
            project_id="test-project",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
            },
        )
        await self.store.save_task(task)

        await self.runtime_service.run_task(project_id="test-project", task_id=task.id)

        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "quantum_harbor")

    async def test_runtime_run_task_does_not_apply_global_org_to_plain_task(self) -> None:
        self.context.mode_state.exec_mode = "org"
        self.context.mode_state.company_profile = "custom"
        self.context.mode_state.task_preferred_agent = "codex"
        task = Task(
            id="task-plain-runtime",
            title="Plain Runtime",
            description="Should stay task mode",
            session_id="session-plain-runtime",
            project_id="test-project",
            metadata={},
        )
        await self.store.save_task(task)

        await self.runtime_service.run_task(project_id="test-project", task_id=task.id)

        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "task")
        self.assertIsNone(call_kwargs["company_profile"])
        self.assertIsNone(call_kwargs["org_id"])
        self.assertEqual(call_kwargs["preferred_agent"], "native")


# ═══════════════════════════════════════════════════════════════════════
# Test 11: WSHandler — _run_task session_id lookup
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerRunTask(unittest.IsolatedAsyncioTestCase):
    """Test _run_task correctly looks up and passes session_id."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))
        self.handler._resolve_force_mode = MagicMock(return_value=(None, None))
        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        await self.store.save_task(
            Task(id=self.task_id, title="Existing Session", session_id=self.session_id)
        )

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_run_task_passes_session_id(self) -> None:
        """_run_task should look up session_id from task and pass to engine."""
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(id=task_id, title="Test", session_id=session_id)
        await self.store.save_task(task)

        await self.handler._run_task("Test", "Description", "single", "classic", task_id)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["session_id"], session_id)

    async def test_run_task_without_task_id(self) -> None:
        """_run_task without task_id should pass session_id=None."""
        await self.handler._run_task("Test", "Description", "single", "classic", None)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertIsNone(call_kwargs.get("session_id"))

    async def test_run_task_prefers_task_level_session_config(self) -> None:
        """_run_task should use persisted task mode/profile over the global default."""
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="Custom Flow",
            session_id=session_id,
            metadata={
                "exec_mode": "custom",
                "company_profile": "corporate",
                "org_id": "quantum_harbor",
            },
        )
        await self.store.save_task(task)
        self.handler._exec_mode = "task"
        self.handler._company_profile = "corporate"

        await self.handler._run_task("Custom Flow", "Description", "task", "corporate", task_id)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["session_id"], session_id)
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "quantum_harbor")
        self.assertIsNone(call_kwargs["preferred_agent"])

    async def test_run_task_uses_task_org_id_fallback_for_custom_identity(self) -> None:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="Org Fallback",
            session_id=session_id,
            project_id="test-project",
            metadata={},
            org_id="quantum_harbor",
        )
        await self.store.save_task(task)
        self.handler._exec_mode = "company"
        self.handler._company_profile = "corporate"

        await self.handler._run_task("Org Fallback", "Description", "company", "corporate", task_id)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "org")
        self.assertEqual(call_kwargs["company_profile"], "custom")
        self.assertEqual(call_kwargs["org_id"], "quantum_harbor")

    async def test_run_task_fails_closed_for_custom_identity_without_org_id(self) -> None:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="Missing Org Runtime",
            session_id=session_id,
            project_id="test-project",
            metadata={"exec_mode": "org", "company_profile": "custom"},
        )
        await self.store.save_task(task)
        self.handler.services_context.get_active_saved_org_name = AsyncMock(
            return_value="wrong-active-org"
        )

        await self.handler._run_task(
            "Missing Org Runtime",
            "Description",
            "org",
            "custom",
            task_id,
        )

        self.engine.process_message.assert_not_called()
        self.handler.services_context.get_active_saved_org_name.assert_not_awaited()

    async def test_run_task_explicit_company_clears_stale_custom_fields(self) -> None:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="Corporate Fallback",
            session_id=session_id,
            project_id="test-project",
            metadata={
                "exec_mode": "company",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
            },
            org_id="quantum_harbor",
        )
        await self.store.save_task(task)

        await self.handler._run_task("Corporate Fallback", "Description", "org", "custom", task_id)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "company")
        self.assertEqual(call_kwargs["company_profile"], "corporate")
        self.assertIsNone(call_kwargs["org_id"])
        self.assertIsNone(call_kwargs["preferred_agent"])

    async def test_session_send_passes_task_mode_preferred_agent(self) -> None:
        task = await self.store.get_task(self.task_id)
        task.metadata = {
            "exec_mode": "task",
            "company_profile": "corporate",
            "preferred_agent": "codex",
        }
        await self.store.save_task(task)

        await self.handler._process_session_message(
            self.task_id,
            "use codex in task mode",
            session_id=self.session_id,
        )

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "project")
        self.assertEqual(call_kwargs["preferred_agent"], "codex")

    async def test_process_session_message_serializes_status_inside_task_lock(self) -> None:
        task = await self.store.get_task(self.task_id)
        task.metadata = {
            "exec_mode": "company",
            "company_profile": "corporate",
        }
        await self.store.save_task(task)

        async def process_message(content: str, **_kwargs: Any) -> str:
            await asyncio.sleep(0.01)
            return f"ok:{content}"

        self.engine.process_message = AsyncMock(side_effect=process_message)

        await asyncio.gather(
            self.handler._process_session_message(self.task_id, "first", session_id=self.session_id),
            self.handler._process_session_message(self.task_id, "second", session_id=self.session_id),
        )

        statuses = [
            item["payload"]["status"]
            for item in self.broadcasts
            if item.get("type") == "board_task_status_changed"
            and item.get("payload", {}).get("task_id") == self.task_id
        ]
        self.assertGreaterEqual(len(statuses), 4)
        self.assertEqual(statuses[:4], ["running", "idle", "running", "idle"])

    async def test_initial_project_state_pushes_index_collab_and_org_info(self) -> None:
        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        self.handler._client_project_ids[ws] = "test-project"
        self.handler._build_org_info_payload = AsyncMock(return_value={"organization_id": "corporate"})

        with patch(
            "opc.plugins.office_ui.ws_handler.build_project_index_sync",
            new=AsyncMock(return_value={"ok": True, "channels": [], "messages": [], "boards": [], "columns": [], "tasks": [], "sessions": []}),
        ), patch(
            "opc.plugins.office_ui.ws_handler.build_collab_sync",
            new=AsyncMock(return_value={"ok": True, "channels": [], "messages": [], "boards": [], "columns": [], "tasks": [], "sessions": []}),
        ):
            await self.handler._send_initial_project_state_for_client(ws, self.engine, "test-project")

        sent_types = [call.args[0]["type"] for call in ws.send_json.await_args_list]
        self.assertEqual(sent_types, ["project_index_push", "collab_sync_push", "org_info"])

    async def test_project_index_request_does_not_cancel_initial_state_push(self) -> None:
        ws = type("FakeWS", (), {"closed": False, "closing": False})()
        ws.send_json = AsyncMock()
        self.handler._client_project_ids[ws] = "test-project"
        initial_task = asyncio.create_task(asyncio.sleep(30))
        self.handler._client_initial_state_tasks[ws] = initial_task

        try:
            with patch(
                "opc.plugins.office_ui.ws_handler.build_project_index_sync",
                new=AsyncMock(return_value={"ok": True, "channels": [], "messages": [], "boards": [], "columns": [], "tasks": [], "sessions": []}),
            ):
                await self.handler._handle_project_index(ws, {"project_id": "test-project"})
                await asyncio.sleep(0)
            self.assertFalse(initial_task.cancelled())
            self.assertIs(self.handler._client_initial_state_tasks.get(ws), initial_task)
        finally:
            initial_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await initial_task

    async def test_run_task_uses_task_mode_preferred_agent(self) -> None:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="Codex Task",
            session_id=session_id,
            metadata={
                "exec_mode": "task",
                "company_profile": "corporate",
                "preferred_agent": "codex",
            },
        )
        await self.store.save_task(task)

        await self.handler._run_task("Codex Task", "Description", "task", "corporate", task_id)

        self.engine.process_message.assert_called_once()
        call_kwargs = self.engine.process_message.call_args.kwargs
        self.assertEqual(call_kwargs["session_id"], session_id)
        self.assertEqual(call_kwargs["mode"], "project")
        self.assertEqual(call_kwargs["preferred_agent"], "codex")

    async def test_run_task_backfills_persisted_transcript_result(self) -> None:
        task_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        task = Task(
            id=task_id,
            title="External Delivery",
            session_id=session_id,
            project_id="test-project",
        )
        await self.store.save_task(task)

        async def get_session_transcript(resolved_session_id: str) -> list[dict[str, Any]]:
            self.assertEqual(resolved_session_id, session_id)
            return [{
                "message": SimpleNamespace(
                    message_id="assistant-result",
                    summary_flag=False,
                    role="assistant",
                    agent_id="executor",
                    created_at=datetime.now(),
                ),
                "parts": [SimpleNamespace(
                    part_type="text",
                    payload={"text": "External final delivery"},
                )],
            }]

        self.store.get_session_transcript = get_session_transcript  # type: ignore[attr-defined]
        self.engine.process_message = AsyncMock(return_value="External final delivery")

        await self.handler._run_task("External Delivery", "Description", "task", "corporate", task_id)

        messages = await self.chat_store.get_channel_messages(f"session:{task_id}", limit=20, project_id="test-project")
        self.assertTrue(any(
            message["sender"] == "executor" and message["content"] == "External final delivery"
            for message in messages
        ))


# ═══════════════════════════════════════════════════════════════════════
# Test 12: WSHandler — on_progress routing
# ═══════════════════════════════════════════════════════════════════════

class TestWSHandlerProgressRouting(unittest.IsolatedAsyncioTestCase):
    """Test that on_progress routes to the correct session channel."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_progress_with_task_id_routes_to_session(self) -> None:
        """Progress with task_id should route to session:{task_id}."""
        await self.handler.on_progress("[Delegating to codex] task=demo", task_id="t1")

        # Check that message was inserted to the correct channel
        cursor = await self.chat_store._db.execute(
            "SELECT channel_id FROM messages WHERE content LIKE '%Delegating to codex%'"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "session:t1")

    async def test_progress_without_task_id_routes_to_activity(self) -> None:
        """Progress without task_id should route to 'activity'."""
        await self.handler.on_progress("[External status] codex started pid=42", task_id=None)

        cursor = await self.chat_store._db.execute(
            "SELECT channel_id FROM messages WHERE content LIKE '%codex started pid=42%'"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "activity:test-project")

    async def test_short_progress_not_stored(self) -> None:
        """Short progress messages (<=10 chars) should not be stored."""
        await self.handler.on_progress("OK", task_id="t1")

        cursor = await self.chat_store._db.execute("SELECT COUNT(*) FROM messages")
        count = (await cursor.fetchone())[0]
        self.assertEqual(count, 0)

    async def test_tool_progress_not_stored(self) -> None:
        """Tool progress lines should not be stored as messages."""
        await self.handler.on_progress("[Tool: file_read] reading src/main.py", task_id="t1")

        cursor = await self.chat_store._db.execute("SELECT COUNT(*) FROM messages")
        count = (await cursor.fetchone())[0]
        self.assertEqual(count, 0)

    async def test_raw_final_response_progress_not_stored(self) -> None:
        """Natural-language final replies should be sourced from transcript sync only."""
        await self.handler.on_progress(
            "I've completed the implementation and validated the happy path.",
            task_id="t1",
        )

        cursor = await self.chat_store._db.execute("SELECT COUNT(*) FROM messages")
        count = (await cursor.fetchone())[0]
        self.assertEqual(count, 0)

    async def test_external_progress_is_attached_to_session_progress_with_detail(self) -> None:
        await self.handler.on_progress("[External:codex:stdout] planning patch", task_id="t1")

        payloads = [
            item["payload"]
            for item in self.broadcasts
            if item.get("type") == "session_progress"
        ]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["task_id"], "t1")
        self.assertEqual(payloads[0]["entry"]["type"], "status_change")
        self.assertEqual(payloads[0]["entry"]["summary"], "codex stdout")
        self.assertEqual(payloads[0]["entry"]["detail"], "planning patch")

    async def test_delegation_progress_is_stored_as_message(self) -> None:
        await self.handler.on_progress("[Delegating to codex] task=demo", task_id="t1")

        messages = await self.chat_store.get_channel_messages("session:t1", limit=20, project_id="test-project")
        self.assertTrue(any("[Delegating to codex]" in message["content"] for message in messages))

    async def test_external_start_status_is_stored_as_message(self) -> None:
        await self.handler.on_progress("[External status] codex started pid=42", task_id="t1")

        messages = await self.chat_store.get_channel_messages("session:t1", limit=20, project_id="test-project")
        self.assertTrue(any("codex started pid=42" in message["content"] for message in messages))

    async def test_external_heartbeat_status_is_not_stored_as_message(self) -> None:
        await self.handler.on_progress("[External status] codex working; last activity 3s ago", task_id="t1")

        messages = await self.chat_store.get_channel_messages("session:t1", limit=20, project_id="test-project")
        self.assertEqual(messages, [])

    async def test_external_visual_progress_uses_role_identity(self) -> None:
        self.adapter.update_role_map({"executor": "agent-executor"})

        await self.handler.on_progress(
            "[External status] codex started pid=42",
            task_id="work-item-1",
            agent_role_id="executor",
        )

        event_payloads = [
            item["payload"]
            for item in self.broadcasts
            if item.get("type") == "event"
        ]
        self.assertTrue(any(
            payload["agent_id"] == "agent-executor" and payload["type"] == "message_out"
            for payload in event_payloads
        ))


class TestWSHandlerTranscriptSync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.engine = _make_engine(self.store)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_terminal_child_status_syncs_child_and_parent_transcripts(self) -> None:
        parent = Task(
            id="parent-1",
            title="Runtime Parent",
            session_id="sess-parent",
            project_id="test-project",
        )
        child = Task(
            id="child-1",
            title="Implementation Work Item",
            session_id="sess-child",
            parent_session_id="sess-parent",
            project_id="test-project",
        )
        await self.store.save_task(parent)
        await self.store.save_task(child)
        self.handler._session_to_task["sess-parent"] = "parent-1"

        async def get_session_transcript(session_id: str) -> list[dict[str, Any]]:
            if session_id == "sess-child":
                return [{
                    "message": SimpleNamespace(
                        message_id="child-msg",
                        summary_flag=False,
                        role="assistant",
                        agent_id="executor",
                        created_at=datetime.now(),
                    ),
                    "parts": [SimpleNamespace(
                        part_type="text",
                        payload={"text": "Child external result"},
                    )],
                }]
            if session_id == "sess-parent":
                return [{
                    "message": SimpleNamespace(
                        message_id="parent-msg",
                        summary_flag=False,
                        role="assistant",
                        agent_id="executor",
                        created_at=datetime.now(),
                    ),
                    "parts": [SimpleNamespace(
                        part_type="subtask_result",
                        payload={
                            "task_title": "Implementation Work Item",
                            "summary": "Child external result",
                        },
                    )],
                }]
            return []

        self.store.get_session_transcript = get_session_transcript  # type: ignore[attr-defined]

        event = MagicMock()
        event.event_type = "task_status_changed"
        event.payload = {"task_id": "child-1", "status": "done"}

        await self.handler.on_opc_event(event)

        child_messages = await self.chat_store.get_channel_messages("session:child-1", limit=20, project_id="test-project")
        parent_messages = await self.chat_store.get_channel_messages("session:parent-1", limit=20, project_id="test-project")
        self.assertTrue(any("Child external result" in message["content"] for message in child_messages))
        self.assertTrue(any("Child external result" in message["content"] for message in parent_messages))

    async def test_process_session_message_prefers_transcript_reply_over_manual_duplicate(self) -> None:
        task = Task(
            id="session-1",
            title="Primary Session",
            session_id="sess-1",
            project_id="test-project",
        )
        await self.store.save_task(task)

        async def get_session_transcript(session_id: str) -> list[dict[str, Any]]:
            self.assertEqual(session_id, "sess-1")
            return [{
                "message": SimpleNamespace(
                    message_id="assistant-msg-1",
                    summary_flag=False,
                    role="assistant",
                    agent_id="executor",
                    created_at=datetime.now(),
                ),
                "parts": [SimpleNamespace(
                    part_type="text",
                    payload={"text": "Final implementation summary"},
                )],
            }]

        self.store.get_session_transcript = get_session_transcript  # type: ignore[attr-defined]
        self.engine.process_message = AsyncMock(return_value="Final implementation summary")

        await self.handler._process_session_message(
            "session-1",
            "Please finish the implementation",
            session_id="sess-1",
        )

        messages = await self.chat_store.get_channel_messages("session:session-1", limit=20, project_id="test-project")
        matching = [message for message in messages if message["content"] == "Final implementation summary"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["sender"], "executor")

        session_broadcasts = [
            item["payload"]
            for item in self.broadcasts
            if item.get("type") == "session_message"
            and item.get("payload", {}).get("content") == "Final implementation summary"
        ]
        self.assertEqual(len(session_broadcasts), 1)
        self.assertEqual(session_broadcasts[0]["sender"], "executor")


class TestWSHandlerEscalationRouting(unittest.IsolatedAsyncioTestCase):
    """Escalations should surface on the user-visible session task."""

    async def asyncSetUp(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.store = StubStore()
        self.memory = StubMemory()
        self.engine = _make_engine(self.store, self.memory)
        self.chat_store = await _make_chat_store()
        self.adapter = EventAdapter()
        self.agent_store = MagicMock()
        self.handler = WSHandler(self.engine, self.agent_store, self.chat_store, self.adapter)
        self.broadcasts: list[dict] = []
        self.handler.broadcast = AsyncMock(side_effect=lambda msg: self.broadcasts.append(msg))

    async def asyncTearDown(self) -> None:
        await self.chat_store._db.close()

    async def test_escalation_routes_to_origin_session_task(self) -> None:
        origin_task_id = "session-task-1"
        exec_task_id = "exec-task-1"
        session_id = "sess-1"

        await self.store.save_task(Task(
            id=origin_task_id,
            title="Session Task",
            project_id="test-project",
            session_id=session_id,
        ))
        await self.store.save_task(Task(
            id=exec_task_id,
            title="Execution Task",
            project_id="test-project",
            session_id=session_id,
            metadata={"origin_task_id": origin_task_id, "execution_mode": "task_mode"},
        ))
        await self.chat_store.create_session_channel(origin_task_id, "Session Task", project_id="test-project")
        self.handler._session_to_task[session_id] = origin_task_id

        event = MagicMock()
        event.payload = {
            "escalation_id": "esc-1",
            "task_id": exec_task_id,
            "type": "decision_needed",
            "message": "Approve tool 'file_read'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
        }

        await self.handler._mirror_escalation(event)

        cursor = await self.chat_store._db.execute(
            "SELECT channel_id, metadata FROM messages WHERE message_id = (SELECT message_id FROM messages ORDER BY timestamp DESC LIMIT 1)"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], f"session:{origin_task_id}")
        metadata = json.loads(row[1]) if row[1] else {}
        self.assertEqual(metadata.get("task_id"), origin_task_id)
        self.assertEqual(metadata.get("source_task_id"), exec_task_id)

        pending_from_origin = self.handler._find_pending_escalation(task_id=origin_task_id)
        pending_from_exec = self.handler._find_pending_escalation(task_id=exec_task_id)
        self.assertIsNotNone(pending_from_origin)
        self.assertIsNotNone(pending_from_exec)
        self.assertEqual(pending_from_origin, pending_from_exec)

    async def test_origin_session_plain_approval_requires_card_click(self) -> None:
        origin_task_id = "session-task-2"
        exec_task_id = "exec-task-2"
        session_id = "sess-2"

        await self.store.save_task(Task(
            id=origin_task_id,
            title="Session Task",
            project_id="test-project",
            session_id=session_id,
        ))
        await self.store.save_task(Task(
            id=exec_task_id,
            title="Execution Task",
            project_id="test-project",
            session_id=session_id,
            metadata={"origin_task_id": origin_task_id, "execution_mode": "task_mode"},
        ))
        await self.chat_store.create_session_channel(origin_task_id, "Session Task", project_id="test-project")
        self.handler._session_to_task[session_id] = origin_task_id

        event = MagicMock()
        event.payload = {
            "escalation_id": "esc-2",
            "task_id": exec_task_id,
            "type": "decision_needed",
            "message": "Approve tool 'file_search'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
        }
        await self.handler._mirror_escalation(event)

        pending = self.handler._find_pending_escalation(task_id=origin_task_id)
        self.assertIsNotNone(pending)
        future = pending["future"]

        ws = MagicMock()
        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": origin_task_id,
            "content": "approve",
        })

        self.assertFalse(future.done())
        self.assertIsNotNone(self.handler._find_pending_escalation(task_id=origin_task_id))
        cursor = await self.chat_store._db.execute(
            "SELECT sender, content FROM messages ORDER BY timestamp DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "assistant")
        self.assertIn("approval card buttons", row[1])
        if not future.done():
            future.cancel()
        self.handler._pending_escalations.clear()
        self.handler._pending_escalation_order.clear()

    async def test_origin_session_card_reply_resolves_pending_escalation(self) -> None:
        origin_task_id = "session-task-2-card"
        exec_task_id = "exec-task-2-card"
        session_id = "sess-2-card"

        await self.store.save_task(Task(
            id=origin_task_id,
            title="Session Task",
            project_id="test-project",
            session_id=session_id,
        ))
        await self.store.save_task(Task(
            id=exec_task_id,
            title="Execution Task",
            project_id="test-project",
            session_id=session_id,
            metadata={"origin_task_id": origin_task_id, "execution_mode": "task_mode"},
        ))
        await self.chat_store.create_session_channel(origin_task_id, "Session Task", project_id="test-project")
        self.handler._session_to_task[session_id] = origin_task_id

        event = MagicMock()
        event.payload = {
            "escalation_id": "esc-2-card",
            "task_id": exec_task_id,
            "type": "decision_needed",
            "message": "Approve tool 'file_search'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
        }
        await self.handler._mirror_escalation(event)

        pending = self.handler._find_pending_escalation(task_id=origin_task_id)
        self.assertIsNotNone(pending)
        future = pending["future"]

        ws = MagicMock()
        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": origin_task_id,
            "content": "Approve once",
            "metadata": {
                "response_to_checkpoint_id": "esc-2-card",
                "response_to_checkpoint_type": "human_escalation",
                "response_to_escalation_id": "esc-2-card",
            },
        })

        self.assertTrue(future.done())
        self.assertEqual(future.result(), "approve_once")
        self.assertIsNone(self.handler._find_pending_escalation(task_id=origin_task_id))
        self.handler._pending_escalations.clear()
        self.handler._pending_escalation_order.clear()

    async def test_escalation_timeout_marks_original_card_terminal(self) -> None:
        origin_task_id = "session-task-timeout"
        exec_task_id = "exec-task-timeout"
        session_id = "sess-timeout"

        await self.store.save_task(Task(
            id=origin_task_id,
            title="Session Task",
            project_id="test-project",
            session_id=session_id,
        ))
        await self.store.save_task(Task(
            id=exec_task_id,
            title="Execution Task",
            project_id="test-project",
            session_id=session_id,
            metadata={"origin_task_id": origin_task_id, "execution_mode": "task_mode"},
        ))
        await self.chat_store.create_session_channel(origin_task_id, "Session Task", project_id="test-project")
        self.handler._session_to_task[session_id] = origin_task_id

        created_event = MagicMock()
        created_event.payload = {
            "escalation_id": "esc-timeout",
            "task_id": exec_task_id,
            "type": "decision_needed",
            "message": "Approve external_agent 'opencode'?",
            "options": [{"id": "approve_once", "label": "Approve once"}],
            "default_action": "approve_once",
        }
        await self.handler._mirror_escalation(created_event)

        timeout_event = MagicMock()
        timeout_event.event_type = "escalation_timeout"
        timeout_event.payload = {
            "escalation_id": "esc-timeout",
            "default_action": "approve_once",
        }
        await self.handler.on_opc_event(timeout_event)

        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            ("escalation::esc-timeout",),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0]) if row and row[0] else {}
        self.assertEqual(metadata.get("checkpoint_status"), "timeout")
        self.assertEqual(metadata.get("checkpoint_timeout_default_action"), "approve_once")
        self.assertIn("checkpoint_resolved_at", metadata)
        self.assertTrue(any(
            item.get("type") == "session_message"
            and (item.get("payload") or {}).get("metadata", {}).get("checkpoint_status") == "timeout"
            for item in self.broadcasts
        ))

    async def test_stale_escalation_reply_marks_card_inactive_without_dispatch(self) -> None:
        origin_task_id = "session-task-stale"
        session_id = "sess-stale"

        await self.store.save_task(Task(
            id=origin_task_id,
            title="Session Task",
            project_id="test-project",
            session_id=session_id,
        ))
        await self.chat_store.create_session_channel(origin_task_id, "Session Task", project_id="test-project")
        await self.chat_store.insert_message(
            channel_id=f"session:{origin_task_id}",
            sender="assistant",
            sender_name="OPC",
            content="Approve external_agent 'opencode'?",
            metadata={
                "checkpoint_type": "human_escalation",
                "checkpoint_id": "esc-stale",
                "escalation_id": "esc-stale",
                "escalation_type": "decision_needed",
                "prompt": "Approve external_agent 'opencode'?",
                "summary": "Approve external_agent 'opencode'?",
                "options": [{"id": "approve_once", "label": "Approve once"}],
                "default_action": "approve_once",
            },
            message_id="escalation::esc-stale",
            project_id="test-project",
        )

        self.handler._dispatch_session_message = AsyncMock()
        self.handler._track_session = MagicMock()
        ws = MagicMock()
        await self.handler._handle_session_send(ws, {
            "project_id": "test-project",
            "task_id": origin_task_id,
            "content": "Approve once",
            "metadata": {
                "response_to_checkpoint_id": "esc-stale",
                "response_to_checkpoint_type": "human_escalation",
                "response_to_escalation_id": "esc-stale",
            },
        })

        self.handler._dispatch_session_message.assert_not_called()
        self.handler._track_session.assert_not_called()
        cursor = await self.chat_store._db.execute(
            "SELECT metadata FROM messages WHERE message_id = ?",
            ("escalation::esc-stale",),
        )
        row = await cursor.fetchone()
        self.assertIsNotNone(row)
        metadata = json.loads(row[0]) if row and row[0] else {}
        self.assertEqual(metadata.get("checkpoint_status"), "stale")
        self.assertEqual(metadata.get("checkpoint_resolution_reason"), "reply_to_inactive_escalation")
        self.assertTrue(any(
            item.get("type") == "session_message"
            and (item.get("payload") or {}).get("metadata", {}).get("checkpoint_status") == "stale"
            for item in self.broadcasts
        ))


# ═══════════════════════════════════════════════════════════════════════
# Test 13: Snapshot Builder — session data in collab_sync
# ═══════════════════════════════════════════════════════════════════════

class TestSnapshotBuilderSessionData(unittest.IsolatedAsyncioTestCase):
    """Test that snapshot_builder includes session data in collab_sync output."""

    async def test_task_to_kanban_includes_session_fields(self) -> None:
        """task_to_kanban should include session_id and chat_channel_id."""
        from opc.plugins.office_ui.snapshot_builder import task_to_kanban

        task = Task(
            id="t1",
            title="Session Task",
            session_id="s-abc",
            project_id="p1",
        )
        result = task_to_kanban(task, display_num=1)

        self.assertEqual(result["task_id"], "t1")
        self.assertEqual(result["session_id"], "s-abc")
        self.assertEqual(result["chat_channel_id"], "session:t1")
        self.assertEqual(result["display_id"], "OPC-1")

    async def test_build_collab_sync_sessions_have_session_id(self) -> None:
        """build_collab_sync should populate sessions with session_id."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        store = StubStore()
        task = Task(
            id="t1",
            title="My Task",
            session_id="s-xyz",
            project_id="p1",
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        await chat_store.create_session_channel("t1", "My Task", project_id="p1")

        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)

            # Check sessions include session_id
            sessions = result.get("sessions", [])
            self.assertTrue(len(sessions) > 0)
            session = sessions[0]
            self.assertEqual(session["session_id"], "s-xyz")
            self.assertEqual(session["task_id"], "t1")
            self.assertEqual(result["boards"][0]["board_id"], "p1")
            self.assertEqual([task["task_id"] for task in result["tasks"]], ["t1"])
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_sessions_include_work_item_role_identity(self) -> None:
        """Session payloads should expose role + employee identity separately for the UI."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        store = StubStore()
        task = Task(
            id="t-role",
            title="Design Execution",
            session_id="sess-role",
            project_id="p1",
            assigned_to="designer",
            metadata={
                "work_item_role_id": "designer",
                "work_item_role_name": "Designer",
                "employee_assignment": {"name": "UI Designer", "employee_id": "designer-1"},
            },
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        await chat_store.create_session_channel("t-role", "Design Execution", project_id="p1")

        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)
            session = next(item for item in result["sessions"] if item["task_id"] == "t-role")

            self.assertEqual(session["work_item_role_id"], "designer")
            self.assertEqual(session["work_item_role_name"], "Designer")
            self.assertEqual(session["employee_assignment"]["name"], "UI Designer")
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_keeps_cancelled_session_rows(self) -> None:
        """Stopped child sessions should survive refresh instead of being pruned as deleted."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        store = StubStore()
        task = Task(
            id="cancelled-child",
            title="Cancelled Child",
            session_id="sess-cancelled-child",
            parent_session_id="sess-parent",
            project_id="p1",
            status=TaskStatus.CANCELLED,
            metadata={"last_stop_reason": "user_stop"},
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        await chat_store.create_session_channel("cancelled-child", "Cancelled Child", project_id="p1")
        await chat_store.insert_message(
            channel_id="session:cancelled-child",
            sender="system",
            sender_name="System",
            content="Task stopped by user",
            project_id="p1",
        )
        await chat_store.append_progress(
            "cancelled-child",
            [{"type": "status_change", "summary": "Stopped", "timestamp": time.time()}],
            project_id="p1",
        )

        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            result = await build_collab_sync(engine, MagicMock(), chat_store)

            sessions = result.get("sessions", [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["task_id"], "cancelled-child")
            self.assertEqual(sessions[0]["status"], "cancelled")
            self.assertEqual(len(sessions[0]["progress_log"]), 1)
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_defers_child_transcript_context(self) -> None:
        """collab_sync should not derive child handoff context from full transcripts."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        class TranscriptStore(StubStore):
            async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
                user_msg = SimpleNamespace(
                    message_id=f"user-{session_id}",
                    summary_flag=False,
                    role="user",
                    agent_id="",
                    created_at=datetime.now(),
                )
                assistant_msg = SimpleNamespace(
                    message_id=f"assistant-{session_id}",
                    summary_flag=False,
                    role="assistant",
                    agent_id="ceo",
                    created_at=datetime.now(),
                )
                return [
                    {
                        "message": user_msg,
                        "parts": [SimpleNamespace(part_type="text", payload={"text": "Recovered CEO brief"})],
                    },
                    {
                        "message": assistant_msg,
                        "parts": [SimpleNamespace(part_type="text", payload={"text": "Final delivery draft"})],
                    },
                ]

            async def get_latest_session_compaction(self, session_id: str) -> None:
                return None

        store = TranscriptStore()
        task = Task(
            id="child-ceo",
            title="CEO Final Delivery",
            session_id="sess-ceo",
            parent_session_id="sess-parent",
            project_id="p1",
            status=TaskStatus.BLOCKED,
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)

            sessions = result.get("sessions", [])
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session["task_id"], "child-ceo")
            self.assertIsNone(session["handoff_context"])
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_recreates_missing_session_channel(self) -> None:
        """Live task sessions should recreate missing UI channels instead of disappearing."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        class TranscriptStore(StubStore):
            async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
                msg = SimpleNamespace(
                    message_id=f"msg-{session_id}",
                    summary_flag=False,
                    role="assistant",
                    agent_id="designer",
                    created_at=datetime.now(),
                )
                part = SimpleNamespace(
                    part_type="text",
                    payload={"text": "Work item resumed."},
                )
                return [{"message": msg, "parts": [part]}]

        store = TranscriptStore()
        task = Task(
            id="child-1",
            title="Design Execution",
            session_id="sess-child-1",
            parent_session_id="sess-parent-1",
            project_id="p1",
            status=TaskStatus.BLOCKED,
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        try:
            # Simulate the broken state: messages already exist for the session,
            # but the session channel row was never created.
            await chat_store.insert_message(
                channel_id="session:child-1",
                sender="designer",
                sender_name="Designer",
                content="Work item resumed.",
                project_id="p1",
            )

            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)

            self.assertTrue(any(t["task_id"] == "child-1" for t in result["tasks"]))
            self.assertTrue(any(s["task_id"] == "child-1" for s in result["sessions"]))

            channels = await chat_store.get_session_channels("p1")
            self.assertTrue(any(ch["channel_id"] == "session:child-1" for ch in channels))
            refreshed_task = await store.get_task("child-1")
            self.assertIsNotNone(refreshed_task)
            self.assertEqual(refreshed_task.status, TaskStatus.BLOCKED)
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_does_not_duplicate_manual_session_history(self) -> None:
        """Transcript reconciliation should not reinsert user/assistant turns already shown in UI."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        class TranscriptStore(StubStore):
            async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
                self.assertEqual(session_id, "sess-dup")
                return [
                    {
                        "message": SimpleNamespace(
                            message_id="engine-user-1",
                            summary_flag=False,
                            role="user",
                            agent_id="",
                            created_at=datetime.now(),
                        ),
                        "parts": [SimpleNamespace(part_type="text", payload={"text": "Need a rollout plan"})],
                    },
                    {
                        "message": SimpleNamespace(
                            message_id="engine-assistant-1",
                            summary_flag=False,
                            role="assistant",
                            agent_id="executor",
                            created_at=datetime.now(),
                        ),
                        "parts": [SimpleNamespace(part_type="text", payload={"text": "Here is the rollout plan."})],
                    },
                ]

        store = TranscriptStore()
        task = Task(
            id="dup-1",
            title="Dup Check",
            session_id="sess-dup",
            project_id="p1",
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("dup-1", "Dup Check", project_id="p1")
            await chat_store.insert_message(
                channel_id="session:dup-1",
                sender="user",
                sender_name="You",
                content="Need a rollout plan",
                message_id="manual-user-1",
                project_id="p1",
            )
            await chat_store.insert_message(
                channel_id="session:dup-1",
                sender="assistant",
                sender_name="OPC",
                content="Here is the rollout plan.",
                message_id="manual-assistant-1",
                project_id="p1",
            )

            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            result = await build_collab_sync(engine, MagicMock(), chat_store)

            session_messages = [
                message
                for message in result.get("messages", [])
                if message["channel_id"] == "session:dup-1"
            ]
            self.assertEqual(len(session_messages), 2)

            cursor = await chat_store._db.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_id = ?",
                ("session:dup-1",),
            )
            raw_count = (await cursor.fetchone())[0]
            self.assertEqual(raw_count, 2)
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_skips_transcript_rehydrate_for_populated_session(self) -> None:
        """Large preloaded sessions should not reload the full transcript during collab_sync."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        class TranscriptStore(StubStore):
            def __init__(self) -> None:
                super().__init__()
                self.transcript_calls = 0

            async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
                self.transcript_calls += 1
                return [
                    {
                        "message": SimpleNamespace(
                            message_id=f"engine-{session_id}",
                            summary_flag=False,
                            role="assistant",
                            agent_id="executor",
                            created_at=datetime.now(),
                        ),
                        "parts": [SimpleNamespace(part_type="text", payload={"text": "Recovered transcript"})],
                    },
                ]

        store = TranscriptStore()
        task = Task(
            id="warm-session",
            title="Warm Session",
            session_id="sess-warm",
            project_id="p1",
        )
        await store.save_task(task)

        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("warm-session", "Warm Session", project_id="p1")
            await chat_store.insert_message(
                channel_id="session:warm-session",
                sender="executor",
                sender_name="Executor",
                content="Already cached",
                message_id="cached-1",
                project_id="p1",
            )

            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            result = await build_collab_sync(engine, MagicMock(), chat_store)

            session_messages = [
                message
                for message in result.get("messages", [])
                if message["channel_id"] == "session:warm-session"
            ]
            self.assertEqual(store.transcript_calls, 0)
            self.assertEqual(len(session_messages), 1)
            self.assertEqual(session_messages[0]["content"], "Already cached")
        finally:
            await chat_store._db.close()

    async def test_get_messages_hides_preexisting_duplicate_rows(self) -> None:
        """Read paths should collapse old duplicate rows already persisted in ui_state.db."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("dup-history", "Dup History", project_id="p1")
            await chat_store.insert_message(
                channel_id="session:dup-history",
                sender="assistant",
                sender_name="OPC",
                content="Fixed the issue.",
                message_id="legacy-manual",
                project_id="p1",
            )
            await chat_store.insert_message(
                channel_id="session:dup-history",
                sender="executor",
                sender_name="Executor",
                content="Fixed the issue.",
                message_id="legacy-engine",
                project_id="p1",
                metadata={"source": "engine", "role": "assistant"},
            )

            channel_messages = await chat_store.get_channel_messages("session:dup-history", limit=20, project_id="p1")
            project_messages = await chat_store.get_messages("p1", limit=20)

            self.assertEqual(len(channel_messages), 1)
            self.assertEqual(channel_messages[0]["sender"], "executor")
            self.assertEqual(len([m for m in project_messages if m["channel_id"] == "session:dup-history"]), 1)
        finally:
            await chat_store._db.close()

    async def test_get_messages_prefers_company_role_result_over_native_raw_turn(self) -> None:
        """Old native company rows should collapse to the role-level result surface."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("company-native-dup", "Company Native Dup", project_id="p1")
            duplicate_content = "最终分析已经完成，结论如下。"
            await chat_store.insert_message(
                channel_id="session:company-native-dup",
                sender="assistant",
                sender_name="Task Generalist",
                content=duplicate_content,
                message_id="native-raw-1",
                project_id="p1",
                metadata={
                    "source": "engine",
                    "transcript_kind": "runtime_v2_assistant",
                },
            )
            await chat_store.insert_message(
                channel_id="session:company-native-dup",
                sender="chao",
                sender_name="Chao",
                content=duplicate_content,
                message_id="role-result-1",
                project_id="p1",
                metadata={
                    "source": "engine",
                    "transcript_kind": "company_role_result",
                },
            )

            channel_messages = await chat_store.get_channel_messages("session:company-native-dup", limit=20, project_id="p1")

            self.assertEqual(len(channel_messages), 1)
            self.assertEqual(channel_messages[0]["message_id"], "role-result-1")
            self.assertEqual(channel_messages[0]["sender_name"], "Chao")
        finally:
            await chat_store._db.close()

    async def test_get_messages_maps_task_generalist_runtime_speaker_to_opc(self) -> None:
        """Persisted task-mode runtime rows should not expose the internal Task Generalist label."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("task-speaker", "Task Speaker", project_id="p1")
            await chat_store.insert_message(
                channel_id="session:task-speaker",
                sender="task_generalist",
                sender_name="Task Generalist",
                content="Native task result.",
                message_id="runtime-assistant-1",
                project_id="p1",
                metadata={
                    "source": "engine",
                    "transcript_kind": "runtime_v2_assistant",
                },
            )

            channel_messages = await chat_store.get_channel_messages("session:task-speaker", limit=20, project_id="p1")

            self.assertEqual(len(channel_messages), 1)
            self.assertEqual(channel_messages[0]["sender_name"], "OPC")

            await chat_store.insert_message(
                channel_id="session:task-speaker",
                sender="task_generalist",
                sender_name="Task Generalist",
                content="Legacy misclassified task result.",
                message_id="runtime-company-assistant-legacy",
                project_id="p1",
                metadata={
                    "source": "engine",
                    "transcript_kind": "runtime_v2_company_assistant",
                },
            )

            channel_messages = await chat_store.get_channel_messages("session:task-speaker", limit=20, project_id="p1")
            legacy_message = next(
                message for message in channel_messages
                if message["message_id"] == "runtime-company-assistant-legacy"
            )
            self.assertEqual(legacy_message["sender_name"], "OPC")
        finally:
            await chat_store._db.close()

    async def test_get_messages_keeps_distinct_manual_user_repeats(self) -> None:
        """Two separate user sends with identical text should stay visible as distinct turns."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("repeat-user", "Repeat User", project_id="p1")
            await chat_store.insert_message(
                channel_id="session:repeat-user",
                sender="user",
                sender_name="You",
                content="继续操作",
                message_id="manual-user-1",
                project_id="p1",
            )
            await chat_store.insert_message(
                channel_id="session:repeat-user",
                sender="user",
                sender_name="You",
                content="继续操作",
                message_id="manual-user-2",
                project_id="p1",
            )

            channel_messages = await chat_store.get_channel_messages("session:repeat-user", limit=20, project_id="p1")

            self.assertEqual(len(channel_messages), 2)
            self.assertEqual(
                [message["message_id"] for message in channel_messages],
                ["manual-user-1", "manual-user-2"],
            )
        finally:
            await chat_store._db.close()

    async def test_backfill_messages_scopes_duplicate_engine_ids_across_channels(self) -> None:
        """Backfill should not fail when the same engine message id appears in multiple channels."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("parent-task", "Parent", project_id="p1")
            await chat_store.create_session_channel("child-task", "Child", project_id="p1")

            parent_inserted = await chat_store.backfill_messages(
                "session:parent-task",
                [{
                    "message_id": "engine-shared-1",
                    "sender": "executor",
                    "sender_name": "Executor",
                    "content": "Shared engine update",
                    "timestamp": time.time(),
                    "metadata": {"source": "engine", "role": "assistant"},
                }],
                project_id="p1",
            )
            child_inserted = await chat_store.backfill_messages(
                "session:child-task",
                [{
                    "message_id": "engine-shared-1",
                    "sender": "executor",
                    "sender_name": "Executor",
                    "content": "Shared engine update",
                    "timestamp": time.time() + 1,
                    "metadata": {"source": "engine", "role": "assistant"},
                }],
                project_id="p1",
            )

            self.assertEqual(len(parent_inserted), 1)
            self.assertEqual(len(child_inserted), 1)
            self.assertNotEqual(parent_inserted[0]["message_id"], child_inserted[0]["message_id"])
            self.assertEqual(child_inserted[0]["metadata"].get("ui_message_id"), "engine-shared-1")

            parent_messages = await chat_store.get_channel_messages("session:parent-task", limit=20, project_id="p1")
            child_messages = await chat_store.get_channel_messages("session:child-task", limit=20, project_id="p1")

            self.assertEqual(len(parent_messages), 1)
            self.assertEqual(len(child_messages), 1)
            self.assertEqual(parent_messages[0]["content"], "Shared engine update")
            self.assertEqual(child_messages[0]["content"], "Shared engine update")
        finally:
            await chat_store._db.close()

    async def test_backfill_merges_same_scope_row_that_raced_the_snapshot(self) -> None:
        """A live insert landing after the backfill snapshot must merge in place,
        never persist a second copy under a `::`-scoped alias id (project 000:
        the same reply was stored twice in one channel)."""
        chat_store = await _make_chat_store()
        try:
            await chat_store.create_session_channel("race-task", "Race", project_id="p1")
            channel_id = "session:race-task"

            original_scope = chat_store._message_scope

            async def racing_scope(message_id: str):
                # Simulate the live insert path landing the same row after the
                # backfill snapshot was taken but before its INSERT runs.
                if message_id == "engine-race-1" and await original_scope(message_id) is None:
                    await chat_store.insert_message(
                        channel_id=channel_id,
                        sender="assistant",
                        sender_name="OPC",
                        content="Reply text",
                        message_id="engine-race-1",
                        project_id="p1",
                        metadata={"note": "live-copy"},
                    )
                return await original_scope(message_id)

            chat_store._message_scope = racing_scope

            await chat_store.backfill_messages(channel_id, [{
                "message_id": "engine-race-1",
                "sender": "assistant",
                "sender_name": "OPC",
                "content": "Reply text",
                "timestamp": time.time(),
                "metadata": {"source": "engine", "role": "assistant"},
            }], project_id="p1")

            rows = await chat_store.get_channel_messages(channel_id, limit=20, project_id="p1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["message_id"], "engine-race-1")
            self.assertNotIn("::", rows[0]["message_id"])
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_marks_primary_as_company_runtime_from_children(self) -> None:
        """Legacy primary sessions should still be marked as company runtimes when child work items exist."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        store = StubStore()
        parent = Task(
            id="parent-1",
            title="Legacy Parent",
            session_id="sess-parent-1",
            project_id="p1",
            metadata={"parent_session_id": "sess-parent-1"},
        )
        child = Task(
            id="child-1",
            title="Planning",
            session_id="sess-child-1",
            parent_session_id="sess-parent-1",
            project_id="p1",
            metadata={
                "company_profile": "classic",
                "work_item_projection_id": "planning",
                "parent_session_id": "sess-parent-1",
            },
        )
        await store.save_task(parent)
        await store.save_task(child)

        chat_store = await _make_chat_store()
        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)

            sessions = {session["task_id"]: session for session in result["sessions"]}
            self.assertIn("parent-1", sessions)
            self.assertIn("child-1", sessions)
            self.assertTrue(sessions["parent-1"]["is_company_runtime"])
            self.assertEqual(sessions["parent-1"]["company_profile"], "corporate")
            self.assertEqual(sessions["child-1"]["mode"], "child")
            self.assertEqual(sessions["child-1"]["parent_session_id"], "sess-parent-1")
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_restores_session_specific_mode_and_profile(self) -> None:
        """collab_sync should restore each session's own exec_mode/company_profile/preferred_agent after refresh."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync, build_project_index_sync

        store = StubStore()
        explicit = Task(
            id="explicit-1",
            title="Explicit Company",
            session_id="sess-explicit-1",
            project_id="p1",
            metadata={
                "exec_mode": "company",
                "company_profile": "corporate",
                "org_id": "quantum_harbor",
                "preferred_agent": "codex",
            },
        )
        legacy = Task(
            id="legacy-1",
            title="Legacy Company",
            session_id="sess-legacy-1",
            project_id="p1",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "classic",
            },
        )
        custom_mistagged = Task(
            id="custom-1",
            title="Company With Stale Custom Profile",
            session_id="sess-custom-1",
            project_id="p1",
            metadata={
                "exec_mode": "company",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
            },
        )
        custom_org = Task(
            id="custom-org-1",
            title="Custom Org",
            session_id="sess-custom-org-1",
            project_id="p1",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                "org_id": "quantum_harbor",
            },
        )
        await store.save_task(explicit)
        await store.save_task(legacy)
        await store.save_task(custom_mistagged)
        await store.save_task(custom_org)

        chat_store = await _make_chat_store()
        await chat_store.create_session_channel("explicit-1", "Explicit Company", project_id="p1")
        await chat_store.create_session_channel("legacy-1", "Legacy Company", project_id="p1")
        await chat_store.create_session_channel("custom-1", "Company With Stale Custom Profile", project_id="p1")
        await chat_store.create_session_channel("custom-org-1", "Custom Org", project_id="p1")

        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"

            agent_store = MagicMock()

            result = await build_collab_sync(engine, agent_store, chat_store)

            sessions = {session["task_id"]: session for session in result["sessions"]}
            self.assertEqual(sessions["explicit-1"]["exec_mode"], "company")
            self.assertEqual(sessions["explicit-1"]["company_profile"], "corporate")
            self.assertEqual(sessions["explicit-1"]["org_id"], "")
            self.assertEqual(sessions["explicit-1"]["preferred_agent"], "codex")
            self.assertEqual(sessions["legacy-1"]["exec_mode"], "company")
            self.assertEqual(sessions["legacy-1"]["company_profile"], "corporate")
            self.assertEqual(sessions["legacy-1"]["preferred_agent"], "native")
            self.assertEqual(sessions["custom-1"]["exec_mode"], "company")
            self.assertEqual(sessions["custom-1"]["company_profile"], "corporate")
            self.assertEqual(sessions["custom-1"]["org_id"], "")
            self.assertEqual(sessions["custom-org-1"]["exec_mode"], "org")
            self.assertEqual(sessions["custom-org-1"]["company_profile"], "custom")
            self.assertEqual(sessions["custom-org-1"]["org_id"], "quantum_harbor")

            index_result = await build_project_index_sync(engine, agent_store, chat_store)
            index_sessions = {session["task_id"]: session for session in index_result["sessions"]}
            self.assertEqual(index_sessions["explicit-1"]["exec_mode"], "company")
            self.assertEqual(index_sessions["explicit-1"]["company_profile"], "corporate")
            self.assertEqual(index_sessions["explicit-1"]["org_id"], "")
            self.assertEqual(index_sessions["custom-1"]["exec_mode"], "company")
            self.assertEqual(index_sessions["custom-1"]["company_profile"], "corporate")
            self.assertEqual(index_sessions["custom-1"]["org_id"], "")
            self.assertEqual(index_sessions["custom-org-1"]["exec_mode"], "org")
            self.assertEqual(index_sessions["custom-org-1"]["company_profile"], "custom")
            self.assertEqual(index_sessions["custom-org-1"]["org_id"], "quantum_harbor")
        finally:
            await chat_store._db.close()

    async def test_build_collab_sync_company_mode_keeps_four_columns_even_with_empty_work_item_board(self) -> None:
        """Company-mode collab_sync should keep the four-column board even with zero visible work items."""
        from opc.plugins.office_ui.snapshot_builder import build_collab_sync

        class CompanyStore(StubStore):
            async def list_open_delegation_runs(self, project_id: str):
                return [
                    DelegationRun(
                        run_id="run-1",
                        project_id=project_id,
                        status="running",
                        lifecycle_status="active",
                    )
                ]

            async def list_delegation_work_items(self, run_id: str):  # noqa: ARG002
                return []

            async def list_seat_states(self, run_id: str):  # noqa: ARG002
                return []

        store = CompanyStore()
        await store.save_task(
            Task(
                id="root-session",
                title="Root Session",
                session_id="sess-root",
                project_id="p1",
                metadata={"execution_mode": "company_mode"},
            )
        )

        chat_store = await _make_chat_store()
        await chat_store.create_session_channel("root-session", "Root Session", project_id="p1")

        try:
            engine = MagicMock()
            engine.store = store
            engine.project_id = "p1"
            engine.org_engine = MagicMock()

            result = await build_collab_sync(engine, MagicMock(), chat_store)

            self.assertEqual(
                [column["name"] for column in result["columns"]],
                ["To do", "In progress", "In review", "Done"],
            )
            self.assertEqual(result["tasks"], [])
        finally:
            await chat_store._db.close()


class TestWSHandlerCommsState(unittest.IsolatedAsyncioTestCase):
    async def test_comms_state_resolves_session_scope_from_root_anchor_task(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler

        store = StubStore()
        root_task = Task(
            id="root-task",
            title="Runtime Root",
            session_id="root-session",
            project_id="test-project",
            metadata={"exec_mode": "company"},
        )
        child_task = Task(
            id="child-task",
            title="Execution Child",
            session_id="child-session",
            parent_session_id="root-session",
            assigned_to="executor",
            project_id="test-project",
            metadata={"work_item_projection_id": "executor__execute"},
        )
        await store.save_task(root_task)
        await store.save_task(child_task)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = str(Path(tmpdir).resolve())
            deliverables = str((Path(tmpdir) / "deliverables").resolve())
            child_task.metadata = {
                **dict(child_task.metadata or {}),
                "workspace_root": workspace_root,
                "target_output_dir": deliverables,
                "output_root": deliverables,
                "comms_workspace_root": workspace_root,
                "comms_root": str(Path(workspace_root) / ".opc-comms"),
            }
            await store.save_task(child_task)

            layout = file_comms.resolve_layout(workspace_root, "test-project", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Need update",
                body="Please confirm the current implementation status.",
            )

            engine = _make_engine(store=store)
            engine.project_id = "test-project"
            handler = WSHandler(engine, MagicMock(), MagicMock(), MagicMock())
            ws = MagicMock()
            ws.send_json = AsyncMock()

            await handler._handle_comms_state(ws, {"project_id": "test-project", "task_id": "root-task"})

            payload = ws.send_json.await_args.args[0]["payload"]
            self.assertTrue(payload["available"])
            self.assertFalse(payload.get("empty", False))
            self.assertEqual(payload["session_id"], "root-session")
            self.assertEqual(payload["project_id"], "test-project")
            roles = {role["role_id"]: role for role in payload.get("roles", [])}
            self.assertIn("executor", roles)
            self.assertEqual(roles["executor"]["unread_count"], 1)


if __name__ == "__main__":
    unittest.main()
