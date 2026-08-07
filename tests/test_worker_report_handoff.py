"""Tests for the two-turn worker→review handoff.

After a worker DONE the runtime no longer treats the last execute-turn
prose as the canonical completion_report. Instead it spawns a hidden
`report::<wid>::v1` work item that resumes the same worker session
under a dedicated report-generation prompt; only after that report
turn finishes does the review card get created. dispatch / delivery
work items skip the report step (they don't need one).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_mode import (
    CompanyWorkItemExecutor,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.phase import DONE_PHASES
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


def _build_child_work_item() -> DelegationWorkItem:
    return DelegationWorkItem(
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
        phase=Phase.RUNNING,
        metadata={
            "team_id": "team::cto",
            "seat_id": "seat::team::cto::engineer",
            "manager_role_id": "cto",
            "manager_seat_id": "seat::team::cto::cto",
            "runtime_model": "multi_team_org",
            "activation_state": "active",
            "work_kind": "execute",
        },
    )


def _build_worker_task() -> Task:
    task = Task(
        id="task-engineer",
        title="Build feature",
        project_id="proj1",
        session_id="session-root",
        parent_session_id="session-root",
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
            "work_item_projection_id": "wi-child",
            "work_kind": "execute",
        },
    )
    set_linked_work_item_id(task, "wi-child")
    return task


class WorkerExecuteDoneSpawnsReportTests(unittest.IsolatedAsyncioTestCase):
    """Phase 1 of the handoff: worker execute turn finishes.

    Expectation: a hidden report work item is created in the worker
    seat's queue. The review work item is NOT created yet — that
    happens only after the report turn finishes.
    """

    async def test_worker_done_spawns_report_card_and_no_review_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)

                await store.save_delegation_work_item(_build_child_work_item())
                worker_task = _build_worker_task()
                await store.save_task(worker_task)

                await executor._apply_done_transition(
                    worker_task,
                    result=TaskResult(
                        status=TaskStatus.DONE,
                        content="All shipped — handoff prose from execute turn.",
                    ),
                )

                report_id = report_work_item_id_for_attempt("wi-child", 1)
                report_card = await store.get_delegation_work_item(report_id)
                self.assertIsNotNone(
                    report_card,
                    "report card must be spawned when worker execute turn finishes",
                )
                self.assertEqual(report_card.kind, "report")
                self.assertEqual(report_card.phase, Phase.READY)
                self.assertTrue(report_card.metadata.get("report_execution_work_item"))
                self.assertTrue(report_card.metadata.get("hidden_from_company_kanban"))
                self.assertEqual(
                    report_card.metadata.get("current_turn_mode"),
                    "report_required",
                )
                self.assertEqual(
                    report_card.metadata.get("report_target_work_item_id"),
                    "wi-child",
                )
                # The same worker seat owns the report card — it's the
                # worker's own session being resumed for the handoff.
                self.assertEqual(report_card.role_id, "engineer")
                self.assertEqual(report_card.seat_id, "seat::team::cto::engineer")

                # The review card must NOT exist yet.
                review_id = review_work_item_id_for_attempt("wi-child", 1)
                self.assertIsNone(
                    await store.get_delegation_work_item(review_id),
                    "review card must wait for the report turn to finish",
                )
            finally:
                await store.close()

    async def test_dispatch_kind_skips_report_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)

                # Dispatch card that actually delegated (a live child card
                # exists in the store). Delegated output routes directly to
                # APPROVED — the children carry the reviewable output, so no
                # review and no report turn for the dispatch card itself.
                # (A dispatch card WITHOUT children is the self-produced
                # case and does get the report/review chain.)
                child = _build_child_work_item()
                child.metadata = dict(child.metadata or {})
                child.metadata["work_kind"] = "dispatch"
                await store.save_delegation_work_item(child)
                delegated = _build_child_work_item()
                delegated.work_item_id = "wi-grandchild"
                delegated.projection_id = "wi-grandchild"
                delegated.parent_work_item_id = "wi-child"
                await store.save_delegation_work_item(delegated)
                worker_task = _build_worker_task()
                worker_task.metadata = dict(worker_task.metadata or {})
                worker_task.metadata["work_kind"] = "dispatch"
                # Delegation is attempt-scoped.  The child row alone may be
                # historical, so mirror the tool's current-turn mutation
                # marker instead of asking DONE routing to infer from rows.
                worker_task.metadata["manager_board_mutation_performed"] = True
                await store.save_task(worker_task)

                await executor._apply_done_transition(
                    worker_task,
                    result=TaskResult(status=TaskStatus.DONE, content="dispatched"),
                )

                self.assertIsNone(
                    await store.get_delegation_work_item(
                        report_work_item_id_for_attempt("wi-child", 1)
                    ),
                    "dispatch DONE must not spawn a report turn",
                )
                self.assertIsNone(
                    await store.get_delegation_work_item(
                        review_work_item_id_for_attempt("wi-child", 1)
                    ),
                    "dispatch DONE must not spawn a review turn",
                )
            finally:
                await store.close()


class ReportTurnDoneSpawnsReviewTests(unittest.IsolatedAsyncioTestCase):
    """Phase 2 of the handoff: the report turn finishes.

    Expectation: the review work item is now spawned, and the
    completion_report it carries is the report turn's output (not the
    original execute prose). The hidden report card itself transitions
    to APPROVED.
    """

    async def _setup_after_execute_done(
        self, store: OPCStore, executor: CompanyWorkItemExecutor
    ) -> tuple[Task, str]:
        await store.save_delegation_work_item(_build_child_work_item())
        worker_task = _build_worker_task()
        await store.save_task(worker_task)
        await executor._apply_done_transition(
            worker_task,
            result=TaskResult(
                status=TaskStatus.DONE,
                content="execute turn prose — should NOT end up as completion_report",
            ),
        )
        report_id = report_work_item_id_for_attempt("wi-child", 1)
        report_card = await store.get_delegation_work_item(report_id)
        self.assertIsNotNone(report_card)
        # In production the dispatcher claims the report card
        # (READY → RUNNING) before the worker actually runs the report
        # turn. We bypass the dispatcher here, so flip it manually so
        # _apply_report_done_transition can later close it RUNNING →
        # APPROVED via the canonical transition table.
        await store.update_delegation_work_item(report_id, phase=Phase.RUNNING)
        return worker_task, report_id

    def _report_turn_task(
        self, *, report_card_id: str, target_work_item_id: str
    ) -> Task:
        # The materialized Task that the dispatcher would build for the
        # report card. We construct it directly here.
        task = Task(
            id="task-report-1",
            title="Write handoff report",
            project_id="proj1",
            session_id="session-root",
            parent_session_id="session-root",
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
                "report_execution_work_item": True,
                "report_target_work_item_id": target_work_item_id,
                "work_kind": "report",
                "work_item_turn_type": "report",
                "current_turn_mode": "report_required",
            },
        )
        set_linked_work_item_id(task, report_card_id)
        set_linked_work_item_id(task, report_card_id)
        return task

    async def test_report_turn_done_spawns_review_with_report_as_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)

                _worker_task, report_id = await self._setup_after_execute_done(
                    store, executor
                )

                # Need to also set the parent's review_owner_role_id /
                # review_owner_seat_id, which the canonical execute-DONE
                # path stamps on the parent metadata. (Done by the
                # earlier _apply_done_transition call.) Sanity check:
                parent = await store.get_delegation_work_item("wi-child")
                self.assertIsNotNone(parent)
                self.assertEqual(parent.metadata.get("review_owner_role_id"), "cto")

                report_task = self._report_turn_task(
                    report_card_id=report_id, target_work_item_id="wi-child"
                )
                report_payload = (
                    "Handoff report:\n\n"
                    '{"summary":"Built the feature with tests.",\n'
                    ' "deliverables":[{"name":"feature.py","path":"/tmp/feature.py","status":"complete"}],\n'
                    ' "acceptance_status":[{"criterion":"feature works","met":true,"evidence":"tests pass"}],\n'
                    ' "risks":["minor flakiness on Windows"],\n'
                    ' "next_actions":["reviewer to verify integration test"]}'
                )
                await executor._apply_done_transition(
                    report_task,
                    result=TaskResult(status=TaskStatus.DONE, content=report_payload),
                )

                review_id = review_work_item_id_for_attempt("wi-child", 1)
                review_card = await store.get_delegation_work_item(review_id)
                self.assertIsNotNone(
                    review_card,
                    "review card must be spawned after report turn finishes",
                )
                self.assertEqual(review_card.kind, "review")
                self.assertEqual(
                    review_card.metadata.get("review_completion_report"),
                    report_payload,
                    "review_completion_report must come from the report turn, not the execute turn",
                )
                evidence = review_card.metadata.get("review_evidence", {}) or {}
                worker_report = evidence.get("worker_report") or {}
                self.assertIn(
                    "Built the feature",
                    str(worker_report.get("summary", "")),
                    "parsed worker report should be merged into review_evidence",
                )

                # The hidden report card itself is now APPROVED.
                report_card_after = await store.get_delegation_work_item(report_id)
                self.assertEqual(report_card_after.phase, Phase.APPROVED)
            finally:
                await store.close()

    async def test_report_json_parsing_failure_falls_back_to_full_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = _make_org_engine(root)
                executor = _build_executor(store, org_engine)
                _worker_task, report_id = await self._setup_after_execute_done(
                    store, executor
                )

                # Pure prose handoff — no JSON. Per design we DO NOT
                # re-prompt the worker; we hand the prose to the
                # reviewer as-is.
                pure_prose = "I built the thing. It works. Tests pass. No JSON."
                report_task = self._report_turn_task(
                    report_card_id=report_id, target_work_item_id="wi-child"
                )
                await executor._apply_done_transition(
                    report_task,
                    result=TaskResult(status=TaskStatus.DONE, content=pure_prose),
                )

                review_id = review_work_item_id_for_attempt("wi-child", 1)
                review_card = await store.get_delegation_work_item(review_id)
                self.assertIsNotNone(review_card)
                self.assertEqual(
                    review_card.metadata.get("review_completion_report"),
                    pure_prose,
                    "prose handoff must reach the reviewer verbatim",
                )
                # No worker_report field when parsing failed (or it's empty).
                evidence = review_card.metadata.get("review_evidence", {}) or {}
                self.assertFalse(
                    evidence.get("worker_report"),
                    "no worker_report block expected when parsing failed",
                )
            finally:
                await store.close()


class ReviewChainRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Crash boundaries use persisted auxiliary cards as their journal.

    The attempt counters on the parent are only lookup caches: a crash can
    happen after an auxiliary card commits but before its counter does.  The
    report card also has to contain the completed report before review-card
    creation is attempted, so reconciliation can resume without a live Task.
    """

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.executor = _build_executor(self.store, _make_org_engine(self.root))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def _save_awaiting_parent(self) -> DelegationWorkItem:
        parent = _build_child_work_item()
        parent.phase = Phase.AWAITING_MANAGER_REVIEW
        parent.metadata = {
            **dict(parent.metadata or {}),
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "completion_report": "execute-turn fallback",
        }
        await self.store.save_delegation_work_item(parent)
        return parent

    async def _run_reconcile(self) -> list[DelegationWorkItem]:
        items = await self.store.list_delegation_work_items("run-1")
        return await self.executor._reconcile_missing_review_chain(items)

    async def _auxiliary_cards(self) -> list[DelegationWorkItem]:
        return [
            item
            for item in await self.store.list_delegation_work_items("run-1")
            if str((item.metadata or {}).get("report_target_work_item_id", "") or "").strip()
            == "wi-child"
            or str((item.metadata or {}).get("review_target_work_item_id", "") or "").strip()
            == "wi-child"
        ]

    async def _setup_running_report(
        self,
    ) -> tuple[str, Task]:
        """Create the real execute->report handoff and claim its report card."""
        await self.store.save_delegation_work_item(_build_child_work_item())
        worker_task = _build_worker_task()
        await self.executor._apply_done_transition(
            worker_task,
            result=TaskResult(status=TaskStatus.DONE, content="execute fallback"),
        )
        report_id = report_work_item_id_for_attempt("wi-child", 1)
        role_session_id = "role-runtime::run-1::engineer"
        await self.store.update_delegation_work_item(
            report_id,
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id=role_session_id,
            claimed_by_seat_id="seat::team::cto::engineer",
            metadata_updates={
                "claimed_by_role_session_id": role_session_id,
                "claimed_task_id": "task-report-1",
            },
        )
        report_task = ReportTurnDoneSpawnsReviewTests()._report_turn_task(
            report_card_id=report_id,
            target_work_item_id="wi-child",
        )
        return report_id, report_task

    async def _setup_running_review(
        self,
        *,
        verdict: dict[str, object],
    ) -> tuple[str, str, Task]:
        """Create and claim a review card using the production report path."""
        report_id, report_task = await self._setup_running_report()
        await self.executor._apply_report_done_transition(
            report_task,
            result=TaskResult(status=TaskStatus.DONE, content="durable report v1"),
        )
        review_id = review_work_item_id_for_attempt("wi-child", 1)
        role_session_id = "role-runtime::run-1::cto"
        await self.store.update_delegation_work_item(
            review_id,
            phase=Phase.RUNNING,
            claimed_by_role_runtime_session_id=role_session_id,
            claimed_by_seat_id="seat::team::cto::cto",
            metadata_updates={
                "claimed_by_role_session_id": role_session_id,
                "claimed_task_id": "task-review-1",
            },
        )
        review_item = await self.store.get_delegation_work_item(review_id)
        self.assertIsNotNone(review_item)
        review_task = Task(
            id="task-review-1",
            title="Review #1: Build feature",
            project_id="proj1",
            session_id="session-cto",
            parent_session_id="session-root",
            assigned_to="cto",
            status=TaskStatus.DONE,
            metadata={
                **dict(review_item.metadata or {}),
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "work_item_runtime": True,
                "review_execution_work_item": True,
                "structured_review_verdict": verdict,
            },
        )
        set_linked_work_item_id(review_task, review_id)
        return report_id, review_id, review_task

    async def test_insert_if_absent_preserves_claimed_deterministic_aux_card(self) -> None:
        cases = (
            (
                "report",
                report_work_item_id_for_attempt("wi-child", 1),
                "engineer",
                "seat::team::cto::engineer",
                {"report_target_work_item_id": "wi-child", "report_attempt": 1},
            ),
            (
                "review",
                review_work_item_id_for_attempt("wi-child", 1),
                "cto",
                "seat::team::cto::cto",
                {"review_target_work_item_id": "wi-child", "review_attempt": 1},
            ),
        )

        for kind, work_item_id, role_id, seat_id, target_metadata in cases:
            with self.subTest(kind=kind):
                ready = DelegationWorkItem(
                    work_item_id=work_item_id,
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id=role_id,
                    seat_id=seat_id,
                    parent_work_item_id="wi-child",
                    title=f"{kind.title()} attempt 1",
                    kind=kind,
                    projection_id=work_item_id,
                    phase=Phase.READY,
                    batch_index=1,
                    metadata={
                        "work_kind": kind,
                        **target_metadata,
                        "persisted_sentinel": f"original-{kind}",
                    },
                )
                self.assertTrue(
                    await self.store.insert_delegation_work_item_if_absent(ready)
                )

                role_session_id = f"role-runtime::run-1::{role_id}"
                claimed_task_id = f"task-{kind}-1"
                await self.store.update_delegation_work_item(
                    work_item_id,
                    phase=Phase.RUNNING,
                    claimed_by_role_runtime_session_id=role_session_id,
                    claimed_by_seat_id=seat_id,
                    metadata_updates={
                        "claimed_by_role_session_id": role_session_id,
                        "claimed_task_id": claimed_task_id,
                    },
                )

                competing_ready = DelegationWorkItem(
                    work_item_id=work_item_id,
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id=role_id,
                    seat_id=seat_id,
                    parent_work_item_id="wi-child",
                    title=f"Competing {kind} attempt 1",
                    kind=kind,
                    projection_id=work_item_id,
                    phase=Phase.READY,
                    batch_index=1,
                    metadata={
                        "work_kind": kind,
                        **target_metadata,
                        "persisted_sentinel": f"competing-{kind}",
                    },
                )

                inserted = await self.store.insert_delegation_work_item_if_absent(
                    competing_ready
                )
                persisted = await self.store.get_delegation_work_item(work_item_id)

                self.assertFalse(inserted)
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted.phase, Phase.RUNNING)
                self.assertEqual(
                    persisted.claimed_by_role_runtime_session_id,
                    role_session_id,
                )
                self.assertEqual(persisted.claimed_by_seat_id, seat_id)
                self.assertEqual(
                    persisted.metadata.get("claimed_by_role_session_id"),
                    role_session_id,
                )
                self.assertEqual(
                    persisted.metadata.get("claimed_task_id"), claimed_task_id
                )
                self.assertEqual(
                    persisted.metadata.get("persisted_sentinel"),
                    f"original-{kind}",
                )

    async def test_report_terminal_write_failure_releases_claim_for_retry(self) -> None:
        report_id, report_task = await self._setup_running_report()
        original_update = self.store.update_delegation_work_item
        injected = False

        async def fail_first_terminal_report_write(work_item_id: str, **kwargs):
            nonlocal injected
            metadata_updates = dict(kwargs.get("metadata_updates") or {})
            if (
                not injected
                and work_item_id == report_id
                and kwargs.get("phase") == Phase.APPROVED
                and metadata_updates.get("report_card_outcome") == "applied"
            ):
                injected = True
                raise RuntimeError("injected terminal report journal failure")
            return await original_update(work_item_id, **kwargs)

        self.store.update_delegation_work_item = AsyncMock(
            side_effect=fail_first_terminal_report_write
        )
        try:
            await self.executor._apply_report_done_transition(
                report_task,
                result=TaskResult(status=TaskStatus.DONE, content="volatile report"),
            )
        finally:
            self.store.update_delegation_work_item = original_update

        self.assertTrue(injected)
        parent = await self.store.get_delegation_work_item("wi-child")
        report = await self.store.get_delegation_work_item(report_id)
        self.assertEqual(parent.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(report.phase, Phase.RUNNING)
        self.assertEqual(report.claimed_by_role_runtime_session_id, "")
        self.assertEqual(report.claimed_by_seat_id, "")
        self.assertEqual(report.metadata.get("claimed_by_role_session_id"), "")
        self.assertEqual(report.metadata.get("claimed_task_id"), "")
        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(
                report,
                {"wi-child": parent, report_id: report},
            )
        )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 1)
            )
        )

    async def test_review_terminal_write_failure_keeps_parent_reviewable(self) -> None:
        report_id, review_id, review_task = await self._setup_running_review(
            verdict={
                "label": "reject",
                "summary": "needs rework",
                "blocking_issues": ["fix the defect"],
                "followups": [],
            }
        )
        original_update = self.store.update_delegation_work_item
        injected = False

        async def fail_first_terminal_review_write(work_item_id: str, **kwargs):
            nonlocal injected
            if (
                not injected
                and work_item_id == review_id
                and kwargs.get("phase") in DONE_PHASES
            ):
                injected = True
                raise RuntimeError("injected terminal review journal failure")
            return await original_update(work_item_id, **kwargs)

        self.store.update_delegation_work_item = AsyncMock(
            side_effect=fail_first_terminal_review_write
        )
        try:
            await self.executor._finalize_review_work_item(review_task)
        finally:
            self.store.update_delegation_work_item = original_update

        self.assertTrue(injected)
        parent = await self.store.get_delegation_work_item("wi-child")
        review = await self.store.get_delegation_work_item(review_id)
        self.assertEqual(parent.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(review.phase, Phase.RUNNING)
        self.assertEqual(review.claimed_by_role_runtime_session_id, "")
        self.assertEqual(review.claimed_by_seat_id, "")
        self.assertEqual(review.metadata.get("claimed_by_role_session_id"), "")
        self.assertEqual(review.metadata.get("claimed_task_id"), "")
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 2)
            )
        )
        self.assertEqual(
            review.metadata.get("review_source_report_work_item_id"),
            report_id,
        )

    async def test_late_review_cannot_override_parent_awaiting_human(self) -> None:
        _report_id, review_id, review_task = await self._setup_running_review(
            verdict={
                "label": "approve",
                "summary": "approve from a now-stale manager turn",
                "blocking_issues": [],
                "followups": [],
            }
        )
        await self.store.update_delegation_work_item(
            "wi-child",
            phase=Phase.AWAITING_HUMAN,
            metadata_updates={"human_checkpoint_sentinel": "must-survive"},
        )

        await self.executor._finalize_review_work_item(review_task)

        parent = await self.store.get_delegation_work_item("wi-child")
        review = await self.store.get_delegation_work_item(review_id)
        self.assertEqual(parent.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(
            parent.metadata.get("human_checkpoint_sentinel"), "must-survive"
        )
        self.assertNotIn("review_resolution_applied_work_item_id", parent.metadata)
        self.assertNotIn("structured_review_verdict", parent.metadata)
        self.assertNotIn("reviewed_at", parent.metadata)

        self.assertIn(review.phase, DONE_PHASES)
        self.assertEqual(
            review.metadata.get("review_work_item_outcome"),
            "target_no_longer_awaiting_manager_review",
        )
        self.assertNotEqual(
            review.metadata.get("review_resolution_state"), "applied"
        )
        self.assertNotIn("review_resolution", review.metadata)
        self.assertEqual(review.claimed_by_role_runtime_session_id, "")
        self.assertEqual(review.claimed_by_seat_id, "")

    async def test_late_review_is_stale_when_newer_applied_report_exists(self) -> None:
        report_v1_id, review_v1_id, review_v1_task = (
            await self._setup_running_review(
                verdict={
                    "label": "approve",
                    "summary": "approval based on report v1",
                    "blocking_issues": [],
                    "followups": [],
                }
            )
        )
        report_v2_id = report_work_item_id_for_attempt("wi-child", 2)
        report_v2 = DelegationWorkItem(
            work_item_id=report_v2_id,
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            manager_role_id="cto",
            manager_seat_id="seat::team::cto::cto",
            parent_work_item_id="wi-child",
            title="Report attempt 2",
            summary="Newer durable handoff.",
            kind="report",
            projection_id=report_v2_id,
            phase=Phase.APPROVED,
            batch_index=2,
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "report",
                "report_execution_work_item": True,
                "report_target_work_item_id": "wi-child",
                "report_attempt": 2,
                "report_card_outcome": "applied",
                "completion_report": "authoritative report v2",
            },
        )
        await self.store.save_delegation_work_item(report_v2)
        await self.store.update_delegation_work_item(
            "wi-child",
            metadata_updates={
                "completion_report": "authoritative report v2",
                "newer_report_sentinel": "must-survive",
            },
        )

        await self.executor._finalize_review_work_item(review_v1_task)

        parent = await self.store.get_delegation_work_item("wi-child")
        review_v1 = await self.store.get_delegation_work_item(review_v1_id)
        self.assertEqual(parent.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(parent.metadata.get("completion_report"), "authoritative report v2")
        self.assertEqual(parent.metadata.get("newer_report_sentinel"), "must-survive")
        self.assertNotIn("review_resolution_applied_work_item_id", parent.metadata)
        self.assertNotIn("structured_review_verdict", parent.metadata)
        self.assertNotIn("reviewed_at", parent.metadata)

        self.assertIn(review_v1.phase, DONE_PHASES)
        self.assertEqual(
            review_v1.metadata.get("review_source_report_work_item_id"),
            report_v1_id,
        )
        self.assertEqual(review_v1.metadata.get("review_resolution_state"), "stale")
        self.assertEqual(
            review_v1.metadata.get("review_resolution_stale_reason"),
            "source_report_superseded",
        )
        self.assertEqual(
            review_v1.metadata.get("review_work_item_outcome"),
            "superseded_by_newer_report",
        )
        self.assertEqual(
            (review_v1.metadata.get("review_resolution") or {}).get(
                "source_report_work_item_id"
            ),
            report_v1_id,
        )

    async def test_reconcile_replays_terminal_review_after_parent_write_failure(
        self,
    ) -> None:
        report_id, review_id, review_task = await self._setup_running_review(
            verdict={
                "label": "reject",
                "summary": "needs rework",
                "blocking_issues": ["fix the defect"],
                "followups": [],
            }
        )
        original_apply = self.store.apply_delegation_review_resolution
        injected = False

        async def fail_first_parent_projection(work_item_id: str, **kwargs):
            nonlocal injected
            if not injected and work_item_id == "wi-child":
                injected = True
                raise RuntimeError("injected child verdict projection failure")
            return await original_apply(work_item_id, **kwargs)

        self.store.apply_delegation_review_resolution = AsyncMock(
            side_effect=fail_first_parent_projection
        )
        try:
            await self.executor._finalize_review_work_item(review_task)
        finally:
            self.store.apply_delegation_review_resolution = original_apply

        self.assertTrue(injected)
        parent_before = await self.store.get_delegation_work_item("wi-child")
        review_before = await self.store.get_delegation_work_item(review_id)
        self.assertEqual(parent_before.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertIn(review_before.phase, DONE_PHASES)
        self.assertEqual(
            review_before.metadata.get("review_source_report_work_item_id"),
            report_id,
        )

        await self._run_reconcile()
        await self._run_reconcile()

        parent_after = await self.store.get_delegation_work_item("wi-child")
        self.assertEqual(parent_after.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(
            (parent_after.metadata.get("structured_review_verdict") or {}).get(
                "label"
            ),
            "reject",
        )
        self.assertEqual(parent_after.metadata.get("review_rework_count"), 1)
        self.assertIn(
            "fix the defect",
            str(parent_after.metadata.get("rework_feedback", "")),
        )
        self.assertEqual(
            parent_after.metadata.get("review_resolution_applied_work_item_id"),
            review_id,
        )
        report_cards = [
            item for item in await self._auxiliary_cards() if item.kind == "report"
        ]
        review_cards = [
            item for item in await self._auxiliary_cards() if item.kind == "review"
        ]
        self.assertEqual([item.work_item_id for item in report_cards], [report_id])
        self.assertEqual([item.work_item_id for item in review_cards], [review_id])

        # A later worker attempt re-enters review with the old journal still
        # present. The atomic applied stamp must make that verdict immutable
        # history, not a resolution to replay onto the new output.
        await self.store.update_delegation_work_item(
            "wi-child",
            phase=Phase.RUNNING,
        )
        await self.store.update_delegation_work_item(
            "wi-child",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        await self._run_reconcile()

        next_cycle_parent = await self.store.get_delegation_work_item("wi-child")
        self.assertEqual(next_cycle_parent.phase, Phase.AWAITING_MANAGER_REVIEW)
        next_report = await self.store.get_delegation_work_item(
            report_work_item_id_for_attempt("wi-child", 2)
        )
        self.assertIsNotNone(next_report)
        self.assertEqual(next_report.phase, Phase.READY)
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_report_card_commit_survives_parent_counter_failure(self) -> None:
        await self._save_awaiting_parent()
        worker_task = _build_worker_task()

        original_update = self.store.update_delegation_work_item

        async def fail_report_counter(work_item_id: str, **kwargs):
            metadata_updates = dict(kwargs.get("metadata_updates") or {})
            if work_item_id == "wi-child" and "report_attempt_count" in metadata_updates:
                raise RuntimeError("injected crash after report-card commit")
            return await original_update(work_item_id, **kwargs)

        self.store.update_delegation_work_item = AsyncMock(side_effect=fail_report_counter)

        first = await self.executor._ensure_report_work_item_for_work_item(
            "wi-child", worker_task=worker_task
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.work_item_id, report_work_item_id_for_attempt("wi-child", 1))
        parent = await self.store.get_delegation_work_item("wi-child")
        self.assertNotIn("report_attempt_count", parent.metadata or {})

        # Make a re-save of v1 observably wrong: retry must discover and
        # return the persisted RUNNING card instead of trying READY again.
        await original_update(first.work_item_id, phase=Phase.RUNNING)
        second = await self.executor._ensure_report_work_item_for_work_item(
            "wi-child", worker_task=worker_task
        )

        self.assertIsNotNone(second)
        self.assertEqual(second.work_item_id, first.work_item_id)
        self.assertEqual(second.phase, Phase.RUNNING)
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_review_card_commit_survives_parent_counter_failure(self) -> None:
        await self._save_awaiting_parent()
        worker_task = _build_worker_task()

        original_update = self.store.update_delegation_work_item

        async def fail_review_counter(work_item_id: str, **kwargs):
            metadata_updates = dict(kwargs.get("metadata_updates") or {})
            if work_item_id == "wi-child" and "review_attempt_count" in metadata_updates:
                raise RuntimeError("injected crash after review-card commit")
            return await original_update(work_item_id, **kwargs)

        self.store.update_delegation_work_item = AsyncMock(side_effect=fail_review_counter)

        first = await self.executor._ensure_review_work_item_for_work_item(
            "wi-child",
            worker_task=worker_task,
            completion_report="handoff",
            metadata_updates={
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
            },
        )
        self.assertIsNotNone(first)
        self.assertEqual(first.work_item_id, review_work_item_id_for_attempt("wi-child", 1))
        parent = await self.store.get_delegation_work_item("wi-child")
        self.assertNotIn("review_attempt_count", parent.metadata or {})

        await original_update(first.work_item_id, phase=Phase.RUNNING)
        second = await self.executor._ensure_review_work_item_for_work_item(
            "wi-child",
            worker_task=worker_task,
            completion_report="handoff",
            metadata_updates={
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
            },
        )

        self.assertIsNotNone(second)
        self.assertEqual(second.work_item_id, first.work_item_id)
        self.assertEqual(second.phase, Phase.RUNNING)
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_reconcile_recovers_review_from_terminal_report(self) -> None:
        # Build the real report turn first so this exercises the exact durable
        # payload written at the report-DONE crash boundary.
        await self.store.save_delegation_work_item(_build_child_work_item())
        worker_task = _build_worker_task()
        await self.executor._apply_done_transition(
            worker_task,
            result=TaskResult(status=TaskStatus.DONE, content="execute fallback"),
        )
        report_id = report_work_item_id_for_attempt("wi-child", 1)
        await self.store.update_delegation_work_item(report_id, phase=Phase.RUNNING)
        report_task = ReportTurnDoneSpawnsReviewTests()._report_turn_task(
            report_card_id=report_id,
            target_work_item_id="wi-child",
        )
        report_payload = (
            '{"summary":"durable handoff","deliverables":[],"risks":[],"next_actions":[]}'
        )

        original_insert = self.store.insert_delegation_work_item_if_absent

        async def fail_review_insert(
            item: DelegationWorkItem,
        ) -> bool:
            if item.kind == "review":
                raise RuntimeError("injected crash while saving review card")
            return await original_insert(item)

        self.store.insert_delegation_work_item_if_absent = AsyncMock(
            side_effect=fail_review_insert
        )
        await self.executor._apply_report_done_transition(
            report_task,
            result=TaskResult(status=TaskStatus.DONE, content=report_payload),
        )

        terminal_report = await self.store.get_delegation_work_item(report_id)
        self.assertEqual(terminal_report.phase, Phase.APPROVED)
        self.assertEqual(terminal_report.metadata.get("report_card_outcome"), "applied")
        self.assertEqual(terminal_report.metadata.get("completion_report"), report_payload)
        self.assertEqual(terminal_report.metadata.get("report_completion_raw"), report_payload)
        self.assertTrue(terminal_report.metadata.get("review_evidence"))
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 1)
            )
        )

        # Simulate restart: there is no runtime Task available to carry the
        # payload, only the parent and terminal report rows.
        self.store.insert_delegation_work_item_if_absent = original_insert
        self.assertEqual(await self.store.get_tasks(), [])
        await self._run_reconcile()
        await self._run_reconcile()

        review = await self.store.get_delegation_work_item(
            review_work_item_id_for_attempt("wi-child", 1)
        )
        self.assertIsNotNone(review)
        self.assertEqual(review.metadata.get("review_completion_report"), report_payload)
        self.assertEqual(
            review.metadata.get("review_source_report_work_item_id"),
            report_id,
        )
        self.assertEqual(
            (review.metadata.get("review_evidence") or {}).get("completion_summary"),
            report_payload,
        )
        reports = [item for item in await self._auxiliary_cards() if item.kind == "report"]
        self.assertEqual([item.work_item_id for item in reports], [report_id])
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_reconcile_repairs_parent_after_terminal_report_projection_failure(
        self,
    ) -> None:
        await self.store.save_delegation_work_item(_build_child_work_item())
        worker_task = _build_worker_task()
        await self.executor._apply_done_transition(
            worker_task,
            result=TaskResult(status=TaskStatus.DONE, content="execute fallback"),
        )
        report_id = report_work_item_id_for_attempt("wi-child", 1)
        await self.store.update_delegation_work_item(report_id, phase=Phase.RUNNING)
        report_task = ReportTurnDoneSpawnsReviewTests()._report_turn_task(
            report_card_id=report_id,
            target_work_item_id="wi-child",
        )
        report_payload = "Report payload committed before the parent projection."

        original_update = self.store.update_delegation_work_item

        async def fail_parent_projection(work_item_id: str, **kwargs):
            metadata_updates = dict(kwargs.get("metadata_updates") or {})
            if (
                work_item_id == "wi-child"
                and metadata_updates.get("completion_report") == report_payload
            ):
                raise RuntimeError("injected crash while projecting report to parent")
            return await original_update(work_item_id, **kwargs)

        self.store.update_delegation_work_item = AsyncMock(
            side_effect=fail_parent_projection
        )
        await self.executor._apply_report_done_transition(
            report_task,
            result=TaskResult(status=TaskStatus.DONE, content=report_payload),
        )

        terminal_report = await self.store.get_delegation_work_item(report_id)
        self.assertEqual(terminal_report.phase, Phase.APPROVED)
        self.assertEqual(terminal_report.metadata.get("report_card_outcome"), "applied")
        self.assertEqual(terminal_report.metadata.get("completion_report"), report_payload)
        parent_before_reconcile = await self.store.get_delegation_work_item("wi-child")
        self.assertNotEqual(
            (parent_before_reconcile.metadata or {}).get("completion_report"),
            report_payload,
        )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                review_work_item_id_for_attempt("wi-child", 1)
            )
        )

        self.store.update_delegation_work_item = original_update
        await self._run_reconcile()
        await self._run_reconcile()

        parent_after_reconcile = await self.store.get_delegation_work_item("wi-child")
        self.assertEqual(
            parent_after_reconcile.metadata.get("completion_report"),
            report_payload,
        )
        review = await self.store.get_delegation_work_item(
            review_work_item_id_for_attempt("wi-child", 1)
        )
        self.assertIsNotNone(review)
        self.assertEqual(review.metadata.get("review_completion_report"), report_payload)
        self.assertEqual(
            review.metadata.get("review_source_report_work_item_id"),
            report_id,
        )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_durable_v2_overrides_lagging_attempt_counters(self) -> None:
        await self._save_awaiting_parent()
        await self.store.update_delegation_work_item(
            "wi-child",
            metadata_updates={
                "report_attempt_count": 1,
                "review_attempt_count": 1,
            },
        )
        terminal_report = DelegationWorkItem(
            work_item_id=report_work_item_id_for_attempt("wi-child", 2),
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            manager_role_id="cto",
            manager_seat_id="seat::team::cto::cto",
            parent_work_item_id="wi-child",
            title="Terminal report v2",
            summary="Immutable report history.",
            kind="report",
            projection_id=report_work_item_id_for_attempt("wi-child", 2),
            phase=Phase.APPROVED,
            batch_index=2,
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "report",
                "report_execution_work_item": True,
                "report_target_work_item_id": "wi-child",
                "report_attempt": 2,
                "report_card_outcome": "applied",
                "completion_report": "Immutable report history.",
                "history_sentinel": "report-v2-must-not-change",
            },
        )
        terminal_review = DelegationWorkItem(
            work_item_id=review_work_item_id_for_attempt("wi-child", 2),
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            manager_role_id="ceo",
            manager_seat_id="seat::team::cto::ceo",
            parent_work_item_id="wi-child",
            title="Terminal review v2",
            summary="Immutable review history.",
            kind="review",
            projection_id=review_work_item_id_for_attempt("wi-child", 2),
            phase=Phase.APPROVED,
            batch_index=2,
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "review",
                "review_execution_work_item": True,
                "review_target_work_item_id": "wi-child",
                "review_attempt": 2,
                "review_work_item_outcome": "approved",
                "history_sentinel": "review-v2-must-not-change",
            },
        )
        await self.store.save_delegation_work_item(terminal_report)
        await self.store.save_delegation_work_item(terminal_review)

        worker_task = _build_worker_task()
        new_report = await self.executor._ensure_report_work_item_for_work_item(
            "wi-child",
            worker_task=worker_task,
        )
        self.assertIsNotNone(new_report)
        self.assertEqual(
            new_report.work_item_id,
            report_work_item_id_for_attempt("wi-child", 3),
        )
        # Review and report auxiliaries must never be active in parallel.
        self.assertIsNone(
            await self.executor._ensure_review_work_item_for_work_item(
                "wi-child",
                worker_task=worker_task,
                completion_report="new completion",
                metadata_updates={
                    "review_owner_role_id": "cto",
                    "review_owner_seat_id": "seat::team::cto::cto",
                },
                source_report_item=terminal_report,
            )
        )
        await self.store.update_delegation_work_item(
            new_report.work_item_id,
            phase=Phase.CANCELLED,
        )
        new_review = await self.executor._ensure_review_work_item_for_work_item(
            "wi-child",
            worker_task=worker_task,
            completion_report="new completion",
            metadata_updates={
                "review_owner_role_id": "cto",
                "review_owner_seat_id": "seat::team::cto::cto",
            },
            source_report_item=terminal_report,
        )

        self.assertIsNotNone(new_review)
        self.assertEqual(
            new_review.work_item_id,
            review_work_item_id_for_attempt("wi-child", 3),
        )
        persisted_report_v2 = await self.store.get_delegation_work_item(
            report_work_item_id_for_attempt("wi-child", 2)
        )
        persisted_review_v2 = await self.store.get_delegation_work_item(
            review_work_item_id_for_attempt("wi-child", 2)
        )
        self.assertEqual(persisted_report_v2.phase, Phase.APPROVED)
        self.assertEqual(
            persisted_report_v2.metadata.get("history_sentinel"),
            "report-v2-must-not-change",
        )
        self.assertEqual(persisted_review_v2.phase, Phase.APPROVED)
        self.assertEqual(
            persisted_review_v2.metadata.get("history_sentinel"),
            "review-v2-must-not-change",
        )

    async def test_reconcile_without_runtime_task_creates_report(self) -> None:
        await self._save_awaiting_parent()
        self.assertEqual(await self.store.get_tasks(), [])

        await self._run_reconcile()

        report = await self.store.get_delegation_work_item(
            report_work_item_id_for_attempt("wi-child", 1)
        )
        self.assertIsNotNone(report)
        self.assertEqual(report.phase, Phase.READY)
        self.assertEqual(report.metadata.get("report_target_work_item_id"), "wi-child")
        self.assertEqual(report.role_id, "engineer")
        self.assertEqual(report.seat_id, "seat::team::cto::engineer")

    async def test_repeated_reconcile_keeps_at_most_one_active_auxiliary(self) -> None:
        await self._save_awaiting_parent()

        for _ in range(5):
            await self._run_reconcile()

        auxiliaries = await self._auxiliary_cards()
        active = [item for item in auxiliaries if item.phase not in DONE_PHASES]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].kind, "report")
        self.assertEqual(active[0].work_item_id, report_work_item_id_for_attempt("wi-child", 1))
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )

    async def test_reconcile_remains_idempotent_after_database_reopen(self) -> None:
        await self._save_awaiting_parent()
        await self._run_reconcile()
        await self.store.close()

        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.executor = _build_executor(self.store, _make_org_engine(self.root))
        for _ in range(3):
            await self._run_reconcile()

        auxiliaries = await self._auxiliary_cards()
        active = [item for item in auxiliaries if item.phase not in DONE_PHASES]
        self.assertEqual(len(active), 1)
        self.assertEqual(
            active[0].work_item_id,
            report_work_item_id_for_attempt("wi-child", 1),
        )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 2)
            )
        )


