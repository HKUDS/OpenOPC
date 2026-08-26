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

import pytest

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
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
async def test_controller_takeover_heals_claimed_runnable_rows(tmp_path: Path) -> None:
    """A won run lease, not process startup, heals a wedged legacy claim."""
    db_path = tmp_path / "tasks.db"
    store = OPCStore(db_path)
    await store.initialize()
    try:
        await store.save_delegation_run(
            DelegationRun(
                run_id="claim-invariant-run",
                project_id="default",
                session_id="claim-invariant-root",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
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
        # Opening the DB cannot steal a healthy process's claim. Recovery is
        # authorized only after this controller wins the durable run lease.
        before_takeover = await reopened.get_delegation_work_item("legacy-wedged")
        assert before_takeover is not None
        assert before_takeover.claimed_by_role_runtime_session_id == (
            "role-runtime::dead-worker"
        )

        lease = await reopened.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            lease_seconds=60,
        )
        assert lease.acquired
        await reopened.settle_stale_delegation_run_claims_for_controller(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            generation=lease.generation,
        )

        healed = await reopened.get_delegation_work_item("legacy-wedged")
        assert healed is not None
        assert healed.phase == Phase.READY_FOR_REWORK
        assert healed.claimed_by_role_runtime_session_id == ""
        assert healed.claimed_by_seat_id == ""
        assert healed.metadata["claimed_by_role_session_id"] == ""
        assert healed.metadata["claimed_task_id"] == ""
        claimed = await reopened.claim_delegation_work_item_if_dispatchable(
            "legacy-wedged",
            expected_phase=Phase.READY_FOR_REWORK,
            role_runtime_session_id="fresh-session",
            seat_id="seat::executor",
            task_id="fresh-task",
            controller_owner_token="claim-invariant-recovery",
            controller_lease_generation=lease.generation,
        )
        assert claimed is not None
        assert claimed.phase == Phase.RUNNING
    finally:
        await reopened.close()


@_async_test
async def test_controller_takeover_settles_linked_fully_legacy_envelope(
    tmp_path: Path,
) -> None:
    """A linked v1 claim stays a parked Task until a new attempt is claimed."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_delegation_run(
            DelegationRun(
                run_id="claim-invariant-run",
                project_id="default",
                session_id="claim-invariant-root",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        task = Task(
            id="legacy-linked-task",
            project_id="default",
            session_id="claim-invariant-root",
            title="legacy-linked",
            status=TaskStatus.DONE,
            metadata={
                "delegation_run_id": "claim-invariant-run",
                "work_item_projection_id": "legacy-linked",
                "work_item_runtime": True,
            },
        )
        item = _work_item(
            "legacy-linked",
            phase=Phase.APPROVED,
            claimed_session="role-runtime::dead-worker",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::dead-worker",
                "claimed_task_id": task.id,
            },
        )
        await store.save_delegation_work_item(item)
        await store.save_task(task)
        assert await store.link_work_item_runtime_task(item.work_item_id, task.id)

        lease = await store.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            lease_seconds=60,
        )
        assert lease.acquired
        assert (
            await store.settle_stale_delegation_run_claims_for_controller(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                generation=lease.generation,
            )
            == 1
        )

        healed = await store.get_delegation_work_item(item.work_item_id)
        parked_task = await store.get_task(task.id)
        assert healed is not None and parked_task is not None
        assert healed.phase == Phase.APPROVED
        assert healed.claimed_by_role_runtime_session_id == ""
        assert healed.claimed_by_seat_id == ""
        assert healed.metadata["claimed_by_role_session_id"] == ""
        assert healed.metadata["claimed_task_id"] == ""
        assert healed.metadata["company_run_controller_owner_token"] == (
            "claim-invariant-recovery"
        )
        assert healed.metadata["company_run_controller_lease_generation"] == (
            lease.generation
        )
        assert int(healed.metadata.get("attempt_seq", 0) or 0) == 0
        assert parked_task.status == TaskStatus.DONE
        for key in (
            "claimed_work_item_attempt_seq",
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
        ):
            assert key not in parked_task.metadata

        assert (
            await store.settle_stale_delegation_run_claims_for_controller(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                generation=lease.generation,
            )
            == 0
        )
    finally:
        await store.close()


@_async_test
async def test_controller_takeover_settles_linked_legacy_attempt_ledger(
    tmp_path: Path,
) -> None:
    """Pre-controller attempt ledgers remain upgradeable after terminal settle."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_delegation_run(
            DelegationRun(
                run_id="claim-invariant-run",
                project_id="default",
                session_id="claim-invariant-root",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        task = Task(
            id="legacy-attempt-task",
            project_id="default",
            session_id="claim-invariant-root",
            title="legacy-attempt",
            status=TaskStatus.DONE,
            metadata={
                "delegation_run_id": "claim-invariant-run",
                "work_item_projection_id": "legacy-attempt",
                "work_item_runtime": True,
            },
        )
        item = _work_item(
            "legacy-attempt",
            phase=Phase.APPROVED,
            claimed_session="role-runtime::dead-worker",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::dead-worker",
                "claimed_task_id": task.id,
                "attempt_seq": 3,
                "attempt_settled": True,
                "attempt_outcome": "approved",
            },
        )
        await store.save_delegation_work_item(item)
        await store.save_task(task)
        assert await store.link_work_item_runtime_task(item.work_item_id, task.id)
        lease = await store.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            lease_seconds=60,
        )
        assert lease.acquired

        assert (
            await store.settle_stale_delegation_run_claims_for_controller(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                generation=lease.generation,
            )
            == 1
        )
        healed = await store.get_delegation_work_item(item.work_item_id)
        parked_task = await store.get_task(task.id)
        assert healed is not None and parked_task is not None
        assert healed.claimed_by_role_runtime_session_id == ""
        assert healed.claimed_by_seat_id == ""
        assert healed.metadata["attempt_seq"] == 3
        assert healed.metadata["attempt_settled"] is True
        assert healed.metadata["attempt_outcome"] == "approved"
        assert healed.metadata["company_run_controller_owner_token"] == (
            "claim-invariant-recovery"
        )
        assert parked_task.status == TaskStatus.DONE
        for key in (
            "claimed_work_item_attempt_seq",
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
        ):
            assert key not in parked_task.metadata
    finally:
        await store.close()


