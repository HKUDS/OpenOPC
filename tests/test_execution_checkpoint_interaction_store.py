from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer0_interaction.coordinator import InteractionCoordinator


def _checkpoint(checkpoint_id: str = "interaction-1") -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        checkpoint_id=checkpoint_id,
        project_id="project-a",
        session_id="runtime-session",
        checkpoint_type="tool_permission",
        task_id="worker-task",
        payload={
            "schema_version": 2,
            "tool_call": {"tool_call_id": "call-1", "name": "shell_exec"},
            "ownership": {"ui_anchor_task_id": "leader-task"},
            "interaction": {"domain_key": f"tool-call:{checkpoint_id}"},
        },
    )


async def _publish(
    store: OPCStore,
    checkpoint: ExecutionCheckpoint,
):
    interaction = dict(checkpoint.payload.get("interaction", {}) or {})
    domain_key = str(interaction.get("domain_key", "") or "").strip()
    assert domain_key
    return await store.create_owner_interaction_checkpoint(
        checkpoint,
        interaction_key=domain_key,
    )


async def _accept(
    store: OPCStore,
    checkpoint_id: str = "interaction-1",
    *,
    request_id: str = "request-1",
    decision_hash: str = "hash-approve-once",
    decision: object = None,
):
    return await store.accept_execution_checkpoint_decision(
        checkpoint_id,
        project_id="project-a",
        checkpoint_type="tool_permission",
        request_id=request_id,
        decision_hash=decision_hash,
        decision={"action": "approve_once"} if decision is None else decision,
    )


