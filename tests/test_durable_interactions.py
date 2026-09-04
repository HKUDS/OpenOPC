from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opc.core.config import AutonomyConfig, OPCConfig
from opc.core.events import EventBus
from opc.core.models import (
    ApprovalAction,
    ApprovalDecision,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExecutionCheckpointConsumptionReceipt,
    Phase,
    RiskLevel,
    Task,
    TaskStatus,
    UserMessage,
)
from opc.database.store import OPCStore, TaskRuntimeToolLedgerSnapshot
from opc.engine import OPCEngine
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)
from opc.llm.provider import LLMProvider
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry


def _fingerprint(call_id: str, name: str, arguments: dict[str, Any], runtime_id: str) -> str:
    return hashlib.sha256(json.dumps(
        {
            "tool_call_id": call_id,
            "tool_name": name,
            "arguments": arguments,
            "runtime_session_id": runtime_id,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


def _tool_checkpoint(
    *,
    checkpoint_id: str = "cp-tool",
    status: str = "pending",
    task_id: str = "worker",
    session_id: str = "worker-session",
    runtime_id: str = "rt-child",
    call_id: str = "call-1",
    name: str = "dangerous_tool",
    arguments: dict[str, Any] | None = None,
) -> ExecutionCheckpoint:
    arguments = dict(arguments or {"value": "original"})
    fingerprint = _fingerprint(call_id, name, arguments, runtime_id)
    return ExecutionCheckpoint(
        checkpoint_id=checkpoint_id,
        project_id="project-a",
        session_id=session_id,
        checkpoint_type="tool_permission",
        status=status,
        task_id=task_id,
        payload={
            "schema_version": 2,
            "interaction": {
                "kind": "tool_permission",
                "domain_key": f"tool_permission:{task_id}:{runtime_id}:{fingerprint}",
                "prompt": "Allow exact call?",
                "options": [
                    {"id": "approve_once", "label": "Approve once"},
                    {"id": "deny", "label": "Deny"},
                ],
                "ownership": {
                    "waiting_task_id": task_id,
                    "waiting_session_id": session_id,
                    "ui_anchor_task_id": "anchor",
                    "ui_anchor_session_id": "root-session",
                    "root_session_id": "root-session",
                    "company_runtime_session_id": "root-session",
                    "tool_runtime_session_id": runtime_id,
                    "execution_parent_task_id": "anchor",
                    "execution_parent_session_id": "root-session",
                },
            },
            "tool_call": {
                "id": call_id,
                "name": name,
                "arguments": arguments,
                "runtime_session_id": runtime_id,
                "fingerprint": fingerprint,
            },
            "approval": {
                "action_kind": "tool",
                "action_name": name,
                "allowlist_enabled": True,
                "allowlist_patterns": ["*"],
            },
        },
    )


async def _publish_owner_checkpoint(
    store: OPCStore,
    checkpoint: ExecutionCheckpoint,
) -> ExecutionCheckpoint:
    """Publish a test owner interaction through the production-only ingress."""

    interaction = dict(checkpoint.payload.get("interaction", {}) or {})
    domain_key = str(interaction.get("domain_key", "") or "").strip()
    if not domain_key:
        domain_key = (
            f"test:{checkpoint.checkpoint_type}:{checkpoint.checkpoint_id}"
        )
        interaction["domain_key"] = domain_key
        checkpoint.payload["interaction"] = interaction
    persisted, _ = await store.create_owner_interaction_checkpoint(
        checkpoint,
        interaction_key=domain_key,
    )
    return persisted


async def _claim_tool_permission_for_test(
    store: OPCStore,
    checkpoint: ExecutionCheckpoint,
    *,
    consumer_id: str,
    claim_id: str,
) -> dict[str, Any]:
    """Build the exact ready permit for a real claimed SQLite checkpoint."""

    await _publish_owner_checkpoint(store, checkpoint)
    accepted = await store.accept_execution_checkpoint_decision(
        checkpoint.checkpoint_id,
        project_id=checkpoint.project_id,
        checkpoint_type=checkpoint.checkpoint_type,
        request_id=f"answer-{checkpoint.checkpoint_id}",
        decision_hash=f"hash-{checkpoint.checkpoint_id}",
        decision={"option_id": "approve_once"},
    )
    assert accepted.acknowledged
    claimed = await store.claim_answered_execution_checkpoint(
        checkpoint.checkpoint_id,
        project_id=checkpoint.project_id,
        checkpoint_type=checkpoint.checkpoint_type,
        consumer_id=consumer_id,
        claim_id=claim_id,
    )
    assert claimed.acquired
    tool_call = dict(checkpoint.payload["tool_call"])
    return {
        "id": tool_call["id"],
        "function": tool_call["name"],
        "arguments": dict(tool_call["arguments"]),
        "fingerprint": tool_call["fingerprint"],
        "runtime_session_id": tool_call["runtime_session_id"],
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_type": checkpoint.checkpoint_type,
        "checkpoint_project_id": checkpoint.project_id,
        "task_id": checkpoint.task_id,
        "claim_id": claim_id,
        "consumer_id": consumer_id,
        "decision": "approve_once",
        "approved": True,
        "state": "ready",
    }


async def _raw_seed_task_runtime_views(
    store: OPCStore,
    task_id: str,
    *,
    runtime_resume: dict[str, Any],
    runtime_metadata: dict[str, Any],
    runtime_mirror: dict[str, Any] | None = None,
) -> None:
    """Inject a legacy/asymmetric row which public Store writes normalize."""

    task = await store.get_task(task_id)
    assert task is not None
    context_snapshot = dict(task.context_snapshot or {})
    metadata = dict(task.metadata or {})
    context_snapshot["runtime_resume"] = runtime_resume
    if runtime_mirror is None:
        context_snapshot.pop("runtime_v2", None)
    else:
        context_snapshot["runtime_v2"] = runtime_mirror
    metadata["runtime_v2"] = runtime_metadata
    assert store._db is not None
    await store._db.execute(
        "UPDATE tasks SET context_snapshot = ?, metadata = ? WHERE id = ?",
        (
            json.dumps(context_snapshot, ensure_ascii=False),
            json.dumps(metadata, ensure_ascii=False),
            task_id,
        ),
    )
    await store._db.commit()


def _bare_engine(store: OPCStore) -> OPCEngine:
    engine = OPCEngine.__new__(OPCEngine)
    engine.store = store
    engine.project_id = "project-a"
    engine.event_bus = EventBus()
    engine.interaction_coordinator = InteractionCoordinator(
        store=store,
        project_id="project-a",
        checkpoint_changed_callback=engine._interaction_checkpoint_changed,
    )
    engine._interaction_consumer_tasks = set()
    engine._initialized = True
    engine._shutting_down = False
    return engine


def test_terminal_company_work_item_does_not_resume_stale_tool_permission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            checkpoint = _tool_checkpoint()
            task = Task(
                id="worker",
                session_id="worker-session",
                project_id="project-a",
                title="failed review",
                status=TaskStatus.FAILED,
                metadata={"work_item_runtime": True},
            )
            work_item = DelegationWorkItem(
                work_item_id="review-failed",
                run_id="run-1",
                role_id="ceo",
                seat_id="seat-ceo",
                title="failed review",
                phase=Phase.FAILED,
            )
            set_linked_work_item_id(task, work_item.work_item_id)
            await store.save_task(task)
            await store.save_delegation_work_item(work_item)
            await store.link_work_item_runtime_task(work_item.work_item_id, task.id)
            tool_call = dict(checkpoint.payload["tool_call"])
            await store.save_runtime_tool_call(
                runtime_session_id=tool_call["runtime_session_id"],
                task_id=task.id,
                session_id=task.session_id,
                message_id="assistant-message",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            await _publish_owner_checkpoint(store, checkpoint)
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="answer",
                decision_hash="answer-hash",
                decision={"option_id": "approve_once"},
            )
            claimed = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="recovery",
            )
            assert claimed.acquired and claimed.checkpoint is not None
            engine = _bare_engine(store)
            engine.approval_engine = _approval_engine(
                store, engine.interaction_coordinator
            )
            lease = InteractionDecisionLease(
                checkpoint=claimed.checkpoint,
                decision={"option_id": "approve_once"},
                consumer_id="recovery",
                claim_id=claimed.claim_id,
            )

            with pytest.raises(
                ValueError,
                match="terminal company WorkItem",
            ):
                await engine._resume_permission_checkpoint(lease)

            persisted = await store.get_task(task.id)
            assert persisted is not None
            assert persisted.status == TaskStatus.FAILED
        finally:
            await store.close()

    asyncio.run(scenario())


def test_cross_process_coordinator_polling_and_idempotent_submit(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        opener_store = OPCStore(db_path)
        submitter_store = OPCStore(db_path)
        await opener_store.initialize()
        await submitter_store.initialize()
        changed_statuses: list[str] = []

        async def changed(checkpoint: ExecutionCheckpoint) -> None:
            changed_statuses.append(checkpoint.status)

        opener = InteractionCoordinator(
            store=opener_store,
            project_id="project-a",
            checkpoint_changed_callback=changed,
        )
        submitter = InteractionCoordinator(
            store=submitter_store,
            project_id="project-a",
            checkpoint_changed_callback=changed,
        )
        checkpoint = _tool_checkpoint()
        waiter = asyncio.create_task(opener.open_and_wait(
            checkpoint,
            prompt="Allow?",
            options=checkpoint.payload["interaction"]["options"],
            consumer_id="live-runtime",
            lease_seconds=2,
        ))
        try:
            for _ in range(50):
                if await submitter_store.get_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                ):
                    break
                await asyncio.sleep(0.01)
            first = await submitter.submit(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                decision={"option_id": "approve_once", "text": "Approve once"},
                client_request_id="request-1",
            )
            duplicate = await submitter.submit(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                decision={"option_id": "approve_once", "text": "Approve once"},
                client_request_id="request-1",
            )
            lease = await asyncio.wait_for(waiter, timeout=2)
            assert first.outcome == "accepted"
            assert duplicate.outcome == "duplicate"
            assert lease.decision["option_id"] == "approve_once"
            assert lease.checkpoint.status == "consuming"
            finished = await opener.finish(lease)
            assert finished.applied
            assert {"pending", "answered", "consuming", "resolved"}.issubset(
                set(changed_statuses)
            )
        finally:
            if not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            await submitter_store.close()
            await opener_store.close()

    asyncio.run(scenario())


def test_engine_submit_never_schedules_consumer_against_registered_live_waiter(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            anchor = Task(
                id="anchor",
                project_id="project-a",
                session_id="root-session",
                metadata={"mode": "company", "execution_mode": "company_mode"},
            )
            worker = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                parent_id="anchor",
                parent_session_id="root-session",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "analysis",
                },
            )
            await store.save_task(anchor)
            await store.save_task(worker)
            engine = _bare_engine(store)
            checkpoint = _tool_checkpoint()
            waiter = asyncio.create_task(engine.interaction_coordinator.open_and_wait(
                checkpoint,
                prompt="Allow?",
                options=checkpoint.payload["interaction"]["options"],
                consumer_id="live-runtime",
                lease_seconds=2,
            ))
            for _ in range(100):
                if await store.get_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                ):
                    break
                await asyncio.sleep(0.01)
            scheduled: list[tuple[str, str]] = []

            def schedule(checkpoint_id: str, checkpoint_type: str) -> None:
                scheduled.append((checkpoint_id, checkpoint_type))

            engine._schedule_interaction_consumption = schedule  # type: ignore[method-assign]
            receipt = await engine.submit_checkpoint_decision(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                decision={"option_id": "approve_once"},
                client_request_id="live-submit",
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            assert receipt["accepted"] is True
            assert scheduled == []
            lease = await asyncio.wait_for(waiter, timeout=2)
            assert lease.checkpoint.status == "consuming"
            finished = await engine.interaction_coordinator.finish(lease)
            assert finished.applied
        finally:
            await store.close()

    asyncio.run(scenario())


def test_scoped_payload_patch_never_overwrites_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first = OPCStore(db_path)
        second = OPCStore(db_path)
        await first.initialize()
        await second.initialize()
        checkpoint = _tool_checkpoint()
        await _publish_owner_checkpoint(first, checkpoint)
        second_coordinator = InteractionCoordinator(
            store=second,
            project_id="project-a",
        )
        start = asyncio.Event()

        async def answer():
            await start.wait()
            return await first.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="request-1",
                decision_hash="hash-1",
                decision={"option_id": "approve_once"},
            )

        async def enrich():
            await start.wait()
            return await second_coordinator.enrich_owner_checkpoint(
                checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                expected_statuses={"pending"},
                payload_patch={"migrated_to_runtime_v2": {"version": 2}},
            )

        answer_task = asyncio.create_task(answer())
        patch_task = asyncio.create_task(enrich())
        start.set()
        answer_receipt, _ = await asyncio.gather(answer_task, patch_task)
        persisted = await first.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-a",
            checkpoint_type="tool_permission",
        )
        try:
            assert answer_receipt.outcome == "accepted"
            assert persisted is not None and persisted.status == "answered"
            assert persisted.payload["interaction"]["decision"]["value"] == {
                "option_id": "approve_once"
            }
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_concurrent_owner_producer_reentry_creates_one_active_interaction(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            first_engine = _bare_engine(first_store)
            second_engine = _bare_engine(second_store)
            checkpoint_data = {
                "project_id": "project-a",
                "session_id": "root-session",
                "checkpoint_type": "task_user_input",
                "task_id": "worker",
                "payload": {
                    "task_id": "worker",
                    "session_id": "root-session",
                    "prompt": "Which market should the analysis cover?",
                    "review_level": "human",
                },
            }
            start = asyncio.Event()

            async def produce(engine: OPCEngine) -> None:
                await start.wait()
                await engine._save_execution_checkpoint(checkpoint_data)

            producers = [
                asyncio.create_task(produce(first_engine)),
                asyncio.create_task(produce(second_engine)),
            ]
            start.set()
            await asyncio.gather(*producers)
            active = await first_store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["task_user_input"],
                statuses=["pending", "answered", "consuming"],
            )
            assert len(active) == 1
            domain_key = active[0].payload["interaction"]["domain_key"]
            assert domain_key

            closed = await first_engine.interaction_coordinator.close_pending_owner_checkpoint(
                active[0].checkpoint_id,
                checkpoint_type="task_user_input",
                status="invalid",
            )
            assert closed[1]
            await first_engine._save_execution_checkpoint(checkpoint_data)
            retried_active = await first_store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["task_user_input"],
                statuses=["pending"],
            )
            assert retried_active == []
            next_checkpoint_data = {
                **checkpoint_data,
                "payload": {
                    **dict(checkpoint_data["payload"]),
                    "prompt": "Which market and time horizon should the analysis cover?",
                    "source_event_id": "followup-turn-2",
                },
            }
            await first_engine._save_execution_checkpoint(next_checkpoint_data)
            next_active = await first_store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["task_user_input"],
                statuses=["pending"],
            )
            assert len(next_active) == 1
            assert next_active[0].checkpoint_id != active[0].checkpoint_id
            assert next_active[0].payload["interaction"]["domain_key"] != domain_key
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_concurrent_distinct_owner_revisions_never_supersede_to_zero(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            first_engine = _bare_engine(first_store)
            second_engine = _bare_engine(second_store)
            start = asyncio.Event()

            async def produce(engine: OPCEngine, revision: int) -> None:
                await start.wait()
                await engine._save_execution_checkpoint({
                    "project_id": "project-a",
                    "session_id": "root-session",
                    "checkpoint_type": "task_user_input",
                    "task_id": "worker",
                    "payload": {
                        "task_id": "worker",
                        "session_id": "root-session",
                        "prompt": f"Question revision {revision}",
                        "basis_hash": f"basis-{revision}",
                        "review_level": "human",
                    },
                })

            producers = [
                asyncio.create_task(produce(first_engine, 1)),
                asyncio.create_task(produce(second_engine, 2)),
            ]
            start.set()
            await asyncio.gather(*producers)

            rows = await first_store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["task_user_input"],
            )
            assert len(rows) == 2
            assert sum(row.status == "pending" for row in rows) == 1
            assert sum(row.status == "superseded" for row in rows) == 1
            winner = next(row for row in rows if row.status == "pending")
            loser = next(row for row in rows if row.status == "superseded")
            assert loser.payload["superseded_by_checkpoint_id"] == winner.checkpoint_id
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_process_message_owner_compatibility_uses_unified_submit_and_claim(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            engine = _bare_engine(store)
            engine._resume_task_checkpoint = AsyncMock(return_value="task resumed")
            engine._resume_staffing_selection_checkpoint = AsyncMock(
                return_value="staffing resumed"
            )
            engine._run_company_delivery_self_evolution_consumed = AsyncMock(
                return_value="delivery resumed"
            )
            canonical_submit = engine.submit_checkpoint_decision
            engine.submit_checkpoint_decision = AsyncMock(  # type: ignore[method-assign]
                wraps=canonical_submit
            )

            cases = [
                (
                    ExecutionCheckpoint(
                        checkpoint_id="cli-input",
                        project_id="project-a",
                        session_id="root-session",
                        checkpoint_type="task_user_input",
                        payload={
                            "original_message": "Analyze an investment target",
                            "interaction": {"kind": "task_user_input"},
                        },
                    ),
                    "Cover the Hong Kong market.",
                    {},
                    "task resumed",
                ),
                (
                    ExecutionCheckpoint(
                        checkpoint_id="cli-staffing",
                        project_id="project-a",
                        session_id="root-session",
                        checkpoint_type="company_staffing_selection",
                        payload={"interaction": {"kind": "company_staffing_selection"}},
                    ),
                    "approve",
                    {
                        "staffing_action": "manual_approve",
                        "staffing_selections": {
                            "analyst": {"kind": "fallback", "id": ""}
                        },
                    },
                    "staffing resumed",
                ),
                (
                    ExecutionCheckpoint(
                        checkpoint_id="cli-delivery",
                        project_id="project-a",
                        session_id="root-session",
                        checkpoint_type="company_delivery_feedback",
                        payload={"interaction": {"kind": "company_delivery_feedback"}},
                    ),
                    "Please improve the valuation sensitivity analysis.",
                    {"checkpoint_reply_kind": "feedback"},
                    "delivery resumed",
                ),
            ]
            for checkpoint, text, metadata, expected in cases:
                await _publish_owner_checkpoint(store, checkpoint)
                result = await engine._maybe_resume_checkpoint(
                    text,
                    session_id="root-session",
                    reply_metadata={
                        **metadata,
                        "response_to_checkpoint_id": checkpoint.checkpoint_id,
                        "response_to_checkpoint_type": checkpoint.checkpoint_type,
                    },
                    requested_mode="company",
                )
                assert result == expected
                persisted = await store.get_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type=checkpoint.checkpoint_type,
                )
                assert persisted is not None and persisted.status == "resolved"

            assert engine.submit_checkpoint_decision.await_count == 3
            engine._resume_task_checkpoint.assert_awaited_once()
            engine._resume_staffing_selection_checkpoint.assert_awaited_once()
            engine._run_company_delivery_self_evolution_consumed.assert_awaited_once()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_new_company_turn_atomically_supersedes_delivery_review_in_engine_domain(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            waiting_task = Task(
                id="delivery-task",
                project_id="project-a",
                session_id="root-session:delivery",
                parent_session_id="root-session",
                status=TaskStatus.AWAITING_HUMAN,
                metadata={
                    "execution_mode": "company_mode",
                    "feedback_scope": "final",
                    "requires_user_feedback": True,
                },
            )
            await store.save_task(waiting_task)
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="delivery-review",
                project_id="project-a",
                session_id=waiting_task.session_id,
                checkpoint_type="company_delivery_feedback",
                task_id=waiting_task.id,
                payload={
                    "waiting_task_id": waiting_task.id,
                    "delivery_revision": 3,
                    "basis_hash": "delivery-basis-3",
                    "interaction": {
                        "kind": "company_delivery_feedback",
                        "ownership": {"root_session_id": "root-session"},
                    },
                },
            )
            await _publish_owner_checkpoint(store, checkpoint)
            engine = _bare_engine(store)
            superseded = await engine.supersede_delivery_feedback_for_new_company_turn(
                root_session_id="root-session",
                conversation_turn_id="turn-4",
            )
            assert superseded == [checkpoint.checkpoint_id]
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="company_delivery_feedback",
            )
            assert persisted is not None and persisted.status == "superseded"
            assert persisted.payload["checkpoint_resolution_reason"] == (
                "new_company_turn_started"
            )
            assert persisted.payload["superseded_by_company_turn"] == {
                "conversation_turn_id": "turn-4",
                "root_session_id": "root-session",
                "prior_delivery_revision": 3,
                "prior_basis_hash": "delivery-basis-3",
            }
            assert "decision" not in persisted.payload["interaction"]
            refreshed_task = await store.get_task(waiting_task.id)
            assert refreshed_task is not None
            assert refreshed_task.status == TaskStatus.DONE
            assert refreshed_task.metadata["feedback_superseded"] is True
            assert refreshed_task.metadata["feedback_superseded_by_turn_id"] == "turn-4"
            events = engine.event_bus.get_history("runtime_event")
            assert events[-1].payload["type"] == "interaction_checkpoint_changed"
            assert events[-1].payload["status"] == "superseded"

            # Re-entry is idempotent and never converts this domain transition
            # into an accepted interaction decision.
            assert await engine.supersede_delivery_feedback_for_new_company_turn(
                root_session_id="root-session",
                conversation_turn_id="turn-4",
            ) == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_handle_message_invokes_delivery_supersede_only_for_plain_company_turn() -> None:
    async def scenario() -> None:
        engine = OPCEngine.__new__(OPCEngine)
        engine.context_loader = object()
        engine.memory = None
        engine.project_id = "project-a"
        engine.store = SimpleNamespace(
            allocate_owner_interaction_source_sequence=AsyncMock(return_value=1)
        )
        engine._ensure_primary_session = AsyncMock()
        engine.supersede_delivery_feedback_for_new_company_turn = AsyncMock(
            return_value=[]
        )
        engine._maybe_resume_checkpoint = AsyncMock(return_value="handled")
        plain = UserMessage(
            channel="cli",
            user_id="owner",
            content="Start a new company delivery revision.",
            session_id="root-session",
            project_context="project-a",
            metadata={"mode": "company", "ui_message_id": "turn-2"},
        )
        response = await engine._handle_message(plain)
        assert response.content == "handled"
        engine.supersede_delivery_feedback_for_new_company_turn.assert_awaited_once_with(
            root_session_id="root-session",
            conversation_turn_id="ui-turn:turn-2",
        )

        engine.supersede_delivery_feedback_for_new_company_turn.reset_mock()
        explicit_reply = UserMessage(
            channel="cli",
            user_id="owner",
            content="approve",
            session_id="root-session",
            project_context="project-a",
            metadata={
                "mode": "company",
                "response_to_checkpoint_id": "delivery-review",
                "response_to_checkpoint_type": "company_delivery_feedback",
            },
        )
        await engine._handle_message(explicit_reply)
        engine.supersede_delivery_feedback_for_new_company_turn.assert_not_awaited()

    asyncio.run(scenario())


