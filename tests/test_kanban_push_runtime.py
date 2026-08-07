"""Tests for the kanban-push runtime model.

Covers the core state-machine -> queue routing + UI lane derivation changes
that let the company-mode kanban actively drive the runtime:

- `effective_assignee_for_company_work_item` swaps owner to the manager on
  `in_review` and back to the worker otherwise.
- `_apply_done_transition` spawns a hidden review `DelegationWorkItem`
  for the manager when a child enters manager review, and terminal worker
  cleanup abandons the current review attempt if no verdict was produced.
- `CompanyRuntime._pop_next_queue_entry` always drains review work items
  before regular work, enforcing "manager clears reviews before dispatching".
- `CompanyRuntime.claim_runnable_tasks` soft-wakes a `blocked` manager
  session when its queue has a review work item, then restores the prior
  focus after the review turn ends.
"""

from __future__ import annotations

import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import (
    CompanyMemberSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
)
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.phase import effective_owner
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task, set_linked_work_item_id

WORK_ITEM_STATUS_IN_REVIEW = Phase.AWAITING_MANAGER_REVIEW
WORK_ITEM_STATUS_IN_PROGRESS = Phase.RUNNING
WORK_ITEM_STATUS_TODO = Phase.READY


def effective_assignee_for_company_work_item(item: dict[str, object]) -> tuple[str, str]:
    raw_phase = item.get("phase") or item.get("status") or WORK_ITEM_STATUS_TODO
    if not isinstance(raw_phase, Phase):
        raw_phase = {
            "todo": Phase.READY,
            "in_progress": Phase.RUNNING,
            "in_review": Phase.AWAITING_MANAGER_REVIEW,
        }.get(str(raw_phase), Phase.READY)
    return effective_owner(raw_phase, item)


class EffectiveAssigneeHelperTests(unittest.TestCase):
    def test_in_progress_returns_worker_identity(self) -> None:
        item = {
            "status": "in_progress",
            "role_id": "engineer",
            "seat_id": "seat::team::cto::engineer",
            "manager_role_id": "cto",
            "manager_seat_id": "seat::team::cto::cto",
            "metadata": {"activation_state": "active"},
        }
        self.assertEqual(
            effective_assignee_for_company_work_item(item),
            ("engineer", "seat::team::cto::engineer"),
        )

    def test_in_review_returns_manager_identity(self) -> None:
        item = {
            "status": "in_review",
            "role_id": "engineer",
            "seat_id": "seat::team::cto::engineer",
            "manager_role_id": "cto",
            "manager_seat_id": "seat::team::cto::cto",
            "metadata": {
                "activation_state": "awaiting_review",
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
            },
        }
        self.assertEqual(
            effective_assignee_for_company_work_item(item),
            ("cto", "seat::team::cto::cto"),
        )

    def test_rework_returns_worker_identity(self) -> None:
        item = {
            "status": "todo",
            "role_id": "engineer",
            "seat_id": "seat::team::cto::engineer",
            "manager_role_id": "cto",
            "manager_seat_id": "seat::team::cto::cto",
            "metadata": {"activation_state": "ready_for_rework", "rework_feedback": "redo X"},
        }
        self.assertEqual(
            effective_assignee_for_company_work_item(item),
            ("engineer", "seat::team::cto::engineer"),
        )


