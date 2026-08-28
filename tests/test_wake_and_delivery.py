"""Dispatcher convergence + delivery card tests.

Phase B replaced the wake / reconcile / reenqueue hooks with a
per-tick ``_rehydrate_parked_member_sessions`` pass in the dispatcher
loop. This test module verifies that:

    - ``_refresh_delegation_dependents`` still clears the parent's
      claim on the WAITING_FOR_CHILDREN → RUNNING edge so
      ``is_orphaned`` returns True and the next tick picks it up.
    - ``_rehydrate_parked_member_sessions`` unparks a blocked
      CompanyMemberSession whose focused work_item is now dispatchable.
    - Final delivery cards route ``done`` → AWAITING_HUMAN, while
      intermediate delivery/attention cards auto-approve.
    - Role-instance session continuity across turns (Phase A).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from opc.core.models import (
    CompanyMemberSession,
    DelegationRoleSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401  registers hooks
from opc.layer2_organization.work_item_links import set_linked_work_item_id


class RefreshDependentsClearClaimTests(unittest.IsolatedAsyncioTestCase):
    """The wake edge (WAITING_FOR_CHILDREN → RUNNING with all children
    APPROVED) must clear the parent's claim so the dispatcher's
    is_orphaned check recognises it as dispatchable on the next tick."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_wake_edge_clears_parent_claim(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
        from opc.layer2_organization.phase import is_dispatchable

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.runtime = MagicMock()
        executor.on_kanban_changed = None
        executor._kanban_dirty = False
        executor._kanban_broadcast_task = None
        executor._kanban_debounce_sec = 0.2

        # One APPROVED child.
        child = DelegationWorkItem(
            work_item_id="child-1",
            run_id="r", cell_id="team::cmo", role_id="designer",
            seat_id="seat::team::cmo::designer",
            manager_role_id="cmo", manager_seat_id="seat::team::ceo::cmo",
            title="child",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-runtime::r::designer",
            metadata={"dependency_work_item_ids": []},
        )
        await self.store.save_delegation_work_item(child)
        await self.store.update_delegation_work_item("child-1", phase=Phase.AWAITING_MANAGER_REVIEW)
        await self.store.update_delegation_work_item("child-1", phase=Phase.APPROVED)

        # Parent is WAITING_FOR_CHILDREN with claim still held.
        parent = DelegationWorkItem(
            work_item_id="parent-1",
            run_id="r", cell_id="team::ceo", role_id="cmo",
            seat_id="seat::team::ceo::cmo",
            manager_role_id="ceo", manager_seat_id="seat::team::ceo::ceo",
            title="parent",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-runtime::r::cmo",
            claimed_by_role_runtime_session_id="role-runtime::r::cmo",
            claimed_by_seat_id="seat::team::ceo::cmo",
            metadata={"dependency_work_item_ids": ["child-1"]},
        )
        await self.store.save_delegation_work_item(parent)
        await self.store.update_delegation_work_item(
            "parent-1", phase=Phase.WAITING_FOR_CHILDREN
        )

        # Task just needs to carry the run_id.
        seed_task = Task(
            id="seed", title="seed", description="",
            assigned_to="ceo", status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
            metadata={"delegation_run_id": "r"},
        )
        await self.store.save_task(seed_task)

        await executor._refresh_delegation_dependents(seed_task)

        after = await self.store.get_delegation_work_item("parent-1")
        self.assertEqual(after.phase, Phase.RUNNING)
        # Claim must be cleared so dispatcher picks it up as orphaned.
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.claimed_by_seat_id, "")
        self.assertTrue(is_dispatchable(after))

    async def test_leader_parent_enters_synthesis_turn_after_all_children_approved(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
        from opc.layer2_organization.phase import is_dispatchable

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.runtime = MagicMock()
        executor.on_kanban_changed = None
        executor._kanban_dirty = False
        executor._kanban_broadcast_task = None
        executor._kanban_debounce_sec = 0.2

        child_a = DelegationWorkItem(
            work_item_id="child-a",
            run_id="r",
            cell_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            manager_role_id="cto",
            manager_seat_id="seat::team::ceo::cto",
            title="implementation",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-runtime::r::engineer",
            metadata={"dependency_work_item_ids": []},
        )
        child_b = DelegationWorkItem(
            work_item_id="child-b",
            run_id="r",
            cell_id="team::cto",
            role_id="qa",
            seat_id="seat::team::cto::qa",
            manager_role_id="cto",
            manager_seat_id="seat::team::ceo::cto",
            title="verification",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-runtime::r::qa",
            metadata={"dependency_work_item_ids": []},
        )
        await self.store.save_delegation_work_item(child_a)
        await self.store.save_delegation_work_item(child_b)
        for child_id in ("child-a", "child-b"):
            await self.store.update_delegation_work_item(child_id, phase=Phase.AWAITING_MANAGER_REVIEW)
            await self.store.update_delegation_work_item(child_id, phase=Phase.APPROVED)

        parent = DelegationWorkItem(
            work_item_id="cto-parent",
            run_id="r",
            cell_id="team::ceo",
            role_id="cto",
            seat_id="seat::team::ceo::cto",
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::ceo",
            title="CTO delegated execution",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-runtime::r::cto",
            claimed_by_role_runtime_session_id="role-runtime::r::cto",
            claimed_by_seat_id="seat::team::ceo::cto",
            metadata={
                "work_kind": "execute",
                "dependency_work_item_ids": ["child-a", "child-b"],
                "delegated_children_pending": True,
                "frontier": "waiting_for_children",
                "last_delegated_by_seat_id": "seat::team::ceo::cto",
            },
        )
        await self.store.save_delegation_work_item(parent)
        await self.store.update_delegation_work_item("cto-parent", phase=Phase.WAITING_FOR_CHILDREN)

        seed_task = Task(
            id="seed",
            title="seed",
            assigned_to="ceo",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={"delegation_run_id": "r"},
        )
        await self.store.save_task(seed_task)

        await executor._refresh_delegation_dependents(seed_task)

        after = await self.store.get_delegation_work_item("cto-parent")
        self.assertEqual(after.phase, Phase.READY)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.claimed_by_seat_id, "")
        self.assertTrue(is_dispatchable(after))
        self.assertEqual(after.metadata["work_kind"], "synthesize")
        self.assertEqual(after.metadata["work_item_turn_type"], "aggregate")
        self.assertEqual(after.metadata["current_turn_mode"], "synthesize_required")
        self.assertEqual(after.metadata["frontier"], "synthesis_ready")
        self.assertEqual(after.metadata["synthesis_source_work_item_ids"], ["child-a", "child-b"])
        self.assertFalse(after.metadata["delegated_children_pending"])
        self.assertEqual(after.metadata["waiting_on_work_item_ids"], [])
        self.assertIn("Synthesize the 2 approved child work items", after.summary)


