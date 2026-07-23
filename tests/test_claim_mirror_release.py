"""Regression tests for the claim-mirror release invariant.

The dispatch CAS (``claim_delegation_work_item_if_dispatchable``) predicates
on BOTH the runtime-claim column (``claimed_by_role_runtime_session_id`` /
``claimed_by_seat_id``) AND the metadata mirror
(``claimed_by_role_session_id`` / ``claimed_task_id``) being empty. Any
release path that clears only the column leaves the card in a runnable
phase but permanently unclaimable — a deadlock.

These tests pin the invariant "clearing the column also clears the mirror"
for the three release paths that matter:

1. ``transition_work_item(release_claim=True)`` into a non-terminal phase
   (the review-reject → READY_FOR_REWORK release), routed through the
   centralized ``update_delegation_work_item`` chokepoint.
2. ``refresh_dependents_for_run`` waking a parent out of
   WAITING_FOR_CHILDREN once all children are APPROVED (clear_claim_on_wake).
3. ``reopen_approved_delegation_work_item_for_rework(release_claim=True)``,
   which clears the column with its own SQL and must clear the mirror too.

Each asserts that after the release both the column and the mirror are
empty AND the CAS can re-claim the card.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.models import DelegationWorkItem, Phase
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401  (register hooks)
from opc.layer2_organization.work_item_transition import (
    refresh_dependents_for_run,
    transition_work_item,
)


class ClaimMirrorReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self) -> OPCStore:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = OPCStore(Path(tmpdir.name) / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        return store

    async def _seed_item(
        self,
        store: OPCStore,
        *,
        work_item_id: str = "wi-1",
        run_id: str = "run-1",
        phase: Phase = Phase.READY,
        metadata: dict | None = None,
    ) -> DelegationWorkItem:
        item = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id=run_id,
            role_id="executor",
            seat_id="seat-1",
            title="Execution",
            summary="Do the work.",
            kind="execute",
            projection_id="execution",
            phase=phase,
            metadata=dict(metadata or {}),
        )
        await store.save_delegation_work_item(item)
        return item

    async def _claim(
        self,
        store: OPCStore,
        work_item_id: str,
        phase: Phase,
        *,
        role_session_id: str = "role-sess-1",
        task_id: str = "task-1",
    ) -> DelegationWorkItem:
        claimed = await store.claim_delegation_work_item_if_dispatchable(
            work_item_id,
            expected_phase=phase,
            role_runtime_session_id=role_session_id,
            seat_id="seat-1",
            task_id=task_id,
        )
        assert claimed is not None, "claim CAS unexpectedly failed"
        return claimed

    def _assert_claim_held(self, item: DelegationWorkItem) -> None:
        metadata = dict(item.metadata or {})
        self.assertTrue(str(item.claimed_by_role_runtime_session_id or "").strip())
        self.assertTrue(str(metadata.get("claimed_by_role_session_id", "") or "").strip())
        self.assertTrue(str(metadata.get("claimed_task_id", "") or "").strip())

    def _assert_claim_fully_released(self, item: DelegationWorkItem) -> None:
        metadata = dict(item.metadata or {})
        self.assertEqual(str(item.claimed_by_role_runtime_session_id or "").strip(), "")
        self.assertEqual(str(item.claimed_by_seat_id or "").strip(), "")
        self.assertEqual(str(metadata.get("claimed_by_role_session_id", "") or "").strip(), "")
        self.assertEqual(str(metadata.get("claimed_task_id", "") or "").strip(), "")

    async def test_reject_release_clears_column_and_mirror_and_reclaims(self) -> None:
        """Scenario 1: review-reject release into READY_FOR_REWORK.

        A claimed card moves RUNNING → AWAITING_MANAGER_REVIEW, then the
        reviewer rejects it: transition_work_item(release_claim=True) into
        READY_FOR_REWORK. Both the column and the mirror must be empty and
        the CAS must re-claim the card.
        """
        store = await self._store()
        await self._seed_item(store)
        claimed = await self._claim(store, "wi-1", Phase.READY)
        self._assert_claim_held(claimed)

        await transition_work_item(
            store,
            "wi-1",
            target_phase=Phase.AWAITING_MANAGER_REVIEW,
            reason="worker_completed",
        )
        released = await transition_work_item(
            store,
            "wi-1",
            target_phase=Phase.READY_FOR_REWORK,
            reason="manager_rejected",
            release_claim=True,
        )
        assert released is not None
        self.assertEqual(released.phase, Phase.READY_FOR_REWORK)
        self._assert_claim_fully_released(released)

        reclaimed = await store.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.READY_FOR_REWORK,
            role_runtime_session_id="role-sess-2",
            seat_id="seat-1",
            task_id="task-2",
        )
        self.assertIsNotNone(reclaimed, "CAS must re-claim after reject-release")

    async def test_children_approved_wake_clears_column_and_mirror_and_reclaims(self) -> None:
        """Scenario 2: parent woken out of WAITING_FOR_CHILDREN.

        A parent holds a durable claim while WAITING_FOR_CHILDREN. Once the
        child is APPROVED, refresh_dependents_for_run wakes the parent to a
        non-terminal phase and releases the claim (clear_claim_on_wake).
        Both the column and the mirror must be empty and the CAS must
        re-claim the parent.
        """
        store = await self._store()
        await self._seed_item(
            store,
            work_item_id="parent",
            metadata={"dependency_work_item_ids": ["child"]},
        )
        await self._seed_item(
            store,
            work_item_id="child",
            phase=Phase.APPROVED,
        )
        # Parent claims, starts running, then parks on its child.
        parent_claimed = await self._claim(store, "parent", Phase.READY)
        self._assert_claim_held(parent_claimed)
        await transition_work_item(
            store,
            "parent",
            target_phase=Phase.WAITING_FOR_CHILDREN,
            reason="delegated_children",
        )
        parked = await store.get_delegation_work_item("parent")
        assert parked is not None
        # The park keeps the durable claim (no release on this transition).
        self._assert_claim_held(parked)

        changed = await refresh_dependents_for_run(store, run_id="run-1")
        self.assertTrue(changed)

        woken = await store.get_delegation_work_item("parent")
        assert woken is not None
        self.assertIn(woken.phase, {Phase.READY, Phase.RUNNING})
        self.assertNotEqual(woken.phase, Phase.WAITING_FOR_CHILDREN)
        self._assert_claim_fully_released(woken)

        reclaimed = await store.claim_delegation_work_item_if_dispatchable(
            "parent",
            expected_phase=woken.phase,
            role_runtime_session_id="role-sess-2",
            seat_id="seat-1",
            task_id="task-2",
        )
        self.assertIsNotNone(reclaimed, "CAS must re-claim the woken parent")

    async def test_reopen_approved_for_rework_clears_mirror_and_reclaims(self) -> None:
        """Scenario 3: reopen an APPROVED card for rework.

        reopen_approved_delegation_work_item_for_rework clears the claim
        column with its own SQL, bypassing update_delegation_work_item. It
        must still clear the metadata mirror so the reopened card does not
        deadlock on a stale audit claim.
        """
        store = await self._store()
        # An APPROVED card retains its claim as an audit record of the last
        # executor (column + mirror).
        await self._seed_item(
            store,
            phase=Phase.APPROVED,
            metadata={
                "claimed_by_role_session_id": "role-sess-1",
                "claimed_task_id": "task-1",
            },
        )
        held = await store.get_delegation_work_item("wi-1")
        assert held is not None
        held.claimed_by_role_runtime_session_id = "role-sess-1"
        held.claimed_by_seat_id = "seat-1"
        await store.save_delegation_work_item(held)
        self._assert_claim_held(await store.get_delegation_work_item("wi-1"))

        reopened = await store.reopen_approved_delegation_work_item_for_rework(
            "wi-1",
            target_phase=Phase.READY_FOR_REWORK,
            release_claim=True,
        )
        assert reopened is not None
        self.assertEqual(reopened.phase, Phase.READY_FOR_REWORK)
        self._assert_claim_fully_released(reopened)

        reclaimed = await store.claim_delegation_work_item_if_dispatchable(
            "wi-1",
            expected_phase=Phase.READY_FOR_REWORK,
            role_runtime_session_id="role-sess-2",
            seat_id="seat-1",
            task_id="task-2",
        )
        self.assertIsNotNone(reclaimed, "CAS must re-claim after reopen-for-rework")


if __name__ == "__main__":
    unittest.main()