def test_can_answer_uses_company_anchor_rejects_manager_and_invalid_decision(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            anchor = Task(
                id="anchor",
                project_id="project-a",
                session_id="root-session",
                metadata={"mode": "company", "execution_mode": "company_mode"},
            )
            worker = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                parent_session_id="root-session",
                parent_id="anchor",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "analysis",
                },
            )
            await store.save_task(anchor)
            await store.save_task(worker)
            checkpoint = _tool_checkpoint()
            await _publish_owner_checkpoint(store, checkpoint)
            engine = _bare_engine(store)

            assert await engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            assert not await engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id="worker",
                requester_session_id="worker-session",
            )
            invalid = await engine.submit_checkpoint_decision(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                decision={"option_id": "invented"},
                client_request_id="invalid-request",
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            assert invalid["accepted"] is False
            assert invalid["reason"] == "invalid_option_id"
            current = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert current is not None and current.status == "pending"

            manager = ExecutionCheckpoint(
                checkpoint_id="manager-wait",
                project_id="project-a",
                session_id="worker-session",
                checkpoint_type="task_user_input",
                task_id="worker",
                payload={
                    "task_id": "worker",
                    "pause_request": {"review_level": "manager"},
                    "interaction": {"kind": "task_user_input"},
                },
            )
            await _publish_owner_checkpoint(store, manager)
            assert not await engine.can_answer_checkpoint(
                manager,
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            rejected_manager = await engine.submit_checkpoint_decision(
                checkpoint_id=manager.checkpoint_id,
                checkpoint_type=manager.checkpoint_type,
                decision={"text": "approve"},
                client_request_id="manager-request",
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            assert rejected_manager["accepted"] is False
            assert rejected_manager["reason"] == "not_authorized"
            persisted_manager = await store.get_execution_checkpoint(
                manager.checkpoint_id,
                project_id="project-a",
                checkpoint_type=manager.checkpoint_type,
            )
            assert persisted_manager is not None
            assert persisted_manager.status == "pending"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_every_owner_interaction_type_rejects_invalid_typed_decision(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            engine = _bare_engine(store)
            cases = [
                (
                    "action_permission",
                    {"option_id": "invented"},
                    {"option_id": "approve_once"},
                    "invalid_option_id",
                ),
                (
                    "task_user_input",
                    {"unexpected": "value"},
                    {"text": "Here is the required detail."},
                    "input_required",
                ),
                (
                    "route_clarification",
                    {"unexpected": "value"},
                    {"text": "This is an investment-analysis request."},
                    "input_required",
                ),
                (
                    "company_runtime_selection",
                    {"unexpected": "value"},
                    {"text": "Use company mode."},
                    "input_required",
                ),
                (
                    "company_work_item_gate",
                    {"text": "maybe"},
                    {"option_id": "approve", "text": "Approve"},
                    "invalid_gate_action",
                ),
                (
                    "company_delivery_feedback",
                    {"checkpoint_reply_kind": "feedback"},
                    {"checkpoint_reply_kind": "approve"},
                    "feedback_text_required",
                ),
                (
                    "company_staffing_selection",
                    {"staffing_action": "manual_approve"},
                    {"staffing_action": "auto_recruit"},
                    "invalid_staffing_selections",
                ),
                (
                    "company_recruitment_confirmation",
                    {"checkpoint_reply_kind": "feedback"},
                    {"checkpoint_reply_kind": "deny"},
                    "feedback_text_required",
                ),
                (
                    "company_reorg_pending",
                    {"text": "later"},
                    {"text": "approve"},
                    "invalid_reorg_action",
                ),
                (
                    "company_run_failure_review",
                    {"text": "rerun it"},
                    {"text": "close"},
                    "invalid_failure_review_action",
                ),
            ]
            for index, (checkpoint_type, invalid, valid, expected_reason) in enumerate(cases):
                payload: dict[str, Any] = {
                    "interaction": {"kind": checkpoint_type},
                }
                if checkpoint_type == "action_permission":
                    payload["interaction"]["options"] = [
                        {"id": "approve_once", "label": "Approve once"},
                        {"id": "deny", "label": "Deny"},
                    ]
                checkpoint = ExecutionCheckpoint(
                    checkpoint_id=f"typed-{index}",
                    project_id="project-a",
                    session_id="root-session",
                    checkpoint_type=checkpoint_type,
                    payload=payload,
                )
                await _publish_owner_checkpoint(store, checkpoint)
                assert engine.interaction_coordinator.validate_decision(
                    checkpoint,
                    valid,
                ) == ""
                receipt = await engine.submit_checkpoint_decision(
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_type=checkpoint_type,
                    decision=invalid,
                    client_request_id=f"typed-request-{index}",
                    requester_session_id="root-session",
                )
                assert receipt["accepted"] is False
                assert receipt["reason"] == expected_reason
                persisted = await store.get_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type=checkpoint_type,
                )
                assert persisted is not None and persisted.status == "pending"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_domain_dispatchers_apply_effects_without_settling_claimed_checkpoint() -> None:
    async def scenario() -> None:
        engine = OPCEngine.__new__(OPCEngine)
        engine._resume_routing_checkpoint = AsyncMock(return_value="route")
        engine._resume_task_checkpoint = AsyncMock(return_value="task")
        engine._resume_company_work_item_gate_decision = AsyncMock(
            return_value="gate"
        )
        engine._ignore_company_delivery_feedback_consumed = AsyncMock(
            return_value="ignore"
        )
        engine._run_company_delivery_self_evolution_consumed = AsyncMock(
            return_value="delivery"
        )
        engine._resume_staffing_selection_checkpoint = AsyncMock(return_value="staff")
        engine._resume_recruitment_checkpoint = AsyncMock(return_value="recruit")
        engine._resume_reorg_checkpoint = AsyncMock(return_value="reorg")

        async def dispatch(checkpoint_type: str, decision: dict[str, Any]) -> str:
            checkpoint = ExecutionCheckpoint(
                project_id="project-a",
                session_id="root-session",
                checkpoint_type=checkpoint_type,
                payload={},
            )
            return await engine._dispatch_interaction_decision(
                InteractionDecisionLease(
                    checkpoint=checkpoint,
                    decision=decision,
                    consumer_id="consumer",
                    claim_id="claim",
                )
            )

        await dispatch("route_clarification", {"text": "details"})
        await dispatch("task_user_input", {"text": "details"})
        await dispatch("company_work_item_gate", {"option_id": "approve"})
        await dispatch(
            "company_delivery_feedback",
            {"checkpoint_reply_kind": "ignore", "text": "ignore"},
        )
        await dispatch(
            "company_delivery_feedback",
            {"checkpoint_reply_kind": "approve", "text": "approve"},
        )
        await dispatch(
            "company_staffing_selection",
            {"staffing_action": "auto_recruit", "text": "auto recruit"},
        )
        await dispatch(
            "company_recruitment_confirmation",
            {"checkpoint_reply_kind": "deny", "text": "deny"},
        )
        await dispatch("company_reorg_pending", {"text": "approve"})
        assert await dispatch(
            "company_run_failure_review",
            {"text": "close"},
        ) == "Company run closure acknowledged."

        engine._resume_routing_checkpoint.assert_awaited_once()
        engine._resume_task_checkpoint.assert_awaited_once()
        engine._resume_company_work_item_gate_decision.assert_awaited_once()
        gate_lease = (
            engine._resume_company_work_item_gate_decision.await_args.args[0]
        )
        assert isinstance(gate_lease, InteractionDecisionLease)
        assert gate_lease.claim_id == "claim"
        assert gate_lease.consumer_id == "consumer"
        engine._ignore_company_delivery_feedback_consumed.assert_awaited_once()
        engine._run_company_delivery_self_evolution_consumed.assert_awaited_once()
        engine._resume_staffing_selection_checkpoint.assert_awaited_once()
        engine._resume_recruitment_checkpoint.assert_awaited_once()
        engine._resume_reorg_checkpoint.assert_awaited_once()

    asyncio.run(scenario())


def test_work_item_gate_handler_carries_full_lease_and_postcommit_faults_are_safe() -> None:
    async def scenario() -> None:
        engine = OPCEngine.__new__(OPCEngine)
        engine.project_id = "project-a"
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="gate-typed-1",
            project_id="project-a",
            session_id="root-session",
            checkpoint_type="company_work_item_gate",
            status="consuming",
            task_id="task-1",
            payload={
                "run_id": "run-1",
                "waiting_task_id": "task-1",
                "waiting_work_item_id": "work-item-1",
                "work_item_attempt_seq": 4,
                "gate_attempt": 2,
                "basis_hash": "basis-1",
                "interaction": {
                    "kind": "company_work_item_gate",
                    "ownership": {"root_session_id": "root-session"},
                },
            },
        )
        resolved = ExecutionCheckpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            project_id=checkpoint.project_id,
            session_id=checkpoint.session_id,
            checkpoint_type=checkpoint.checkpoint_type,
            status="resolved",
            task_id=checkpoint.task_id,
            payload=checkpoint.payload,
        )
        engine.store = SimpleNamespace(
            apply_company_work_item_gate_decision_for_controller=AsyncMock(
                return_value=SimpleNamespace(
                    applied=True,
                    outcome="applied",
                    checkpoint=resolved,
                    conflict_reason="",
                )
            )
        )

        def failed_wake(_run_id: str) -> bool:
            raise RuntimeError("wake failed")

        engine.company_executor = SimpleNamespace(
            controller_lease_credential=lambda _run_id: {
                "run_id": "run-1",
                "project_id": "project-a",
                "root_session_id": "root-session",
                "owner_token": "owner-1",
                "generation": 7,
            },
            wake_live_run_dispatcher=failed_wake,
        )
        engine.interaction_coordinator = SimpleNamespace(
            notify_persisted_owner_checkpoint=AsyncMock(
                side_effect=RuntimeError("notify failed")
            )
        )
        lease = InteractionDecisionLease(
            checkpoint=checkpoint,
            decision={"option_id": "approve"},
            consumer_id="consumer-1",
            claim_id="claim-1",
        )

        message = await engine._resume_company_work_item_gate_decision(lease)

        assert message == "Company work-item gate approved."
        call = engine.store.apply_company_work_item_gate_decision_for_controller.await_args
        context, command = call.args
        assert context.owner_token == "owner-1"
        assert context.generation == 7
        assert command.claim_id == lease.claim_id
        assert command.consumer_id == lease.consumer_id
        assert command.attempt_seq == 4
        assert command.gate_attempt == 2
        assert command.basis_hash == "basis-1"
        engine.interaction_coordinator.notify_persisted_owner_checkpoint.assert_awaited_once_with(
            resolved
        )

    asyncio.run(scenario())


def test_busy_crash_lease_is_reclaimed_and_release_retries_are_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="route-1",
                project_id="project-a",
                session_id="root-session",
                checkpoint_type="route_clarification",
                payload={"interaction": {"kind": "route_clarification"}},
            )
            await _publish_owner_checkpoint(store, checkpoint)
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
                request_id="request-1",
                decision_hash="hash-1",
                decision={"text": "details"},
            )
            await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
                consumer_id="crashed-controller",
                lease_seconds=0.12,
                claimed_at=datetime.now(),
            )
            engine = _bare_engine(store)
            scope_attempts = 0
            dispatch_attempts = 0

            async def resolve_scope(_: ExecutionCheckpoint) -> dict[str, str]:
                nonlocal scope_attempts
                scope_attempts += 1
                if scope_attempts < 3:
                    raise RuntimeError("transient")
                return {}

            async def dispatch(
                _: InteractionDecisionLease,
                **__: Any,
            ) -> str:
                nonlocal dispatch_attempts
                dispatch_attempts += 1
                return "applied"

            engine._interaction_execution_scope = resolve_scope  # type: ignore[method-assign]
            engine._dispatch_interaction_decision = dispatch  # type: ignore[method-assign]
            await asyncio.wait_for(
                engine._consume_answered_interaction(
                    checkpoint.checkpoint_id,
                    checkpoint.checkpoint_type,
                ),
                timeout=3,
            )
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
            )
            assert scope_attempts == 3
            assert dispatch_attempts == 1
            assert persisted is not None and persisted.status == "resolved"
            assert persisted.payload["interaction_retry"]["attempts"] == 2
        finally:
            await store.close()

    asyncio.run(scenario())


def test_long_live_consumer_renews_lease_and_prevents_second_dispatch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="long-consumer",
                project_id="project-a",
                session_id="root-session",
                checkpoint_type="route_clarification",
                payload={"interaction": {"kind": "route_clarification"}},
            )
            await _publish_owner_checkpoint(first_store, checkpoint)
            await first_store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
                request_id="long-request",
                decision_hash="long-hash",
                decision={"text": "continue"},
            )
            first_engine = _bare_engine(first_store)
            second_engine = _bare_engine(second_store)
            for engine in (first_engine, second_engine):
                engine._INTERACTION_LEASE_SECONDS = 0.15

            first_started = asyncio.Event()
            allow_finish = asyncio.Event()
            first_dispatches = 0
            second_dispatches = 0

            async def first_dispatch(_lease, **_kwargs):
                nonlocal first_dispatches
                first_dispatches += 1
                first_started.set()
                await allow_finish.wait()
                return "first finished"

            async def second_dispatch(_lease, **_kwargs):
                nonlocal second_dispatches
                second_dispatches += 1
                return "duplicate dispatch"

            first_engine._dispatch_interaction_decision = first_dispatch  # type: ignore[method-assign]
            second_engine._dispatch_interaction_decision = second_dispatch  # type: ignore[method-assign]
            first_consumer = asyncio.create_task(
                first_engine._consume_answered_interaction(
                    checkpoint.checkpoint_id,
                    checkpoint.checkpoint_type,
                )
            )
            await asyncio.wait_for(first_started.wait(), timeout=1)
            # Exceed the original lease.  Heartbeats must keep the first claim
            # live while the domain effect is still running.
            await asyncio.sleep(0.22)
            second_consumer = asyncio.create_task(
                second_engine._consume_answered_interaction(
                    checkpoint.checkpoint_id,
                    checkpoint.checkpoint_type,
                )
            )
            await asyncio.sleep(0.18)
            assert second_dispatches == 0
            allow_finish.set()
            await asyncio.wait_for(
                asyncio.gather(first_consumer, second_consumer),
                timeout=2,
            )

            assert first_dispatches == 1
            assert second_dispatches == 0
            persisted = await first_store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
            )
            assert persisted is not None and persisted.status == "resolved"
            claim = persisted.payload["interaction"]["claim"]
            assert claim.get("heartbeat_at")
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_sync_blocked_owner_effect_loses_lease_without_replay(
    tmp_path: Path,
) -> None:
    """A stalled event loop cannot report success after another DB owner wins."""

    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        second_store = OPCStore(db_path)
        await first_store.initialize()
        await second_store.initialize()
        try:
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="sync-blocked-effect",
                project_id="project-a",
                session_id="root-session",
                checkpoint_type="route_clarification",
                payload={
                    "interaction": {
                        "kind": "route_clarification",
                        "domain_key": "route:sync-blocked-effect",
                    }
                },
            )
            await _publish_owner_checkpoint(first_store, checkpoint)
            accepted = await first_store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
                request_id="sync-blocked-request",
                decision_hash="sync-blocked-hash",
                decision={"text": "continue"},
            )
            assert accepted.acknowledged

            first_engine = _bare_engine(first_store)
            first_engine._INTERACTION_LEASE_SECONDS = 0.09
            effect_started = threading.Event()
            side_effects: list[str] = []
            competing_result: dict[str, Any] = {}
            competing_failure: list[BaseException] = []

            async def first_dispatch(_lease, **_kwargs):
                effect_started.set()
                # Deliberately block this controller's whole event loop so its
                # sole heartbeat cannot renew before the short lease expires.
                time.sleep(0.28)
                side_effects.append("first-effect")
                return "stale controller tried to finish"

            first_engine._dispatch_interaction_decision = first_dispatch  # type: ignore[method-assign]

            def competing_controller() -> None:
                try:
                    assert effect_started.wait(timeout=2)
                    time.sleep(0.14)

                    async def reclaim_and_probe() -> None:
                        claim = await second_store.claim_answered_execution_checkpoint(
                            checkpoint.checkpoint_id,
                            project_id="project-a",
                            checkpoint_type=checkpoint.checkpoint_type,
                            consumer_id="controller-b",
                            claim_id="claim-b",
                            lease_seconds=0.09,
                        )
                        competing_result["claim_outcome"] = claim.outcome
                        competing_result["claim_status"] = (
                            claim.checkpoint.status if claim.checkpoint else ""
                        )
                        second_engine = _bare_engine(second_store)
                        second_dispatches = 0

                        async def second_dispatch(_lease, **_kwargs):
                            nonlocal second_dispatches
                            second_dispatches += 1
                            return "must not replay"

                        second_engine._dispatch_interaction_decision = second_dispatch  # type: ignore[method-assign]
                        await second_engine._consume_answered_interaction(
                            checkpoint.checkpoint_id,
                            checkpoint.checkpoint_type,
                        )
                        competing_result["second_dispatches"] = second_dispatches

                    asyncio.run(reclaim_and_probe())
                except BaseException as exc:  # surfaced in the main test loop
                    competing_failure.append(exc)

            thread = threading.Thread(
                target=competing_controller,
                name="competing-owner-controller",
                daemon=True,
            )
            thread.start()
            await first_engine._consume_answered_interaction(
                checkpoint.checkpoint_id,
                checkpoint.checkpoint_type,
            )
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert not competing_failure

            # The external/domain effect crossed its durable execution fence
            # exactly once.  The expired claim is terminalized conservatively;
            # neither controller can replay or overwrite it with a stale
            # successful completion.
            assert side_effects == ["first-effect"]
            assert competing_result == {
                "claim_outcome": "invalid_state",
                "claim_status": "outcome_unknown",
                "second_dispatches": 0,
            }
            persisted = await first_store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type=checkpoint.checkpoint_type,
            )
            assert persisted is not None
            assert persisted.status == "outcome_unknown"
            interaction = persisted.payload["interaction"]
            assert interaction["execution"]["state"] == "outcome_unknown"
            assert interaction["completion"]["final_status"] == "outcome_unknown"
            assert interaction["completion"]["consumer_id"].startswith(
                f"engine:{id(first_engine)}:"
            )
            assert "interaction_result" not in persisted.payload
        finally:
            await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