def test_concurrent_checkpoint_create_has_one_winner_without_replacement(
    tmp_path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            candidates = [_checkpoint(), _checkpoint()]
            candidates[0].payload["creator"] = "office"
            candidates[1].payload["creator"] = "cli"
            start = asyncio.Event()

            async def create(store: OPCStore, checkpoint: ExecutionCheckpoint):
                await start.wait()
                return await _publish(store, checkpoint)

            attempts = [
                asyncio.create_task(create(first_store, candidates[0])),
                asyncio.create_task(create(second_store, candidates[1])),
            ]
            start.set()
            results = await asyncio.gather(*attempts)

            created_flags = [created for _, created in results]
            assert created_flags.count(True) == 1
            assert created_flags.count(False) == 1
            winner = created_flags.index(True)
            expected_creator = candidates[winner].payload["creator"]
            assert {row.payload["creator"] for row, _ in results} == {expected_creator}
            persisted = await first_store.get_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None
            assert persisted.payload["creator"] == expected_creator
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_atomic_owner_publish_keeps_exactly_one_deterministic_latest_revision(
    tmp_path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            older = ExecutionCheckpoint(
                checkpoint_id="owner-revision-1",
                project_id="project-a",
                session_id="root-session",
                checkpoint_type="task_user_input",
                task_id="worker-task",
                payload={
                    "basis_hash": "basis-1",
                    "prompt": "First question",
                    "interaction": {"domain_key": "domain-1"},
                },
                created_at=datetime(2026, 1, 1, 0, 0, 0),
                updated_at=datetime(2026, 1, 1, 0, 0, 0),
            )
            newer = ExecutionCheckpoint(
                checkpoint_id="owner-revision-2",
                project_id="project-a",
                session_id="root-session",
                checkpoint_type="task_user_input",
                task_id="worker-task",
                payload={
                    "basis_hash": "basis-2",
                    "prompt": "Second question",
                    "interaction": {"domain_key": "domain-2"},
                },
                created_at=datetime(2026, 1, 1, 0, 0, 1),
                updated_at=datetime(2026, 1, 1, 0, 0, 1),
            )
            start = asyncio.Event()

            async def publish(
                store: OPCStore,
                checkpoint: ExecutionCheckpoint,
                domain_key: str,
                order: int,
            ):
                await start.wait()
                return await store.publish_owner_interaction_checkpoint(
                    checkpoint,
                    interaction_key=domain_key,
                    supersession_key="worker-input-flow",
                    supersession_order=[1, order],
                )

            attempts = [
                asyncio.create_task(publish(first_store, older, "domain-1", 1)),
                asyncio.create_task(publish(second_store, newer, "domain-2", 2)),
            ]
            start.set()
            await asyncio.gather(*attempts)

            rows = await first_store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["task_user_input"],
            )
            assert {row.checkpoint_id for row in rows} == {
                "owner-revision-1",
                "owner-revision-2",
            }
            assert {
                row.checkpoint_id for row in rows if row.status == "pending"
            } == {"owner-revision-2"}
            superseded = next(
                row for row in rows if row.checkpoint_id == "owner-revision-1"
            )
            assert superseded.status == "superseded"
            assert (
                superseded.payload["superseded_by_checkpoint_id"]
                == "owner-revision-2"
            )
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_owner_source_sequence_is_durable_and_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first = OPCStore(db_path)
        second = OPCStore(db_path)
        await first.initialize()
        await second.initialize()
        try:
            first_sequence = await first.allocate_owner_interaction_source_sequence(
                project_id="project-a",
                scope_id="root-session",
                source_event_id="turn-1",
            )
            retry_sequence = await second.allocate_owner_interaction_source_sequence(
                project_id="project-a",
                scope_id="root-session",
                source_event_id="turn-1",
            )
            next_sequence = await second.allocate_owner_interaction_source_sequence(
                project_id="project-a",
                scope_id="root-session",
                source_event_id="turn-2",
            )
            assert (first_sequence, retry_sequence, next_sequence) == (1, 1, 2)
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_late_old_revision_cannot_replace_newer_owner_interaction(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            def recruitment(revision: int) -> ExecutionCheckpoint:
                return ExecutionCheckpoint(
                    checkpoint_id=f"recruitment-{revision}",
                    project_id="project-a",
                    session_id="root-session",
                    checkpoint_type="company_recruitment_confirmation",
                    payload={
                        "recruitment_revision": revision,
                        "interaction": {
                            "domain_key": f"recruitment-event-{revision}",
                        },
                    },
                )

            await store.publish_owner_interaction_checkpoint(
                recruitment(2),
                interaction_key="recruitment-event-2",
                supersession_key="recruitment-flow",
                supersession_order=[7, 2],
            )
            # Revision 1 is first observed only after revision 2 is durable.
            # Producer time/UUID must not let this stale publication win.
            await store.publish_owner_interaction_checkpoint(
                recruitment(1),
                interaction_key="recruitment-event-1",
                supersession_key="recruitment-flow",
                supersession_order=[7, 1],
            )
            rows = await store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["company_recruitment_confirmation"],
            )
            assert {
                row.checkpoint_id for row in rows if row.status == "pending"
            } == {"recruitment-2"}
            stale = next(row for row in rows if row.checkpoint_id == "recruitment-1")
            assert stale.status == "superseded"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_taskless_owner_supersession_uses_explicit_flow_key(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            for revision in (1, 2):
                checkpoint = ExecutionCheckpoint(
                    checkpoint_id=f"taskless-recruitment-{revision}",
                    project_id="project-a",
                    session_id="root-session",
                    checkpoint_type="company_recruitment_confirmation",
                    task_id=None,
                    payload={
                        "recruitment_revision": revision,
                        "interaction": {
                            "domain_key": f"taskless-domain-{revision}",
                        },
                    },
                )
                await store.publish_owner_interaction_checkpoint(
                    checkpoint,
                    interaction_key=f"taskless-domain-{revision}",
                    supersession_key="root-recruitment-flow",
                    supersession_order=[3, revision],
                )
            rows = await store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["company_recruitment_confirmation"],
            )
            assert {
                row.checkpoint_id for row in rows if row.status == "pending"
            } == {"taskless-recruitment-2"}
        finally:
            await store.close()

    asyncio.run(scenario())


def test_checkpoint_create_never_overwrites_existing_answer(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            original = _checkpoint()
            original.payload["identity"] = {"tool_call_id": "call-1"}
            row, created = await _publish(store, original)
            assert created is True
            assert row.status == "pending"
            assert (await _accept(store)).outcome == "accepted"

            replacement = _checkpoint()
            replacement.payload = {
                "identity": {"tool_call_id": "different-call"},
                "destructive_replacement": True,
                "interaction": {"domain_key": "tool-call:interaction-1"},
            }
            existing, created = await _publish(store, replacement)

            assert created is False
            assert existing.status == "answered"
            assert existing.payload["identity"] == {"tool_call_id": "call-1"}
            assert "destructive_replacement" not in existing.payload
            assert (
                existing.payload["interaction"]["decision"]["request_id"] == "request-1"
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_decision_accept_is_scoped_durable_idempotent_and_immutable(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            await _publish(store, _checkpoint())

            assert (
                await store.get_execution_checkpoint(
                    "interaction-1",
                    project_id="other-project",
                )
                is None
            )
            wrong_type = await store.accept_execution_checkpoint_decision(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="task_user_input",
                request_id="request-1",
                decision_hash="hash-approve-once",
                decision={"action": "approve_once"},
            )
            assert wrong_type.outcome == "not_found"

            accepted = await _accept(store)
            assert accepted.outcome == "accepted"
            assert accepted.acknowledged is True
            assert accepted.checkpoint is not None
            assert accepted.checkpoint.status == "answered"
            assert accepted.checkpoint.payload["tool_call"]["tool_call_id"] == "call-1"
            assert accepted.checkpoint.payload["interaction"]["decision"]["value"] == {
                "action": "approve_once"
            }

            duplicate = await _accept(store)
            assert duplicate.outcome == "duplicate"
            assert duplicate.acknowledged is True

            changed_value = await _accept(
                store,
                decision={"action": "deny"},
            )
            assert changed_value.outcome == "conflict"
            assert changed_value.acknowledged is False
            changed_request = await _accept(
                store,
                request_id="request-2",
            )
            assert changed_request.outcome == "duplicate"
            assert changed_request.acknowledged is True
            changed_hash = await _accept(
                store,
                request_id="request-3",
                decision_hash="different-hash",
            )
            assert changed_hash.outcome == "conflict"

            persisted = await store.get_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None
            assert persisted.status == "answered"
            assert (
                persisted.payload["interaction"]["decision"]["request_id"]
                == "request-1"
            )
            assert persisted.payload["interaction"]["decision"]["value"] == {
                "action": "approve_once"
            }
        finally:
            await store.close()

    asyncio.run(scenario())


def test_task_user_input_accept_is_linearized_with_task_settlement(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        submitter = OPCStore(db_path)
        settler = OPCStore(db_path)
        await submitter.initialize()
        await settler.initialize()
        try:
            settled_task = Task(
                title="Already settled",
                project_id="project-a",
                status=TaskStatus.AWAITING_HUMAN,
            )
            await settler.save_task(settled_task)
            settled_checkpoint = ExecutionCheckpoint(
                checkpoint_id="settled-task-input",
                project_id="project-a",
                session_id="runtime-session",
                checkpoint_type="task_user_input",
                task_id=settled_task.id,
                payload={
                    "waiting_task_id": settled_task.id,
                    "interaction": {"domain_key": "settled-task-input"},
                },
            )
            await _publish(submitter, settled_checkpoint)

            settled_task.status = TaskStatus.DONE
            await settler.save_task(settled_task)
            rejected = await submitter.accept_execution_checkpoint_decision(
                settled_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="task_user_input",
                request_id="late-answer",
                decision_hash="late-answer-hash",
                decision={"text": "continue"},
            )
            assert rejected.outcome == "invalid_state"
            assert rejected.checkpoint is not None
            assert rejected.checkpoint.status == "stale"
            assert rejected.checkpoint.payload["stale_reason"] == (
                f"task {settled_task.id} settled as done"
            )
            assert "decision" not in rejected.checkpoint.payload["interaction"]

            live_task = Task(
                title="Answer wins",
                project_id="project-a",
                status=TaskStatus.AWAITING_HUMAN,
            )
            await settler.save_task(live_task)
            live_checkpoint = ExecutionCheckpoint(
                checkpoint_id="live-task-input",
                project_id="project-a",
                session_id="runtime-session",
                checkpoint_type="task_user_input",
                task_id=live_task.id,
                payload={
                    "waiting_task_id": live_task.id,
                    "interaction": {"domain_key": "live-task-input"},
                },
            )
            await _publish(submitter, live_checkpoint)
            accepted = await submitter.accept_execution_checkpoint_decision(
                live_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="task_user_input",
                request_id="on-time-answer",
                decision_hash="on-time-answer-hash",
                decision={"text": "continue"},
            )
            assert accepted.outcome == "accepted"
            live_task.status = TaskStatus.DONE
            await settler.save_task(live_task)
            persisted = await submitter.get_execution_checkpoint(
                live_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="task_user_input",
            )
            assert persisted is not None
            assert persisted.status == "answered"
            assert persisted.payload["interaction"]["decision"]["value"] == {
                "text": "continue"
            }
        finally:
            await settler.close()
            await submitter.close()

    asyncio.run(scenario())


def test_concurrent_decisions_have_exactly_one_winner(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            await _publish(first_store, _checkpoint())
            start = asyncio.Event()

            async def submit(
                store: OPCStore,
                request_id: str,
                action: str,
            ):
                await start.wait()
                return await _accept(
                    store,
                    request_id=request_id,
                    decision_hash=f"hash-{action}",
                    decision={"action": action},
                )

            attempts = [
                asyncio.create_task(
                    submit(first_store, "approve-request", "approve_once")
                ),
                asyncio.create_task(submit(second_store, "deny-request", "deny")),
            ]
            start.set()
            receipts = await asyncio.gather(*attempts)

            assert [receipt.outcome for receipt in receipts].count("accepted") == 1
            assert [receipt.outcome for receipt in receipts].count("conflict") == 1
            winner = receipts.index(
                next(item for item in receipts if item.outcome == "accepted")
            )
            persisted = await first_store.get_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
            )
            assert persisted is not None
            assert (
                persisted.payload["interaction"]["decision"]["value"]
                == [
                    {"action": "approve_once"},
                    {"action": "deny"},
                ][winner]
            )
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_claim_lease_recovery_and_finish_preserve_latest_payload(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            await _publish(store, _checkpoint())
            assert (await _accept(store)).outcome == "accepted"
            started = datetime(2026, 8, 11, 10, 0, 0)

            first = await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
                lease_seconds=10,
                claimed_at=started,
            )
            assert first.outcome == "claimed"
            assert first.acquired is True
            assert first.checkpoint is not None
            assert first.checkpoint.status == "consuming"

            duplicate = await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
                lease_seconds=10,
                claimed_at=started + timedelta(seconds=1),
            )
            assert duplicate.outcome == "duplicate"

            busy = await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-b",
                claim_id="claim-b",
                lease_seconds=10,
                claimed_at=started + timedelta(seconds=2),
            )
            assert busy.outcome == "busy"
            assert busy.claim_id == "claim-a"

            reclaimed = await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-b",
                claim_id="claim-b",
                lease_seconds=10,
                claimed_at=started + timedelta(seconds=11),
            )
            assert reclaimed.outcome == "reclaimed"
            assert reclaimed.checkpoint is not None
            claim = reclaimed.checkpoint.payload["interaction"]["claim"]
            assert claim["attempt"] == 2
            assert claim["reclaimed_from_claim_id"] == "claim-a"

            stale_finish = await store.finish_execution_checkpoint_consumption(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
            )
            assert stale_finish.outcome == "conflict"

            # A domain writer can add metadata between claim and completion;
            # finish must merge its patch into the latest stored payload.
            current = await store.get_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
            )
            assert current is not None
            patched, applied = await coordinator.enrich_owner_checkpoint(
                "interaction-1",
                checkpoint_type="tool_permission",
                expected_statuses={"consuming"},
                payload_patch={"runtime_v2": {"stdout": "cloned"}},
            )
            assert applied is True
            assert patched is not None

            finished = await store.finish_execution_checkpoint_consumption(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-b",
                claim_id="claim-b",
                payload_patch={"tool_call": {"result_status": "succeeded"}},
            )
            assert finished.outcome == "finished"
            assert finished.applied is True
            assert finished.checkpoint is not None
            assert finished.checkpoint.status == "resolved"
            assert finished.checkpoint.payload["runtime_v2"] == {"stdout": "cloned"}
            assert finished.checkpoint.payload["tool_call"] == {
                "tool_call_id": "call-1",
                "name": "shell_exec",
                "result_status": "succeeded",
            }
            assert (
                finished.checkpoint.payload["interaction"]["decision"]["request_id"]
                == "request-1"
            )

            duplicate_finish = await store.finish_execution_checkpoint_consumption(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-b",
                claim_id="claim-b",
            )
            assert duplicate_finish.outcome == "duplicate"
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_release_returns_answer_to_queue_and_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            await _publish(store, _checkpoint())
            await _accept(store)
            await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
            )

            released = await store.release_execution_checkpoint_claim(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
                reason="controller shutdown",
                payload_patch={"recovery": {"retriable": True}},
            )
            assert released.outcome == "released"
            assert released.checkpoint is not None
            assert released.checkpoint.status == "answered"
            assert "claim" not in released.checkpoint.payload["interaction"]
            assert (
                released.checkpoint.payload["interaction"]["last_release"]["reason"]
                == "controller shutdown"
            )

            duplicate = await store.release_execution_checkpoint_claim(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
            )
            assert duplicate.outcome == "duplicate"

            next_claim = await store.claim_answered_execution_checkpoint(
                "interaction-1",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-b",
                claim_id="claim-b",
            )
            assert next_claim.outcome == "claimed"
            assert next_claim.checkpoint is not None
            assert next_claim.checkpoint.payload["recovery"] == {"retriable": True}
        finally:
            await store.close()

    asyncio.run(scenario())


