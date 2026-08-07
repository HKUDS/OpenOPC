"""Regression tests for the claim-release invariant (project-0011 livelock).

The claim CAS refuses any card whose claim columns are non-empty, so every
write that puts a card back into a fresh-runnable phase (READY /
READY_FOR_REWORK) must release ownership in the same write. Before this
invariant existed, two paths leaked claims and wedged whole runs:

- review REJECT → READY_FOR_REWORK kept the worker's claim columns and
  metadata mirror (``apply_delegation_review_resolution``);
- the synthesis wake WAITING_FOR_CHILDREN → READY cleared the columns but
  left the metadata mirror, which the CAS also used to gate claims.

The dispatcher retried the claim every tick and lost every time — a silent
livelock that survived restarts because the startup sweep skipped runnable
phases.
"""
from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

from opc.core.models import DelegationWorkItem, Phase
from opc.database.store import OPCStore


def _async_test(func):
    @wraps(func)
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


def _work_item(
    work_item_id: str,
    *,
    phase: Phase,
    metadata: dict | None = None,
    claimed_session: str = "",
    claimed_seat: str = "",
) -> DelegationWorkItem:
    return DelegationWorkItem(
        work_item_id=work_item_id,
        run_id="claim-invariant-run",
        cell_id="team::executor",
        role_id="executor",
        seat_id="seat::executor",
        title=work_item_id,
        kind="execute",
        projection_id=work_item_id,
        phase=phase,
        claimed_by_role_runtime_session_id=claimed_session,
        claimed_by_seat_id=claimed_seat,
        metadata=dict(metadata or {}),
    )


async def _assert_claimable(store: OPCStore, work_item_id: str, phase: Phase) -> None:
    claimed = await store.claim_delegation_work_item_if_dispatchable(
        work_item_id,
        expected_phase=phase,
        role_runtime_session_id="fresh-session",
        seat_id="seat::executor",
        task_id="fresh-task",
    )
    assert claimed is not None, f"{work_item_id} must be claimable after release"
    assert claimed.phase == Phase.RUNNING
    assert claimed.claimed_by_role_runtime_session_id == "fresh-session"


@_async_test
async def test_rework_verdict_releases_ownership(tmp_path: Path) -> None:
    """REJECT → READY_FOR_REWORK blanks claim columns and mirror in one write."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        item = _work_item(
            "rework-target",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            claimed_session="role-runtime::dead-worker",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::dead-worker",
                "claimed_task_id": "dead-task",
            },
        )
        await store.save_delegation_work_item(item)
        applied = await store.apply_delegation_review_resolution(
            item.work_item_id,
            source_report_work_item_id="",
            target_phase=Phase.READY_FOR_REWORK,
            blocked_reason="",
            metadata_updates={"rework_feedback": "fix the numbers"},
        )
        assert applied is not None
        assert applied.phase == Phase.READY_FOR_REWORK
        assert applied.claimed_by_role_runtime_session_id == ""
        assert applied.claimed_by_seat_id == ""
        assert applied.metadata["claimed_by_role_session_id"] == ""
        assert applied.metadata["claimed_task_id"] == ""
        await _assert_claimable(store, item.work_item_id, Phase.READY_FOR_REWORK)
    finally:
        await store.close()


@_async_test
async def test_phase_write_to_runnable_releases_ownership(tmp_path: Path) -> None:
    """Any update that lands in READY/READY_FOR_REWORK drops the claim."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        item = _work_item(
            "synthesis-parent",
            phase=Phase.WAITING_FOR_CHILDREN,
            claimed_session="role-runtime::dead-parent",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::dead-parent",
                "claimed_task_id": "parent-task",
            },
        )
        await store.save_delegation_work_item(item)
        updated = await store.update_delegation_work_item(
            item.work_item_id,
            phase=Phase.READY,
            metadata_updates={"work_kind": "synthesize"},
        )
        assert updated is not None
        assert updated.claimed_by_role_runtime_session_id == ""
        assert updated.claimed_by_seat_id == ""
        assert updated.metadata["claimed_by_role_session_id"] == ""
        assert updated.metadata["claimed_task_id"] == ""
        await _assert_claimable(store, item.work_item_id, Phase.READY)
    finally:
        await store.close()


@_async_test
async def test_claim_cas_ignores_stale_mirror(tmp_path: Path) -> None:
    """Ownership truth is the claim columns; a stale mirror must not gate."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        item = _work_item(
            "stale-mirror",
            phase=Phase.READY,
            metadata={
                "claimed_by_role_session_id": "role-runtime::forgotten",
                "claimed_task_id": "forgotten-task",
            },
        )
        await store.save_delegation_work_item(item)
        await _assert_claimable(store, item.work_item_id, Phase.READY)
    finally:
        await store.close()


@_async_test
async def test_startup_sweep_heals_claimed_runnable_rows(tmp_path: Path) -> None:
    """Legacy rows wedged in a runnable phase with a claim heal on restart."""
    db_path = tmp_path / "tasks.db"
    store = OPCStore(db_path)
    await store.initialize()
    try:
        item = _work_item(
            "legacy-wedged",
            phase=Phase.READY_FOR_REWORK,
            claimed_session="role-runtime::dead-worker",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::dead-worker",
                "claimed_task_id": "dead-task",
            },
        )
        await store.save_delegation_work_item(item)
    finally:
        await store.close()

    reopened = OPCStore(db_path)
    await reopened.initialize()
    try:
        healed = await reopened.get_delegation_work_item("legacy-wedged")
        assert healed is not None
        assert healed.phase == Phase.READY_FOR_REWORK
        assert healed.claimed_by_role_runtime_session_id == ""
        assert healed.claimed_by_seat_id == ""
        assert healed.metadata["claimed_by_role_session_id"] == ""
        assert healed.metadata["claimed_task_id"] == ""
        await _assert_claimable(reopened, "legacy-wedged", Phase.READY_FOR_REWORK)
    finally:
        await reopened.close()
