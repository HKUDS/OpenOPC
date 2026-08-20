from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opc.core.config import OPCConfig
from opc.core.events import EventBus
from opc.core.models import (
    ReorgChangeSet,
    ReorgEventKind,
    ReorgProposalStatus,
    ReorgRoleChange,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan
from opc.layer2_organization.reorg_manager import ReorgManager
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)


def test_reorg_manager_has_no_second_owner_approval_path() -> None:
    assert not hasattr(ReorgManager, "request_reorg_approval")


def _manager(store: OPCStore, root: Path) -> tuple[ReorgManager, OrgEngine]:
    org_engine = OrgEngine(OPCConfig(), root)
    communication = CommunicationManager(
        store,
        EventBus(),
        org_engine=org_engine,
    )
    return (
        ReorgManager(
            store=store,
            org_engine=org_engine,
            communication=communication,
            interaction_coordinator=InteractionCoordinator(
                store=store,
                project_id="project-a",
            ),
        ),
        org_engine,
    )


async def _proposal(manager: ReorgManager):
    return await manager.propose_reorg(
        project_id="project-a",
        summary="Add an architecture reviewer.",
        source_role_id="coordinator",
        changeset=ReorgChangeSet(
            role_changes=[
                ReorgRoleChange(
                    action="add",
                    role={
                        "id": "architecture_reviewer",
                        "name": "Architecture Reviewer",
                        "responsibility": "Review architecture changes.",
                    },
                )
            ]
        ),
    )


async def _claim_reorg_decision(
    manager: ReorgManager,
    proposal,
    *,
    approved: bool,
    consumer_id: str = "test-controller",
) -> InteractionDecisionLease:
    coordinator = manager.interaction_coordinator
    assert coordinator is not None
    checkpoints = await manager.store.get_execution_checkpoints(
        project_id=proposal.project_id,
        checkpoint_types=["company_reorg_pending"],
        statuses=["pending"],
    )
    checkpoint = next(
        row
        for row in checkpoints
        if row.payload.get("proposal_id") == proposal.proposal_id
    )
    decision = {"option_id": "approve" if approved else "deny"}
    accepted = await coordinator.submit(
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_type=checkpoint.checkpoint_type,
        decision=decision,
        client_request_id=f"decision:{consumer_id}:{approved}",
        checkpoint=checkpoint,
    )
    assert accepted.acknowledged
    claim = await manager.store.claim_answered_execution_checkpoint(
        checkpoint.checkpoint_id,
        project_id=proposal.project_id,
        checkpoint_type=checkpoint.checkpoint_type,
        consumer_id=consumer_id,
    )
    assert claim.acquired and claim.checkpoint is not None
    return InteractionDecisionLease(
        checkpoint=claim.checkpoint,
        decision=decision,
        consumer_id=consumer_id,
        claim_id=claim.claim_id,
    )


