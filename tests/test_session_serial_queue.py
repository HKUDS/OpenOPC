"""Fix 5 PR3 tests — per-role serial queue semantics.

Covers:
- Schema + model roundtrip for ``pending_work_item_ids``.
- Atomic enqueue/dequeue helpers (FIFO, idempotent, busy check).
- ``enqueue_session_work_on_runnable_hook`` firing only when the feature
  flag is on AND the session is focused on a different work item.
- ``clear_session_focus_on_terminal_hook`` dequeuing the next pending
  work item and clearing its ``queued_behind_session`` stamp.
- ``is_dispatchable`` returns False for items stamped with
  ``queued_behind_session``.

The flag defaults to ON. Compatibility tests can still flip
``store.role_serial_queue_enabled = False`` before acting.
"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from opc.core.config import OPCConfig
from opc.core.models import (
    DelegationRun,
    DelegationRoleSession,
    DelegationWorkItem,
    Phase,
    SeatState,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401 (registers hooks)
from opc.layer2_organization.phase import is_dispatchable
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues


class _StoreFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()


# ── Schema + model roundtrip ─────────────────────────────────────────────


class SchemaRoundtripTests(_StoreFixture):
    async def test_pending_work_item_ids_persists(self) -> None:
        session = DelegationRoleSession(
            role_session_id="role-runtime::r::cto",
            run_id="r", role_id="cto",
            pending_work_item_ids=["wi-1", "wi-2", "wi-3"],
        )
        await self.store.save_delegation_role_session(session)

        loaded = await self.store.get_delegation_role_session("role-runtime::r::cto")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.pending_work_item_ids, ["wi-1", "wi-2", "wi-3"])

    async def test_default_pending_is_empty_list(self) -> None:
        session = DelegationRoleSession(
            role_session_id="role-runtime::r::cto",
            run_id="r", role_id="cto",
        )
        await self.store.save_delegation_role_session(session)
        loaded = await self.store.get_delegation_role_session("role-runtime::r::cto")
        self.assertEqual(loaded.pending_work_item_ids, [])

    async def test_legacy_role_statuses_normalize_to_three_states(self) -> None:
        cases = [
            ("cold", "", "idle", ""),
            ("reserved", "wi-1", "running", "wi-1"),
            ("running", "", "idle", ""),
            ("idle", "wi-stale", "idle", ""),
            ("dead", "", "blocked", ""),
            ("handoff_pending", "wi-2", "blocked", "wi-2"),
        ]
        for index, (status, focus, expected_status, expected_focus) in enumerate(cases):
            sid = f"role-runtime::r::legacy-{index}"
            await self.store.save_delegation_role_session(
                DelegationRoleSession(
                    role_session_id=sid,
                    run_id="r",
                    role_id=f"legacy-{index}",
                    status=status,
                    focused_work_item_id=focus,
                )
            )
            loaded = await self.store.get_delegation_role_session(sid)
            self.assertEqual(loaded.status, expected_status)
            self.assertEqual(loaded.focused_work_item_id, expected_focus)

    async def test_seat_status_and_resident_status_normalize_together(self) -> None:
        seat = SeatState(
            seat_state_id="seat-state::r::cto",
            team_instance_id="team-instance::r::cto",
            run_id="r",
            team_id="team::cto",
            seat_id="seat::cto",
            role_id="cto",
            status="reserved",
            resident_status="cold",
            current_work_item_id="wi-1",
        )
        await self.store.save_seat_state(seat)
        loaded = await self.store.get_seat_state(seat.seat_state_id)
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.resident_status, "running")


# ── Atomic enqueue/dequeue + busy check ─────────────────────────────────


class QueueHelperTests(_StoreFixture):
    async def _seed_session(self, *, focused: str = "") -> str:
        sid = "role-runtime::r::cto"
        session = DelegationRoleSession(
            role_session_id=sid, run_id="r", role_id="cto",
            focused_work_item_id=focused,
            status="running" if focused else "idle",
        )
        await self.store.save_delegation_role_session(session)
        return sid

    async def test_enqueue_appends_in_order(self) -> None:
        sid = await self._seed_session()
        self.assertTrue(await self.store.enqueue_pending_work_item(sid, "wi-1"))
        self.assertTrue(await self.store.enqueue_pending_work_item(sid, "wi-2"))
        self.assertTrue(await self.store.enqueue_pending_work_item(sid, "wi-3"))

        loaded = await self.store.get_delegation_role_session(sid)
        self.assertEqual(loaded.pending_work_item_ids, ["wi-1", "wi-2", "wi-3"])

    async def test_enqueue_duplicate_is_noop(self) -> None:
        sid = await self._seed_session()
        self.assertTrue(await self.store.enqueue_pending_work_item(sid, "wi-1"))
        # Second enqueue returns False and leaves the queue intact.
        self.assertFalse(await self.store.enqueue_pending_work_item(sid, "wi-1"))

        loaded = await self.store.get_delegation_role_session(sid)
        self.assertEqual(loaded.pending_work_item_ids, ["wi-1"])

    async def test_enqueue_unknown_session_returns_false(self) -> None:
        self.assertFalse(
            await self.store.enqueue_pending_work_item("no::such::session", "wi-1")
        )

    async def test_dequeue_pops_fifo_head(self) -> None:
        sid = await self._seed_session()
        for wid in ("wi-1", "wi-2", "wi-3"):
            await self.store.enqueue_pending_work_item(sid, wid)

        self.assertEqual(await self.store.dequeue_pending_work_item(sid), "wi-1")
        self.assertEqual(await self.store.dequeue_pending_work_item(sid), "wi-2")
        loaded = await self.store.get_delegation_role_session(sid)
        self.assertEqual(loaded.pending_work_item_ids, ["wi-3"])

    async def test_dequeue_empty_queue_returns_none(self) -> None:
        sid = await self._seed_session()
        self.assertIsNone(await self.store.dequeue_pending_work_item(sid))

    async def test_role_session_is_busy_reflects_focus(self) -> None:
        sid = await self._seed_session(focused="wi-current")
        self.assertTrue(await self.store.role_session_is_busy(sid))

        other = await self._seed_other_session()
        self.assertFalse(await self.store.role_session_is_busy(other))

    async def _seed_other_session(self) -> str:
        sid = "role-runtime::r::cmo"
        session = DelegationRoleSession(
            role_session_id=sid, run_id="r", role_id="cmo",
        )
        await self.store.save_delegation_role_session(session)
        return sid


# ── is_dispatchable honors queued stamp ──────────────────────────────────


class IsDispatchableQueueFilterTests(unittest.TestCase):
    def test_returns_false_when_queued_behind_session_stamp_present(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-queued",
            run_id="r", cell_id="c", role_id="cto",
            seat_id="seat", manager_role_id="", manager_seat_id="",
            title="t",
            phase=Phase.READY,
            metadata={"queued_behind_session": "role-runtime::r::cto"},
        )
        self.assertFalse(is_dispatchable(item))

    def test_returns_true_for_runnable_without_stamp(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-fresh",
            run_id="r", cell_id="c", role_id="cto",
            seat_id="seat", manager_role_id="", manager_seat_id="",
            title="t",
            phase=Phase.READY,
        )
        self.assertTrue(is_dispatchable(item))

    def test_empty_stamp_is_treated_as_not_queued(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-empty",
            run_id="r", cell_id="c", role_id="cto",
            seat_id="seat", manager_role_id="", manager_seat_id="",
            title="t",
            phase=Phase.READY,
            metadata={"queued_behind_session": ""},
        )
        self.assertTrue(is_dispatchable(item))

    def test_returns_false_when_company_runtime_dispatch_hold_present(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-suspended",
            run_id="r", cell_id="c", role_id="cto",
            seat_id="seat", manager_role_id="", manager_seat_id="",
            title="t",
            phase=Phase.RUNNING,
            metadata={"dispatch_hold": "company_runtime_suspended"},
        )
        self.assertFalse(is_dispatchable(item))

    def test_passive_review_parent_without_claim_is_not_dispatchable(self) -> None:
        for phase in (Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN):
            with self.subTest(phase=phase):
                item = DelegationWorkItem(
                    work_item_id=f"wi-{phase.value}",
                    run_id="r", cell_id="c", role_id="cto",
                    seat_id="seat", manager_role_id="ceo",
                    manager_seat_id="seat::ceo", title="t",
                    phase=phase,
                )
                self.assertFalse(is_dispatchable(item))


# ── enqueue_session_work_on_runnable_hook ────────────────────────────────


class EnqueueHookTests(_StoreFixture):
    async def _seed_session(self, *, focused: str = "") -> str:
        sid = "role-runtime::r::cto"
        session = DelegationRoleSession(
            role_session_id=sid, run_id="r", role_id="cto",
            focused_work_item_id=focused, status="running" if focused else "idle",
        )
        await self.store.save_delegation_role_session(session)
        return sid

    async def _save_runnable_item(
        self, *, wid: str, sid: str
    ) -> DelegationWorkItem:
        item = DelegationWorkItem(
            work_item_id=wid,
            run_id="r", cell_id="c",
            role_id="cto", seat_id="seat",
            role_runtime_session_id=sid,
            manager_role_id="ceo", manager_seat_id="seat::ceo",
            title="t",
            phase=Phase.READY,
        )
        await self.store.save_delegation_work_item(item)
        return item

    async def test_flag_off_is_noop(self) -> None:
        # Flag defaults to False — enqueueing a runnable work item for a
        # busy session must NOT touch the session queue or stamp metadata.
        sid = await self._seed_session(focused="wi-current")
        self.store.role_serial_queue_enabled = False

        await self._save_runnable_item(wid="wi-new", sid=sid)

        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, [])
        item = await self.store.get_delegation_work_item("wi-new")
        self.assertNotIn("queued_behind_session", item.metadata)

    async def test_flag_on_busy_session_enqueues(self) -> None:
        sid = await self._seed_session(focused="wi-current")
        self.store.role_serial_queue_enabled = True

        await self._save_runnable_item(wid="wi-new", sid=sid)

        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, ["wi-new"])
        item = await self.store.get_delegation_work_item("wi-new")
        self.assertEqual(item.metadata.get("queued_behind_session"), sid)
        # Stamped item is no longer dispatchable — the claim filter skips it.
        self.assertFalse(is_dispatchable(item))

    async def test_flag_on_idle_session_does_not_enqueue(self) -> None:
        # An idle session (no focus) should let the work item proceed
        # through the normal claim path — not get queued.
        sid = await self._seed_session(focused="")
        self.store.role_serial_queue_enabled = True

        await self._save_runnable_item(wid="wi-new", sid=sid)

        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, [])
        item = await self.store.get_delegation_work_item("wi-new")
        self.assertNotIn("queued_behind_session", item.metadata)

    async def test_same_item_as_focus_not_queued(self) -> None:
        # If the session's focus IS the work item whose phase is moving
        # (e.g. a re-READY cycle after rework), we must NOT enqueue
        # against itself — the session is actively handling it.
        sid = await self._seed_session(focused="wi-same")
        self.store.role_serial_queue_enabled = True

        await self._save_runnable_item(wid="wi-same", sid=sid)

        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, [])


# ── clear_session_focus_on_terminal_hook — dequeue path ─────────────────


class DequeueOnTerminalTests(_StoreFixture):
    async def test_flag_on_terminal_dequeues_and_clears_stamp(self) -> None:
        sid = "role-runtime::r::cto"
        session = DelegationRoleSession(
            role_session_id=sid, run_id="r", role_id="cto",
            focused_work_item_id="wi-active",
            status="running",
            pending_work_item_ids=["wi-next", "wi-later"],
        )
        await self.store.save_delegation_role_session(session)
        self.store.role_serial_queue_enabled = True

        # Queued item has the stamp from when it was enqueued.
        queued = DelegationWorkItem(
            work_item_id="wi-next",
            run_id="r", cell_id="c", role_id="cto", seat_id="s",
            manager_role_id="ceo", manager_seat_id="seat::ceo",
            role_runtime_session_id=sid,
            title="t",
            phase=Phase.READY,
            metadata={"queued_behind_session": sid},
        )
        await self.store.save_delegation_work_item(queued)
        later = DelegationWorkItem(
            work_item_id="wi-later",
            run_id="r", cell_id="c", role_id="cto", seat_id="s",
            manager_role_id="ceo", manager_seat_id="seat::ceo",
            role_runtime_session_id=sid,
            title="t",
            phase=Phase.READY,
            metadata={"queued_behind_session": sid},
        )
        await self.store.save_delegation_work_item(later)

        # The active work item transitions to APPROVED — terminal. Seed
        # a linked task so the task.status sync hook also gets its input.
        task = Task(
            id="task-active", title="t", description="",
            assigned_to="cto", status=TaskStatus.RUNNING,
            project_id="p", session_id="s",
        )
        await self.store.save_task(task)

        active = DelegationWorkItem(
            work_item_id="wi-active",
            run_id="r", cell_id="c", role_id="cto", seat_id="s",
            manager_role_id="ceo", manager_seat_id="seat::ceo",
            role_runtime_session_id=sid,
            title="t",
            phase=Phase.RUNNING,
        )
        await self.store.save_delegation_work_item(active)
        await self.store.link_work_item_runtime_task(active.work_item_id, task.id)
        await self.store.update_delegation_work_item(
            "wi-active", phase=Phase.APPROVED
        )

        # Head of queue ("wi-next") has been dequeued; stamp removed.
        session_after = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session_after.pending_work_item_ids, ["wi-later"])
        queued_after = await self.store.get_delegation_work_item("wi-next")
        self.assertNotIn("queued_behind_session", queued_after.metadata)
        self.assertTrue(is_dispatchable(queued_after))

    async def test_flag_off_terminal_does_not_dequeue(self) -> None:
        sid = "role-runtime::r::cto"
        session = DelegationRoleSession(
            role_session_id=sid, run_id="r", role_id="cto",
            focused_work_item_id="wi-active",
            status="running",
            pending_work_item_ids=["wi-next"],
        )
        await self.store.save_delegation_role_session(session)
        self.store.role_serial_queue_enabled = False  # explicit

        active = DelegationWorkItem(
            work_item_id="wi-active",
            run_id="r", cell_id="c", role_id="cto", seat_id="s",
            manager_role_id="ceo", manager_seat_id="seat::ceo",
            role_runtime_session_id=sid,
            title="t",
            phase=Phase.RUNNING,
        )
        await self.store.save_delegation_work_item(active)
        await self.store.update_delegation_work_item(
            "wi-active", phase=Phase.APPROVED
        )

        # Queue untouched when flag is off.
        session_after = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session_after.pending_work_item_ids, ["wi-next"])


# ── serial queue reconciler ──────────────────────────────────────────────


class SerialQueueReconcilerTests(_StoreFixture):
    async def _seed_session(
        self,
        *,
        sid: str = "role-runtime::r::cto",
        focused: str = "",
        pending: list[str] | None = None,
        status: str | None = None,
    ) -> str:
        session = DelegationRoleSession(
            role_session_id=sid,
            run_id="r",
            role_id=sid.rsplit("::", 1)[-1],
            focused_work_item_id=focused,
            status=status or ("running" if focused else "idle"),
            pending_work_item_ids=list(pending or []),
        )
        await self.store.save_delegation_role_session(session)
        return sid

    async def _save_item(
        self,
        *,
        wid: str,
        sid: str,
        phase: Phase = Phase.READY,
        marker: bool = False,
    ) -> DelegationWorkItem:
        item = DelegationWorkItem(
            work_item_id=wid,
            run_id="r",
            cell_id="c",
            role_id=sid.rsplit("::", 1)[-1],
            seat_id="seat",
            manager_role_id="ceo",
            manager_seat_id="seat::ceo",
            role_runtime_session_id=sid,
            title="t",
            phase=phase,
            metadata={"queued_behind_session": sid} if marker else {},
        )
        await self.store.save_delegation_work_item(item)
        return item

    async def test_reconcile_clears_stale_marker_for_idle_empty_queue(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = await self._seed_session()
        await self._save_item(wid="wi-ready", sid=sid, marker=True)

        result = await reconcile_role_serial_queues(self.store, "r")

        self.assertIn("wi-ready", result["cleared_markers"])
        item = await self.store.get_delegation_work_item("wi-ready")
        self.assertNotIn("queued_behind_session", item.metadata)
        self.assertTrue(is_dispatchable(item))

    async def test_reconcile_preserves_valid_marker_for_busy_session(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = await self._seed_session(focused="wi-active", pending=["wi-next"])
        active = await self._save_item(wid="wi-active", sid=sid, phase=Phase.RUNNING)
        active.claimed_by_role_runtime_session_id = sid
        active.claimed_by_seat_id = "seat"
        await self.store.save_delegation_work_item(active)
        await self._save_item(wid="wi-next", sid=sid, marker=True)

        result = await reconcile_role_serial_queues(self.store, "r")

        self.assertEqual(result["cleared_markers"], [])
        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, ["wi-next"])
        item = await self.store.get_delegation_work_item("wi-next")
        self.assertEqual(item.metadata.get("queued_behind_session"), sid)
        self.assertFalse(is_dispatchable(item))

    async def test_reconcile_promotes_aux_behind_unclaimed_review_focus(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = await self._seed_session(
            focused="wi-parent",
            pending=["wi-report"],
            status="running",
        )
        await self._save_item(
            wid="wi-parent",
            sid=sid,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        report = await self._save_item(wid="wi-report", sid=sid, marker=True)
        report.metadata.update({
            "report_execution_work_item": True,
            "report_target_work_item_id": "wi-parent",
        })
        await self.store.save_delegation_work_item(report)

        result = await reconcile_role_serial_queues(self.store, "r")

        self.assertIn(sid, result["cleared_focus_session_ids"])
        self.assertIn("wi-report", result["promoted_work_item_ids"])
        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.focused_work_item_id, "")
        self.assertEqual(session.pending_work_item_ids, [])
        report_after = await self.store.get_delegation_work_item("wi-report")
        self.assertNotIn("queued_behind_session", report_after.metadata)
        self.assertTrue(is_dispatchable(report_after))

    async def test_reconcile_preserves_live_claimed_review_focus(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = await self._seed_session(
            focused="wi-parent",
            pending=["wi-report"],
            status="running",
        )
        parent = await self._save_item(
            wid="wi-parent",
            sid=sid,
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        parent.claimed_by_role_runtime_session_id = sid
        parent.claimed_by_seat_id = "seat"
        await self.store.save_delegation_work_item(parent)
        await self._save_item(wid="wi-report", sid=sid, marker=True)

        result = await reconcile_role_serial_queues(self.store, "r")

        self.assertEqual(result["cleared_focus_session_ids"], [])
        self.assertEqual(result["promoted_work_item_ids"], [])
        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.focused_work_item_id, "wi-parent")
        self.assertEqual(session.pending_work_item_ids, ["wi-report"])
        report = await self.store.get_delegation_work_item("wi-report")
        self.assertEqual(report.metadata.get("queued_behind_session"), sid)
        self.assertFalse(is_dispatchable(report))

    async def test_reconcile_prunes_dead_entries_and_promotes_one_head(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = "role-runtime::r::cto"
        await self._save_item(wid="wi-done", sid=sid, phase=Phase.APPROVED, marker=True)
        await self._save_item(wid="wi-next", sid=sid, marker=True)
        await self._save_item(wid="wi-later", sid=sid, marker=True)
        await self._seed_session(
            sid=sid,
            pending=["wi-missing", "wi-done", "wi-next", "wi-later"],
        )

        result = await reconcile_role_serial_queues(self.store, "r")

        self.assertIn("wi-missing", result["pruned_pending_ids"])
        self.assertIn("wi-done", result["pruned_pending_ids"])
        self.assertIn("wi-next", result["promoted_work_item_ids"])
        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, ["wi-later"])
        promoted = await self.store.get_delegation_work_item("wi-next")
        later = await self.store.get_delegation_work_item("wi-later")
        self.assertNotIn("queued_behind_session", promoted.metadata)
        self.assertEqual(later.metadata.get("queued_behind_session"), sid)

        second = await reconcile_role_serial_queues(self.store, "r")

        self.assertNotIn("wi-later", second["promoted_work_item_ids"])
        later_after = await self.store.get_delegation_work_item("wi-later")
        self.assertEqual(later_after.metadata.get("queued_behind_session"), sid)

    async def test_terminal_transition_reconciles_even_when_focus_already_empty(self) -> None:
        self.store.role_serial_queue_enabled = True
        sid = await self._seed_session(pending=["wi-next"])
        await self._save_item(wid="wi-next", sid=sid, marker=True)
        active = await self._save_item(wid="wi-active", sid=sid, phase=Phase.RUNNING)

        await self.store.update_delegation_work_item(
            active.work_item_id,
            phase=Phase.APPROVED,
        )

        session = await self.store.get_delegation_role_session(sid)
        self.assertEqual(session.pending_work_item_ids, [])
        next_item = await self.store.get_delegation_work_item("wi-next")
        self.assertNotIn("queued_behind_session", next_item.metadata)
        self.assertTrue(is_dispatchable(next_item))

    async def test_idempotent_work_item_save_repairs_linked_task_status(self) -> None:
        task = Task(
            id="task-drift",
            title="t",
            description="",
            assigned_to="cto",
            status=TaskStatus.AWAITING_MANAGER_REVIEW,
            project_id="p",
            session_id="s",
            metadata={},
        )
        await self.store.save_task(task)
        item = DelegationWorkItem(
            work_item_id="wi-approved",
            run_id="r",
            cell_id="c",
            role_id="cto",
            seat_id="seat",
            manager_role_id="ceo",
            manager_seat_id="seat::ceo",
            title="t",
            phase=Phase.APPROVED,
        )
        await self.store.save_delegation_work_item(item)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)
        task.status = TaskStatus.AWAITING_MANAGER_REVIEW
        await self.store.save_task(task)

        await self.store.save_delegation_work_item(item)

        repaired = await self.store.get_task(task.id)
        self.assertEqual(repaired.status, TaskStatus.DONE)


class StartupClaimSweepTests(_StoreFixture):
    async def _seed_run(self) -> None:
        await self.store.save_delegation_run(
            DelegationRun(
                run_id="r",
                project_id="default",
                session_id="root-session",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )

    async def _take_over_and_settle(self) -> None:
        lease = await self.store.acquire_delegation_run_controller_lease(
            "r",
            project_id="default",
            root_session_id="root-session",
            owner_token="startup-recovery-test",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        await self.store.settle_stale_delegation_run_claims_for_controller(
            "r",
            project_id="default",
            root_session_id="root-session",
            owner_token="startup-recovery-test",
            generation=lease.generation,
        )

    async def test_restart_preserves_claim_until_controller_takeover(self) -> None:
        await self._seed_run()
        sid = "role-runtime::r::cto"
        report_id = "report::wi-parent::v1"
        await self.store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=sid,
                run_id="r",
                role_id="cto",
                focused_work_item_id=report_id,
                status="running",
            )
        )
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id=report_id,
                run_id="r",
                cell_id="c",
                role_id="cto",
                seat_id="seat",
                role_runtime_session_id=sid,
                manager_role_id="ceo",
                manager_seat_id="seat::ceo",
                title="report",
                kind="report",
                phase=Phase.RUNNING,
                claimed_by_role_runtime_session_id=sid,
                claimed_by_seat_id="seat",
                metadata={
                    "report_execution_work_item": True,
                    "report_target_work_item_id": "wi-parent",
                    "claimed_by_role_session_id": sid,
                    "claimed_task_id": "task-report",
                },
            )
        )

        db_path = self.store.db_path
        await self.store.close()
        self.store = OPCStore(db_path=db_path)
        await self.store.initialize()
        self.store.role_serial_queue_enabled = True

        # Merely opening a second process is not proof the first controller is
        # dead.  The durable run lease must be won before its claims are
        # released; this is the invariant that prevents split-brain startup.
        report_before = await self.store.get_delegation_work_item(report_id)
        session_before = await self.store.get_delegation_role_session(sid)
        self.assertEqual(report_before.claimed_by_role_runtime_session_id, sid)
        self.assertFalse(is_dispatchable(report_before))
        self.assertEqual(session_before.focused_work_item_id, report_id)
        self.assertEqual(session_before.status, "running")

        await self._take_over_and_settle()
        result = await reconcile_role_serial_queues(self.store, "r")

        report_after = await self.store.get_delegation_work_item(report_id)
        session_after = await self.store.get_delegation_role_session(sid)
        self.assertEqual(report_after.claimed_by_role_runtime_session_id, "")
        self.assertTrue(is_dispatchable(report_after))
        self.assertEqual(session_after.focused_work_item_id, "")
        self.assertEqual(session_after.status, "idle")
        self.assertEqual(result["cleared_focus_session_ids"], [])

    async def test_controller_takeover_releases_review_claim_without_dispatching_parent(self) -> None:
        await self._seed_run()
        parent = DelegationWorkItem(
            work_item_id="wi-parent",
            run_id="r", cell_id="c", role_id="cto", seat_id="seat",
            manager_role_id="ceo", manager_seat_id="seat::ceo", title="parent",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            claimed_by_role_runtime_session_id="dead-role-session",
            claimed_by_seat_id="seat",
            metadata={
                "claimed_by_role_session_id": "dead-role-session",
                "claimed_task_id": "task-parent",
            },
        )
        report = DelegationWorkItem(
            work_item_id="wi-report",
            run_id="r", cell_id="c", role_id="cto", seat_id="seat",
            manager_role_id="ceo", manager_seat_id="seat::ceo", title="report",
            phase=Phase.READY,
            metadata={
                "report_execution_work_item": True,
                "report_target_work_item_id": "wi-parent",
            },
        )
        await self.store.save_delegation_work_item(parent)
        await self.store.save_delegation_work_item(report)

        db_path = self.store.db_path
        await self.store.close()
        self.store = OPCStore(db_path=db_path)
        await self.store.initialize()

        parent_before = await self.store.get_delegation_work_item("wi-parent")
        self.assertEqual(
            parent_before.claimed_by_role_runtime_session_id,
            "dead-role-session",
        )

        await self._take_over_and_settle()
        parent_after = await self.store.get_delegation_work_item("wi-parent")
        report_after = await self.store.get_delegation_work_item("wi-report")
        self.assertEqual(parent_after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(parent_after.claimed_by_seat_id, "")
        self.assertEqual(parent_after.metadata.get("claimed_by_role_session_id"), "")
        self.assertEqual(parent_after.metadata.get("claimed_task_id"), "")
        self.assertTrue(parent_after.metadata.get("controller_takeover_released_at"))
        self.assertFalse(is_dispatchable(parent_after))
        self.assertTrue(is_dispatchable(report_after))


# ── Config loader picks up the feature flag ──────────────────────────────


class ConfigLoaderFlagTests(unittest.TestCase):
    """OPCConfig.load must pass ``role_serial_queue_enabled`` through from
    the org YAML. The engine mirrors this onto the store during init so
    phase hooks can consult it without an upward dependency."""

    def test_flag_on_in_yaml_reaches_org_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            (config_dir / "org_config.yaml").write_text(
                textwrap.dedent(
                    """\
                    role_serial_queue_enabled: true
                    """
                ),
                encoding="utf-8",
            )
            config = OPCConfig.load(config_dir=config_dir)
            self.assertTrue(config.org.role_serial_queue_enabled)

    def test_flag_default_true_when_key_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp)
            # An empty org_config.yaml — flag should stay at its default.
            (config_dir / "org_config.yaml").write_text(
                "team_runtime:\n  default_team_id: ''\n",
                encoding="utf-8",
            )
            config = OPCConfig.load(config_dir=config_dir)
            self.assertTrue(config.org.role_serial_queue_enabled)


if __name__ == "__main__":
    unittest.main()
