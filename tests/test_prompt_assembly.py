from __future__ import annotations

from pathlib import Path
import unittest

from opc.core.config import OPCConfig
from opc.core.models import AgentInfo, Task
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer2_organization.talent_market import resolve_prompt_refs
from opc.layer3_agent.native_agent import PromptProfileManager


def _make_pm(*, role_prompts: list[str]) -> PromptProfileManager:
    role = AgentInfo(
        role_id="cmo",
        name="CMO",
        responsibility="Marketing strategy and brand oversight.",
        prompt_refs=role_prompts,
    )
    return PromptProfileManager(role=role, config=OPCConfig())


def _make_assembler() -> ContextAssembler:
    return ContextAssembler(memory=None, store=None, communication=None)


def _base_prompt(prompt: str) -> str:
    addendum_headers = [
        "\n\n## Task Tracking",
        "\n\n## Company Work Item Contract",
        "\n\n## Runtime Clock",
        "\n\n## Organization Runtime Contract",
        "\n\n## Work Item Turn: Report Generation",
        "\n\n## Kanban Review Turn",
        "\n\n## Review Requirement",
        "\n\n## Task-Mode Orchestration",
        "\n\n## Role Operating Instructions",
        "\n\n## Runtime Profile Override",
    ]
    indexes = [prompt.find(header) for header in addendum_headers if prompt.find(header) >= 0]
    base = prompt[:min(indexes)] if indexes else prompt
    return base.rstrip()


def _base_prompt_contract(prompt: str) -> str:
    prompt = _base_prompt(prompt)
    marker = "\n\n## Core Operating Principles"
    index = prompt.find(marker)
    return prompt[index:].strip() if index >= 0 else _base_prompt(prompt)


class RoleOperatingInstructionsSectionTest(unittest.TestCase):
    def test_role_prompt_appears_under_role_operating_instructions_section(self) -> None:
        pm = _make_pm(role_prompts=["Optimize for audience fit and brand consistency."])
        task = Task(title="Plan launch")
        _, prompt = pm.build_prompt(task)
        self.assertIn(
            "## Role Operating Instructions\nOptimize for audience fit and brand consistency.",
            prompt,
        )

    def test_multiple_role_prompts_joined_with_blank_line(self) -> None:
        pm = _make_pm(role_prompts=["First directive.", "Second directive."])
        task = Task(title="Plan launch")
        _, prompt = pm.build_prompt(task)
        self.assertIn(
            "## Role Operating Instructions\nFirst directive.\n\nSecond directive.",
            prompt,
        )

    def test_empty_role_prompts_omits_section(self) -> None:
        pm = _make_pm(role_prompts=[])
        task = Task(title="Plan launch")
        self.assertNotIn("## Role Operating Instructions", pm.build_prompt(task)[1])


class UnifiedNativePromptTest(unittest.TestCase):
    def test_prompt_profile_metadata_no_longer_changes_base_contract(self) -> None:
        pm = _make_pm(role_prompts=[])
        cases = [
            Task(title="Plan launch", metadata={"prompt_profile": "plan"}),
            Task(title="Review launch", metadata={"prompt_profile": "review"}),
            Task(title="Verify launch", metadata={"subagent_profile": "verify"}),
            Task(title="Draft launch", metadata={"_subagent_mode": "plan"}),
            Task(title="Execute launch", metadata={"prompt_profile": "coding"}),
        ]

        base_prompt = ""
        for task in cases:
            profile, prompt = pm.build_prompt(task)
            self.assertEqual(profile, "unified")
            self.assertIn("## Core Operating Principles", prompt)
            self.assertNotIn("## Core Beliefs", prompt)
            self.assertNotIn("because it's worth it", prompt)
            self.assertNotIn("out of love for completeness", prompt)
            self.assertIn("## Native Working Contract", prompt)
            self.assertNotIn("## Plan Profile", prompt)
            self.assertNotIn("## Planning Principles", prompt)
            self.assertNotIn("## Review Principles", prompt)
            self.assertNotIn("## Verification Contract", prompt)
            current_base = _base_prompt(prompt)
            if not base_prompt:
                base_prompt = current_base
            self.assertEqual(current_base, base_prompt)

    def test_company_and_task_mode_share_unified_base_with_context_addenda(self) -> None:
        pm = _make_pm(role_prompts=[])
        _, task_prompt = pm.build_prompt(
            Task(title="Task turn", metadata={"mode": "task", "execution_mode": "task_mode"})
        )
        _, company_prompt = pm.build_prompt(
            Task(
                title="Review turn",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_turn_type": "review",
                    "work_item_projection_title": "Review turn",
                },
            )
        )

        self.assertNotEqual(_base_prompt(task_prompt).split("\n\n", 1)[0], _base_prompt(company_prompt).split("\n\n", 1)[0])
        self.assertEqual(_base_prompt_contract(task_prompt), _base_prompt_contract(company_prompt))
        self.assertIn("## Task-Mode Orchestration", task_prompt)
        self.assertNotIn("## Task-Mode Orchestration", company_prompt)
        self.assertIn("## Company Work Item Contract", company_prompt)

    def test_task_generalist_role_prompt_refs_are_not_injected_as_persona(self) -> None:
        role = AgentInfo(
            role_id="task_generalist",
            name="Task Generalist",
            responsibility="Primary session agent for task mode.",
            prompt_refs=["Legacy task-mode persona text."],
        )
        pm = PromptProfileManager(role=role, config=OPCConfig())
        _, prompt = pm.build_prompt(
            Task(title="Task turn", metadata={"mode": "task", "execution_mode": "task_mode"})
        )

        self.assertIn("## Task-Mode Orchestration", prompt)
        self.assertIn("company organization, recruiting flow, employee persona", prompt)
        self.assertNotIn("## Role Operating Instructions", prompt)
        self.assertNotIn("Legacy task-mode persona text.", prompt)


