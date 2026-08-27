"""Tests for the automatic worker→manager review handoff in company mode.

Validates the three hooks that glue worker completion to manager review:

1. Worker completion automatically transitions the work item to ``in_review``
   through the canonical done transition and preserves the ``manager_role_id`` /
   ``manager_seat_id`` of the reviewer even when the task metadata has lost
   them (common after work-item runtime rehydration).

2. ``CompanyRuntime.complete_claim`` sends an ``APPROVAL_REQUEST`` message to
   the manager with ``reply_needed=True`` and ``review_required`` metadata so
   the dispatch loop wakes the manager up through the existing
   attention-work-item mechanism.

3. ``_resolve_current_turn_mode`` returns ``"review_pending"`` when the manager
   has direct reports with work items awaiting review, overriding
   ``dispatch_required`` / ``monitor_children``. The contract builder injects
   a ``## Review Requirement`` block listing the pending items.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from opc.core.models import (
    AgentMessage,
    CommsSemanticType,
    CompanyMemberSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.prompt_contract import make_prompt_contract
from opc.layer2_organization.work_item_transition import transition_work_item_from_task
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.company_runtime_contract import (
    build_company_work_item_contract,
    build_external_company_work_item_contract,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class RecordingCommunication:
    """Communication stub that captures manager notifications."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_manager_notification(
        self,
        *,
        from_agent: str,
        task: Task | None,
        semantic_type: CommsSemanticType,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        reply_needed: bool = False,
        requires_ack: bool = False,
    ) -> AgentMessage:
        record = {
            "from_agent": from_agent,
            "task": task,
            "semantic_type": semantic_type,
            "subject": subject,
            "body": body,
            "metadata": dict(metadata or {}),
            "reply_needed": reply_needed,
            "requires_ack": requires_ack,
        }
        self.calls.append(record)
        return AgentMessage(
            from_agent=from_agent,
            to_agents=["manager"],
            subject=subject,
            body=body,
            semantic_type=semantic_type,
            reply_needed=reply_needed,
            requires_ack=requires_ack,
            metadata=dict(metadata or {}),
        )

    async def read_inbox(self, **_: Any) -> list[dict[str, Any]]:
        return []


class StubOrgEngine:
    """Minimal org engine providing reports_to resolution for tests."""

    def __init__(self) -> None:
        self._roles = {
            "ceo": SimpleNamespace(role_id="ceo", reports_to="owner"),
            "cto": SimpleNamespace(role_id="cto", reports_to="ceo"),
            "engineer": SimpleNamespace(role_id="engineer", reports_to="cto"),
        }

    def get_agent(self, role_id: str):
        return self._roles.get(role_id)


def _make_worker_session(
    *,
    role_id: str = "engineer",
    seat_id: str = "seat::eng",
    manager_role_id: str = "cto",
    manager_seat_id: str = "seat::cto",
    status: str = "running",
) -> CompanyMemberSession:
    session = CompanyMemberSession(
        member_session_id=f"member::{role_id}",
        role_id=role_id,
        employee_id="employee-1",
        seat_id=seat_id,
        status=status,
        resident_status=status,
        manager_role_id=manager_role_id,
        manager_role_ids=[manager_role_id] if manager_role_id else [],
        metadata={
            "seat_id": seat_id,
            "manager_role_id": manager_role_id,
            "manager_seat_id": manager_seat_id,
            "session_scope_id": "scope-1",
            "runtime_model": "multi_team_org",
        },
    )
    return session


def _make_manager_session(
    *,
    role_id: str = "cto",
    seat_id: str = "seat::cto",
    direct_report_seat_ids: list[str] | None = None,
    direct_report_role_ids: list[str] | None = None,
    manager_board_summary: dict[str, Any] | None = None,
) -> CompanyMemberSession:
    session = CompanyMemberSession(
        member_session_id=f"member::{role_id}",
        role_id=role_id,
        employee_id="mgr-employee",
        seat_id=seat_id,
        status="idle",
        resident_status="idle",
        manager_role_id="ceo",
        manager_role_ids=["ceo"],
        metadata={
            "seat_id": seat_id,
            "runtime_model": "multi_team_org",
            "direct_report_seat_ids": list(direct_report_seat_ids or []),
            "direct_report_role_ids": list(direct_report_role_ids or []),
            "managed_team_id": "team::cto",
            "manager_board_summary": dict(manager_board_summary or {}),
        },
    )
    return session


def _make_worker_task(
    *,
    task_id: str = "task-engineer",
    work_item_id: str = "work-item-engineer",
    status: TaskStatus = TaskStatus.DONE,
    manager_role_id: str = "cto",
    manager_seat_id: str = "seat::cto",
) -> Task:
    task = Task(
        id=task_id,
        title="Engineer deliverable",
        assigned_to="engineer",
        status=status,
        project_id="proj1",
        metadata={
            "runtime_model": "multi_team_org",
            "work_item_role_id": "engineer",
            "work_item_projection_id": "engineer__execute",
            "manager_role_id": manager_role_id,
            "manager_seat_id": manager_seat_id,
            "delegation_run_id": "run-1",
        },
    )
    set_linked_work_item_id(task, work_item_id)
    return task


async def _apply_task_transition(
    executor: Any,
    task: Task,
    *,
    status: str,
    result: TaskResult | None = None,
) -> None:
    if status == TaskStatus.DONE.value:
        await executor._apply_done_transition(task, result=result)
        return
    summary = ""
    if result is not None:
        summary = str(result.content or "").strip()
    try:
        target: TaskStatus | str = TaskStatus(status)
    except ValueError:
        target = status
    await transition_work_item_from_task(
        executor.store,
        task,
        target_status_or_phase=target,
        reason="test_task_transition",
        summary=summary or None,
    )
    await executor._notify_kanban_changed()


# ---------------------------------------------------------------------------
# complete_claim() review-notification hook
# ---------------------------------------------------------------------------


class CompleteClaimReviewNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_done_sends_approval_request_with_reply_needed(self) -> None:
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session(status="running")
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task(status=TaskStatus.DONE)

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(status=TaskStatus.DONE, content="Deliverable ready for review."),
        )

        # One or more notifications sent. The FIRST should be the review
        # request (semantic=APPROVAL_REQUEST, reply_needed=True).
        review_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.APPROVAL_REQUEST
        ]
        self.assertEqual(len(review_calls), 1)
        review = review_calls[0]
        self.assertTrue(review["reply_needed"])
        self.assertIn("Review needed", review["subject"])
        self.assertEqual(review["metadata"]["work_item_id"], "work-item-engineer")
        self.assertTrue(review["metadata"]["review_required"])
        self.assertEqual(review["metadata"]["completion_report"], "Deliverable ready for review.")
        # Session state cleared.
        self.assertEqual(session.status, "idle")
        self.assertEqual(session.current_assignment, {})

    async def test_worker_done_without_summary_still_triggers_review(self) -> None:
        """Edge case: worker finished but produced no content. The manager
        still needs to be woken up to inspect the artifacts directly."""
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session()
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task(status=TaskStatus.DONE)

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(status=TaskStatus.DONE, content=""),
        )

        review_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.APPROVAL_REQUEST
        ]
        self.assertEqual(len(review_calls), 1)
        self.assertTrue(review_calls[0]["reply_needed"])
        # Body must still contain actionable context so the manager doesn't
        # receive an empty ping.
        self.assertTrue(review_calls[0]["body"])
        self.assertIn("work-item-engineer", review_calls[0]["body"])

    async def test_worker_awaiting_manager_review_releases_role_session(self) -> None:
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session(status="running")
        session.focused_work_item_id = "work-item-engineer"
        session.current_task_id = "task-engineer"
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task(status=TaskStatus.AWAITING_MANAGER_REVIEW)

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(
                status=TaskStatus.AWAITING_MANAGER_REVIEW,
                content="Submitted for manager review.",
            ),
        )

        self.assertEqual(session.status, "idle")
        self.assertEqual(session.resident_status, "idle")
        self.assertEqual(session.focused_work_item_id, "")
        self.assertEqual(session.current_task_id, "")
        blocker_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.BLOCKER
        ]
        self.assertEqual(blocker_calls, [])

    async def test_blocked_worker_does_not_request_review(self) -> None:
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session()
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task(status=TaskStatus.AWAITING_PEER)
        task.metadata["peer_wait"] = {"waiting_on_agents": ["reviewer"]}

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(status=TaskStatus.AWAITING_PEER, content="Waiting for reviewer."),
        )

        approval_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.APPROVAL_REQUEST
        ]
        self.assertEqual(approval_calls, [])
        # A blocker notification is still sent so the manager sees the status.
        blocker_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.BLOCKER
        ]
        self.assertTrue(blocker_calls)

    async def test_worker_without_manager_does_not_request_review(self) -> None:
        """Top-level role (CEO) completing a root task has no manager to
        notify; the code path must degrade gracefully."""
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session(
            role_id="ceo",
            manager_role_id="",
            manager_seat_id="",
        )
        session.manager_role_id = ""
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task(manager_role_id="", manager_seat_id="")
        task.metadata["manager_role_id"] = ""
        task.metadata["manager_seat_id"] = ""

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(status=TaskStatus.DONE, content="All done."),
        )

        self.assertEqual(communication.calls, [])

    async def test_non_multi_team_org_sends_work_item_result_not_approval(self) -> None:
        """Work-item runtime company mode (not multi_team_org) should keep the
        legacy WORK_ITEM_RESULT semantic unchanged."""
        communication = RecordingCommunication()
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=communication)
        session = _make_worker_session()
        # Remove the multi_team_org marker from session metadata.
        session.metadata.pop("runtime_model", None)
        runtime.member_sessions[session.member_session_id] = session
        task = _make_worker_task()
        task.metadata.pop("runtime_model", None)

        await runtime.complete_claim(
            session,
            task,
            result=TaskResult(status=TaskStatus.DONE, content="Work item output."),
        )

        work_item_calls = [
            call for call in communication.calls
            if call["semantic_type"] == CommsSemanticType.WORK_ITEM_RESULT
        ]
        self.assertEqual(len(work_item_calls), 1)
        self.assertFalse(work_item_calls[0]["reply_needed"])


# ---------------------------------------------------------------------------
# _resolve_current_turn_mode review_pending detection
# ---------------------------------------------------------------------------


class ReviewPendingTurnModeTests(unittest.TestCase):
    def _runtime(self) -> CompanyRuntime:
        return CompanyRuntime(org_engine=StubOrgEngine(), communication=RecordingCommunication())

    def test_pending_in_review_children_yields_review_pending(self) -> None:
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            direct_report_role_ids=["engineer"],
            manager_board_summary={
                "total_children": 2,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                        "review_owner_role_id": "cto",
                        "review_owner_seat_id": "seat::cto",
                        "title": "Slice 1",
                        "role_id": "engineer",
                    },
                    {
                        "work_item_id": "wi-2",
                        "phase": "running",
                        "kanban_column": "in_progress",
                        "title": "Slice 2",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertEqual(mode, "review_pending")

    def test_no_pending_reviews_falls_back_to_monitor_children(self) -> None:
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            direct_report_role_ids=["engineer"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "running",
                        "kanban_column": "in_progress",
                        "title": "Slice 1",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertEqual(mode, "monitor_children")

    def test_already_approved_item_does_not_count_as_pending(self) -> None:
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "approved",
                        "kanban_column": "done",
                        "review_owner_role_id": "cto",
                        "title": "Slice 1",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertEqual(mode, "monitor_children")

    def test_pending_review_owned_by_other_role_is_ignored(self) -> None:
        """A human-gate item whose reviewer is not this manager must not
        flip this manager into review_pending mode."""
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-human",
                        "phase": "awaiting_human",
                        "kanban_column": "in_review",
                        "review_owner_role_id": "owner",
                        "review_owner_seat_id": "seat::owner",
                        "title": "Needs human sign-off",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertNotEqual(mode, "review_pending")

    def test_review_pending_overrides_dispatch_required(self) -> None:
        """With no children yet a manager would see ``dispatch_required``.
        If a pending review exists (e.g. from a prior batch), it takes
        priority so the manager clears the queue before dispatching more."""
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                        "title": "Slice 1",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertEqual(mode, "review_pending")

    def test_attention_review_wrapper_stays_review_pending(self) -> None:
        """A wake-up Review Turn wrapper is not the dedicated child review card.

        It must surface the pending-review queue, not run a target-less review
        against the stale original session brief.
        """
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            direct_report_role_ids=["engineer"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                        "title": "Coral implementation",
                        "role_id": "engineer",
                    },
                ],
            },
        )
        task = Task(
            id="attention-review",
            title="Review Turn: cto",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "review",
                "attention_work_item": True,
            },
        )

        mode = runtime._resolve_current_turn_mode(manager, task)

        self.assertEqual(mode, "review_pending")

    def test_non_multi_team_org_returns_empty(self) -> None:
        """For legacy company mode (runtime_model != multi_team_org) the
        runtime returns an empty string regardless of pending reviews."""
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                    },
                ],
            },
        )
        manager.metadata["runtime_model"] = ""  # pretend not multi-team-org
        runtime.member_sessions[manager.member_session_id] = manager
        mode = runtime._resolve_current_turn_mode(manager)
        self.assertEqual(mode, "")

    def test_dedicated_review_work_item_forces_review_execute(self) -> None:
        runtime = self._runtime()
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            direct_report_role_ids=["engineer"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                        "review_owner_role_id": "cto",
                        "review_owner_seat_id": "seat::cto",
                    },
                ],
            },
        )
        runtime.member_sessions[manager.member_session_id] = manager
        task = Task(
            id="review-turn",
            title="Review: wi-1",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "review",
                "review_execution_work_item": True,
                "review_target_work_item_id": "wi-1",
                "delegation_seat_id": "seat::cto",
            },
        )
        set_linked_work_item_id(task, "review::wi-1")
        mode = runtime._resolve_current_turn_mode(manager, task)
        self.assertEqual(mode, "review_execute")