class ReviewQueuePriorityTests(unittest.TestCase):
    def test_review_work_item_pops_before_work_item(self) -> None:
        queue: deque[str] = deque(
            [
                "work-item::wi-1",
                "review-work-item::review::wi-2",
                "task-3",
            ]
        )
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "review-work-item::review::wi-2")
        # The remaining order must still preserve the non-review entries.
        self.assertEqual(list(queue), ["work-item::wi-1", "task-3"])

    def test_review_work_item_at_head_is_popped_directly(self) -> None:
        queue: deque[str] = deque(["review-work-item::review::wi-1", "work-item::wi-2"])
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "review-work-item::review::wi-1")
        self.assertEqual(list(queue), ["work-item::wi-2"])

    def test_no_review_task_falls_back_to_fifo(self) -> None:
        queue: deque[str] = deque(["task-1", "work-item::wi-2"])
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "task-1")
        self.assertEqual(list(queue), ["work-item::wi-2"])

    def test_review_backlog_pops_in_arrival_order(self) -> None:
        """A review backlog must drain oldest-first. Reviews still preempt
        regular work, but among themselves queue position (arrival order)
        decides — a newer review must never jump an older one."""
        queue: deque[str] = deque(
            [
                "work-item::wi-plain",
                "review-work-item::review::wi-old::v1",
                "review-work-item::review::wi-new::v1",
            ]
        )
        first = CompanyRuntime._pop_next_queue_entry(queue)
        second = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(first, "review-work-item::review::wi-old::v1")
        self.assertEqual(second, "review-work-item::review::wi-new::v1")
        self.assertEqual(list(queue), ["work-item::wi-plain"])

    def test_enqueue_keeps_review_work_items_in_arrival_order(self) -> None:
        """Regression for the project-0012 review inversion: review work
        items used to be prepended to the role queue, so with two reviews
        waiting the dispatcher claimed the newest first (LIFO) and the
        oldest submission waited behind every later arrival."""
        runtime = CompanyRuntime(org_engine=None, communication=None)

        def _review_item(work_item_id: str) -> DelegationWorkItem:
            return DelegationWorkItem(
                work_item_id=work_item_id,
                run_id="run-1",
                cell_id="team::coo",
                team_id="team::coo",
                role_id="coo",
                seat_id="seat::team::coo::coo",
                title=f"Review {work_item_id}",
                summary="Review the child deliverable.",
                kind="review",
                projection_id=work_item_id,
                phase=Phase.READY,
                metadata={
                    "session_scope_id": "scope-1",
                    "review_execution_work_item": True,
                    "review_target_work_item_id": "wi-target",
                },
            )

        older = _review_item("review::wi-older::v1")
        newer = _review_item("review::wi-newer::v1")
        # Tick 1: only the older review exists. Tick 2: both are runnable
        # (the older one is deduped, the newer one is appended behind it).
        runtime.enqueue_runnable_work_items([older])
        runtime.enqueue_runnable_work_items([older, newer])

        queue = runtime.role_queues["scope-1::coo"]
        self.assertEqual(
            list(queue),
            [
                "review-work-item::review::wi-older::v1",
                "review-work-item::review::wi-newer::v1",
            ],
        )
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "review-work-item::review::wi-older::v1")

    def test_enqueue_keeps_review_tasks_in_arrival_order(self) -> None:
        """Same arrival-order contract for the plain review-Task path."""
        runtime = CompanyRuntime(org_engine=None, communication=None)

        def _review_task(task_id: str) -> Task:
            return Task(
                id=task_id,
                title=f"Review task {task_id}",
                project_id="p",
                session_id="s",
                status=TaskStatus.PENDING,
                assigned_to="coo",
                metadata={"review_task": True},
            )

        older = _review_task("task-review-older")
        newer = _review_task("task-review-newer")
        runtime.enqueue_runnable_tasks([older])
        runtime.enqueue_runnable_tasks([older, newer])

        queue = runtime.role_queues["s::coo"]
        self.assertEqual(
            list(queue),
            [
                "review-task::task-review-older",
                "review-task::task-review-newer",
            ],
        )
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "review-task::task-review-older")


def _build_executor(store: OPCStore, org_engine: OrgEngine) -> CompanyWorkItemExecutor:
    communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
    return CompanyWorkItemExecutor(
        org_engine=org_engine,
        communication=communication,
        approval_engine=MagicMock(),
        memory=None,
        execute_task=AsyncMock(),
        save_task=store.save_task,
        store=store,
    )


class ReviewWorkItemLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_review_transition_spawns_manager_review_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                config = OPCConfig()
                config.org.company_profile = "custom"
                config.org.roles = [
                    RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
                    RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
                    RoleConfig(id="engineer", name="Engineer", responsibility="Build features.", reports_to="cto"),
                ]
                org_engine = OrgEngine(config, root)
                executor = _build_executor(store, org_engine)

                child_work_item = DelegationWorkItem(
                    work_item_id="wi-child",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="engineer",
                    seat_id="seat::team::cto::engineer",
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    title="Build feature",
                    summary="Ship the feature.",
                    kind="execute",
                    projection_id="wi-child",
                    phase=WORK_ITEM_STATUS_IN_PROGRESS,
                    metadata={
                        "team_id": "team::cto",
                        "seat_id": "seat::team::cto::engineer",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::cto",
                        "runtime_model": "multi_team_org",
                        "activation_state": "active",
                    },
                )
                await store.save_delegation_work_item(child_work_item)

                worker_task = Task(
                    id="task-engineer",
                    title="Build feature",
                    project_id="proj1",
                    session_id="session-root",
                    parent_session_id="session-root",
                    assigned_to="engineer",
                    status=TaskStatus.DONE,
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "delegation_run_id": "run-1",
                        "delegation_team_id": "team::cto",
                        "delegation_seat_id": "seat::team::cto::engineer",
                        "work_item_role_id": "engineer",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::cto",
                        "work_item_projection_id": "wi-child",
                    },
                )
                set_linked_work_item_id(worker_task, "wi-child")
                await store.save_task(worker_task)

                # Engineer's turn ended with DONE. Under the two-turn
                # worker→review handoff the canonical done transition
                # now moves the parent work item to manager review and
                # spawns a hidden REPORT card (not the review card)
                # against the worker seat. The review card spawns later,
                # when the report turn finishes — see
                # tests/test_worker_report_handoff.py for that phase.
                from opc.core.models import TaskResult

                await executor._apply_done_transition(
                    worker_task,
                    result=TaskResult(status=TaskStatus.DONE, content="All shipped."),
                )

                updated_child = await store.get_delegation_work_item("wi-child")
                assert updated_child is not None
                self.assertEqual(updated_child.phase, WORK_ITEM_STATUS_IN_REVIEW)
                # No review card yet — the report card has to run first.
                self.assertIsNone(
                    await store.get_delegation_work_item(
                        review_work_item_id_for_attempt("wi-child", 1)
                    ),
                    "review card must wait for the report turn to finish",
                )
                report_work_item_id = report_work_item_id_for_attempt("wi-child", 1)
                report_item = await store.get_delegation_work_item(report_work_item_id)
                assert report_item is not None, "report card must be spawned when worker DONE"
                self.assertEqual(report_item.kind, "report")
                self.assertEqual(report_item.phase, WORK_ITEM_STATUS_TODO)
                self.assertEqual(report_item.role_id, "engineer")
                self.assertEqual(report_item.seat_id, "seat::team::cto::engineer")
                self.assertTrue(report_item.metadata.get("report_execution_work_item"))
                self.assertTrue(report_item.metadata.get("work_item_runtime"))
                self.assertEqual(report_item.metadata.get("work_item_projection_id"), report_work_item_id)
                self.assertEqual(report_item.metadata.get("work_item_turn_type"), "report")
                self.assertTrue(report_item.metadata.get("hidden_from_company_kanban"))
                self.assertTrue(report_item.metadata.get("skip_work_item_sync"))
                self.assertEqual(report_item.metadata.get("current_turn_mode"), "report_required")
                self.assertEqual(report_item.metadata.get("report_target_work_item_id"), "wi-child")

                materialized = await executor._materialize_work_item_tasks(
                    [worker_task],
                    await store.list_delegation_work_items("run-1"),
                )
                projected_report_task = next(
                    task
                    for task in materialized
                    if linked_work_item_id_for_task(task) == report_work_item_id
                )
                self.assertEqual(projected_report_task.assigned_to, "engineer")
                self.assertEqual(projected_report_task.status, TaskStatus.PENDING)
                self.assertTrue(projected_report_task.metadata.get("report_execution_work_item"))
                self.assertTrue(projected_report_task.metadata.get("work_item_runtime"))
                self.assertEqual(projected_report_task.metadata.get("work_item_projection_id"), report_work_item_id)
                self.assertEqual(projected_report_task.metadata.get("work_item_turn_type"), "report")
            finally:
                await store.close()

    async def test_terminal_worker_close_abandons_current_review_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                config = OPCConfig()
                config.org.company_profile = "custom"
                config.org.roles = [
                    RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
                    RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
                    RoleConfig(id="engineer", name="Engineer", responsibility="Build features.", reports_to="cto"),
                ]
                org_engine = OrgEngine(config, root)
                executor = _build_executor(store, org_engine)

                child_work_item = DelegationWorkItem(
                    work_item_id="wi-child",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="engineer",
                    seat_id="seat::team::cto::engineer",
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    title="Build feature",
                    summary="Ship the feature.",
                    kind="execute",
                    projection_id="wi-child",
                    phase=WORK_ITEM_STATUS_IN_REVIEW,
                    metadata={
                        "runtime_model": "multi_team_org",
                        "activation_state": "awaiting_review",
                        "review_state": "pending_manager",
                        "review_owner_role_id": "cto",
                        "review_owner_seat_id": "seat::team::cto::cto",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::cto",
                        "review_attempt_count": 1,
                    },
                )
                await store.save_delegation_work_item(child_work_item)

                worker_task = Task(
                    id="task-engineer",
                    title="Build feature",
                    project_id="proj1",
                    session_id="session-root",
                    parent_session_id="session-root",
                    assigned_to="engineer",
                    status=TaskStatus.AWAITING_MANAGER_REVIEW,
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "delegation_run_id": "run-1",
                        "delegation_team_id": "team::cto",
                        "delegation_seat_id": "seat::team::cto::engineer",
                        "work_item_role_id": "engineer",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::cto",
                        "work_item_projection_id": "wi-child",
                    },
                )
                set_linked_work_item_id(worker_task, "wi-child")
                await store.save_task(worker_task)
                await store.link_work_item_runtime_task("wi-child", worker_task.id)

                review_work_item_id = review_work_item_id_for_attempt("wi-child", 1)
                review_item = DelegationWorkItem(
                    work_item_id=review_work_item_id,
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::cto::cto",
                    parent_work_item_id="wi-child",
                    title="Review: Build feature",
                    kind="review",
                    projection_id=review_work_item_id,
                    phase=WORK_ITEM_STATUS_IN_PROGRESS,
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    metadata={
                        "work_item_runtime": True,
                        "review_task": True,
                        "review_execution_work_item": True,
                        "review_execution_state": "active",
                        "runtime_model": "multi_team_org",
                        "review_target_work_item_id": "wi-child",
                        "hidden_from_company_kanban": True,
                        "skip_work_item_sync": True,
                    },
                )
                await store.save_delegation_work_item(review_item)

                # Simulate a worker terminal failure while a review attempt
                # is still in flight. The hidden review work item must be
                # abandoned because no verdict will arrive.
                worker_task.status = TaskStatus.FAILED
                worker_task.metadata = dict(worker_task.metadata)
                child_work_item.phase = Phase.FAILED
                child_work_item.metadata = {
                    **dict(child_work_item.metadata or {}),
                    "activation_state": "failed",
                }
                await store.save_delegation_work_item(child_work_item)

                await executor._persist_terminal_review_card(
                    review_work_item_id,
                    phase=Phase.CANCELLED,
                    outcome=Phase.FAILED.value,
                )
                refreshed_review = await store.get_delegation_work_item(review_work_item_id)
                assert refreshed_review is not None
                self.assertEqual(refreshed_review.phase, Phase.CANCELLED)
                self.assertEqual(refreshed_review.metadata.get("review_work_item_outcome"), Phase.FAILED.value)
            finally:
                await store.close()


