from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer0_interaction.coordinator import InteractionCoordinator


async def _publish_delivery_review(
    store: OPCStore,
    *,
    checkpoint_id: str = "delivery-review",
    task_id: str = "delivery-task",
) -> ExecutionCheckpoint:
    checkpoint = ExecutionCheckpoint(
        checkpoint_id=checkpoint_id,
        project_id="project-a",
        session_id="root-session:delivery",
        checkpoint_type="company_delivery_feedback",
        task_id=task_id,
        payload={
            "waiting_task_id": task_id,
            "delivery_revision": 7,
            "basis_hash": "delivery-basis-7",
            "interaction": {
                "kind": "company_delivery_feedback",
                "domain_key": "delivery:root-session:revision-7",
                "ownership": {
                    "waiting_task_id": task_id,
                    "waiting_session_id": "root-session:delivery",
                    "root_session_id": "root-session",
                },
            },
        },
    )
    coordinator = InteractionCoordinator(store=store, project_id="project-a")
    persisted, created = await coordinator.publish_owner_checkpoint(
        checkpoint,
        interaction_key="delivery:root-session:revision-7",
        supersession_key="delivery:root-session",
        supersession_order=(7,),
    )
    assert created is True
    return persisted


def test_delivery_supersession_rolls_back_both_rows_then_replays_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            task = Task(
                id="delivery-task",
                project_id="project-a",
                session_id="root-session:delivery",
                parent_session_id="root-session",
                status=TaskStatus.AWAITING_HUMAN,
                metadata={
                    "execution_mode": "company_mode",
                    "feedback_scope": "final",
                    "requires_user_feedback": True,
                    "progress_log": ["Waiting for delivery review."],
                },
            )
            await store.save_task(task)
            checkpoint = await _publish_delivery_review(store)

            original_close = store._close_company_delivery_review_task_row

            async def fail_after_task_write(**kwargs):
                await original_close(**kwargs)
                raise RuntimeError("simulated crash after Task write")

            with monkeypatch.context() as patch:
                patch.setattr(
                    store,
                    "_close_company_delivery_review_task_row",
                    fail_after_task_write,
                )
                with pytest.raises(
                    RuntimeError,
                    match="simulated crash after Task write",
                ):
                    await store.supersede_pending_delivery_review_for_company_turn(
                        checkpoint.checkpoint_id,
                        project_id="project-a",
                        root_session_id="root-session",
                        conversation_turn_id="turn-8",
                    )

            # The injected failure happened after the Task UPSERT, but both
            # durable rows remain at their pre-turn state after rollback.
            rolled_back_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_delivery_feedback",
            )
            assert rolled_back_checkpoint is not None
            assert rolled_back_checkpoint.status == "pending"
            assert "superseded_by_company_turn" not in rolled_back_checkpoint.payload
            rolled_back_task = await store.get_task(task.id)
            assert rolled_back_task is not None
            assert rolled_back_task.status == TaskStatus.AWAITING_HUMAN
            assert rolled_back_task.metadata["requires_user_feedback"] is True
            assert rolled_back_task.metadata["progress_log"] == [
                "Waiting for delivery review."
            ]
            assert "feedback_superseded_by_turn_id" not in rolled_back_task.metadata

            applied = (
                await store.supersede_pending_delivery_review_for_company_turn(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    root_session_id="root-session",
                    conversation_turn_id="turn-8",
                )
            )
            assert applied.outcome == "applied"
            assert applied.applied is True
            assert applied.checkpoint is not None
            assert applied.checkpoint.status == "superseded"
            assert applied.checkpoint.payload["superseded_by_company_turn"] == {
                "conversation_turn_id": "turn-8",
                "root_session_id": "root-session",
                "prior_delivery_revision": 7,
                "prior_basis_hash": "delivery-basis-7",
            }
            assert applied.task is not None
            assert applied.task.status == TaskStatus.DONE
            assert applied.task.metadata["feedback_superseded_by_turn_id"] == "turn-8"
            assert applied.task.metadata["progress_log"] == [
                "Waiting for delivery review.",
                "Delivery human review closed: new_company_turn_started.",
            ]

            duplicate = (
                await store.supersede_pending_delivery_review_for_company_turn(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    root_session_id="root-session",
                    conversation_turn_id="turn-8",
                )
            )
            assert duplicate.outcome == "duplicate"
            assert duplicate.acknowledged is True
            replayed_task = await store.get_task(task.id)
            assert replayed_task is not None
            assert replayed_task.metadata["progress_log"] == (
                applied.task.metadata["progress_log"]
            )

            stale_turn = (
                await store.supersede_pending_delivery_review_for_company_turn(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    root_session_id="root-session",
                    conversation_turn_id="turn-9",
                )
            )
            assert stale_turn.outcome == "invalid_state"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_delivery_supersession_scope_mismatch_never_closes_task(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            task = Task(
                id="delivery-task",
                project_id="project-a",
                session_id="root-session:delivery",
                parent_session_id="root-session",
                status=TaskStatus.AWAITING_HUMAN,
                metadata={"requires_user_feedback": True},
            )
            await store.save_task(task)
            checkpoint = await _publish_delivery_review(store)

            receipt = (
                await store.supersede_pending_delivery_review_for_company_turn(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    root_session_id="different-root",
                    conversation_turn_id="turn-8",
                )
            )
            assert receipt.outcome == "scope_mismatch"
            persisted_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_delivery_feedback",
            )
            persisted_task = await store.get_task(task.id)
            assert persisted_checkpoint is not None
            assert persisted_checkpoint.status == "pending"
            assert persisted_task is not None
            assert persisted_task.status == TaskStatus.AWAITING_HUMAN
            assert persisted_task.metadata["requires_user_feedback"] is True
        finally:
            await store.close()

    asyncio.run(scenario())