# ---------------------------------------------------------------------------
# Contract builder: review_pending prompt injection
# ---------------------------------------------------------------------------


class ReviewPendingContractTests(unittest.TestCase):
    def test_native_contract_keeps_native_tool_wording_by_default(self) -> None:
        task = Task(
            id="plan-turn",
            title="Plan turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={"work_item_turn_type": "plan"},
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("agent_spawn(profile='explore')", contract)

    def test_external_contract_uses_external_tool_wording_across_turns(self) -> None:
        cases = [
            Task(
                id="intake-turn",
                title="Intake turn",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.PENDING,
                metadata={"work_item_turn_type": "intake"},
            ),
            Task(
                id="execute-turn",
                title="Execute turn",
                project_id="proj1",
                assigned_to="engineer",
                status=TaskStatus.PENDING,
                metadata={"work_item_turn_type": "execute"},
            ),
            Task(
                id="review-turn",
                title="Review turn",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.PENDING,
                metadata={"work_item_turn_type": "review"},
            ),
            Task(
                id="dispatch-turn",
                title="Dispatch turn",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.PENDING,
                metadata={
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "direct_report_seat_ids": ["seat::team::ceo::cto"],
                },
            ),
            Task(
                id="report-turn",
                title="Report turn",
                project_id="proj1",
                assigned_to="engineer",
                status=TaskStatus.PENDING,
                metadata={
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "report_required",
                    "report_execution_work_item": True,
                },
            ),
        ]

        for task in cases:
            with self.subTest(task=task.id):
                contract = build_external_company_work_item_contract(task)
                self.assertNotIn("agent_spawn(profile='explore')", contract)
                self.assertNotIn("file_read", contract)
                self.assertNotIn("git_*", contract)
                self.assertNotIn("bash", contract)

        review_contract = build_company_work_item_contract(cases[2], audience="external")
        self.assertIn("external agent", review_contract)
        self.assertIn('"review_verdict":"approve"', review_contract)
        self.assertIn('"review_verdict":"reject"', review_contract)

    def test_contract_includes_review_requirement_block(self) -> None:
        task = Task(
            id="mgr-turn",
            title="Manager turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "review_pending",
                "pending_review_items": [
                    {
                        "work_item_id": "wi-1",
                        "title": "Slice 1",
                        "role_id": "engineer",
                        "deliverable_summary": "Implemented rollback path.",
                        "review_state": "pending_manager",
                    }
                ],
            },
        )

        contract = build_company_work_item_contract(task)
        self.assertIn("Review Requirement", contract)
        self.assertIn("review_verdict", contract)
        self.assertIn("wi-1", contract)
        self.assertIn("engineer", contract)
        self.assertIn("Implemented rollback path", contract)
        self.assertIn("only provide analysis/planning/search notes", contract)
        self.assertIn("Previous review findings are only leads", contract)

    def test_attention_review_wrapper_uses_pending_queue_contract(self) -> None:
        task = Task(
            id="attention-review",
            title="Review Turn: cto",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "review_pending",
                "work_item_turn_type": "review",
                "attention_work_item": True,
                "direct_report_seat_ids": ["seat::eng"],
                "pending_review_items": [
                    {
                        "work_item_id": "wi-1",
                        "title": "Coral implementation",
                        "role_id": "engineer",
                        "deliverable_summary": "Implemented Coral Magnet Maze.",
                    }
                ],
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("Review Requirement", contract)
        self.assertIn("wi-1", contract)
        self.assertIn("Implemented Coral Magnet Maze", contract)
        self.assertNotIn("Kanban Review Turn", contract)

    def test_contract_without_pending_items_omits_review_block(self) -> None:
        task = Task(
            id="mgr-turn",
            title="Manager turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "direct_report_seat_ids": ["seat::eng"],
            },
        )
        contract = build_company_work_item_contract(task)
        self.assertNotIn("Review Requirement", contract)

    def test_review_block_header_still_emitted_when_items_payload_missing(self) -> None:
        """Even if the runtime forgot to attach ``pending_review_items`` the
        header block should still fire based on the turn_mode signal so the
        manager sees a coherent instruction."""
        task = Task(
            id="mgr-turn",
            title="Manager turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "review_pending",
            },
        )
        contract = build_company_work_item_contract(task)
        self.assertIn("Review Requirement", contract)
        self.assertIn("review_verdict", contract)

    def test_dedicated_review_turn_renders_structured_review_evidence(self) -> None:
        task = Task(
            id="mgr-review-turn",
            title="Review: Slice 1",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "review",
                "current_turn_mode": "review_execute",
                "review_execution_work_item": True,
                "review_target_work_item_id": "wi-1",
                "review_target_worker_role_id": "engineer",
                "review_target_title": "Slice 1",
                "review_target_description": "Implement the rollback path.",
                "review_target_prompt_contract": make_prompt_contract(
                    task_brief=(
                        "Implement the rollback path. "
                        + "This long target brief must remain visible in full. " * 30
                    ),
                    deliverables=["Rollback implementation"],
                    acceptance_criteria=["Rollback test passes"],
                    non_overlap_guard="Do not modify unrelated auth flows.",
                ),
                "review_completion_report": "Completed and validated rollback support.",
                "review_evidence": {
                    "artifact_manifest": [
                        {"kind": "file", "label": "report", "value": "reports/review.md"}
                    ],
                    "changed_areas": ["src/api.py", "tests/test_api.py"],
                    "verification_results": {
                        "status": {"label": "verified", "summary": "pytest passed"},
                        "checks": [
                            {"command": "pytest -q", "status": "pass", "summary": "42 passed"}
                        ],
                    },
                    "key_commands": ["pytest -q"],
                    "output_paths": ["reports/review.md"],
                    "open_risks": ["Needs auth review"],
                },
            },
        )
        contract = build_company_work_item_contract(task)
        self.assertIn("Kanban Review Turn", contract)
        # The new prompt phrases this as "Treat the original brief as
        # the contract." rather than the previous "original child brief
        # as the contract." wording — both convey the same idea.
        self.assertIn("original brief as the contract", contract)
        self.assertIn("This long target brief must remain visible in full.", contract)
        self.assertIn("Rollback implementation", contract)
        self.assertIn("Rollback test passes", contract)
        self.assertIn("Do not modify unrelated auth flows.", contract)
        self.assertNotIn("brief=`", contract)
        self.assertIn("Current workspace evidence is the truth", contract)
        self.assertIn("Artifact Manifest", contract)
        self.assertIn("Verification", contract)
        self.assertIn("Output Paths", contract)
        self.assertIn("pytest -q", contract)
        self.assertIn("Needs auth review", contract)


