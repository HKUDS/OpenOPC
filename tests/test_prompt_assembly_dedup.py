"""Tests for the prompt-assembly deduplication refactor.

These tests pin down the structural decisions made in the refactor:

1. Work-item identity (the global_intent_summary / your_responsibility /
   inputs / deliverables / acceptance_criteria / out_of_scope block)
   is rendered exactly ONCE in any per-turn prompt for a company-mode
   role. The runtime plan and ownership contract no longer
   re-render those fields.

2. Static collaboration boilerplate (Company Work Item Contract, the
   Work Item Turn: ... headers, the Blocking collaboration (rare)
   block, the "When to send / When to reply" prose previously baked
   into the comms inbox section) no longer appears in any
   ContextAssembler output. It now lives in the
   ``collaboration-playbook`` skill, injected per-turn through the
   skills channel for company_mode runs only.

3. ``SkillLibrary.build_skills_summary`` honors the per-skill
   ``modes`` frontmatter field: a skill restricted to company_mode
   is fully hidden — body AND description — when the call is made
   without a matching ``execution_mode``.

4. ``render_inbox_section`` no longer emits the "When to send" /
   "When to reply" prose. ``render_meetings_section`` returns "" when
   no meetings are open instead of emitting "(no open meetings)".

The first two tests are the load-bearing ones: if a future refactor
introduces a new builder that re-renders work-item identity or
re-introduces a static collaboration paragraph into the per-turn
prompt, these tests should fail and tell the author to either move
the content into the playbook skill or pick a single canonical
section to own it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.models import SessionMessageRecord, SessionPartRecord
from opc.core.models import Task
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer2_organization import comms as _comms
from opc.layer2_organization.collaboration_policy import render_ownership_contract
from opc.layer5_memory.skill_library import Skill, SkillLibrary


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_STATIC_PHRASES_THAT_BELONG_IN_THE_PLAYBOOK = (
    # Removed from context_assembler._build_work_item_collaboration_guidelines
    "## Company Work Item Contract",
    "## Work Item Turn: Plan / Intake / Dispatch",
    "## Work Item Turn: Execute",
    "## Work Item Turn: Review",
    "## Work Item Turn: Aggregate / Deliver",
    # Removed from build_external_runtime_mailbox_context
    "## Blocking collaboration (rare)",
    # Removed from comms.render_inbox_section
    "### When to send a message",
    "### When to reply to a message you received",
    # Removed from comms.render_meetings_section empty case
    "(no open meetings)",
)


def _work_item_assignment_task(*, role: str = "ceo") -> Task:
    """Build a task with a representative work_item_assignment payload."""
    return Task(
        id=f"task-{role}",
        title="CEO Intake",
        description="legacy description",
        assigned_to=role,
        metadata={
            "execution_mode": "company_mode",
            "work_item_projection_title": "CEO Intake",
            "work_item_turn_type": "intake",
            "work_item_assignment": {
                "global_intent_summary": "Ship the migration to the new auth provider this quarter.",
                "your_responsibility": "Frame the company-wide intake brief and route slices to the C-suite.",
                "out_of_scope": [
                    "Do not implement code in this work item.",
                    "Do not validate gate evidence in this work item.",
                ],
                "inputs": [
                    "Use the original user request as the mission baseline.",
                ],
                "deliverables": [
                    "deliverables/ceo_intake.md with Mission Summary, Scope, Risks, Routing, Acceptance",
                ],
                "acceptance_criteria": [
                    "Brief covers every required section and stays inside the work-item boundary.",
                ],
            },
            "work_item_runtime_plan": {
                "turn_type": "intake",
                "summary": "Frame the company mission and route slices.",
                # The next four are intentionally duplicates of the
                # work_item_assignment fields above. After the refactor
                # they MUST NOT be rendered by _render_work_item_runtime_plan.
                "inputs": ["Use the original user request as the mission baseline."],
                "deliverables": ["deliverables/ceo_intake.md ..."],
                "acceptance_criteria": ["Brief covers every required section ..."],
                "out_of_scope": ["Do not implement code in this work item."],
                # Work-item-unique field that the runtime plan IS allowed
                # to carry. Used to verify the runtime plan still
                # surfaces non-duplicate content.
                "collaboration_expectations": [
                    "Direct messages for clarifications; meetings only for cross-role decisions."
                ],
                "verification_required": False,
            },
            "ownership_contract": {
                # The summary and expected_artifacts fields are
                # intentionally duplicates of the work_item_assignment
                # responsibility/deliverables. After the refactor
                # render_ownership_contract MUST NOT render them.
                "summary": "Frame the company-wide intake brief and route slices to the C-suite.",
                "expected_artifacts": ["deliverables/ceo_intake.md ..."],
                # The boundary fields below SHOULD still be rendered.
                "write_scope": "/tmp/ceo-intake-workspace",
                "allowed_collaboration_targets": ["cto"],
                "downstream_consumer": ["cto"],
            },
        },
    )


# ----------------------------------------------------------------------
# Bucket 1: work-item identity is rendered exactly once
# ----------------------------------------------------------------------


class WorkItemIdentityDeduplicationTests(unittest.TestCase):
    def test_work_item_runtime_plan_does_not_re_render_work_item_assignment_fields(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        task = _work_item_assignment_task()

        rendered = assembler._render_work_item_runtime_plan(task.metadata["work_item_runtime_plan"])

        # Headings that used to come out of work_item_runtime_plan are
        # now ONLY produced by company_mode._build_work_item_description /
        # ContextAssembler.build_task_brief. The runtime plan
        # renderer must not re-emit them.
        self.assertNotIn("Inputs:", rendered)
        self.assertNotIn("Deliverables:", rendered)
        self.assertNotIn("Acceptance Criteria:", rendered)
        self.assertNotIn("Out of Scope:", rendered)
        # Work-item-unique fields are still rendered.
        self.assertIn("Collaboration Expectations:", rendered)
        self.assertIn("Direct messages for clarifications", rendered)
        self.assertIn("Work item turn type: intake", rendered)
        self.assertIn("No automatic verification pass is required", rendered)

    def test_render_ownership_contract_omits_summary_and_expected_artifacts(self) -> None:
        task = _work_item_assignment_task()

        rendered = render_ownership_contract(task)

        # The boundary header still appears...
        self.assertIn("## Ownership Contract", rendered)
        self.assertIn("Write scope: /tmp/ceo-intake-workspace", rendered)
        self.assertIn("Allowed collaboration targets:", rendered)
        self.assertIn("Downstream consumers:", rendered)
        # ...but the summary and expected_artifacts strings (which
        # are duplicates of work-item identity) MUST NOT.
        self.assertNotIn("Frame the company-wide intake brief", rendered)
        self.assertNotIn("Expected artifacts:", rendered)
        self.assertNotIn("deliverables/ceo_intake.md ...", rendered)

    def test_build_task_brief_renders_projection_identity_once(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        task = _work_item_assignment_task()

        brief = assembler.build_task_brief(task)

        # Work-item identity headers appear exactly once.
        self.assertEqual(brief.count("## Global Intent Summary"), 1)
        self.assertEqual(brief.count("## Your Responsibility"), 1)
        self.assertEqual(brief.count("## Inputs"), 1)
        self.assertEqual(brief.count("## Deliverables"), 1)
        self.assertEqual(brief.count("## Acceptance Criteria"), 1)
        self.assertEqual(brief.count("## Out of Scope"), 1)
        # The literal responsibility text from work_item_assignment also
        # appears exactly once across the entire brief.
        self.assertEqual(
            brief.count("Frame the company-wide intake brief and route slices to the C-suite."),
            1,
        )
        # runtime role map is no longer included in build_task_brief
        # — it was superseded by the unified Topology section in the
        # collaboration context path. Pinning this down so a future
        # refactor doesn't accidentally re-introduce it.
        self.assertNotIn("## Runtime Role Map", brief)
        # Ownership contract is also no longer included in
        # build_task_brief — it lives only in the collaboration
        # path now, so the brief and the system context don't
        # double-render it for native agents.
        self.assertNotIn("## Ownership Contract", brief)


# ----------------------------------------------------------------------
# Bootstrap work-item assignment input/acceptance deduplication
# ----------------------------------------------------------------------


class RuntimeWorkItemAcceptanceDedupTests(unittest.TestCase):
    """Pin down generic acceptance criteria deduplication."""

    def _planner(self) -> Any:
        from opc.layer2_organization.company_mode import CompanyRuntimeSpecBuilder

        class _OrgEngine:
            def get_agent(self, role_id: str):
                return None

        return CompanyRuntimeSpecBuilder(_OrgEngine())

    def _projection_spec(self, *, dependencies: list[str]) -> Any:
        from opc.layer2_organization.org_work_item_planner import WorkItemProjectionSpec

        return WorkItemProjectionSpec(
            projection_id="cto_synthesis",
            turn_type="aggregate",
            title="CTO Synthesis",
            summary="Review child outputs and synthesize the engineering handoff.",
            role_id="cto",
            dependency_projection_ids=list(dependencies),
            execution_strategy="auto",
        )

    def test_generic_acceptance_criteria_omit_main_deliverable_echo(self) -> None:
        planner = self._planner()
        projection_spec = self._projection_spec(dependencies=["senior_engineer__execute"])
        deliverables = [
            "A concrete output that fulfills this work item objective and can be handed off downstream.",
        ]

        criteria = planner._infer_work_item_acceptance(projection_spec, deliverables)

        self.assertFalse(
            any(item.startswith("The main deliverable is present:") for item in criteria),
            msg=f"Generic acceptance criteria still echo the first deliverable verbatim: {criteria}",
        )
        self.assertNotIn(deliverables[0], "\n".join(criteria))


# ----------------------------------------------------------------------
# Bucket 2: Self section consolidation
# ----------------------------------------------------------------------


class SelfSectionConsolidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_section_merges_identity_persona_and_delta(self) -> None:
        memory = SimpleNamespace(
            build_agent_memory_context=AsyncMock(return_value=""),
            build_memory_context=AsyncMock(return_value=""),
        )
        assembler = ContextAssembler(memory=memory, store=SimpleNamespace())
        task = Task(
            id="self-section-task",
            title="Engineering",
            assigned_to="senior_engineer",
            metadata={
                "execution_mode": "company_mode",
                "employee_assignment": {
                    "name": "Backend Architect",
                    "employee_id": "backend-architect",
                    "role_id": "senior_engineer",
                    "category": "engineering",
                    "domains": ["coding", "api"],
                    "experience_score": 5,
                },
                "employee_prompt_context": "Backend specialist focused on APIs.",
                "employee_delta_context": "## Working Patterns\n- Leave reviewer-friendly artifacts.",
            },
        )

        rendered = await assembler.build_role_reference_context(task, role_id="senior_engineer")

        # Single canonical Self section with role, employee, and profile sub-blocks.
        self.assertIn("## Self", rendered)
        self.assertIn("### Role", rendered)
        self.assertIn("- Role: senior_engineer", rendered)
        self.assertIn("### Employee", rendered)
        self.assertIn("- Employee: Backend Architect", rendered)
        self.assertIn("### Employee Persona", rendered)
        self.assertIn("Backend specialist focused on APIs.", rendered)
        self.assertIn("### Learned Working Profile", rendered)
        # The legacy independent headers must NOT be present.
        self.assertNotIn("## Assigned Employee", rendered)
        self.assertNotRegex(rendered, r"(?m)^## Employee Persona$")
        self.assertNotIn("## Employee Delta Profile", rendered)


# ----------------------------------------------------------------------
# Static collaboration boilerplate is gone from per-turn prompt
# ----------------------------------------------------------------------


class StaticCollaborationBoilerplateRemovedTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_static_collaboration_phrases_in_external_context(self) -> None:
        memory = SimpleNamespace(
            build_focused_memory_context=AsyncMock(return_value=""),
            build_memory_context=AsyncMock(return_value=""),
            build_agent_memory_context=AsyncMock(return_value=""),
        )
        # We do not need a real CommunicationManager — just enough
        # surface for build_collaboration_context to short-circuit.
        communication = SimpleNamespace(
            build_agent_context=AsyncMock(
                return_value={
                    "inbox": [],
                    "annotations": [],
                    "pending_handoffs": [],
                    "allowed_contacts": [
                        {
                            "role_id": "cto",
                            "name": "CTO",
                            "relation": "downstream",
                            "responsibility": "Owns technical execution",
                        }
                    ],
                }
            )
        )
        assembler = ContextAssembler(memory=memory, store=None, communication=communication)
        task = _work_item_assignment_task()

        external_ctx = await assembler.build_external_context(task, role_id="ceo")

        for phrase in _STATIC_PHRASES_THAT_BELONG_IN_THE_PLAYBOOK:
            self.assertNotIn(
                phrase,
                external_ctx,
                msg=f"Static phrase {phrase!r} should now live in the collaboration-playbook skill, not in per-turn context",
            )

    async def test_task_mode_external_context_omits_collaboration_mailbox_and_comms_prompting(self) -> None:
        memory = SimpleNamespace(
            build_focused_memory_context=AsyncMock(return_value=""),
            build_memory_context=AsyncMock(return_value=""),
            build_agent_memory_context=AsyncMock(return_value=""),
        )
        assembler = ContextAssembler(
            memory=memory,
            store=None,
            communication=SimpleNamespace(build_agent_context=AsyncMock(return_value={})),
        )
        task = Task(
            id="task-mode-external",
            title="Task Mode External",
            description="Handle the task directly.",
            assigned_to="task_generalist",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "work_item_role_id": "task_generalist",
            },
            context_snapshot={},
        )

        external_ctx = await assembler.build_external_context(task, role_id="task_generalist")

        self.assertNotIn("## Continue After Blocking Collaboration", external_ctx)
        self.assertNotIn("## External Collaboration Actions", external_ctx)
        self.assertNotIn("## Comms", external_ctx)

    def test_render_inbox_section_omits_when_to_send_prose(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            layout = _comms.resolve_layout(tmpdir, "proj1", "sess1")
            _comms.ensure_layout(layout, ["ceo", "cto"])
            rendered = _comms.render_inbox_section(layout, "ceo")
        self.assertIn("### Mailbox", rendered)
        self.assertIn("runtime-owned", rendered)
        # The standing rules now live in the playbook skill.
        self.assertNotIn("### When to send a message", rendered)
        self.assertNotIn("### When to reply to a message you received", rendered)
        self.assertNotIn("Send ONLY when", rendered)
        self.assertNotIn("Reply ONLY when", rendered)
        self.assertNotIn("call `read_inbox`", rendered)

    def test_render_meetings_section_returns_empty_when_no_meetings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            layout = _comms.resolve_layout(tmpdir, "proj1", "sess1")
            _comms.ensure_layout(layout, ["ceo"])
            rendered = _comms.render_meetings_section(layout, "ceo")
        self.assertEqual(rendered, "")


# ----------------------------------------------------------------------
# Current-turn replay is omitted from prompt history
# ----------------------------------------------------------------------


class PromptHistoryReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_prompt_builders_skip_latest_user_turn_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryManager(Path(tmpdir))
            earlier_user = SessionMessageRecord(session_id="sess-1", role="user")
            assistant = SessionMessageRecord(session_id="sess-1", role="assistant")
            current_user = SessionMessageRecord(session_id="sess-1", role="user")
            visible_items = [
                {
                    "message": earlier_user,
                    "parts": [SessionPartRecord(message_id=earlier_user.message_id, session_id="sess-1", payload={"text": "first request"})],
                },
                {
                    "message": assistant,
                    "parts": [SessionPartRecord(message_id=assistant.message_id, session_id="sess-1", payload={"text": "assistant reply"})],
                },
                {
                    "message": current_user,
                    "parts": [SessionPartRecord(message_id=current_user.message_id, session_id="sess-1", payload={"text": "current request"})],
                },
            ]
            memory._get_visible_session_transcript = AsyncMock(return_value=visible_items)  # type: ignore[method-assign]
            memory.build_session_memory_context = AsyncMock(return_value="")  # type: ignore[method-assign]

            full_history = await memory.build_session_history_messages("sess-1")
            trimmed_history = await memory.build_session_history_tail_messages(
                "sess-1",
                include_latest_user_turn=False,
            )
            prompt_context = await memory.build_session_prompt_context(
                "sess-1",
                include_latest_user_turn=False,
            )

            self.assertEqual(len(full_history), 3)
            self.assertEqual(len(trimmed_history), 2)
            self.assertIn("first request", trimmed_history[0]["content"])
            self.assertIn("assistant reply", trimmed_history[1]["content"])
            self.assertNotIn("current request", prompt_context)
            self.assertIn("first request", prompt_context)
            self.assertIn("assistant reply", prompt_context)

    async def test_child_session_seed_message_is_omitted_from_prompt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryManager(Path(tmpdir))
            seed_user = SessionMessageRecord(
                session_id="sess-2",
                role="user",
                metadata={"kind": "child_session_seed"},
            )
            assistant = SessionMessageRecord(session_id="sess-2", role="assistant")
            followup_user = SessionMessageRecord(session_id="sess-2", role="user")
            visible_items = [
                {
                    "message": seed_user,
                    "parts": [
                        SessionPartRecord(
                            message_id=seed_user.message_id,
                            session_id="sess-2",
                            payload={
                                "text": "## Global Intent Summary\n做一个3d坦克大战游戏\n\n## Your Responsibility\nOwn the execution work item for role engineer."
                            },
                        )
                    ],
                },
                {
                    "message": assistant,
                    "parts": [SessionPartRecord(message_id=assistant.message_id, session_id="sess-2", payload={"text": "working on it"})],
                },
                {
                    "message": followup_user,
                    "parts": [SessionPartRecord(message_id=followup_user.message_id, session_id="sess-2", payload={"text": "please keep the game browser-friendly"})],
                },
            ]
            memory._get_visible_session_transcript = AsyncMock(return_value=visible_items)  # type: ignore[method-assign]
            memory.build_session_memory_context = AsyncMock(return_value="")  # type: ignore[method-assign]

            prompt_context = await memory.build_session_prompt_context(
                "sess-2",
                include_latest_user_turn=False,
            )
            history_messages = await memory.build_session_history_messages(
                "sess-2",
                include_latest_user_turn=False,
            )

            self.assertNotIn("Your Responsibility", prompt_context)
            self.assertNotIn("Global Intent Summary", prompt_context)
            self.assertEqual(len(history_messages), 1)
            self.assertIn("working on it", history_messages[0]["content"])
            self.assertIn("working on it", prompt_context)


# ----------------------------------------------------------------------
# SkillLibrary mode filtering and the playbook skill
# ----------------------------------------------------------------------


class SkillModeFilteringTests(unittest.TestCase):
    def _make_library(self, *, with_playbook: bool = True) -> SkillLibrary:
        tmpdir = Path(tempfile.mkdtemp(prefix="opc-skill-test-"))
        skills_dir = tmpdir / "skills"
        skills_dir.mkdir()
        if with_playbook:
            playbook_dir = skills_dir / "collaboration-playbook"
            playbook_dir.mkdir()
            (playbook_dir / "SKILL.md").write_text(
                "---\n"
                "name: collaboration-playbook\n"
                "description: Standing rules for company-mode collaboration.\n"
                "always: true\n"
                "modes:\n"
                "  - company_mode\n"
                "---\n\n"
                "# Collaboration Playbook\n\n"
                "Stay inside your work-item boundary.\n"
            )
        memory_dir = skills_dir / "memory"
        memory_dir.mkdir()
        (memory_dir / "SKILL.md").write_text(
            "---\n"
            "name: memory\n"
            "description: Read and edit canonical OpenOPC memory.\n"
            "always: true\n"
            "---\n\n"
            "# Memory\n\n"
            "Use `.opc/memory/global.md` and `.opc/memory/projects/<current_project_id>.md`.\n"
        )
        # Also create one ordinary skill that should always show.
        ordinary_dir = skills_dir / "writing"
        ordinary_dir.mkdir()
        (ordinary_dir / "SKILL.md").write_text(
            "---\n"
            "name: writing\n"
            "description: Helpful writing patterns.\n"
            "---\n\n"
            "# Writing\n"
        )
        library = SkillLibrary(tmpdir)
        library.load_all()
        return library

    def test_company_mode_skill_is_visible_under_company_mode(self) -> None:
        library = self._make_library()
        summary = library.build_skills_summary(execution_mode="company_mode")
        self.assertIn("## Skill: collaboration-playbook", summary)
        self.assertIn("Stay inside your work-item boundary.", summary)
        # Ordinary skill is also catalogued.
        self.assertIn("**writing**", summary)

    def test_company_mode_skill_is_completely_hidden_in_task_mode(self) -> None:
        library = self._make_library()
        summary = library.build_skills_summary(execution_mode="task_mode")
        # Body must be gone.
        self.assertNotIn("Stay inside your work-item boundary.", summary)
        self.assertNotIn("## Skill: collaboration-playbook", summary)
        # Description must ALSO be gone — even the catalog summary
        # entry must not appear in task mode.
        self.assertNotIn("collaboration-playbook", summary)
        self.assertNotIn(
            "Standing rules for company-mode collaboration.", summary
        )
        # The ordinary skill is unaffected.
        self.assertIn("**writing**", summary)
        self.assertIn("## Skill: memory", summary)

    def test_company_mode_skill_is_hidden_when_execution_mode_unknown(self) -> None:
        library = self._make_library()
        summary = library.build_skills_summary(execution_mode=None)
        self.assertNotIn("collaboration-playbook", summary)
        self.assertIn("**writing**", summary)
        self.assertNotIn("## Skill: memory", summary)

    def test_memory_skill_visible_in_task_mode_for_all_agents(self) -> None:
        library = self._make_library()
        summary = library.build_skills_summary(
            execution_mode="task_mode",
            role_id="worker",
            user_facing=False,
            final_decider_role_id=None,
        )
        self.assertIn("## Skill: memory", summary)

    def test_memory_skill_visible_only_to_user_facing_final_decider_in_company_mode(self) -> None:
        library = self._make_library()
        worker_summary = library.build_skills_summary(
            execution_mode="company_mode",
            role_id="worker",
            user_facing=True,
            final_decider_role_id="ceo",
        )
        agent_turn_summary = library.build_skills_summary(
            execution_mode="company_mode",
            role_id="ceo",
            user_facing=False,
            final_decider_role_id="ceo",
        )
        final_summary = library.build_skills_summary(
            execution_mode="company_mode",
            role_id="ceo",
            user_facing=True,
            final_decider_role_id="ceo",
        )

        self.assertNotIn("## Skill: memory", worker_summary)
        self.assertNotIn("## Skill: memory", agent_turn_summary)
        self.assertIn("## Skill: memory", final_summary)

    def test_skill_visibility_helper_handles_modes_field_shapes(self) -> None:
        # Backward compat: skill with no modes is visible everywhere.
        skill_no_modes = Skill(name="x", modes=[])
        self.assertTrue(SkillLibrary._skill_visible_in_mode(skill_no_modes, None))
        self.assertTrue(SkillLibrary._skill_visible_in_mode(skill_no_modes, "company_mode"))
        self.assertTrue(SkillLibrary._skill_visible_in_mode(skill_no_modes, "task_mode"))
        # Restricted skill is hidden everywhere except declared modes.
        skill_company = Skill(name="y", modes=["company_mode"])
        self.assertFalse(SkillLibrary._skill_visible_in_mode(skill_company, None))
        self.assertFalse(SkillLibrary._skill_visible_in_mode(skill_company, ""))
        self.assertFalse(SkillLibrary._skill_visible_in_mode(skill_company, "task_mode"))
        self.assertTrue(SkillLibrary._skill_visible_in_mode(skill_company, "company_mode"))
        skill_memory = Skill(name="memory", modes=[])
        self.assertTrue(SkillLibrary._skill_visible_in_mode(skill_memory, "task_mode"))
        self.assertFalse(SkillLibrary._skill_visible_in_mode(skill_memory, "company_mode"))
        self.assertTrue(
            SkillLibrary._skill_visible_in_mode(
                skill_memory,
                "company_mode",
                role_id="ceo",
                user_facing=True,
                final_decider_role_id="ceo",
            )
        )


# ----------------------------------------------------------------------
# Real bundled collaboration-playbook skill
# ----------------------------------------------------------------------


class BundledPlaybookSkillTests(unittest.TestCase):
    """Sanity-check the playbook shipped under .opc/skills/."""

    @classmethod
    def setUpClass(cls) -> None:
        # tests/ -> repo root
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.skill_path = cls.repo_root / ".opc" / "skills" / "collaboration-playbook" / "SKILL.md"

    def test_playbook_file_exists_with_company_mode_only_frontmatter(self) -> None:
        self.assertTrue(
            self.skill_path.exists(),
            msg=f"Expected collaboration playbook at {self.skill_path}",
        )
        text = self.skill_path.read_text(encoding="utf-8")
        self.assertIn("name: collaboration-playbook", text)
        self.assertIn("always: true", text)
        # The mode restriction is what makes the skill default-true
        # in company mode and invisible in task mode.
        self.assertIn("modes:", text)
        self.assertIn("- company_mode", text)
        self.assertNotIn("Prefer direct execution", text)

    def test_bundled_playbook_loads_through_skill_library(self) -> None:
        library = SkillLibrary(self.repo_root / ".opc")
        library.load_all()
        skill = library.get("collaboration-playbook")
        self.assertIsNotNone(skill)
        assert skill is not None
        self.assertTrue(skill.always)
        self.assertEqual(skill.modes, ["company_mode"])
        self.assertIn("Stay inside your work-item boundary.", skill.content)

        company_summary = library.build_skills_summary(execution_mode="company_mode")
        self.assertIn("## Skill: collaboration-playbook", company_summary)

        task_summary = library.build_skills_summary(execution_mode="task_mode")
        self.assertNotIn("collaboration-playbook", task_summary)


if __name__ == "__main__":
    unittest.main()
