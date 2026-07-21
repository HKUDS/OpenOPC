"""Tests for the verdict-parse-retry path.

When the reviewer agent emits output that the runtime cannot extract
into an approve/reject label, the runtime must NOT silently mark the
worker for rework. Instead it spawns review attempts with a parse-failure
hint; after MAX_VERDICT_PARSE_RETRIES failures, the child is closed as
done/approved with audit metadata because this is a reviewer-side output
failure. Non-final review parse failures must not create human-review
checkpoints or worker rework loops.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    MAX_VERDICT_PARSE_RETRIES,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.work_item_links import set_linked_work_item_id


def _build_executor(store: OPCStore, org_engine: OrgEngine) -> CompanyWorkItemExecutor:
    communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
    return CompanyWorkItemExecutor(
        org_engine=org_engine,
        communication=communication,
        approval_engine=MagicMock(),
        memory=None,
        execute_task=AsyncMock(),
        save_task=store.save_task,
        store=store,
    )


def _make_org_engine(root: Path) -> OrgEngine:
    config = OPCConfig()
    config.org.company_profile = "custom"
    config.org.roles = [
        RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
        RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
        RoleConfig(id="engineer", name="Engineer", responsibility="Build features.", reports_to="cto"),
    ]
    return OrgEngine(config, root)


def _build_review_setup(store: OPCStore) -> tuple[DelegationWorkItem, str]:
    """Persist a child + first review attempt and return them."""
    child = DelegationWorkItem(
        work_item_id="wi-child",
        run_id="run-1",
        cell_id="team::cto",
        team_id="team::cto",
        role_id="engineer",
        seat_id="seat::team::cto::engineer",
        manager_role_id="cto",
        manager_seat_id="seat::team::cto::cto",
        title="Build feature",
        summary="Ship the feature.",
        kind="execute",
        projection_id="wi-child",
        phase=Phase.AWAITING_MANAGER_REVIEW,
        metadata={
            "team_id": "team::cto",
            "seat_id": "seat::team::cto::engineer",
            "manager_role_id": "cto",
            "manager_seat_id": "seat::team::cto::cto",
            "runtime_model": "multi_team_org",
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "review_attempt_count": 1,
        },
    )
    return child, review_work_item_id_for_attempt("wi-child", 1)


def _make_review_card(*, review_card_id: str) -> DelegationWorkItem:
    return DelegationWorkItem(
        work_item_id=review_card_id,
        run_id="run-1",
        cell_id="team::cto",
        team_id="team::cto",
        role_id="cto",
        seat_id="seat::team::cto::cto",
        parent_work_item_id="wi-child",
        manager_role_id="cto",
        manager_seat_id="seat::team::cto::cto",
        title="Review #1",
        summary="Review the deliverable.",
        kind="review",
        projection_id=review_card_id,
        phase=Phase.RUNNING,
        metadata={
            "runtime_model": "multi_team_org",
            "review_execution_work_item": True,
            "review_attempt": 1,
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "review_target_work_item_id": "wi-child",
            "review_target_worker_task_id": "task-engineer",
            "review_target_worker_role_id": "engineer",
            "review_target_worker_seat_id": "seat::team::cto::engineer",
            "review_target_title": "Build feature",
            "review_target_description": "Ship the feature.",
            "review_completion_report": "worker said it's done",
            "current_turn_mode": "review_execute",
            "hidden_from_company_kanban": True,
        },
    )


def _make_review_task(
    *,
    review_card_id: str,
    structured_verdict: object,
) -> Task:
    """Build the Task object the dispatcher would hand to _finalize."""
    task = Task(
        id="task-review-1",
        title="Review #1",
        project_id="proj1",
        session_id="session-cto",
        parent_session_id="session-cto",
        assigned_to="cto",
        status=TaskStatus.DONE,
        metadata={
            "execution_mode": "company_mode",
            "runtime_model": "multi_team_org",
            "work_item_runtime": True,
            "delegation_run_id": "run-1",
            "delegation_team_id": "team::cto",
            "delegation_seat_id": "seat::team::cto::cto",
            "work_item_role_id": "cto",
            "review_execution_work_item": True,
            "review_attempt": 1,
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "review_target_work_item_id": "wi-child",
            "review_target_worker_task_id": "task-engineer",
            "review_target_worker_role_id": "engineer",
            "review_target_worker_seat_id": "seat::team::cto::engineer",
            "review_completion_report": "worker said it's done",
            "structured_review_verdict": structured_verdict,
        },
    )
    set_linked_work_item_id(task, review_card_id)
    return task


class VerdictParseRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_failed_review_card_can_record_parse_failure_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                executor = _build_executor(store, _make_org_engine(root))
                review = _make_review_card(review_card_id="review-failed")
                review.phase = Phase.FAILED
                await store.save_delegation_work_item(review)

                persisted = await executor._persist_terminal_review_card(
                    "review-failed",
                    phase=Phase.CANCELLED,
                    outcome="verdict_parse_failed",
                )

                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.phase, Phase.FAILED)
                self.assertEqual(
                    persisted.metadata.get("review_work_item_outcome"),
                    "verdict_parse_failed",
                )
            finally:
                await store.close()

    async def test_unparseable_verdict_spawns_retry_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                # Worker task must exist — _retry_verdict_parse_failed
                # loads it via store.get_task to spawn the next review.
                worker_task = Task(
                    id="task-engineer",
                    title="Build feature",
                    project_id="proj1",
                    session_id="session-eng",
                    parent_session_id="session-eng",
                    assigned_to="engineer",
                    status=TaskStatus.DONE,
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "delegation_run_id": "run-1",
                        "delegation_team_id": "team::cto",
                        "delegation_seat_id": "seat::team::cto::engineer",
                        "work_item_role_id": "engineer",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::cto",
                    },
                )
                set_linked_work_item_id(worker_task, "wi-child")
                await store.save_task(worker_task)
                await store.link_work_item_runtime_task("wi-child", worker_task.id)

                # Reviewer's structured verdict is empty / unparseable.
                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={"some_other_key": "noise"},
                )
                await executor._finalize_review_work_item(review_task)

                # Child must NOT have moved (still in review).
                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(child_after.phase, Phase.AWAITING_MANAGER_REVIEW)
                # Retry counter should be 1.
                self.assertEqual(
                    int(child_after.metadata.get("review_verdict_parse_retry_count", 0)),
                    1,
                )
                # Old review card is CANCELLED with verdict_parse_failed outcome.
                old_review = await store.get_delegation_work_item(review_id_v1)
                self.assertEqual(old_review.phase, Phase.CANCELLED)
                self.assertEqual(
                    old_review.metadata.get("review_work_item_outcome"),
                    "verdict_parse_failed",
                )
                # Review #2 spawned, with parse-retry hint.
                review_id_v2 = review_work_item_id_for_attempt("wi-child", 2)
                new_review = await store.get_delegation_work_item(review_id_v2)
                self.assertIsNotNone(new_review)
                self.assertEqual(
                    new_review.metadata.get("review_retry_reason"),
                    "verdict_parse_failed",
                )
                self.assertIn(
                    "could not be parsed",
                    str(new_review.metadata.get("review_retry_hint", "")),
                )
            finally:
                await store.close()

    async def test_retry_card_save_failure_stays_in_review_and_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                executor = _build_executor(store, _make_org_engine(root))
                child, review_id_v1 = _build_review_setup(store)
                report_id = report_work_item_id_for_attempt("wi-child", 1)
                child.metadata = {
                    **dict(child.metadata or {}),
                    "report_attempt_count": 1,
                }
                report = DelegationWorkItem(
                    work_item_id=report_id,
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="engineer",
                    seat_id="seat::team::cto::engineer",
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    parent_work_item_id="wi-child",
                    title="Report #1: Build feature",
                    summary="Durable worker report.",
                    kind="report",
                    projection_id=report_id,
                    phase=Phase.APPROVED,
                    batch_index=1,
                    metadata={
                        "runtime_model": "multi_team_org",
                        "work_kind": "report",
                        "report_execution_work_item": True,
                        "report_attempt": 1,
                        "report_target_work_item_id": "wi-child",
                        "report_card_outcome": "applied",
                        "completion_report": "Durable worker report.",
                        "review_evidence": {
                            "completion_summary": "Durable worker report."
                        },
                    },
                )
                review = _make_review_card(review_card_id=review_id_v1)
                review.metadata = {
                    **dict(review.metadata or {}),
                    "review_source_report_work_item_id": report_id,
                }
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(report)
                await store.save_delegation_work_item(review)

                original_insert = store.insert_delegation_work_item_if_absent
                review_id_v2 = review_work_item_id_for_attempt("wi-child", 2)
                injected = False

                async def fail_first_retry_card_insert(
                    item: DelegationWorkItem,
                ) -> bool:
                    nonlocal injected
                    if not injected and item.work_item_id == review_id_v2:
                        injected = True
                        raise RuntimeError("injected review retry save failure")
                    return await original_insert(item)

                store.insert_delegation_work_item_if_absent = AsyncMock(
                    side_effect=fail_first_retry_card_insert
                )
                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={"unparseable": True},
                )
                try:
                    await executor._finalize_review_work_item(review_task)
                finally:
                    store.insert_delegation_work_item_if_absent = original_insert

                self.assertTrue(injected)
                child_after_failure = await store.get_delegation_work_item(
                    "wi-child"
                )
                failed_review = await store.get_delegation_work_item(review_id_v1)
                self.assertEqual(
                    child_after_failure.phase,
                    Phase.AWAITING_MANAGER_REVIEW,
                )
                self.assertFalse(
                    child_after_failure.metadata.get(
                        "review_verdict_parse_failed_auto_done", False
                    )
                )
                self.assertEqual(failed_review.phase, Phase.CANCELLED)
                self.assertEqual(
                    failed_review.metadata.get("review_work_item_outcome"),
                    "verdict_parse_failed",
                )
                self.assertIsNone(
                    await store.get_delegation_work_item(review_id_v2)
                )

                run_items = await store.list_delegation_work_items("run-1")
                await executor._reconcile_missing_review_chain(run_items)
                await executor._reconcile_missing_review_chain(
                    await store.list_delegation_work_items("run-1")
                )

                retry = await store.get_delegation_work_item(review_id_v2)
                self.assertIsNotNone(retry)
                self.assertEqual(retry.phase, Phase.READY)
                self.assertEqual(
                    retry.metadata.get("review_source_report_work_item_id"),
                    report_id,
                )
                self.assertEqual(
                    retry.metadata.get("review_retry_reason"),
                    "verdict_parse_failed",
                )
                self.assertIsNone(
                    await store.get_delegation_work_item(
                        report_work_item_id_for_attempt("wi-child", 2)
                    )
                )
            finally:
                await store.close()

    async def test_retry_preserves_review_owner_when_runtime_task_lost_owner_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                # Mirrors the production failure: the completed review
                # runtime Task no longer carried review_owner_* fields, and
                # review_target_worker_task_id pointed at a report turn whose
                # manager seat was not the reviewer seat.
                report_task = Task(
                    id="task-report",
                    title="Report #1: Build feature",
                    project_id="proj1",
                    session_id="session-report",
                    parent_session_id="session-root",
                    assigned_to="engineer",
                    status=TaskStatus.DONE,
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "delegation_run_id": "run-1",
                        "delegation_team_id": "team::engineer",
                        "delegation_seat_id": "seat::team::engineer::engineer",
                        "work_item_role_id": "engineer",
                        "manager_role_id": "cto",
                        "manager_seat_id": "seat::team::cto::engineer",
                    },
                )
                await store.save_task(report_task)

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={"some_other_key": "noise"},
                )
                review_task.metadata.pop("review_owner_role_id", None)
                review_task.metadata.pop("review_owner_seat_id", None)
                review_task.metadata["review_target_worker_task_id"] = "task-report"

                await executor._finalize_review_work_item(review_task)

                review_id_v2 = review_work_item_id_for_attempt("wi-child", 2)
                new_review = await store.get_delegation_work_item(review_id_v2)
                self.assertIsNotNone(new_review)
                self.assertEqual(new_review.role_id, "cto")
                self.assertEqual(new_review.seat_id, "seat::team::cto::cto")
                self.assertEqual(
                    new_review.metadata.get("review_owner_seat_id"),
                    "seat::team::cto::cto",
                )
                self.assertEqual(new_review.title, "Review #2: Build feature")
                child_after = await store.get_delegation_work_item("wi-child")
                self.assertTrue(
                    CompanyWorkItemExecutor._work_item_is_runnable(
                        new_review,
                        {
                            "wi-child": child_after,
                            review_id_v2: new_review,
                        },
                    )
                )
            finally:
                await store.close()

    async def test_parse_retry_budget_auto_done_without_worker_rework(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                # Already at max retries: the next unparseable verdict
                # must not be charged to the worker as rework.
                child.metadata = dict(child.metadata or {})
                child.metadata["review_verdict_parse_retry_count"] = MAX_VERDICT_PARSE_RETRIES
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={"garbage": True},
                )
                await executor._finalize_review_work_item(review_task)

                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(child_after.phase, Phase.APPROVED)
                self.assertEqual(str(child_after.metadata.get("rework_feedback", "")), "")
                self.assertTrue(child_after.metadata.get("review_verdict_parse_failed_auto_done"))
                self.assertIn(
                    "unparseable",
                    str(child_after.metadata.get("review_parse_failure_feedback", "")),
                )
                self.assertEqual(
                    child_after.metadata.get("structured_review_verdict", {}).get("label"),
                    "approve",
                )
                self.assertEqual(
                    int(child_after.metadata.get("review_rework_count", 0)),
                    0,
                )
                review_after = await store.get_delegation_work_item(review_id_v1)
                self.assertEqual(
                    review_after.metadata.get("review_work_item_outcome"),
                    "verdict_parse_failed_auto_done",
                )
                # No new review card spawned past the cap.
                review_id_v2 = review_work_item_id_for_attempt("wi-child", 2)
                self.assertIsNone(await store.get_delegation_work_item(review_id_v2))
            finally:
                await store.close()

    async def test_parseable_approve_applies_mechanically(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={
                        "label": "approve",
                        "summary": "Looks good, ship it.",
                        "blocking_issues": [],
                        "followups": [],
                    },
                )
                await executor._finalize_review_work_item(review_task)

                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(child_after.phase, Phase.APPROVED)
                self.assertEqual(
                    int(child_after.metadata.get("review_verdict_parse_retry_count", 0)),
                    0,
                    "approve must not touch parse-retry counter",
                )
            finally:
                await store.close()

    async def test_parseable_reject_with_no_issues_still_applies(self) -> None:
        # The runtime no longer second-guesses verdict shape. A reject
        # with empty blocking_issues / followups is mechanically applied
        # as rework — the reviewer was trusted to produce it. (This is
        # the behavioural inverse of the old _verdict_is_actionable
        # path which would have salvaged or degraded.)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={
                        "label": "reject",
                        "summary": "reject",
                        "blocking_issues": [],
                        "followups": [],
                    },
                )
                await executor._finalize_review_work_item(review_task)

                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(child_after.phase, Phase.READY_FOR_REWORK)
                self.assertEqual(
                    int(child_after.metadata.get("review_rework_count", 0)),
                    1,
                )
                # Audit fields from the old salvage/degrade path must
                # NOT be set — that whole machinery is gone.
                self.assertNotIn(
                    "review_verdict_degraded", child_after.metadata,
                )
                self.assertNotIn(
                    "review_verdict_salvaged", child_after.metadata,
                )
            finally:
                await store.close()

    async def test_max_review_reworks_default_is_five_and_auto_done(self) -> None:
        # After the configured rework cap, the runtime stops sending the
        # worker through another rework cycle and marks the item done.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                child.metadata = dict(child.metadata or {})
                child.metadata["review_rework_count"] = 5  # at default cap
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={
                        "label": "reject",
                        "summary": "still not good",
                        "blocking_issues": ["fix X"],
                        "followups": [],
                    },
                )
                await executor._finalize_review_work_item(review_task)

                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(child_after.phase, Phase.APPROVED)
                self.assertTrue(child_after.metadata.get("review_rework_cap_reached_auto_done"))
                self.assertEqual(int(child_after.metadata.get("review_rework_cap", 0)), 5)
                self.assertEqual(
                    int(child_after.metadata.get("review_rework_count_at_auto_done", 0)),
                    5,
                )
                self.assertEqual(int(child_after.metadata.get("review_rework_count", 0)), 5)
                review_after = await store.get_delegation_work_item(review_id_v1)
                self.assertEqual(
                    review_after.metadata.get("review_work_item_outcome"),
                    "auto_done_rework_cap",
                )
            finally:
                await store.close()

    async def test_max_review_reworks_overridable_via_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                child, review_id_v1 = _build_review_setup(store)
                child.metadata = dict(child.metadata or {})
                child.metadata["max_review_reworks"] = 3
                child.metadata["review_rework_count"] = 3  # at custom cap
                await store.save_delegation_work_item(child)
                await store.save_delegation_work_item(_make_review_card(review_card_id=review_id_v1))

                review_task = _make_review_task(
                    review_card_id=review_id_v1,
                    structured_verdict={
                        "label": "reject",
                        "summary": "still failing",
                        "blocking_issues": ["fix Y"],
                        "followups": [],
                    },
                )
                await executor._finalize_review_work_item(review_task)

                child_after = await store.get_delegation_work_item("wi-child")
                self.assertEqual(
                    child_after.phase, Phase.APPROVED,
                    "custom max_review_reworks=3 must stop rework at the third rework",
                )
                self.assertTrue(child_after.metadata.get("review_rework_cap_reached_auto_done"))
                self.assertEqual(int(child_after.metadata.get("review_rework_cap", 0)), 3)
                self.assertEqual(
                    int(child_after.metadata.get("review_rework_count_at_auto_done", 0)),
                    3,
                )
                self.assertEqual(int(child_after.metadata.get("review_rework_count", 0)), 3)
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