# ---------------------------------------------------------------------------
# Company comms reactivation must stay scoped to the relevant work item.
# ---------------------------------------------------------------------------


class WorkItemScopedCommsTests(unittest.TestCase):
    def test_actionable_unread_scope_uses_target_work_item_not_role_inbox(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        self.assertTrue(
            CompanyWorkItemExecutor._unread_header_matches_scope(
                {
                    "semantic_type": "approval_request",
                    "target_work_item_id": "wi-target",
                    "task_id": "legacy-task",
                },
                work_item_id="wi-target",
                task_id="task-other",
                require_work_item_scope=True,
            )
        )
        self.assertFalse(
            CompanyWorkItemExecutor._unread_header_matches_scope(
                {
                    "semantic_type": "approval_request",
                    "target_work_item_id": "wi-other",
                    "task_id": "legacy-task",
                },
                work_item_id="wi-target",
                task_id="task-other",
                require_work_item_scope=True,
            )
        )

    def test_parent_work_item_context_does_not_match_child_reactivation_scope(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        self.assertFalse(
            CompanyWorkItemExecutor._unread_header_matches_scope(
                {
                    "semantic_type": "approval_request",
                    "parent_work_item_id": "wi-parent",
                    "target_work_item_id": "wi-child",
                },
                work_item_id="wi-parent",
                task_id="task-parent",
                require_work_item_scope=True,
            )
        )


# ---------------------------------------------------------------------------
# Pending review context snapshot injection
# ---------------------------------------------------------------------------


class PendingReviewContextInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_task_copies_pending_review_items(self) -> None:
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=RecordingCommunication())
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={
                "total_children": 1,
                "upstream_summary": [
                    {
                        "work_item_id": "wi-1",
                        "phase": "awaiting_manager_review",
                        "kanban_column": "in_review",
                        "review_owner_role_id": "cto",
                        "review_owner_seat_id": "seat::cto",
                        "title": "Slice 1",
                        "role_id": "engineer",
                    }
                ],
            },
        )
        # Emulate what _refresh_manager_board_state would populate.
        pending_items = runtime._pending_reviews_from_board_summary(manager)
        manager.metadata["pending_review_items"] = pending_items
        runtime.member_sessions[manager.member_session_id] = manager

        task = Task(
            id="mgr-turn",
            title="CTO turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_role_id": "cto",
            },
        )
        set_linked_work_item_id(task, "parent-wi")
        runtime.prepare_task_for_session(manager, task)

        items = task.context_snapshot.get("pending_review_items")
        self.assertIsNotNone(items)
        self.assertEqual(items[0]["work_item_id"], "wi-1")
        self.assertEqual(task.metadata["pending_review_items"][0]["work_item_id"], "wi-1")

    async def test_prepare_task_clears_pending_review_items_when_empty(self) -> None:
        runtime = CompanyRuntime(org_engine=StubOrgEngine(), communication=RecordingCommunication())
        manager = _make_manager_session(
            direct_report_seat_ids=["seat::eng"],
            manager_board_summary={"total_children": 0, "upstream_summary": []},
        )
        runtime.member_sessions[manager.member_session_id] = manager

        task = Task(
            id="mgr-turn",
            title="CTO turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_role_id": "cto",
                "pending_review_items": [{"work_item_id": "stale"}],
            },
            context_snapshot={"pending_review_items": [{"work_item_id": "stale"}]},
        )
        set_linked_work_item_id(task, "parent-wi")
        runtime.prepare_task_for_session(manager, task)

        self.assertNotIn("pending_review_items", task.context_snapshot)
        self.assertNotIn("pending_review_items", task.metadata)


# ---------------------------------------------------------------------------
# End-to-end: task transition → summarize_parent_status →
# turn_mode transition. Uses a real OPCStore so the canonicalisation,
# upstream_summary payload, and review_owner metadata round-trip work.
# ---------------------------------------------------------------------------


class TaskTransitionReviewFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix=f"review-flow-{uuid.uuid4().hex}-"))
        self.store = OPCStore(self._tmp / "tasks.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _seed_run_and_work_item(self) -> DelegationWorkItem:
        from opc.core.models import Phase

        item = DelegationWorkItem(
            work_item_id="wi-engineer",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::eng",
            parent_work_item_id="wi-cto",
            title="Engineer deliverable",
            summary="",
            kind="execute",
            projection_id="engineer__execute",
            phase=Phase.RUNNING,
            manager_role_id="cto",
            manager_seat_id="seat::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "dependency_work_item_ids": [],
            },
        )
        await self.store.save_delegation_work_item(item)
        return item

    async def test_done_task_transitions_work_item_to_in_review(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        await self._seed_run_and_work_item()

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.DONE,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "manager_role_id": "cto",
                "manager_seat_id": "seat::cto",
                "work_item_projection_id": "engineer__execute",
            },
        )
        set_linked_work_item_id(task, "wi-engineer")

        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.DONE.value,
            result=TaskResult(status=TaskStatus.DONE, content="All green."),
        )

        refreshed = await self.store.get_delegation_work_item("wi-engineer")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(refreshed.metadata.get("review_owner_role_id"), "cto")
        self.assertEqual(refreshed.metadata.get("review_owner_seat_id"), "seat::cto")
        self.assertEqual(refreshed.metadata.get("completion_report"), "All green.")

    def test_approve_with_missing_evidence_is_treated_as_review_blocker(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        reason = CompanyWorkItemExecutor._review_approval_blocker_reason({
            "structured_review_verdict": {"label": "approve", "summary": "Looks good."},
            "review_completion_report": "No artifacts were provided, so there is no evidence to verify.",
            "review_evidence": {
                "artifact_manifest": [],
                "output_paths": [],
                "verification_results": {
                    "status": {"label": "missing", "summary": "No checks were run."},
                    "checks": [],
                },
            },
        })

        self.assertIn("verification evidence", reason)

    def test_approve_with_generic_not_verified_evidence_is_not_a_hard_blocker(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        reason = CompanyWorkItemExecutor._review_approval_blocker_reason({
            "structured_review_verdict": {"label": "approve", "summary": "Reviewer verified the handoff."},
            "review_completion_report": "Artifacts are present; external verification was not run.",
            "review_evidence": {
                "artifact_manifest": [{"path": "TEST_REPORT.md", "status": "provided"}],
                "output_paths": ["TEST_REPORT.md"],
                "verification_results": {
                    "status": {"label": "not_verified", "summary": "No automated verifier was available."},
                    "checks": [],
                },
            },
        })

        self.assertEqual(reason, "")

    def test_approve_cannot_override_failed_or_partial_verification(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        for label in ("failed", "partial"):
            with self.subTest(label=label):
                reason = CompanyWorkItemExecutor._review_approval_blocker_reason({
                    "structured_review_verdict": {
                        "label": "approve",
                        "summary": "Looks good.",
                    },
                    "review_completion_report": "Reviewer approved the handoff.",
                    "review_evidence": {
                        "artifact_manifest": [
                            {"path": "TEST_REPORT.md", "status": "provided"}
                        ],
                        "output_paths": ["TEST_REPORT.md"],
                        "verification_results": {
                            "status": {
                                "label": label,
                                "summary": "Verifier found unresolved quality issues.",
                            },
                            "checks": [],
                        },
                    },
                })

                self.assertIn(f"`{label}`", reason)

    async def test_failed_task_can_close_in_progress_work_item_as_terminal_done(self) -> None:
        """Terminal failures should close a claimed work item without routing
        through manager review."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        await self._seed_run_and_work_item()

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.FAILED,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "manager_role_id": "cto",
                "manager_seat_id": "seat::cto",
                "work_item_projection_id": "engineer__execute",
            },
        )
        set_linked_work_item_id(task, "wi-engineer")

        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.FAILED.value,
            result=TaskResult(status=TaskStatus.FAILED, content="Dispatch guard rejected the turn."),
        )

        refreshed = await self.store.get_delegation_work_item("wi-engineer")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.phase, Phase.FAILED)

    async def test_done_transition_falls_back_to_work_item_manager_when_task_metadata_missing(self) -> None:
        """After work-item runtime rehydration, the task may have lost its
        ``manager_role_id``/``manager_seat_id``. The done transition must look
        up the work item itself so review_owner is still populated."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        await self._seed_run_and_work_item()

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.DONE,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                # manager_role_id / manager_seat_id deliberately absent.
                "work_item_projection_id": "engineer__execute",
            },
        )
        set_linked_work_item_id(task, "wi-engineer")

        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.DONE.value,
            result=TaskResult(status=TaskStatus.DONE, content="Output."),
        )

        refreshed = await self.store.get_delegation_work_item("wi-engineer")
        self.assertEqual(refreshed.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(refreshed.metadata.get("review_owner_role_id"), "cto")
        self.assertEqual(refreshed.metadata.get("review_owner_seat_id"), "seat::cto")

    async def test_done_transition_does_not_regress_approved_work_item_back_to_review(self) -> None:
        """A repeated late DONE transition must preserve an already-approved phase.

        Shared role sessions can emit a second completion after a review
        verdict has already finalized the worker item. That follow-up must
        not try to send APPROVED -> AWAITING_MANAGER_REVIEW, which violates
        the phase state machine and crashes the claimed work item.
        """
        from opc.core.models import Phase
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        await self._seed_run_and_work_item()
        await self.store.update_delegation_work_item("wi-engineer", phase=Phase.APPROVED)

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.DONE,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "manager_role_id": "cto",
                "manager_seat_id": "seat::cto",
                "work_item_projection_id": "engineer__execute",
            },
        )
        set_linked_work_item_id(task, "wi-engineer")

        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.DONE.value,
            result=TaskResult(status=TaskStatus.DONE, content="Late duplicate sync."),
        )

        refreshed = await self.store.get_delegation_work_item("wi-engineer")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.phase, Phase.APPROVED)

    async def test_summarize_parent_status_surfaces_review_owner_fields(self) -> None:
        """The per-session upstream summary must now include review_owner_*
        fields so ``_pending_reviews_from_board_summary`` can filter on them."""
        # Parent work item (manager's own).
        parent = DelegationWorkItem(
            work_item_id="wi-cto",
            run_id="run-1",
            cell_id="team::ceo",
            team_id="team::ceo",
            role_id="cto",
            seat_id="seat::cto",
            parent_work_item_id="",
            title="CTO slice",
            kind="plan",
            projection_id="cto__plan",
            phase=Phase.RUNNING,
            manager_role_id="ceo",
            manager_seat_id="seat::ceo",
            metadata={"runtime_model": "multi_team_org"},
        )
        await self.store.save_delegation_work_item(parent)
        # Child that the CTO needs to review.
        child = DelegationWorkItem(
            work_item_id="wi-engineer",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::eng",
            parent_work_item_id="wi-cto",
            title="Engineer slice",
            kind="execute",
            projection_id="engineer__execute",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            manager_role_id="cto",
            manager_seat_id="seat::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "review_state": "pending_manager",
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::cto",
                "activation_state": "awaiting_review",
                "completion_report": "Done.",
            },
        )
        await self.store.save_delegation_work_item(child)

        summary = await self.store.summarize_parent_status(
            "run-1",
            manager_seat_id="seat::cto",
            parent_work_item_id="wi-cto",
        )
        rows = [row for row in summary["upstream_summary"] if row["work_item_id"] == "wi-engineer"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_owner_role_id"], "cto")
        self.assertEqual(rows[0]["review_owner_seat_id"], "seat::cto")
        self.assertEqual(rows[0]["completion_report"], "Done.")

    async def test_end_to_end_review_pending_turn_mode_flows_from_store(self) -> None:
        """Full round-trip: seed store → refresh board state → turn mode
        resolves to review_pending because a pending review exists."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        parent = DelegationWorkItem(
            work_item_id="wi-cto",
            run_id="run-1",
            cell_id="team::ceo",
            team_id="team::ceo",
            role_id="cto",
            seat_id="seat::cto",
            parent_work_item_id="",
            title="CTO slice",
            kind="plan",
            projection_id="cto__plan",
            phase=Phase.RUNNING,
            manager_role_id="ceo",
            manager_seat_id="seat::ceo",
            metadata={"runtime_model": "multi_team_org"},
        )
        await self.store.save_delegation_work_item(parent)
        await self._seed_run_and_work_item()

        # Worker completes: transition their work item to in_review first.
        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )
        worker_task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.DONE,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "manager_role_id": "cto",
                "manager_seat_id": "seat::cto",
            },
        )
        set_linked_work_item_id(worker_task, "wi-engineer")
        await _apply_task_transition(
            executor,
            worker_task,
            status=TaskStatus.DONE.value,
            result=TaskResult(status=TaskStatus.DONE, content="Done."),
        )

        # Now simulate the CTO manager session refreshing its board state.
        runtime = CompanyRuntime(
            org_engine=StubOrgEngine(),
            communication=RecordingCommunication(),
            store=self.store,
        )
        manager = _make_manager_session(
            role_id="cto",
            seat_id="seat::cto",
            direct_report_seat_ids=["seat::eng"],
            direct_report_role_ids=["engineer"],
        )
        manager.focused_work_item_id = "wi-cto"
        manager.metadata["delegation_run_id"] = "run-1"
        runtime.member_sessions[manager.member_session_id] = manager

        await runtime._refresh_manager_board_state(manager)

        # Pending reviews discovered from store.
        pending = list(manager.metadata.get("pending_review_items", []) or [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["work_item_id"], "wi-engineer")
        self.assertEqual(
            runtime._resolve_current_turn_mode(manager),
            "review_pending",
        )


# ---------------------------------------------------------------------------
# Regression: on_kanban_changed logger must surface the real exception.
#
# The original warning was a single line ("on_kanban_changed collab_sync
# broadcast failed") because loguru silently drops ``exc_info=True`` when the
# sink was not configured with ``backtrace=True`` (see ``opc_logger.py``).
# That made the real failure on the new09 run undiagnosable from the log
# file. We now use ``logger.opt(exception=True)`` so the full traceback is
# emitted regardless of sink configuration. Guarding against regression keeps
# the diagnostic channel open the next time the transient race fires.
# ---------------------------------------------------------------------------


class OnKanbanChangedLoggerTests(unittest.TestCase):
    def test_warning_uses_opt_exception_for_real_traceback(self) -> None:
        """The on_kanban_changed handler must call logger.opt(exception=True)
        so the stack trace is emitted even though our file sink lacks
        ``backtrace=True``. Using ``logger.warning(..., exc_info=True)`` alone
        would be silently swallowed."""
        source = Path("opc/plugins/office_ui/ws_handler.py").read_text()
        # Locate the handler body.
        handler_marker = "async def on_kanban_changed(self"
        handler_idx = source.find(handler_marker)
        self.assertGreaterEqual(handler_idx, 0, "on_kanban_changed handler not found")
        # The handler is short; take the next ~40 lines.
        body = source[handler_idx:handler_idx + 2000]
        self.assertIn(
            "logger.opt(exception=True)",
            body,
            "on_kanban_changed must log via logger.opt(exception=True) so the "
            "traceback survives loguru sinks without backtrace=True",
        )
        # And the exception type / message should be inline so the first log
        # line alone is already actionable.
        self.assertIn("type(exc).__name__", body)


# ---------------------------------------------------------------------------
# After the kanban-write-tool removal refactor the contract only exposes
# `delegate_work` + `manager_board_read`; runtime owns all state transitions.
# ---------------------------------------------------------------------------


class MultiTeamOrgGuidelineDifferentiationTests(unittest.TestCase):
    def _contract(self) -> str:
        task = Task(
            id="mgr-turn",
            title="Manager turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "direct_report_seat_ids": ["seat::eng"],
            },
        )
        return build_company_work_item_contract(task)

    def test_only_intent_side_tools_are_described(self) -> None:
        contract = self._contract()
        self.assertIn("Manager Runtime Contract", contract)
        self.assertIn("Use `delegate_work` only to CREATE child WorkItems", contract)
        self.assertIn("Use `manager_board_read` only to READ child-board state", contract)
        self.assertIn("direct reports", contract)
        self.assertNotIn("Dispatch Planning Contract", contract)
        self.assertNotIn("Leader Delegation Planning Overlay", contract)
        # Write-side kanban tools were removed; runtime advances state.
        self.assertNotIn("manager_board_update", contract)
        self.assertNotIn("manager_board_release", contract)
        self.assertNotIn("manager_board_rollup", contract)
        self.assertNotIn("Team-management toolset", contract)

    def test_leaf_worker_omits_leader_delegation_overlay(self) -> None:
        task = Task(
            id="worker-turn",
            title="Worker turn",
            project_id="proj1",
            assigned_to="engineer",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "worker_execute",
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("Organization Runtime Contract", contract)
        self.assertIn("assigned WorkItem", contract)
        self.assertNotIn("Manager Runtime Contract", contract)
        self.assertNotIn("Leader Delegation Planning Overlay", contract)
        self.assertNotIn("Dispatch Planning Contract", contract)
        self.assertNotIn("`delegate_work`", contract)
        self.assertNotIn("`manager_board_read`", contract)

    def test_manager_capable_execute_turn_keeps_only_manager_contract(self) -> None:
        task = Task(
            id="manager-execute-turn",
            title="Manager execute turn",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "worker_execute",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::cto",
                            "role_id": "cto",
                            "direct_report_seat_ids": ["seat::team::cto::senior_engineer"],
                        }
                    ]
                },
                "delegation_seat_id": "seat::team::ceo::cto",
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("Organization Runtime Contract", contract)
        self.assertIn("Manager Runtime Contract", contract)
        self.assertNotIn("Leader Delegation Planning Overlay", contract)
        self.assertNotIn("Dispatch Planning Contract", contract)
        self.assertNotIn("Prefer direct execution", contract)

    def test_manager_decision_contract_preserves_direct_or_delegate_choice(self) -> None:
        task = Task(
            id="mgr-turn",
            title="Manager turn",
            project_id="proj1",
            assigned_to="cmo",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "manager_decide",
                "direct_report_seat_ids": ["seat::marketing-specialist"],
                "allowed_delegate_role_ids": ["marketing_specialist"],
            },
        )

        contract = build_company_work_item_contract(task)

        self.assertIn("Manager Execution Decision", contract)
        self.assertIn("Manager Runtime Contract", contract)
        self.assertNotIn("Leader Delegation Planning Overlay", contract)
        self.assertIn("Assess the assigned outcome before choosing an execution shape", contract)
        self.assertIn("Delegate only when distinct downstream outcomes benefit", contract)
        self.assertIn("Complete the WorkItem directly when", contract)
        self.assertNotIn("NO_DELEGATION_JUSTIFICATION", contract)
        self.assertNotIn("must delegate", contract)


# ---------------------------------------------------------------------------
# Regression: blocked manager sessions must be unblocked when an attention
# work item (including a review turn) is created/revived for them.
#
# Root cause found while debugging the new09 real run:
#   1. Worker completes → APPROVAL_REQUEST message lands in manager inbox  ✓
#   2. Worker's work_item transitions to ``in_review``                     ✓
#   3. _queue_multi_team_response_tasks sees the session's protocol_backlog
#      and calls _upsert_attention_work_item → review work item created    ✓
#   4. BUT the manager session was parked ``blocked`` in complete_claim,
#      and claim_runnable_tasks skips "blocked" sessions                   ✗
#   5. The review attention item is queued but NEVER claimed → manager
#      never comes back → review queue stays full forever.
#
# Fix: _upsert_attention_work_item now calls _unblock_attention_session
# after creating/reviving an attention work item, flipping the session
# back to ``idle`` so the next claim_runnable_tasks pass picks it up.
# ---------------------------------------------------------------------------


class UnblockAttentionSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix=f"unblock-attn-{uuid.uuid4().hex}-"))
        self.store = OPCStore(self._tmp / "tasks.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_blocked_manager(self) -> CompanyMemberSession:
        # A manager session that delegated and parked. This is exactly the
        # state complete_claim leaves it in after task.status = BLOCKED.
        session = CompanyMemberSession(
            member_session_id="member::cto",
            role_id="cto",
            employee_id="cto-emp",
            seat_id="seat::team::cto::cto",
            team_id="team::cto",
            status="blocked",
            resident_status="blocked",
            current_task_id="cto-parent-task",
            focused_work_item_id="wi-cto-parent",
            manager_role_id="ceo",
            manager_role_ids=["ceo"],
            metadata={
                "runtime_model": "multi_team_org",
                "seat_id": "seat::team::cto::cto",
                "team_id": "team::cto",
                "session_scope_id": "scope-1",
                "direct_report_seat_ids": ["seat::team::cto::senior_engineer"],
                "direct_report_role_ids": ["senior_engineer"],
                "managed_team_id": "team::cto",
                "manager_role_id": "ceo",
                "manager_seat_id": "seat::team::ceo::cto",
            },
        )
        return session

    async def test_upsert_attention_work_item_unblocks_the_session(self) -> None:
        """Primary regression test for the new09 review-flow stall."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        # Seed CTO's parent + a child in_review awaiting CTO's review.
        parent = DelegationWorkItem(
            work_item_id="wi-cto-parent",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            parent_work_item_id="",
            title="CTO oversight",
            kind="plan",
            projection_id="cto__plan",
            phase=Phase.WAITING_FOR_CHILDREN,
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "activation_state": "waiting_for_children",
                "dependency_work_item_ids": ["wi-senior-engineer"],
            },
        )
        child = DelegationWorkItem(
            work_item_id="wi-senior-engineer",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="senior_engineer",
            seat_id="seat::team::cto::senior_engineer",
            parent_work_item_id="wi-cto-parent",
            title="Senior engineer deliverable",
            kind="execute",
            projection_id="senior_engineer__execute",
            phase=Phase.AWAITING_MANAGER_REVIEW,  # child is awaiting CTO's approval
            manager_role_id="cto",
            manager_seat_id="seat::team::cto::cto",
            metadata={
                "runtime_model": "multi_team_org",
                "review_state": "pending_manager",
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
                "activation_state": "awaiting_review",
                "completion_report": "Built the app.",
            },
        )
        await self.store.save_delegation_work_item(parent)
        await self.store.save_delegation_work_item(child)

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )

        manager = self._make_blocked_manager()
        executor.runtime.member_sessions[manager.member_session_id] = manager
        assert manager.status == "blocked"  # precondition

        # The root task the loop picks for run_id resolution. Must carry
        # delegation_run_id so _upsert_attention_work_item's guard passes.
        root_task = Task(
            id="root",
            title="CEO Intake",
            project_id="proj1",
            assigned_to="ceo",
            status=TaskStatus.BLOCKED,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "work_item_runtime": True,
            },
        )

        source_message = {
            "msg_id": "msg-review-1",
            "from_agent": "senior_engineer",
            "subject": "Review needed: Built the app",
            "body": "Please review.",
            "semantic_type": "approval_request",
            "message_class": "protocol",
            "metadata": {"message_class": "protocol"},
        }

        work_items = [parent, child]
        tasks = [root_task]
        updated_tasks, updated_work_items = await executor._upsert_attention_work_item(
            root_task=root_task,
            tasks=tasks,
            work_items=work_items,
            session=manager,
            source_message=source_message,
        )

        # Core assertion: the manager session is no longer blocked and will
        # be picked up by claim_runnable_tasks on the next iteration.
        self.assertEqual(manager.status, "idle")
        self.assertEqual(manager.resident_status, "idle")

    async def test_upsert_attention_syncs_blocked_current_task_without_phase_change(self) -> None:
        """Regression for blocked task + already-runnable work item.

        The helper used to define ``target_phase`` only when it actively
        changed the work-item phase. If the work item was already READY but
        its projected task was still BLOCKED, syncing the task status crashed
        with UnboundLocalError.
        """
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        parent = DelegationWorkItem(
            work_item_id="wi-cto-parent",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            parent_work_item_id="",
            title="CTO oversight",
            kind="plan",
            projection_id="cto__plan",
            phase=Phase.READY,
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::cto",
            metadata={"runtime_model": "multi_team_org"},
        )
        await self.store.save_delegation_work_item(parent)

        current_task = Task(
            id="cto-parent-task",
            title="CTO oversight",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.BLOCKED,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "work_item_runtime": True,
            },
        )
        set_linked_work_item_id(current_task, "wi-cto-parent")

        save_task = AsyncMock()
        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=save_task,
            store=self.store,
        )

        manager = self._make_blocked_manager()
        manager.current_work_item = {"work_item_id": "wi-cto-parent"}
        source_message = {
            "msg_id": "msg-review-2",
            "from_agent": "senior_engineer",
            "subject": "Review needed",
            "body": "Please review.",
            "semantic_type": "approval_request",
            "message_class": "protocol",
            "metadata": {"message_class": "protocol"},
        }

        await executor._upsert_attention_work_item(
            root_task=current_task,
            tasks=[current_task],
            work_items=[parent],
            session=manager,
            source_message=source_message,
        )

        save_task.assert_awaited_once()
        saved_task = save_task.await_args.args[0]
        self.assertEqual(saved_task.status, TaskStatus.PENDING)
        self.assertEqual(manager.status, "idle")
        self.assertEqual(manager.resident_status, "idle")

    async def test_upsert_attention_does_not_touch_idle_session(self) -> None:
        """If a session is already idle/cold, we must not reset its other
        fields by mistake — the helper is a targeted unblock, not a blanket
        status reset."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )
        idle_session = self._make_blocked_manager()
        idle_session.status = "idle"
        idle_session.resident_status = "idle"
        idle_session.current_task_id = "some-task"  # must not be cleared
        prior_current_task_id = idle_session.current_task_id

        executor._unblock_attention_session(idle_session)

        # status stays idle, current_task_id untouched.
        self.assertEqual(idle_session.status, "idle")
        self.assertEqual(idle_session.current_task_id, prior_current_task_id)

    async def test_unblock_attention_session_handles_draining_status(self) -> None:
        """A draining manager session should also be rescued — otherwise
        the team can stall during graceful shutdown / replan windows."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
        )
        session = self._make_blocked_manager()
        session.status = "draining"
        session.resident_status = "draining"

        executor._unblock_attention_session(session)

        self.assertEqual(session.status, "idle")
        self.assertEqual(session.resident_status, "idle")