def test_generic_supersede_rejects_owner_interactions(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            answered = _checkpoint("answered")
            consuming = _checkpoint("consuming")
            pending = _checkpoint("pending")
            await _publish(store, answered)
            await _publish(store, consuming)
            await _publish(store, pending)
            await _accept(store, "answered", request_id="answer-request")
            await _accept(store, "consuming", request_id="consume-request")
            await store.claim_answered_execution_checkpoint(
                "consuming",
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
            )

            with pytest.raises(ValueError, match="owner interaction"):
                await store.supersede_pending_checkpoints(
                    project_id="project-a",
                    session_id="runtime-session",
                    checkpoint_types=["tool_permission"],
                    exclude_checkpoint_id="new-turn",
                )
            rows = await store.get_execution_checkpoints(project_id="project-a")
            statuses = {row.checkpoint_id: row.status for row in rows}
            assert statuses == {
                "answered": "answered",
                "consuming": "consuming",
                "pending": "pending",
            }
        finally:
            await store.close()

    asyncio.run(scenario())


def test_generic_checkpoint_mutators_reject_owner_interactions(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            checkpoint = _checkpoint("owner-guard")

            with pytest.raises(RuntimeError, match="owner interactions must use"):
                await store.create_execution_checkpoint(checkpoint)
            with pytest.raises(RuntimeError, match="owner interactions must use"):
                await store.save_execution_checkpoint(checkpoint)

            persisted, created = await _publish(store, checkpoint)
            assert created is True

            disguised_non_owner = ExecutionCheckpoint(
                checkpoint_id=persisted.checkpoint_id,
                project_id="project-a",
                session_id="runtime-session",
                checkpoint_type="runtime_suspend",
                status="resolved",
                task_id="task-1",
                payload={"replacement": True},
            )
            with pytest.raises(RuntimeError, match="cannot overwrite an owner"):
                await store.save_execution_checkpoint(disguised_non_owner)

            with pytest.raises(RuntimeError, match="scoped decision/claim/finish"):
                await store.compare_and_set_execution_checkpoint(
                    persisted.checkpoint_id,
                    expected_statuses={"pending"},
                    status="resolved",
                    payload=dict(persisted.payload),
                )
            with pytest.raises(RuntimeError, match="runtime checkpoint completion"):
                await store.complete_execution_checkpoint_and_reopen_ui_anchor(
                    persisted.checkpoint_id,
                    project_id="project-a",
                    session_id="runtime-session",
                    expected_status="pending",
                    status="resolved",
                    payload=dict(persisted.payload),
                )

            current = await store.get_execution_checkpoint(
                persisted.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert current is not None
            assert current.status == "pending"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_startup_retires_only_active_legacy_human_escalations(tmp_path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        store = OPCStore(db_path)
        await store.initialize()
        try:
            active_statuses = ("pending", "answered", "consuming", "resuming")
            for status in active_statuses:
                await store.save_execution_checkpoint(
                    ExecutionCheckpoint(
                        checkpoint_id=f"legacy-{status}",
                        project_id="project-a",
                        session_id="legacy-session",
                        checkpoint_type="human_escalation",
                        status=status,
                        task_id="legacy-task",
                        payload={"prompt": f"legacy {status}"},
                    )
                )
            await store.save_execution_checkpoint(
                ExecutionCheckpoint(
                    checkpoint_id="legacy-resolved",
                    project_id="project-a",
                    session_id="legacy-session",
                    checkpoint_type="human_escalation",
                    status="resolved",
                    task_id="legacy-task",
                    payload={"history": "keep"},
                )
            )
            await store.save_execution_checkpoint(
                ExecutionCheckpoint(
                    checkpoint_id="peer-wait-pending",
                    project_id="project-a",
                    session_id="peer-session",
                    checkpoint_type="task_peer_wait",
                    status="pending",
                    task_id="peer-task",
                    payload={"unrelated": True},
                )
            )
        finally:
            await store.close()

        reopened = OPCStore(db_path)
        await reopened.initialize()
        try:
            rows = await reopened.get_execution_checkpoints(project_id="project-a")
            by_id = {row.checkpoint_id: row for row in rows}
            for status in active_statuses:
                migrated = by_id[f"legacy-{status}"]
                assert migrated.status == "stale"
                assert migrated.payload["stale_reason"] == (
                    "legacy human escalation protocol retired"
                )
                provenance = migrated.payload["legacy_interaction_migration"]
                assert provenance["source_checkpoint_type"] == "human_escalation"
                assert provenance["prior_status"] == status
                assert provenance["terminal_status"] == "stale"
                assert provenance["migrated_at"] == migrated.payload["stale_at"]
                assert "interaction" not in migrated.payload

            assert by_id["legacy-resolved"].status == "resolved"
            assert by_id["legacy-resolved"].payload == {"history": "keep"}
            assert by_id["peer-wait-pending"].status == "pending"
            assert by_id["peer-wait-pending"].payload == {"unrelated": True}
            pending = await reopened.get_pending_checkpoints(project_id="project-a")
            assert [row.checkpoint_id for row in pending] == ["peer-wait-pending"]
            migrated_updated_at = {
                status: by_id[f"legacy-{status}"].updated_at
                for status in active_statuses
            }
        finally:
            await reopened.close()

        reopened_again = OPCStore(db_path)
        await reopened_again.initialize()
        try:
            rows = await reopened_again.get_execution_checkpoints(project_id="project-a")
            by_id = {row.checkpoint_id: row for row in rows}
            for status in active_statuses:
                assert by_id[f"legacy-{status}"].updated_at == migrated_updated_at[status]
                assert (
                    by_id[f"legacy-{status}"].payload["legacy_interaction_migration"]
                    ["prior_status"]
                    == status
                )
        finally:
            await reopened_again.close()

    asyncio.run(scenario())


def test_owner_decision_primitives_reject_non_owner_checkpoints(tmp_path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="runtime-suspend-1",
                project_id="project-a",
                session_id="runtime-session",
                checkpoint_type="runtime_suspend",
                task_id="worker-task",
                payload={"interaction": {"domain_key": "must-not-be-used"}},
            )
            await store.create_execution_checkpoint(checkpoint)

            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await coordinator.open_and_wait(
                    checkpoint,
                    prompt="Continue?",
                    options=[{"id": "continue", "label": "Continue"}],
                    consumer_id="controller-a",
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.accept_execution_checkpoint_decision(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    request_id="request-1",
                    decision_hash="hash-1",
                    decision={"option_id": "continue"},
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.claim_answered_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    consumer_id="controller-a",
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.begin_execution_checkpoint_effect(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    claim_id="claim-1",
                    consumer_id="controller-a",
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.renew_execution_checkpoint_claim(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    claim_id="claim-1",
                    consumer_id="controller-a",
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.finish_execution_checkpoint_consumption(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    claim_id="claim-1",
                    consumer_id="controller-a",
                )
            with pytest.raises(ValueError, match="only accepts owner interaction"):
                await store.release_execution_checkpoint_claim(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="runtime_suspend",
                    claim_id="claim-1",
                    consumer_id="controller-a",
                )

            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="runtime_suspend",
            )
            assert persisted is not None
            assert persisted.status == "pending"
            assert "decision" not in dict(persisted.payload.get("interaction", {}))
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())