class ReportCardRunnableFilterTests(unittest.TestCase):
    """Pin the dispatcher-runnability filters for report cards.

    Two parallel filters in company_mode.py independently decide whether
    a hidden card is runnable: ``_work_item_is_runnable`` (the engine
    enqueue gate) and the materialization filter inside
    ``_materialize_work_item_tasks``. Both must let report cards
    through, or the worker session is never re-engaged for the handoff
    turn and the parent stays at AWAITING_MANAGER_REVIEW forever — the
    new22/app-4 stuck-at-review pathology.
    """

    def _make_parent_and_report(self) -> tuple[DelegationWorkItem, DelegationWorkItem]:
        parent = DelegationWorkItem(
            work_item_id="wi-parent",
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
            projection_id="wi-parent",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            metadata={
                "runtime_model": "multi_team_org",
                "team_id": "team::cto",
            },
        )
        report = DelegationWorkItem(
            work_item_id="report::wi-parent::v1",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            parent_work_item_id="wi-parent",
            kind="report",
            projection_id="report::wi-parent::v1",
            phase=Phase.READY,
            metadata={
                "runtime_model": "multi_team_org",
                "report_execution_work_item": True,
                "hidden_from_company_kanban": True,
                "report_target_work_item_id": "wi-parent",
                "team_id": "team::cto",
            },
        )
        return parent, report

    def test_runnable_filter_passes_report_card(self) -> None:
        parent, report = self._make_parent_and_report()
        wi_map = {parent.work_item_id: parent, report.work_item_id: report}
        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(report, wi_map),
            "report card must be considered runnable so the engine enqueues "
            "it; the new22/app-4 pathology was that the hidden+not-review "
            "filter excluded it and the parent stayed at AWAITING_MANAGER_REVIEW.",
        )

    def test_runnable_filter_skips_report_when_parent_left_review(self) -> None:
        parent, report = self._make_parent_and_report()
        # Parent has somehow advanced past review (e.g. CANCELLED). The
        # report card is now obsolete and must NOT be claimed.
        parent.phase = Phase.CANCELLED
        wi_map = {parent.work_item_id: parent, report.work_item_id: report}
        self.assertFalse(
            CompanyWorkItemExecutor._work_item_is_runnable(report, wi_map),
            "report card must not run when its parent is no longer in review",
        )