def test_applied_reorg_is_immutable_and_never_migrates_twice(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "tasks.db"
        store = OPCStore(database_path)
        competing_store = OPCStore(database_path)
        await store.initialize()
        await competing_store.initialize()
        try:
            manager, org_engine = _manager(store, tmp_path)
            competing_manager = ReorgManager(
                store=competing_store,
                org_engine=org_engine,
                communication=None,
                interaction_coordinator=InteractionCoordinator(
                    store=competing_store,
                    project_id="project-a",
                ),
            )
            proposal = await _proposal(manager)
            decision_lease = await _claim_reorg_decision(
                manager,
                proposal,
                approved=True,
            )
            approved = await manager.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                notes="Approved once.",
                interaction_lease=decision_lease,
            )
            duplicate_approval = await manager.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                notes="A duplicate request must not create another event.",
                interaction_lease=decision_lease,
            )
            assert approved.status == ReorgProposalStatus.APPROVED
            assert duplicate_approval.status == ReorgProposalStatus.APPROVED

            version_before = org_engine.current_org_version()
            apply_calls = 0
            original_apply_changeset = org_engine.apply_changeset

            def counted_apply_changeset(*args, **kwargs):
                nonlocal apply_calls
                apply_calls += 1
                return original_apply_changeset(*args, **kwargs)

            org_engine.apply_changeset = counted_apply_changeset
            effect_started = asyncio.Event()
            release_winner = asyncio.Event()
            original_migrate = manager._migrate_active_state

            async def blocked_migrate(*args, **kwargs):
                effect_started.set()
                await release_winner.wait()
                return await original_migrate(*args, **kwargs)

            manager._migrate_active_state = blocked_migrate
            winner = asyncio.create_task(manager.apply_reorg(proposal.proposal_id))
            await effect_started.wait()
            with pytest.raises(RuntimeError, match="will not be replayed"):
                await competing_manager.apply_reorg(proposal.proposal_id)
            release_winner.set()
            first = await winner
            version_after = org_engine.current_org_version()
            assert apply_calls == 1
            assert version_after == version_before + 1

            # Once the winner has durably finished, the other controller must
            # return the persisted receipt without replaying any side effects.
            restart_duplicate = await competing_manager.apply_reorg(
                proposal.proposal_id
            )
            assert restart_duplicate == first
            assert apply_calls == 1
            assert org_engine.current_org_version() == version_after

            persisted = await store.get_reorg_proposal(proposal.proposal_id)
            assert persisted is not None
            assert persisted.status == ReorgProposalStatus.APPLIED
            assert persisted.metadata["apply_result"] == first
            events = await store.list_reorg_events(
                "project-a",
                proposal_id=proposal.proposal_id,
            )
            assert sum(
                event.event_kind == ReorgEventKind.APPROVED for event in events
            ) == 1
            assert sum(
                event.event_kind == ReorgEventKind.APPLIED for event in events
            ) == 1
            with pytest.raises(ValueError, match="immutable"):
                await manager.set_reorg_approval(
                    proposal.proposal_id,
                        approved=False,
                        notes="Cannot reverse an applied decision.",
                        interaction_lease=decision_lease,
                )
            with pytest.raises(ValueError, match="immutable"):
                await manager.set_reorg_approval(
                    proposal.proposal_id,
                        approved=True,
                        notes="Cannot reopen an applied decision.",
                        interaction_lease=decision_lease,
                )
        finally:
            await competing_store.close()
            await store.close()

    asyncio.run(scenario())


def test_reorg_decision_is_immutable_across_store_connections(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "tasks.db"
        store = OPCStore(database_path)
        competing_store = OPCStore(database_path)
        await store.initialize()
        await competing_store.initialize()
        try:
            manager, org_engine = _manager(store, tmp_path)
            competing_manager = ReorgManager(
                store=competing_store,
                org_engine=org_engine,
                communication=None,
                interaction_coordinator=InteractionCoordinator(
                    store=competing_store,
                    project_id="project-a",
                ),
            )
            proposal = await _proposal(manager)
            checkpoint = next(
                row
                for row in await store.get_execution_checkpoints(
                    project_id="project-a",
                    checkpoint_types=["company_reorg_pending"],
                    statuses=["pending"],
                )
                if row.payload.get("proposal_id") == proposal.proposal_id
            )
            coordinators = [
                manager.interaction_coordinator,
                competing_manager.interaction_coordinator,
            ]
            assert all(coordinators)
            decisions = [
                {"option_id": "approve"},
                {"option_id": "deny"},
            ]
            receipts = await asyncio.gather(
                *(
                    coordinator.submit(
                        checkpoint_id=checkpoint.checkpoint_id,
                        checkpoint_type=checkpoint.checkpoint_type,
                        decision=decision,
                        client_request_id=f"decision-{index}",
                        checkpoint=checkpoint,
                    )
                    for index, (coordinator, decision) in enumerate(
                        zip(coordinators, decisions)
                    )
                )
            )
            assert sum(receipt.acknowledged for receipt in receipts) == 1
            winner_index = next(
                index for index, receipt in enumerate(receipts) if receipt.acknowledged
            )
            winner_manager = [manager, competing_manager][winner_index]
            winner_store = [store, competing_store][winner_index]
            winner_decision = decisions[winner_index]
            claim = await winner_store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_reorg_pending",
                consumer_id=f"winner-{winner_index}",
            )
            assert claim.acquired and claim.checkpoint is not None
            lease = InteractionDecisionLease(
                checkpoint=claim.checkpoint,
                decision=winner_decision,
                consumer_id=f"winner-{winner_index}",
                claim_id=claim.claim_id,
            )
            winner = await winner_manager.set_reorg_approval(
                proposal.proposal_id,
                approved=winner_index == 0,
                notes="Winning durable owner decision.",
                interaction_lease=lease,
            )

            persisted = await competing_store.get_reorg_proposal(
                proposal.proposal_id
            )
            assert persisted is not None
            assert persisted.status == winner.status
            assert persisted.status in {
                ReorgProposalStatus.APPROVED,
                ReorgProposalStatus.DENIED,
            }
            events = await store.list_reorg_events(
                proposal.project_id,
                proposal_id=proposal.proposal_id,
            )
            decision_events = [
                event
                for event in events
                if event.event_kind
                in {ReorgEventKind.APPROVED, ReorgEventKind.DENIED}
            ]
            assert len(decision_events) == 1
            assert decision_events[0].event_kind.value == persisted.status.value
        finally:
            await competing_store.close()
            await store.close()

    asyncio.run(scenario())


