"""work-item ownership checks for UI-critical WorkItem metadata.

Fields persisted to WorkItem:
* ``progress_log`` — via ``_append_progress`` (hot path; WorkItem-owned)
* ``work_item_role_name`` / ``employee_prompt_context`` /
  ``employee_delta_context`` — via ``_materialize_work_item_tasks``
  (set-once at materialization)

Not mirrored intentionally (scope deferral):
* ``working_memory`` — kanban doesn't read it
* ``employee_assignment`` — company work-item owned; task mode does not use it

Mirror is best-effort: failures are logged at DEBUG and do NOT propagate.
Readers use the structured runtime link; task-side copies are execution
context only.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from opc.core.models import DelegationWorkItem, Phase, Task, TaskResult, TaskStatus
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401  (register hooks)
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.metadata_ownership import copy_work_item_execution_metadata
from opc.layer2_organization.work_item_links import set_linked_work_item_id


def _executor_with_store(store) -> CompanyWorkItemExecutor:
    async def _noop(*_a, **_kw):
        return None

    return CompanyWorkItemExecutor(
        org_engine=MagicMock(),
        communication=None,
        approval_engine=None,
        memory=None,
        execute_task=_noop,
        save_task=_noop,
        store=store,
    )


class MirrorFieldsToWorkItemTests(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_mirror_fields_to_work_item`` — the Step 9 write helper."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.executor = _executor_with_store(self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def _seed_work_item(self, wid: str = "wi-1") -> DelegationWorkItem:
        wi = DelegationWorkItem(
            work_item_id=wid,
            run_id="run-1",
            cell_id="c",
            team_instance_id="ti",
            team_id="t",
            role_id="r",
            seat_id="s",
            title="t",
            phase=Phase.RUNNING,
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        return wi

    def _task_linked_to(self, wid: str, extra_meta: dict) -> Task:
        task = Task(
            id="t-1",
            title="t",
            status=TaskStatus.RUNNING,
            metadata=dict(extra_meta),
        )
        set_linked_work_item_id(task, wid)
        return task

    async def test_mirror_copies_specified_keys_to_work_item(self) -> None:
        await self._seed_work_item()
        task = self._task_linked_to(
            "wi-1",
            {
                "work_item_role_name": "Senior Engineer",
                "employee_prompt_context": "prompt-context",
                "progress_log": ["step 1", "step 2"],
                "unrelated": "ignored",
            },
        )
        await self.executor._mirror_fields_to_work_item(
            task,
            ["work_item_role_name", "employee_prompt_context", "progress_log"],
        )
        after = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(after.metadata.get("work_item_role_name"), "Senior Engineer")
        self.assertEqual(after.metadata.get("employee_prompt_context"), "prompt-context")
        self.assertEqual(after.metadata.get("progress_log"), ["step 1", "step 2"])
        self.assertNotIn("unrelated", after.metadata)  # only specified keys

    async def test_mirror_without_work_item_id_is_noop(self) -> None:
        """task-mode path: no structured WorkItem link returns silently."""
        task = Task(id="t-2", title="t", metadata={"progress_log": ["x"]})
        # Should not raise even though there's no wid:
        await self.executor._mirror_fields_to_work_item(task, ["progress_log"])

    async def test_mirror_failure_is_swallowed(self) -> None:
        """DB failure must not propagate — the view's fallback keeps readers
        functional."""
        await self._seed_work_item()
        task = self._task_linked_to("wi-1", {"progress_log": ["a"]})
        failing_store = MagicMock()
        failing_store.update_delegation_work_item = AsyncMock(side_effect=RuntimeError("boom"))
        ex = _executor_with_store(failing_store)
        # Must NOT raise:
        await ex._mirror_fields_to_work_item(task, ["progress_log"])

    async def test_mirror_skips_missing_keys(self) -> None:
        """Only keys that exist on task.metadata are mirrored — absent keys
        don't stomp existing values with None."""
        wi = await self._seed_work_item()
        wi.metadata["existing"] = "keep-me"
        await self.store.save_delegation_work_item(wi)
        task = self._task_linked_to("wi-1", {"progress_log": ["x"]})
        await self.executor._mirror_fields_to_work_item(
            task, ["progress_log", "missing_key"]
        )
        after = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(after.metadata.get("progress_log"), ["x"])
        self.assertEqual(after.metadata.get("existing"), "keep-me")
        self.assertNotIn("missing_key", after.metadata)

    async def test_append_progress_mirrors_progress_log(self) -> None:
        """End-to-end: calling _append_progress on a company-mode task
        updates WorkItem progress without writing a new Task mirror."""
        await self._seed_work_item()
        task = self._task_linked_to("wi-1", {})
        await self.executor._append_progress(task, "step A")
        await self.executor._append_progress(task, "step B")

        self.assertNotIn("progress_log", task.metadata)
        after = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(after.metadata.get("progress_log"), ["step A", "step B"])

    async def test_append_progress_does_not_mirror_working_memory(self) -> None:
        """working_memory is intentionally NOT mirrored — kanban doesn't
        read it; avoids unnecessary DB churn on every append."""
        await self._seed_work_item()
        task = self._task_linked_to("wi-1", {})
        await self.executor._append_progress(task, "msg")
        after = await self.store.get_delegation_work_item("wi-1")
        self.assertIn("progress_log", after.metadata)
        self.assertNotIn("working_memory", after.metadata)

    async def test_append_progress_in_task_mode_no_wid_still_works(self) -> None:
        """task-mode tasks (no work_item_id) should still have their local
        progress_log updated; mirror is a no-op (not an error)."""
        task = Task(id="t-nope", title="t", metadata={})
        await self.executor._append_progress(task, "hello")
        self.assertEqual(task.metadata["progress_log"], ["hello"])


