"""Regression: a blocked member session parked on a runnable work item must
converge to idle inside claim_runnable_tasks (OBS-8, t1/t3 native wedge).

The wake write (children approved → parent READY) can race complete_claim's
review-preempt restore, leaving the in-memory session `blocked` while the DB
work item is runnable and unclaimed. The dispatcher then skips the session
every tick forever. The claim loop is the consumption point, so it owns the
final say: a blocked session whose park reason no longer exists is converged
to idle before the skip decision.
"""
from __future__ import annotations

import unittest

from opc.core.models import CompanyMemberSession, DelegationWorkItem, Phase
from opc.layer2_organization.company_runtime import CompanyRuntime


def _runtime() -> CompanyRuntime:
    return CompanyRuntime(org_engine=None, communication=None, store=None)


def _session(role: str, focused: str) -> CompanyMemberSession:
    session = CompanyMemberSession(
        member_session_id=f"ms-{role}",
        role_id=role,
        employee_id=f"{role}-default",
    )
    session.status = "blocked"
    session.resident_status = "blocked"
    session.focused_work_item_id = focused
    return session


def _work_item(work_item_id: str, *, phase: Phase) -> DelegationWorkItem:
    return DelegationWorkItem(
        work_item_id=work_item_id,
        run_id="r",
        cell_id="c",
        role_id="cmo",
        seat_id="seat-cmo",
        title="parent",
        phase=phase,
        claimed_by_role_runtime_session_id="",
    )


class BlockedSessionClaimReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_session_with_ready_focus_converges_to_idle(self) -> None:
        runtime = _runtime()
        session = _session("cmo", focused="wi-1")
        runtime.member_sessions[session.member_session_id] = session
        item = _work_item("wi-1", phase=Phase.READY)

        await runtime.claim_runnable_tasks([], work_items=[item])

        self.assertEqual(session.status, "idle")
        self.assertEqual(session.focused_work_item_id, "")

    async def test_blocked_session_with_empty_focus_converges_to_idle(self) -> None:
        runtime = _runtime()
        session = _session("cmo", focused="")
        runtime.member_sessions[session.member_session_id] = session

        await runtime.claim_runnable_tasks([], work_items=[])

        self.assertEqual(session.status, "idle")

    async def test_blocked_session_with_terminal_focus_converges_to_idle(self) -> None:
        """Observed t3 wedge: preempt-restore left the session parked on its
        own APPROVED review card — the awaited event already happened."""
        runtime = _runtime()
        session = _session("cto", focused="review::wi-9::v1")
        runtime.member_sessions[session.member_session_id] = session
        item = _work_item("review::wi-9::v1", phase=Phase.APPROVED)

        await runtime.claim_runnable_tasks([], work_items=[item])

        self.assertEqual(session.status, "idle")
        self.assertEqual(session.focused_work_item_id, "")

    async def test_blocked_session_waiting_children_stays_blocked(self) -> None:
        runtime = _runtime()
        session = _session("cmo", focused="wi-1")
        runtime.member_sessions[session.member_session_id] = session
        item = _work_item("wi-1", phase=Phase.WAITING_FOR_CHILDREN)

        await runtime.claim_runnable_tasks([], work_items=[item])

        self.assertEqual(session.status, "blocked")
        self.assertEqual(session.focused_work_item_id, "wi-1")


if __name__ == "__main__":
    unittest.main()
