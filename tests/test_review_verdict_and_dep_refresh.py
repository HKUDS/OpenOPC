"""Regression tests for Fix 1 (empty-verdict rework loop) and Fix 3
(dep-frontier refresh on all terminal child transitions).

Both bugs were observed live in project ``new16`` session ``app12``
(session_id ``875bdbde-7b97-4239-8e32-f2e52c96b289``), 2026-04-20:

Fix 1 — reviewer produced
``{"label":"reject","summary":"reject","blocking_issues":[],"followups":[]}``
for 5–6 consecutive rounds; each rework turn had no actionable guidance
and reproduced the same output, burning the full ``max_review_reworks``
budget before forced escalation.

Fix 3 — cto parent ``cdb248d8`` sat in WAITING_FOR_CHILDREN for 13+
minutes with the claim held by an idle session. Cause: refresh only
fired on the APPROVED-verdict branch of ``_finalize_review_work_item``
(line 5480). When children escalated to AWAITING_HUMAN or got
CANCELLED/FAILED, the parent was never re-evaluated.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401  (register hooks)
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_transition import refresh_dependents_for_run


# ── Fix 1 / Fix 4 verdict-shape tests removed: runtime no longer makes
# shape-based decisions about verdicts. The reviewer agent's verdict is
# applied mechanically. See tests/test_verdict_parse_retry.py for the
# only remaining structural fallback (verdict cannot be parsed at all).


# ── Fix 3: refresh_dependents_for_run + hook wiring ──────────────────────


def _make_work_item(
    *,
    work_item_id: str,
    run_id: str,
    phase: Phase,
    role_id: str = "w",
    dependency_ids: list[str] | None = None,
    claimed_by: str = "",
    team_instance_id: str = "ti",
    team_id: str = "team::x",
    parent_work_item_id: str | None = None,
    metadata: dict | None = None,
) -> DelegationWorkItem:
    metadata = dict(metadata or {})
    if dependency_ids:
        metadata["dependency_work_item_ids"] = list(dependency_ids)
    return DelegationWorkItem(
        work_item_id=work_item_id,
        run_id=run_id,
        cell_id="c",
        team_instance_id=team_instance_id,
        team_id=team_id,
        role_id=role_id,
        seat_id=f"seat::{role_id}",
        parent_work_item_id=parent_work_item_id,
        manager_role_id="m",
        manager_seat_id="ms",
        title=f"item-{work_item_id}",
        phase=phase,
        claimed_by_role_runtime_session_id=claimed_by,
        claimed_by_seat_id=f"seat::{role_id}" if claimed_by else "",
        metadata=metadata,
    )


class RefreshDependentsForRunTests(unittest.IsolatedAsyncioTestCase):
    """Integration tests against a real OPCStore. Each test constructs a
    parent + children, transitions children, and asserts the parent's
    phase + claim after the refresh pass."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def _save(self, *items: DelegationWorkItem) -> None:
        for it in items:
            await self.store.save_delegation_work_item(it)

    def _executor(self) -> CompanyWorkItemExecutor:
        async def execute_task(task: Task) -> TaskResult:
            return TaskResult(status=task.status, content="", artifacts={})

        return CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(on_kanban_changed=None, on_work_items_created=None),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=self.store.save_task,
            store=self.store,
        )

    async def test_waiting_dependency_work_item_releases_when_dependencies_already_approved(self) -> None:
        dep_a = _make_work_item(work_item_id="dep-a", run_id="run-follow", phase=Phase.APPROVED)
        dep_b = _make_work_item(work_item_id="dep-b", run_id="run-follow", phase=Phase.APPROVED)
        follow = _make_work_item(
            work_item_id="follow",
            run_id="run-follow",
            phase=Phase.WAITING_DEPENDENCIES,
            role_id="report_producer",
            dependency_ids=["dep-a", "dep-b"],
        )
        await self._save(dep_a, dep_b, follow)
        task = Task(
            id="follow-task",
            title="Follow-up",
            project_id="proj1",
            assigned_to="report_producer",
            status=TaskStatus.BLOCKED,
            metadata={"work_item_projection_id": "follow", "work_item_turn_type": "execute"},
        )
        set_linked_work_item_id(task, "follow")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task("follow", task.id)

        executor = self._executor()
        work_items = await executor._refresh_ready_work_items(
            await self.store.list_delegation_work_items("run-follow"),
            tasks=[task],
        )
        await executor._sync_task_projection_from_work_items([task], work_items)

        refreshed = await self.store.get_delegation_work_item("follow")
        refreshed_task = await self.store.get_task(task.id)
        self.assertEqual(refreshed.phase, Phase.READY)
        self.assertEqual(refreshed_task.status, TaskStatus.PENDING)

    async def test_waiting_dependency_work_item_stays_waiting_until_hard_dependencies_approve(self) -> None:
        dep_a = _make_work_item(work_item_id="dep-a", run_id="run-wait", phase=Phase.APPROVED)
        dep_b = _make_work_item(work_item_id="dep-b", run_id="run-wait", phase=Phase.RUNNING)
        follow = _make_work_item(
            work_item_id="follow",
            run_id="run-wait",
            phase=Phase.WAITING_DEPENDENCIES,
            role_id="report_producer",
            dependency_ids=["dep-a", "dep-b"],
        )
        await self._save(dep_a, dep_b, follow)

        executor = self._executor()
        await executor._refresh_ready_work_items(
            await self.store.list_delegation_work_items("run-wait"),
            tasks=[],
        )

        refreshed = await self.store.get_delegation_work_item("follow")
        self.assertEqual(refreshed.phase, Phase.WAITING_DEPENDENCIES)
        self.assertEqual(refreshed.metadata["waiting_on_work_item_ids"], ["dep-b"])

    async def test_materialized_follow_up_refreshes_already_approved_dependencies(self) -> None:
        parent = _make_work_item(
            work_item_id="parent",
            run_id="run-materialize",
            phase=Phase.RUNNING,
            role_id="chief_analyst",
        )
        dep_a = _make_work_item(work_item_id="dep-a", run_id="run-materialize", phase=Phase.APPROVED)
        dep_b = _make_work_item(work_item_id="dep-b", run_id="run-materialize", phase=Phase.APPROVED)
        await self._save(parent, dep_a, dep_b)

        manager_task = Task(
            id="manager-task",
            title="Chief Analyst Intake",
            project_id="proj1",
            assigned_to="chief_analyst",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_runtime": True,
                "work_item_runtime_version": 1,
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-materialize",
                "delegation_seat_id": "seat::chief_analyst",
                "runtime_topology": {
                    "seats": [
                        {
                            "role_id": "report_producer",
                            "seat_id": "seat::report_producer",
                            "team_id": "team::report",
                            "team_instance_id": "team-instance::report",
                            "seat_state_id": "seat-state::report",
                        }
                    ]
                },
            },
        )
        set_linked_work_item_id(manager_task, "parent")
        await self.store.save_task(manager_task)
        await self.store.link_work_item_runtime_task("parent", manager_task.id)

        executor = self._executor()
        executor._active_tasks = [manager_task]
        follow_up_result = TaskResult(
            status=TaskStatus.DONE,
            content="Create a PPT deck.",
            artifacts={
                "follow_up_actions": [
                    {
                        "action": "delegate_followup",
                        "target_role_id": "report_producer",
                        "title": "Generate PPT deck",
                        "summary": "Create a PPT with image2 visuals.",
                        "depends_on_work_item_ids": ["dep-a", "dep-b"],
                    }
                ]
            },
        )
        created = await executor._materialize_follow_up_work_items(
            manager_task,
            follow_up_result,
        )

        self.assertEqual(len(created), 1)
        follow = await self.store.get_delegation_work_item(created[0])
        parent_after = await self.store.get_delegation_work_item("parent")
        self.assertEqual(follow.phase, Phase.READY)
        self.assertEqual(parent_after.phase, Phase.WAITING_FOR_CHILDREN)

        # Re-emitting the same dedupe key reuses board state; it is not a
        # current-turn creation signal for the dispatch guard.
        reused = await executor._materialize_follow_up_work_items(
            manager_task,
            follow_up_result,
        )
        self.assertEqual(reused, [])
        follow_ups = [
            item
            for item in await self.store.list_delegation_work_items("run-materialize")
            if (item.metadata or {}).get("follow_up_dedupe_key")
        ]
        self.assertEqual(len(follow_ups), 1)

    async def test_parent_wakes_when_last_child_approved(self) -> None:
        """The canonical app12 fix: parent in WAITING_FOR_CHILDREN with a
        stale claim unblocks to RUNNING and releases the claim when all
        children reach APPROVED."""
        parent = _make_work_item(
            work_item_id="parent",
            run_id="run-a",
            phase=Phase.RUNNING,  # will move to WAITING_FOR_CHILDREN via refresh
            role_id="cto",
            dependency_ids=["child-1", "child-2"],
            claimed_by="role-runtime::run-a::seat::team::ceo::cto",
        )
        child1 = _make_work_item(
            work_item_id="child-1",
            run_id="run-a",
            phase=Phase.APPROVED,
            parent_work_item_id="parent",
        )
        child2 = _make_work_item(
            work_item_id="child-2",
            run_id="run-a",
            phase=Phase.RUNNING,  # still in progress
            parent_work_item_id="parent",
        )
        await self._save(parent, child1, child2)

        # First pass: one child not approved → parent transitions
        # RUNNING → WAITING_FOR_CHILDREN.
        await refresh_dependents_for_run(self.store, run_id="run-a")
        after = await self.store.get_delegation_work_item("parent")
        self.assertEqual(after.phase, Phase.WAITING_FOR_CHILDREN)
        self.assertEqual(
            after.claimed_by_role_runtime_session_id,
            "role-runtime::run-a::seat::team::ceo::cto",
        )

        # Second child completes → hook fires refresh → parent unblocks.
        await self.store.update_delegation_work_item(
            "child-2", phase=Phase.APPROVED
        )
        after = await self.store.get_delegation_work_item("parent")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.claimed_by_seat_id, "")

    async def test_final_delivery_parent_resumes_as_delivery_after_followup_child_approved(self) -> None:
        """A final delivery card reopened by owner follow-up parks while its
        new child runs, then resumes as a delivery/synthesis turn when that
        child is approved."""
        parent = _make_work_item(
            work_item_id="delivery-parent",
            run_id="run-delivery-followup",
            phase=Phase.WAITING_FOR_CHILDREN,
            role_id="ceo",
            dependency_ids=["old-research", "new-ppt"],
            claimed_by="role-runtime::run-delivery-followup::ceo",
            metadata={
                "work_kind": "delivery",
                "delegation_turn_kind": "delivery",
                "work_item_turn_type": "deliver",
                "current_turn_mode": "dispatch_required",
                "feedback_scope": "final",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
            },
        )
        old_research = _make_work_item(
            work_item_id="old-research",
            run_id="run-delivery-followup",
            phase=Phase.APPROVED,
            parent_work_item_id="delivery-parent",
        )
        new_ppt = _make_work_item(
            work_item_id="new-ppt",
            run_id="run-delivery-followup",
            phase=Phase.RUNNING,
            parent_work_item_id="delivery-parent",
        )
        await self._save(parent, old_research, new_ppt)

        await self.store.update_delegation_work_item("new-ppt", phase=Phase.APPROVED)

        after = await self.store.get_delegation_work_item("delivery-parent")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.claimed_by_seat_id, "")
        self.assertEqual(after.metadata.get("work_kind"), "delivery")
        self.assertEqual(after.metadata.get("delegation_turn_kind"), "delivery")
        self.assertEqual(after.metadata.get("work_item_turn_type"), "deliver")
        self.assertEqual(after.metadata.get("current_turn_mode"), "deliver_required")
        self.assertEqual(after.metadata.get("waiting_on_work_item_ids"), [])

    async def test_child_cancelled_triggers_parent_refresh(self) -> None:
        """Fix 3 core + failure-triage release: a non-APPROVED terminal
        (CANCELLED) fires the refresh hook, and because every dependency is
        now settled the parent is RELEASED for a triage turn instead of
        waiting on the dead child forever (the project-4444 deadlock)."""
        parent = _make_work_item(
            work_item_id="parent-c",
            run_id="run-b",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-a", "child-b"],
            claimed_by="claim-x",
        )
        child_a = _make_work_item(
            work_item_id="child-a",
            run_id="run-b",
            phase=Phase.APPROVED,
        )
        child_b = _make_work_item(
            work_item_id="child-b",
            run_id="run-b",
            phase=Phase.RUNNING,
        )
        await self._save(parent, child_a, child_b)

        await self.store.update_delegation_work_item(
            "child-b", phase=Phase.CANCELLED
        )
        after = await self.store.get_delegation_work_item("parent-c")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("cancelled", [])), ["child-b"])
        self.assertEqual(list(settlement.get("failed", [])), [])
        self.assertEqual(list(after.metadata.get("waiting_on_work_item_ids", [])), [])

    async def test_failed_child_releases_parent_for_triage_synthesis(self) -> None:
        """Project-4444 regression: a FAILED child must release the
        delegating parent into a synthesis/triage turn with the failure
        stamped, not pin it in WAITING_FOR_CHILDREN forever."""
        parent = _make_work_item(
            work_item_id="parent-f",
            run_id="run-f",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-ok", "child-bad"],
            claimed_by="claim-y",
            metadata={"delegated_children_pending": True},
        )
        child_ok = _make_work_item(
            work_item_id="child-ok", run_id="run-f", phase=Phase.APPROVED
        )
        child_bad = _make_work_item(
            work_item_id="child-bad", run_id="run-f", phase=Phase.RUNNING
        )
        await self._save(parent, child_ok, child_bad)

        await self.store.update_delegation_work_item("child-bad", phase=Phase.FAILED)

        after = await self.store.get_delegation_work_item("parent-f")
        self.assertEqual(after.phase, Phase.READY)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.metadata.get("work_kind"), "synthesize")
        self.assertEqual(after.metadata.get("current_turn_mode"), "synthesize_required")
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["child-bad"])
        self.assertIn("failed/cancelled", str(after.summary or ""))

    async def test_doomed_chain_marks_stuck_and_releases_parent(self) -> None:
        """Transitive settlement: sibling B hard-depends on FAILED A, so B
        can never run. The parent (deps [A, B]) must still be released,
        with B recorded as stuck rather than waited on forever."""
        parent = _make_work_item(
            work_item_id="parent-chain",
            run_id="run-chain",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["chain-a", "chain-b"],
            metadata={"runtime_model": "multi_team_org"},
        )
        chain_a = _make_work_item(
            work_item_id="chain-a", run_id="run-chain", phase=Phase.RUNNING
        )
        chain_b = _make_work_item(
            work_item_id="chain-b",
            run_id="run-chain",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["chain-a"],
        )
        await self._save(parent, chain_a, chain_b)

        await self.store.update_delegation_work_item("chain-a", phase=Phase.FAILED)

        after_parent = await self.store.get_delegation_work_item("parent-chain")
        after_b = await self.store.get_delegation_work_item("chain-b")
        self.assertEqual(after_parent.phase, Phase.RUNNING)
        settlement = dict(after_parent.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["chain-a"])
        self.assertEqual(list(settlement.get("stuck", [])), ["chain-b"])
        # The stuck sibling itself is NOT released — the triage turn decides.
        self.assertEqual(after_b.phase, Phase.WAITING_DEPENDENCIES)
        # The released parent must actually be claimable despite the
        # non-terminal stuck dep, or the release is cosmetic.
        run_items = await self.store.list_delegation_work_items("run-chain")
        by_id = {item.work_item_id: item for item in run_items}
        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(by_id["parent-chain"], by_id)
        )

    async def test_transitive_only_failure_still_releases_parent(self) -> None:
        """`parent → B → FAILED A` where A is NOT a direct dep of the
        parent: no direct dep ever turns FAILED, but B is doomed, so the
        parent must still be released instead of waiting forever."""
        parent = _make_work_item(
            work_item_id="parent-t",
            run_id="run-t",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["t-b"],
            metadata={"runtime_model": "multi_team_org"},
        )
        t_b = _make_work_item(
            work_item_id="t-b",
            run_id="run-t",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["t-a"],
        )
        t_a = _make_work_item(work_item_id="t-a", run_id="run-t", phase=Phase.RUNNING)
        await self._save(t_b, parent, t_a)

        await self.store.update_delegation_work_item("t-a", phase=Phase.FAILED)

        after_parent = await self.store.get_delegation_work_item("parent-t")
        self.assertEqual(after_parent.phase, Phase.RUNNING)
        settlement = dict(after_parent.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), [])
        self.assertEqual(list(settlement.get("stuck", [])), ["t-b"])
        run_items = await self.store.list_delegation_work_items("run-t")
        by_id = {item.work_item_id: item for item in run_items}
        self.assertTrue(
            CompanyWorkItemExecutor._work_item_is_runnable(by_id["parent-t"], by_id)
        )

    async def test_delivery_rollup_released_over_failed_dependency(self) -> None:
        """A delivery card WAITING_DEPENDENCIES on a FAILED child is released
        READY so the failure report reaches the user instead of wedging."""
        delivery = _make_work_item(
            work_item_id="delivery-f",
            run_id="run-dlv",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["dlv-child"],
            metadata={"work_kind": "delivery"},
        )
        child = _make_work_item(
            work_item_id="dlv-child", run_id="run-dlv", phase=Phase.RUNNING
        )
        await self._save(delivery, child)

        await self.store.update_delegation_work_item("dlv-child", phase=Phase.FAILED)

        after = await self.store.get_delegation_work_item("delivery-f")
        self.assertEqual(after.phase, Phase.READY)
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["dlv-child"])

    async def test_settlement_cascade_cancels_unrebuilt_stuck_children(self) -> None:
        """Once the settled triage card is APPROVED, leftover doomed stuck
        children are cancelled so the run can reach a fully-terminal state."""
        parent = _make_work_item(
            work_item_id="parent-casc",
            run_id="run-casc",
            phase=Phase.RUNNING,
            dependency_ids=["casc-a", "casc-b"],
            metadata={
                "dependency_settlement": {
                    "failed": ["casc-a"],
                    "cancelled": [],
                    "stuck": ["casc-b"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        casc_a = _make_work_item(
            work_item_id="casc-a", run_id="run-casc", phase=Phase.FAILED
        )
        casc_b = _make_work_item(
            work_item_id="casc-b",
            run_id="run-casc",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["casc-a"],
        )
        await self._save(casc_b, casc_a, parent)

        await self.store.update_delegation_work_item("parent-casc", phase=Phase.APPROVED)

        after_b = await self.store.get_delegation_work_item("casc-b")
        after_parent = await self.store.get_delegation_work_item("parent-casc")
        self.assertEqual(after_b.phase, Phase.CANCELLED)
        self.assertEqual(
            after_b.metadata.get("last_transition_reason"),
            "upstream_dependency_failed_parent_settled",
        )
        settlement = dict(after_parent.metadata.get("dependency_settlement", {}) or {})
        self.assertTrue(settlement.get("cascaded_at"))

    async def test_settlement_cascade_cancels_deep_doomed_chain(self) -> None:
        """Closure: C hard-depends on stuck B; C is in no released card's
        direct dependency list, but cancelling B dooms it — the cascade
        must cancel the whole chain or the run never terminalizes."""
        parent = _make_work_item(
            work_item_id="parent-deep",
            run_id="run-deep",
            phase=Phase.RUNNING,
            dependency_ids=["deep-a", "deep-b"],
            metadata={
                "dependency_settlement": {
                    "failed": ["deep-a"],
                    "cancelled": [],
                    "stuck": ["deep-b"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        deep_a = _make_work_item(
            work_item_id="deep-a", run_id="run-deep", phase=Phase.FAILED
        )
        deep_b = _make_work_item(
            work_item_id="deep-b",
            run_id="run-deep",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["deep-a"],
        )
        deep_c = _make_work_item(
            work_item_id="deep-c",
            run_id="run-deep",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["deep-b"],
        )
        await self._save(deep_c, deep_b, deep_a, parent)

        await self.store.update_delegation_work_item("parent-deep", phase=Phase.APPROVED)

        after_b = await self.store.get_delegation_work_item("deep-b")
        after_c = await self.store.get_delegation_work_item("deep-c")
        self.assertEqual(after_b.phase, Phase.CANCELLED)
        self.assertEqual(after_c.phase, Phase.CANCELLED)

    async def test_settlement_cascade_spares_rewired_children(self) -> None:
        """Stuck children the manager rewired onto a live replacement stay
        alive: rewiring drops them out of the doomed set."""
        parent = _make_work_item(
            work_item_id="parent-rw",
            run_id="run-rw",
            phase=Phase.RUNNING,
            dependency_ids=["rw-a", "rw-b"],
            metadata={
                "dependency_settlement": {
                    "failed": ["rw-a"],
                    "cancelled": [],
                    "stuck": ["rw-b"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        # Failed child was manager-deleted with a replacement, so rw-b's
        # dependency normalizes onto the live replacement card.
        rw_a = _make_work_item(
            work_item_id="rw-a",
            run_id="run-rw",
            phase=Phase.CANCELLED,
            metadata={
                "deleted_by_manager_tool": True,
                "replacement_dependency_work_item_ids": ["rw-a2"],
            },
        )
        rw_a2 = _make_work_item(
            work_item_id="rw-a2", run_id="run-rw", phase=Phase.RUNNING
        )
        rw_b = _make_work_item(
            work_item_id="rw-b",
            run_id="run-rw",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["rw-a"],
        )
        await self._save(rw_a2, rw_b, rw_a, parent)

        await self.store.update_delegation_work_item("parent-rw", phase=Phase.APPROVED)

        after_b = await self.store.get_delegation_work_item("rw-b")
        self.assertEqual(after_b.phase, Phase.WAITING_DEPENDENCIES)

    async def test_park_does_not_wait_on_settled_failed_or_doomed_deps(self) -> None:
        """A triage turn that accepts partial results must be able to
        finish: settled deps (terminal or doomed) are not pending, so the
        card completes instead of re-parking forever on the FAILED child
        it just triaged."""
        parent = _make_work_item(
            work_item_id="park-parent",
            run_id="run-park",
            phase=Phase.RUNNING,
            dependency_ids=["park-ok", "park-bad", "park-stuck"],
            metadata={
                "dependency_settlement": {
                    "failed": ["park-bad"],
                    "cancelled": [],
                    "stuck": ["park-stuck"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        park_ok = _make_work_item(
            work_item_id="park-ok", run_id="run-park", phase=Phase.APPROVED
        )
        park_bad = _make_work_item(
            work_item_id="park-bad", run_id="run-park", phase=Phase.FAILED
        )
        park_stuck = _make_work_item(
            work_item_id="park-stuck",
            run_id="run-park",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["park-bad"],
        )
        await self._save(park_ok, park_bad, park_stuck, parent)

        task = Task(
            id="park-task",
            title="Triage turn",
            project_id="proj1",
            assigned_to="m",
            status=TaskStatus.RUNNING,
            metadata={"work_item_projection_id": "park-parent"},
        )
        set_linked_work_item_id(task, "park-parent")
        await self.store.save_task(task)

        executor = self._executor()
        parked = await executor._park_for_delegated_children(task)

        self.assertFalse(parked)
        after = await self.store.get_delegation_work_item("park-parent")
        self.assertEqual(after.phase, Phase.RUNNING)
        # Settlement stamp survives (needed by the cascade at APPROVED).
        self.assertTrue(dict(after.metadata.get("dependency_settlement", {}) or {}))

    async def test_park_race_releases_triage_when_failure_beat_the_park(self) -> None:
        """Race: children failed while the manager turn was still running,
        so the failure hook fired before the card parked (and may have
        regressed it to WAITING_FOR_CHILDREN without a stamp). Park must
        re-arm the triage release instead of silently returning False."""
        parent = _make_work_item(
            work_item_id="race-parent",
            run_id="run-race",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["race-bad"],
        )
        race_bad = _make_work_item(
            work_item_id="race-bad", run_id="run-race", phase=Phase.FAILED
        )
        await self._save(race_bad, parent)

        task = Task(
            id="race-task",
            title="Dispatch turn",
            project_id="proj1",
            assigned_to="m",
            status=TaskStatus.RUNNING,
            metadata={"work_item_projection_id": "race-parent"},
        )
        set_linked_work_item_id(task, "race-parent")
        await self.store.save_task(task)

        executor = self._executor()
        parked = await executor._park_for_delegated_children(task)

        self.assertTrue(parked)
        after = await self.store.get_delegation_work_item("race-parent")
        self.assertNotEqual(after.phase, Phase.WAITING_FOR_CHILDREN)
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["race-bad"])

    async def test_released_triage_card_is_not_doomed_for_upper_parents(self) -> None:
        """A released (stamped) triage card is alive: it must not count as
        doomed, or the upper parent settles early and its cascade could
        cancel a triage card that is about to run."""
        triage = _make_work_item(
            work_item_id="alive-triage",
            run_id="run-alive",
            phase=Phase.READY,
            dependency_ids=["alive-bad"],
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "synthesize",
                "dependency_settlement": {
                    "failed": ["alive-bad"],
                    "cancelled": [],
                    "stuck": [],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        alive_bad = _make_work_item(
            work_item_id="alive-bad", run_id="run-alive", phase=Phase.FAILED
        )
        upper = _make_work_item(
            work_item_id="alive-upper",
            run_id="run-alive",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["alive-triage"],
        )
        await self._save(alive_bad, triage, upper)

        from opc.layer2_organization.work_item_transition import (
            compute_doomed_work_item_ids,
        )

        run_items = await self.store.list_delegation_work_items("run-alive")
        by_id = {item.work_item_id: item for item in run_items}
        doomed = compute_doomed_work_item_ids(by_id)
        self.assertNotIn("alive-triage", doomed)

        await refresh_dependents_for_run(self.store, run_id="run-alive")
        after_upper = await self.store.get_delegation_work_item("alive-upper")
        # The upper parent keeps waiting for the live triage card — no
        # premature settlement over it.
        self.assertEqual(after_upper.phase, Phase.WAITING_FOR_CHILDREN)
        self.assertFalse(
            dict(after_upper.metadata.get("dependency_settlement", {}) or {})
        )

    async def test_settlement_cascade_retries_after_partial_failure(self) -> None:
        """Retry: B (stamped stuck) was cancelled by a previous cascade
        attempt but deeper C failed to cancel. The next pass must still
        traverse through terminal B and cancel C before stamping."""
        parent = _make_work_item(
            work_item_id="retry-parent",
            run_id="run-retry",
            phase=Phase.APPROVED,
            dependency_ids=["retry-a", "retry-b"],
            metadata={
                "dependency_settlement": {
                    "failed": ["retry-a"],
                    "cancelled": [],
                    "stuck": ["retry-b"],
                    "settled_at": "2026-07-13T00:00:00",
                },
            },
        )
        retry_a = _make_work_item(
            work_item_id="retry-a", run_id="run-retry", phase=Phase.FAILED
        )
        retry_b = _make_work_item(
            work_item_id="retry-b",
            run_id="run-retry",
            phase=Phase.CANCELLED,
            dependency_ids=["retry-a"],
        )
        retry_c = _make_work_item(
            work_item_id="retry-c",
            run_id="run-retry",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["retry-b"],
        )
        await self._save(retry_c, retry_b, retry_a, parent)

        await refresh_dependents_for_run(self.store, run_id="run-retry")

        after_c = await self.store.get_delegation_work_item("retry-c")
        after_parent = await self.store.get_delegation_work_item("retry-parent")
        self.assertEqual(after_c.phase, Phase.CANCELLED)
        settlement = dict(after_parent.metadata.get("dependency_settlement", {}) or {})
        self.assertTrue(settlement.get("cascaded_at"))

    async def test_info_dependency_does_not_block_settlement(self) -> None:
        """Info-class deps never gate claiming, so an in-flight info dep
        must not keep a failed board from settling either."""
        parent = _make_work_item(
            work_item_id="info-parent",
            run_id="run-info",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["info-bad", "info-fyi"],
            metadata={"dependency_classes": {"info-fyi": "info"}},
        )
        info_bad = _make_work_item(
            work_item_id="info-bad", run_id="run-info", phase=Phase.RUNNING
        )
        info_fyi = _make_work_item(
            work_item_id="info-fyi", run_id="run-info", phase=Phase.RUNNING
        )
        await self._save(info_fyi, info_bad, parent)

        await self.store.update_delegation_work_item("info-bad", phase=Phase.FAILED)

        after = await self.store.get_delegation_work_item("info-parent")
        self.assertNotEqual(after.phase, Phase.WAITING_FOR_CHILDREN)
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["info-bad"])
        self.assertNotIn("info-fyi", list(settlement.get("stuck", [])))

    async def test_late_created_rollup_card_gets_settlement_on_dispatcher_tick(self) -> None:
        """A delivery card created AFTER its dependency failed sees no
        failure hook; the dispatcher tick must run the frontier for it."""
        late_bad = _make_work_item(
            work_item_id="late-bad", run_id="run-late", phase=Phase.FAILED
        )
        await self._save(late_bad)
        delivery = _make_work_item(
            work_item_id="late-delivery",
            run_id="run-late",
            phase=Phase.WAITING_DEPENDENCIES,
            dependency_ids=["late-bad"],
            metadata={"work_kind": "delivery"},
        )
        await self._save(delivery)

        executor = self._executor()
        run_items = await self.store.list_delegation_work_items("run-late")
        await executor._refresh_ready_work_items(run_items, tasks=[])

        after = await self.store.get_delegation_work_item("late-delivery")
        self.assertEqual(after.phase, Phase.READY)
        settlement = dict(after.metadata.get("dependency_settlement", {}) or {})
        self.assertEqual(list(settlement.get("failed", [])), ["late-bad"])

    async def test_park_still_waits_on_live_children(self) -> None:
        """Control: genuinely in-flight children still park the manager."""
        parent = _make_work_item(
            work_item_id="live-parent",
            run_id="run-live",
            phase=Phase.RUNNING,
            dependency_ids=["live-ok", "live-running"],
        )
        live_ok = _make_work_item(
            work_item_id="live-ok", run_id="run-live", phase=Phase.APPROVED
        )
        live_running = _make_work_item(
            work_item_id="live-running", run_id="run-live", phase=Phase.RUNNING
        )
        await self._save(live_ok, live_running, parent)

        task = Task(
            id="live-task",
            title="Dispatch turn",
            project_id="proj1",
            assigned_to="m",
            status=TaskStatus.RUNNING,
            metadata={"work_item_projection_id": "live-parent"},
        )
        set_linked_work_item_id(task, "live-parent")
        await self.store.save_task(task)

        executor = self._executor()
        parked = await executor._park_for_delegated_children(task)

        self.assertTrue(parked)
        after = await self.store.get_delegation_work_item("live-parent")
        self.assertEqual(after.phase, Phase.WAITING_FOR_CHILDREN)
        self.assertEqual(
            list(after.metadata.get("waiting_on_work_item_ids", [])),
            ["live-running"],
        )

    async def test_manager_deleted_child_is_pruned_from_parent_dependencies(self) -> None:
        """Manager-deleted hidden children are graph edits, not hard blockers.

        A normal CANCELLED dependency still blocks (covered above). This case
        mirrors a recovery dispatch: an obsolete child was cancelled/hidden by
        the manager, a replacement child finished, and the parent should not
        wait forever on the stale id.
        """
        parent = _make_work_item(
            work_item_id="parent-prune",
            run_id="run-prune",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["approved-child", "deleted-child", "replacement-child"],
            claimed_by="stale-parent-claim",
        )
        approved_child = _make_work_item(
            work_item_id="approved-child",
            run_id="run-prune",
            phase=Phase.APPROVED,
            parent_work_item_id="parent-prune",
        )
        deleted_child = _make_work_item(
            work_item_id="deleted-child",
            run_id="run-prune",
            phase=Phase.CANCELLED,
            parent_work_item_id="parent-prune",
        )
        deleted_child.metadata.update(
            {
                "deleted_by_manager_tool": True,
                "hidden_from_company_kanban": True,
                "upstream_visibility": "hidden",
            }
        )
        replacement_child = _make_work_item(
            work_item_id="replacement-child",
            run_id="run-prune",
            phase=Phase.APPROVED,
            parent_work_item_id="parent-prune",
        )
        await self._save(parent, approved_child, deleted_child, replacement_child)

        changed = await refresh_dependents_for_run(self.store, run_id="run-prune")

        self.assertTrue(changed)
        after = await self.store.get_delegation_work_item("parent-prune")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(
            list(after.metadata.get("dependency_work_item_ids", [])),
            ["approved-child", "replacement-child"],
        )
        self.assertEqual(after.metadata.get("waiting_on_work_item_ids"), [])
        self.assertIn("deleted-child", after.metadata.get("pruned_dependency_work_item_ids", []))

    async def test_final_delivery_child_awaiting_human_triggers_refresh(self) -> None:
        """A genuine final-delivery human wait still participates in dependency refresh."""
        parent = _make_work_item(
            work_item_id="parent-h",
            run_id="run-c",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-h1", "child-h2"],
            claimed_by="claim-z",
        )
        child_h1 = _make_work_item(
            work_item_id="child-h1",
            run_id="run-c",
            phase=Phase.APPROVED,
        )
        child_h2 = _make_work_item(
            work_item_id="child-h2",
            run_id="run-c",
            phase=Phase.RUNNING,
        )
        await self._save(parent, child_h1, child_h2)

        await self.store.update_delegation_work_item(
            "child-h2", phase=Phase.AWAITING_HUMAN
        )
        # Parent stays waiting (AWAITING_HUMAN is not APPROVED). Once the
        # final delivery is accepted, the dependency hook unblocks it.
        await self.store.update_delegation_work_item(
            "child-h2", phase=Phase.APPROVED
        )
        after = await self.store.get_delegation_work_item("parent-h")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")

    async def test_refresh_is_reentrancy_safe(self) -> None:
        """The hook fires during a write; the write itself can be
        triggered by the hook's update (parent phase change). Verify we
        don't recurse forever — 3 levels deep should converge."""
        grandparent = _make_work_item(
            work_item_id="gp",
            run_id="run-d",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["parent-d"],
            claimed_by="gp-claim",
        )
        parent_d = _make_work_item(
            work_item_id="parent-d",
            run_id="run-d",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["leaf"],
            claimed_by="p-claim",
            parent_work_item_id="gp",
        )
        leaf = _make_work_item(
            work_item_id="leaf",
            run_id="run-d",
            phase=Phase.RUNNING,
            parent_work_item_id="parent-d",
        )
        await self._save(grandparent, parent_d, leaf)

        await self.store.update_delegation_work_item(
            "leaf", phase=Phase.APPROVED
        )
        # One approval should cascade: leaf approved → parent_d runs →
        # parent_d needs to hit APPROVED for grandparent to unblock.
        # parent_d will run but not be approved in this test, so
        # grandparent should still be WAITING_FOR_CHILDREN but its dep
        # waiting_on_work_item_ids should reflect the current state.
        p_after = await self.store.get_delegation_work_item("parent-d")
        gp_after = await self.store.get_delegation_work_item("gp")
        self.assertEqual(p_after.phase, Phase.RUNNING)
        self.assertEqual(p_after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(gp_after.phase, Phase.WAITING_FOR_CHILDREN)

    async def test_refresh_no_change_returns_false(self) -> None:
        """Calling refresh with no eligible work items is a no-op."""
        lonely = _make_work_item(
            work_item_id="solo",
            run_id="run-e",
            phase=Phase.RUNNING,
            # no dependency_ids → nothing to refresh
        )
        await self._save(lonely)
        changed = await refresh_dependents_for_run(
            self.store, run_id="run-e"
        )
        self.assertFalse(changed)

    # ── RC3 Step-1 fixes: READY_FOR_REWORK triggers refresh + broadened claim release ──

    async def test_child_ready_for_rework_bubbles_refresh(self) -> None:
        """Step-1 core: reviewer rejects child with rework → child flips to
        READY_FOR_REWORK. Before the fix, this transition was not in
        _DEPENDENT_REFRESH_TARGETS so the parent's waiting_on_work_item_ids
        stayed stale. Verify the hook now fires and parent's frontier
        reflects the rework child."""
        parent = _make_work_item(
            work_item_id="parent-r",
            run_id="run-r",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-r1", "child-r2"],
            claimed_by="stale-claim",
        )
        child_r1 = _make_work_item(
            work_item_id="child-r1",
            run_id="run-r",
            phase=Phase.APPROVED,
        )
        child_r2 = _make_work_item(
            work_item_id="child-r2",
            run_id="run-r",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        await self._save(parent, child_r1, child_r2)

        # Reviewer returns rework verdict → child flips to READY_FOR_REWORK.
        # Before Step 1 fix: parent frontier untouched, claim stale forever.
        # After fix: hook runs, waiting_on_work_item_ids is rewritten.
        await self.store.update_delegation_work_item(
            "child-r2", phase=Phase.READY_FOR_REWORK
        )
        after = await self.store.get_delegation_work_item("parent-r")
        self.assertEqual(after.phase, Phase.WAITING_FOR_CHILDREN)
        self.assertEqual(
            list(after.metadata.get("waiting_on_work_item_ids", [])),
            ["child-r1", "child-r2"],
        )

    async def test_parent_wakes_from_waiting_with_stale_claim_released(self) -> None:
        """Step-1 broadened claim release: parent in WAITING_FOR_CHILDREN
        exits toward ANY non-terminal phase → stale claim is cleared.
        Earlier, claim release only fired when target==RUNNING and
        all_approved; if the parent instead went to e.g. READY through
        dependency regression propagation, claim stayed held.
        This test drives the WAITING_FOR_CHILDREN → RUNNING path (the
        canonical case), but the new condition also covers other wakes.
        """
        parent = _make_work_item(
            work_item_id="parent-w",
            run_id="run-w",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-w"],
            claimed_by="idle-session-123",
        )
        child_w = _make_work_item(
            work_item_id="child-w",
            run_id="run-w",
            phase=Phase.RUNNING,
        )
        await self._save(parent, child_w)

        await self.store.update_delegation_work_item(
            "child-w", phase=Phase.APPROVED
        )
        after = await self.store.get_delegation_work_item("parent-w")
        self.assertEqual(after.phase, Phase.RUNNING)
        self.assertEqual(after.claimed_by_role_runtime_session_id, "")
        self.assertEqual(after.claimed_by_seat_id, "")

    async def test_parent_terminal_keeps_claim_as_audit(self) -> None:
        """Step-1 broadened claim release explicitly excludes DONE_PHASES:
        a WAITING_FOR_CHILDREN parent that transitions to CANCELLED/FAILED
        MUST retain the claim as a "last executor" audit record.
        Force this path by simulating a higher-level cancel that flips the
        parent directly to CANCELLED via the store.
        """
        parent = _make_work_item(
            work_item_id="parent-t",
            run_id="run-t",
            phase=Phase.WAITING_FOR_CHILDREN,
            dependency_ids=["child-t"],
            claimed_by="historical-session",
        )
        child_t = _make_work_item(
            work_item_id="child-t",
            run_id="run-t",
            phase=Phase.RUNNING,
        )
        await self._save(parent, child_t)

        # Directly cancel the parent (as if a higher-level cancel cascaded).
        await self.store.update_delegation_work_item(
            "parent-t", phase=Phase.CANCELLED
        )
        after = await self.store.get_delegation_work_item("parent-t")
        self.assertEqual(after.phase, Phase.CANCELLED)
        # Claim preserved — it is a historical audit record for terminal items.
        self.assertEqual(
            after.claimed_by_role_runtime_session_id, "historical-session"
        )


if __name__ == "__main__":
    unittest.main()