# ---------------------------------------------------------------------------
# Regression: kanban UI must refresh on every work-item transition, not
# only at the gather boundary.
#
# Symptom from a real run: "when already in_progress the kanban still
# shows Todo; when already ready for in_review it still shows In Progress."
#
# Root cause: _execute_multi_team_org only called on_kanban_changed AFTER
# asyncio.gather of all _run_claimed_work_item coroutines completed. With 7
# parallel codex workers that took minutes, the UI was stuck on the
# pre-claim snapshot until the SLOWEST worker finished and then jumped
# directly from "Todo" to "In Review", silently eating the "In Progress"
# state. Managers reviewing later saw the same lag on in_review → done.
#
# Fix: worker-start and worker-end transitions now fire
# on_kanban_changed so every transition hits the UI immediately. The
# _upsert_attention_work_item path gets the same treatment so fresh
# Review Turn / Dispatch Turn cards surface without delay.
# ---------------------------------------------------------------------------


class KanbanPerTransitionPushTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix=f"kanban-push-{uuid.uuid4().hex}-"))
        self.store = OPCStore(self._tmp / "tasks.db")
        await self.store.initialize()
        self.refresh_count = 0

        async def on_changed() -> None:
            self.refresh_count += 1

        self.on_changed = on_changed

    async def asyncTearDown(self) -> None:
        await self.store.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _executor(self):
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        return CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
            on_kanban_changed=self.on_changed,
        )

    async def _seed_work_item(self, status: str) -> DelegationWorkItem:
        phase_by_status = {
            "ready": Phase.READY,
            "in_progress": Phase.RUNNING,
            "in_review": Phase.AWAITING_MANAGER_REVIEW,
            "done": Phase.APPROVED,
        }
        item = DelegationWorkItem(
            work_item_id="wi-1",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="senior_engineer",
            seat_id="seat::eng",
            parent_work_item_id="wi-parent",
            title="Slice",
            kind="execute",
            projection_id="senior_engineer__execute",
            phase=phase_by_status[status],
            manager_role_id="cto",
            manager_seat_id="seat::cto",
            metadata={"runtime_model": "multi_team_org"},
        )
        await self.store.save_delegation_work_item(item)
        return item

    async def test_running_transition_pushes_to_ui(self) -> None:
        """Worker start (status=RUNNING) must fire on_kanban_changed so the
        card moves out of Todo immediately.

        The push is now debounced (see ``_schedule_kanban_notification``),
        so we wait out one debounce window before asserting.  A single
        transition still produces exactly one broadcast — the debounce
        only collapses multiple rapid-fire pushes.
        """
        executor = self._executor()
        await self._seed_work_item("ready")

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.RUNNING,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
            },
        )
        set_linked_work_item_id(task, "wi-1")
        before = self.refresh_count
        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.RUNNING.value,
        )
        await asyncio.sleep(executor._kanban_debounce_sec + 0.1)
        self.assertEqual(self.refresh_count, before + 1)

    async def test_done_transition_pushes_to_ui(self) -> None:
        """Worker finish (status=DONE) must fire on_kanban_changed so the
        card moves to In Review immediately, not after the whole parallel
        batch clears.  Debounced — see ``test_running_transition_pushes_to_ui``.
        """
        executor = self._executor()
        await self._seed_work_item("in_progress")

        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.DONE,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "manager_role_id": "cto",
                "manager_seat_id": "seat::cto",
            },
        )
        set_linked_work_item_id(task, "wi-1")
        before = self.refresh_count
        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.DONE.value,
            result=TaskResult(status=TaskStatus.DONE, content="done"),
        )
        await asyncio.sleep(executor._kanban_debounce_sec + 0.1)
        self.assertEqual(self.refresh_count, before + 1)

    async def test_transition_without_callback_is_safe(self) -> None:
        """Executors constructed without on_kanban_changed must not crash
        when the per-transition hook fires."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
            # on_kanban_changed deliberately omitted.
        )
        await self._seed_work_item("ready")
        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.RUNNING,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
            },
        )
        set_linked_work_item_id(task, "wi-1")
        # Should not raise.
        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.RUNNING.value,
        )

    async def test_callback_failure_does_not_break_transition(self) -> None:
        """If the UI push raises (e.g. transient WS race), the work-item
        state machine must continue unaffected."""
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        async def bad_callback() -> None:
            raise RuntimeError("UI gone")

        executor = CompanyWorkItemExecutor(
            org_engine=StubOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=self.store,
            on_kanban_changed=bad_callback,
        )
        await self._seed_work_item("ready")
        task = Task(
            id="engineer-task",
            title="Engineer work item",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.RUNNING,
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
            },
        )
        set_linked_work_item_id(task, "wi-1")
        # Must not propagate.
        await _apply_task_transition(
            executor,
            task,
            status=TaskStatus.RUNNING.value,
        )
        # And the DB write DID happen despite the UI error.
        refreshed = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(refreshed.phase, Phase.RUNNING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