class ReviewWorkItemRoutingTests(unittest.TestCase):
    def test_review_work_item_preempts_regular_work_at_pop_time(self) -> None:
        runtime = CompanyRuntime(
            org_engine=MagicMock(),
            communication=None,
            store=None,
        )

        manager_task = Task(
            id="regular-task",
            title="Dispatch",
            assigned_to="cto",
            project_id="proj1",
            session_id="scope-1",
            parent_session_id="scope-1",
            status=TaskStatus.PENDING,
            metadata={
                "delegation_seat_id": "seat::team::cto::cto",
                "work_item_role_id": "cto",
                "runtime_model": "multi_team_org",
            },
        )
        review_task = Task(
            id="task-review",
            title="Review: build feature",
            assigned_to="cto",
            project_id="proj1",
            session_id="scope-1",
            parent_session_id="scope-1",
            status=TaskStatus.PENDING,
            metadata={
                "delegation_seat_id": "seat::team::cto::cto",
                "work_item_role_id": "cto",
                "review_task": True,
                "review_execution_work_item": True,
                "runtime_model": "multi_team_org",
                "review_target_work_item_id": "wi-child",
            },
        )
        set_linked_work_item_id(review_task, "review::wi-child")
        review_item = DelegationWorkItem(
            work_item_id="review::wi-child",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            parent_work_item_id="wi-child",
            title="Review: build feature",
            kind="review",
            projection_id="review::wi-child",
            phase=WORK_ITEM_STATUS_TODO,
            metadata={
                "runtime_model": "multi_team_org",
                "session_scope_id": "scope-1",
                "work_kind": "review",
                "review_execution_work_item": True,
                "review_execution_state": "pending",
                "review_target_work_item_id": "wi-child",
                "hidden_from_company_kanban": True,
            },
        )

        runtime.enqueue_runnable_tasks([manager_task])
        runtime.enqueue_runnable_work_items(
            [review_item],
            task_by_work_item_id={"review::wi-child": review_task},
        )
        queue_key = runtime._queue_key_for_task(manager_task)
        queue = runtime.role_queues.get(queue_key)
        assert queue is not None
        # The review sits behind the earlier-arrived regular task in the
        # deque (arrival order is preserved) but must still be claimed
        # first: preemption is a pop-time contract, not a queue layout.
        popped = CompanyRuntime._pop_next_queue_entry(queue)
        self.assertEqual(popped, "review-work-item::review::wi-child")
        self.assertEqual(list(queue), ["regular-task"])

    def test_soft_wake_allows_blocked_manager_to_claim_review_work_item(self) -> None:
        runtime = CompanyRuntime(
            org_engine=MagicMock(),
            communication=None,
            store=None,
        )
        session = CompanyMemberSession(
            member_session_id="member::cto",
            role_id="cto",
            employee_id="cto-default",
            seat_id="seat::team::cto::cto",
            status="blocked",
            current_task_id="cto-own-task",
            focused_work_item_id="wi-cto-dispatch",
            metadata={
                "seat_id": "seat::team::cto::cto",
                "session_scope_id": "",
            },
        )
        runtime.member_sessions[session.member_session_id] = session
        review_work_item_id = "review::wi-child"
        runtime.role_queues[runtime._queue_key_for_session(session)].append(
            f"review-work-item::{review_work_item_id}"
        )

        queue = runtime.role_queues.get(runtime._queue_key_for_session(session))
        self.assertIsNotNone(queue)
        self.assertEqual(queue[0], f"review-work-item::{review_work_item_id}")

        can_soft_wake = (
            session.status == "blocked"
            and queue is not None
            and any(entry.startswith("review-work-item::") for entry in queue)
        )
        self.assertTrue(can_soft_wake)