class PersonaSectionTest(unittest.TestCase):
    def test_employee_prompt_context_appears_under_persona_subsection(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="Plan launch",
            metadata={
                "employee_prompt_context": "I focus on emotional engagement and growth metrics.",
            },
        )
        block = ca._build_self_section(task)
        self.assertIn("## Self", block)
        self.assertIn(
            "### Employee Persona\nI focus on emotional engagement and growth metrics.",
            block,
        )

    def test_empty_employee_prompt_context_skips_persona_subsection(self) -> None:
        ca = _make_assembler()
        task = Task(title="Plan launch", metadata={"employee_prompt_context": ""})
        self.assertNotIn("### Employee Persona", ca._build_self_section(task))

    def test_persona_renders_alongside_role_and_employee_when_assignment_present(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="Plan launch",
            metadata={
                "employee_assignment": {
                    "name": "Sarah",
                    "employee_id": "cmo-sarah",
                    "role_id": "cmo",
                    "category": "marketing",
                    "domains": ["growth"],
                    "experience_score": 0,
                },
                "employee_prompt_context": "I love clean code.",
            },
        )
        block = ca._build_self_section(task)
        self.assertEqual(block.count("## Self\n"), 1)
        self.assertIn("### Role", block)
        self.assertIn("- Role: cmo", block)
        self.assertIn("### Employee", block)
        self.assertIn("- Employee: Sarah", block)
        self.assertIn("### Employee Persona\nI love clean code.", block)

    def test_self_role_uses_current_seat_name_and_responsibility(self) -> None:
        ca = _make_assembler()
        task = Task(
            title="CEO intake",
            assigned_to="ceo",
            metadata={
                "runtime_model": "multi_team_org",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::ceo",
                            "role_id": "ceo",
                            "metadata": {
                                "role_name": "CEO",
                                "responsibility": "Own final delivery and coordinate direct reports.",
                            },
                        }
                    ]
                },
                "employee_assignment": {
                    "name": "CEO Fallback Empty Employee",
                    "employee_id": "ceo-fallback-empty-employee",
                    "role_id": "ceo",
                    "category": "fallback",
                    "domains": [],
                    "experience_score": 0.0,
                },
            },
        )
        block = ca._build_self_section(task)
        self.assertIn("### Role", block)
        self.assertIn("- Role: ceo (CEO)", block)
        self.assertIn("- Responsibility: Own final delivery and coordinate direct reports.", block)
        self.assertIn("### Employee", block)
        self.assertIn("- Assignment: fallback employee profile", block)
        self.assertNotIn("- Domains:", block)
        self.assertNotIn("- Experience score:", block)


class PromptRefResolutionTest(unittest.TestCase):
    def test_literal_multiline_prompt_is_not_treated_as_path(self) -> None:
        prompt = (
            "You are a Principal Investigator.\n"
            "Leads research direction, formulates hypotheses, oversees publications, "
            "and mentors researchers.\n"
            "Working style: See what others miss, ask what others won't.\n"
            "Domains of expertise: research-direction, hypothesis, publication."
        )
        self.assertEqual(resolve_prompt_refs([prompt], Path(".opc")), [prompt])


class DualChannelCoexistenceTest(unittest.TestCase):
    def test_real_hire_populates_both_role_and_persona_sections(self) -> None:
        pm = _make_pm(
            role_prompts=["Optimize for audience fit and brand consistency."],
        )
        task = Task(
            title="Plan launch",
            metadata={
                "employee_prompt_context": "I focus on emotional engagement.",
            },
        )
        _, role_prompt = pm.build_prompt(task)

        ca = _make_assembler()
        self_block = ca._build_self_section(task)

        self.assertIn(
            "## Role Operating Instructions\nOptimize for audience fit and brand consistency.",
            role_prompt,
        )
        self.assertIn("### Employee Persona\nI focus on emotional engagement.", self_block)
        self.assertNotIn("I focus on emotional engagement", role_prompt)
        self.assertNotIn("Optimize for audience fit", self_block)


if __name__ == "__main__":
    unittest.main()
