from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.models import SessionLinkRecord, SessionMessageRecord, SessionRecord, Task
from opc.database.store import OPCStore


class TestOPCStoreTaskDeletion(unittest.IsolatedAsyncioTestCase):
    async def _count(self, store: OPCStore, table: str, where: str = "", params: tuple = ()) -> int:
        assert store._db is not None
        query = f"SELECT COUNT(*) FROM {table}"
        if where:
            query += f" WHERE {where}"
        async with store._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    async def test_hard_delete_task_removes_task_and_session_state(self) -> None:
        """Hard delete should remove the task row and linked session state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                task = Task(
                    id="task-1",
                    session_id="session-1",
                    title="New Chat",
                    project_id="default",
                )
                await store.save_task(task)
                await store.save_session(
                    SessionRecord(
                        session_id="session-1",
                        project_id="default",
                        title="New Chat",
                    )
                )
                await store.save_session_message(
                    SessionMessageRecord(
                        message_id="msg-1",
                        session_id="session-1",
                        task_id="task-1",
                        role="user",
                    )
                )
                await store.save_session_link(
                    SessionLinkRecord(
                        link_id="link-1",
                        project_id="default",
                        session_id="parent-session",
                        linked_session_id="session-1",
                        task_id="task-1",
                    )
                )

                await store.hard_delete_task(task.id, task.session_id)

                self.assertIsNone(await store.get_task(task.id))
                self.assertIsNone(await store.get_session(task.session_id))
                self.assertEqual(await store.get_session_links("parent-session"), [])
                async with store._db.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
                    (task.session_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(row[0], 0)
            finally:
                await store.close()

    async def test_hard_delete_task_removes_company_runtime_artifacts(self) -> None:
        """Deleting a top-level chat should remove its company runtime graph."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                task = Task(
                    id="task-runtime",
                    session_id="session-runtime",
                    title="Company Chat",
                    project_id="default",
                )
                await store.save_task(task)
                await store.save_session(
                    SessionRecord(
                        session_id=task.session_id or "",
                        project_id="default",
                        title="Company Chat",
                    )
                )

                assert store._db is not None
                now = "2026-05-12T00:00:00"
                run_id = "run-runtime"
                cell_id = "cell-runtime"
                work_item_id = "wi-runtime"
                role_session_id = "role-runtime"
                member_session_id = "member-runtime"

                await store._db.execute(
                    "INSERT INTO delegation_runs (run_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (run_id, task.session_id, now, now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_cells (cell_id, run_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (cell_id, run_id, now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO delegation_work_items
                    (work_item_id, run_id, cell_id, phase, role_runtime_session_id,
                     claimed_by_role_runtime_session_id, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_item_id,
                        run_id,
                        cell_id,
                        "running",
                        role_session_id,
                        role_session_id,
                        '{"runtime_task_id":"task-runtime"}',
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO work_item_runtime_links
                    (work_item_id, runtime_task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (work_item_id, task.id, now, now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_events (event_id, run_id, work_item_id, event_type, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("event-delegation", run_id, work_item_id, "work_started", now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_role_sessions (role_session_id, run_id, role_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (role_session_id, run_id, "engineer", now, now),
                )
                await store._db.execute(
                    "INSERT INTO role_runtime_sessions (role_session_id, run_id, role_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (role_session_id, run_id, "engineer", now, now),
                )
                await store._db.execute(
                    "INSERT INTO team_instances (team_instance_id, run_id, team_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("team-runtime", run_id, "team", task.session_id, now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO seat_states
                    (seat_state_id, team_instance_id, run_id, team_id, seat_id,
                     member_session_id, role_runtime_session_id, current_task_id,
                     current_work_item_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "seat-runtime",
                        "team-runtime",
                        run_id,
                        "team",
                        "seat",
                        member_session_id,
                        role_session_id,
                        task.id,
                        work_item_id,
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_sessions
                    (runtime_session_id, session_id, task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (role_session_id, None, None, now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_sessions
                    (runtime_session_id, session_id, task_id, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        member_session_id,
                        None,
                        None,
                        '{"role_session_id":"role-runtime","run_id":"run-runtime"}',
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("event-runtime", role_session_id, "message", now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("event-member-runtime", member_session_id, "message", now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_transcript_entries (entry_id, runtime_session_id, content, created_at) VALUES (?, ?, ?, ?)",
                    ("transcript-runtime", role_session_id, "hello", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_tool_calls
                    (call_record_id, runtime_session_id, tool_call_id, tool_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("tool-call-runtime", role_session_id, "call-1", "shell", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_tool_results
                    (result_record_id, runtime_session_id, tool_call_id, tool_name, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("tool-result-runtime", role_session_id, "call-1", "shell", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_permission_grants
                    (grant_id, runtime_session_id, scope, tool_name, candidate, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("grant-runtime", role_session_id, "once", "shell", "godot", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_subagent_runs
                    (subagent_run_id, runtime_session_id, task_id, agent_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("subagent-runtime", role_session_id, task.id, "codex", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_worktree_sessions
                    (worktree_session_id, runtime_session_id, task_id, path, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("worktree-runtime", role_session_id, task.id, "/tmp/work", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_compaction_boundaries
                    (boundary_id, runtime_session_id, task_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("boundary-runtime", role_session_id, task.id, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO execution_checkpoints
                    (checkpoint_id, session_id, checkpoint_type, task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("checkpoint-runtime", task.session_id, "company_delivery_feedback", "", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO external_sessions
                    (session_key, agent_type, session_id, opc_session_id, task_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("ext-runtime", "codex", "provider-session", task.session_id, "", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO role_memory
                    (memory_id, project_id, role_id, scope, summary, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "role-memory-runtime",
                        "default",
                        "engineer",
                        "project",
                        "Keep durable role memory",
                        '{"task_id":"task-runtime","run_id":"run-runtime"}',
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO agent_memory_snapshots
                    (snapshot_id, project_id, session_id, employee_id, role_id,
                     memory_scope, memory_kind, summary_message_id,
                     source_boundary_message_id, summary_text, memory_text,
                     metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-memory-runtime",
                        "default",
                        "",
                        "employee-engineer",
                        "engineer",
                        "project",
                        "final",
                        "",
                        "",
                        "Keep durable employee memory",
                        "Long-lived employee memory should survive chat deletion.",
                        '{"task_ids":["task-runtime"],"session_ids":["session-runtime"],"run_id":"run-runtime"}',
                        now,
                        now,
                    ),
                )
                for event_id, payload in (
                    ("evt-task", '{"task_id":"task-runtime"}'),
                    ("evt-run", '{"run_id":"run-runtime"}'),
                    ("evt-work", '{"work_item_id":"wi-runtime"}'),
                    ("evt-runtime", '{"runtime_session_id":"role-runtime"}'),
                    ("evt-role", '{"member_session_id":"role-runtime"}'),
                    ("evt-member-session", '{"member_session_id":"member-runtime"}'),
                ):
                    await store._db.execute(
                        "INSERT INTO events (event_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
                        (event_id, "test", payload, now),
                    )
                await store._db.commit()

                await store.hard_delete_task(task.id, task.session_id)

                for table in (
                    "tasks",
                    "sessions",
                    "delegation_runs",
                    "delegation_cells",
                    "delegation_work_items",
                    "work_item_runtime_links",
                    "delegation_events",
                    "delegation_role_sessions",
                    "role_runtime_sessions",
                    "team_instances",
                    "seat_states",
                    "runtime_sessions",
                    "runtime_events",
                    "runtime_transcript_entries",
                    "runtime_user_turn_seeds",
                    "runtime_tool_calls",
                    "runtime_tool_results",
                    "runtime_permission_grants",
                    "runtime_subagent_runs",
                    "runtime_worktree_sessions",
                    "runtime_compaction_boundaries",
                    "execution_checkpoints",
                    "external_sessions",
                    "events",
                ):
                    self.assertEqual(await self._count(store, table), 0, table)
                self.assertEqual(
                    await self._count(store, "role_memory", "memory_id = ?", ("role-memory-runtime",)),
                    1,
                )
                self.assertEqual(
                    await self._count(store, "agent_memory_snapshots", "snapshot_id = ?", ("agent-memory-runtime",)),
                    1,
                )
            finally:
                await store.close()

    async def test_delete_company_runtime_artifacts_for_session_cleans_orphan_run(self) -> None:
        """Session cleanup should work even after task/session rows are gone."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                assert store._db is not None
                now = "2026-05-12T00:00:00"
                deleted_session_id = "session-deleted"
                run_id = "run-deleted"
                work_item_id = "wi-deleted"
                role_session_id = "role-runtime-deleted"
                member_session_id = "role-session::default::session-deleted::ceo::employee"

                await store._db.execute(
                    "INSERT INTO delegation_runs (run_id, session_id, status, lifecycle_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, deleted_session_id, "running", "active", now, now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_runs (run_id, session_id, status, lifecycle_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("run-keep", "session-keep", "running", "active", now, now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_cells (cell_id, run_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("cell-deleted", run_id, now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO delegation_work_items
                    (work_item_id, run_id, cell_id, phase, role_runtime_session_id,
                     claimed_by_role_runtime_session_id, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_item_id,
                        run_id,
                        "cell-deleted",
                        "running",
                        role_session_id,
                        role_session_id,
                        '{"session_scope_id":"session-deleted"}',
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    "INSERT INTO delegation_events (event_id, run_id, work_item_id, event_type, created_at) VALUES (?, ?, ?, ?, ?)",
                    ("delegation-event-deleted", run_id, work_item_id, "work_started", now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_role_sessions (role_session_id, run_id, role_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (role_session_id, run_id, "ceo", now, now),
                )
                await store._db.execute(
                    "INSERT INTO role_runtime_sessions (role_session_id, run_id, role_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (role_session_id, run_id, "ceo", now, now),
                )
                await store._db.execute(
                    "INSERT INTO team_instances (team_instance_id, run_id, team_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("team-deleted", run_id, "team", deleted_session_id, now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO seat_states
                    (seat_state_id, team_instance_id, run_id, team_id, seat_id,
                     member_session_id, role_runtime_session_id, current_work_item_id,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "seat-deleted",
                        "team-deleted",
                        run_id,
                        "team",
                        "seat",
                        member_session_id,
                        role_session_id,
                        work_item_id,
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_sessions
                    (runtime_session_id, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        member_session_id,
                        '{"role_session_id":"role-runtime-deleted","run_id":"run-deleted"}',
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("runtime-event-deleted", member_session_id, "message", now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("runtime-event-run-deleted", run_id, "serial_queue_reconciled", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO execution_checkpoints
                    (checkpoint_id, session_id, checkpoint_type, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "checkpoint-deleted",
                        deleted_session_id,
                        "company_recruitment_confirmation",
                        '{"run_id":"run-deleted"}',
                        now,
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO external_sessions
                    (session_key, agent_type, session_id, opc_session_id, task_id, metadata, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "external-deleted",
                        "codex",
                        "provider",
                        deleted_session_id,
                        "",
                        '{"run_id":"run-deleted"}',
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO role_memory
                    (memory_id, project_id, role_id, scope, summary, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "role-memory-keep",
                        "default",
                        "ceo",
                        "project",
                        "Keep durable role memory",
                        '{"session_id":"session-deleted","run_id":"run-deleted"}',
                        now,
                    ),
                )
                await store._db.execute(
                    """
                    INSERT INTO agent_memory_snapshots
                    (snapshot_id, project_id, session_id, employee_id, role_id,
                     memory_scope, memory_kind, summary_message_id,
                     source_boundary_message_id, summary_text, memory_text,
                     metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-memory-keep",
                        "default",
                        "",
                        "employee-ceo",
                        "ceo",
                        "project",
                        "final",
                        "",
                        "",
                        "Keep durable employee memory",
                        "Long-lived employee memory should survive chat deletion.",
                        '{"session_ids":["session-deleted"],"run_id":"run-deleted"}',
                        now,
                        now,
                    ),
                )
                for event_id, payload in (
                    ("evt-deleted-session", '{"member_session_id":"role-session::default::session-deleted::ceo::employee"}'),
                    ("evt-deleted-run", '{"run_id":"run-deleted"}'),
                    ("evt-keep", '{"run_id":"run-keep"}'),
                ):
                    await store._db.execute(
                        "INSERT INTO events (event_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
                        (event_id, "test", payload, now),
                    )
                await store._db.commit()

                await store.delete_company_runtime_artifacts_for_session(deleted_session_id)

                self.assertEqual(
                    await self._count(store, "delegation_runs", "run_id = ?", (run_id,)),
                    0,
                )
                self.assertEqual(
                    await self._count(store, "delegation_runs", "run_id = ?", ("run-keep",)),
                    1,
                )
                for table in (
                    "delegation_cells",
                    "delegation_work_items",
                    "delegation_events",
                    "delegation_role_sessions",
                    "role_runtime_sessions",
                    "team_instances",
                    "seat_states",
                    "runtime_sessions",
                    "runtime_events",
                    "execution_checkpoints",
                    "external_sessions",
                ):
                    self.assertEqual(await self._count(store, table), 0, table)
                self.assertEqual(await self._count(store, "events"), 1)
                self.assertEqual(
                    await self._count(store, "events", "event_id = ?", ("evt-keep",)),
                    1,
                )
                self.assertEqual(
                    await self._count(store, "role_memory", "memory_id = ?", ("role-memory-keep",)),
                    1,
                )
                self.assertEqual(
                    await self._count(store, "agent_memory_snapshots", "snapshot_id = ?", ("agent-memory-keep",)),
                    1,
                )
            finally:
                await store.close()

    async def test_hard_delete_child_task_preserves_parent_company_run(self) -> None:
        """Deleting a child work-item chat must not delete the parent run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                parent = Task(
                    id="task-parent",
                    session_id="session-parent",
                    title="Parent Chat",
                    project_id="default",
                )
                child = Task(
                    id="task-child",
                    session_id="session-child",
                    parent_session_id=parent.session_id,
                    title="Child Work Item",
                    project_id="default",
                )
                await store.save_task(parent)
                await store.save_task(child)
                await store.save_session(SessionRecord(session_id=parent.session_id or "", project_id="default"))
                await store.save_session(
                    SessionRecord(
                        session_id=child.session_id or "",
                        project_id="default",
                        parent_session_id=parent.session_id,
                    )
                )

                assert store._db is not None
                now = "2026-05-12T00:00:00"
                await store._db.execute(
                    "INSERT INTO delegation_runs (run_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("run-parent", parent.session_id, now, now),
                )
                await store._db.execute(
                    "INSERT INTO delegation_cells (cell_id, run_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("cell-parent", "run-parent", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO delegation_work_items
                    (work_item_id, run_id, cell_id, phase, role_runtime_session_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("wi-child", "run-parent", "cell-parent", "running", "role-child", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO delegation_work_items
                    (work_item_id, run_id, cell_id, phase, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("wi-keep", "run-parent", "cell-parent", "ready", now, now),
                )
                await store._db.execute(
                    "INSERT INTO work_item_runtime_links (work_item_id, runtime_task_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("wi-child", child.id, now, now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_sessions (runtime_session_id, created_at, updated_at) VALUES (?, ?, ?)",
                    ("role-child", now, now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_sessions
                    (runtime_session_id, session_id, task_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("runtime-child", child.session_id, child.id, now, now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("event-role-child", "role-child", "message", now),
                )
                await store._db.execute(
                    "INSERT INTO runtime_events (event_id, runtime_session_id, event_type, created_at) VALUES (?, ?, ?, ?)",
                    ("event-runtime-child", "runtime-child", "message", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO runtime_transcript_entries
                    (entry_id, runtime_session_id, task_id, session_id, content, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("transcript-child", "role-child", child.id, child.session_id, "child output", now),
                )
                await store._db.execute(
                    """
                    INSERT INTO seat_states
                    (seat_state_id, team_instance_id, run_id, team_id, seat_id,
                     current_task_id, current_work_item_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("seat-parent", "team-parent", "run-parent", "team", "seat", child.id, "wi-child", now, now),
                )
                await store._db.commit()

                await store.hard_delete_task(child.id, child.session_id)

                self.assertIsNone(await store.get_task(child.id))
                self.assertIsNotNone(await store.get_task(parent.id))
                self.assertEqual(await self._count(store, "delegation_runs"), 1)
                self.assertEqual(await self._count(store, "delegation_cells"), 1)
                self.assertEqual(await self._count(store, "delegation_work_items"), 1)
                self.assertEqual(
                    await self._count(store, "delegation_work_items", "work_item_id = ?", ("wi-keep",)),
                    1,
                )
                self.assertEqual(await self._count(store, "work_item_runtime_links"), 0)
                self.assertEqual(await self._count(store, "runtime_sessions"), 1)
                self.assertEqual(
                    await self._count(store, "runtime_sessions", "runtime_session_id = ?", ("role-child",)),
                    1,
                )
                self.assertEqual(await self._count(store, "runtime_events"), 1)
                self.assertEqual(await self._count(store, "runtime_transcript_entries"), 0)
                self.assertEqual(await self._count(store, "seat_states"), 1)
                async with store._db.execute(
                    "SELECT current_task_id, current_work_item_id FROM seat_states WHERE seat_state_id = ?",
                    ("seat-parent",),
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(tuple(row), ("", ""))
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