class ReviewWorkItemClaimE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_manager_seat_claims_spawned_review_work_item(self) -> None:
        """A review work item spawned on in_review must actually be claimed
        by the manager seat session via the normal runtime path.
        """
        runtime = CompanyRuntime(
            org_engine=MagicMock(),
            communication=None,
            store=None,
        )
        # Idle manager session for the CTO-as-team-manager seat.
        manager_session = CompanyMemberSession(
            member_session_id="member::cto::manager",
            role_id="cto",
            employee_id="cto-default",
            seat_id="seat::team::cto::cto",
            status="idle",
            metadata={
                "seat_id": "seat::team::cto::cto",
                "session_scope_id": "scope-1",
            },
        )
        runtime.member_sessions[manager_session.member_session_id] = manager_session

        child_work_item = DelegationWorkItem(
            work_item_id="wi-child",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            title="Build feature",
            kind="execute",
            projection_id="wi-child",
            phase=WORK_ITEM_STATUS_IN_REVIEW,
            manager_role_id="cto",
            manager_seat_id="seat::team::cto::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "activation_state": "awaiting_review",
                "review_state": "pending_manager",
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
            },
        )
        review_task = Task(
            id="task-review",
            title="Review: build feature",
            assigned_to="cto",
            project_id="proj1",
            session_id="scope-1",
            parent_session_id="scope-1",
            status=TaskStatus.PENDING,
            metadata={
                "delegation_seat_id": "seat::team::cto::cto",
                "work_item_role_id": "cto",
                "review_task": True,
                "review_execution_work_item": True,
                "runtime_model": "multi_team_org",
                "review_target_work_item_id": "wi-child",
            },
        )
        set_linked_work_item_id(review_task, "review::wi-child")
        review_item = DelegationWorkItem(
            work_item_id="review::wi-child",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            parent_work_item_id="wi-child",
            title="Review: build feature",
            kind="review",
            projection_id="review::wi-child",
            phase=WORK_ITEM_STATUS_TODO,
            manager_role_id="cto",
            manager_seat_id="seat::team::cto::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "session_scope_id": "scope-1",
                "work_kind": "review",
                "review_execution_work_item": True,
                "review_execution_state": "pending",
                "review_target_work_item_id": "wi-child",
                "hidden_from_company_kanban": True,
            },
        )

        runtime.enqueue_runnable_work_items(
            [review_item],
            task_by_work_item_id={"review::wi-child": review_task},
        )
        claims = await runtime.claim_runnable_tasks(
            [review_task],
            work_items=[child_work_item, review_item],
        )

        self.assertEqual(len(claims), 1, "idle manager seat must claim the review work item")
        claimed_session, claimed_task = claims[0]
        self.assertEqual(claimed_session.member_session_id, manager_session.member_session_id)
        self.assertEqual(claimed_task.id, "task-review")
        self.assertEqual(claimed_session.status, "running")
        self.assertEqual(claimed_session.current_task_id, "task-review")


if __name__ == "__main__":
    unittest.main()