@_async_test
async def test_controller_takeover_adopts_fully_released_modern_terminal_task(
    tmp_path: Path,
) -> None:
    """A settled modern WorkItem can recover its fully parked linked Task."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_delegation_run(
            DelegationRun(
                run_id="claim-invariant-run",
                project_id="default",
                session_id="claim-invariant-root",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        task = Task(
            id="released-modern-task",
            project_id="default",
            session_id="claim-invariant-root",
            title="released-modern",
            status=TaskStatus.DONE,
            metadata={
                "delegation_run_id": "claim-invariant-run",
                "work_item_projection_id": "released-modern",
                "work_item_runtime": True,
            },
        )
        item = _work_item(
            "released-modern",
            phase=Phase.APPROVED,
            claimed_session="role-runtime::prior-worker",
            claimed_seat="seat::executor",
            metadata={
                "claimed_by_role_session_id": "role-runtime::prior-worker",
                "claimed_task_id": task.id,
                "attempt_seq": 3,
                "attempt_settled": True,
                "attempt_outcome": "approved",
                "company_run_controller_owner_token": "prior-controller",
                "company_run_controller_lease_generation": 1,
            },
        )
        await store.save_delegation_work_item(item)
        await store.save_task(task)
        assert await store.link_work_item_runtime_task(item.work_item_id, task.id)

        prior_lease = await store.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="prior-controller",
            lease_seconds=60,
        )
        assert prior_lease.acquired and prior_lease.generation == 1
        assert await store.release_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="prior-controller",
            generation=prior_lease.generation,
        )
        recovery_lease = await store.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            lease_seconds=60,
        )
        assert recovery_lease.acquired and recovery_lease.generation == 2

        assert (
            await store.settle_stale_delegation_run_claims_for_controller(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                generation=recovery_lease.generation,
            )
            == 1
        )
        healed = await store.get_delegation_work_item(item.work_item_id)
        adopted_task = await store.get_task(task.id)
        assert healed is not None and adopted_task is not None
        assert healed.claimed_by_role_runtime_session_id == ""
        assert healed.claimed_by_seat_id == ""
        assert healed.metadata["claimed_task_id"] == ""
        assert healed.metadata["attempt_seq"] == 3
        assert healed.metadata["attempt_settled"] is True
        assert healed.metadata["company_run_controller_owner_token"] == (
            "claim-invariant-recovery"
        )
        assert healed.metadata[
            "company_run_controller_lease_generation"
        ] == recovery_lease.generation
        assert adopted_task.metadata["claimed_work_item_attempt_seq"] == 3
        assert adopted_task.metadata[
            "company_run_controller_owner_token"
        ] == "claim-invariant-recovery"
        assert adopted_task.metadata[
            "company_run_controller_lease_generation"
        ] == recovery_lease.generation
    finally:
        await store.close()


@_async_test
async def test_controller_takeover_rejects_present_legacy_credential_keys(
    tmp_path: Path,
) -> None:
    """Zero or unsettled attempt residue is not a terminal legacy envelope."""
    variants = (
        ("work-item-zero-attempt", {"attempt_seq": 0}, {}),
        (
            "work-item-unsettled-attempt",
            {"attempt_seq": 1, "attempt_settled": False},
            {},
        ),
        ("task-zero-attempt", {}, {"claimed_work_item_attempt_seq": 0}),
    )
    for suffix, item_credentials, task_credentials in variants:
        store = OPCStore(tmp_path / f"{suffix}.db")
        await store.initialize()
        try:
            await store.save_delegation_run(
                DelegationRun(
                    run_id="claim-invariant-run",
                    project_id="default",
                    session_id="claim-invariant-root",
                    execution_model="multi_team_org",
                    status="running",
                    lifecycle_status="active",
                )
            )
            task = Task(
                id=f"{suffix}-task",
                project_id="default",
                session_id="claim-invariant-root",
                title=suffix,
                status=TaskStatus.DONE,
                metadata={
                    "delegation_run_id": "claim-invariant-run",
                    "work_item_projection_id": suffix,
                    "work_item_runtime": True,
                    **task_credentials,
                },
            )
            item = _work_item(
                suffix,
                phase=Phase.APPROVED,
                claimed_session=f"role-runtime::{suffix}",
                claimed_seat="seat::executor",
                metadata={
                    "claimed_by_role_session_id": f"role-runtime::{suffix}",
                    "claimed_task_id": task.id,
                    **item_credentials,
                },
            )
            await store.save_delegation_work_item(item)
            await store.save_task(task)
            assert await store.link_work_item_runtime_task(
                item.work_item_id,
                task.id,
            )
            lease = await store.acquire_delegation_run_controller_lease(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                lease_seconds=60,
            )
            assert lease.acquired
            before_item = await store.get_delegation_work_item(item.work_item_id)
            before_task = await store.get_task(task.id)

            with pytest.raises(
                RuntimeError,
                match="mixed linked Task/WorkItem attempt envelope",
            ):
                await store.settle_stale_delegation_run_claims_for_controller(
                    "claim-invariant-run",
                    project_id="default",
                    root_session_id="claim-invariant-root",
                    owner_token="claim-invariant-recovery",
                    generation=lease.generation,
                )

            assert await store.get_delegation_work_item(item.work_item_id) == before_item
            assert await store.get_task(task.id) == before_task
        finally:
            await store.close()


@_async_test
async def test_controller_takeover_rejects_mixed_linked_envelope_atomically(
    tmp_path: Path,
) -> None:
    """One partial Task credential rolls back every claim release in the run."""
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await store.save_delegation_run(
            DelegationRun(
                run_id="claim-invariant-run",
                project_id="default",
                session_id="claim-invariant-root",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        pairs: list[tuple[DelegationWorkItem, Task]] = []
        for suffix, task_metadata in (
            ("legacy-first", {}),
            ("mixed-second", {"claimed_work_item_attempt_seq": 1}),
        ):
            task = Task(
                id=f"{suffix}-task",
                project_id="default",
                session_id="claim-invariant-root",
                title=suffix,
                status=TaskStatus.DONE,
                metadata={
                    "delegation_run_id": "claim-invariant-run",
                    "work_item_projection_id": suffix,
                    "work_item_runtime": True,
                    **task_metadata,
                },
            )
            item = _work_item(
                suffix,
                phase=Phase.APPROVED,
                claimed_session=f"role-runtime::{suffix}",
                claimed_seat="seat::executor",
                metadata={
                    "claimed_by_role_session_id": f"role-runtime::{suffix}",
                    "claimed_task_id": task.id,
                },
            )
            await store.save_delegation_work_item(item)
            await store.save_task(task)
            assert await store.link_work_item_runtime_task(
                item.work_item_id,
                task.id,
            )
            pairs.append((item, task))

        lease = await store.acquire_delegation_run_controller_lease(
            "claim-invariant-run",
            project_id="default",
            root_session_id="claim-invariant-root",
            owner_token="claim-invariant-recovery",
            lease_seconds=60,
        )
        assert lease.acquired
        before_items = {
            item.work_item_id: await store.get_delegation_work_item(item.work_item_id)
            for item, _task in pairs
        }
        before_tasks = {task.id: await store.get_task(task.id) for _item, task in pairs}

        try:
            await store.settle_stale_delegation_run_claims_for_controller(
                "claim-invariant-run",
                project_id="default",
                root_session_id="claim-invariant-root",
                owner_token="claim-invariant-recovery",
                generation=lease.generation,
            )
        except RuntimeError as exc:
            assert "mixed linked Task/WorkItem attempt envelope" in str(exc)
        else:
            raise AssertionError("a partial linked Task credential must fail closed")

        for item, task in pairs:
            assert (
                await store.get_delegation_work_item(item.work_item_id)
                == (before_items[item.work_item_id])
            )
            assert await store.get_task(task.id) == before_tasks[task.id]
    finally:
        await store.close()
