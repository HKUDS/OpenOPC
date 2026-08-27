from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import DelegationWorkItem, Phase, Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401 -- register hooks
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.phase import InvalidPhaseTransition
from opc.layer2_organization.work_item_identity import (
    canonical_work_item_turn_type_for_kind,
    mark_projected_work_item_task,
    mark_work_item_projection,
)
from opc.layer2_organization.work_item_runtime import mark_work_item_runtime


PROJECT_ID = "legacy-project"
RUN_ID = "legacy-run"


def _db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".opc" / "projects" / PROJECT_ID / "tasks.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _projection_metadata(
    work_item_id: str,
    *,
    kind: str,
    manager_role_id: str = "cto",
    extra: dict | None = None,
) -> dict:
    turn_type = canonical_work_item_turn_type_for_kind(kind, fallback=kind)
    return mark_work_item_projection(
        mark_work_item_runtime(
            {
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "delegation_run_id": RUN_ID,
                "delegation_team_id": "team::cto",
                "delegation_seat_id": f"seat::{work_item_id}",
                "work_item_role_id": "engineer",
                "manager_role_id": manager_role_id,
                "manager_seat_id": f"seat::team::cto::{manager_role_id}",
                "work_kind": kind,
                **dict(extra or {}),
            }
        ),
        projection_id=work_item_id,
        turn_type=turn_type,
    )


async def _save_linked_old_wait(
    store: OPCStore,
    key: str,
    *,
    kind: str = "execute",
    phase: Phase = Phase.AWAITING_HUMAN,
    manager_role_id: str = "cto",
    metadata: dict | None = None,
    parent_work_item_id: str | None = None,
) -> tuple[DelegationWorkItem, Task]:
    work_item_id = f"wi-{key}"
    merged_metadata = _projection_metadata(
        work_item_id,
        kind=kind,
        manager_role_id=manager_role_id,
        extra=metadata,
    )
    item = DelegationWorkItem(
        work_item_id=work_item_id,
        run_id=RUN_ID,
        cell_id="team::cto",
        team_id="team::cto",
        role_id="engineer",
        seat_id=f"seat::{work_item_id}",
        manager_role_id=manager_role_id,
        manager_seat_id=f"seat::team::cto::{manager_role_id}",
        parent_work_item_id=parent_work_item_id,
        title=f"Legacy {key}",
        summary=f"Completed legacy work {key}",
        kind=kind,
        projection_id=work_item_id,
        phase=phase,
        claimed_by_role_runtime_session_id=f"runtime::{key}",
        claimed_by_seat_id=f"seat::{work_item_id}",
        metadata={
            **merged_metadata,
            "claimed_by_role_session_id": f"runtime::{key}",
            "claimed_task_id": f"task-{key}",
        },
    )
    task_status = (
        TaskStatus.AWAITING_MANAGER_REVIEW
        if phase == Phase.AWAITING_MANAGER_REVIEW
        else TaskStatus.AWAITING_HUMAN
    )
    task = Task(
        id=f"task-{key}",
        session_id=f"session-{key}",
        parent_session_id="root-session",
        title=item.title,
        description=item.summary,
        assigned_to="engineer",
        status=task_status,
        project_id=PROJECT_ID,
        result={
            "content": f"completed-result-{key}",
            "artifacts": {"side_effect_token": f"effect-{key}"},
        },
        metadata=mark_projected_work_item_task(
            merged_metadata,
            projection_id=work_item_id,
            turn_type=canonical_work_item_turn_type_for_kind(kind, fallback=kind),
        ),
    )
    await store.save_delegation_work_item(item)
    await store.save_task(task)
    assert await store.link_work_item_runtime_task(item.work_item_id, task.id)
    return item, task


def _insert_old_checkpoints(
    db_path: Path,
    rows: list[tuple[str, str, str, dict]],
) -> None:
    now = datetime.now().isoformat()
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            """INSERT INTO execution_checkpoints
               (checkpoint_id, project_id, session_id, checkpoint_type,
                status, task_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, 'task_user_input', ?, ?, ?, ?, ?)""",
            [
                (
                    checkpoint_id,
                    PROJECT_ID,
                    f"session-{key}",
                    status,
                    f"task-{key}",
                    json.dumps(payload),
                    now,
                    now,
                )
                for checkpoint_id, key, status, payload in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _simulate_unprojected_task_after_work_item_commit(
    db_path: Path,
    *,
    work_item_id: str,
    phase: Phase,
    task_id: str,
    work_item_metadata_updates: dict | None = None,
) -> None:
    """Persist the exact crash window left by the old post-commit hook."""

    now = datetime.now().isoformat()
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT metadata FROM delegation_work_items WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        metadata.update(dict(work_item_metadata_updates or {}))
        connection.execute(
            """UPDATE delegation_work_items
               SET phase = ?, metadata = ?, updated_at = ?
               WHERE work_item_id = ?""",
            (phase.value, json.dumps(metadata), now, work_item_id),
        )
        connection.execute(
            """UPDATE tasks
               SET status = ?, execution_lock = 1, execution_locked_at = ?
               WHERE id = ?""",
            (TaskStatus.AWAITING_HUMAN.value, now, task_id),
        )
        connection.commit()
    finally:
        connection.close()


def _set_review_resolution_state(
    db_path: Path,
    review_work_item_id: str,
    *,
    state: str,
    metadata_updates: dict | None = None,
) -> None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT metadata FROM delegation_work_items WHERE work_item_id = ?",
            (review_work_item_id,),
        ).fetchone()
        assert row is not None
        metadata = json.loads(row[0])
        metadata["review_resolution_state"] = state
        metadata.update(dict(metadata_updates or {}))
        connection.execute(
            """UPDATE delegation_work_items
               SET metadata = ?, updated_at = ? WHERE work_item_id = ?""",
            (json.dumps(metadata), datetime.now().isoformat(), review_work_item_id),
        )
        connection.commit()
    finally:
        connection.close()