class _Part:
    def __init__(self, part_type: str, payload: dict[str, Any]) -> None:
        self.part_type = part_type
        self.payload = payload


class _Message:
    def __init__(self, role: str) -> None:
        self.role = role
        self.summary_flag = False


class _ResumeStore:
    def __init__(
        self,
        checkpoint: ExecutionCheckpoint | list[ExecutionCheckpoint],
    ) -> None:
        checkpoints = checkpoint if isinstance(checkpoint, list) else [checkpoint]
        self.checkpoints = {
            item.checkpoint_id: item
            for item in checkpoints
        }
        self.checkpoint = checkpoints[0]
        self.saved_tasks: list[Task] = []
        self.tasks: dict[str, Task] = {}
        self.results: list[dict[str, Any]] = []
        self.finish_calls = 0
        self.transcript = [
            {"message": _Message("user"), "parts": [_Part("text", {"text": "original request"})]},
            {
                "message": _Message("assistant"),
                "parts": [
                    _Part("text", {"text": "I will use the tool."}),
                    *[
                        _Part("tool_call", {
                            "tool_call_id": item.payload["tool_call"]["id"],
                            "tool_name": item.payload["tool_call"]["name"],
                            "arguments": item.payload["tool_call"]["arguments"],
                        })
                        for item in checkpoints
                    ],
                ],
            },
        ]

    async def get_session_transcript(self, session_id: str):
        return list(self.transcript)

    async def get_execution_checkpoint(self, checkpoint_id: str, **kwargs):
        _ = kwargs
        return self.checkpoints.get(checkpoint_id)

    async def save_task(self, task: Task) -> None:
        self.saved_tasks.append(task)
        self.tasks[task.id] = task

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def get_task_runtime_tool_ledger(
        self,
        task_id: str,
        **_: Any,
    ) -> TaskRuntimeToolLedgerSnapshot | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        context_snapshot = dict(task.context_snapshot or {})
        metadata = dict(task.metadata or {})
        (
            runtime_resume,
            runtime_metadata,
            permits,
            continuation,
            runtime_session_id,
        ) = OPCStore._canonicalize_task_runtime_tool_ledger(
            context_snapshot,
            metadata,
        )
        context_snapshot.setdefault("runtime_v2", {})
        OPCStore._write_canonical_task_runtime_tool_ledger(
            context_snapshot,
            metadata,
            runtime_resume=runtime_resume,
            runtime_metadata=runtime_metadata,
            permits=permits,
            continuation=continuation,
            runtime_session_id=runtime_session_id,
        )
        return TaskRuntimeToolLedgerSnapshot(
            task_id=task_id,
            runtime_session_id=runtime_session_id,
            permits=permits,
            continue_after_tool_result=continuation,
            runtime_resume=context_snapshot["runtime_resume"],
            runtime_metadata=metadata["runtime_v2"],
            context_runtime_v2=context_snapshot["runtime_v2"],
        )

    async def update_task_runtime_tool_permit(
        self,
        task_id: str,
        *,
        runtime_session_id: str,
        fingerprint: str,
        permit: dict[str, Any] | None,
        **_: Any,
    ) -> Task:
        task = self.tasks[task_id]
        context_snapshot = dict(task.context_snapshot or {})
        metadata = dict(task.metadata or {})
        runtime_resume = dict(context_snapshot.get("runtime_resume", {}) or {})
        runtime_metadata = dict(metadata.get("runtime_v2", {}) or {})
        approved_calls = dict(runtime_resume.get("approved_tool_calls", {}) or {})
        if permit is None:
            approved_calls.pop(fingerprint, None)
        else:
            approved_calls[fingerprint] = dict(permit)
        OPCStore._write_canonical_task_runtime_tool_ledger(
            context_snapshot,
            metadata,
            runtime_resume=runtime_resume,
            runtime_metadata=runtime_metadata,
            permits=approved_calls,
            continuation=False,
            runtime_session_id=runtime_session_id,
        )
        task.context_snapshot = context_snapshot
        task.metadata = metadata
        return task

    async def save_runtime_session(self, **kwargs) -> None:
        _ = kwargs

    async def save_runtime_transcript_entry(self, **kwargs) -> None:
        _ = kwargs

    async def save_runtime_tool_result(self, **kwargs) -> None:
        self.results.append(dict(kwargs))

    async def finish_execution_checkpoint_consumption(self, *args, **kwargs):
        checkpoint_id = str(args[0] if args else kwargs.get("checkpoint_id", ""))
        self.finish_calls += 1
        checkpoint = self.checkpoints[checkpoint_id]
        checkpoint.status = "resolved"
        return ExecutionCheckpointConsumptionReceipt(
            outcome="finished",
            checkpoint=checkpoint,
        )


class _ResumeCoordinator:
    """Minimal coordinator seam for transcript-focused NativeRuntime tests."""

    def __init__(self, store: _ResumeStore) -> None:
        self.store = store

    async def begin_exact_tool_effect(self, permit: dict[str, Any]):
        executing = {**permit, "state": "executing"}
        await self.store.update_task_runtime_tool_permit(
            str(permit["task_id"]),
            runtime_session_id=str(permit["runtime_session_id"]),
            fingerprint=str(permit["fingerprint"]),
            permit=executing,
        )
        return SimpleNamespace(acquired=True, outcome="started")

    async def settle_interrupted_exact_tool(
        self,
        permit: dict[str, Any],
        *,
        state: str,
        **_: Any,
    ) -> ExecutionCheckpointConsumptionReceipt:
        checkpoint = self.store.checkpoints[str(permit["checkpoint_id"])]
        if state == "result_persisted":
            checkpoint.status = "resolved"
            outcome = "finished"
        elif state == "executing":
            checkpoint.status = "outcome_unknown"
            outcome = "finished"
        else:
            checkpoint.status = "answered"
            outcome = "released"
        return ExecutionCheckpointConsumptionReceipt(
            outcome=outcome,
            checkpoint=checkpoint,
        )

    async def persist_exact_tool_result(
        self,
        permit: dict[str, Any],
        **kwargs: Any,
    ) -> ExecutionCheckpointConsumptionReceipt:
        await self.store.save_runtime_tool_result(**kwargs)
        await self.store.update_task_runtime_tool_permit(
            str(kwargs["task_id"]),
            runtime_session_id=str(kwargs["runtime_session_id"]),
            fingerprint=str(permit["fingerprint"]),
            permit=None,
        )
        return await self.store.finish_execution_checkpoint_consumption(
            str(permit["checkpoint_id"]),
        )


class _ResumeMemory:
    def __init__(self, store: _ResumeStore) -> None:
        self.store = store
        self.messages: list[dict[str, Any]] = []

    async def build_session_memory_context(self, session_id: str) -> str:
        _ = session_id
        return ""

    async def record_user_turn(self, *args, **kwargs):
        _ = (args, kwargs)

    async def append_session_message(self, *args, **kwargs):
        self.messages.append({"args": args, "kwargs": kwargs})
        return type("SavedMessage", (), {"message_id": f"message-{len(self.messages)}"})()

    async def append_session_part(self, *args, **kwargs):
        _ = (args, kwargs)

    async def update_runtime_session_memory(self, **kwargs):
        _ = kwargs
        return {}


class _FinishLLM:
    def __init__(self) -> None:
        self.config = type("Cfg", (), {"max_tokens": 2048})()
        self.seen_messages: list[list[dict[str, Any]]] = []

    def prepare_user_message_content(self, content: str, attachment_refs=None):
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):
        return tools

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    sanitize_tool_call_history = staticmethod(
        LLMProvider.sanitize_tool_call_history
    )

    async def chat_stream(self, messages, tools=None):
        _ = tools
        self.seen_messages.append([dict(item) for item in messages])
        yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
        yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "finished"}, "model": "stub"})()
        yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()


