"""Regression tests for the round-3 fixes.

Fix 4 — reviewer prompt strengthening + adapter-level structural
validation. The strengthened prompt should carry an explicit schema
block + a concrete good example; the adapter should refuse to propagate
a structurally-empty reject verdict, so it falls through to the keyword
fallback (giving Fix 1's runtime retry a better signal to work with).

Fix 6 — phase-transition hook that nulls stale ``focused_work_item_id``
on role_runtime_sessions when their work item reaches a terminal phase.
Addresses the app13 pattern where two leader sessions held
``status=blocked`` with focus on APPROVED items for ~30 minutes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.models import DelegationWorkItem, Phase, RoleRuntimeSession, Task
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401  (register hooks)
from opc.layer3_agent.adapters.base import ExternalAgentAdapter
from opc.layer3_agent import company_runtime_contract
from opc.layer3_agent import native_agent


# ── Fix 4 structural validator removed: the adapter no longer drops
# verdicts based on shape. infer_review_verdict is now a pure JSON
# extractor; if the verdict can't be parsed, the runtime spawns one
# verdict-parse-retry attempt (see test_verdict_parse_retry.py) before
# escalating to AWAITING_HUMAN. ────────────────────────────────────────


class InferReviewVerdictAdapterIntegrationTests(unittest.TestCase):
    """Integration at the adapter level: feed raw output through
    ``ExternalAgentAdapter.infer_review_verdict`` and assert that a
    structurally-empty reject verdict does NOT propagate as a "valid"
    parsed dict — it drops to the keyword fallback instead."""

    class _StubAdapter(ExternalAgentAdapter):
        agent_type = "stub"

        async def is_available(self) -> bool:  # pragma: no cover
            return True

        async def execute(self, task, workspace_path):  # pragma: no cover
            raise NotImplementedError

        def build_invocation(self, task, workspace_path=None):  # pragma: no cover
            return [], {}

        async def get_status(self):  # pragma: no cover
            from opc.core.models import AgentStatus
            return AgentStatus(agent="stub", available=True)

    def setUp(self) -> None:
        self.adapter = self._StubAdapter(_config_stub())

    def test_explicit_valid_reject_json_propagates(self) -> None:
        raw = (
            '{"review_verdict":"reject",'
            '"summary":"Missing handoff and validation fails on two checks",'
            '"blocking_issues":["Create handoff.md with architecture summary"],'
            '"followups":["Add integration test later"]}'
        )
        verdict = self.adapter.infer_review_verdict(raw)
        self.assertEqual(verdict.get("label"), "reject")
        self.assertEqual(len(verdict.get("blocking_issues", [])), 1)
        self.assertIn("Missing handoff", verdict.get("summary", ""))

    def test_explicit_valid_approve_json_propagates(self) -> None:
        raw = '{"review_verdict":"approve","summary":"Meets acceptance bar; tests pass"}'
        verdict = self.adapter.infer_review_verdict(raw)
        self.assertEqual(verdict.get("label"), "approve")

    def test_empty_reject_json_propagates_unchanged(self) -> None:
        # The adapter is no longer in the business of inspecting verdict
        # shape. An empty-reject JSON propagates AS-IS; the runtime side
        # decides whether to act on it. (The runtime applies it
        # mechanically — the reviewer was trusted to produce it.)
        raw = '{"review_verdict":"reject","summary":"reject","blocking_issues":[],"followups":[]}'
        verdict = self.adapter.infer_review_verdict(raw)
        self.assertEqual(verdict.get("label"), "reject")
        self.assertEqual(verdict.get("blocking_issues"), [])
        self.assertEqual(verdict.get("followups"), [])

    def test_unparseable_output_returns_empty(self) -> None:
        # Without a structured verdict block, the adapter no longer
        # falls back to keyword scanning the prose. Returns {} so the
        # runtime can spawn a verdict-parse-retry attempt instead.
        raw = "The deliverable looks fine; ship it."
        verdict = self.adapter.infer_review_verdict(raw)
        self.assertEqual(verdict, {})


class ReviewPromptSchemaTests(unittest.TestCase):
    """The review prompts must carry the *suggested* JSON shape so
    agents know what the runtime can parse, but must NOT carry the
    old hard-rule wording (auto-reject, min char counts, mandatory
    schema). The runtime is mechanical now; the reviewer is trusted.
    """

    def test_company_review_prompt_keeps_suggested_schema(self) -> None:
        text = company_runtime_contract._COMPANY_REVIEW_WORK_ITEM_GUIDELINES
        self.assertIn("review_verdict", text)
        self.assertIn("blocking_issues", text)
        self.assertIn("followups", text)
        self.assertNotIn("Mandatory verdict schema", text)
        self.assertNotIn("auto-rejected", text.lower())
        self.assertNotIn("min 30 chars", text.lower())

    def test_kanban_review_turn_header_keeps_suggested_schema(self) -> None:
        text = company_runtime_contract._REVIEW_EXECUTE_HEADER
        self.assertIn("review_verdict", text)
        self.assertIn("blocking_issues", text)
        self.assertNotIn("Mandatory verdict schema", text)
        self.assertNotIn("auto-rejected", text.lower())

    def test_review_pending_header_keeps_suggested_schema(self) -> None:
        text = company_runtime_contract._REVIEW_PENDING_HEADER
        self.assertIn("review_verdict", text)
        self.assertIn("blocking_issues", text)
        self.assertNotIn("Mandatory verdict schema", text)
        self.assertNotIn("auto-rejected", text.lower())

    def test_native_agent_prompt_keeps_suggested_schema(self) -> None:
        # The guidelines live in company_runtime_contract; the native agent
        # injects them through build_company_work_item_contract, so assert
        # through the wiring the native agent actually uses.
        task = Task(
            id="review-task",
            title="Review work item",
            metadata={"work_item_turn_type": "review"},
        )
        text = native_agent.build_company_work_item_contract(task)
        self.assertIn("review_verdict", text)
        self.assertIn("blocking_issues", text)
        self.assertNotIn("Mandatory verdict schema", text)
        self.assertNotIn("Rejected at ingestion", text)

    def test_company_report_generation_header_present(self) -> None:
        # The new two-turn worker→review handoff: there must be a
        # report-generation prompt block the runtime can inject when
        # the worker is spawned for the post-execute report turn.
        text = company_runtime_contract._COMPANY_REPORT_GENERATION_HEADER
        self.assertIn("Report Generation", text)
        self.assertIn("deliverables", text)
        self.assertIn("acceptance_status", text)


# ── Fix 6: clear session focus on terminal phase ─────────────────────────


def _session(
    *,
    role_session_id: str,
    run_id: str,
    role_id: str = "cto",
    focused: str = "",
    status: str = "idle",
) -> RoleRuntimeSession:
    return RoleRuntimeSession(
        role_session_id=role_session_id,
        run_id=run_id,
        team_instance_id="ti",
        team_id="team::x",
        role_id=role_id,
        seat_id="s",
        focused_work_item_id=focused,
        status=status,
    )


class ClearSessionFocusOnTerminalHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def _seed_work_and_session(
        self,
        *,
        work_item_id: str = "wi-1",
        run_id: str = "run-1",
        session_focus_matches: bool = True,
        session_status: str = "blocked",
        session_role_id: str = "cto",
    ) -> tuple[DelegationWorkItem, RoleRuntimeSession]:
        wi = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id=run_id, cell_id="c",
            team_instance_id="ti",
            team_id="team::x",
            role_id=session_role_id,
            seat_id="s",
            manager_role_id="m",
            manager_seat_id="ms",
            title="t",
            phase=Phase.RUNNING,
        )
        await self.store.save_delegation_work_item(wi)
        sess = _session(
            role_session_id=f"role-runtime::{run_id}::ti::{session_role_id}",
            run_id=run_id,
            role_id=session_role_id,
            focused=work_item_id if session_focus_matches else "other-wi",
            status=session_status,
        )
        await self.store.save_delegation_role_session(sess)
        return wi, sess

    async def test_approving_work_clears_focused_and_flips_blocked_to_idle(self) -> None:
        """The app13 pattern: session is blocked on a work item that now
        reaches APPROVED. Hook must clear focus + flip blocked→idle."""
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=True, session_status="blocked",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.APPROVED)

        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        self.assertEqual(refreshed.focused_work_item_id, "")
        self.assertEqual(refreshed.status, "idle")

    async def test_failing_work_also_clears_focus(self) -> None:
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=True, session_status="blocked",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.RUNNING)
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.FAILED)
        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        self.assertEqual(refreshed.focused_work_item_id, "")

    async def test_cancelling_work_clears_focus(self) -> None:
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=True, session_status="blocked",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.CANCELLED)
        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        self.assertEqual(refreshed.focused_work_item_id, "")

    async def test_running_session_becomes_idle_when_terminal_clears_focus(self) -> None:
        """Three-state role sessions cannot remain running without focus."""
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=True, session_status="running",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.APPROVED)
        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        self.assertEqual(refreshed.focused_work_item_id, "")
        self.assertEqual(refreshed.status, "idle")

    async def test_non_matching_session_untouched(self) -> None:
        """Hook must only rewrite sessions focused on THIS work item."""
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=False, session_status="blocked",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.APPROVED)
        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        # Session pointing at some other work item must stay intact.
        self.assertEqual(refreshed.focused_work_item_id, "other-wi")
        self.assertEqual(refreshed.status, "blocked")

    async def test_awaiting_human_does_not_clear(self) -> None:
        """AWAITING_HUMAN is an escalation, not a terminal. The session
        is legitimately still focused on the item until the human acts.
        Hook must NOT clear focus on that transition."""
        wi, sess = await self._seed_work_and_session(
            session_focus_matches=True, session_status="blocked",
        )
        await self.store.update_delegation_work_item(wi.work_item_id, phase=Phase.RUNNING)
        await self.store.update_delegation_work_item(
            wi.work_item_id, phase=Phase.AWAITING_MANAGER_REVIEW
        )
        await self.store.update_delegation_work_item(
            wi.work_item_id, phase=Phase.AWAITING_HUMAN
        )
        refreshed = await self.store.get_delegation_role_session(sess.role_session_id)
        self.assertEqual(refreshed.focused_work_item_id, wi.work_item_id)
        self.assertEqual(refreshed.status, "blocked")


def _config_stub():
    from opc.core.config import ExternalAgentConfig

    return ExternalAgentConfig(
        agent_type="stub",
        binary="stub",
        extra_args=[],
    )


if __name__ == "__main__":
    unittest.main()
