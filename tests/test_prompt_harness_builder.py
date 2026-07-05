from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.config import OPCConfig
from opc.core.models import ExecutionMode, Task
from opc.layer3_agent.prompt_harness.builder import PromptHarnessBuilder
from opc.layer3_agent.prompt_harness.tool_strategy import NativeToolStrategyBuilder
from opc.layer5_memory.skill_library import SkillLibrary


class _ContextAssembler:
    async def build_system_context(self, task: Task, role_id: str = "") -> str:
        _ = (task, role_id)
        return "## Focused Context\n- Investigate the service."


class _Preferences:
    def build_preference_context(self, project_id: str | None = None) -> str:
        _ = project_id
        return "## Preferences\n- Keep responses terse."

    def summarize_autonomy_preferences(self, project_id: str | None = None) -> str:
        _ = project_id
        return "## Autonomy\n- Ask before destructive operations."


class _Skills:
    def build_skills_summary(
        self,
        project_id: str | None = None,
        *,
        execution_mode: str | None = None,
        role_id: str | None = None,
        user_facing: bool = False,
        final_decider_role_id: str | None = None,
        skill_refs: list[str] | None = None,
    ) -> str:
        _ = (project_id, execution_mode, role_id, user_facing, final_decider_role_id, skill_refs)
        return "## Available Skills\n- coding: Best practices for code changes."


class PromptHarnessBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_builder_emits_dynamic_messages_and_boot_artifacts(self) -> None:
        task = Task(
            id="task-prompt-harness",
            title="Implement fix",
            description="Investigate and patch the runtime.",
            session_id="sess-harness",
            project_id="proj1",
            metadata={"secretary_context": "## Secretary\n- Guard workspace."},
            context_snapshot={
                "runtime_resume": {"runtime_session_id": "rt_123", "task_ledger": [{"content": "Fix bug"}]},
                "resident_assignment": {"assignment_id": "assign-1", "team_memory_digest": "# Team Memory\n- Respect rollback ordering."},
            },
        )
        builder = PromptHarnessBuilder(
            task=task,
            role_id="executor",
            config=OPCConfig(),
            context_assembler=_ContextAssembler(),
            preferences=_Preferences(),
            skills=_Skills(),
        )

        output = await builder.build(
            system_prompt="You are a coding agent.",
            allowed_tools=["file_read", "file_edit", "shell_exec"],
        )

        self.assertEqual(output.system_prompt, "You are a coding agent.")
        self.assertEqual(output.runtime_policy_messages, [])
        self.assertGreaterEqual(len(output.workspace_context_messages), 1)
        self.assertGreaterEqual(len(output.dynamic_messages), 1)
        self.assertTrue(any("Focused Context" in item["content"] for item in output.dynamic_messages))
        self.assertFalse(any("Preferences" in item["content"] for item in output.dynamic_messages))
        self.assertFalse(any("Secretary" in item["content"] for item in output.dynamic_messages))
        self.assertTrue(any("Tool Strategy" in item["content"] for item in output.artifact_messages))
        self.assertTrue(any("Use shell execution for commands" in item["content"] for item in output.artifact_messages))
        self.assertTrue(any(record["type"] == "skills_delta" for record in output.artifact_manifest))
        self.assertTrue(any(record["type"] == "team_memory_delta" for record in output.artifact_manifest))
        self.assertIn("tool_surface_delta", output.artifact_hashes)

    async def test_legacy_boolean_runtime_resume_does_not_crash_prompt_harness(self) -> None:
        task = Task(
            id="task-legacy-runtime-resume",
            title="Judge delivery",
            description="Return JSON only.",
            session_id="sess-harness",
            project_id="proj1",
            metadata={"execution_mode": ExecutionMode.COMPANY_MODE.value},
            context_snapshot={"runtime_resume": True},
        )
        builder = PromptHarnessBuilder(
            task=task,
            role_id="chao",
            config=OPCConfig(),
            context_assembler=_ContextAssembler(),
            preferences=_Preferences(),
            skills=_Skills(),
        )

        output = await builder.build(
            system_prompt="You are an assessment agent.",
            allowed_tools=[],
        )

        self.assertEqual(output.system_prompt, "You are an assessment agent.")
        self.assertFalse(any(record["type"] == "resume_state" for record in output.artifact_manifest))

    async def test_builder_injects_task_mode_memory_skill_once_for_native_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "skills" / "memory"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: memory\n"
                "description: Durable memory.\n"
                "always: true\n"
                "---\n\n"
                "# Memory\n\n"
                "Use `.opc/memory/global.md` and `.opc/memory/projects/<current_project_id>.md`.\n",
                encoding="utf-8",
            )
            skills = SkillLibrary(root)
            skills.load_all("proj1")
            task = Task(
                id="task-memory-harness",
                title="Remember durable preference",
                description="Remember durable preference",
                project_id="proj1",
                metadata={"execution_mode": ExecutionMode.TASK_MODE.value},
            )
            builder = PromptHarnessBuilder(
                task=task,
                role_id="task_generalist",
                config=OPCConfig(),
                context_assembler=_ContextAssembler(),
                preferences=_Preferences(),
                skills=skills,
            )

            output = await builder.build(
                system_prompt="You are a coding agent.",
                allowed_tools=["file_read", "file_edit"],
            )

            joined = "\n\n".join(item["content"] for item in output.artifact_messages)
            self.assertEqual(joined.count("## Skill: memory"), 1)
            self.assertEqual(joined.count(".opc/memory/global.md"), 1)
            self.assertTrue(any(record["type"] == "skills_delta" for record in output.artifact_manifest))

    def test_tool_strategy_renders_only_available_categories(self) -> None:
        task_mode_strategy = NativeToolStrategyBuilder(
            ["file_read", "file_edit", "shell_exec", "send_dm"],
            company_mode=False,
        ).render()
        company_strategy = NativeToolStrategyBuilder(
            ["file_read", "send_dm"],
            company_mode=True,
        ).render()

        self.assertIn("Use dedicated read/search/list tools", task_mode_strategy)
        self.assertIn("Use dedicated edit/write/patch tools", task_mode_strategy)
        self.assertIn("Use shell execution for commands", task_mode_strategy)
        self.assertNotIn("company collaboration tools", task_mode_strategy)
        self.assertNotIn("Use Python execution", task_mode_strategy)
        self.assertIn("company collaboration tools", company_strategy)