def test_restart_executes_exact_original_tool_call_once_without_llm_regeneration() -> None:
    async def scenario() -> None:
        checkpoint = _tool_checkpoint(status="consuming")
        claim = {
            "claim_id": "claim-1",
            "consumer_id": "recovery",
            "lease_expires_at": "2099-01-01T00:00:00",
        }
        checkpoint.payload["interaction"]["claim"] = claim
        permit = {
            "id": "call-1",
            "function": "dangerous_tool",
            "arguments": {"value": "original"},
            "fingerprint": checkpoint.payload["tool_call"]["fingerprint"],
            "runtime_session_id": "rt-child",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "checkpoint_project_id": checkpoint.project_id,
            "task_id": "worker",
            "claim_id": "claim-1",
            "consumer_id": "recovery",
            "decision": "approve_once",
            "approved": True,
            "state": "ready",
        }
        task = Task(
            id="worker",
            project_id="project-a",
            session_id="worker-session",
            status=TaskStatus.PENDING,
            metadata={"mode": "task", "execution_mode": "task_mode"},
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt-child",
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                }
            },
        )
        store = _ResumeStore(checkpoint)
        await store.save_task(task)
        memory = _ResumeMemory(store)
        llm = _FinishLLM()
        registry = ToolRegistry()
        executed: list[str] = []

        async def dangerous_tool(value: str) -> dict[str, str]:
            executed.append(value)
            return {"value": value}

        registry.register(ToolDefinition(
            name="dangerous_tool",
            description="mutating tool",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            func=dangerous_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            memory_manager=memory,
            config=OPCConfig(),
            max_iterations=2,
            interaction_coordinator=_ResumeCoordinator(store),
        )
        result = await runtime.run(
            system_prompt="system",
            user_message="do not regenerate",
            task=task,
        )
        assert result.status == TaskStatus.DONE
        assert executed == ["original"]
        assert store.finish_calls == 1
        assert len(store.results) == 1
        first_llm_messages = llm.seen_messages[0]
        assert first_llm_messages[-1]["role"] == "tool"
        assert first_llm_messages[-1]["tool_call_id"] == "call-1"
        assert first_llm_messages[-2]["role"] == "assistant"
        assert first_llm_messages[-2]["tool_calls"][0]["id"] == "call-1"
        assert "approved_tool_calls" not in task.context_snapshot["runtime_resume"]

    asyncio.run(scenario())


def test_restart_persists_canonical_denied_exact_tool_result_without_execution() -> None:
    async def scenario() -> None:
        checkpoint = _tool_checkpoint(status="consuming")
        checkpoint.payload["interaction"]["claim"] = {
            "claim_id": "claim-denied-restart",
            "consumer_id": "recovery-denied-restart",
            "lease_expires_at": "2099-01-01T00:00:00",
        }
        tool_call = dict(checkpoint.payload["tool_call"])
        permit = {
            "id": tool_call["id"],
            "function": tool_call["name"],
            "arguments": dict(tool_call["arguments"]),
            "fingerprint": tool_call["fingerprint"],
            "runtime_session_id": "rt-child",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "checkpoint_project_id": checkpoint.project_id,
            "task_id": "worker",
            "claim_id": "claim-denied-restart",
            "consumer_id": "recovery-denied-restart",
            "decision": "deny",
            "approved": False,
            "state": "ready",
        }
        task = Task(
            id="worker",
            project_id="project-a",
            session_id="worker-session",
            status=TaskStatus.PENDING,
            metadata={"mode": "task", "execution_mode": "task_mode"},
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt-child",
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                }
            },
        )
        store = _ResumeStore(checkpoint)
        await store.save_task(task)
        llm = _FinishLLM()
        registry = ToolRegistry()
        executed: list[str] = []

        async def dangerous_tool(value: str) -> dict[str, str]:
            executed.append(value)
            return {"value": value}

        registry.register(ToolDefinition(
            name="dangerous_tool",
            description="mutating tool",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
            func=dangerous_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            memory_manager=_ResumeMemory(store),
            config=OPCConfig(),
            max_iterations=2,
            interaction_coordinator=_ResumeCoordinator(store),
        )

        result = await runtime.run(
            system_prompt="system",
            user_message="do not execute the denied call",
            task=task,
        )

        assert result.status == TaskStatus.DONE
        assert executed == []
        assert store.finish_calls == 1
        assert len(store.results) == 1
        persisted_result = store.results[0]["payload"]
        assert persisted_result == runtime._canonical_exact_tool_call_denial_result(
            permit=permit,
        )
        assert persisted_result["error"] == "The owner denied this exact ToolCall."
        assert persisted_result["success"] is False
        assert persisted_result["approval"]["action"] == "reject"
        assert persisted_result["approval"]["human_reply"] == "deny"
        assert persisted_result["approval"]["approval_checkpoint_id"] == (
            checkpoint.checkpoint_id
        )
        assert persisted_result["approval"]["approved_tool_call_fingerprint"] == (
            permit["fingerprint"]
        )
        assert store.results[0]["metadata"]["permission_decision"][
            "resolution"
        ] == "deny"
        assert llm.seen_messages[0][-1]["role"] == "tool"
        assert llm.seen_messages[0][-1]["tool_call_id"] == permit["id"]
        assert "approved_tool_calls" not in task.context_snapshot["runtime_resume"]

    asyncio.run(scenario())


def test_restart_drains_multiple_keyed_tool_permits_without_overwrite() -> None:
    async def scenario() -> None:
        checkpoints = [
            _tool_checkpoint(status="consuming"),
            _tool_checkpoint(
                checkpoint_id="cp-tool-2",
                status="consuming",
                call_id="call-2",
                arguments={"value": "second"},
            ),
        ]
        permits: dict[str, dict[str, Any]] = {}
        for index, checkpoint in enumerate(checkpoints, start=1):
            checkpoint.payload["interaction"]["claim"] = {
                "claim_id": f"claim-{index}",
                "consumer_id": "recovery",
                "lease_expires_at": "2099-01-01T00:00:00",
            }
            tool_call = checkpoint.payload["tool_call"]
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": "rt-child",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": checkpoint.project_id,
                "task_id": "worker",
                "claim_id": f"claim-{index}",
                "consumer_id": "recovery",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            permits[permit["fingerprint"]] = permit
        task = Task(
            id="worker",
            project_id="project-a",
            session_id="worker-session",
            status=TaskStatus.PENDING,
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt-child",
                    "approved_tool_calls": permits,
                }
            },
        )
        store = _ResumeStore(checkpoints)
        await store.save_task(task)
        memory = _ResumeMemory(store)
        llm = _FinishLLM()
        registry = ToolRegistry()
        executed: list[str] = []

        async def dangerous_tool(value: str) -> dict[str, str]:
            executed.append(value)
            return {"value": value}

        registry.register(ToolDefinition(
            name="dangerous_tool",
            description="mutating tool",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
            func=dangerous_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            memory_manager=memory,
            config=OPCConfig(),
            max_iterations=4,
            interaction_coordinator=_ResumeCoordinator(store),
        )
        result = await runtime.run(
            system_prompt="system",
            user_message="do not regenerate either call",
            task=task,
        )
        assert result.status == TaskStatus.DONE
        assert executed == ["original", "second"]
        assert store.finish_calls == 2
        assert [row["tool_call_id"] for row in store.results] == ["call-1", "call-2"]
        assert len(llm.seen_messages) == 1
        assert "approved_tool_calls" not in task.context_snapshot["runtime_resume"]

    asyncio.run(scenario())


def test_abort_settlement_attempts_every_permit_then_raises_first_failure() -> None:
    async def scenario() -> None:
        checkpoints = [
            _tool_checkpoint(checkpoint_id="cp-abort-1", call_id="abort-1"),
            _tool_checkpoint(checkpoint_id="cp-abort-2", call_id="abort-2"),
        ]
        permits: dict[str, dict[str, Any]] = {}
        for index, checkpoint in enumerate(checkpoints, start=1):
            tool_call = dict(checkpoint.payload["tool_call"])
            permits[tool_call["fingerprint"]] = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": tool_call["runtime_session_id"],
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": checkpoint.project_id,
                "task_id": "abort-worker",
                "claim_id": f"abort-claim-{index}",
                "consumer_id": f"abort-consumer-{index}",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
        task = Task(
            id="abort-worker",
            project_id="project-a",
            session_id="abort-session",
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt-child",
                    "approved_tool_calls": permits,
                }
            },
            metadata={
                "runtime_v2": {
                    "runtime_session_id": "rt-child",
                    "approved_tool_calls": permits,
                }
            },
        )
        store = _ResumeStore(checkpoints)
        await store.save_task(task)

        class PartiallyFailingCoordinator:
            def __init__(self) -> None:
                self.attempted: list[str] = []

            async def settle_interrupted_exact_tool(
                self,
                permit: dict[str, Any],
                **_: Any,
            ) -> ExecutionCheckpointConsumptionReceipt:
                checkpoint_id = str(permit["checkpoint_id"])
                self.attempted.append(checkpoint_id)
                if checkpoint_id == "cp-abort-1":
                    raise RuntimeError("first settlement failed")
                checkpoint = store.checkpoints[checkpoint_id]
                checkpoint.status = "answered"
                return ExecutionCheckpointConsumptionReceipt(
                    outcome="released",
                    checkpoint=checkpoint,
                )

        coordinator = PartiallyFailingCoordinator()
        runtime = NativeRuntimeV2(
            llm=_FinishLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=_ResumeMemory(store),
            config=OPCConfig(),
            interaction_coordinator=coordinator,  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="first settlement failed"):
            await runtime._settle_interrupted_durable_tool_permissions(
                task=task,
                error=RuntimeError("runtime aborted"),
            )
        assert coordinator.attempted == ["cp-abort-1", "cp-abort-2"]
        remaining = runtime._approved_resume_tool_calls(task)
        assert set(remaining) == {checkpoints[0].payload["tool_call"]["fingerprint"]}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("permit_state", "expected_checkpoint_status", "permit_remains"),
    [
        ("executing", "outcome_unknown", False),
        ("result_persisted", "resolved", False),
        ("denied", "answered", False),
        ("unknown_legacy_state", "consuming", True),
    ],
)
def test_native_runtime_metadata_only_nonready_permit_fails_before_llm_or_tool(
    tmp_path: Path,
    permit_state: str,
    expected_checkpoint_status: str,
    permit_remains: bool,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"metadata-{permit_state}-runtime.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            runtime_id = "rt-metadata-executing"
            task_id = "metadata-executing-worker"
            session_id = "metadata-executing-session"
            task = Task(
                id=task_id,
                project_id="project-a",
                session_id=session_id,
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": runtime_id},
                },
                metadata={"runtime_v2": {"runtime_session_id": runtime_id}},
            )
            await store.save_task(task)
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-metadata-executing",
                task_id=task_id,
                session_id=session_id,
                runtime_id=runtime_id,
                call_id="call-metadata-executing",
            )
            permit = await _claim_tool_permission_for_test(
                store,
                checkpoint,
                consumer_id="metadata-executing-consumer",
                claim_id="metadata-executing-claim",
            )
            await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=permit["fingerprint"],
                permit=permit,
            )
            if permit_state in {"executing", "result_persisted"}:
                assert (await coordinator.begin_exact_tool_effect(permit)).acquired
                permit = {**permit, "state": "executing"}
            if permit_state == "result_persisted":
                await store.save_runtime_tool_result(
                    runtime_session_id=runtime_id,
                    task_id=task_id,
                    session_id=session_id,
                    message_id="precommitted-result",
                    tool_call_id=str(permit["id"]),
                    tool_name=str(permit["function"]),
                    payload={"success": True},
                )
                permit = {**permit, "state": "result_persisted"}
                await store.update_task_runtime_tool_permit(
                    task_id,
                    runtime_session_id=runtime_id,
                    fingerprint=permit["fingerprint"],
                    permit=permit,
                )
            elif permit_state not in {"executing"}:
                permit = {**permit, "state": permit_state}
                if permit_state == "denied":
                    await store.update_task_runtime_tool_permit(
                        task_id,
                        runtime_session_id=runtime_id,
                        fingerprint=permit["fingerprint"],
                        permit=permit,
                    )
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={"runtime_session_id": runtime_id},
                runtime_metadata={
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                },
                runtime_mirror={
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {},
                    "audit_note": "non-authoritative empty shadow",
                },
            )
            await store.save_runtime_session(
                runtime_session_id=runtime_id,
                project_id="project-a",
                session_id=session_id,
                task_id=task_id,
                status="running",
                metadata={"resume_marker": {"cursor": 3}},
            )

            executed: list[str] = []

            async def dangerous_tool(value: str) -> dict[str, str]:
                executed.append(value)
                return {"value": value}

            registry = ToolRegistry()
            registry.register(ToolDefinition(
                name="dangerous_tool",
                description="must not replay",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
                func=dangerous_tool,
                requires_confirmation=True,
                concurrency_safe=False,
                read_only=False,
            ))
            llm = _FinishLLM()
            runtime = NativeRuntimeV2(
                llm=llm,
                tool_registry=registry,
                memory_manager=_ResumeMemory(store),  # type: ignore[arg-type]
                config=OPCConfig(),
                interaction_coordinator=coordinator,
            )
            persisted_task = await store.get_task(task_id)
            assert persisted_task is not None
            with pytest.raises(RuntimeError, match="exact recovery|unknown state"):
                await runtime.run(
                    system_prompt="must stop before model",
                    user_message="must not replay effect",
                    task=persisted_task,
                )

            assert llm.seen_messages == []
            assert executed == []
            settled = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert settled is not None
            assert settled.status == expected_checkpoint_status
            ledger = await store.get_task_runtime_tool_ledger(
                task_id,
                project_id="project-a",
            )
            assert ledger is not None
            if permit_remains:
                assert ledger.permits[permit["fingerprint"]]["state"] == permit_state
            else:
                assert ledger.permits == {}
            runtime_row = await store.get_runtime_session(runtime_id)
            assert runtime_row is not None and runtime_row["status"] == "failed"
            assert runtime_row["metadata"]["resume_marker"] == {"cursor": 3}
            assert await store.list_runtime_sessions(
                project_id="project-a",
                status="running",
                task_id=task_id,
            ) == []
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_runtime_refresh_canonicalizes_marker_and_fails_closed_on_active_id_conflict(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "runtime-refresh-canonical.db")
        await store.initialize()
        try:
            task_id = "runtime-refresh-worker"
            await store.save_task(Task(id=task_id, project_id="project-a"))
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={"runtime_session_id": "rt-marker"},
                runtime_metadata={
                    "runtime_session_id": "rt-marker",
                    "continue_after_tool_result": True,
                },
                runtime_mirror={
                    "runtime_session_id": "rt-stale",
                    "approved_tool_calls": {"stale": {"id": "stale"}},
                    "audit_note": "keep",
                },
            )
            runtime = NativeRuntimeV2(
                llm=_FinishLLM(),
                tool_registry=ToolRegistry(),
                memory_manager=_ResumeMemory(store),  # type: ignore[arg-type]
                config=OPCConfig(),
            )
            task = await store.get_task(task_id)
            assert task is not None
            await runtime._refresh_durable_tool_permits(task)
            assert runtime._runtime_session_id(task) == "rt-marker"
            assert task.context_snapshot["runtime_resume"][
                "continue_after_tool_result"
            ] is True
            assert task.metadata["runtime_v2"][
                "continue_after_tool_result"
            ] is True
            assert task.context_snapshot["runtime_v2"] == {
                "runtime_session_id": "rt-marker",
                "audit_note": "keep",
            }

            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={
                    "runtime_session_id": "rt-conflict-a",
                    "continue_after_tool_result": True,
                },
                runtime_metadata={
                    "runtime_session_id": "rt-conflict-b",
                    "continue_after_tool_result": True,
                },
            )
            with pytest.raises(RuntimeError, match="conflicting runtime"):
                await runtime._refresh_durable_tool_permits(task)
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("controller_save", [False, True])
def test_broad_new_task_save_strips_exact_ledger_authority(
    tmp_path: Path,
    controller_save: bool,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"new-task-{controller_save}.db")
        await store.initialize()
        try:
            runtime_id = "rt-broad-new"
            permit = {
                "id": "call-stale",
                "function": "shell",
                "arguments": {"command": "true"},
                "fingerprint": "stale-fingerprint",
                "runtime_session_id": runtime_id,
                "state": "executing",
            }
            metadata: dict[str, Any] = {
                "runtime_v2": {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                    "continue_after_tool_result": True,
                    "ordinary_note": "keep",
                }
            }
            if controller_save:
                metadata.update({
                    "delegation_run_id": "run-broad-new",
                    "company_run_controller_owner_token": "owner-token",
                    "company_run_controller_lease_generation": 1,
                    "claimed_work_item_attempt_seq": 1,
                })
                store._company_controller_task_fence_matches = AsyncMock(  # type: ignore[method-assign]
                    return_value=True
                )
            task = Task(
                id=f"broad-new-{controller_save}",
                project_id="project-a",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": runtime_id,
                        "approved_tool_call": permit,
                        "approved_tool_calls": {permit["fingerprint"]: permit},
                        "continue_after_tool_result": True,
                    },
                    "runtime_v2": {
                        "runtime_session_id": runtime_id,
                        "approved_tool_calls": {permit["fingerprint"]: permit},
                        "continue_after_tool_result": True,
                        "audit_note": "keep",
                    },
                },
                metadata=metadata,
            )
            await store.save_task(task)
            persisted = await store.get_task(task.id)
            assert persisted is not None
            for container in (
                persisted.context_snapshot["runtime_resume"],
                persisted.metadata["runtime_v2"],
                persisted.context_snapshot["runtime_v2"],
            ):
                assert container["runtime_session_id"] == runtime_id
                for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                    assert key not in container
            assert persisted.metadata["runtime_v2"]["ordinary_note"] == "keep"
            assert persisted.context_snapshot["runtime_v2"]["audit_note"] == "keep"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_deleted_task_stale_broad_upsert_cannot_restore_exact_ledger(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "deleted-stale-task.db")
        await store.initialize()
        try:
            runtime_id = "rt-deleted-stale"
            permit = {
                "id": "deleted-call",
                "function": "shell",
                "arguments": {"command": "true"},
                "fingerprint": "deleted-fingerprint",
                "runtime_session_id": runtime_id,
                "state": "executing",
            }
            await store.save_task(Task(
                id="deleted-stale-worker",
                project_id="project-a",
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": runtime_id},
                },
                metadata={"runtime_v2": {"runtime_session_id": runtime_id}},
            ))
            await _raw_seed_task_runtime_views(
                store,
                "deleted-stale-worker",
                runtime_resume={
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                    "continue_after_tool_result": True,
                },
                runtime_metadata={
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                    "continue_after_tool_result": True,
                },
                runtime_mirror={
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                    "continue_after_tool_result": True,
                },
            )
            stale = await store.get_task("deleted-stale-worker")
            assert stale is not None
            await store.hard_delete_task(stale.id)
            await store.save_task(stale)
            restored = await store.get_task(stale.id)
            assert restored is not None
            for container in (
                restored.context_snapshot["runtime_resume"],
                restored.metadata["runtime_v2"],
                restored.context_snapshot["runtime_v2"],
            ):
                for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                    assert key not in container
        finally:
            await store.close()

    asyncio.run(scenario())


def test_exact_remove_cas_rejects_advanced_or_reowned_permit_and_absent_is_noop(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "exact-remove-cas.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            runtime_id = "rt-remove-cas"
            task_id = "remove-cas-worker"
            session_id = "remove-cas-session"
            await store.save_task(Task(
                id=task_id,
                project_id="project-a",
                session_id=session_id,
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": runtime_id},
                },
                metadata={"runtime_v2": {"runtime_session_id": runtime_id}},
            ))
            old_checkpoint = _tool_checkpoint(
                checkpoint_id="cp-remove-old",
                task_id=task_id,
                session_id=session_id,
                runtime_id=runtime_id,
                call_id="same-call",
            )
            ready = await _claim_tool_permission_for_test(
                store,
                old_checkpoint,
                consumer_id="old-consumer",
                claim_id="old-claim",
            )
            ready["state"] = "ready"
            await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=ready["fingerprint"],
                permit=ready,
            )
            assert (await coordinator.begin_exact_tool_effect(ready)).acquired
            executing = {**ready, "state": "executing"}
            await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=ready["fingerprint"],
                permit=executing,
            )
            with pytest.raises(RuntimeError, match="ownership changed"):
                await store.update_task_runtime_tool_permit(
                    task_id,
                    runtime_session_id=runtime_id,
                    fingerprint=ready["fingerprint"],
                    permit=None,
                    expected_permit=ready,
                )
            current = await store.get_task_runtime_tool_ledger(
                task_id,
                project_id="project-a",
            )
            assert current is not None
            assert current.permits[ready["fingerprint"]]["state"] == "executing"

            finished = await coordinator.persist_exact_tool_result(
                executing,
                runtime_session_id=runtime_id,
                tool_name=str(executing["function"]),
                payload={"success": True},
                tool_call_id=str(executing["id"]),
                task_id=task_id,
                session_id=session_id,
                message_id="old-result",
                metadata={"kind": "remove-cas"},
            )
            assert finished.applied
            migrated = await store.get_task(task_id)
            assert migrated is not None
            migrated.context_snapshot["runtime_resume"]["runtime_session_id"] = (
                "rt-remove-new-owner"
            )
            migrated.metadata["runtime_v2"]["runtime_session_id"] = (
                "rt-remove-new-owner"
            )
            await store.save_task(migrated)
            duplicate = await coordinator.persist_exact_tool_result(
                executing,
                runtime_session_id=runtime_id,
                tool_name=str(executing["function"]),
                payload={"success": True},
                tool_call_id=str(executing["id"]),
                task_id=task_id,
                session_id=session_id,
                message_id="old-result-retry",
                metadata={"kind": "remove-cas-retry"},
            )
            assert duplicate.outcome == "duplicate"
            await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=ready["fingerprint"],
                permit=None,
                expected_permit=executing,
            )
            final = await store.get_task_runtime_tool_ledger(
                task_id,
                project_id="project-a",
            )
            assert final is not None
            assert final.runtime_session_id == "rt-remove-new-owner"
            assert final.permits == {}
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_stale_marker_and_continuation_tails_never_reclaim_new_runtime_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "stale-continuation-tail.db")
        await store.initialize()
        try:
            task_id = "stale-tail-worker"
            await store.save_task(Task(
                id=task_id,
                project_id="project-a",
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": "rt-tail-old"},
                },
                metadata={"runtime_v2": {"runtime_session_id": "rt-tail-old"}},
            ))
            await store.set_task_runtime_continuation_marker(
                task_id,
                runtime_session_id="rt-tail-old",
                enabled=True,
            )
            continuation = await store.claim_runtime_tool_continuation(
                project_id="project-a",
                task_id=task_id,
                runtime_session_id="rt-tail-old",
                consumer_id="old-runner",
            )
            assert continuation.acquired
            await store.set_task_runtime_continuation_marker(
                task_id,
                runtime_session_id="rt-tail-old",
                enabled=False,
            )
            migrated = await store.get_task(task_id)
            assert migrated is not None
            migrated.context_snapshot["runtime_resume"]["runtime_session_id"] = (
                "rt-tail-new"
            )
            migrated.metadata["runtime_v2"]["runtime_session_id"] = "rt-tail-new"
            await store.save_task(migrated)

            repeated_clear = await store.set_task_runtime_continuation_marker(
                task_id,
                runtime_session_id="rt-tail-old",
                enabled=False,
            )
            assert repeated_clear.context_snapshot["runtime_resume"][
                "runtime_session_id"
            ] == "rt-tail-new"
            with pytest.raises(RuntimeError, match="conflicts with continuation"):
                await store.set_task_runtime_continuation_marker(
                    task_id,
                    runtime_session_id="rt-tail-old",
                    enabled=True,
                )
            renewed = await store.renew_runtime_tool_continuation(
                project_id="project-a",
                task_id=task_id,
                runtime_session_id="rt-tail-old",
                consumer_id="old-runner",
                claim_id=continuation.claim_id,
            )
            assert renewed.outcome == "conflict"
            finished = await store.finish_runtime_tool_continuation(
                project_id="project-a",
                task_id=task_id,
                runtime_session_id="rt-tail-old",
                consumer_id="old-runner",
                claim_id=continuation.claim_id,
            )
            assert finished.outcome == "conflict"
            final = await store.get_task_runtime_tool_ledger(
                task_id,
                project_id="project-a",
            )
            assert final is not None and final.runtime_session_id == "rt-tail-new"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_startup_metadata_migration_serializes_with_atomic_tool_result(
    tmp_path: Path,
) -> None:
    async def setup(
        migration_store: OPCStore,
        result_store: OPCStore,
    ) -> dict[str, Any]:
        runtime_id = "rt-migration-result"
        task_id = "migration-result-worker"
        session_id = "migration-result-session"
        await migration_store.save_task(Task(
            id=task_id,
            project_id="project-a",
            session_id=session_id,
            context_snapshot={
                "runtime_resume": {"runtime_session_id": runtime_id},
            },
            metadata={
                "work_item_runtime": True,
                "runtime_v2": {"runtime_session_id": runtime_id},
            },
        ))
        checkpoint = _tool_checkpoint(
            checkpoint_id="cp-migration-result",
            task_id=task_id,
            session_id=session_id,
            runtime_id=runtime_id,
            call_id="call-migration-result",
        )
        permit = await _claim_tool_permission_for_test(
            result_store,
            checkpoint,
            consumer_id="migration-consumer",
            claim_id="migration-claim",
        )
        permit["state"] = "ready"
        await result_store.update_task_runtime_tool_permit(
            task_id,
            runtime_session_id=runtime_id,
            fingerprint=permit["fingerprint"],
            permit=permit,
        )
        coordinator = InteractionCoordinator(
            store=result_store,
            project_id="project-a",
        )
        assert (await coordinator.begin_exact_tool_effect(permit)).acquired
        await coordinator.shutdown()
        permit["state"] = "executing"
        await result_store.update_task_runtime_tool_permit(
            task_id,
            runtime_session_id=runtime_id,
            fingerprint=permit["fingerprint"],
            permit=permit,
        )
        return permit

    db_path = tmp_path / "migration-barrier.db"
    migration_store = OPCStore(db_path)
    result_store = OPCStore(db_path)
    asyncio.run(migration_store.initialize())
    asyncio.run(result_store.initialize(run_startup_maintenance=False))
    permit = asyncio.run(setup(migration_store, result_store))
    migration_read = threading.Event()
    allow_migration = threading.Event()
    result_started = threading.Event()
    result_done = threading.Event()
    errors: list[BaseException] = []
    original_cas = migration_store._cas_transform_metadata_row

    async def pausing_cas(**kwargs: Any) -> bool:
        transform = kwargs["transform"]

        def paused_transform(metadata: dict[str, Any]):
            migration_read.set()
            if not allow_migration.wait(timeout=5):
                raise TimeoutError("migration barrier was not released")
            return transform(metadata)

        return await original_cas(**{**kwargs, "transform": paused_transform})

    migration_store._cas_transform_metadata_row = pausing_cas  # type: ignore[method-assign]

    def migrate() -> None:
        try:
            asyncio.run(migration_store._migrate_work_item_runtime_metadata())
        except BaseException as exc:
            errors.append(exc)

    def commit_result() -> None:
        result_started.set()
        try:
            receipt = asyncio.run(
                result_store.persist_runtime_tool_result_and_finish_permission(
                    runtime_session_id=str(permit["runtime_session_id"]),
                    tool_name=str(permit["function"]),
                    payload={"success": True},
                    tool_call_id=str(permit["id"]),
                    task_id=str(permit["task_id"]),
                    session_id="migration-result-session",
                    message_id="migration-result-message",
                    metadata={"kind": "migration-barrier"},
                    fingerprint=str(permit["fingerprint"]),
                    checkpoint_id=str(permit["checkpoint_id"]),
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    claim_id=str(permit["claim_id"]),
                    consumer_id=str(permit["consumer_id"]),
                )
            )
            assert receipt.applied
        except BaseException as exc:
            errors.append(exc)
        finally:
            result_done.set()

    migration_thread = threading.Thread(target=migrate, daemon=True)
    result_thread = threading.Thread(target=commit_result, daemon=True)
    migration_thread.start()
    assert migration_read.wait(timeout=5)
    result_thread.start()
    assert result_started.wait(timeout=2)
    time.sleep(0.1)
    assert not result_done.is_set()
    allow_migration.set()
    migration_thread.join(timeout=5)
    result_thread.join(timeout=5)
    assert not migration_thread.is_alive()
    assert not result_thread.is_alive()
    assert errors == []

    final = asyncio.run(result_store.get_task(str(permit["task_id"])))
    assert final is not None
    assert final.metadata["work_item_runtime_version"] == 1
    for container in (
        final.context_snapshot["runtime_resume"],
        final.metadata["runtime_v2"],
    ):
        assert "approved_tool_calls" not in container
    assert len(asyncio.run(result_store.list_runtime_tool_results(
        str(permit["runtime_session_id"])
    ))) == 1
    asyncio.run(result_store.close())
    asyncio.run(migration_store.close())