def test_reorg_application_failure_settles_without_unsafe_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            manager, org_engine = _manager(store, tmp_path)

            before_effect = await _proposal(manager)
            before_lease = await _claim_reorg_decision(
                manager, before_effect, approved=True
            )
            await manager.set_reorg_approval(
                before_effect.proposal_id,
                True,
                interaction_lease=before_lease,
            )
            original_snapshot = manager.build_org_snapshot

            async def fail_before_effect(_project_id: str):
                raise RuntimeError("snapshot failed")

            manager.build_org_snapshot = fail_before_effect
            with pytest.raises(RuntimeError, match="snapshot failed"):
                await manager.apply_reorg(before_effect.proposal_id)
            failed = await store.get_reorg_proposal(before_effect.proposal_id)
            assert failed is not None
            assert failed.status == ReorgProposalStatus.FAILED
            with pytest.raises(ValueError, match="must be approved"):
                await manager.apply_reorg(before_effect.proposal_id)

            manager.build_org_snapshot = original_snapshot
            after_effect = await _proposal(manager)
            after_lease = await _claim_reorg_decision(
                manager, after_effect, approved=True, consumer_id="after-controller"
            )
            await manager.set_reorg_approval(
                after_effect.proposal_id,
                True,
                interaction_lease=after_lease,
            )
            version_before = org_engine.current_org_version()
            migration_calls = 0

            async def fail_after_effect(*_args, **_kwargs):
                nonlocal migration_calls
                migration_calls += 1
                raise RuntimeError("migration outcome is uncertain")

            manager._migrate_active_state = fail_after_effect
            with pytest.raises(RuntimeError, match="outcome is uncertain"):
                await manager.apply_reorg(after_effect.proposal_id)
            uncertain = await store.get_reorg_proposal(after_effect.proposal_id)
            assert uncertain is not None
            assert uncertain.status == ReorgProposalStatus.EXECUTION_UNKNOWN
            assert org_engine.current_org_version() == version_before + 1
            with pytest.raises(ValueError, match="must be approved"):
                await manager.apply_reorg(after_effect.proposal_id)
            assert migration_calls == 1
            assert org_engine.current_org_version() == version_before + 1
        finally:
            await store.close()

    asyncio.run(scenario())