def _org_engine(root: Path) -> OrgEngine:
    config = OPCConfig()
    config.org.company_profile = "custom"
    config.org.roles = [
        RoleConfig(id="ceo", name="CEO", responsibility="Lead.", reports_to="owner"),
        RoleConfig(id="cto", name="CTO", responsibility="Review.", reports_to="ceo"),
        RoleConfig(
            id="engineer",
            name="Engineer",
            responsibility="Build.",
            reports_to="cto",
        ),
    ]
    return OrgEngine(config, root)


def _executor(store: OPCStore, root: Path) -> tuple[CompanyWorkItemExecutor, AsyncMock]:
    org_engine = _org_engine(root)
    execute_task = AsyncMock()
    communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
    return (
        CompanyWorkItemExecutor(
            org_engine=org_engine,
            communication=communication,
            approval_engine=MagicMock(),
            memory=None,
            execute_task=execute_task,
            save_task=store.save_task,
            store=store,
        ),
        execute_task,
    )


def _old_verifier_payload(key: str, *, evidence_only: bool = False) -> dict:
    payload = {
        "task_id": f"task-{key}",
        "pause_request": {"requires_user_input": False},
    }
    if evidence_only:
        payload["verification_evidence"] = {
            "status": "provided",
            "verdict": "partial",
        }
    else:
        payload["verification"] = {
            "completed": True,
            "passed": False,
            "verdict": "VERDICT: PARTIAL",
        }
    return payload