def test_atomic_result_rejects_corrupted_legacy_permit_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "corrupt-legacy-result.db")
        await store.initialize()
        try:
            task_id = "corrupt-legacy-worker"
            runtime_id = "rt-corrupt-legacy"
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-corrupt-legacy",
                task_id=task_id,
                runtime_id=runtime_id,
                call_id="call-corrupt-legacy",
            )
            await store.save_task(Task(
                id=task_id,
                project_id="project-a",
                session_id="worker-session",
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": runtime_id},
                },
                metadata={"runtime_v2": {"runtime_session_id": runtime_id}},
            ))
            permit = await _claim_tool_permission_for_test(
                store,
                checkpoint,
                consumer_id="corrupt-consumer",
                claim_id="corrupt-claim",
            )
            await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=permit["fingerprint"],
                permit=permit,
            )
            coordinator = InteractionCoordinator(store=store, project_id="project-a")
            assert (await coordinator.begin_exact_tool_effect(permit)).acquired
            await coordinator.shutdown()
            corrupted = {**permit, "function": "different_tool", "state": "executing"}
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": corrupted,
                },
                runtime_metadata={"runtime_session_id": runtime_id},
            )
            receipt = await store.persist_runtime_tool_result_and_finish_permission(
                runtime_session_id=runtime_id,
                tool_name="dangerous_tool",
                payload={"success": True},
                tool_call_id=str(permit["id"]),
                task_id=task_id,
                session_id="worker-session",
                message_id="corrupt-result",
                metadata={},
                fingerprint=str(permit["fingerprint"]),
                checkpoint_id=str(permit["checkpoint_id"]),
                project_id="project-a",
                checkpoint_type="tool_permission",
                claim_id=str(permit["claim_id"]),
                consumer_id=str(permit["consumer_id"]),
            )
            assert receipt.outcome == "conflict"
            assert await store.list_runtime_tool_results(runtime_id) == []
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None and persisted.status == "consuming"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_two_controllers_elect_one_runtime_for_two_answered_tool_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        stores = [OPCStore(db_path), OPCStore(db_path)]
        await stores[0].initialize()
        await stores[1].initialize()
        engines = [_bare_engine(store) for store in stores]
        for engine in engines:
            engine._INTERACTION_LEASE_SECONDS = 2.0
            engine.approval_engine = _approval_engine(
                engine.store,
                engine.interaction_coordinator,
            )
        task = Task(
            id="worker",
            project_id="project-a",
            session_id="worker-session",
            status=TaskStatus.PENDING,
            context_snapshot={"runtime_resume": {"runtime_session_id": "rt-child"}},
            metadata={"runtime_v2": {"runtime_session_id": "rt-child"}},
        )
        checkpoints = [
            _tool_checkpoint(),
            _tool_checkpoint(
                checkpoint_id="cp-tool-2",
                call_id="call-2",
                arguments={"value": "second"},
            ),
        ]
        await stores[0].save_task(task)
        for index, checkpoint in enumerate(checkpoints, start=1):
            await stores[0].create_owner_interaction_checkpoint(
                checkpoint,
                interaction_key=checkpoint.payload["interaction"]["domain_key"],
            )
            tool_call = checkpoint.payload["tool_call"]
            await stores[0].save_runtime_tool_call(
                runtime_session_id="rt-child",
                task_id="worker",
                session_id="worker-session",
                message_id="assistant-1",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            await stores[0].accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id=f"answer-{index}",
                decision_hash=f"hash-{index}",
                decision={"option_id": "approve_once"},
            )

        active_runtimes = 0
        max_active_runtimes = 0
        runtime_starts = 0
        executed: list[str] = []

        def install_runner(engine: OPCEngine) -> None:
            async def run(tasks: list[Task], use_external: str | None = None) -> str:
                nonlocal active_runtimes, max_active_runtimes, runtime_starts
                _ = use_external
                runtime_starts += 1
                active_runtimes += 1
                max_active_runtimes = max(max_active_runtimes, active_runtimes)
                try:
                    persisted = None
                    for _ in range(200):
                        persisted = await engine.store.get_task(tasks[0].id)
                        permits = dict(
                            dict(
                                dict(persisted.context_snapshot or {}).get(
                                    "runtime_resume", {}
                                )
                                or {}
                            ).get("approved_tool_calls", {})
                            or {}
                        )
                        if len(permits) == 2:
                            break
                        await asyncio.sleep(0.005)
                    assert persisted is not None and len(permits) == 2
                    for fingerprint, permit in sorted(
                        permits.items(), key=lambda item: str(item[1].get("id", ""))
                    ):
                        call_id = str(permit["id"])
                        executed.append(call_id)
                        await engine.store.save_runtime_tool_result(
                            runtime_session_id="rt-child",
                            task_id="worker",
                            session_id="worker-session",
                            message_id=f"result-{call_id}",
                            tool_call_id=call_id,
                            tool_name=str(permit["function"]),
                            payload={"success": True},
                        )
                        finish = await engine.store.finish_execution_checkpoint_consumption(
                            str(permit["checkpoint_id"]),
                            project_id="project-a",
                            checkpoint_type="tool_permission",
                            claim_id=str(permit["claim_id"]),
                            consumer_id=str(permit["consumer_id"]),
                            final_status="resolved",
                        )
                        assert finish.applied
                        await engine.store.update_task_runtime_tool_permit(
                            "worker",
                            runtime_session_id="rt-child",
                            fingerprint=fingerprint,
                            permit=None,
                            expected_permit=permit,
                        )
                    return "resumed"
                finally:
                    active_runtimes -= 1

            engine._execute_single_agent = run  # type: ignore[method-assign]

        for engine in engines:
            install_runner(engine)
        try:
            await asyncio.gather(
                engines[0]._consume_answered_interaction(
                    checkpoints[0].checkpoint_id, "tool_permission"
                ),
                engines[1]._consume_answered_interaction(
                    checkpoints[1].checkpoint_id, "tool_permission"
                ),
            )
            rows = await stores[0].get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["tool_permission"],
            )
            assert runtime_starts == 1
            assert max_active_runtimes == 1
            assert executed == ["call-1", "call-2"]
            assert {row.status for row in rows} == {"resolved"}
            final_task = await stores[0].get_task("worker")
            assert final_task is not None
            assert "approved_tool_calls" not in final_task.context_snapshot[
                "runtime_resume"
            ]
        finally:
            for engine in engines:
                await engine.interaction_coordinator.shutdown()
            await stores[1].close()
            await stores[0].close()

    asyncio.run(scenario())