class ReportFailureStormBrakeTests(unittest.IsolatedAsyncioTestCase):
    """FAILED report cards must not mint Report #N forever on reconcile."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.executor = _build_executor(self.store, _make_org_engine(self.root))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def _failed_report(self, attempt: int) -> DelegationWorkItem:
        report_id = report_work_item_id_for_attempt("wi-child", attempt)
        return DelegationWorkItem(
            work_item_id=report_id,
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="engineer",
            seat_id="seat::team::cto::engineer",
            parent_work_item_id="wi-child",
            kind="report",
            projection_id=report_id,
            phase=Phase.FAILED,
            batch_index=attempt,
            metadata={
                "runtime_model": "multi_team_org",
                "report_execution_work_item": True,
                "report_attempt": attempt,
                "report_target_work_item_id": "wi-child",
                "hidden_from_company_kanban": True,
            },
        )

    async def test_reconcile_stops_after_consecutive_report_failures(self) -> None:
        parent = _build_child_work_item()
        parent.phase = Phase.AWAITING_MANAGER_REVIEW
        parent.metadata = {
            **dict(parent.metadata or {}),
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "max_consecutive_report_failures": 3,
        }
        await self.store.save_delegation_work_item(parent)
        for attempt in (1, 2, 3):
            await self.store.save_delegation_work_item(self._failed_report(attempt))

        before = [
            item.work_item_id
            for item in await self.store.list_delegation_work_items("run-1")
            if item.kind == "report"
        ]
        await self.executor._reconcile_missing_review_chain(
            await self.store.list_delegation_work_items("run-1")
        )
        after = [
            item.work_item_id
            for item in await self.store.list_delegation_work_items("run-1")
            if item.kind == "report"
        ]
        self.assertEqual(before, after)
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 4)
            )
        )
        refreshed = await self.store.get_delegation_work_item("wi-child")
        self.assertEqual(refreshed.phase, Phase.FAILED)
        self.assertIn("report_chain_failure", str(refreshed.blocked_reason or ""))
        self.assertFalse(
            str((refreshed.metadata or {}).get("report_chain_hold", "") or "").strip()
        )

    async def test_terminalize_write_failure_quarantines_via_hold(self) -> None:
        parent = _build_child_work_item()
        parent.phase = Phase.AWAITING_MANAGER_REVIEW
        parent.metadata = {
            **dict(parent.metadata or {}),
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "max_consecutive_report_failures": 3,
        }
        await self.store.save_delegation_work_item(parent)
        for attempt in (1, 2, 3):
            await self.store.save_delegation_work_item(self._failed_report(attempt))

        with patch(
            "opc.layer2_organization.company_mode.transition_work_item",
            side_effect=RuntimeError("db write lost"),
        ):
            await self.executor._reconcile_missing_review_chain(
                await self.store.list_delegation_work_items("run-1")
            )
        self.assertIsNone(
            await self.store.get_delegation_work_item(
                report_work_item_id_for_attempt("wi-child", 4)
            )
        )
        refreshed = await self.store.get_delegation_work_item("wi-child")
        self.assertEqual(refreshed.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(
            refreshed.metadata.get("report_chain_hold"),
            "consecutive_report_failures",
        )

    async def test_reconcile_allows_retry_below_failure_limit(self) -> None:
        parent = _build_child_work_item()
        parent.phase = Phase.AWAITING_MANAGER_REVIEW
        parent.metadata = {
            **dict(parent.metadata or {}),
            "review_owner_role_id": "cto",
            "review_owner_seat_id": "seat::team::cto::cto",
            "max_consecutive_report_failures": 3,
        }
        await self.store.save_delegation_work_item(parent)
        await self.store.save_delegation_work_item(self._failed_report(1))
        await self.store.save_delegation_work_item(self._failed_report(2))

        await self.executor._reconcile_missing_review_chain(
            await self.store.list_delegation_work_items("run-1")
        )
        next_report = await self.store.get_delegation_work_item(
            report_work_item_id_for_attempt("wi-child", 3)
        )
        self.assertIsNotNone(next_report)
        self.assertEqual(next_report.phase, Phase.READY)
        refreshed = await self.store.get_delegation_work_item("wi-child")
        self.assertFalse(
            str((refreshed.metadata or {}).get("report_chain_hold", "") or "").strip()
        )


if __name__ == "__main__":
    unittest.main()