def test_startup_atomically_retires_exact_legacy_owner_waits_without_replaying_tools(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()

        parent = DelegationWorkItem(
            work_item_id="parent-intake",
            run_id=RUN_ID,
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::ceo",
            title="Continue after intake",
            kind="aggregate",
            projection_id="parent-intake",
            phase=Phase.WAITING_FOR_CHILDREN,
            metadata={
                "dependency_work_item_ids": ["wi-verifier-answered"],
                "waiting_on_work_item_ids": ["wi-verifier-answered"],
            },
        )
        await store.save_delegation_work_item(parent)

        active_statuses = ("pending", "answered", "consuming", "resuming")
        manual_keys: list[str] = []
        for status in active_statuses:
            key = f"manual-{status}"
            manual_keys.append(key)
            await _save_linked_old_wait(store, key)

        verifier_specs = {
            "pending": ("execute", {}, None),
            "answered": (
                "intake",
                {"manager_board_mutation_performed": True},
                "parent-intake",
            ),
            "consuming": ("aggregate", {}, None),
            "resuming": ("plan", {}, None),
        }
        for status, (kind, metadata, parent_id) in verifier_specs.items():
            await _save_linked_old_wait(
                store,
                f"verifier-{status}",
                kind=kind,
                metadata=metadata,
                parent_work_item_id=parent_id,
            )

        await _save_linked_old_wait(store, "explicit-input")
        await _save_linked_old_wait(
            store,
            "final-delivery",
            kind="deliver",
            metadata={
                "authoritative_output": True,
                "user_visible": True,
                "feedback_scope": "final",
                "review_owner_kind": "human",
            },
        )
        await _save_linked_old_wait(
            store,
            "top-seat-execute",
            manager_role_id="owner",
        )
        await _save_linked_old_wait(store, "unknown-aux", kind="review")
        await store.close()

        rows: list[tuple[str, str, str, dict]] = []
        for status, key in zip(active_statuses, manual_keys, strict=True):
            rows.append(
                (
                    f"cp-{key}",
                    key,
                    status,
                    {
                        "task_id": f"task-{key}",
                        "manual_intervention_source": "review_rework_escalation",
                    },
                )
            )
        for index, status in enumerate(active_statuses):
            key = f"verifier-{status}"
            rows.append(
                (
                    f"cp-{key}",
                    key,
                    status,
                    _old_verifier_payload(key, evidence_only=index == 2),
                )
            )
        rows.extend(
            [
                (
                    "cp-explicit-input",
                    "explicit-input",
                    "pending",
                    {
                        **_old_verifier_payload("explicit-input"),
                        "pause_request": {"requires_user_input": True},
                    },
                ),
                (
                    "cp-final-delivery",
                    "final-delivery",
                    "pending",
                    _old_verifier_payload("final-delivery"),
                ),
                (
                    "cp-top-seat-execute",
                    "top-seat-execute",
                    "pending",
                    _old_verifier_payload("top-seat-execute"),
                ),
                (
                    "cp-unknown-aux",
                    "unknown-aux",
                    "pending",
                    _old_verifier_payload("unknown-aux"),
                ),
            ]
        )
        _insert_old_checkpoints(db_path, rows)

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            for key in manual_keys:
                checkpoint = await restarted.get_execution_checkpoint(
                    f"cp-{key}",
                    project_id=PROJECT_ID,
                    checkpoint_type="task_user_input",
                )
                item = await restarted.get_delegation_work_item(f"wi-{key}")
                task = await restarted.get_task(f"task-{key}")
                assert checkpoint is not None and checkpoint.status == "stale"
                assert checkpoint.payload["legacy_interaction_migration"]["kind"] == (
                    "review_rework_escalation"
                )
                assert item is not None and item.phase == Phase.APPROVED
                assert task is not None and task.status == TaskStatus.DONE

            expected_verifier_phases = {
                "pending": Phase.AWAITING_MANAGER_REVIEW,
                "answered": Phase.APPROVED,
                "consuming": Phase.APPROVED,
                "resuming": Phase.AWAITING_MANAGER_REVIEW,
            }
            for status, expected_phase in expected_verifier_phases.items():
                key = f"verifier-{status}"
                checkpoint = await restarted.get_execution_checkpoint(
                    f"cp-{key}",
                    project_id=PROJECT_ID,
                    checkpoint_type="task_user_input",
                )
                item = await restarted.get_delegation_work_item(f"wi-{key}")
                task = await restarted.get_task(f"task-{key}")
                assert checkpoint is not None and checkpoint.status == "stale"
                assert item is not None and item.phase == expected_phase
                assert task is not None
                assert task.result["artifacts"]["side_effect_token"] == f"effect-{key}"
                assert task.metadata["legacy_owner_wait_retirement"][
                    "original_execution_replayed"
                ] is False
                assert item.metadata["report_source_result_content"] == (
                    f"completed-result-{key}"
                )
                if expected_phase == Phase.APPROVED:
                    assert task.status == TaskStatus.DONE
                else:
                    assert task.status == TaskStatus.AWAITING_MANAGER_REVIEW

            # The APPROVED intake completion fires the normal dependency hook,
            # so its existing parent can continue without a synthetic owner gate.
            parent_after = await restarted.get_delegation_work_item("parent-intake")
            assert parent_after is not None and parent_after.phase == Phase.RUNNING

            for key in ("explicit-input", "final-delivery"):
                checkpoint = await restarted.get_execution_checkpoint(
                    f"cp-{key}",
                    project_id=PROJECT_ID,
                    checkpoint_type="task_user_input",
                )
                item = await restarted.get_delegation_work_item(f"wi-{key}")
                task = await restarted.get_task(f"task-{key}")
                assert checkpoint is not None and checkpoint.status == "pending"
                assert item is not None and item.phase == Phase.AWAITING_HUMAN
                assert task is not None and task.status == TaskStatus.AWAITING_HUMAN

            for key, expected_phase, expected_status in (
                ("top-seat-execute", Phase.APPROVED, TaskStatus.DONE),
                ("unknown-aux", Phase.FAILED, TaskStatus.FAILED),
            ):
                checkpoint = await restarted.get_execution_checkpoint(
                    f"cp-{key}",
                    project_id=PROJECT_ID,
                    checkpoint_type="task_user_input",
                )
                item = await restarted.get_delegation_work_item(f"wi-{key}")
                task = await restarted.get_task(f"task-{key}")
                assert checkpoint is not None and checkpoint.status == "stale"
                assert item is not None and item.phase == expected_phase
                assert task is not None and task.status == expected_status
                assert task.result["artifacts"]["side_effect_token"] == f"effect-{key}"
                assert task.metadata["legacy_owner_wait_retirement"][
                    "original_execution_replayed"
                ] is False

            executor, original_execute = _executor(restarted, tmp_path)
            reconciled = await executor._reconcile_missing_review_chain(
                await restarted.list_delegation_work_items(RUN_ID)
            )
            assert reconciled
            for status in ("pending", "resuming"):
                target_id = f"wi-verifier-{status}"
                report = await restarted.get_delegation_work_item(
                    report_work_item_id_for_attempt(target_id, 1)
                )
                assert report is not None and report.phase == Phase.READY
                assert report.metadata["report_source_result_content"] == (
                    f"completed-result-verifier-{status}"
                )
            original_execute.assert_not_awaited()

            # Startup migration is one-shot and preserves the original audit.
            first_audit = dict(
                (
                    await restarted.get_execution_checkpoint(
                        "cp-verifier-pending",
                        project_id=PROJECT_ID,
                        checkpoint_type="task_user_input",
                    )
                ).payload["legacy_interaction_migration"]
            )
        finally:
            await restarted.close()

        second_restart = OPCStore(db_path)
        await second_restart.initialize()
        try:
            checkpoint = await second_restart.get_execution_checkpoint(
                "cp-verifier-pending",
                project_id=PROJECT_ID,
                checkpoint_type="task_user_input",
            )
            assert checkpoint is not None
            assert checkpoint.payload["legacy_interaction_migration"] == first_audit
        finally:
            await second_restart.close()

    asyncio.run(scenario())


async def _save_report_and_legacy_review(
    store: OPCStore,
    target_id: str,
    *,
    report_attempt: int,
    source_report_id: str | None = None,
    resolution_metadata_updates: dict | None = None,
) -> tuple[str, str]:
    report_id = report_work_item_id_for_attempt(target_id, report_attempt)
    report = DelegationWorkItem(
        work_item_id=report_id,
        run_id=RUN_ID,
        cell_id="team::cto",
        team_id="team::cto",
        role_id="engineer",
        seat_id=f"seat::{target_id}",
        parent_work_item_id=target_id,
        title=f"Report {report_attempt}",
        kind="report",
        projection_id=report_id,
        phase=Phase.APPROVED,
        batch_index=report_attempt,
        metadata={
            "report_target_work_item_id": target_id,
            "report_card_outcome": "applied",
            "completion_report": f"report-{report_attempt}",
        },
    )
    await store.save_delegation_work_item(report)
    review_id = review_work_item_id_for_attempt(target_id, report_attempt)
    source_id = source_report_id or report_id
    review = DelegationWorkItem(
        work_item_id=review_id,
        run_id=RUN_ID,
        cell_id="team::cto",
        team_id="team::cto",
        role_id="cto",
        seat_id="seat::team::cto::cto",
        parent_work_item_id=target_id,
        title=f"Review {report_attempt}",
        kind="review",
        projection_id=review_id,
        phase=Phase.APPROVED,
        batch_index=report_attempt,
        metadata={
            "review_target_work_item_id": target_id,
            "review_source_report_work_item_id": source_id,
            "review_resolution_state": "pending",
            "review_resolution": {
                "target_work_item_id": target_id,
                "target_phase": Phase.AWAITING_HUMAN.value,
                "source_report_work_item_id": source_id,
                "blocked_reason": "legacy human escalation",
                "metadata_updates": (
                    {"legacy_verdict_sentinel": review_id}
                    if resolution_metadata_updates is None
                    else dict(resolution_metadata_updates)
                ),
            },
        },
    )
    await store.save_delegation_work_item(review)
    return report_id, review_id


async def _save_atomic_delegate_old_wait(
    store: OPCStore,
    key: str,
) -> DelegationWorkItem:
    item = DelegationWorkItem(
        work_item_id=f"wi-{key}",
        run_id=RUN_ID,
        cell_id="team::cto",
        team_id="team::cto",
        role_id="engineer",
        seat_id=f"seat::{key}",
        parent_work_item_id="wi-manager",
        source_role_id="cto",
        source_seat_id="seat::team::cto::cto",
        title=f"Delegated legacy wait {key}",
        kind="execute",
        projection_id=f"wi-{key}",
        phase=Phase.AWAITING_HUMAN,
        batch_id="legacy-review-batch",
        metadata={
            **_projection_metadata(
                f"wi-{key}",
                kind="execute",
            ),
            "created_by_delegate_work": True,
            "delegate_invocation_id": f"invocation-{key}",
            "delegate_invocation_index": 0,
            "created_by_seat_id": "seat::team::cto::cto",
            "manager_board_parent_work_item_id": "wi-manager",
            "scope_key": key,
        },
    )
    return (await store.append_delegated_work_items_atomically([item]))[0]


def test_live_stale_review_recovery_rejects_reserved_delegate_metadata_update(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(_db_path(tmp_path))
        await store.initialize()
        try:
            target = await _save_atomic_delegate_old_wait(
                store,
                "live-reserved-conflict",
            )
            report_v2 = report_work_item_id_for_attempt(target.work_item_id, 2)
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id=report_v2,
                    run_id=RUN_ID,
                    parent_work_item_id=target.work_item_id,
                    title="New applied report",
                    kind="report",
                    phase=Phase.APPROVED,
                    batch_index=2,
                    metadata={
                        "report_target_work_item_id": target.work_item_id,
                        "report_card_outcome": "applied",
                    },
                )
            )

            with pytest.raises(ValueError, match="reserved metadata is immutable"):
                await store.recover_legacy_human_review_target_from_stale_resolution(
                    target.work_item_id,
                    stale_source_report_work_item_id="superseded-report",
                    metadata_updates={"delegate_sequence_index": 99},
                )

            unchanged = await store.get_delegation_work_item(target.work_item_id)
            assert unchanged is not None
            assert unchanged.phase == Phase.AWAITING_HUMAN
            assert unchanged.metadata["delegate_sequence_index"] == 0
        finally:
            await store.close()

    asyncio.run(scenario())