def test_expired_executing_continuation_is_not_reclaimed_by_second_store(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first = OPCStore(db_path)
        second = OPCStore(db_path)
        await first.initialize()
        await second.initialize()
        try:
            checkpoint = _tool_checkpoint()
            tool_call = checkpoint.payload["tool_call"]
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": "rt-child",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": "project-a",
                "task_id": "worker",
                "claim_id": "tool-claim",
                "consumer_id": "tool-controller",
                "approved": True,
                "decision": "approve_once",
                "state": "ready",
            }
            task = Task(
                id="worker",
                project_id="project-a",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": "rt-child",
                    }
                },
                metadata={
                    "runtime_v2": {
                        "runtime_session_id": "rt-child",
                    }
                },
            )
            await first.save_task(task)
            await first.create_owner_interaction_checkpoint(
                checkpoint,
                interaction_key=checkpoint.payload["interaction"]["domain_key"],
            )
            await first.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="tool-answer",
                decision_hash="tool-answer-hash",
                decision={"option_id": "approve_once"},
            )
            tool_claim = await first.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="tool-controller",
                claim_id="tool-claim",
                lease_seconds=1.0,
            )
            assert tool_claim.acquired
            await first.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id="rt-child",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            assert (await first.begin_execution_checkpoint_effect(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="tool-controller",
                claim_id="tool-claim",
            )).acquired
            permit = {**permit, "state": "executing"}
            await first.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id="rt-child",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            claim = await first.claim_runtime_tool_continuation(
                project_id="project-a",
                task_id="worker",
                runtime_session_id="rt-child",
                consumer_id="controller-a",
                lease_seconds=0.1,
            )
            assert claim.acquired
            started = await first.begin_runtime_tool_continuation(
                project_id="project-a",
                task_id="worker",
                runtime_session_id="rt-child",
                consumer_id="controller-a",
                claim_id=claim.claim_id,
            )
            assert started.acquired
            assert first._db is not None
            assert not first._db._conn.in_transaction
            assert second._db is not None
            assert not second._db._conn.in_transaction

            async def delayed_second_claim():
                await asyncio.sleep(0.15)
                return await second.claim_runtime_tool_continuation(
                    project_id="project-a",
                    task_id="worker",
                    runtime_session_id="rt-child",
                    consumer_id="controller-b",
                    lease_seconds=0.1,
                )

            contender = asyncio.create_task(delayed_second_claim())
            # Let the continuation lease expire while the exact external
            # effect remains durably fenced as executing.
            await asyncio.sleep(0.25)
            receipt = await contender
            assert receipt.outcome == "busy"
            assert receipt.consumer_id == "controller-a"
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_expired_continuation_is_reclaimed_when_interaction_never_started_effect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first = OPCStore(db_path)
        second = OPCStore(db_path)
        await first.initialize()
        await second.initialize()
        try:
            checkpoint = _tool_checkpoint()
            await _publish_owner_checkpoint(first, checkpoint)
            await first.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="answer-old",
                decision_hash="answer-old-hash",
                decision={"option_id": "approve_once"},
            )
            old_claim = await first.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="old-interaction-controller",
                claim_id="old-interaction-claim",
                lease_seconds=1.0,
            )
            assert old_claim.acquired

            tool_call = checkpoint.payload["tool_call"]
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": "rt-child",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": "project-a",
                "task_id": "worker",
                "claim_id": "old-interaction-claim",
                "consumer_id": "old-interaction-controller",
                "approved": True,
                "decision": "approve_once",
                "state": "ready",
            }
            await first.save_task(Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": "rt-child",
                    }
                },
                metadata={
                    "runtime_v2": {
                        "runtime_session_id": "rt-child",
                    }
                },
            ))
            await first.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id="rt-child",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            continuation = await first.claim_runtime_tool_continuation(
                project_id="project-a",
                task_id="worker",
                runtime_session_id="rt-child",
                consumer_id="old-runtime-controller",
                lease_seconds=0.1,
            )
            assert continuation.acquired
            assert (
                await first.begin_runtime_tool_continuation(
                    project_id="project-a",
                    task_id="worker",
                    runtime_session_id="rt-child",
                    consumer_id="old-runtime-controller",
                    claim_id=continuation.claim_id,
                )
            ).acquired

            released = await first.release_execution_checkpoint_claim(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                claim_id="old-interaction-claim",
                consumer_id="old-interaction-controller",
                reason="runner_stopped_before_effect",
            )
            assert released.applied
            new_claim = await first.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="new-interaction-controller",
                claim_id="new-interaction-claim",
                lease_seconds=1.0,
            )
            assert new_claim.acquired
            assert new_claim.checkpoint is not None
            assert new_claim.checkpoint.payload["interaction"]["execution"][
                "state"
            ] == "ready"

            await asyncio.sleep(0.12)
            reclaimed = await second.claim_runtime_tool_continuation(
                project_id="project-a",
                task_id="worker",
                runtime_session_id="rt-child",
                consumer_id="new-runtime-controller",
                lease_seconds=0.1,
            )
            assert reclaimed.outcome == "reclaimed"
            assert reclaimed.consumer_id == "new-runtime-controller"
            assert reclaimed.claim_id != continuation.claim_id
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("permit_view", ["resume", "metadata"])
def test_expired_executing_permission_reconciles_committed_tool_result(
    tmp_path: Path,
    permit_view: str,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"{permit_view}.db")
        await store.initialize()
        try:
            checkpoint = _tool_checkpoint()
            await store.create_owner_interaction_checkpoint(
                checkpoint,
                interaction_key=checkpoint.payload["interaction"]["domain_key"],
            )
            tool_call = checkpoint.payload["tool_call"]
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": tool_call["runtime_session_id"],
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": checkpoint.project_id,
                "task_id": "worker",
                "claim_id": "old-claim",
                "consumer_id": "old-controller",
                "approved": True,
                "decision": "approve_once",
                "state": "ready",
            }
            await store.save_task(Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": "rt-child",
                    }
                },
                metadata={
                    "runtime_v2": {
                        "runtime_session_id": "rt-child",
                    }
                },
            ))
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="answer",
                decision_hash="answer-hash",
                decision={"option_id": "approve_once"},
            )
            old_claim = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="old-controller",
                claim_id="old-claim",
                lease_seconds=0.05,
            )
            assert old_claim.acquired
            await store.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id="rt-child",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            assert (await store.begin_execution_checkpoint_effect(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="old-controller",
                claim_id="old-claim",
            )).acquired
            permit = {**permit, "state": "executing"}
            await store.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id="rt-child",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            await _raw_seed_task_runtime_views(
                store,
                "worker",
                runtime_resume={
                    "runtime_session_id": "rt-child",
                    **(
                        {
                            "approved_tool_calls": {
                                tool_call["fingerprint"]: permit,
                            }
                        }
                        if permit_view == "resume"
                        else {}
                    ),
                },
                runtime_metadata={
                    "runtime_session_id": "rt-child",
                    **(
                        {
                            "approved_tool_calls": {
                                tool_call["fingerprint"]: permit,
                            }
                        }
                        if permit_view == "metadata"
                        else {}
                    ),
                },
            )
            canonical = await store.get_task_runtime_tool_ledger(
                "worker",
                project_id="project-a",
            )
            assert canonical is not None
            assert canonical.permits[tool_call["fingerprint"]] == permit
            await store.save_runtime_tool_call(
                runtime_session_id="rt-child",
                task_id="worker",
                session_id="worker-session",
                message_id="assistant-message",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            await store.save_runtime_tool_result(
                runtime_session_id="rt-child",
                task_id="worker",
                session_id="worker-session",
                message_id="result-message",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                payload={"success": True},
            )
            await asyncio.sleep(0.08)
            recovered = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="new-controller",
            )
            assert recovered.outcome == "reclaimed"
            assert recovered.checkpoint is not None
            assert recovered.checkpoint.status == "consuming"
            assert recovered.checkpoint.payload["approval_result"][
                "tool_result_persisted"
            ] is True
            engine = _bare_engine(store)
            engine.approval_engine = _approval_engine(
                store, engine.interaction_coordinator
            )
            async def complete_native_resume(*_args: Any, **_kwargs: Any) -> str:
                # NativeRuntimeV2 consumes this entry marker before it resumes.
                # Preserve that production contract even though this test mocks
                # the runtime body itself.
                await store.set_task_runtime_continuation_marker(
                    "worker",
                    runtime_session_id="rt-child",
                    enabled=False,
                )
                return "continued after persisted result"

            engine._execute_single_agent = AsyncMock(
                side_effect=complete_native_resume
            )
            outcome = await engine._resume_permission_checkpoint(
                InteractionDecisionLease(
                    checkpoint=recovered.checkpoint,
                    decision={"option_id": "approve_once"},
                    consumer_id="new-controller",
                    claim_id=recovered.claim_id,
                )
            )
            assert outcome.runtime_owns_completion is True
            assert engine._execute_single_agent.await_count == 1
            assert len(await store.list_runtime_tool_results("rt-child")) == 1
            resolved = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert resolved is not None and resolved.status == "resolved"
            persisted_task = await store.get_task("worker")
            assert persisted_task is not None
            assert "approved_tool_calls" not in persisted_task.context_snapshot[
                "runtime_resume"
            ]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_atomic_tool_result_completion_never_readds_permit_or_refinishes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        coordinator = InteractionCoordinator(
            store=store,
            project_id="project-a",
        )
        try:
            checkpoint = _tool_checkpoint()
            tool_call = dict(checkpoint.payload["tool_call"])
            await store.create_owner_interaction_checkpoint(
                checkpoint,
                interaction_key=checkpoint.payload["interaction"]["domain_key"],
            )
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="answer",
                decision_hash="answer-hash",
                decision={"option_id": "approve_once"},
            )
            claimed = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="controller-a",
                claim_id="claim-a",
            )
            assert claimed.acquired
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": tool_call["runtime_session_id"],
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": "worker",
                "claim_id": "claim-a",
                "consumer_id": "controller-a",
                "approved": True,
                "decision": "approve_once",
                "state": "ready",
            }
            task = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": tool_call["runtime_session_id"],
                    },
                    # NativeRuntimeV2._persist_task_ledger historically
                    # mirrored metadata.runtime_v2 wholesale here.
                    "runtime_v2": {
                        "runtime_session_id": tool_call["runtime_session_id"],
                    },
                },
                metadata={
                    "runtime_v2": {
                        "runtime_session_id": tool_call["runtime_session_id"],
                    }
                },
            )
            await store.save_task(task)
            await store.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id=tool_call["runtime_session_id"],
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            assert (
                await coordinator.begin_exact_tool_effect(permit)
            ).acquired
            permit = {**permit, "state": "executing"}
            await store.update_task_runtime_tool_permit(
                "worker",
                runtime_session_id=tool_call["runtime_session_id"],
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )

            first = await coordinator.persist_exact_tool_result(
                permit,
                runtime_session_id=tool_call["runtime_session_id"],
                tool_name=tool_call["name"],
                payload={"success": True, "result": {"value": "done"}},
                tool_call_id=tool_call["id"],
                task_id="worker",
                session_id="worker-session",
                message_id="tool-result-message",
                metadata={"kind": "test"},
                checkpoint_payload_patch={
                    "approval_result": {"tool_result_persisted": True}
                },
            )
            assert first.outcome == "finished"

            persisted_task = await store.get_task("worker")
            assert persisted_task is not None
            assert "approved_tool_calls" not in persisted_task.context_snapshot[
                "runtime_resume"
            ]
            assert "approved_tool_calls" not in persisted_task.context_snapshot[
                "runtime_v2"
            ]
            persisted_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted_checkpoint is not None
            assert persisted_checkpoint.status == "resolved"
            assert len(
                await store.list_runtime_tool_results(
                    tool_call["runtime_session_id"]
                )
            ) == 1

            duplicate = await coordinator.persist_exact_tool_result(
                permit,
                runtime_session_id=tool_call["runtime_session_id"],
                tool_name=tool_call["name"],
                payload={"success": True, "result": {"value": "done"}},
                tool_call_id=tool_call["id"],
                task_id="worker",
                session_id="worker-session",
                message_id="tool-result-message",
                metadata={"kind": "test"},
            )
            assert duplicate.outcome == "duplicate"
            assert len(
                await store.list_runtime_tool_results(
                    tool_call["runtime_session_id"]
                )
            ) == 1
            persisted_task = await store.get_task("worker")
            assert persisted_task is not None
            assert "approved_tool_calls" not in persisted_task.context_snapshot[
                "runtime_resume"
            ]
            assert "approved_tool_calls" not in persisted_task.context_snapshot[
                "runtime_v2"
            ]
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_ready_permit_claim_rotation_is_allowed_before_effect(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "claim-rotation.db")
        await store.initialize()
        try:
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-claim-rotation",
                task_id="claim-rotation-worker",
                session_id="claim-rotation-session",
                runtime_id="rt-claim-rotation",
                call_id="call-claim-rotation",
            )
            await store.save_task(
                Task(
                    id="claim-rotation-worker",
                    project_id="project-a",
                    session_id="claim-rotation-session",
                )
            )
            await _publish_owner_checkpoint(store, checkpoint)
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="claim-rotation-answer",
                decision_hash="claim-rotation-hash",
                decision={"option_id": "approve_once"},
            )
            old_claim = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="old-consumer",
                claim_id="old-claim",
                lease_seconds=0.05,
            )
            assert old_claim.acquired
            tool_call = dict(checkpoint.payload["tool_call"])
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": tool_call["runtime_session_id"],
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": checkpoint.project_id,
                "task_id": checkpoint.task_id,
                "claim_id": "old-claim",
                "consumer_id": "old-consumer",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            await store.update_task_runtime_tool_permit(
                "claim-rotation-worker",
                runtime_session_id="rt-claim-rotation",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )

            await asyncio.sleep(0.08)
            new_claim = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="new-consumer",
                claim_id="new-claim",
            )
            assert new_claim.outcome == "reclaimed"
            rotated = await store.update_task_runtime_tool_permit(
                "claim-rotation-worker",
                runtime_session_id="rt-claim-rotation",
                fingerprint=tool_call["fingerprint"],
                permit={
                    **permit,
                    "claim_id": "new-claim",
                    "consumer_id": "new-consumer",
                },
            )
            persisted_permit = rotated.context_snapshot["runtime_resume"][
                "approved_tool_calls"
            ][tool_call["fingerprint"]]
            assert persisted_permit["claim_id"] == "new-claim"
            assert persisted_permit["consumer_id"] == "new-consumer"
            assert persisted_permit["state"] == "ready"
            with pytest.raises(RuntimeError, match="ownership changed"):
                await store.update_task_runtime_tool_permit(
                    "claim-rotation-worker",
                    runtime_session_id="rt-claim-rotation",
                    fingerprint=tool_call["fingerprint"],
                    permit=None,
                    expected_permit=permit,
                )
            retained = await store.get_task_runtime_tool_ledger(
                "claim-rotation-worker",
                project_id="project-a",
            )
            assert retained is not None
            assert retained.permits[tool_call["fingerprint"]]["claim_id"] == (
                "new-claim"
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_exact_permit_state_is_monotonic_and_terminal_result_cannot_resurrect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "permit-monotonic.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-monotonic",
                task_id="monotonic-worker",
                session_id="monotonic-session",
                runtime_id="rt-monotonic",
                call_id="call-monotonic",
            )
            await store.save_task(
                Task(
                    id="monotonic-worker",
                    project_id="project-a",
                    session_id="monotonic-session",
                )
            )
            ready = await _claim_tool_permission_for_test(
                store,
                checkpoint,
                consumer_id="monotonic-consumer",
                claim_id="monotonic-claim",
            )
            ready["state"] = "ready"
            await store.update_task_runtime_tool_permit(
                "monotonic-worker",
                runtime_session_id="rt-monotonic",
                fingerprint=ready["fingerprint"],
                permit=ready,
            )
            started = await coordinator.begin_exact_tool_effect(ready)
            assert started.outcome == "started" and started.acquired
            executing = {**ready, "state": "executing"}
            atomic_ledger = await store.get_task_runtime_tool_ledger(
                "monotonic-worker",
                project_id="project-a",
            )
            assert atomic_ledger is not None
            assert atomic_ledger.permits[ready["fingerprint"]] == executing

            duplicate_effect = await coordinator.begin_exact_tool_effect(ready)
            assert duplicate_effect.outcome == "invalid_state"
            assert duplicate_effect.acquired is False
            with pytest.raises(RuntimeError, match="cannot move backward"):
                await store.update_task_runtime_tool_permit(
                    "monotonic-worker",
                    runtime_session_id="rt-monotonic",
                    fingerprint=ready["fingerprint"],
                    permit=ready,
                )
            ledger = await store.get_task_runtime_tool_ledger(
                "monotonic-worker",
                project_id="project-a",
            )
            assert ledger is not None
            assert ledger.permits[ready["fingerprint"]]["state"] == "executing"

            completed = await coordinator.persist_exact_tool_result(
                executing,
                runtime_session_id="rt-monotonic",
                tool_name=str(executing["function"]),
                payload={"success": True, "result": {"ok": True}},
                tool_call_id=str(executing["id"]),
                task_id="monotonic-worker",
                session_id="monotonic-session",
                message_id="monotonic-result",
                metadata={"kind": "monotonic-regression"},
            )
            assert completed.outcome == "finished"
            with pytest.raises(
                RuntimeError,
                match="not actively consuming|after its result",
            ):
                await store.update_task_runtime_tool_permit(
                    "monotonic-worker",
                    runtime_session_id="rt-monotonic",
                    fingerprint=ready["fingerprint"],
                    permit=ready,
                )
            final_ledger = await store.get_task_runtime_tool_ledger(
                "monotonic-worker",
                project_id="project-a",
            )
            assert final_ledger is not None and final_ledger.permits == {}
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_exact_effect_fence_rejects_denied_permission_atomically(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "denied-effect-fence.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-denied-effect",
                task_id="denied-effect-worker",
                session_id="denied-effect-session",
                runtime_id="rt-denied-effect",
                call_id="call-denied-effect",
            )
            await store.save_task(Task(
                id="denied-effect-worker",
                project_id="project-a",
                session_id="denied-effect-session",
            ))
            await _publish_owner_checkpoint(store, checkpoint)
            accepted = await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="deny-answer",
                decision_hash="deny-hash",
                decision={"option_id": "deny"},
            )
            assert accepted.acknowledged
            claim = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="deny-consumer",
                claim_id="deny-claim",
            )
            assert claim.acquired
            tool_call = dict(checkpoint.payload["tool_call"])
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": tool_call["runtime_session_id"],
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": "denied-effect-worker",
                "claim_id": "deny-claim",
                "consumer_id": "deny-consumer",
                "decision": "deny",
                "approved": False,
                "state": "ready",
            }
            await store.update_task_runtime_tool_permit(
                "denied-effect-worker",
                runtime_session_id="rt-denied-effect",
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            receipt = await coordinator.begin_exact_tool_effect(permit)
            assert receipt.outcome == "invalid_state"
            assert not receipt.acquired
            ledger = await store.get_task_runtime_tool_ledger(
                "denied-effect-worker",
                project_id="project-a",
            )
            assert ledger is not None
            assert ledger.permits[tool_call["fingerprint"]]["state"] == "ready"
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None
            assert persisted.payload["interaction"]["execution"]["state"] == "ready"
            assert await store.list_runtime_tool_results("rt-denied-effect") == []
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("accepted_decision", "forged_decision", "forged_approved"),
    [
        ("deny", "approve_once", True),
        ("approve_once", "deny", False),
    ],
)
def test_exact_effect_fence_rejects_raw_permit_with_forged_decision(
    tmp_path: Path,
    accepted_decision: str,
    forged_decision: str,
    forged_approved: bool,
) -> None:
    async def scenario() -> None:
        store = OPCStore(
            tmp_path / f"forged-{accepted_decision}-{forged_decision}.db"
        )
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            task_id = f"forged-{accepted_decision}-worker"
            runtime_id = f"rt-forged-{accepted_decision}"
            checkpoint = _tool_checkpoint(
                checkpoint_id=f"cp-forged-{accepted_decision}",
                task_id=task_id,
                session_id=f"session-forged-{accepted_decision}",
                runtime_id=runtime_id,
                call_id=f"call-forged-{accepted_decision}",
            )
            await store.save_task(Task(
                id=task_id,
                project_id="project-a",
                session_id=checkpoint.session_id,
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": runtime_id},
                },
                metadata={"runtime_v2": {"runtime_session_id": runtime_id}},
            ))
            await _publish_owner_checkpoint(store, checkpoint)
            accepted = await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id=f"answer-{accepted_decision}",
                decision_hash=f"hash-{accepted_decision}",
                decision={"option_id": accepted_decision},
            )
            assert accepted.acknowledged
            claimed = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="forged-consumer",
                claim_id="forged-claim",
            )
            assert claimed.acquired
            tool_call = dict(checkpoint.payload["tool_call"])
            forged = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": runtime_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": task_id,
                "claim_id": "forged-claim",
                "consumer_id": "forged-consumer",
                "decision": forged_decision,
                "approved": forged_approved,
                "state": "ready",
            }
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={"runtime_session_id": runtime_id},
                runtime_metadata={
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": forged,
                },
            )
            receipt = await coordinator.begin_exact_tool_effect(forged)
            assert receipt.outcome == "invalid_state"
            assert not receipt.acquired
            ledger = await store.get_task_runtime_tool_ledger(
                task_id,
                project_id="project-a",
            )
            assert ledger is not None
            assert ledger.permits[tool_call["fingerprint"]] == forged
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None
            assert persisted.payload["interaction"]["execution"]["state"] == "ready"
            assert await store.list_runtime_tool_results(runtime_id) == []
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_generic_stale_task_save_cannot_delete_or_resurrect_store_permit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        projection_store = OPCStore(db_path)
        permit_store = OPCStore(db_path)
        await projection_store.initialize()
        await permit_store.initialize(run_startup_maintenance=False)
        try:
            runtime_id = "rt-generic-worker"
            task = Task(
                id="generic-worker",
                project_id="project-a",
                session_id="generic-session",
                metadata={
                    "runtime_v2": {"runtime_session_id": runtime_id},
                },
            )
            await projection_store.save_task(task)
            stale = await projection_store.get_task(task.id)
            assert stale is not None
            fingerprint = "exact-fingerprint"
            checkpoint = _tool_checkpoint(
                checkpoint_id="cp-generic",
                task_id=task.id,
                session_id=task.session_id or "generic-session",
                runtime_id=runtime_id,
                call_id="call-generic",
                name="shell",
                arguments={"command": "python3 -m json.tool result.json"},
            )
            await _publish_owner_checkpoint(permit_store, checkpoint)
            await permit_store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="generic-answer",
                decision_hash="generic-answer-hash",
                decision={"option_id": "approve_once"},
            )
            claimed = await permit_store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="consumer-generic",
                claim_id="claim-generic",
            )
            assert claimed.acquired
            fingerprint = checkpoint.payload["tool_call"]["fingerprint"]
            permit = {
                "id": "call-generic",
                "function": "shell",
                "arguments": {"command": "python3 -m json.tool result.json"},
                "fingerprint": fingerprint,
                "runtime_session_id": runtime_id,
                "checkpoint_id": "cp-generic",
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": task.id,
                "claim_id": "claim-generic",
                "consumer_id": "consumer-generic",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            await permit_store.update_task_runtime_tool_permit(
                task.id,
                runtime_session_id=runtime_id,
                fingerprint=fingerprint,
                permit=permit,
            )

            stale.metadata["projection_note"] = "saved after permit"
            await projection_store.save_task(stale)
            with_permit = await permit_store.get_task(task.id)
            assert with_permit is not None
            assert with_permit.context_snapshot["runtime_resume"][
                "approved_tool_calls"
            ][fingerprint]["id"] == "call-generic"
            assert with_permit.metadata["runtime_v2"]["approved_tool_calls"][
                fingerprint
            ]["id"] == "call-generic"
            assert "approved_tool_calls" not in with_permit.context_snapshot.get(
                "runtime_v2", {}
            )

            await permit_store.update_task_runtime_tool_permit(
                task.id,
                runtime_session_id=runtime_id,
                fingerprint=fingerprint,
                permit=None,
                expected_permit=permit,
            )
            # ``save_task`` merged the permit into the caller's stale object;
            # after Store consumption the same object must not re-add it.
            assert "approved_tool_calls" in stale.context_snapshot[
                "runtime_resume"
            ]
            # Match NativeRuntimeV2._persist_task_ledger's legacy mirror:
            # it copies metadata.runtime_v2 wholesale into this context key.
            stale.context_snapshot["runtime_v2"] = dict(
                stale.metadata["runtime_v2"]
            )
            assert "approved_tool_calls" in stale.context_snapshot["runtime_v2"]
            stale.metadata["projection_note"] = "saved after result"
            await projection_store.save_task(stale)
            without_permit = await permit_store.get_task(task.id)
            assert without_permit is not None
            assert "approved_tool_calls" not in without_permit.context_snapshot[
                "runtime_resume"
            ]
            assert "approved_tool_calls" not in without_permit.metadata[
                "runtime_v2"
            ]
            assert "approved_tool_calls" not in without_permit.context_snapshot[
                "runtime_v2"
            ]
            assert without_permit.metadata["projection_note"] == (
                "saved after result"
            )
        finally:
            await permit_store.close()
            await projection_store.close()

    asyncio.run(scenario())


def test_inactive_runtime_id_can_be_replaced_before_exact_permit(
    tmp_path: Path,
) -> None:
    """A completed runtime id is audit state, not a permanent Task owner."""

    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        projection_store = OPCStore(db_path)
        interaction_store = OPCStore(db_path)
        await projection_store.initialize()
        await interaction_store.initialize(run_startup_maintenance=False)
        coordinator = InteractionCoordinator(
            store=interaction_store,
            project_id="project-a",
        )
        try:
            old_runtime_id = "rt-finished-old"
            new_runtime_id = "rt-current-new"
            task = Task(
                id="runtime-replacement-worker",
                project_id="project-a",
                session_id="runtime-replacement-session",
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": old_runtime_id},
                    # This legacy audit mirror is explicitly non-authoritative.
                    "runtime_v2": {
                        "runtime_session_id": old_runtime_id,
                        "approved_tool_call": None,
                        "approved_tool_calls": {},
                        "continue_after_tool_result": False,
                    },
                },
                metadata={
                    "runtime_v2": {"runtime_session_id": old_runtime_id},
                },
            )
            await projection_store.save_task(task)

            checkpoint = _tool_checkpoint(
                checkpoint_id="runtime-replacement-permission",
                task_id=task.id,
                session_id=task.session_id or "",
                runtime_id=new_runtime_id,
                call_id="call-runtime-replacement",
                name="shell",
                arguments={"command": "python3 -m json.tool result.json"},
            )
            await _publish_owner_checkpoint(interaction_store, checkpoint)
            accepted = await interaction_store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="approve-runtime-replacement",
                decision_hash="approve-runtime-replacement-hash",
                decision={"option_id": "approve_once"},
            )
            assert accepted.acknowledged
            claimed = await interaction_store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="runtime-replacement-consumer",
                claim_id="runtime-replacement-claim",
            )
            assert claimed.acquired

            tool_call = dict(checkpoint.payload["tool_call"])
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": new_runtime_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": "project-a",
                "task_id": task.id,
                "claim_id": "runtime-replacement-claim",
                "consumer_id": "runtime-replacement-consumer",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }

            # No active authoritative ledger remains for the old runtime, so a
            # genuinely new native runtime may take over this Task.
            replaced = await interaction_store.update_task_runtime_tool_permit(
                task.id,
                runtime_session_id=new_runtime_id,
                fingerprint=tool_call["fingerprint"],
                permit=permit,
            )
            assert replaced.context_snapshot["runtime_resume"][
                "runtime_session_id"
            ] == new_runtime_id
            assert replaced.metadata["runtime_v2"][
                "runtime_session_id"
            ] == new_runtime_id
            assert replaced.context_snapshot["runtime_v2"][
                "runtime_session_id"
            ] == new_runtime_id
            for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                assert key not in replaced.context_snapshot["runtime_v2"]

            # Once the new permit exists it is an ownership fence: another
            # runtime cannot replace it, while the same runtime can advance it.
            mismatched = {**permit, "runtime_session_id": "rt-conflicting"}
            with pytest.raises(
                RuntimeError,
                match="runtime session conflicts with exact ToolCall permit",
            ):
                await interaction_store.update_task_runtime_tool_permit(
                    task.id,
                    runtime_session_id="rt-conflicting",
                    fingerprint="conflicting-fingerprint",
                    permit=mismatched,
                )
            executing_permit = {**permit, "state": "executing"}
            assert (
                await coordinator.begin_exact_tool_effect(permit)
            ).acquired
            same_runtime = await interaction_store.update_task_runtime_tool_permit(
                task.id,
                runtime_session_id=new_runtime_id,
                fingerprint=tool_call["fingerprint"],
                permit=executing_permit,
            )
            assert same_runtime.context_snapshot["runtime_resume"][
                "approved_tool_calls"
            ][tool_call["fingerprint"]]["state"] == "executing"
            stale_after_permit = await projection_store.get_task(task.id)
            assert stale_after_permit is not None

            await interaction_store.save_runtime_tool_call(
                runtime_session_id=new_runtime_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id="runtime-replacement-assistant",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )
            completion = await coordinator.persist_exact_tool_result(
                executing_permit,
                runtime_session_id=new_runtime_id,
                tool_name=tool_call["name"],
                payload={"success": True, "result": {"exit_code": 0}},
                tool_call_id=tool_call["id"],
                task_id=task.id,
                session_id=task.session_id,
                message_id="runtime-replacement-result",
                metadata={"kind": "runtime-replacement-regression"},
                checkpoint_payload_patch={
                    "approval_result": {"tool_result_persisted": True}
                },
            )
            assert completion.outcome == "finished"

            # Model the native ledger tail-save, including its obsolete audit
            # mirror. The atomic result is final: this save may retain ordinary
            # metadata but cannot resurrect the consumed permit anywhere.
            stale_after_permit.context_snapshot["runtime_v2"] = dict(
                stale_after_permit.metadata["runtime_v2"]
            )
            stale_after_permit.metadata["post_result_tail_save"] = True
            await projection_store.save_task(stale_after_permit)
            final_task = await interaction_store.get_task(task.id)
            assert final_task is not None
            assert final_task.metadata["post_result_tail_save"] is True
            assert final_task.context_snapshot["runtime_resume"][
                "runtime_session_id"
            ] == new_runtime_id
            assert final_task.metadata["runtime_v2"][
                "runtime_session_id"
            ] == new_runtime_id
            for container in (
                final_task.context_snapshot["runtime_resume"],
                final_task.metadata["runtime_v2"],
                final_task.context_snapshot["runtime_v2"],
            ):
                assert "approved_tool_call" not in container
                assert "approved_tool_calls" not in container
                assert "continue_after_tool_result" not in container
            assert len(
                await interaction_store.list_runtime_tool_results(new_runtime_id)
            ) == 1
        finally:
            await coordinator.shutdown()
            await interaction_store.close()
            await projection_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("ledger_shape", ["metadata_plural", "resume_singular"])