def test_denied_and_unreviewed_reorgs_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            manager, _org_engine = _manager(store, tmp_path)
            proposal = await _proposal(manager)
            direct = await store.decide_reorg_proposal(
                proposal.proposal_id,
                approved=True,
                notes="Must not bypass the owner card.",
            )
            assert direct.outcome == "conflict"
            with pytest.raises(ValueError, match="claimed owner interaction"):
                await manager.set_reorg_approval(
                    proposal.proposal_id,
                    approved=True,
                    notes="Missing interaction lease.",
                )
            with pytest.raises(ValueError, match="must be approved"):
                await manager.apply_reorg(proposal.proposal_id)

            decision_lease = await _claim_reorg_decision(
                manager,
                proposal,
                approved=False,
            )
            denied = await manager.set_reorg_approval(
                proposal.proposal_id,
                approved=False,
                notes="Denied once.",
                interaction_lease=decision_lease,
            )
            duplicate_denial = await manager.set_reorg_approval(
                proposal.proposal_id,
                approved=False,
                notes="Duplicate denial.",
                interaction_lease=decision_lease,
            )
            assert denied.status == ReorgProposalStatus.DENIED
            assert duplicate_denial.status == ReorgProposalStatus.DENIED
            with pytest.raises(ValueError, match="immutable"):
                await manager.set_reorg_approval(
                    proposal.proposal_id,
                    approved=True,
                    notes="Cannot reverse denial.",
                    interaction_lease=decision_lease,
                )
            with pytest.raises(ValueError, match="must be approved"):
                await manager.apply_reorg(proposal.proposal_id)

            events = await store.list_reorg_events(
                "project-a",
                proposal_id=proposal.proposal_id,
            )
            assert sum(
                event.event_kind == ReorgEventKind.DENIED for event in events
            ) == 1
            assert not any(
                event.event_kind == ReorgEventKind.APPLIED for event in events
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_applied_reorg_continuation_is_fenced_before_company_dispatch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database_path = tmp_path / "tasks.db"
        store = OPCStore(database_path)
        competing_store = OPCStore(database_path)
        await store.initialize()
        await competing_store.initialize()
        try:
            manager, org_engine = _manager(store, tmp_path)
            waiting_task = Task(
                session_id="reorg-child-session",
                parent_session_id="reorg-root-session",
                title="Resume after reorg",
                project_id="project-a",
                assigned_to="coordinator",
                status=TaskStatus.AWAITING_HUMAN,
                metadata={
                    "company_work_item_plan": {
                        "profile": "corporate",
                        "metadata": {"original_request": "Resume the company"},
                    },
                    "execution_task_ids": [],
                },
            )
            waiting_task.metadata["execution_task_ids"] = [waiting_task.id]
            await store.save_task(waiting_task)
            proposal = await manager.propose_reorg(
                project_id="project-a",
                session_id=waiting_task.session_id,
                task_id=waiting_task.id,
                summary="Add a continuation reviewer.",
                source_role_id="coordinator",
                changeset=ReorgChangeSet(
                    role_changes=[
                        ReorgRoleChange(
                            action="add",
                            role={
                                "id": "continuation_reviewer",
                                "name": "Continuation Reviewer",
                                "responsibility": "Review resumed work.",
                            },
                        )
                    ]
                ),
            )
            coordinator = manager.interaction_coordinator
            assert coordinator is not None
            checkpoint = next(
                row
                for row in await store.get_execution_checkpoints(
                    project_id="project-a",
                    checkpoint_types=["company_reorg_pending"],
                    statuses=["pending"],
                )
                if row.payload.get("proposal_id") == proposal.proposal_id
            )
            decision = {"option_id": "approve"}
            accepted = await coordinator.submit(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type=checkpoint.checkpoint_type,
                decision=decision,
                client_request_id="reorg-continuation-decision",
                checkpoint=checkpoint,
            )
            assert accepted.acknowledged
            claimed_at = datetime.now()
            claim = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_reorg_pending",
                consumer_id="controller-a",
                claim_id="claim-a",
                lease_seconds=30,
                claimed_at=claimed_at,
            )
            assert claim.acquired and claim.checkpoint is not None
            lease = InteractionDecisionLease(
                checkpoint=claim.checkpoint,
                decision=decision,
                consumer_id="controller-a",
                claim_id="claim-a",
            )
            await manager.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                interaction_lease=lease,
            )
            await manager.apply_reorg(proposal.proposal_id)

            execute = AsyncMock(return_value="company resumed")
            engine = OPCEngine.__new__(OPCEngine)
            engine.store = store
            engine.reorg_manager = manager
            engine.interaction_coordinator = coordinator
            engine.org_engine = org_engine
            engine.company_executor = SimpleNamespace(execute=execute)
            current_task = await store.get_task(waiting_task.id)
            assert current_task is not None
            engine._reconcile_company_work_item_plan_state = AsyncMock(  # type: ignore[method-assign]
                return_value=(
                    CompanyWorkItemRuntimePlan(profile="corporate"),
                    [current_task],
                )
            )

            result = await engine._resume_reorg_checkpoint(lease, "approve")
            assert "company resumed" in result
            execute.assert_awaited_once()
            executing = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_reorg_pending",
            )
            assert executing is not None
            assert executing.status == "consuming"
            assert executing.payload["interaction"]["execution"]["state"] == "executing"

            # Simulate a process death after the resumed dispatcher returned
            # but before the outer consumer could finish the card.  A second
            # controller may not replay that non-idempotent dispatcher.
            reclaimed = await competing_store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_reorg_pending",
                consumer_id="controller-b",
                claim_id="claim-b",
                claimed_at=claimed_at + timedelta(seconds=31),
            )
            assert reclaimed.outcome == "invalid_state"
            assert reclaimed.checkpoint is not None
            assert reclaimed.checkpoint.status == "outcome_unknown"
            execute.assert_awaited_once()
        finally:
            await competing_store.close()
            await store.close()

    asyncio.run(scenario())