def test_live_stale_review_recovery_keeps_ordinary_legacy_compatibility(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(_db_path(tmp_path))
        await store.initialize()
        try:
            target, _task = await _save_linked_old_wait(
                store,
                "live-ordinary-compatibility",
                phase=Phase.AWAITING_HUMAN,
            )
            report_v2 = report_work_item_id_for_attempt(target.work_item_id, 2)
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id=report_v2,
                    run_id=RUN_ID,
                    parent_work_item_id=target.work_item_id,
                    title="New applied report",
                    kind="report",
                    phase=Phase.APPROVED,
                    batch_index=2,
                    metadata={
                        "report_target_work_item_id": target.work_item_id,
                        "report_card_outcome": "applied",
                    },
                )
            )

            recovered = (
                await store.recover_legacy_human_review_target_from_stale_resolution(
                    target.work_item_id,
                    stale_source_report_work_item_id="superseded-report",
                    metadata_updates={"ordinary_recovery_sentinel": True},
                )
            )
            assert recovered is not None
            assert recovered.phase == Phase.AWAITING_MANAGER_REVIEW
            assert recovered.metadata["ordinary_recovery_sentinel"] is True
        finally:
            await store.close()

    asyncio.run(scenario())


def test_startup_legacy_review_resolution_rejects_delegate_identity_override(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        target = await _save_atomic_delegate_old_wait(
            store,
            "startup-reserved-conflict",
        )
        _report_id, review_id = await _save_report_and_legacy_review(
            store,
            target.work_item_id,
            report_attempt=1,
            resolution_metadata_updates={"created_by_delegate_work": False},
        )
        await store.close()

        restarted = OPCStore(db_path)
        with pytest.raises(ValueError, match="reserved metadata is immutable"):
            await restarted.initialize()
        await restarted.close()

        inspector = OPCStore(db_path)
        await inspector.initialize(run_startup_maintenance=False)
        try:
            unchanged = await inspector.get_delegation_work_item(
                target.work_item_id
            )
            review = await inspector.get_delegation_work_item(review_id)
            assert unchanged is not None
            assert unchanged.phase == Phase.AWAITING_HUMAN
            assert unchanged.metadata["created_by_delegate_work"] is True
            assert unchanged.metadata["delegate_sequence_index"] == 0
            assert review is not None
            assert review.metadata["review_resolution_state"] == "pending"
        finally:
            await inspector.close()

    asyncio.run(scenario())


def test_startup_retires_legacy_human_review_resolution_with_exact_latest_report_cas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        manager_item, _ = await _save_linked_old_wait(
            store,
            "resolution-manager",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        human_item, _ = await _save_linked_old_wait(
            store,
            "resolution-human",
            phase=Phase.AWAITING_HUMAN,
        )
        _, manager_review_id = await _save_report_and_legacy_review(
            store,
            manager_item.work_item_id,
            report_attempt=1,
        )
        _, human_review_id = await _save_report_and_legacy_review(
            store,
            human_item.work_item_id,
            report_attempt=1,
        )
        await store.close()

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            for target_id, review_id in (
                (manager_item.work_item_id, manager_review_id),
                (human_item.work_item_id, human_review_id),
            ):
                target = await restarted.get_delegation_work_item(target_id)
                review = await restarted.get_delegation_work_item(review_id)
                task = await restarted.get_runtime_task_for_work_item(target_id)
                assert target is not None and target.phase == Phase.APPROVED
                assert target.metadata["review_resolution_applied_work_item_id"] == review_id
                audit = target.metadata["review_resolution_retirement"]
                assert audit["original_target_phase"] == Phase.AWAITING_HUMAN.value
                assert audit["normalized_target_phase"] == Phase.APPROVED.value
                assert review is not None
                assert review.metadata["review_resolution_state"] == "applied"
                assert review.metadata["review_work_item_outcome"] == (
                    "legacy_awaiting_human_resolution_retired_to_approved"
                )
                assert task is not None and task.status == TaskStatus.DONE

            persisted_manager_review = await restarted.get_delegation_work_item(
                manager_review_id
            )
            assert persisted_manager_review is not None
            first_review_audit = dict(
                persisted_manager_review.metadata["review_resolution_retirement"]
            )

            with pytest.raises(InvalidPhaseTransition):
                await restarted.apply_delegation_review_resolution(
                    manager_item.work_item_id,
                    source_report_work_item_id="",
                    target_phase=Phase.AWAITING_HUMAN,
                    blocked_reason="",
                    metadata_updates={},
                )
        finally:
            await restarted.close()

        second_restart = OPCStore(db_path)
        await second_restart.initialize()
        try:
            persisted_manager_review = (
                await second_restart.get_delegation_work_item(manager_review_id)
            )
            assert persisted_manager_review is not None
            assert (
                persisted_manager_review.metadata["review_resolution_retirement"]
                == first_review_audit
            )
        finally:
            await second_restart.close()

    asyncio.run(scenario())


def test_stale_legacy_human_resolution_recovers_latest_report_review_without_worker_replay(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        target, _task = await _save_linked_old_wait(
            store,
            "stale-resolution",
            phase=Phase.AWAITING_HUMAN,
        )
        report_v1, review_v1 = await _save_report_and_legacy_review(
            store,
            target.work_item_id,
            report_attempt=1,
        )
        report_v2 = report_work_item_id_for_attempt(target.work_item_id, 2)
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id=report_v2,
                run_id=RUN_ID,
                cell_id="team::cto",
                team_id="team::cto",
                role_id="engineer",
                seat_id=target.seat_id,
                parent_work_item_id=target.work_item_id,
                title="Newest report",
                kind="report",
                projection_id=report_v2,
                phase=Phase.APPROVED,
                batch_index=2,
                metadata={
                    "report_target_work_item_id": target.work_item_id,
                    "report_card_outcome": "applied",
                    "completion_report": "newest report content",
                },
            )
        )
        await store.close()

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            target_after = await restarted.get_delegation_work_item(target.work_item_id)
            old_review = await restarted.get_delegation_work_item(review_v1)
            task_after = await restarted.get_runtime_task_for_work_item(target.work_item_id)
            assert target_after is not None
            assert target_after.phase == Phase.AWAITING_MANAGER_REVIEW
            assert task_after is not None
            assert task_after.status == TaskStatus.AWAITING_MANAGER_REVIEW
            assert task_after.result["artifacts"]["side_effect_token"] == (
                "effect-stale-resolution"
            )
            assert old_review is not None
            assert old_review.metadata["review_resolution_state"] == "stale"
            assert old_review.metadata["review_resolution_stale_reason"] == (
                "source_report_superseded"
            )
            assert report_v1 != report_v2

            executor, original_execute = _executor(restarted, tmp_path)
            await executor._reconcile_missing_review_chain(
                await restarted.list_delegation_work_items(RUN_ID)
            )
            latest_review = await restarted.get_delegation_work_item(
                review_work_item_id_for_attempt(target.work_item_id, 2)
            )
            assert latest_review is not None and latest_review.phase == Phase.READY
            assert latest_review.metadata["review_source_report_work_item_id"] == report_v2
            original_execute.assert_not_awaited()
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_live_reconcile_retires_imported_stale_human_resolution_in_one_pass(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(_db_path(tmp_path))
        await store.initialize()
        try:
            target, _task = await _save_linked_old_wait(
                store,
                "live-stale-resolution",
                phase=Phase.AWAITING_HUMAN,
            )
            _report_v1, review_v1 = await _save_report_and_legacy_review(
                store,
                target.work_item_id,
                report_attempt=1,
            )
            report_v2 = report_work_item_id_for_attempt(target.work_item_id, 2)
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id=report_v2,
                    run_id=RUN_ID,
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="engineer",
                    seat_id=target.seat_id,
                    parent_work_item_id=target.work_item_id,
                    title="Imported newest report",
                    kind="report",
                    projection_id=report_v2,
                    phase=Phase.APPROVED,
                    batch_index=2,
                    metadata={
                        "report_target_work_item_id": target.work_item_id,
                        "report_card_outcome": "applied",
                        "completion_report": "imported newest report content",
                    },
                )
            )

            executor, original_execute = _executor(store, tmp_path)
            await executor._reconcile_missing_review_chain(
                await store.list_delegation_work_items(RUN_ID)
            )

            target_after = await store.get_delegation_work_item(target.work_item_id)
            old_review = await store.get_delegation_work_item(review_v1)
            latest_review = await store.get_delegation_work_item(
                review_work_item_id_for_attempt(target.work_item_id, 2)
            )
            task_after = await store.get_runtime_task_for_work_item(
                target.work_item_id
            )
            assert target_after is not None
            assert target_after.phase == Phase.AWAITING_MANAGER_REVIEW
            assert task_after is not None
            assert task_after.status == TaskStatus.AWAITING_MANAGER_REVIEW
            assert old_review is not None
            assert old_review.metadata["review_resolution_state"] == "stale"
            assert latest_review is not None and latest_review.phase == Phase.READY
            assert latest_review.metadata["review_source_report_work_item_id"] == (
                report_v2
            )
            original_execute.assert_not_awaited()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_startup_repairs_task_projection_after_legacy_retirement_commit_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        target, task = await _save_linked_old_wait(
            store,
            "applied-commit-crash",
            phase=Phase.AWAITING_HUMAN,
        )
        report_id, review_id = await _save_report_and_legacy_review(
            store,
            target.work_item_id,
            report_attempt=1,
        )
        original_result = dict(task.result or {})
        original_effect = task.result["artifacts"]["side_effect_token"]
        await store.close()

        retirement_audit = {
            "kind": "legacy_manager_review_awaiting_human_resolution",
            "review_work_item_id": review_id,
            "source_report_work_item_id": report_id,
            "original_target_phase": Phase.AWAITING_HUMAN.value,
            "normalized_target_phase": Phase.APPROVED.value,
            "retired_at": datetime.now().isoformat(),
        }
        _simulate_unprojected_task_after_work_item_commit(
            db_path,
            work_item_id=target.work_item_id,
            phase=Phase.APPROVED,
            task_id=task.id,
            work_item_metadata_updates={
                "review_resolution_applied_work_item_id": review_id,
                "review_resolution_retirement": retirement_audit,
            },
        )

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            target_after = await restarted.get_delegation_work_item(
                target.work_item_id
            )
            task_after = await restarted.get_task(task.id)
            review_after = await restarted.get_delegation_work_item(review_id)
            assert target_after is not None and target_after.phase == Phase.APPROVED
            assert task_after is not None and task_after.status == TaskStatus.DONE
            assert task_after.execution_lock is False
            assert task_after.execution_locked_at is None
            assert task_after.result == original_result
            assert task_after.result["artifacts"]["side_effect_token"] == original_effect
            assert review_after is not None
            assert review_after.metadata["review_resolution_state"] == "applied"
            assert review_after.metadata["review_resolution_retirement"] == (
                retirement_audit
            )

            executor, original_execute = _executor(restarted, tmp_path)
            await executor._reconcile_missing_review_chain(
                await restarted.list_delegation_work_items(RUN_ID)
            )
            original_execute.assert_not_awaited()
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_startup_repairs_task_projection_after_stale_recovery_commit_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        target, task = await _save_linked_old_wait(
            store,
            "stale-recovery-commit-crash",
            phase=Phase.AWAITING_HUMAN,
        )
        report_v1, review_v1 = await _save_report_and_legacy_review(
            store,
            target.work_item_id,
            report_attempt=1,
        )
        report_v2 = report_work_item_id_for_attempt(target.work_item_id, 2)
        await store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id=report_v2,
                run_id=RUN_ID,
                cell_id="team::cto",
                team_id="team::cto",
                role_id="engineer",
                seat_id=target.seat_id,
                parent_work_item_id=target.work_item_id,
                title="New report after stale recovery crash",
                kind="report",
                projection_id=report_v2,
                phase=Phase.APPROVED,
                batch_index=2,
                metadata={
                    "report_target_work_item_id": target.work_item_id,
                    "report_card_outcome": "applied",
                    "completion_report": "new report after stale recovery crash",
                },
            )
        )
        original_result = dict(task.result or {})
        original_metadata = dict(task.metadata or {})
        await store.close()

        recovery_audit = {
            "kind": "legacy_human_review_target_recovered",
            "stale_review_work_item_id": review_v1,
            "stale_source_report_work_item_id": report_v1,
            "latest_source_report_work_item_id": report_v2,
            "reason": "source_report_superseded",
            "prior_phase": Phase.AWAITING_HUMAN.value,
            "normalized_phase": Phase.AWAITING_MANAGER_REVIEW.value,
            "retired_at": datetime.now().isoformat(),
        }
        _simulate_unprojected_task_after_work_item_commit(
            db_path,
            work_item_id=target.work_item_id,
            phase=Phase.AWAITING_MANAGER_REVIEW,
            task_id=task.id,
            work_item_metadata_updates={
                "review_resolution_retirement": recovery_audit,
                "claimed_by_role_session_id": "",
                "claimed_task_id": "",
            },
        )
        _set_review_resolution_state(
            db_path,
            review_v1,
            state="stale",
            metadata_updates={
                "review_resolution_stale_reason": "source_report_superseded",
                "review_resolution_stale_at": datetime.now().isoformat(),
                "review_work_item_outcome": "superseded_by_newer_report",
            },
        )

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            target_after = await restarted.get_delegation_work_item(
                target.work_item_id
            )
            task_after = await restarted.get_task(task.id)
            review_after = await restarted.get_delegation_work_item(review_v1)
            assert target_after is not None
            assert target_after.phase == Phase.AWAITING_MANAGER_REVIEW
            assert task_after is not None
            assert task_after.status == TaskStatus.AWAITING_MANAGER_REVIEW
            assert task_after.execution_lock is False
            assert task_after.execution_locked_at is None
            assert task_after.result == original_result
            assert task_after.metadata == original_metadata
            assert review_after is not None
            assert review_after.metadata["review_resolution_state"] == "stale"
            assert review_after.metadata["review_resolution_stale_reason"] == (
                "source_report_superseded"
            )

            executor, original_execute = _executor(restarted, tmp_path)
            await executor._reconcile_missing_review_chain(
                await restarted.list_delegation_work_items(RUN_ID)
            )
            latest_review = await restarted.get_delegation_work_item(
                review_work_item_id_for_attempt(target.work_item_id, 2)
            )
            assert latest_review is not None and latest_review.phase == Phase.READY
            original_execute.assert_not_awaited()
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_startup_never_retires_owner_owned_final_delivery_review_journals(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        protected_specs = {
            "deliver-turn": ("deliver", {}),
            "self-evolution-turn": ("self_evolution", {}),
            "human-review-owner": ("execute", {"review_owner_kind": "human"}),
            "authoritative-final": (
                "execute",
                {
                    "authoritative_output": True,
                    "user_visible": True,
                    "feedback_scope": "final",
                },
            ),
        }
        saved: list[tuple[DelegationWorkItem, Task, str]] = []
        for key, (kind, metadata) in protected_specs.items():
            target, task = await _save_linked_old_wait(
                store,
                key,
                kind=kind,
                phase=Phase.AWAITING_HUMAN,
                metadata=metadata,
            )
            _report_id, review_id = await _save_report_and_legacy_review(
                store,
                target.work_item_id,
                report_attempt=1,
            )
            saved.append((target, task, review_id))
        await store.close()

        for target, task, _review_id in saved:
            _simulate_unprojected_task_after_work_item_commit(
                db_path,
                work_item_id=target.work_item_id,
                phase=Phase.AWAITING_HUMAN,
                task_id=task.id,
            )

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            for target, task, review_id in saved:
                target_after = await restarted.get_delegation_work_item(
                    target.work_item_id
                )
                task_after = await restarted.get_task(task.id)
                review_after = await restarted.get_delegation_work_item(review_id)
                assert target_after is not None
                assert target_after.phase == Phase.AWAITING_HUMAN
                assert task_after is not None
                assert task_after.status == TaskStatus.AWAITING_HUMAN
                assert task_after.execution_lock is True
                assert task_after.result == task.result
                assert review_after is not None
                assert review_after.metadata["review_resolution_state"] == "pending"
                assert "review_resolution_retirement" not in review_after.metadata
                assert "review_resolution_applied_work_item_id" not in (
                    target_after.metadata
                )
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_startup_never_retires_review_journal_with_active_explicit_user_input(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db_path = _db_path(tmp_path)
        store = OPCStore(db_path)
        await store.initialize()
        target, task = await _save_linked_old_wait(
            store,
            "explicit-input-review-journal",
            phase=Phase.AWAITING_HUMAN,
        )
        _report_id, review_id = await _save_report_and_legacy_review(
            store,
            target.work_item_id,
            report_attempt=1,
        )
        await store.close()
        _simulate_unprojected_task_after_work_item_commit(
            db_path,
            work_item_id=target.work_item_id,
            phase=Phase.AWAITING_HUMAN,
            task_id=task.id,
        )
        _insert_old_checkpoints(
            db_path,
            [
                (
                    "cp-explicit-input-review-journal",
                    "explicit-input-review-journal",
                    "pending",
                    {
                        "task_id": task.id,
                        "pause_request": {"requires_user_input": True},
                        "prompt": "Choose the product direction",
                    },
                )
            ],
        )

        restarted = OPCStore(db_path)
        await restarted.initialize()
        try:
            target_after = await restarted.get_delegation_work_item(
                target.work_item_id
            )
            task_after = await restarted.get_task(task.id)
            review_after = await restarted.get_delegation_work_item(review_id)
            checkpoint_after = await restarted.get_execution_checkpoint(
                "cp-explicit-input-review-journal",
                project_id=PROJECT_ID,
                checkpoint_type="task_user_input",
            )
            assert target_after is not None
            assert target_after.phase == Phase.AWAITING_HUMAN
            assert task_after is not None
            assert task_after.status == TaskStatus.AWAITING_HUMAN
            assert task_after.execution_lock is True
            assert task_after.result == task.result
            assert review_after is not None
            assert review_after.metadata["review_resolution_state"] == "pending"
            assert "review_resolution_retirement" not in review_after.metadata
            assert checkpoint_after is not None
            assert checkpoint_after.status == "pending"
            assert checkpoint_after.payload["pause_request"][
                "requires_user_input"
            ] is True
        finally:
            await restarted.close()

    asyncio.run(scenario())