def test_atomic_tool_result_canonicalizes_one_sided_or_legacy_permit(
    tmp_path: Path,
    ledger_shape: str,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"{ledger_shape}.db")
        await store.initialize()
        coordinator = InteractionCoordinator(
            store=store,
            project_id="project-a",
        )
        try:
            runtime_id = f"rt-{ledger_shape}"
            task_id = f"worker-{ledger_shape}"
            checkpoint = _tool_checkpoint(
                checkpoint_id=f"cp-{ledger_shape}",
                task_id=task_id,
                session_id=f"session-{ledger_shape}",
                runtime_id=runtime_id,
                call_id=f"call-{ledger_shape}",
            )
            permit = await _claim_tool_permission_for_test(
                store,
                checkpoint,
                consumer_id=f"consumer-{ledger_shape}",
                claim_id=f"claim-{ledger_shape}",
            )
            await store.save_task(
                Task(
                    id=task_id,
                    project_id="project-a",
                    session_id=f"session-{ledger_shape}",
                )
            )
            if ledger_shape == "metadata_plural":
                runtime_resume = {"runtime_session_id": runtime_id}
                runtime_metadata = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {
                        permit["fingerprint"]: permit,
                    },
                }
            else:
                runtime_resume = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit,
                }
                runtime_metadata = {"runtime_session_id": runtime_id}
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume=runtime_resume,
                runtime_metadata=runtime_metadata,
                runtime_mirror={
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit,
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                    "continue_after_tool_result": True,
                    "audit_note": "must survive without ownership keys",
                },
            )

            assert (await coordinator.begin_exact_tool_effect(permit)).acquired
            permit = {**permit, "state": "executing"}
            receipt = await coordinator.persist_exact_tool_result(
                permit,
                runtime_session_id=runtime_id,
                tool_name=str(permit["function"]),
                payload={"success": True, "result": {"shape": ledger_shape}},
                tool_call_id=str(permit["id"]),
                task_id=task_id,
                session_id=f"session-{ledger_shape}",
                message_id=f"result-{ledger_shape}",
                metadata={"shape": ledger_shape},
            )
            assert receipt.outcome == "finished"
            persisted = await store.get_task(task_id)
            assert persisted is not None
            for container in (
                persisted.context_snapshot["runtime_resume"],
                persisted.metadata["runtime_v2"],
                persisted.context_snapshot["runtime_v2"],
            ):
                assert container["runtime_session_id"] == runtime_id
                for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                    assert key not in container
            assert persisted.context_snapshot["runtime_v2"]["audit_note"] == (
                "must survive without ownership keys"
            )
            assert len(await store.list_runtime_tool_results(runtime_id)) == 1
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("shape", "should_fail"),
    [
        ("different_singulars", True),
        ("plural_plus_distinct_singular", True),
        ("single_singular", False),
        ("matching_plural_and_singular", False),
    ],
)
def test_legacy_singular_canonicalization_never_unions_authorities(
    tmp_path: Path,
    shape: str,
    should_fail: bool,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"singular-{shape}.db")
        await store.initialize()
        try:
            runtime_id = "rt-singular-canonical"
            task_id = f"singular-{shape}"
            await store.save_task(Task(id=task_id, project_id="project-a"))
            permit_a = {
                "id": "call-a",
                "function": "shell",
                "arguments": {"command": "true"},
                "fingerprint": "fingerprint-a",
                "runtime_session_id": runtime_id,
            }
            permit_b = {
                **permit_a,
                "id": "call-b",
                "fingerprint": "fingerprint-b",
            }
            if shape == "different_singulars":
                runtime_resume = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit_a,
                }
                runtime_metadata = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit_b,
                }
            elif shape == "plural_plus_distinct_singular":
                runtime_resume = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {"fingerprint-a": permit_a},
                }
                runtime_metadata = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit_b,
                }
            elif shape == "single_singular":
                runtime_resume = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit_a,
                }
                runtime_metadata = {"runtime_session_id": runtime_id}
            else:
                runtime_resume = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_calls": {"fingerprint-a": permit_a},
                }
                runtime_metadata = {
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": permit_a,
                }
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume=runtime_resume,
                runtime_metadata=runtime_metadata,
            )

            if should_fail:
                with pytest.raises(RuntimeError, match="legacy exact ToolCall"):
                    await store.set_task_runtime_continuation_marker(
                        task_id,
                        runtime_session_id=runtime_id,
                        enabled=True,
                    )
                unchanged = await store.get_task(task_id)
                assert unchanged is not None
                assert unchanged.context_snapshot["runtime_resume"] == runtime_resume
                assert unchanged.metadata["runtime_v2"] == runtime_metadata
                return

            canonical = await store.set_task_runtime_continuation_marker(
                task_id,
                runtime_session_id=runtime_id,
                enabled=True,
            )
            for container in (
                canonical.context_snapshot["runtime_resume"],
                canonical.metadata["runtime_v2"],
            ):
                assert container["approved_tool_calls"] == {
                    "fingerprint-a": permit_a,
                }
                assert "approved_tool_call" not in container
                assert container["continue_after_tool_result"] is True
        finally:
            await store.close()

    asyncio.run(scenario())


def test_permit_update_migrates_legacy_and_remove_retains_other_permit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "legacy-permits.db")
        await store.initialize()
        try:
            runtime_id = "rt-legacy-merge"
            task_id = "legacy-merge-worker"
            old_fingerprint = "legacy-fingerprint"
            new_fingerprint = "new-fingerprint"
            old_permit = {
                "id": "call-legacy",
                "fingerprint": old_fingerprint,
                "runtime_session_id": runtime_id,
                "checkpoint_id": "cp-legacy",
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": task_id,
                "claim_id": "claim-legacy",
                "consumer_id": "consumer-legacy",
                "function": "shell",
                "arguments": {"command": "true"},
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            new_permit = {
                "id": "call-new",
                "fingerprint": new_fingerprint,
                "runtime_session_id": runtime_id,
                "checkpoint_id": "cp-new",
                "checkpoint_type": "tool_permission",
                "checkpoint_project_id": "project-a",
                "task_id": task_id,
                "claim_id": "claim-new",
                "consumer_id": "consumer-new",
                "function": "shell",
                "arguments": {"command": "true"},
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            await store.save_task(
                Task(id=task_id, project_id="project-a", session_id="legacy-session")
            )
            for permit in (old_permit, new_permit):
                checkpoint = _tool_checkpoint(
                    checkpoint_id=str(permit["checkpoint_id"]),
                    task_id=task_id,
                    session_id="legacy-session",
                    runtime_id=runtime_id,
                    call_id=str(permit["id"]),
                    name="shell",
                    arguments=dict(permit["arguments"]),
                )
                # This migration test uses readable labels as fingerprints;
                # make the checkpoint immutable ToolCall reference match them.
                checkpoint.payload["tool_call"]["fingerprint"] = permit[
                    "fingerprint"
                ]
                checkpoint.payload["interaction"]["domain_key"] = (
                    f"tool_permission:{task_id}:{runtime_id}:{permit['fingerprint']}"
                )
                await _publish_owner_checkpoint(store, checkpoint)
                await store.accept_execution_checkpoint_decision(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    request_id=f"answer-{permit['checkpoint_id']}",
                    decision_hash=f"hash-{permit['checkpoint_id']}",
                    decision={"option_id": "approve_once"},
                )
                claimed = await store.claim_answered_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    consumer_id=str(permit["consumer_id"]),
                    claim_id=str(permit["claim_id"]),
                )
                assert claimed.acquired
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": old_permit,
                },
                runtime_metadata={"runtime_session_id": runtime_id},
                runtime_mirror={
                    "runtime_session_id": runtime_id,
                    "approved_tool_call": old_permit,
                },
            )

            merged = await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=new_fingerprint,
                permit=new_permit,
            )
            expected = {old_fingerprint, new_fingerprint}
            for container in (
                merged.context_snapshot["runtime_resume"],
                merged.metadata["runtime_v2"],
            ):
                assert set(container["approved_tool_calls"]) == expected
                assert "approved_tool_call" not in container
            assert "approved_tool_call" not in merged.context_snapshot["runtime_v2"]

            retained = await store.update_task_runtime_tool_permit(
                task_id,
                runtime_session_id=runtime_id,
                fingerprint=old_fingerprint,
                permit=None,
                expected_permit=old_permit,
            )
            for container in (
                retained.context_snapshot["runtime_resume"],
                retained.metadata["runtime_v2"],
            ):
                assert set(container["approved_tool_calls"]) == {new_fingerprint}
                assert container["approved_tool_calls"][new_fingerprint]["id"] == (
                    "call-new"
                )
            for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                assert key not in retained.context_snapshot["runtime_v2"]
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("incoming_side", ["metadata", "resume"])
def test_inactive_conflicting_runtime_ids_accept_one_sided_replacement(
    tmp_path: Path,
    incoming_side: str,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"inactive-{incoming_side}.db")
        await store.initialize()
        try:
            task_id = f"inactive-{incoming_side}-worker"
            await store.save_task(Task(id=task_id, project_id="project-a"))
            await _raw_seed_task_runtime_views(
                store,
                task_id,
                runtime_resume={
                    "runtime_session_id": "rt-old-resume",
                    "approved_tool_calls": {},
                    "continue_after_tool_result": False,
                },
                runtime_metadata={
                    "runtime_session_id": "rt-old-metadata",
                    "approved_tool_call": None,
                },
                runtime_mirror={
                    "runtime_session_id": "rt-stale-mirror",
                    "approved_tool_calls": {},
                    "continue_after_tool_result": False,
                    "audit_note": incoming_side,
                },
            )
            incoming = await store.get_task(task_id)
            assert incoming is not None
            if incoming_side == "metadata":
                incoming.metadata["runtime_v2"]["runtime_session_id"] = "rt-new"
            else:
                incoming.context_snapshot["runtime_resume"][
                    "runtime_session_id"
                ] = "rt-new"
            await store.save_task(incoming)

            persisted = await store.get_task(task_id)
            assert persisted is not None
            for container in (
                persisted.context_snapshot["runtime_resume"],
                persisted.metadata["runtime_v2"],
                persisted.context_snapshot["runtime_v2"],
            ):
                assert container["runtime_session_id"] == "rt-new"
                for key in OPCStore._TASK_RUNTIME_TOOL_LEDGER_KEYS:
                    assert key not in container
            assert persisted.context_snapshot["runtime_v2"]["audit_note"] == (
                incoming_side
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_resident_lead_stale_review_save_cannot_erase_exact_tool_permit(
    tmp_path: Path,
) -> None:
    """A run12-shaped controller snapshot cannot clobber Store-owned state.

    The resident lead keeps a review Task object alive while the interaction
    consumer writes an exact permit through a second Store.  The two
    coroutines deliberately place the old controller save between the permit
    write and the atomic ToolResult commit, which was the production failure
    window.
    """

    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        controller_store = OPCStore(db_path)
        interaction_store = OPCStore(db_path)
        await controller_store.initialize()
        await interaction_store.initialize(run_startup_maintenance=False)
        coordinator = InteractionCoordinator(
            store=interaction_store,
            project_id="project-a",
        )
        try:
            await controller_store.save_delegation_run(
                DelegationRun(
                    run_id="run-investment",
                    project_id="project-a",
                    session_id="root-investment",
                    execution_model="multi_team_org",
                    status="running",
                    lifecycle_status="active",
                )
            )
            work_item = DelegationWorkItem(
                work_item_id="review-company-analysis",
                run_id="run-investment",
                cell_id="team-investment",
                team_id="team-investment",
                role_id="investment_lead",
                seat_id="seat-investment-lead",
                title="Review company analysis",
                summary="Validate the analyst deliverable.",
                kind="review",
                projection_id="review-company-analysis",
                phase=Phase.READY,
                metadata={
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "work_kind": "review",
                    "review_execution_work_item": True,
                },
            )
            await controller_store.save_delegation_work_item(work_item)
            runtime_id = "rt-resident-investment-lead"
            review_task = Task(
                id="lead-review-task",
                project_id="project-a",
                session_id="root-investment:review::company-analysis::v1",
                parent_session_id="root-investment",
                assigned_to="investment_lead",
                status=TaskStatus.PENDING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "work_item_projection_id": "review-company-analysis",
                    "work_item_turn_type": "review",
                    "work_kind": "review",
                    "review_execution_work_item": True,
                    "delegation_run_id": "run-investment",
                    "delegation_seat_id": "seat-investment-lead",
                    "work_item_role_id": "investment_lead",
                    "member_session_state": {
                        "role_id": "investment_lead",
                        "status": "running",
                        "resident_status": "running",
                        "current_task_id": "lead-review-task",
                        "focused_work_item_id": "review-company-analysis",
                    },
                    "runtime_v2": {"runtime_session_id": runtime_id},
                },
            )
            await controller_store.save_task(review_task)
            await controller_store.link_work_item_runtime_task(
                work_item.work_item_id,
                review_task.id,
            )
            lease = await controller_store.acquire_delegation_run_controller_lease(
                "run-investment",
                project_id="project-a",
                root_session_id="root-investment",
                owner_token="resident-controller",
                lease_seconds=60,
            )
            assert lease.acquired
            claimed_item = await controller_store.claim_delegation_work_item_if_dispatchable(
                work_item.work_item_id,
                expected_phase=Phase.READY,
                role_runtime_session_id="role-session::investment-lead",
                seat_id="seat-investment-lead",
                task_id=review_task.id,
                controller_owner_token="resident-controller",
                controller_lease_generation=lease.generation,
            )
            assert claimed_item is not None
            stale_review_task = await controller_store.get_task(review_task.id)
            assert stale_review_task is not None
            stale_review_task.status = TaskStatus.RUNNING
            stale_review_task.metadata.update(
                {
                    "company_run_controller_owner_token": "resident-controller",
                    "company_run_controller_lease_generation": lease.generation,
                    "claimed_work_item_attempt_seq": int(
                        claimed_item.metadata.get("attempt_seq", 0) or 0
                    ),
                }
            )
            await controller_store.save_task(stale_review_task)
            # This is the old controller snapshot retained across the owner
            # interaction.  It intentionally predates ``approved_tool_calls``.
            stale_review_task = await controller_store.get_task(review_task.id)
            assert stale_review_task is not None

            checkpoint = _tool_checkpoint(
                checkpoint_id="lead-review-tool-permission",
                task_id=review_task.id,
                session_id=review_task.session_id or "",
                runtime_id=runtime_id,
                call_id="call-review-json",
                name="shell",
                arguments={
                    "command": (
                        "python3 -m json.tool "
                        "investment_case/company_analysis.json"
                    )
                },
            )
            await _publish_owner_checkpoint(interaction_store, checkpoint)
            accepted = await interaction_store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="owner-approve-review-json",
                decision_hash="approve-review-json-hash",
                decision={"option_id": "approve_once"},
            )
            assert accepted.acknowledged
            claimed = await interaction_store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="resident-runtime-consumer",
                claim_id="resident-runtime-claim",
            )
            assert claimed.acquired
            tool_call = dict(checkpoint.payload["tool_call"])
            permit = {
                "id": tool_call["id"],
                "function": tool_call["name"],
                "arguments": dict(tool_call["arguments"]),
                "fingerprint": tool_call["fingerprint"],
                "runtime_session_id": runtime_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": "project-a",
                "task_id": review_task.id,
                "claim_id": "resident-runtime-claim",
                "consumer_id": "resident-runtime-consumer",
                "decision": "approve_once",
                "approved": True,
                "state": "ready",
            }
            await interaction_store.save_runtime_tool_call(
                runtime_session_id=runtime_id,
                task_id=review_task.id,
                session_id=review_task.session_id,
                message_id="lead-review-assistant",
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                arguments=tool_call["arguments"],
            )

            permit_written = asyncio.Event()
            stale_save_finished = asyncio.Event()
            stale_save_had_permit = False

            async def interaction_consumer() -> None:
                await interaction_store.update_task_runtime_tool_permit(
                    review_task.id,
                    runtime_session_id=runtime_id,
                    fingerprint=tool_call["fingerprint"],
                    permit=permit,
                    prepare_for_resume=True,
                )
                assert (await coordinator.begin_exact_tool_effect(permit)).acquired
                permit["state"] = "executing"
                await interaction_store.update_task_runtime_tool_permit(
                    review_task.id,
                    runtime_session_id=runtime_id,
                    fingerprint=tool_call["fingerprint"],
                    permit=permit,
                    prepare_for_resume=True,
                )
                permit_written.set()
                await stale_save_finished.wait()
                completion = await coordinator.persist_exact_tool_result(
                    permit,
                    runtime_session_id=runtime_id,
                    tool_name=tool_call["name"],
                    payload={
                        "success": True,
                        "result": {
                            "exit_code": 0,
                            "stdout": "valid JSON\n",
                        },
                    },
                    tool_call_id=tool_call["id"],
                    task_id=review_task.id,
                    session_id=review_task.session_id,
                    message_id="lead-review-tool-result",
                    metadata={"kind": "run12-regression"},
                    checkpoint_payload_patch={
                        "approval_result": {"tool_result_persisted": True}
                    },
                )
                assert completion.outcome == "finished"

            async def resident_controller_save() -> None:
                nonlocal stale_save_had_permit
                await permit_written.wait()
                stale_review_task.metadata["resident_review_heartbeat"] = (
                    "controller-save-after-permit"
                )
                await controller_store.save_task(stale_review_task)
                persisted_between = await controller_store.get_task(review_task.id)
                assert persisted_between is not None
                persisted_permits = dict(
                    persisted_between.context_snapshot["runtime_resume"].get(
                        "approved_tool_calls", {}
                    )
                    or {}
                )
                stale_save_had_permit = (
                    tool_call["fingerprint"] in persisted_permits
                )
                assert "approved_tool_calls" not in persisted_between.context_snapshot.get(
                    "runtime_v2", {}
                )
                stale_save_finished.set()

            await asyncio.wait_for(
                asyncio.gather(
                    interaction_consumer(),
                    resident_controller_save(),
                ),
                timeout=5,
            )
            assert stale_save_had_permit

            resolved = await interaction_store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert resolved is not None and resolved.status == "resolved"
            assert resolved.payload["interaction"]["execution"]["state"] == (
                "result_persisted"
            )
            results = await interaction_store.list_runtime_tool_results(runtime_id)
            assert [row["tool_call_id"] for row in results] == [tool_call["id"]]

            persisted = await interaction_store.get_task(review_task.id)
            assert persisted is not None
            assert "approved_tool_calls" not in persisted.context_snapshot[
                "runtime_resume"
            ]
            assert "approved_tool_calls" not in persisted.metadata["runtime_v2"]
            assert "approved_tool_calls" not in persisted.context_snapshot.get(
                "runtime_v2", {}
            )
            assert (
                persisted.metadata["resident_review_heartbeat"]
                == "controller-save-after-permit"
            )

            # The controller object was merged with the permit during its
            # first save.  Reusing it after completion must not resurrect the
            # consumed one-shot permit.
            assert "approved_tool_calls" in stale_review_task.context_snapshot[
                "runtime_resume"
            ]
            stale_review_task.context_snapshot["runtime_v2"] = dict(
                stale_review_task.metadata["runtime_v2"]
            )
            assert "approved_tool_calls" in stale_review_task.context_snapshot[
                "runtime_v2"
            ]
            stale_review_task.metadata["post_result_controller_save"] = True
            await controller_store.save_task(stale_review_task)
            final_task = await interaction_store.get_task(review_task.id)
            assert final_task is not None
            assert "approved_tool_calls" not in final_task.context_snapshot[
                "runtime_resume"
            ]
            assert "approved_tool_calls" not in final_task.metadata["runtime_v2"]
            assert "approved_tool_calls" not in final_task.context_snapshot[
                "runtime_v2"
            ]
            assert final_task.metadata["post_result_controller_save"] is True
        finally:
            await coordinator.shutdown()
            await interaction_store.close()
            await controller_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("permit_view", ["resume", "metadata"])
