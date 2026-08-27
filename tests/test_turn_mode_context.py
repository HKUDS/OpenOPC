"""Phase C: infer_turn_mode classifier + mode-aware context injection.

Covers:
    - ``infer_turn_mode`` returns the correct TurnMode for each of the 5
      canonical work-item states.
    - ``ContextAssembler.build_rework_feedback_context`` injects the
      reviewer's reject reason, rework attempt counter, and structured
      verdict lists into the prompt when the work item is in
      READY_FOR_REWORK.
    - ``ContextAssembler.build_turn_mode_context`` emits a header
      naming the current turn mode.
    - An end-to-end rework scenario: a child work item that has been
      bounced by the reviewer reappears on the dispatcher's queue; the
      prompt built for its second attempt contains the reviewer's
      specific reject reason so the agent can address it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer2_organization import comms as file_comms
from opc.layer2_organization.prompt_contract import make_prompt_contract
from opc.layer2_organization.turn_mode import TurnMode, infer_turn_mode
from opc.layer2_organization.work_item_links import set_linked_work_item_id


def _build_wi(**kwargs) -> DelegationWorkItem:
    """Create a DelegationWorkItem with sensible defaults."""
    defaults = dict(
        run_id="r",
        cell_id="c",
        role_id="worker",
        seat_id="seat-worker",
        manager_role_id="mgr",
        manager_seat_id="seat-mgr",
        title="t",
    )
    defaults.update(kwargs)
    return DelegationWorkItem(**defaults)


def _link_task(task: Task, work_item_id: str) -> Task:
    if work_item_id:
        set_linked_work_item_id(task, work_item_id)
    return task


class InferTurnModeTests(unittest.TestCase):
    def test_review_queue_entry_forces_review(self) -> None:
        wi = _build_wi(phase=Phase.RUNNING)
        self.assertEqual(infer_turn_mode(wi, is_review_entry=True), TurnMode.REVIEW)

    def test_review_execution_metadata_flag_is_review(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={"review_execution_work_item": True},
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REVIEW)

    def test_review_kind_is_review(self) -> None:
        wi = _build_wi(kind="review", phase=Phase.RUNNING)
        self.assertEqual(infer_turn_mode(wi), TurnMode.REVIEW)

    def test_ready_for_rework_is_rework(self) -> None:
        wi = _build_wi(
            phase=Phase.READY_FOR_REWORK,
            metadata={"rework_feedback": "fix X and Y"},
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_parent_with_deps_and_running_is_integrate(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "dependency_work_item_ids": ["child-a", "child-b"],
                "allowed_delegate_role_ids": ["designer", "copywriter"],
                "frontier": "resumed",
            },
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.INTEGRATE)

    def test_manager_without_children_is_delegate(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={"allowed_delegate_role_ids": ["designer", "copywriter"]},
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.DELEGATE)

    def test_leaf_worker_is_execute(self) -> None:
        wi = _build_wi(phase=Phase.RUNNING)
        self.assertEqual(infer_turn_mode(wi), TurnMode.EXECUTE)

    def test_running_gate_harness_feedback_is_rework(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_feedback": (
                    "critical_claims[0] period_end is outside the analysis year"
                ),
                "gate_harness_rework_count": 1,
            },
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_task_owned_gate_harness_count_supplements_claimed_work_item(self) -> None:
        wi = _build_wi(phase=Phase.RUNNING, metadata={})
        self.assertEqual(
            infer_turn_mode(
                wi,
                supplemental_metadata={"gate_harness_rework_count": 2},
            ),
            TurnMode.REWORK,
        )

    def test_task_gate_fields_override_stale_empty_work_item_projection(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_feedback": "",
                "gate_harness_rework_count": 0,
                "gate_harness_rework_request": {},
            },
        )

        self.assertEqual(
            infer_turn_mode(
                wi,
                supplemental_metadata={
                    "gate_harness_rework_request": {
                        "feedback": "CURRENT_TASK_GATE_FAILURE",
                    },
                },
            ),
            TurnMode.REWORK,
        )

    def test_task_explicit_gate_clear_suppresses_stale_work_item_rework(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_feedback": "STALE_WORK_ITEM_FAILURE",
                "gate_harness_rework_count": 99,
                "gate_harness_rework_request": {"rework_round": 99},
            },
        )

        self.assertEqual(
            infer_turn_mode(
                wi,
                supplemental_metadata={
                    "gate_harness_rework_feedback": "",
                    "gate_harness_rework_count": 0,
                    "gate_harness_rework_request": {},
                },
            ),
            TurnMode.EXECUTE,
        )

    def test_gate_harness_request_alone_is_rework(self) -> None:
        wi = _build_wi(phase=Phase.RUNNING, metadata={})

        self.assertEqual(
            infer_turn_mode(
                wi,
                supplemental_metadata={
                    "gate_harness_rework_request": {
                        "blockers": ["Correct the deterministic artifact."],
                    },
                },
            ),
            TurnMode.REWORK,
        )

    def test_malformed_review_and_gate_counts_fail_safe(self) -> None:
        malformed_counts = ("not-an-int", {"value": 2}, [2], float("inf"))
        for metadata_key in (
            "review_rework_count",
            "gate_harness_rework_count",
        ):
            for malformed_count in malformed_counts:
                with self.subTest(
                    metadata_key=metadata_key,
                    malformed_count=malformed_count,
                ):
                    wi = _build_wi(
                        phase=Phase.RUNNING,
                        metadata={metadata_key: malformed_count},
                    )
                    self.assertEqual(infer_turn_mode(wi), TurnMode.EXECUTE)

    def test_malformed_gate_count_does_not_hide_valid_feedback(self) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_count": {"invalid": True},
                "gate_harness_rework_feedback": "FIX_THE_CURRENT_FAILURE",
            },
        )

        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_rework_takes_precedence_over_integrate(self) -> None:
        # A manager who was rejected on their integration turn must be
        # routed to REWORK (the rework_feedback block is more critical
        # than the team_deliverables block at this point).
        wi = _build_wi(
            phase=Phase.READY_FOR_REWORK,
            metadata={
                "dependency_work_item_ids": ["child-a"],
                "rework_feedback": "missing acceptance section",
            },
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_review_takes_precedence_over_everything(self) -> None:
        wi = _build_wi(
            phase=Phase.READY_FOR_REWORK,  # would otherwise be REWORK
            kind="review",
            metadata={
                "gate_harness_rework_count": 3,
                "gate_harness_rework_request": {"feedback": "gate failure"},
            },
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REVIEW)

    def test_report_takes_precedence_over_gate_rework(self) -> None:
        wi = _build_wi(
            phase=Phase.READY_FOR_REWORK,
            kind="report",
            metadata={
                "report_execution_work_item": True,
                "gate_harness_rework_count": 3,
                "gate_harness_rework_request": {"feedback": "gate failure"},
            },
        )

        self.assertEqual(infer_turn_mode(wi), TurnMode.REPORT)


class BuildReworkFeedbackContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.memory = AsyncMock()
        self.memory.build_focused_memory_context.return_value = ""
        self.memory.build_memory_context.return_value = ""
        self.memory.build_agent_memory_context.return_value = ""
        self.assembler = ContextAssembler(memory=self.memory, store=self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_rework_feedback_is_injected_when_work_item_is_ready_for_rework(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-1",
            phase=Phase.RUNNING,
            role_id="designer",
            seat_id="seat::team::cmo::designer",
            manager_role_id="cmo",
            manager_seat_id="seat::team::ceo::cmo",
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        await self.store.update_delegation_work_item(
            "wi-1",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        await self.store.update_delegation_work_item(
            "wi-1",
            phase=Phase.READY_FOR_REWORK,
            metadata_updates={
                "rework_feedback": (
                    "The video concept is missing two required features (real-time "
                    "subtitles and language coverage claim). Reject."
                ),
                "structured_review_verdict": {
                    "label": "reject",
                    "summary": "missing features",
                    "blocking_issues": [
                        "Video concept omits real-time subtitle rendering.",
                        "No coverage claim for the 100+ languages promise.",
                    ],
                    "followups": [
                        "Consider a 10-second 'global reach' intro.",
                    ],
                },
                "review_rework_count": 1,
                "review_owner_role_id": "cmo",
            },
        )

        task = Task(
            id="t1",
            title="Create video concept",
            description="fancy promotional video",
            assigned_to="designer",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
        )
        task = _link_task(task, "wi-1")
        rendered = await self.assembler.build_rework_feedback_context(task)
        # Header present
        self.assertIn("## Reviewer Feedback (Rework Required)", rendered)
        # Reviewer attribution
        self.assertIn("Reviewer: cmo", rendered)
        # Rework attempt counter
        self.assertIn("Rework attempt: #1", rendered)
        # The reviewer's actual reject text verbatim
        self.assertIn(
            "The video concept is missing two required features",
            rendered,
        )
        # Blocking issues rendered as bullets
        self.assertIn("### Blocking Issues", rendered)
        self.assertIn("Video concept omits real-time subtitle rendering.", rendered)
        self.assertIn("No coverage claim", rendered)
        # Follow-ups rendered separately
        self.assertIn("### Follow-ups", rendered)
        self.assertIn("Consider a 10-second 'global reach' intro.", rendered)

    async def test_non_rework_work_item_produces_no_feedback_section(self) -> None:
        wi = _build_wi(
            work_item_id="wi-2",
            phase=Phase.RUNNING,
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t2",
            title="Do something",
            description="normal task",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
        )
        task = _link_task(task, "wi-2")
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertEqual(rendered, "")

    async def test_gate_harness_feedback_is_a_first_class_correction_block(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-gate-rework",
            phase=Phase.RUNNING,
            role_id="investment_analyst",
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-gate-rework",
            title="Correct the investment evidence",
            description="",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "gate_harness_rework_count": 2,
                "gate_harness_rework_feedback": (
                    "Deterministic pre-delivery validation failed: "
                    "company_analysis.json period_end is outside 2026."
                ),
                "gate_harness_rework_request": {
                    "source_work_item_title": "Final investment delivery",
                    "rework_round": 2,
                    "feedback": (
                        "Deterministic pre-delivery validation failed: "
                        "company_analysis.json period_end is outside 2026."
                    ),
                    "blockers": [
                        "company_analysis.json: use an ended period in calendar year 2026",
                        "risk_analysis.json: value and period must occur in one durable hit",
                    ],
                    "constraints": ["Preserve exactly three critical claims."],
                },
            },
            result={"content": "All files already exist; no edits required."},
        )
        task = _link_task(task, "wi-gate-rework")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertIn(
            "## Deterministic Gate Feedback (Rework Required)", rendered
        )
        self.assertIn("Rework attempt: #2", rendered)
        self.assertIn("period_end is outside 2026", rendered)
        self.assertIn("value and period must occur in one durable hit", rendered)
        self.assertIn("Preserve exactly three critical claims.", rendered)
        self.assertIn("not a request to re-verify", rendered)
        self.assertIn("Do not claim success based only on syntax checks", rendered)
        self.assertIn("Do not repeat it unchanged", rendered)

    async def test_task_gate_request_beats_stale_higher_work_item_round(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-stale-gate-round",
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_count": 99,
                "gate_harness_rework_feedback": "STALE_WORK_ITEM_FEEDBACK",
                "gate_harness_rework_request": {
                    "rework_round": 99,
                    "feedback": "STALE_WORK_ITEM_REQUEST",
                    "blockers": ["STALE_WORK_ITEM_BLOCKER"],
                },
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-current-gate-round",
            title="Apply the current controller correction",
            description="",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "gate_harness_rework_request": {
                    "rework_round": 1,
                    "feedback": "CURRENT_TASK_REQUEST",
                    "blockers": ["CURRENT_TASK_BLOCKER"],
                },
            },
        )
        task = _link_task(task, "wi-stale-gate-round")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertIn("Rework attempt: #1", rendered)
        self.assertIn("CURRENT_TASK_REQUEST", rendered)
        self.assertIn("CURRENT_TASK_BLOCKER", rendered)
        self.assertNotIn("#99", rendered)
        self.assertNotIn("STALE_WORK_ITEM", rendered)

    async def test_work_item_gate_request_is_fallback_when_task_has_no_gate_fields(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-gate-fallback",
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_request": {
                    "rework_round": 2,
                    "feedback": "WORK_ITEM_FALLBACK_REQUEST",
                    "blockers": ["WORK_ITEM_FALLBACK_BLOCKER"],
                },
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-gate-fallback",
            title="Use compatibility gate projection",
            description="",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={"runtime_model": "multi_team_org"},
        )
        task = _link_task(task, "wi-gate-fallback")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertIn("Rework attempt: #2", rendered)
        self.assertIn("WORK_ITEM_FALLBACK_REQUEST", rendered)
        self.assertIn("WORK_ITEM_FALLBACK_BLOCKER", rendered)

    async def test_task_explicit_gate_clear_suppresses_stale_work_item_context(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-stale-gate-context",
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_count": 8,
                "gate_harness_rework_feedback": "STALE_GATE_CONTEXT",
                "gate_harness_rework_request": {
                    "rework_round": 8,
                    "blockers": ["STALE_GATE_BLOCKER"],
                },
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-clear-stale-gate-context",
            title="Current task has no gate correction",
            description="",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "gate_harness_rework_feedback": "",
                "gate_harness_rework_count": 0,
                "gate_harness_rework_request": {},
            },
        )
        task = _link_task(task, "wi-stale-gate-context")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertEqual(rendered, "")

    async def test_gate_context_parses_scalar_collections_and_counts_safely(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-malformed-gate-context",
            phase=Phase.RUNNING,
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-malformed-gate-context",
            title="Normalize untrusted durable gate metadata",
            description="",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "gate_harness_rework_count": "not-an-int",
                "gate_harness_rework_request": {
                    "rework_round": {"invalid": True},
                    "feedback": "VALID_GATE_FEEDBACK",
                    "blockers": "ONE_COMPLETE_BLOCKER",
                    "constraints": 42,
                },
            },
        )
        task = _link_task(task, "wi-malformed-gate-context")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertIn("VALID_GATE_FEEDBACK", rendered)
        self.assertEqual(rendered.count("- ONE_COMPLETE_BLOCKER"), 1)
        self.assertNotIn("Rework attempt: #", rendered)
        self.assertNotIn("- 42", rendered)

    async def test_reviewer_context_ignores_malformed_rework_count(self) -> None:
        wi = _build_wi(
            work_item_id="wi-malformed-review-count",
            phase=Phase.RUNNING,
            metadata={
                "rework_feedback": "VALID_REVIEW_FEEDBACK",
                "review_rework_count": {"invalid": True},
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-malformed-review-count",
            title="Handle malformed review metadata",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={"runtime_model": "multi_team_org"},
        )
        task = _link_task(task, "wi-malformed-review-count")

        rendered = await self.assembler.build_rework_feedback_context(task)

        self.assertIn("VALID_REVIEW_FEEDBACK", rendered)
        self.assertNotIn("Rework attempt: #", rendered)

    async def test_full_prompt_ignores_malformed_task_review_count(self) -> None:
        wi = _build_wi(
            work_item_id="wi-malformed-task-review-count",
            phase=Phase.RUNNING,
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-malformed-task-review-count",
            title="Retry after manager review",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "rework_feedback": "FIX_WITH_MALFORMED_DURABLE_COUNT",
                "review_rework_count": {"invalid": True},
            },
        )
        task = _link_task(task, "wi-malformed-task-review-count")

        sections = await self.assembler.build_sections(task, role_id="worker")
        system_context = await self.assembler.build_system_context(
            task,
            role_id="worker",
        )

        self.assertIn("FIX_WITH_MALFORMED_DURABLE_COUNT", system_context)
        self.assertIn("## Review Rework (Round 1)", sections["core"])

    async def test_review_prompt_suppresses_stale_gate_harness_context(self) -> None:
        wi = _build_wi(
            work_item_id="wi-review-gate-conflict",
            kind="review",
            phase=Phase.RUNNING,
            metadata={"review_execution_work_item": True},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-review-gate-conflict",
            title="Review the worker result",
            description="",
            assigned_to="reviewer",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "review",
                "review_execution_work_item": True,
                "gate_harness_rework_count": 7,
                "gate_harness_rework_feedback": "STALE_REVIEW_GATE_SECRET",
                "gate_harness_rework_request": {
                    "rework_round": 7,
                    "feedback": "STALE_REVIEW_GATE_SECRET",
                },
            },
        )
        task = _link_task(task, "wi-review-gate-conflict")

        sections = await self.assembler.build_sections(task, role_id="reviewer")
        assembled_context = "\n".join(sections.values())

        self.assertIn("Required action: review", sections["turn_mode"])
        self.assertEqual(sections["rework_feedback"], "")
        self.assertNotIn("STALE_REVIEW_GATE_SECRET", assembled_context)
        self.assertNotIn("## Gate Harness Rework", assembled_context)

    async def test_report_prompt_suppresses_stale_gate_harness_context(self) -> None:
        wi = _build_wi(
            work_item_id="wi-report-gate-conflict",
            kind="report",
            phase=Phase.RUNNING,
            metadata={"report_execution_work_item": True},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-report-gate-conflict",
            title="Report the worker result",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "report",
                "report_execution_work_item": True,
                "gate_harness_rework_count": 7,
                "gate_harness_rework_feedback": "STALE_REPORT_GATE_SECRET",
                "gate_harness_rework_request": {
                    "rework_round": 7,
                    "feedback": "STALE_REPORT_GATE_SECRET",
                },
            },
        )
        task = _link_task(task, "wi-report-gate-conflict")

        sections = await self.assembler.build_sections(task, role_id="worker")
        assembled_context = "\n".join(sections.values())

        self.assertIn("Required action: report", sections["turn_mode"])
        self.assertEqual(sections["rework_feedback"], "")
        self.assertNotIn("STALE_REPORT_GATE_SECRET", assembled_context)
        self.assertNotIn("## Gate Harness Rework", assembled_context)

    async def test_report_task_identity_suppresses_gate_without_linked_work_item(
        self,
    ) -> None:
        task = Task(
            id="t-report-no-work-item",
            title="Report the worker result",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_turn_type": "report",
                "report_execution_work_item": True,
                "gate_harness_rework_count": 4,
                "gate_harness_rework_feedback": "STALE_UNLINKED_REPORT_SECRET",
            },
        )

        sections = await self.assembler.build_sections(task, role_id="worker")
        system_context = await self.assembler.build_system_context(
            task,
            role_id="worker",
        )

        self.assertIn("Required action: report", sections["turn_mode"])
        self.assertEqual(sections["rework_feedback"], "")
        self.assertNotIn("STALE_LINKED_EXECUTE_GATE", system_context)
        self.assertNotIn("STALE_TASK_GATE_FOR_REPORT", system_context)

    async def test_current_auxiliary_mode_suppresses_gate_from_full_context(
        self,
    ) -> None:
        cases = (
            ("report_required", "report", "report"),
            ("review_execute", "review", "review"),
        )
        for current_mode, expected_action, suffix in cases:
            with self.subTest(current_mode=current_mode):
                wi = _build_wi(
                    work_item_id=f"wi-current-aux-{suffix}",
                    kind="execute",
                    phase=Phase.RUNNING,
                    metadata={},
                )
                await self.store.save_delegation_work_item(wi)
                task = Task(
                    id=f"t-current-aux-{suffix}",
                    title="Current auxiliary turn",
                    description="",
                    assigned_to="worker",
                    status=TaskStatus.RUNNING,
                    project_id="p",
                    session_id="s",
                    metadata={
                        "runtime_model": "multi_team_org",
                        "current_turn_mode": current_mode,
                        "gate_harness_rework_feedback": (
                            f"STALE_CURRENT_MODE_GATE_{suffix.upper()}"
                        ),
                    },
                )
                task = _link_task(task, f"wi-current-aux-{suffix}")

                sections = await self.assembler.build_sections(
                    task,
                    role_id="worker",
                )
                system_context = await self.assembler.build_system_context(
                    task,
                    role_id="worker",
                )

                self.assertIn(
                    f"Required action: {expected_action}",
                    sections["turn_mode"],
                )
                self.assertEqual(sections["rework_feedback"], "")
                self.assertNotIn("STALE_CURRENT_MODE_GATE", system_context)

    async def test_current_report_turn_beats_stale_linked_execute_work_item(
        self,
    ) -> None:
        wi = _build_wi(
            work_item_id="wi-stale-execute-for-report",
            kind="execute",
            phase=Phase.RUNNING,
            metadata={
                "gate_harness_rework_count": 12,
                "gate_harness_rework_feedback": "STALE_LINKED_EXECUTE_GATE",
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-current-report-stale-link",
            title="Current report projection",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "report_required",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_feedback": "STALE_TASK_GATE_FOR_REPORT",
            },
        )
        task = _link_task(task, "wi-stale-execute-for-report")

        turn_mode = await self.assembler.build_turn_mode_context(task)
        rework = await self.assembler.build_rework_feedback_context(task)

        self.assertIn("Required action: report", turn_mode)
        self.assertEqual(rework, "")

    async def test_generic_task_kind_does_not_override_execute_mode(self) -> None:
        for generic_kind in ("report", "review"):
            with self.subTest(generic_kind=generic_kind):
                wi = _build_wi(
                    work_item_id=f"wi-generic-kind-{generic_kind}",
                    kind="execute",
                    phase=Phase.RUNNING,
                    metadata={},
                )
                await self.store.save_delegation_work_item(wi)
                task = Task(
                    id=f"t-generic-kind-{generic_kind}",
                    title="Ordinary business execute turn",
                    description="",
                    assigned_to="worker",
                    status=TaskStatus.RUNNING,
                    project_id="p",
                    session_id="s",
                    metadata={
                        "runtime_model": "multi_team_org",
                        "kind": generic_kind,
                    },
                )
                task = _link_task(task, f"wi-generic-kind-{generic_kind}")

                turn_mode = await self.assembler.build_turn_mode_context(task)

                self.assertIn("Required action: execute", turn_mode)

    def test_legacy_gate_summary_is_suppressed_for_non_business_auxiliary_turns(
        self,
    ) -> None:
        cases = (
            {"work_item_turn_type": "self_evolution"},
            {
                "runtime_auxiliary_task": True,
                "runtime_auxiliary_kind": "meeting_turn",
            },
            {"attention_work_item": True},
        )
        for auxiliary_metadata in cases:
            with self.subTest(auxiliary_metadata=auxiliary_metadata):
                task = Task(
                    id="t-aux-stale-gate",
                    title="Auxiliary runtime turn",
                    description="",
                    assigned_to="worker",
                    status=TaskStatus.RUNNING,
                    project_id="p",
                    session_id="s",
                    metadata={
                        "runtime_model": "multi_team_org",
                        "gate_harness_rework_feedback": "STALE_AUX_GATE_SECRET",
                        **auxiliary_metadata,
                    },
                )

                rendered = self.assembler._build_working_summary(task)

                self.assertNotIn("STALE_AUX_GATE_SECRET", rendered)
                self.assertNotIn("## Gate Harness Rework", rendered)

    async def test_rework_without_feedback_still_empty(self) -> None:
        # Phase is rework but feedback metadata missing (pathological);
        # we don't inject an empty block.
        wi = _build_wi(
            work_item_id="wi-3",
            phase=Phase.RUNNING,
            metadata={},
        )
        await self.store.save_delegation_work_item(wi)
        await self.store.update_delegation_work_item(
            "wi-3",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        await self.store.update_delegation_work_item(
            "wi-3",
            phase=Phase.READY_FOR_REWORK,
        )
        task = Task(
            id="t3",
            title="t",
            description="",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
        )
        task = _link_task(task, "wi-3")
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertEqual(rendered, "")


class BuildTurnModeContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        class _Memory:
            async def build_focused_memory_context(self, **_: object) -> str:
                return ""

            async def build_memory_context(self, **_: object) -> str:
                return ""

            async def build_agent_memory_context(self, *_: object, **__: object) -> str:
                return ""

        self.memory = _Memory()
        self.assembler = ContextAssembler(memory=self.memory, store=self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_emits_header_for_multi_team_org_task(self) -> None:
        wi = _build_wi(
            work_item_id="wi-ex",
            phase=Phase.RUNNING,
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-ex",
            title="t",
            description="",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
        )
        task = _link_task(task, "wi-ex")
        rendered = await self.assembler.build_turn_mode_context(task)
        self.assertTrue(rendered.startswith("## Turn Mode"))
        self.assertIn("- Required action:", rendered)
        self.assertIn("execute", rendered)

    async def test_opaque_external_team_does_not_receive_internal_turn_guidance(self) -> None:
        task = Task(
            id="t-opaque-team",
            title="Opaque team assignment",
            description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_unit_kind": "opaque_external_team",
                "current_turn_mode": "worker_execute",
            },
        )

        rendered = await self.assembler.build_turn_mode_context(task)

        self.assertEqual(rendered, "")

    async def test_legacy_dispatch_mode_is_upgraded_to_manager_decision(self) -> None:
        wi = _build_wi(
            work_item_id="wi-delegate",
            phase=Phase.RUNNING,
            metadata={"allowed_delegate_role_ids": ["designer", "copywriter"]},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-delegate",
            title="t",
            description="",
            assigned_to="manager",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
            },
        )
        task = _link_task(task, "wi-delegate")
        rendered = await self.assembler.build_turn_mode_context(task)
        self.assertTrue(rendered.startswith("## Turn Mode"))
        self.assertIn("- Runtime state: `dispatch_required`", rendered)
        self.assertIn("- Required action:", rendered)
        self.assertIn("decide", rendered)
        self.assertIn("delegate outcome-based child WorkItems or complete it", rendered)

    async def test_authoritative_dispatch_mode_never_falls_back_to_execute_without_loaded_work_item(self) -> None:
        task = Task(
            id="t-manager-dispatch-without-link",
            title="CMO outcome",
            description="",
            assigned_to="cmo",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "direct_report_seat_ids": ["seat::team::cmo::content_specialist"],
            },
        )

        rendered = await self.assembler.build_turn_mode_context(task)

        self.assertIn("- Runtime state: `dispatch_required`", rendered)
        self.assertIn("- Required action: decide", rendered)
        self.assertIn("delegate outcome-based child WorkItems or complete it", rendered)
        self.assertNotIn("deliverable yourself", rendered)

    async def test_manager_decide_mode_preserves_manager_choice(self) -> None:
        task = Task(
            id="t-manager-decide",
            title="CMO outcome",
            description="",
            assigned_to="cmo",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": "manager_decide",
                "direct_report_seat_ids": ["seat::team::cmo::content_specialist"],
            },
        )

        rendered = await self.assembler.build_turn_mode_context(task)

        self.assertIn("- Runtime state: `manager_decide`", rendered)
        self.assertIn("- Required action: decide", rendered)
        self.assertIn("delegate outcome-based child WorkItems or complete it", rendered)

    async def test_inferred_legacy_manager_mode_is_also_a_choice(self) -> None:
        wi = _build_wi(
            work_item_id="wi-inferred-manager",
            phase=Phase.RUNNING,
            metadata={"allowed_delegate_role_ids": ["content_specialist"]},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-inferred-manager",
            title="CMO outcome",
            description="",
            assigned_to="cmo",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={"runtime_model": "multi_team_org"},
        )
        task = _link_task(task, "wi-inferred-manager")

        rendered = await self.assembler.build_turn_mode_context(task)

        self.assertIn("- Required action: decide", rendered)
        self.assertIn("delegate outcome-based child WorkItems or complete it", rendered)
        self.assertNotIn("execute locally only when", rendered)

    async def test_authoritative_manager_states_render_their_own_actions(self) -> None:
        cases = {
            "monitor_children": "monitor",
            "review_pending": "review",
            "synthesize_required": "integrate",
            "deliver_required": "deliver",
        }
        for runtime_state, expected_action in cases.items():
            with self.subTest(runtime_state=runtime_state):
                task = Task(
                    id=f"t-manager-{runtime_state}",
                    title="Manager outcome",
                    description="",
                    assigned_to="cto",
                    status=TaskStatus.PENDING,
                    project_id="p",
                    session_id="s",
                    metadata={
                        "runtime_model": "multi_team_org",
                        "current_turn_mode": runtime_state,
                        "direct_report_seat_ids": ["seat::team::cto::engineer"],
                    },
                )

                rendered = await self.assembler.build_turn_mode_context(task)

                self.assertIn(f"- Runtime state: `{runtime_state}`", rendered)
                self.assertIn(f"- Required action: {expected_action}", rendered)
                self.assertNotIn("deliverable yourself", rendered)

    async def test_work_item_assignment_context_renders_manager_planning_packet(self) -> None:
        wi = _build_wi(
            work_item_id="wi-assignment",
            phase=Phase.RUNNING,
            summary="Legacy summary fallback.",
            metadata={
                "prompt_contract": make_prompt_contract(
                    task_brief="Context: CTO owns the realtime translation app outcome.",
                    upstream_intent_summary="Build a multilingual realtime translator app.",
                    manager_planning_handoff="CEO plan: app build first, marketing after feature list.",
                    manager_outcome_dispatch=True,
                    owned_outcome_kind="execute",
                    scope_key="cto-app-build",
                    deliverables=["src app scaffold", "language coverage matrix"],
                    acceptance_criteria=["child work items can execute without guessing"],
                    dependency_specs=[{"kind": "role_id", "value": "cmo", "raw": "cmo"}],
                    coordination_notes="CMO depends on feature inventory.",
                    delegation_rationale="CTO owns implementation and technical feasibility.",
                    non_overlap_guard="Do not write CMO campaign copy.",
                ),
                "brief": "Context: CTO owns the realtime translation app outcome.",
                "planning_context": "CEO plan: app build first, marketing after feature list.",
                "scope_key": "cto-app-build",
                "deliverables": ["src app scaffold", "language coverage matrix"],
                "acceptance_criteria": ["child work items can execute without guessing"],
                "dependency_specs": [{"kind": "role_id", "value": "cmo", "raw": "cmo"}],
                "coordination_notes": "CMO depends on feature inventory.",
                "delegation_rationale": "CTO owns implementation and technical feasibility.",
                "non_overlap_guard": "Do not write CMO campaign copy.",
                "manager_outcome_dispatch": True,
                "owned_outcome_kind": "execute",
                "allowed_delegate_role_ids": ["engineer"],
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-assignment",
            title="t",
            description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "original_message": "Build a multilingual realtime translator app.",
            },
        )
        task = _link_task(task, "wi-assignment")

        rendered = await self.assembler.build_work_item_assignment_context(task)

        self.assertIn("## Work Item Assignment Context", rendered)
        self.assertNotIn("Original User Request", rendered)
        self.assertNotIn("Assigned Work Item Brief", rendered)
        self.assertIn("Manager Planning Handoff", rendered)
        self.assertIn("CEO plan: app build first", rendered)
        self.assertNotIn("Context: CTO owns the realtime translation app outcome.", rendered)
        self.assertIn("Manager outcome turn", rendered)
        self.assertIn("complete it directly, delegate suitable parts", rendered)
        self.assertNotIn("starts with delegation", rendered)
        self.assertNotIn("Do not execute the production work yourself", rendered)
        self.assertIn("src app scaffold", rendered)
        self.assertIn("child work items can execute without guessing", rendered)
        self.assertIn("CMO depends on feature inventory.", rendered)
        self.assertIn("Do not write CMO campaign copy.", rendered)

        view = await self.assembler.build_prompt_assignment_view(task)
        self.assertEqual(
            view.primary_task_brief,
            "Context: CTO owns the realtime translation app outcome.",
        )

    async def test_external_layers_put_current_brief_only_in_primary_task_brief(self) -> None:
        wi = _build_wi(
            work_item_id="wi-child",
            phase=Phase.RUNNING,
            parent_work_item_id="wi-parent",
            metadata={
                "prompt_contract": make_prompt_contract(
                    task_brief="Context: engineer owns the render script.",
                    upstream_intent_summary="Build the Marvel recap video pipeline.",
                    deliverables=["render.py"],
                    acceptance_criteria=["script runs without guessing"],
                    non_overlap_guard="Do not write marketing copy.",
                ),
                "brief": "Context: engineer owns the render script.",
                "upstream_intent_summary": "Build the Marvel recap video pipeline.",
                "deliverables": ["render.py"],
                "acceptance_criteria": ["script runs without guessing"],
                "non_overlap_guard": "Do not write marketing copy.",
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-child",
            title="Engineer child",
            description="Runtime task description should not own the company brief.",
            assigned_to="engineer",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
                "original_message": "Full original request that should not be expanded downstream.",
            },
        )
        task = _link_task(task, "wi-child")

        layers = await self.assembler.build_external_context_layers(task, role_id="engineer")

        self.assertEqual(layers.primary_task_brief, "Context: engineer owns the render script.")
        self.assertIn("Upstream Intent Summary", layers.company_runtime_context)
        self.assertIn("Build the Marvel recap video pipeline.", layers.company_runtime_context)
        self.assertNotIn("Assigned Work Item Brief", layers.company_runtime_context)
        self.assertNotIn("Original User Request", layers.company_runtime_context)
        self.assertNotIn("Full original request", layers.company_runtime_context)
        self.assertNotIn("Context: engineer owns the render script.", layers.company_runtime_context)

    async def test_assignment_context_requires_prompt_contract_not_legacy_brief_fallback(self) -> None:
        wi = _build_wi(
            work_item_id="wi-no-contract",
            phase=Phase.RUNNING,
            summary="Legacy summary must not be prompt source.",
            metadata={
                "brief": "Legacy brief must not be prompt source.",
                "deliverables": ["legacy deliverable"],
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-no-contract",
            title="Legacy child",
            description="Runtime task description must not be prompt source.",
            assigned_to="engineer",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
            },
        )
        task = _link_task(task, "wi-no-contract")

        rendered = await self.assembler.build_work_item_assignment_context(task)
        layers = await self.assembler.build_external_context_layers(task, role_id="engineer")

        self.assertEqual(rendered, "")
        self.assertEqual(layers.primary_task_brief, "")
        self.assertNotIn("Legacy brief", layers.company_runtime_context)
        self.assertNotIn("legacy deliverable", layers.company_runtime_context)

    async def test_synthesize_turn_suppresses_dispatch_only_assignment_fields(self) -> None:
        wi = _build_wi(
            work_item_id="wi-synth",
            phase=Phase.RUNNING,
            metadata={
                "allowed_delegate_role_ids": ["engineer"],
                "current_turn_mode": "synthesize_required",
                "prompt_contract": make_prompt_contract(
                    task_brief="Integrate approved child outputs.",
                    upstream_intent_summary="Ship the final package.",
                    manager_planning_handoff="Dispatch plan should not appear during synthesize.",
                    manager_outcome_dispatch=True,
                    owned_outcome_kind="execute",
                    deliverables=["final package"],
                    acceptance_criteria=["approved children are integrated"],
                ),
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-synth",
            title="Synthesize",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
                "current_turn_mode": "synthesize_required",
            },
        )
        task = _link_task(task, "wi-synth")

        rendered = await self.assembler.build_work_item_assignment_context(task)

        self.assertIn("Ship the final package.", rendered)
        self.assertIn("final package", rendered)
        self.assertNotIn("Manager outcome turn", rendered)
        self.assertNotIn("Manager Planning Handoff", rendered)
        self.assertNotIn("Dispatch plan should not appear", rendered)

    async def test_skipped_for_non_multi_team_org(self) -> None:
        task = Task(
            id="t-plain",
            title="t",
            description="",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={},
        )
        rendered = await self.assembler.build_turn_mode_context(task)
        self.assertEqual(rendered, "")


class TeamMemoryDigestTests(unittest.TestCase):
    def test_default_scaffold_is_not_prompt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = file_comms.resolve_layout(tmp, "proj1", "session1")
            file_comms.ensure_layout(layout, ["cto"])

            self.assertEqual(file_comms.read_team_memory_digest(layout), "")

    def test_non_empty_team_memory_is_prompt_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = file_comms.resolve_layout(tmp, "proj1", "session1")
            file_comms.ensure_layout(layout, ["cto"])
            layout.team_memory_path.write_text(
                "# Team Memory\n\n- Shared constraint: rollback first.\n",
                encoding="utf-8",
            )

            digest = file_comms.read_team_memory_digest(layout)

            self.assertIn("team_memory_path:", digest)
            self.assertIn("Shared constraint: rollback first.", digest)


class ReworkE2EPromptTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a rework'd child work item's build_sections output
    contains the reviewer's reject reason so the agent's next turn
    knows what to fix."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.memory = AsyncMock()
        self.memory.build_focused_memory_context = AsyncMock(return_value="")
        self.memory.build_memory_context = AsyncMock(return_value="")
        self.memory.build_agent_memory_context = AsyncMock(return_value="")
        self.assembler = ContextAssembler(memory=self.memory, store=self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_rework_prompt_contains_rejecter_reason(self) -> None:
        # Simulate the designer's work item: it was running, got
        # bounced by the reviewer, now back in READY_FOR_REWORK with
        # the reject rationale recorded.
        wi = _build_wi(
            work_item_id="designer-wi",
            phase=Phase.RUNNING,
            role_id="designer",
            seat_id="seat::team::cmo::designer",
            manager_role_id="cmo",
            manager_seat_id="seat::team::ceo::cmo",
        )
        await self.store.save_delegation_work_item(wi)
        await self.store.update_delegation_work_item(
            "designer-wi", phase=Phase.AWAITING_MANAGER_REVIEW
        )
        REJECT_TEXT = (
            "The hero-shot animation is too slow; shrink it to 2s max. "
            "Also the color palette clashes with the logo — use the "
            "brand-approved palette from the style guide."
        )
        await self.store.update_delegation_work_item(
            "designer-wi",
            phase=Phase.READY_FOR_REWORK,
            metadata_updates={
                "rework_feedback": REJECT_TEXT,
                "review_rework_count": 1,
                "review_owner_role_id": "cmo",
                "structured_review_verdict": {
                    "label": "reject",
                    "blocking_issues": [
                        "Hero animation must be ≤2 seconds.",
                        "Palette violates brand guide.",
                    ],
                    "followups": [],
                },
            },
        )

        # A dispatcher-rehydrated task for the rework turn.
        task = Task(
            id="designer-task-2",
            title="Create fancy promotional video concept",
            description="promo video for translation app",
            assigned_to="designer",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
                "delegation_run_id": "r",
                "work_item_role_id": "designer",
            },
        )
        task = _link_task(task, "designer-wi")

        sections = await self.assembler.build_sections(task, role_id="designer")

        # Turn mode section announces "rework" explicitly.
        self.assertIn("rework", sections["turn_mode"])
        # Rework feedback section contains the reviewer's verbatim reason.
        self.assertIn(REJECT_TEXT, sections["rework_feedback"])
        # And the structured blocking issues.
        self.assertIn("Hero animation must be ≤2 seconds.", sections["rework_feedback"])
        self.assertIn("Palette violates brand guide.", sections["rework_feedback"])

        # Joined system context includes both blocks.
        joined = "\n\n".join(part for part in sections.values() if part)
        self.assertIn(REJECT_TEXT, joined)
        self.assertIn("## Reviewer Feedback (Rework Required)", joined)
        self.assertIn("## Turn Mode", joined)


class ReworkAfterDispatcherClaimTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the new14 silent-rework-loop bug.

    Real-world sequence: reviewer rejects → work item set to
    READY_FOR_REWORK with rework_feedback in metadata → dispatcher
    immediately claims and flips phase to RUNNING → context is
    assembled with phase==RUNNING. The original gates checked
    ``phase == READY_FOR_REWORK`` and silently dropped both the
    feedback block and the REWORK turn-mode header, so the agent
    re-ran the same task with no visibility into the reviewer's
    objections. Loop continued until escalation.

    These tests pin the post-fix behaviour: feedback is gated on
    metadata content, not on phase, so a RUNNING work item with
    ``rework_feedback`` set still produces the rework prompt block
    and the REWORK turn-mode header.
    """

    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.memory = AsyncMock()
        self.memory.build_focused_memory_context = AsyncMock(return_value="")
        self.memory.build_memory_context = AsyncMock(return_value="")
        self.memory.build_agent_memory_context = AsyncMock(return_value="")
        self.assembler = ContextAssembler(memory=self.memory, store=self.store)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def test_infer_turn_mode_returns_rework_when_phase_running_but_feedback_set(
        self,
    ) -> None:
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={
                "rework_feedback": "Missing handoffs/cto_app_architecture.md",
                "review_rework_count": 2,
            },
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_infer_turn_mode_running_with_only_rework_count_is_rework(self) -> None:
        # rework_feedback could be empty if a fix-4 fallback also
        # missed (defensive); review_rework_count > 0 alone still
        # signals a rework attempt.
        wi = _build_wi(
            phase=Phase.RUNNING,
            metadata={"review_rework_count": 1},
        )
        self.assertEqual(infer_turn_mode(wi), TurnMode.REWORK)

    def test_fresh_running_work_item_is_still_execute(self) -> None:
        # Sanity: brand-new work items have neither field; classifier
        # must NOT misfire as REWORK.
        wi = _build_wi(phase=Phase.RUNNING, metadata={})
        self.assertEqual(infer_turn_mode(wi), TurnMode.EXECUTE)

    async def test_build_rework_feedback_context_fires_when_phase_is_running(
        self,
    ) -> None:
        # Mirror the exact sequence: store the work item with phase
        # RUNNING and metadata carrying the reviewer's reject reason.
        wi = _build_wi(
            work_item_id="wi-running-rework",
            phase=Phase.RUNNING,
            role_id="cto",
            metadata={
                "rework_feedback": (
                    "Missing canonical file: trans_app/handoffs/cto_app_architecture.md"
                ),
                "review_rework_count": 3,
                "review_owner_role_id": "ceo",
                "structured_review_verdict": {
                    "label": "reject",
                    "blocking_issues": [
                        "cto_app_architecture.md does not exist under trans_app/handoffs/.",
                    ],
                    "followups": [],
                },
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-running-rework",
            title="Translate app technical plan and implementation",
            description="ship the trans_app scaffold",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
        )
        task = _link_task(task, "wi-running-rework")
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertIn("## Reviewer Feedback (Rework Required)", rendered)
        self.assertIn("Reviewer: ceo", rendered)
        self.assertIn("Rework attempt: #3", rendered)
        self.assertIn("cto_app_architecture.md", rendered)
        self.assertIn("### Blocking Issues", rendered)

    async def test_full_e2e_running_phase_still_yields_rework_sections(self) -> None:
        # build_sections is what the prompt assembler actually calls.
        # Both the turn_mode and rework_feedback sections must be
        # populated even when the work item phase has been flipped to
        # RUNNING by the dispatcher's claim step.
        wi = _build_wi(
            work_item_id="wi-e2e-running",
            phase=Phase.RUNNING,
            role_id="cto",
            metadata={
                "rework_feedback": "REJECT_FOR_TEST: file X is missing",
                "review_rework_count": 1,
                "review_owner_role_id": "ceo",
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-e2e",
            title="t",
            description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
                "delegation_run_id": "r",
                "work_item_role_id": "cto",
            },
        )
        task = _link_task(task, "wi-e2e-running")
        sections = await self.assembler.build_sections(task, role_id="cto")
        self.assertIn("rework", sections["turn_mode"])
        self.assertIn("REJECT_FOR_TEST", sections["rework_feedback"])

    async def test_claimed_gate_harness_attempt_uses_rework_prompt_semantics(
        self,
    ) -> None:
        """Mirror pre-delivery reset -> dispatcher claim -> prompt assembly.

        Controller-owned gate fields live on the Task, while the claimed
        WorkItem is already RUNNING and carries only the incremented attempt.
        This was the exact shape that made run25 look like a fresh EXECUTE
        turn and caused unchanged artifacts to be resubmitted.
        """

        wi = _build_wi(
            work_item_id="wi-gate-attempt-2",
            phase=Phase.RUNNING,
            role_id="investment_analyst",
            metadata={"attempt_seq": 2},
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-gate-attempt-2",
            title="NVDA/AMD/AVGO company-analysis note (JSON)",
            description="write the evidence note",
            assigned_to="investment_analyst",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "execution_mode": "company_mode",
                "delegation_run_id": "run-25-shape",
                "work_item_role_id": "investment_analyst",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_feedback": (
                    "company_analysis.json: critical_claims[0] period_end "
                    "must be in the analysis calendar year"
                ),
                "gate_harness_rework_request": {
                    "rework_round": 1,
                    "feedback": (
                        "company_analysis.json: critical_claims[0] period_end "
                        "must be in the analysis calendar year"
                    ),
                    "blockers": [
                        "Replace the stale 2025 claim with a supported 2026 ended period."
                    ],
                },
            },
        )
        task = _link_task(task, "wi-gate-attempt-2")

        sections = await self.assembler.build_sections(
            task,
            role_id="investment_analyst",
        )

        self.assertIn("Required action: rework", sections["turn_mode"])
        self.assertIn(
            "## Deterministic Gate Feedback (Rework Required)",
            sections["rework_feedback"],
        )
        self.assertIn("Replace the stale 2025 claim", sections["rework_feedback"])
        self.assertIn("Do not claim success", sections["rework_feedback"])
        assembled_context = "\n".join(sections.values())
        self.assertEqual(
            assembled_context.count(
                "## Deterministic Gate Feedback (Rework Required)"
            ),
            1,
        )

    async def test_gate_harness_rework_mode_survives_missing_work_item(self) -> None:
        task = Task(
            id="t-gate-no-wi",
            title="recover deterministic rework",
            description="",
            assigned_to="worker",
            status=TaskStatus.RUNNING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_feedback": "FIX_THIS_GATE_FAILURE",
            },
        )

        rendered = await self.assembler.build_turn_mode_context(task)

        self.assertIn("Required action: rework", rendered)

    async def test_previous_submission_excerpt_is_included(self) -> None:
        wi = _build_wi(
            work_item_id="wi-prev",
            phase=Phase.RUNNING,
            role_id="cto",
            metadata={
                "rework_feedback": "Missing handoff file",
                "review_rework_count": 1,
            },
        )
        await self.store.save_delegation_work_item(wi)
        task = Task(
            id="t-prev",
            title="t",
            description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
            result={
                "content": (
                    "已交付。请以本地目录为准: trans_app/. 入口文件: "
                    "FINAL_DELIVERY_INDEX.md"
                ),
            },
        )
        task = _link_task(task, "wi-prev")
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertIn("### Your Previous Submission", rendered)
        self.assertIn("已交付", rendered)

    async def test_previous_submission_truncated_when_huge(self) -> None:
        wi = _build_wi(
            work_item_id="wi-huge",
            phase=Phase.RUNNING,
            metadata={"rework_feedback": "fix it", "review_rework_count": 1},
        )
        await self.store.save_delegation_work_item(wi)
        big = "X" * 5000
        task = Task(
            id="t-huge",
            title="t",
            description="",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "runtime_model": "multi_team_org",
            },
            result={"content": big},
        )
        task = _link_task(task, "wi-huge")
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertIn("### Your Previous Submission", rendered)
        self.assertIn("… (truncated)", rendered)

    async def test_feedback_falls_back_to_task_metadata_when_store_misses(
        self,
    ) -> None:
        # The dispatcher copies rework_feedback into task.metadata
        # at materialization (company_mode.py:3853-3869). Even if the
        # work item lookup fails (e.g. store gone or linked work item id
        # missing), the task-side copy must still drive the prompt.
        task = Task(
            id="t-no-wi",
            title="t",
            description="",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            project_id="p",
            session_id="s",
            metadata={
                "rework_feedback": "TASK_META_FEEDBACK: redo the X step",
                "review_rework_count": 1,
                "runtime_model": "multi_team_org",
            },
        )
        rendered = await self.assembler.build_rework_feedback_context(task)
        self.assertIn("TASK_META_FEEDBACK", rendered)
        self.assertIn("Rework attempt: #1", rendered)


if __name__ == "__main__":
    unittest.main()
