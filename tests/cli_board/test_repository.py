from __future__ import annotations

import unittest
from datetime import datetime

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Phase,
    SessionMessageRecord,
    SessionPartRecord,
    Task,
    TaskStatus,
)
from opc.plugins.cli_board.services.board_repository import BoardRepository
from opc.layer2_organization.work_item_links import set_linked_work_item_id


class _StubStore:
    def __init__(
        self,
        tasks,
        checkpoints,
        transcripts,
        *,
        delegation_runs=None,
        work_items_by_run=None,
    ) -> None:
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._transcripts = transcripts
        self._delegation_runs = list(delegation_runs or [])
        self._work_items_by_run = dict(work_items_by_run or {})

    async def get_tasks(self, **_kw):
        return list(self._tasks)

    async def get_pending_checkpoints(self, **_kw):
        return list(self._checkpoints)

    async def get_session_transcript(self, session_id):
        return list(self._transcripts.get(session_id, []))

    # The company-mode branch in BoardRepository feature-detects these methods.
    # Only present them when the test wires a delegation run, so the standard
    # tests above keep exercising the Task path.
    async def list_open_delegation_runs(self, *, project_id=None):
        return list(self._delegation_runs)

    async def list_delegation_work_items(self, run_id):
        return list(self._work_items_by_run.get(run_id, []))


class _StubFacade:
    def __init__(self, store) -> None:
        self.project_id = "demo"
        self._engine = type("Engine", (), {"store": store})()

    async def ensure_ready(self):
        return self._engine


class BoardRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def test_permission_prompt_uses_complete_verbatim_interaction_payload(self) -> None:
        prompt = "if True:\n    print(`<tag>`)\n\n" + ("x" * 4_000)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-exact",
            project_id="demo",
            checkpoint_type="tool_permission",
            payload={"interaction": {"prompt": prompt}},
        )

        _summary, rendered = BoardRepository._checkpoint_prompt(checkpoint)

        self.assertEqual(rendered, prompt)

    async def test_snapshot_hides_internal_origin_tasks(self) -> None:
        visible = Task(
            id="visible-1",
            title="Visible task",
            description="Board-visible task",
            status=TaskStatus.PENDING,
            session_id="session-visible",
            project_id="demo",
        )
        hidden = Task(
            id="hidden-1",
            title="Internal task",
            description="Background execution",
            status=TaskStatus.RUNNING,
            session_id="session-hidden",
            project_id="demo",
            metadata={"origin_task_id": "visible-1"},
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-1",
            project_id="demo",
            session_id="session-visible",
            checkpoint_type="company_delivery_feedback",
            status="pending",
            payload={"prompt": "Approve delivery?"},
        )
        repo = BoardRepository(_StubFacade(_StubStore([visible, hidden], [checkpoint], {})), project_id="demo")

        snapshot = await repo.load_snapshot()

        self.assertEqual(len(snapshot.tasks), 1)
        self.assertEqual(snapshot.hidden_task_count, 1)
        self.assertEqual(snapshot.tasks[0].task_id, "visible-1")
        self.assertIsNotNone(snapshot.tasks[0].pending_checkpoint)
        self.assertEqual(len(snapshot.session_summaries), 1)
        self.assertEqual(snapshot.metrics.visible_tasks, 1)
        self.assertEqual(snapshot.metrics.pending_checkpoint_count, 1)
        self.assertTrue(any(alert.task_id == "visible-1" for alert in snapshot.alerts))

    async def test_load_task_detail_includes_transcript_and_linked_executions(self) -> None:
        task = Task(
            id="task-1",
            title="Primary task",
            description="Do the work",
            status=TaskStatus.RUNNING,
            session_id="session-1",
            project_id="demo",
            metadata={"progress_log": ["planning", "running"]},
        )
        linked = Task(
            id="task-2",
            title="Linked task",
            description="Hidden execution",
            status=TaskStatus.RUNNING,
            session_id="session-2",
            project_id="demo",
            metadata={"origin_task_id": "task-1"},
        )
        transcript = {
            "session-1": [
                {
                    "message": SessionMessageRecord(
                        message_id="m1",
                        session_id="session-1",
                        role="user",
                        created_at=datetime.now(),
                    ),
                    "parts": [
                        SessionPartRecord(
                            message_id="m1",
                            session_id="session-1",
                            part_type="text",
                            payload={"text": "Please implement the feature"},
                        )
                    ],
                },
                {
                    "message": SessionMessageRecord(
                        message_id="m2",
                        session_id="session-1",
                        role="assistant",
                        created_at=datetime.now(),
                    ),
                    "parts": [
                        SessionPartRecord(
                            message_id="m2",
                            session_id="session-1",
                            part_type="text",
                            payload={"text": "Working on it"},
                        )
                    ],
                },
            ]
        }
        repo = BoardRepository(_StubFacade(_StubStore([task, linked], [], transcript)), project_id="demo")

        detail = await repo.load_task_detail("task-1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.task.task_id, "task-1")
        self.assertEqual(len(detail.transcript), 2)
        self.assertEqual(len(detail.linked_executions), 1)
        self.assertEqual(detail.linked_executions[0].task_id, "task-2")
        self.assertEqual(detail.progress_entries, ["planning", "running"])
        self.assertEqual(detail.task.display_id, "OPC-1")

    async def test_company_mode_snapshot_uses_work_items_as_cards(self) -> None:
        """In company mode, kanban cards come from DelegationWorkItem (not Task).

        Asserts: card task_id == work_item_id; canonical four-state column
        mapping (todo / in-progress / in-review / done); session_id remains
        only as an audit reference; column_order includes "in-review".
        """
        run = DelegationRun(
            run_id="run-1",
            project_id="demo",
            company_profile="corporate",
            lifecycle_status="active",
        )
        # Synthetic root work item — must be hidden (no parent_work_item_id).
        root_item = DelegationWorkItem(
            work_item_id="wi-root",
            run_id="run-1",
            title="Root",
            summary="root",
            phase=Phase.READY,
        )
        wi_todo = DelegationWorkItem(
            work_item_id="wi-todo",
            run_id="run-1",
            parent_work_item_id="wi-root",
            role_id="researcher",
            title="Investigate API",
            summary="Find current rate-limit behavior.",
            phase=Phase.READY,
            kind="execute",
            metadata={"dependency_work_item_ids": []},
        )
        wi_running = DelegationWorkItem(
            work_item_id="wi-running",
            run_id="run-1",
            parent_work_item_id="wi-root",
            role_id="engineer",
            title="Implement fix",
            summary="Patch the throttle.",
            phase=Phase.RUNNING,
            kind="execute",
            claimed_by_role_runtime_session_id="role-rt-1",
            metadata={"activation_state": "active"},
        )
        wi_review = DelegationWorkItem(
            work_item_id="wi-review",
            run_id="run-1",
            parent_work_item_id="wi-root",
            role_id="qa",
            title="Verify deliverable",
            summary="Check the test plan.",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            kind="review",
            metadata={"review_state": "pending_manager"},
        )
        wi_done = DelegationWorkItem(
            work_item_id="wi-done",
            run_id="run-1",
            parent_work_item_id="wi-root",
            role_id="engineer",
            title="Earlier patch",
            summary="Already shipped.",
            phase=Phase.APPROVED,
            kind="execute",
        )
        # A runtime Task linked to wi-running for audit/transcript only.
        linked_task = Task(
            id="task-running",
            title="Implement fix",
            description="runtime execution",
            status=TaskStatus.RUNNING,
            session_id="session-running",
            project_id="demo",
            metadata={},
        )
        set_linked_work_item_id(linked_task, "wi-running")

        store = _StubStore(
            tasks=[linked_task],
            checkpoints=[],
            transcripts={},
            delegation_runs=[run],
            work_items_by_run={"run-1": [root_item, wi_todo, wi_running, wi_review, wi_done]},
        )
        repo = BoardRepository(_StubFacade(store), project_id="demo")
        snapshot = await repo.load_snapshot()

        self.assertEqual(snapshot.mode, "company")
        self.assertEqual(snapshot.column_order, ["todo", "in-progress", "in-review", "done"])
        # Root item is filtered; the four leaf work items become cards.
        self.assertEqual([t.task_id for t in snapshot.tasks], ["wi-todo", "wi-running", "wi-review", "wi-done"])
        # Card identity is the work_item_id, not Task.id.
        self.assertEqual(snapshot.tasks[0].work_item_id, "wi-todo")
        # Four-state canonical column mapping.
        column_by_id = {t.task_id: t.column_id for t in snapshot.tasks}
        self.assertEqual(column_by_id["wi-todo"], "todo")
        self.assertEqual(column_by_id["wi-running"], "in-progress")
        self.assertEqual(column_by_id["wi-review"], "in-review")
        self.assertEqual(column_by_id["wi-done"], "done")
        # session_id is an audit reference only (populated for the linked card).
        running_card = next(t for t in snapshot.tasks if t.task_id == "wi-running")
        self.assertEqual(running_card.session_id, "session-running")
        self.assertEqual(running_card.runtime_task_id, "task-running")
        self.assertEqual(running_card.execution_turn_id, "task-running")
        todo_card = next(t for t in snapshot.tasks if t.task_id == "wi-todo")
        self.assertIsNone(todo_card.session_id)
        self.assertIsNone(todo_card.runtime_task_id)
        self.assertIsNone(todo_card.execution_turn_id)
        # Metrics surface the new in_review_count.
        self.assertEqual(snapshot.metrics.in_review_count, 1)
        self.assertEqual(snapshot.metrics.in_progress_count, 1)
        self.assertEqual(snapshot.metrics.todo_count, 1)
        self.assertEqual(snapshot.metrics.done_count, 1)
        # Hidden count = synthetic root item.
        self.assertEqual(snapshot.hidden_task_count, 1)

    async def test_company_mode_load_task_detail_resolves_work_item_id(self) -> None:
        run = DelegationRun(run_id="run-2", project_id="demo", lifecycle_status="active")
        wi = DelegationWorkItem(
            work_item_id="wi-x",
            run_id="run-2",
            parent_work_item_id="wi-root",
            role_id="engineer",
            title="Detail target",
            summary="check detail path",
            phase=Phase.RUNNING,
            metadata={"handoff_context": "from upstream"},
        )
        linked_task = Task(
            id="task-x",
            title="runtime",
            description="",
            status=TaskStatus.RUNNING,
            session_id="session-x",
            project_id="demo",
            metadata={"progress_log": ["start"]},
        )
        set_linked_work_item_id(linked_task, "wi-x")
        transcript = {
            "session-x": [
                {
                    "message": SessionMessageRecord(
                        message_id="m1", session_id="session-x", role="assistant", created_at=datetime.now()
                    ),
                    "parts": [SessionPartRecord(message_id="m1", session_id="session-x", part_type="text", payload={"text": "ok"})],
                }
            ]
        }
        store = _StubStore(
            tasks=[linked_task],
            checkpoints=[],
            transcripts=transcript,
            delegation_runs=[run],
            work_items_by_run={"run-2": [wi]},
        )
        repo = BoardRepository(_StubFacade(store), project_id="demo")

        detail = await repo.load_task_detail("wi-x")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.task.task_id, "wi-x")
        self.assertEqual(detail.task.work_item_id, "wi-x")
        self.assertEqual(detail.task.runtime_task_id, "task-x")
        self.assertEqual(detail.task.execution_turn_id, "task-x")
        self.assertEqual(detail.context_preview, "from upstream")
        self.assertEqual(len(detail.transcript), 1)
        self.assertEqual(detail.progress_entries, ["start"])

    async def test_snapshot_preserves_adaptive_metadata_for_widgets(self) -> None:
        task = Task(
            id="task-adaptive",
            title="Adaptive task",
            description="Blocked on signals",
            status=TaskStatus.BLOCKED,
            session_id="session-adaptive",
            project_id="demo",
            metadata={
                "adaptive": {
                    "normalized_state": "waiting_for_gate",
                    "blocked_reason": "Waiting for required signals: implementation_ready",
                    "work_item_profile": {"gate_owner_role_id": "cto"},
                    "signals": [
                        {"name": "implementation_ready", "required": True, "satisfied": False},
                    ],
                    "confidence": 0.72,
                }
            },
        )
        repo = BoardRepository(_StubFacade(_StubStore([task], [], {})), project_id="demo")

        snapshot = await repo.load_snapshot()

        self.assertEqual(snapshot.tasks[0].metadata["adaptive"]["normalized_state"], "waiting_for_gate")
        self.assertEqual(snapshot.tasks[0].metadata["adaptive"]["work_item_profile"]["gate_owner_role_id"], "cto")