def test_executing_without_result_finishes_outcome_unknown_without_replay(
    tmp_path: Path,
    permit_view: str,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"{permit_view}.db")
        await store.initialize()
        try:
            checkpoint = _tool_checkpoint()
            await _publish_owner_checkpoint(store, checkpoint)
            await store.save_runtime_tool_call(
                runtime_session_id="rt-child",
                task_id="worker",
                session_id="worker-session",
                message_id="assistant-1",
                tool_call_id="call-1",
                tool_name="dangerous_tool",
                arguments={"value": "original"},
            )
            permit = {
                "id": "call-1",
                "function": "dangerous_tool",
                "arguments": {"value": "original"},
                "fingerprint": checkpoint.payload["tool_call"]["fingerprint"],
                "runtime_session_id": "rt-child",
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "checkpoint_project_id": checkpoint.project_id,
                "task_id": "worker",
                "claim_id": "old-claim",
                "consumer_id": "crashed",
                "decision": "approve_once",
                "approved": True,
                "state": "executing",
            }
            task = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                context_snapshot={
                    "runtime_resume": {
                        "runtime_session_id": "rt-child",
                        **(
                            {"approved_tool_calls": {permit["fingerprint"]: permit}}
                            if permit_view == "resume"
                            else {}
                        ),
                    }
                },
                metadata={
                    "runtime_v2": {
                        "runtime_session_id": "rt-child",
                        **(
                            {"approved_tool_calls": {permit["fingerprint"]: permit}}
                            if permit_view == "metadata"
                            else {}
                        ),
                    }
                },
            )
            await store.save_task(task)
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="request-1",
                decision_hash="hash-1",
                decision={"option_id": "approve_once"},
            )
            claimed = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="crashed",
                claim_id="old-claim",
            )
            assert claimed.acquired and claimed.checkpoint is not None
            assert (
                await store.begin_execution_checkpoint_effect(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    consumer_id="crashed",
                    claim_id="old-claim",
                )
            ).acquired
            permit["state"] = "executing"
            await _raw_seed_task_runtime_views(
                store,
                "worker",
                runtime_resume={
                    "runtime_session_id": "rt-child",
                    **(
                        {"approved_tool_calls": {permit["fingerprint"]: permit}}
                        if permit_view == "resume"
                        else {}
                    ),
                },
                runtime_metadata={
                    "runtime_session_id": "rt-child",
                    **(
                        {"approved_tool_calls": {permit["fingerprint"]: permit}}
                        if permit_view == "metadata"
                        else {}
                    ),
                },
            )
            claimed_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert claimed_checkpoint is not None
            engine = _bare_engine(store)
            engine.approval_engine = _approval_engine(
                store,
                engine.interaction_coordinator,
            )
            engine._execute_single_agent = AsyncMock(
                side_effect=AssertionError("executing ToolCall must not be replayed")
            )
            lease = InteractionDecisionLease(
                checkpoint=claimed_checkpoint,
                decision={"option_id": "approve_once"},
                consumer_id="crashed",
                claim_id="old-claim",
            )
            result = await engine._resume_permission_checkpoint(lease)
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert "not replayed" in result.message
            assert engine._execute_single_agent.await_count == 0
            assert persisted is not None and persisted.status == "outcome_unknown"
            assert await store.list_runtime_tool_results("rt-child") == []
            persisted_task = await store.get_task("worker")
            assert persisted_task is not None
            for container in (
                persisted_task.context_snapshot["runtime_resume"],
                persisted_task.metadata["runtime_v2"],
            ):
                assert "approved_tool_calls" not in container
        finally:
            await store.close()

    asyncio.run(scenario())


class _Preferences:
    opc_home = None

    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs) -> None:
        _ = kwargs


class _ApprovalMemory:
    def append_autonomy_event(self, event, project=False) -> None:
        _ = (event, project)


def _approval_engine(store: OPCStore, coordinator: InteractionCoordinator) -> ApprovalEngine:
    return ApprovalEngine(
        llm=object(),
        store=store,
        preferences=_Preferences(),
        memory=_ApprovalMemory(),
        config=AutonomyConfig(),
        interaction_coordinator=coordinator,
    )


def test_concurrent_live_approvals_persist_distinct_keyed_permits(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            coordinator = InteractionCoordinator(store=store, project_id="project-a")
            approval = _approval_engine(store, coordinator)
            task = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
            )
            await store.save_task(task)
            checkpoints = [
                _tool_checkpoint(),
                _tool_checkpoint(
                    checkpoint_id="cp-concurrent-2",
                    call_id="call-2",
                    arguments={"value": "second"},
                ),
            ]
            for index, checkpoint in enumerate(checkpoints):
                await _publish_owner_checkpoint(store, checkpoint)
                await store.accept_execution_checkpoint_decision(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    request_id=f"answer-concurrent-{index}",
                    decision_hash=f"hash-concurrent-{index}",
                    decision={"option_id": "approve_once"},
                )
                claimed = await store.claim_answered_execution_checkpoint(
                    checkpoint.checkpoint_id,
                    project_id="project-a",
                    checkpoint_type="tool_permission",
                    consumer_id=f"consumer-{index}",
                    claim_id=f"claim-{index}",
                )
                assert claimed.acquired and claimed.checkpoint is not None
                checkpoints[index] = claimed.checkpoint

            async def apply(index: int) -> bool:
                checkpoint = checkpoints[index]
                approved, _ = await approval._apply_human_approval_reply(
                    task=task,
                    action_kind="tool",
                    action_name="dangerous_tool",
                    decision=ApprovalDecision(
                        action=ApprovalAction.ESCALATE,
                        risk_level=RiskLevel.HIGH,
                        rationale="requires owner",
                        confidence=1.0,
                        policy_source="test",
                    ),
                    metadata={"arguments": checkpoint.payload["tool_call"]["arguments"]},
                    allowlist_enabled=True,
                    allowlist_patterns=["*"],
                    reply="approve_once",
                    decision_lease=InteractionDecisionLease(
                        checkpoint=checkpoint,
                        decision={"option_id": "approve_once"},
                        consumer_id=f"consumer-{index}",
                        claim_id=f"claim-{index}",
                    ),
                )
                return approved

            assert await asyncio.gather(apply(0), apply(1)) == [True, True]
            persisted = await store.get_task(task.id)
            assert persisted is not None
            permits = persisted.context_snapshot["runtime_resume"]["approved_tool_calls"]
            assert {permit["id"] for permit in permits.values()} == {"call-1", "call-2"}
            assert all(permit["state"] == "ready" for permit in permits.values())
            assert "approved_tool_call" not in persisted.context_snapshot["runtime_resume"]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_tool_approval_without_stable_call_id_fails_before_publishing_card(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            coordinator = InteractionCoordinator(store=store, project_id="project-a")
            approval = _approval_engine(store, coordinator)
            task = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
            )
            with pytest.raises(
                RuntimeError,
                match="stable ToolCall id and runtime session id",
            ):
                await approval._ask_user(
                    task=task,
                    action_kind="tool",
                    action_name="dangerous_tool",
                    decision=ApprovalDecision(
                        action=ApprovalAction.ESCALATE,
                        risk_level=RiskLevel.HIGH,
                        rationale="requires owner",
                        confidence=1.0,
                        policy_source="test",
                    ),
                    metadata={
                        "arguments": {"value": "missing-id"},
                        "tool_call": {"runtime_session_id": "rt-child"},
                    },
                )
            rows = await store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["tool_permission"],
            )
            assert rows == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_live_native_subagent_permission_keeps_child_runtime_and_parent_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            anchor = Task(
                id="anchor",
                project_id="project-a",
                session_id="root-session",
                metadata={"mode": "company", "execution_mode": "company_mode"},
            )
            worker = Task(
                id="worker",
                project_id="project-a",
                session_id="worker-session",
                parent_id="anchor",
                parent_session_id="root-session",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "analysis",
                },
            )
            child = Task(
                id="child",
                project_id="project-a",
                session_id="worker-session:agent-child",
                parent_id="worker",
                parent_session_id="worker-session",
                metadata={
                    **worker.metadata,
                    "_native_runtime_depth": 1,
                    "_subagent_name": "researcher",
                },
            )
            await store.save_task(anchor)
            await store.save_task(worker)
            # Persisting the child makes its inherited/shared role session
            # visible to the identity index; ownership must still resolve via
            # the durable parent rather than treating the child as a root.
            await store.save_task(child)
            engine = _bare_engine(store)
            approval = _approval_engine(store, engine.interaction_coordinator)
            ask = asyncio.create_task(approval._ask_user(
                task=child,
                action_kind="tool",
                action_name="dangerous_tool",
                decision=ApprovalDecision(
                    action=ApprovalAction.ESCALATE,
                    risk_level=RiskLevel.HIGH,
                    rationale="requires owner",
                    confidence=1.0,
                    policy_source="test",
                ),
                metadata={
                    "arguments": {"value": "child"},
                    "tool_call": {
                        "id": "child-call",
                        "runtime_session_id": "rt-child-exact",
                    },
                },
            ))
            checkpoint = None
            for _ in range(100):
                rows = await store.get_execution_checkpoints(
                    project_id="project-a",
                    checkpoint_types=["tool_permission"],
                    statuses=["pending"],
                )
                if rows:
                    checkpoint = rows[0]
                    break
                await asyncio.sleep(0.01)
            assert checkpoint is not None
            ownership = checkpoint.payload["interaction"]["ownership"]
            assert checkpoint.task_id == "child"
            assert ownership["waiting_task_id"] == "child"
            assert ownership["execution_parent_task_id"] == "worker"
            assert ownership["ui_anchor_task_id"] == "anchor"
            assert ownership["company_runtime_session_id"] == "root-session"
            assert checkpoint.payload["tool_call"]["runtime_session_id"] == "rt-child-exact"
            assert not await engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id="child",
                requester_session_id=child.session_id,
            )
            receipt = await engine.submit_checkpoint_decision(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type="tool_permission",
                decision={"option_id": "approve_once"},
                client_request_id="approve-child",
                requester_task_id="anchor",
                requester_session_id="root-session",
            )
            assert receipt["accepted"] is True
            allowed, _ = await asyncio.wait_for(ask, timeout=2)
            assert allowed is True
            saved_child = await store.get_task("child")
            assert saved_child is not None
            calls = saved_child.context_snapshot["runtime_resume"]["approved_tool_calls"]
            assert len(calls) == 1
            assert next(iter(calls.values()))["runtime_session_id"] == "rt-child-exact"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_restart_routes_exact_native_subagent_permit_to_child_runtime(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            child = Task(
                id="child",
                project_id="project-a",
                session_id="worker-session:agent-child",
                parent_id="worker",
                parent_session_id="worker-session",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "analysis",
                    "_native_runtime_depth": 1,
                    "_subagent_name": "researcher",
                },
            )
            await store.save_task(child)
            checkpoint = _tool_checkpoint(
                task_id="child",
                session_id=child.session_id,
                runtime_id="rt-child-exact",
                call_id="child-call",
                arguments={"value": "child"},
            )
            checkpoint.payload["interaction"]["ownership"]["waiting_task_id"] = "child"
            checkpoint.payload["interaction"]["ownership"][
                "waiting_session_id"
            ] = child.session_id
            checkpoint.payload["interaction"]["ownership"][
                "tool_runtime_session_id"
            ] = "rt-child-exact"
            await _publish_owner_checkpoint(store, checkpoint)
            await store.save_runtime_tool_call(
                runtime_session_id="rt-child-exact",
                task_id="child",
                session_id=child.session_id,
                message_id="assistant-child",
                tool_call_id="child-call",
                tool_name="dangerous_tool",
                arguments={"value": "child"},
            )
            await store.accept_execution_checkpoint_decision(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                request_id="child-answer",
                decision_hash="child-hash",
                decision={"option_id": "approve_once"},
            )
            claimed = await store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
                consumer_id="recovery",
            )
            assert claimed.acquired and claimed.checkpoint is not None
            engine = _bare_engine(store)
            engine.approval_engine = _approval_engine(
                store,
                engine.interaction_coordinator,
            )

            async def resume_child(
                tasks: list[Task],
                _use_external: str | None = None,
                **_: Any,
            ) -> str:
                resumed = tasks[0]
                permits = dict(
                    dict(resumed.context_snapshot.get("runtime_resume", {}) or {}).get(
                        "approved_tool_calls", {}
                    )
                    or {}
                )
                assert len(permits) == 1
                permit = next(iter(permits.values()))
                assert (
                    await engine.interaction_coordinator.begin_exact_tool_effect(permit)
                ).acquired
                permit = {**permit, "state": "executing"}
                await store.update_task_runtime_tool_permit(
                    "child",
                    runtime_session_id="rt-child-exact",
                    fingerprint=str(permit["fingerprint"]),
                    permit=permit,
                )
                finished = await engine.interaction_coordinator.persist_exact_tool_result(
                    permit,
                    runtime_session_id="rt-child-exact",
                    tool_name="dangerous_tool",
                    payload={"success": True, "result": {"value": "child"}},
                    tool_call_id="child-call",
                    task_id="child",
                    session_id=child.session_id,
                    message_id="child-result",
                    metadata={"kind": "test"},
                )
                assert finished.applied
                return "child resumed"

            engine._execute_single_agent = AsyncMock(side_effect=resume_child)
            lease = InteractionDecisionLease(
                checkpoint=claimed.checkpoint,
                decision={"option_id": "approve_once"},
                consumer_id="recovery",
                claim_id=claimed.claim_id,
            )
            outcome = await engine._resume_permission_checkpoint(lease)
            assert outcome.runtime_owns_completion is True
            resumed_tasks = engine._execute_single_agent.await_args.args[0]
            assert [task.id for task in resumed_tasks] == ["child"]
            assert resumed_tasks[0].context_snapshot["runtime_resume"][
                "runtime_session_id"
            ] == "rt-child-exact"
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None and persisted.status == "resolved"
        finally:
            await store.close()

    asyncio.run(scenario())


def test_wrong_runtime_or_checkpoint_owner_never_consumes_one_shot_permit() -> None:
    async def scenario() -> None:
        checkpoint = _tool_checkpoint(status="consuming")
        checkpoint.payload["interaction"]["claim"] = {
            "claim_id": "claim-1",
            "consumer_id": "consumer-1",
        }
        permit = {
            "id": "call-1",
            "function": "dangerous_tool",
            "arguments": {"value": "original"},
            "fingerprint": checkpoint.payload["tool_call"]["fingerprint"],
            "runtime_session_id": "rt-child",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "checkpoint_project_id": checkpoint.project_id,
            "task_id": "different-task",
            "claim_id": "claim-1",
            "consumer_id": "consumer-1",
            "decision": "approve_once",
            "approved": True,
            "state": "ready",
        }
        store = _ResumeStore(checkpoint)
        memory = _ResumeMemory(store)
        registry = ToolRegistry()
        executed: list[str] = []

        async def dangerous_tool(value: str):
            executed.append(value)
            return {"value": value}

        registry.register(ToolDefinition(
            name="dangerous_tool",
            description="dangerous",
            parameters={"type": "object", "properties": {}},
            func=dangerous_tool,
            requires_confirmation=True,
            concurrency_safe=False,
            read_only=False,
        ))
        approval_calls = 0

        async def deny_callback(tool, arguments, task, on_progress, call_context=None):
            nonlocal approval_calls
            _ = (tool, arguments, task, on_progress, call_context)
            approval_calls += 1
            return False, ApprovalDecision(
                action=ApprovalAction.REJECT,
                risk_level=RiskLevel.HIGH,
                rationale="invalid permit",
                confidence=1.0,
                policy_source="test",
            )

        runtime = NativeRuntimeV2(
            llm=_FinishLLM(),
            tool_registry=registry,
            memory_manager=memory,
            config=OPCConfig(),
            approval_callback=deny_callback,
        )
        task = Task(id="worker", project_id="project-a", session_id="worker-session")
        resolver = RuntimePermissionAdapter()
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=resolver,
            hook_bus=runtime._build_tool_hook_bus(
                runtime_session_id="rt-child",
                task=task,
                permission_resolver=resolver,
            ),
        )
        result = await executor.execute([{
            "id": "call-1",
            "function": "dangerous_tool",
            "arguments": {"value": "original"},
            "_approval_permit": permit,
        }], task=task)
        assert approval_calls == 1
        assert executed == []
        assert result[0]["result"]["success"] is False

    asyncio.run(scenario())


def test_action_permission_live_finishes_and_restart_is_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        try:
            task = Task(
                id="action-task",
                project_id="project-a",
                session_id="action-session",
            )
            coordinator = InteractionCoordinator(store=store, project_id="project-a")
            approval = _approval_engine(store, coordinator)
            live = asyncio.create_task(approval._ask_user(
                task=task,
                action_kind="external_agent",
                action_name="publisher",
                decision=ApprovalDecision(
                    action=ApprovalAction.ESCALATE,
                    risk_level=RiskLevel.HIGH,
                    rationale="external action",
                    confidence=1.0,
                    policy_source="test",
                ),
                metadata={
                    "command": "publisher --force",
                    "source_event_id": "external-invocation:live-action-1",
                },
            ))
            live_checkpoint = None
            for _ in range(100):
                rows = await store.get_execution_checkpoints(
                    project_id="project-a",
                    checkpoint_types=["action_permission"],
                    statuses=["pending"],
                )
                if rows:
                    live_checkpoint = rows[0]
                    break
                await asyncio.sleep(0.01)
            assert live_checkpoint is not None
            await coordinator.submit(
                checkpoint_id=live_checkpoint.checkpoint_id,
                checkpoint_type="action_permission",
                decision={"option_id": "approve_once"},
                client_request_id="live-action",
            )
            allowed, _ = await asyncio.wait_for(live, timeout=2)
            assert allowed is True
            persisted_live = await store.get_execution_checkpoint(
                live_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="action_permission",
            )
            assert persisted_live is not None and persisted_live.status == "resolved"

            restart_checkpoint = ExecutionCheckpoint(
                checkpoint_id="action-restart",
                project_id="project-a",
                session_id="action-session",
                checkpoint_type="action_permission",
                task_id="action-task",
                payload={
                    "interaction": {
                        "kind": "action_permission",
                        "domain_key": "action_permission:restart-action-1",
                        "options": [{"id": "approve_once", "label": "Approve once"}],
                    },
                    "approval": {
                        "action_kind": "external_agent",
                        "action_name": "publisher",
                        "allowlist_enabled": True,
                        "allowlist_patterns": ["*"],
                    },
                },
            )
            await _publish_owner_checkpoint(store, restart_checkpoint)
            await store.accept_execution_checkpoint_decision(
                restart_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="action_permission",
                request_id="restart-action",
                decision_hash="restart-hash",
                decision={"option_id": "approve_once"},
            )
            claim = await store.claim_answered_execution_checkpoint(
                restart_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="action_permission",
                consumer_id="restart-consumer",
            )
            assert claim.acquired and claim.checkpoint is not None
            engine = _bare_engine(store)
            engine.approval_engine = approval
            outcome = await engine._resume_action_permission_checkpoint(
                InteractionDecisionLease(
                    checkpoint=claim.checkpoint,
                    decision={"option_id": "approve_once"},
                    consumer_id="restart-consumer",
                    claim_id=claim.claim_id,
                )
            )
            persisted_restart = await store.get_execution_checkpoint(
                restart_checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="action_permission",
            )
            assert "not replayed" in outcome.message
            assert persisted_restart is not None
            assert persisted_restart.status == "stale_reissue_required"
            assert persisted_restart.payload["approval_result"]["action_replayed"] is False
        finally:
            await store.close()

    asyncio.run(scenario())
