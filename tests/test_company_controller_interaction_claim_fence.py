from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from opc.core.models import DelegationRun, ExecutionCheckpoint, Task
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator


def _async_test(func):
    @wraps(func)
    def runner(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return runner


async def _seed_answered_company_interaction(store: OPCStore) -> None:
    await store.save_delegation_run(
        DelegationRun(
            run_id="run-1",
            project_id="project-a",
            session_id="root-session",
            execution_model="multi_team_org",
            status="running",
            lifecycle_status="active",
        )
    )
    await store.save_task(
        Task(
            id="worker-task",
            project_id="project-a",
            session_id="worker-session",
            parent_session_id="root-session",
            title="Wait for owner input",
            metadata={"delegation_run_id": "run-1"},
        )
    )
    checkpoint = ExecutionCheckpoint(
        checkpoint_id="interaction-1",
        project_id="project-a",
        session_id="worker-session",
        checkpoint_type="task_user_input",
        task_id="worker-task",
        payload={
            "interaction": {
                "kind": "task_user_input",
                "domain_key": "task-user-input:interaction-1",
                "ownership": {
                    "waiting_task_id": "worker-task",
                    "waiting_session_id": "worker-session",
                    "ui_anchor_task_id": "root-task",
                    "ui_anchor_session_id": "root-session",
                    "root_session_id": "root-session",
                    "company_runtime_session_id": "root-session",
                },
            }
        },
    )
    await store.create_owner_interaction_checkpoint(
        checkpoint,
        interaction_key="task-user-input:interaction-1",
    )
    accepted = await store.accept_execution_checkpoint_decision(
        checkpoint.checkpoint_id,
        project_id="project-a",
        checkpoint_type=checkpoint.checkpoint_type,
        request_id="owner-answer-1",
        decision_hash="answer-hash-1",
        decision={"text": "Continue with the documented assumptions."},
    )
    assert accepted.acknowledged


@_async_test
async def test_takeover_in_observation_claim_window_blocks_nonowner_atomically(
    tmp_path: Path,
) -> None:
    """A real second Store may acquire gen2 after an old recovery observation."""

    db_path = tmp_path / "tasks.db"
    stale_store = OPCStore(db_path)
    winner_store = OPCStore(db_path)
    await stale_store.initialize()
    await winner_store.initialize(run_startup_maintenance=False)
    observed_without_controller = asyncio.Event()
    takeover_complete = asyncio.Event()
    try:
        await _seed_answered_company_interaction(stale_store)
        generation_one = await stale_store.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="generation-one-owner",
            lease_seconds=60,
        )
        assert generation_one.acquired
        assert await stale_store.release_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="generation-one-owner",
            generation=generation_one.generation,
        )
        before = await stale_store.get_execution_checkpoint(
            "interaction-1",
            project_id="project-a",
            checkpoint_type="task_user_input",
        )
        assert before is not None and before.status == "answered"

        async def stale_check_then_claim():
            # This is the exact historical Engine window.  Correctness no
            # longer depends on the result of this observation.
            assert not await stale_store.delegation_run_controller_lease_is_current(
                "run-1",
                project_id="project-a",
            )
            observed_without_controller.set()
            await takeover_complete.wait()
            return await stale_store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="task_user_input",
                consumer_id="stale-recovery",
                enforce_company_controller_eligibility=True,
                controller_run_id="run-1",
                controller_root_session_id="root-session",
                controller_owner_token="generation-one-owner",
                controller_lease_generation=generation_one.generation,
            )

        async def acquire_generation_two():
            await observed_without_controller.wait()
            lease = await winner_store.acquire_delegation_run_controller_lease(
                "run-1",
                project_id="project-a",
                root_session_id="root-session",
                owner_token="generation-two-owner",
                lease_seconds=60,
            )
            assert lease.acquired
            takeover_complete.set()
            return lease

        claim, lease = await asyncio.gather(
            stale_check_then_claim(),
            acquire_generation_two(),
        )
        assert lease.generation > generation_one.generation
        assert claim.outcome == "controller_busy"
        assert not claim.acquired

        after = await stale_store.get_execution_checkpoint(
            "interaction-1",
            project_id="project-a",
            checkpoint_type="task_user_input",
        )
        assert after == before
        assert "claim" not in after.payload["interaction"]
    finally:
        await winner_store.close()
        await stale_store.close()


@_async_test
async def test_matching_local_controller_generation_can_claim(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await _seed_answered_company_interaction(store)
        lease = await store.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="local-owner",
            lease_seconds=60,
        )
        assert lease.acquired

        claim = await store.claim_answered_execution_checkpoint(
            "interaction-1",
            project_id="project-a",
            checkpoint_type="task_user_input",
            consumer_id="local-recovery",
            enforce_company_controller_eligibility=True,
            controller_run_id="run-1",
            controller_root_session_id="root-session",
            controller_owner_token="local-owner",
            controller_lease_generation=lease.generation,
        )
        assert claim.acquired
        assert claim.outcome == "claimed"
        assert claim.checkpoint is not None
        assert claim.checkpoint.status == "consuming"
    finally:
        await store.close()


@_async_test
async def test_no_live_controller_allows_recovery_claim(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "tasks.db")
    await store.initialize()
    try:
        await _seed_answered_company_interaction(store)
        expired = await store.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="crashed-owner",
            lease_seconds=60,
        )
        assert expired.acquired
        assert await store.renew_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="crashed-owner",
            generation=expired.generation,
            lease_seconds=1,
            heartbeat_at=datetime.now() - timedelta(seconds=10),
        )
        claim = await store.claim_answered_execution_checkpoint(
            "interaction-1",
            project_id="project-a",
            checkpoint_type="task_user_input",
            consumer_id="restart-recovery",
            enforce_company_controller_eligibility=True,
        )
        assert claim.acquired
        assert claim.outcome == "claimed"
        assert claim.checkpoint is not None
        assert claim.checkpoint.status == "consuming"
    finally:
        await store.close()


@_async_test
async def test_engine_recovery_consumer_uses_atomic_controller_fence(
    tmp_path: Path,
) -> None:
    owner_store = OPCStore(tmp_path / "tasks.db")
    remote_store = OPCStore(tmp_path / "tasks.db")
    await owner_store.initialize()
    await remote_store.initialize(run_startup_maintenance=False)
    try:
        await _seed_answered_company_interaction(owner_store)
        lease = await owner_store.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-a",
            root_session_id="root-session",
            owner_token="remote-owner",
            lease_seconds=60,
        )
        assert lease.acquired

        engine = OPCEngine(project_id="project-a")
        engine.store = remote_store
        engine.interaction_coordinator = InteractionCoordinator(
            store=remote_store,
            project_id="project-a",
        )
        engine.company_executor = SimpleNamespace(
            controller_lease_credential=lambda _run_id: None,
        )

        await engine._consume_answered_interaction_if_controller_eligible(
            "interaction-1",
            "task_user_input",
        )
        persisted = await remote_store.get_execution_checkpoint(
            "interaction-1",
            project_id="project-a",
            checkpoint_type="task_user_input",
        )
        assert persisted is not None
        assert persisted.status == "answered"
        assert "claim" not in persisted.payload["interaction"]
    finally:
        await remote_store.close()
        await owner_store.close()