class StageB13RemovalTests(unittest.IsolatedAsyncioTestCase):
    """work-item mirror check: verify employee_prompt_context /
    employee_delta_context are NO LONGER on company-mode task.metadata
    but ARE on work_item.metadata — readers via WorkItemContextView still
    see them through the work_item side."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def test_company_task_metadata_omits_prompt_and_delta_context(self) -> None:
        """Read the company_mode module source and confirm the two fields
        are no longer assigned into company-mode task metadata at the
        runtime bootstrap/materialization paths."""
        import inspect

        from opc.layer2_organization import company_mode

        src = inspect.getsource(company_mode)
        # These literal assignments were removed by Step 13. The strings
        # ``"employee_prompt_context"`` and ``"employee_delta_context"``
        # should now only appear as work_item.metadata writes, not inside
        # task_metadata dict literals.
        for forbidden in (
            '"employee_prompt_context": str((employee_assignment',
            '"employee_delta_context": str((employee_assignment',
        ):
            self.assertNotIn(
                forbidden,
                src,
                f"Step 13 regression: task_metadata dict still sets {forbidden!r} — "
                "these belong on work_item.metadata only.",
            )


class WorkItemExecutionCopyTests(unittest.TestCase):
    def test_manager_mutation_metadata_is_available_to_revised_child_turn(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-manager",
            run_id="run-1",
            cell_id="team::cto",
            role_id="cto",
            seat_id="seat::team::ceo::cto",
            title="Revised manager item",
            metadata={
                "manager_mutation_revision": 2,
                "manager_mutation_action": "modify",
                "manager_mutation_reason": "User changed the game direction.",
                "manager_mutation_user_input": "Replace the old cooking game with Neon Rails.",
            },
        )

        copied = copy_work_item_execution_metadata(item)

        self.assertEqual(copied["manager_mutation_revision"], 2)
        self.assertEqual(copied["manager_mutation_action"], "modify")
        self.assertIn("game direction", copied["manager_mutation_reason"])
        self.assertIn("Neon Rails", copied["manager_mutation_user_input"])

    def test_report_source_metadata_is_available_to_report_turn(self) -> None:
        item = DelegationWorkItem(
            work_item_id="report::wi-worker::v1",
            run_id="run-1",
            cell_id="team::cto",
            role_id="senior_engineer",
            seat_id="seat::team::cto::senior_engineer",
            title="Report #1",
            metadata={
                "report_execution_work_item": True,
                "report_source_summary": "Implemented the browser game files.",
                "report_source_result_content": "Created index.html, styles.css, and game.js.",
                "report_source_evidence": {
                    "output_paths": ["coral-magnet-maze/index.html"],
                    "verification_results": {
                        "checks": [{"command": "node --check game.js", "status": "pass"}],
                    },
                },
            },
        )

        copied = copy_work_item_execution_metadata(item)

        self.assertEqual(copied["report_source_summary"], "Implemented the browser game files.")
        self.assertIn("index.html", copied["report_source_result_content"])
        self.assertEqual(
            copied["report_source_evidence"]["output_paths"],
            ["coral-magnet-maze/index.html"],
        )

    def test_existing_runtime_task_refreshes_from_modified_work_item(self) -> None:
        executor = _executor_with_store(None)
        item = DelegationWorkItem(
            work_item_id="wi-game",
            run_id="run-1",
            cell_id="team::cto",
            role_id="cto",
            seat_id="seat::team::ceo::cto",
            title="Build playable Neon Rails Rush browser game",
            summary="Implement the revised Neon Rails Rush game artifact.",
            kind="execute",
            projection_id="cto::execute::game",
            phase=Phase.READY,
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "execute",
                "prompt_contract": {
                    "version": 2,
                    "task_brief": "Build Neon Rails Rush, not Moon Orchard Panic.",
                },
                "manager_mutation_revision": 1,
                "manager_mutation_action": "modify",
                "manager_mutation_reason": "User changed the game direction.",
                "manager_mutation_user_input": "Switch to Neon Rails Rush.",
            },
        )
        task = Task(
            id="task-game",
            title="Build playable Moon Orchard Panic browser game",
            description="Implement the old Moon Orchard Panic artifact.",
            status=TaskStatus.BLOCKED,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "cto::execute::game",
                "work_item_turn_type": "execute",
                "prompt_contract": {
                    "version": 2,
                    "task_brief": "Build Moon Orchard Panic.",
                },
                "work_item_runtime_plan": {
                    "projection_id": "cto::execute::game",
                    "turn_type": "execute",
                    "summary": "Old summary",
                },
                "dispatch_hold": "company_runtime_suspended",
                "company_runtime_stop_state": "suspended",
                "rework_feedback": "Old rejected scope.",
                "progress_log": ["stale task-side progress"],
            },
        )
        set_linked_work_item_id(task, "wi-game")

        changed = executor._apply_work_item_projection_to_task(task, item)

        self.assertTrue(changed)
        self.assertEqual(task.title, "Build playable Neon Rails Rush browser game")
        self.assertEqual(task.description, "Implement the revised Neon Rails Rush game artifact.")
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(
            task.metadata["prompt_contract"]["task_brief"],
            "Build Neon Rails Rush, not Moon Orchard Panic.",
        )
        self.assertEqual(task.metadata["manager_mutation_action"], "modify")
        self.assertEqual(task.metadata["manager_mutation_revision"], 1)
        self.assertNotIn("dispatch_hold", task.metadata)
        self.assertNotIn("company_runtime_stop_state", task.metadata)
        self.assertNotIn("rework_feedback", task.metadata)
        self.assertNotIn("progress_log", task.metadata)
        self.assertEqual(
            task.metadata["work_item_runtime_plan"]["summary"],
            "Implement the revised Neon Rails Rush game artifact.",
        )

    def test_delivery_dependency_invalidation_clears_stale_task_package(self) -> None:
        executor = _executor_with_store(None)
        invalidated_at = "2026-08-25T19:31:50"
        item = DelegationWorkItem(
            work_item_id="delivery::one",
            run_id="run-1",
            cell_id="team::ceo",
            role_id="ceo",
            seat_id="seat::team::ceo::ceo",
            title="Final delivery",
            kind="delivery",
            projection_id="ceo::delivery::one",
            phase=Phase.READY_FOR_REWORK,
            metadata={
                "work_kind": "delivery",
                "requires_user_feedback": True,
                "delivery_inputs_invalidated_at": invalidated_at,
                "allowed_delegate_role_ids": [],
            },
        )
        task = Task(
            id="delivery-task",
            title="Final delivery",
            status=TaskStatus.DONE,
            metadata={
                "work_item_projection_id": "ceo::delivery::one",
                "work_item_turn_type": "deliver",
                "delivery_package": {"content": "obsolete"},
                "feedback_closed": True,
                "human_review_closed": True,
                "allowed_delegate_role_ids": ["cto", "cmo"],
            },
            context_snapshot={
                "delivery_package": {"content": "obsolete"},
                "work_item_owned_outputs": {
                    "delivery_package": {"content": "obsolete"}
                },
            },
        )
        set_linked_work_item_id(task, "delivery::one")

        changed = executor._apply_work_item_projection_to_task(task, item)

        self.assertTrue(changed)
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertNotIn("delivery_package", task.metadata)
        self.assertNotIn("delivery_package", task.context_snapshot)
        self.assertNotIn("work_item_owned_outputs", task.context_snapshot)
        self.assertFalse(task.metadata["feedback_closed"])
        self.assertFalse(task.metadata["human_review_closed"])
        self.assertEqual(task.metadata["allowed_delegate_role_ids"], [])
        self.assertEqual(
            task.metadata["delivery_outputs_reset_for_inputs_invalidated_at"],
            invalidated_at,
        )


class ApplyDoneTransitionWorkerOnlyContractTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for the review-of-review recursion bug observed in
    new20. Encodes the root work-item root-fix CONTRACT:

    * ``_apply_done_transition`` is for WORKER tasks only.
    * For review execution work_items, it is a strict no-op (returns None
      with zero side effects — no phase write, no spawn, no finalize).
    * Review card finalization happens EXACTLY ONCE at
      ``_run_claimed_work_item`` tail via ``_finalize_review_work_item``.

    Violations of this contract produce either (a) infinite review-of-review
    recursion (spawn branch), or (b) double verdict application (finalize
    branch). The contract is enforced by the helper's entry guard.
    """

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.executor = _executor_with_store(self.store)
        # Record any calls to _finalize_review_work_item so we can assert
        # the helper does NOT invoke it (contract: tail is the sole caller).
        self._finalize_calls: list[str] = []

        async def fake_finalize(task):
            self._finalize_calls.append(task.id)

        self.executor._finalize_review_work_item = fake_finalize  # type: ignore

        # Same for the spawn path — the helper must not spawn a review
        # work_item for a review card under any circumstance.
        self._spawn_calls: list[str] = []

        async def fake_spawn(*args, **kwargs):
            self._spawn_calls.append(str(kwargs.get("work_item_id") or (args[0] if args else "")))

        self.executor._ensure_review_work_item_for_work_item = fake_spawn  # type: ignore
        self._report_calls: list[str] = []

        async def fake_report(*args, **kwargs):
            self._report_calls.append(str(kwargs.get("work_item_id") or (args[0] if args else "")))

        self.executor._ensure_report_work_item_for_work_item = fake_report  # type: ignore

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_review_card_is_strict_noop(self) -> None:
        """CONTRACT: review_execution_work_item=True → helper is a pure
        no-op. No finalize call (tail owns that), no spawn (prevents
        review-of-review), no phase write."""
        task = Task(
            id="review-task-1",
            title="Review #1: worker card",
            metadata={
                "review_execution_work_item": True,
                "review_task": True,
            },
        )
        set_linked_work_item_id(task, "review::orig::v1")
        returned = await self.executor._apply_done_transition(task, result=None)
        self.assertIsNone(returned)
        self.assertEqual(self._finalize_calls, [], "helper must NOT call finalize (double-fire with tail)")
        self.assertEqual(self._spawn_calls, [], "helper must NOT spawn review (would create review-of-review)")

    async def test_non_review_task_bypasses_guard(self) -> None:
        """A task WITHOUT the review flag hits the normal worker path.
        Here we use no work_item_id so it takes the task-mode fallback,
        confirming the guard is gated precisely on the review flag."""
        task = Task(
            id="normal-task",
            title="Regular worker work item",
            metadata={},
        )
        await self.executor._apply_done_transition(task, result=None)
        self.assertEqual(self._finalize_calls, [])
        self.assertEqual(self._spawn_calls, [])

    async def test_attention_review_wrapper_auto_approves_without_review_chain(self) -> None:
        """Attention wrappers may have turn_type=review, but they are not
        deliverables and must not create report/review-of-review cards."""
        work_item = DelegationWorkItem(
            work_item_id="wi-attention-review",
            run_id="run-1",
            cell_id="team::cto",
            team_id="team::cto",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            parent_work_item_id="wi-parent",
            manager_role_id="ceo",
            manager_seat_id="seat::team::ceo::ceo",
            title="Review Turn: cto",
            kind="review",
            projection_id="wi-attention-review",
            phase=Phase.RUNNING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_kind": "review",
                "work_item_turn_type": "review",
                "attention_work_item": True,
            },
        )
        await self.store.save_delegation_work_item(work_item)
        task = Task(
            id="task-attention-review",
            title="Review Turn: cto",
            assigned_to="cto",
            status=TaskStatus.DONE,
            project_id="proj1",
            session_id="scope-1",
            parent_session_id="root",
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "delegation_cell_id": "team::cto",
                "work_kind": "review",
                "work_item_turn_type": "review",
                "attention_work_item_id": "wi-attention-review",
                "manager_role_id": "ceo",
                "manager_seat_id": "seat::team::ceo::ceo",
            },
        )
        set_linked_work_item_id(task, "wi-attention-review")
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(work_item.work_item_id, task.id)

        returned = await self.executor._apply_done_transition(
            task,
            result=TaskResult(status=TaskStatus.DONE, content="Reviewed the board."),
        )

        self.assertEqual(returned, Phase.APPROVED)
        after = await self.store.get_delegation_work_item("wi-attention-review")
        assert after is not None
        self.assertEqual(after.phase, Phase.APPROVED)
        self.assertEqual(after.metadata.get("attention_work_item_outcome"), "completed")
        self.assertEqual(self._report_calls, [])
        self.assertEqual(self._spawn_calls, [])


if __name__ == "__main__":
    unittest.main()