class RehydrateParkedMemberSessionsTests(unittest.IsolatedAsyncioTestCase):
    """The dispatcher's per-tick unpark pass converts a blocked CMS
    whose focused work_item is now dispatchable back to 'idle' so
    claim_runnable_tasks will pick it up on the same tick."""

    def _executor(self):
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)

        class _Runtime:
            member_sessions: dict[str, CompanyMemberSession] = {}
            role_sessions: dict[str, DelegationRoleSession] = {}

        executor.runtime = _Runtime()
        executor.runtime.member_sessions = {}
        executor.runtime.role_sessions = {}
        return executor

    def test_blocked_cms_with_dispatchable_focus_becomes_idle(self) -> None:
        executor = self._executor()
        cms = CompanyMemberSession(
            member_session_id="m1",
            role_id="cmo",
            employee_id="cmo-default",
        )
        cms.status = "blocked"
        cms.focused_work_item_id = "wi-1"
        cms.role_session_id = "role-runtime::r::cmo"
        executor.runtime.member_sessions["m1"] = cms

        # Focused work_item is orphan-RUNNING → dispatchable.
        wi = DelegationWorkItem(
            work_item_id="wi-1",
            run_id="r", cell_id="c", role_id="cmo", seat_id="seat-cmo",
            manager_role_id="ceo", manager_seat_id="seat-ceo",
            title="parent",
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id="",
        )

        executor._rehydrate_parked_member_sessions([wi])
        self.assertEqual(cms.status, "idle")
        self.assertEqual(cms.resident_status, "idle")

    def test_blocked_cms_with_non_dispatchable_focus_stays_blocked(self) -> None:
        executor = self._executor()
        cms = CompanyMemberSession(
            member_session_id="m1",
            role_id="cmo",
            employee_id="cmo-default",
        )
        cms.status = "blocked"
        cms.focused_work_item_id = "wi-1"
        executor.runtime.member_sessions["m1"] = cms

        # Still waiting on children — claim held, phase WAITING_FOR_CHILDREN.
        # Phase is in runnable_in_progress set, but claim non-empty
        # → not orphan → not dispatchable.
        wi = DelegationWorkItem(
            work_item_id="wi-1",
            run_id="r", cell_id="c", role_id="cmo", seat_id="seat-cmo",
            manager_role_id="ceo", manager_seat_id="seat-ceo",
            title="parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            claimed_by_role_runtime_session_id="role-runtime::r::cmo",
        )

        executor._rehydrate_parked_member_sessions([wi])
        self.assertEqual(cms.status, "blocked")

    def test_rework_ready_cms_flips_idle_via_runnable_phase(self) -> None:
        executor = self._executor()
        cms = CompanyMemberSession(
            member_session_id="designer",
            role_id="designer",
            employee_id="designer-default",
        )
        cms.status = "blocked"  # artificially blocked (simulating stale state)
        cms.focused_work_item_id = "wi-1"
        executor.runtime.member_sessions["designer"] = cms

        # READY_FOR_REWORK is in RUNNABLE_PHASES → is_dispatchable True
        # regardless of claim.
        wi = DelegationWorkItem(
            work_item_id="wi-1",
            run_id="r", cell_id="c", role_id="designer", seat_id="seat-designer",
            manager_role_id="cmo", manager_seat_id="seat-cmo",
            title="rework",
            phase=Phase.READY_FOR_REWORK,
            claimed_by_role_runtime_session_id="role-runtime::r::designer",
        )

        executor._rehydrate_parked_member_sessions([wi])
        self.assertEqual(cms.status, "idle")

    def test_cms_with_missing_focused_work_item_becomes_idle(self) -> None:
        executor = self._executor()
        cms = CompanyMemberSession(
            member_session_id="m1",
            role_id="cmo",
            employee_id="cmo-default",
        )
        cms.status = "blocked"
        cms.focused_work_item_id = "does-not-exist"
        executor.runtime.member_sessions["m1"] = cms

        executor._rehydrate_parked_member_sessions([])
        self.assertEqual(cms.status, "idle")
        self.assertEqual(cms.focused_work_item_id, "")


class DeliveryCardPhaseSyncTests(unittest.IsolatedAsyncioTestCase):
    """Final delivery cards route "done" → AWAITING_HUMAN."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def _executor(self):
        from contextvars import ContextVar
        from opc.layer2_organization.company_mode import (
            CompanyExecutorRunState,
            CompanyWorkItemExecutor,
        )

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor._default_run_state = CompanyExecutorRunState()
        executor._run_state_var = ContextVar(
            f"test-company-executor-run-state:{id(executor)}",
            default=None,
        )
        executor.store = self.store
        executor.memory = None
        executor.on_kanban_changed = None
        executor._kanban_dirty = False
        executor._kanban_broadcast_task = None
        executor._kanban_debounce_sec = 0.2
        executor.runtime = MagicMock()
        executor._runtime_invariant_issue_keys = set()
        return executor

    async def test_delivery_task_completes_to_awaiting_human(self) -> None:
        executor = self._executor()
        delivery_task = Task(
            id="delivery-task", title="Deliver", description="",
            assigned_to="ceo", status=TaskStatus.DONE,
            project_id="p", session_id="s",
            metadata={
                "work_item_projection_id": "ceo::delivery::abc",
                "work_kind": "delivery",
                "review_owner_kind": "human",
                "delegation_run_id": "r",
                "execution_mode": "company_mode",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        set_linked_work_item_id(delivery_task, "delivery-wi")
        await self.store.save_task(delivery_task)
        delivery_wi = DelegationWorkItem(
            work_item_id="delivery-wi",
            run_id="r", cell_id="c", role_id="ceo", seat_id="seat-ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Deliver",
            kind="delivery",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-ceo",
            claimed_by_role_runtime_session_id="role-ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_kind": "delivery",
                "review_owner_kind": "human",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_delegation_work_item(delivery_wi)
        await self.store.link_work_item_runtime_task(delivery_wi.work_item_id, delivery_task.id)

        await executor._apply_done_transition(delivery_task)

        after = await self.store.get_delegation_work_item("delivery-wi")
        self.assertEqual(after.phase, Phase.AWAITING_HUMAN)

    async def test_deliver_alias_completes_to_awaiting_human(self) -> None:
        executor = self._executor()
        delivery_task = Task(
            id="deliver-task", title="Deliver", description="",
            assigned_to="ceo", status=TaskStatus.DONE,
            project_id="p", session_id="s",
            metadata={
                "work_item_projection_id": "ceo::deliver::abc",
                "work_item_turn_type": "deliver",
                "work_kind": "deliver",
                "review_owner_kind": "human",
                "delegation_run_id": "r",
                "execution_mode": "company_mode",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        set_linked_work_item_id(delivery_task, "deliver-wi")
        await self.store.save_task(delivery_task)
        delivery_wi = DelegationWorkItem(
            work_item_id="deliver-wi",
            run_id="r", cell_id="c", role_id="ceo", seat_id="seat-ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Deliver",
            kind="deliver",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-ceo",
            claimed_by_role_runtime_session_id="role-ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_kind": "deliver",
                "work_item_turn_type": "deliver",
                "review_owner_kind": "human",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
            },
        )
        await self.store.save_delegation_work_item(delivery_wi)
        await self.store.link_work_item_runtime_task(delivery_wi.work_item_id, delivery_task.id)

        await executor._apply_done_transition(delivery_task)

        after = await self.store.get_delegation_work_item("deliver-wi")
        self.assertEqual(after.phase, Phase.AWAITING_HUMAN)

    async def test_final_delivery_without_required_feedback_auto_approves(self) -> None:
        executor = self._executor()
        delivery_task = Task(
            id="delivery-no-feedback-task", title="Deliver", description="",
            assigned_to="ceo", status=TaskStatus.DONE,
            project_id="p", session_id="s",
            metadata={
                "work_item_projection_id": "ceo::delivery::no_feedback",
                "work_item_turn_type": "deliver",
                "work_kind": "delivery",
                "review_owner_kind": "human",
                "delegation_run_id": "r",
                "execution_mode": "company_mode",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": False,
                "feedback_scope": "final",
            },
        )
        set_linked_work_item_id(delivery_task, "delivery-no-feedback-wi")
        await self.store.save_task(delivery_task)
        delivery_wi = DelegationWorkItem(
            work_item_id="delivery-no-feedback-wi",
            run_id="r", cell_id="c", role_id="ceo", seat_id="seat-ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Deliver",
            kind="delivery",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-ceo",
            claimed_by_role_runtime_session_id="role-ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_item_turn_type": "deliver",
                "work_kind": "delivery",
                "review_owner_kind": "human",
                "user_visible": True,
                "authoritative_output": True,
                "requires_user_feedback": False,
                "feedback_scope": "final",
            },
        )
        await self.store.save_delegation_work_item(delivery_wi)
        await self.store.link_work_item_runtime_task(delivery_wi.work_item_id, delivery_task.id)

        await executor._apply_done_transition(delivery_task)

        after = await self.store.get_delegation_work_item("delivery-no-feedback-wi")
        self.assertEqual(after.phase, Phase.APPROVED)

    async def test_attention_delivery_auto_approves_without_human_review(self) -> None:
        executor = self._executor()
        delivery_task = Task(
            id="attention-delivery-task", title="Attention delivery", description="",
            assigned_to="ceo", status=TaskStatus.DONE,
            project_id="p", session_id="s",
            metadata={
                "work_item_projection_id": "ceo::attention::abc",
                "work_item_turn_type": "deliver",
                "work_kind": "deliver",
                "review_owner_kind": "human",
                "delegation_run_id": "r",
                "execution_mode": "company_mode",
                "attention_work_item": True,
                "user_visible": False,
                "authoritative_output": False,
            },
        )
        set_linked_work_item_id(delivery_task, "attention-delivery-wi")
        await self.store.save_task(delivery_task)
        delivery_wi = DelegationWorkItem(
            work_item_id="attention-delivery-wi",
            run_id="r", cell_id="c", role_id="ceo", seat_id="seat-ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Attention delivery",
            kind="deliver",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-ceo",
            claimed_by_role_runtime_session_id="role-ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_kind": "deliver",
                "work_item_turn_type": "deliver",
                "review_owner_kind": "human",
                "attention_work_item": True,
                "user_visible": False,
                "authoritative_output": False,
            },
        )
        await self.store.save_delegation_work_item(delivery_wi)
        await self.store.link_work_item_runtime_task(delivery_wi.work_item_id, delivery_task.id)

        await executor._apply_done_transition(delivery_task)

        after = await self.store.get_delegation_work_item("attention-delivery-wi")
        self.assertEqual(after.phase, Phase.APPROVED)

    async def test_aggregate_task_auto_approves_without_review(self) -> None:
        executor = self._executor()
        aggregate_task = Task(
            id="aggregate-task",
            title="Synthesize child results",
            description="",
            assigned_to="cto",
            status=TaskStatus.DONE,
            project_id="p",
            session_id="s",
            metadata={
                "work_item_projection_id": "cto::aggregate::abc",
                "work_item_turn_type": "aggregate",
                "work_kind": "synthesize",
                "delegation_run_id": "r",
                "manager_role_id": "ceo",
                "manager_seat_id": "seat-ceo",
            },
        )
        set_linked_work_item_id(aggregate_task, "aggregate-wi")
        await self.store.save_task(aggregate_task)
        aggregate_wi = DelegationWorkItem(
            work_item_id="aggregate-wi",
            run_id="r",
            cell_id="c",
            role_id="cto",
            seat_id="seat-cto",
            manager_role_id="ceo",
            manager_seat_id="seat-ceo",
            title="Synthesize child results",
            kind="execute",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-cto",
            claimed_by_role_runtime_session_id="role-cto",
            metadata={"work_kind": "synthesize", "work_item_turn_type": "aggregate"},
        )
        await self.store.save_delegation_work_item(aggregate_wi)
        await self.store.link_work_item_runtime_task(aggregate_wi.work_item_id, aggregate_task.id)

        await executor._apply_done_transition(
            aggregate_task,
            result=TaskResult(status=TaskStatus.DONE, content="Integrated handoff."),
        )

        after = await self.store.get_delegation_work_item("aggregate-wi")
        self.assertEqual(after.phase, Phase.APPROVED)
        items = await self.store.list_delegation_work_items("r")
        self.assertFalse(any(item.kind == "report" for item in items))
        self.assertFalse(any(item.kind == "review" for item in items))

    async def test_stale_report_for_delivery_parent_closes_without_review_card(self) -> None:
        executor = self._executor()
        parent = DelegationWorkItem(
            work_item_id="deliver-parent",
            run_id="r",
            cell_id="c",
            role_id="ceo",
            seat_id="seat-ceo",
            title="Deliver",
            kind="deliver",
            phase=Phase.AWAITING_HUMAN,
            metadata={"work_kind": "deliver", "work_item_turn_type": "deliver"},
        )
        report = DelegationWorkItem(
            work_item_id="report::deliver-parent::v1",
            run_id="r",
            cell_id="c",
            role_id="ceo",
            seat_id="seat-ceo",
            title="Report",
            kind="report",
            phase=Phase.RUNNING,
            parent_work_item_id=parent.work_item_id,
            metadata={
                "work_kind": "report",
                "work_item_turn_type": "report",
                "report_execution_work_item": True,
                "report_target_work_item_id": parent.work_item_id,
            },
        )
        report_task = Task(
            id="report-task",
            title="Report",
            assigned_to="ceo",
            status=TaskStatus.DONE,
            project_id="p",
            session_id="s",
            metadata={
                "work_item_projection_id": report.work_item_id,
                "work_item_turn_type": "report",
                "work_kind": "report",
                "report_execution_work_item": True,
                "report_target_work_item_id": parent.work_item_id,
                "delegation_run_id": "r",
            },
        )
        set_linked_work_item_id(report_task, report.work_item_id)
        await self.store.save_delegation_work_item(parent)
        await self.store.save_delegation_work_item(report)
        await self.store.save_task(report_task)
        await self.store.link_work_item_runtime_task(report.work_item_id, report_task.id)

        await executor._apply_report_done_transition(
            report_task,
            result=TaskResult(status=TaskStatus.DONE, content="Delivery report."),
        )

        closed = await self.store.get_delegation_work_item(report.work_item_id)
        self.assertEqual(closed.phase, Phase.APPROVED)
        self.assertEqual(
            closed.metadata["report_card_outcome"],
            "parent_not_awaiting_review",
        )
        items = await self.store.list_delegation_work_items("r")
        self.assertFalse(any(str(item.work_item_id).startswith("review::deliver-parent") for item in items))

    async def test_dispatch_task_auto_approves_without_review(self) -> None:
        """Phase D: dispatch-kind cards don't need reviewer — DONE → APPROVED.
        Fixes the a7846729 stuck-at-awaiting_manager_review case."""
        executor = self._executor()
        dispatch_task = Task(
            id="dispatch-task", title="Dispatch Turn: ceo", description="",
            assigned_to="ceo", status=TaskStatus.DONE,
            project_id="p", session_id="s",
            metadata={
                "work_item_projection_id": "ceo::dispatch::xyz",
                "work_kind": "dispatch",
                "delegation_run_id": "r",
                "manager_role_id": "owner",
                "manager_seat_id": "owner",
            },
        )
        set_linked_work_item_id(dispatch_task, "dispatch-wi")
        await self.store.save_task(dispatch_task)
        dispatch_wi = DelegationWorkItem(
            work_item_id="dispatch-wi",
            run_id="r", cell_id="c", role_id="ceo", seat_id="seat-ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Dispatch Turn: ceo",
            kind="dispatch",
            phase=Phase.RUNNING,
            role_runtime_session_id="role-ceo",
            claimed_by_role_runtime_session_id="role-ceo",
        )
        await self.store.save_delegation_work_item(dispatch_wi)
        await self.store.link_work_item_runtime_task(dispatch_wi.work_item_id, dispatch_task.id)

        await executor._apply_done_transition(dispatch_task)

        after = await self.store.get_delegation_work_item("dispatch-wi")
        # Went straight to APPROVED, no review card spawn.
        self.assertEqual(after.phase, Phase.APPROVED)
        # Verify no review work item was created for this dispatch.
        all_items = await self.store.list_delegation_work_items("r")
        review_items = [w for w in all_items if w.kind == "review"]
        self.assertEqual(review_items, [])


class SpawnDeliveryCardTests(unittest.IsolatedAsyncioTestCase):
    """Phase 2: _spawn_delivery_card_after_intake produces a
    WAITING_DEPENDENCIES delivery card tied to the given children."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_spawn_creates_delivery_card(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.memory = None
        executor.save_task = self.store.save_task

        intake_wi = DelegationWorkItem(
            work_item_id="intake-1",
            run_id="r", cell_id="team::ceo", team_id="team::ceo",
            role_id="ceo", seat_id="seat::team::ceo::ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Intake top request",
            kind="intake",
            phase=Phase.RUNNING,
            batch_id="batch-1",
            role_runtime_session_id="role-ceo",
            metadata={
                "original_message": "hello world",
                "delegation_playbook": {"some": "plan"},
                "contact_role_ids": ["cmo", "coo"],
                "allowed_delegate_role_ids": ["cmo", "coo", "cto"],
                "comms_workspace_root": "/tmp/ws",
                "target_output_dir": "/tmp/ws/proj",
                "runtime_model": "multi_team_org",
                "employee_assignment": {
                    "employee_id": "chief-of-staff",
                    "role_id": "ceo",
                },
            },
        )
        await self.store.save_delegation_work_item(intake_wi)

        task = Task(
            id="intake-task", title="Intake", description="",
            assigned_to="ceo", status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
            metadata={
                "delegation_run_id": "r",
            },
        )
        set_linked_work_item_id(task, "intake-1")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(intake_wi.work_item_id, task.id)

        await executor._spawn_delivery_card_after_intake(
            task=task,
            intake_work_item=intake_wi,
            dependency_ids=["child-a", "child-b", "child-c"],
        )

        items = await self.store.list_delegation_work_items("r")
        delivery = [wi for wi in items if wi.kind == "delivery"]
        self.assertEqual(len(delivery), 1)
        d = delivery[0]
        self.assertEqual(d.role_id, "ceo")
        self.assertEqual(d.seat_id, "seat::team::ceo::ceo")
        self.assertEqual(d.phase, Phase.WAITING_DEPENDENCIES)
        self.assertEqual(d.parent_work_item_id, "intake-1")
        self.assertEqual(
            d.metadata.get("dependency_work_item_ids"),
            ["child-a", "child-b", "child-c"],
        )
        self.assertEqual(d.metadata.get("review_owner_kind"), "human")
        self.assertEqual(d.metadata.get("work_kind"), "delivery")
        self.assertEqual(d.metadata.get("current_turn_mode"), "deliver_required")
        self.assertEqual(
            d.metadata.get("employee_assignment"),
            {"employee_id": "chief-of-staff", "role_id": "ceo"},
        )
        self.assertEqual(d.metadata.get("intake_work_item_id"), "intake-1")
        self.assertEqual(d.metadata.get("allowed_delegate_role_ids"), [])
        self.assertTrue(d.metadata.get("user_visible"))
        self.assertTrue(d.metadata.get("authoritative_output"))
        self.assertTrue(d.metadata.get("requires_user_feedback"))
        self.assertIsInstance(d.metadata.get("prompt_contract"), dict)
        self.assertTrue(d.metadata["prompt_contract"])

    async def test_spawn_delivery_card_respects_required_feedback_policy(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.memory = None
        executor.save_task = self.store.save_task

        intake_wi = DelegationWorkItem(
            work_item_id="intake-policy",
            run_id="r", cell_id="team::ceo", team_id="team::ceo",
            role_id="ceo", seat_id="seat::team::ceo::ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Intake top request",
            kind="intake",
            phase=Phase.RUNNING,
            batch_id="batch-1",
            role_runtime_session_id="role-ceo",
            metadata={
                "delegation_playbook": {
                    "company_work_item_plan": {
                        "profile": "custom",
                        "root_projection_id": "root-intake",
                        "projections": [
                            {
                                "projection_id": "root-intake",
                                "turn_type": "intake",
                                "role_id": "ceo",
                                "title": "Root Intake",
                                "delivery_policy": {
                                    "user_visible": True,
                                    "authoritative_output": True,
                                    "requires_user_feedback": True,
                                },
                            }
                        ],
                    }
                },
            },
        )
        await self.store.save_delegation_work_item(intake_wi)
        task = Task(
            id="intake-policy-task", title="Intake", description="",
            assigned_to="ceo", status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
            metadata={"delegation_run_id": "r"},
        )
        set_linked_work_item_id(task, "intake-policy")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(intake_wi.work_item_id, task.id)

        await executor._spawn_delivery_card_after_intake(
            task=task,
            intake_work_item=intake_wi,
            dependency_ids=[],
        )

        items = await self.store.list_delegation_work_items("r")
        delivery = [wi for wi in items if wi.kind == "delivery"]
        self.assertEqual(len(delivery), 1)
        self.assertEqual(delivery[0].phase, Phase.WAITING_DEPENDENCIES)
        self.assertEqual(
            delivery[0].metadata.get("dependency_work_item_ids"),
            ["intake-policy"],
        )
        self.assertTrue(delivery[0].metadata.get("requires_user_feedback"))

    async def test_spawn_delivery_card_ignores_root_policy_feedback_false(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.memory = None
        executor.save_task = self.store.save_task

        intake_wi = DelegationWorkItem(
            work_item_id="intake-policy-false",
            run_id="r", cell_id="team::ceo", team_id="team::ceo",
            role_id="ceo", seat_id="seat::team::ceo::ceo",
            manager_role_id="owner", manager_seat_id="owner",
            title="Intake top request",
            kind="intake",
            phase=Phase.RUNNING,
            batch_id="batch-1",
            role_runtime_session_id="role-ceo",
            metadata={
                "delegation_playbook": {
                    "company_work_item_plan": {
                        "profile": "custom",
                        "root_projection_id": "root-intake",
                        "projections": [
                            {
                                "projection_id": "root-intake",
                                "turn_type": "intake",
                                "role_id": "ceo",
                                "title": "Root Intake",
                                "delivery_policy": {
                                    "user_visible": True,
                                    "authoritative_output": True,
                                    "requires_user_feedback": False,
                                },
                            }
                        ],
                    }
                },
            },
        )
        await self.store.save_delegation_work_item(intake_wi)
        task = Task(
            id="intake-policy-false-task", title="Intake", description="",
            assigned_to="ceo", status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
            metadata={"delegation_run_id": "r"},
        )
        set_linked_work_item_id(task, "intake-policy-false")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(intake_wi.work_item_id, task.id)

        await executor._spawn_delivery_card_after_intake(
            task=task,
            intake_work_item=intake_wi,
            dependency_ids=[],
        )

        items = await self.store.list_delegation_work_items("r")
        delivery = [wi for wi in items if wi.kind == "delivery"]
        self.assertEqual(len(delivery), 1)
        self.assertTrue(delivery[0].metadata.get("requires_user_feedback"))

    async def test_intake_parking_can_close_from_waiting_for_children(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.memory = None
        executor.save_task = self.store.save_task

        intake_wi = DelegationWorkItem(
            work_item_id="intake-1",
            run_id="r",
            cell_id="team::ceo",
            team_id="team::ceo",
            role_id="ceo",
            seat_id="seat::team::ceo::ceo",
            manager_role_id="owner",
            manager_seat_id="owner",
            title="Intake top request",
            kind="intake",
            phase=Phase.WAITING_FOR_CHILDREN,
            batch_id="batch-1",
            role_runtime_session_id="role-ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "dependency_work_item_ids": ["child-1"],
                "waiting_on_work_item_ids": ["child-1"],
                "delegated_children_pending": True,
                "frontier": "waiting_for_children",
            },
        )
        child_wi = DelegationWorkItem(
            work_item_id="child-1",
            run_id="r",
            cell_id="team::ceo",
            team_id="team::ceo",
            role_id="cto",
            seat_id="seat::team::ceo::cto",
            parent_work_item_id="intake-1",
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::ceo",
            title="Child implementation",
            kind="execute",
            phase=Phase.READY,
            metadata={"runtime_model": "multi_team_org"},
        )
        await self.store.save_delegation_work_item(intake_wi)
        await self.store.save_delegation_work_item(child_wi)

        task = Task(
            id="intake-task",
            title="Intake",
            description="",
            assigned_to="ceo",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={"delegation_run_id": "r"},
        )
        set_linked_work_item_id(task, "intake-1")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(intake_wi.work_item_id, task.id)

        parked = await executor._park_for_delegated_children(task)

        self.assertFalse(parked)
        refreshed = await self.store.get_delegation_work_item("intake-1")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.phase, Phase.APPROVED)
        self.assertTrue(refreshed.metadata.get("intake_delivery_spawned"))
        delivery = [
            wi
            for wi in await self.store.list_delegation_work_items("r")
            if wi.kind == "delivery"
        ]
        self.assertEqual(len(delivery), 1)
        self.assertEqual(delivery[0].phase, Phase.WAITING_DEPENDENCIES)


class SessionContinuityTests(unittest.IsolatedAsyncioTestCase):
    """Phase A role-instance model: a second task for the same role
    must reuse the existing role_session (one codex session per role,
    not per seat)."""

    async def test_second_delegation_reuses_role_session(self) -> None:
        from opc.layer2_organization.company_runtime import CompanyRuntime

        runtime = CompanyRuntime(org_engine=None, communication=None)
        task_a = Task(
            id="t-a", title="first", description="",
            assigned_to="designer", status=TaskStatus.PENDING,
            project_id="p", session_id="s",
            metadata={
                "delegation_run_id": "r1",
                "work_item_runtime": True,
                "delegation_seat_id": "seat::team::cmo::designer",
                "work_item_role_id": "designer",
            },
        )
        task_b = Task(
            id="t-b", title="second (rework)", description="",
            assigned_to="designer", status=TaskStatus.PENDING,
            project_id="p", session_id="s",
            metadata={
                "delegation_run_id": "r1",
                "work_item_runtime": True,
                "delegation_seat_id": "seat::team::cmo::designer",
                "work_item_role_id": "designer",
            },
        )
        rs_a = runtime._ensure_role_session(task_a)
        rs_a.adapter_session_state = {
            "external_resume_session_id": "codex-session-xyz",
            "selected_execution_agent": "codex",
        }
        rs_a.status = "idle"

        rs_b = runtime._ensure_role_session(task_b)
        self.assertIs(rs_a, rs_b)
        self.assertEqual(
            rs_b.adapter_session_state.get("external_resume_session_id"),
            "codex-session-xyz",
        )


if __name__ == "__main__":
    unittest.main()
