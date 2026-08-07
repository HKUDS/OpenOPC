from __future__ import annotations

import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from pydantic import ValidationError

from opc.core.config import (
    AgentsConfig,
    DEFAULT_ORGANIZATION_ID,
    ExternalAgentConfig,
    OPCConfig,
)
from opc.core.models import (
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExecutionMode,
    RouterDecision,
    SessionMessageRecord,
    SessionPartRecord,
    Task,
    TaskStatus,
    UserMessage,
    WorkItemExecutionStrategy,
)
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyRuntimeSpecBuilder, CompanyWorkItemExecutor
from opc.plugins.office_ui.services.models import ServiceError
from opc.layer3_agent.adapters.claude_code import ClaudeCodeAdapter
from opc.layer3_agent.adapters.codex_adapter import CodexAdapter
from opc.layer3_agent.adapters.cursor_adapter import CursorAdapter
from opc.layer3_agent.adapters.opencode_adapter import OpenCodeAdapter
from opc.layer3_agent.adapters.registry import AdapterRegistry
from opc.layer2_organization.org_engine import TASK_MODE_GENERAL_ROLE_ID


@contextlib.contextmanager
def _workspace_tempdir() -> Path:
    base = Path.cwd() / ".tmp-test" / f"runtime-config-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class RuntimeConfigEnforcementTests(unittest.IsolatedAsyncioTestCase):
    def test_role_prompt_task_uses_skip_history_flag_not_boolean_runtime_resume(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        source_task = Task(
            id="delivery-task",
            title="Delivery",
            description="Final delivery.",
            assigned_to="chao",
            project_id="proj1",
            session_id="sess1",
            metadata={"work_item_role_id": "chao"},
        )

        prompt_task = engine._build_role_prompt_task(
            source_task,
            prompt_kind="ceo_pre_delivery_assessment",
            description="Assessment payload",
            execution_agent="native",
            system_prompt="Return JSON only.",
        )

        self.assertNotIn("runtime_resume", prompt_task.context_snapshot)
        self.assertTrue(prompt_task.context_snapshot["skip_session_history"])

    def test_external_agent_approval_mode_allows_only_three_modes(self) -> None:
        for mode in ("user-settings", "auto", "full-auto"):
            config = ExternalAgentConfig(command="codex", approval_mode=mode)
            self.assertEqual(config.approval_mode, mode)

        for invalid_mode in ("manual", "force", "ask"):
            with self.assertRaises(ValidationError):
                ExternalAgentConfig(command="codex", approval_mode=invalid_mode)

    def test_external_agent_defaults_use_install_ready_modes(self) -> None:
        config = AgentsConfig()
        self.assertEqual(config.agents["codex"].approval_mode, "auto")
        self.assertEqual(config.agents["claude_code"].approval_mode, "full-auto")
        self.assertEqual(config.agents["cursor"].approval_mode, "full-auto")
        self.assertEqual(config.agents["opencode"].approval_mode, "full-auto")
        self.assertEqual(config.agents["opencode"].model, "")

    def test_load_migrates_legacy_external_agent_approval_modes_once(self) -> None:
        with _workspace_tempdir() as config_dir:
            agent_config_path = config_dir / "agent_config.yaml"
            agent_config_path.write_text(
                yaml.dump(
                    {
                        "external_agents": {
                            "preferred_order": ["claude_code", "codex", "cursor", "opencode"],
                            "claude_code": {"command": "claude", "approval_mode": "bypass"},
                            "codex": {"command": "codex", "approval_mode": "delegate"},
                            "cursor": {"command": "cursor", "approval_mode": "delegate"},
                            "opencode": {"command": "opencode", "approval_mode": "delegate"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = OPCConfig.load(config_dir)

            self.assertEqual(
                {name: agent.approval_mode for name, agent in config.agents.agents.items()},
                {
                    "claude_code": "auto",
                    "codex": "auto",
                    "cursor": "auto",
                    "opencode": "auto",
                },
            )
            reloaded = yaml.safe_load(agent_config_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["external_agents"]["claude_code"]["approval_mode"], "auto")
            self.assertEqual(reloaded["external_agents"]["codex"]["approval_mode"], "auto")

    def test_load_migrates_legacy_opencode_default_model(self) -> None:
        with _workspace_tempdir() as config_dir:
            agent_config_path = config_dir / "agent_config.yaml"
            agent_config_path.write_text(
                yaml.dump(
                    {
                        "external_agents": {
                            "preferred_order": ["opencode"],
                            "opencode": {
                                "command": "opencode",
                                "model": "opencode/minimax-m2.5-free",
                                "model_flag": "--model",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = OPCConfig.load(config_dir)

            self.assertEqual(config.agents.agents["opencode"].model, "")
            reloaded = yaml.safe_load(agent_config_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["external_agents"]["opencode"]["model"], "")

    def _build_task_mode_engine(self) -> OPCEngine:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.secretary_policies = None
        engine.org_engine = SimpleNamespace(
            configure_task_mode_tools=MagicMock(),
            get_task_mode_role=MagicMock(return_value=SimpleNamespace(role_id=TASK_MODE_GENERAL_ROLE_ID)),
            current_org_version=MagicMock(return_value=1),
            current_runtime_topology_version=MagicMock(return_value=1),
        )
        engine.task_scheduler = SimpleNamespace(
            create_tasks=AsyncMock(
                return_value=[
                    Task(
                        id="task-1",
                        title="Task mode execution",
                        assigned_to=TASK_MODE_GENERAL_ROLE_ID,
                        project_id="proj1",
                        session_id="sess-1",
                        metadata={},
                    )
                ]
            )
        )
        engine.store = SimpleNamespace(save_task=AsyncMock())
        engine.memory = None
        engine._resolve_workspace_contract = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "workspace_root": "/tmp/task-mode-workspace",
                "output_root": "",
                "comms_workspace_root": "/tmp/task-mode-workspace",
                "comms_root": "/tmp/task-mode-workspace/.opc-comms",
            }
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]
        engine._build_attachment_context = MagicMock(return_value="")  # type: ignore[method-assign]
        engine._normalize_attachment_refs = MagicMock(return_value=[])  # type: ignore[method-assign]
        engine._requests_explicit_project_knowledge = MagicMock(return_value=False)  # type: ignore[method-assign]
        engine._task_mode_tool_names = MagicMock(  # type: ignore[method-assign]
            return_value=["request_user_input", "shell_exec", "browser_navigate", "agent_spawn"]
        )
        return engine

    async def test_company_followup_keeps_explicit_native_override(self) -> None:
        engine = OPCEngine()
        decision = RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            preferred_agent="native",
            domains=[],
            company_profile="corporate",
        )

        updated = engine._apply_session_execution_defaults(
            decision,
            {
                "mode": ExecutionMode.COMPANY_MODE.value,
                "company_profile": "corporate",
                "preferred_agent": "codex",
            },
            "继续修这个 company mode 任务",
        )

        self.assertEqual(updated.mode, ExecutionMode.COMPANY_MODE)
        self.assertEqual(updated.preferred_agent, "native")

    async def test_task_mode_defaults_to_native_main_agent(self) -> None:
        engine = self._build_task_mode_engine()

        result = await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "Open the workspace and investigate the issue.",
            session_id="sess-1",
        )

        self.assertEqual(result, "executed")
        created = engine.task_scheduler.create_tasks.await_args.args[0][0]
        self.assertEqual(created["assigned_to"], TASK_MODE_GENERAL_ROLE_ID)
        self.assertIsNone(created["assigned_external_agent"])
        self.assertTrue(created["metadata"]["force_native_execution"])
        self.assertEqual(created["metadata"]["work_item_execution_strategy"], WorkItemExecutionStrategy.NATIVE.value)
        self.assertEqual(created["metadata"]["task_mode_contract"], "single_full_capability_main_agent")
        self.assertEqual(created["metadata"]["runtime_kind"], "task_mode_agent_turn")
        self.assertNotIn("work_item_projection_id", created["metadata"])
        self.assertNotIn("work_item_turn_type", created["metadata"])
        self.assertNotIn("work_item_metadata", created["metadata"])
        self.assertNotIn("employee_assignment", created["metadata"])
        self.assertNotIn("employee_prompt_context", created["metadata"])
        self.assertNotIn("employee_delta_context", created["metadata"])
        self.assertIsNone(engine._execute_single_agent.await_args.args[1])

    async def test_task_mode_preserves_explicit_external_override(self) -> None:
        engine = self._build_task_mode_engine()

        result = await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[], preferred_agent="codex"),
            "Implement the feature with the external coding agent.",
            session_id="sess-1",
        )

        self.assertEqual(result, "executed")
        created = engine.task_scheduler.create_tasks.await_args.args[0][0]
        self.assertEqual(created["assigned_to"], TASK_MODE_GENERAL_ROLE_ID)
        self.assertEqual(created["assigned_external_agent"], "codex")
        self.assertFalse(created["metadata"]["force_native_execution"])
        self.assertEqual(created["metadata"]["preferred_external_agent"], "codex")
        self.assertEqual(created["metadata"]["work_item_execution_strategy"], WorkItemExecutionStrategy.EXTERNAL.value)
        self.assertNotIn("work_item_projection_id", created["metadata"])
        self.assertNotIn("work_item_turn_type", created["metadata"])
        self.assertNotIn("employee_assignment", created["metadata"])
        self.assertEqual(engine._execute_single_agent.await_args.args[1], "codex")

    async def test_company_root_task_uses_selected_agent_over_template_preference(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=None),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        engine.org_engine = SimpleNamespace(
            current_org_version=MagicMock(return_value=1),
            current_runtime_topology_version=MagicMock(return_value=1),
        )
        engine._requests_explicit_project_knowledge = MagicMock(return_value=False)  # type: ignore[method-assign]

        work_item = DelegationWorkItem(
            work_item_id="wi-opencode",
            run_id="run-1",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={
                "seat_id": "seat-engineer",
                "team_id": "team::engineering",
                "work_kind": "execute",
            },
        )
        runtime_topology = {
            "final_decider_role_id": "lead",
            "seats": [
                {
                    "seat_id": "seat-engineer",
                    "team_id": "team::engineering",
                    "role_id": "engineer",
                    "preferred_external_agent": "claude_code",
                    "selected_execution_agent": "opencode",
                    "execution_agent_locked": True,
                    "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                    "metadata": {"role_name": "Engineer"},
                }
            ],
        }

        task = await engine._ensure_runtime_work_item_task(
            work_item=work_item,
            parent_session_id="sess-company",
            original_message="Build the thing.",
            decision=RouterDecision(mode=ExecutionMode.COMPANY_MODE, domains=[], company_profile="corporate"),
            runtime_topology=runtime_topology,
            delegation_playbook={},
            secretary_context="",
            target_output_dir=None,
            origin_channel="cli",
            origin_chat_id="",
            origin_thread_id="",
            origin_task_id=None,
            attachment_refs=[],
            attachment_context="",
            force_native_execution=False,
        )

        self.assertEqual(task.assigned_external_agent, "opencode")
        self.assertEqual(task.metadata["selected_execution_agent"], "opencode")
        self.assertEqual(task.metadata["preferred_external_agent"], "opencode")
        self.assertEqual(task.metadata["work_item_execution_strategy"], WorkItemExecutionStrategy.EXTERNAL.value)

    async def test_company_root_task_persists_decision_org_over_active_config(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=None),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        engine.org_engine = SimpleNamespace(
            current_org_version=MagicMock(return_value=1),
            current_runtime_topology_version=MagicMock(return_value=1),
        )
        engine._requests_explicit_project_knowledge = MagicMock(return_value=False)
        work_item = DelegationWorkItem(
            work_item_id="wi-selected-org",
            run_id="run-selected-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )

        task = await engine._ensure_runtime_work_item_task(
            work_item=work_item,
            parent_session_id="sess-company",
            original_message="Build the thing.",
            decision=RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                domains=[],
                company_profile="custom",
                org_id="selected-org",
            ),
            runtime_topology={
                "final_decider_role_id": "lead",
                "seats": [
                    {
                        "seat_id": "seat-engineer",
                        "team_id": "team::engineering",
                        "role_id": "engineer",
                        "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                        "metadata": {"role_name": "Engineer"},
                    }
                ],
            },
            delegation_playbook={},
            secretary_context="",
            target_output_dir=None,
            origin_channel="cli",
            origin_chat_id="",
            origin_thread_id="",
            origin_task_id=None,
            attachment_refs=[],
            attachment_context="",
            force_native_execution=False,
        )

        self.assertEqual(task.org_id, "selected-org")
        self.assertEqual(task.metadata["org_id"], "selected-org")
        self.assertEqual(task.metadata["organization_id"], "selected-org")

    def test_build_spec_rejects_custom_run_without_durable_org_id(self) -> None:
        builder = CompanyRuntimeSpecBuilder(
            org_engine=SimpleNamespace(
                get_company_profile=lambda: "custom",
                config=SimpleNamespace(
                    org=SimpleNamespace(
                        organization_id="active-org",
                        organization_name="Active Org",
                        organization_config_file="org_active-org_config.yaml",
                        company_profile="custom",
                    )
                ),
            )
        )
        with self.assertRaises(ServiceError) as ctx:
            builder.build_spec(
                RouterDecision(
                    mode=ExecutionMode.COMPANY_MODE,
                    company_profile="custom",
                    org_id=None,
                ),
                original_message="Run the company.",
            )
        assert ctx.exception.code == "org_id_required"

    def test_build_spec_serializes_selected_org_not_active_config(self) -> None:
        builder = CompanyRuntimeSpecBuilder(
            org_engine=SimpleNamespace(
                get_company_profile=lambda: "custom",
                config=SimpleNamespace(
                    org=SimpleNamespace(
                        organization_id="active-org",
                        organization_name="Active Org",
                        organization_config_file="org_active-org_config.yaml",
                        company_profile="custom",
                    )
                ),
            )
        )
        decision = RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="custom",
            org_id="selected-org",
        )
        spec = builder.build_spec(decision, original_message="Run the company.")
        assert spec.metadata["org_id"] == "selected-org"
        assert spec.metadata["organization_id"] == "selected-org"

    def test_runtime_org_id_for_identity_never_derives_from_active_config(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        decision = RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="custom",
            org_id=None,
        )
        resolved = OPCEngine._runtime_org_id_for_identity(
            decision,
            {},
            engine.config.org,
        )
        assert resolved is None

    async def test_ensure_runtime_work_item_task_rejects_custom_without_durable_org(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=None),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        engine.org_engine = SimpleNamespace(
            current_org_version=MagicMock(return_value=1),
            current_runtime_topology_version=MagicMock(return_value=1),
        )
        engine._requests_explicit_project_knowledge = MagicMock(return_value=False)
        work_item = DelegationWorkItem(
            work_item_id="wi-no-durable-org",
            run_id="run-no-durable-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )
        with self.assertRaises(ServiceError) as ctx:
            await engine._ensure_runtime_work_item_task(
                work_item=work_item,
                parent_session_id="sess-company",
                original_message="Build the thing.",
                decision=RouterDecision(
                    mode=ExecutionMode.COMPANY_MODE,
                    company_profile="custom",
                    org_id=None,
                ),
                runtime_topology={
                    "final_decider_role_id": "lead",
                    "seats": [
                        {
                            "seat_id": "seat-engineer",
                            "team_id": "team::engineering",
                            "role_id": "engineer",
                            "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                            "metadata": {"role_name": "Engineer"},
                        }
                    ],
                },
                delegation_playbook={},
                secretary_context="",
                target_output_dir=None,
                origin_channel="cli",
                origin_chat_id="",
                origin_thread_id="",
                origin_task_id=None,
                attachment_refs=[],
                attachment_context="",
                force_native_execution=False,
            )
        assert ctx.exception.code == "org_id_required"
        engine.store.save_task.assert_not_awaited()

    async def test_ensure_runtime_work_item_task_rejects_existing_custom_task_without_durable_org(self) -> None:
        existing = Task(
            id="existing-no-org",
            title="Engineering execution",
            project_id="proj1",
            session_id="sess-company:wi-existing-no-org",
            assigned_to="engineer",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "engineering-execute",
                "work_item_turn_type": "execute",
                "company_profile": "custom",
                "delegation_seat_id": "seat-engineer",
            },
        )
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=existing),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        work_item = DelegationWorkItem(
            work_item_id="wi-existing-no-org",
            run_id="run-existing-no-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )
        with self.assertRaises(ServiceError) as ctx:
            await engine._ensure_runtime_work_item_task(
                work_item=work_item,
                parent_session_id="sess-company",
                original_message="Build the thing.",
                decision=RouterDecision(
                    mode=ExecutionMode.COMPANY_MODE,
                    company_profile="custom",
                    org_id=None,
                ),
                runtime_topology={
                    "final_decider_role_id": "lead",
                    "seats": [
                        {
                            "seat_id": "seat-engineer",
                            "team_id": "team::engineering",
                            "role_id": "engineer",
                            "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                            "metadata": {"role_name": "Engineer"},
                        }
                    ],
                },
                delegation_playbook={},
                secretary_context="",
                target_output_dir=None,
                origin_channel="cli",
                origin_chat_id="",
                origin_thread_id="",
                origin_task_id=None,
                attachment_refs=[],
                attachment_context="",
                force_native_execution=False,
            )
        assert ctx.exception.code == "org_id_required"
        engine.store.save_task.assert_not_awaited()

    async def test_ensure_runtime_work_item_task_rejects_conflicting_org_ids(self) -> None:
        existing = Task(
            id="existing-conflict-org",
            title="Engineering execution",
            project_id="proj1",
            session_id="sess-company:wi-existing-conflict-org",
            assigned_to="engineer",
            org_id="persisted-org",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "engineering-execute",
                "work_item_turn_type": "execute",
                "company_profile": "custom",
                "organization_id": "persisted-org",
                "delegation_seat_id": "seat-engineer",
            },
        )
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=existing),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        work_item = DelegationWorkItem(
            work_item_id="wi-existing-conflict-org",
            run_id="run-existing-conflict-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )
        with self.assertRaises(ServiceError) as ctx:
            await engine._ensure_runtime_work_item_task(
                work_item=work_item,
                parent_session_id="sess-company",
                original_message="Build the thing.",
                decision=RouterDecision(
                    mode=ExecutionMode.COMPANY_MODE,
                    company_profile="custom",
                    org_id="selected-org",
                ),
                runtime_topology={
                    "final_decider_role_id": "lead",
                    "seats": [
                        {
                            "seat_id": "seat-engineer",
                            "team_id": "team::engineering",
                            "role_id": "engineer",
                            "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                            "metadata": {"role_name": "Engineer"},
                        }
                    ],
                },
                delegation_playbook={},
                secretary_context="",
                target_output_dir=None,
                origin_channel="cli",
                origin_chat_id="",
                origin_thread_id="",
                origin_task_id=None,
                attachment_refs=[],
                attachment_context="",
                force_native_execution=False,
            )
        assert ctx.exception.code == "org_id_conflict"
        engine.store.save_task.assert_not_awaited()

    async def test_ensure_runtime_work_item_task_repairs_existing_custom_task_from_persisted_durable_org(self) -> None:
        existing = Task(
            id="existing-durable-org",
            title="Engineering execution",
            project_id="proj1",
            session_id="sess-company:wi-existing-durable-org",
            assigned_to="engineer",
            org_id="persisted-org",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "engineering-execute",
                "work_item_turn_type": "execute",
                "company_profile": "custom",
                "organization_id": "stale-org",
                "delegation_seat_id": "seat-engineer",
            },
        )
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        engine.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=existing),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        engine.memory = SimpleNamespace(ensure_session=AsyncMock())
        work_item = DelegationWorkItem(
            work_item_id="wi-existing-durable-org",
            run_id="run-existing-durable-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )
        repaired = await engine._ensure_runtime_work_item_task(
            work_item=work_item,
            parent_session_id="sess-company",
            original_message="Build the thing.",
            decision=RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id=None,
            ),
            runtime_topology={
                "final_decider_role_id": "lead",
                "seats": [
                    {
                        "seat_id": "seat-engineer",
                        "team_id": "team::engineering",
                        "role_id": "engineer",
                        "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                        "metadata": {"role_name": "Engineer"},
                    }
                ],
            },
            delegation_playbook={},
            secretary_context="",
            target_output_dir=None,
            origin_channel="cli",
            origin_chat_id="",
            origin_thread_id="",
            origin_task_id=None,
            attachment_refs=[],
            attachment_context="",
            force_native_execution=False,
        )
        self.assertEqual(repaired.id, "existing-durable-org")
        self.assertEqual(repaired.org_id, "persisted-org")
        self.assertEqual(repaired.metadata["org_id"], "persisted-org")
        self.assertEqual(repaired.metadata["organization_id"], "persisted-org")
        engine.store.save_task.assert_awaited_with(existing)

    async def test_delivery_self_evolution_custom_without_durable_org_fails_closed(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        waiting_task = Task(
            id="waiting-custom",
            project_id="proj1",
            session_id="sess-delivery",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "custom",
                "work_item_runtime": True,
                "work_item_projection_id": "delivery",
                "work_item_turn_type": "deliver",
            },
        )
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=waiting_task))
        engine._mark_company_runtime_checkpoint_status = AsyncMock()
        engine._create_company_self_evolution_root_work_item = AsyncMock()
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery",
            project_id="proj1",
            session_id="sess-delivery",
            task_id="waiting-custom",
            checkpoint_type="company_delivery_feedback",
            payload={"waiting_task_id": "waiting-custom", "task_ids": ["waiting-custom"]},
        )
        result = await engine._run_company_delivery_self_evolution_consumed(
            checkpoint,
            action="approve",
        )
        assert "durable org identity" in result
        engine._mark_company_runtime_checkpoint_status.assert_awaited_once_with(
            checkpoint,
            status="invalid",
        )
        engine._create_company_self_evolution_root_work_item.assert_not_awaited()

    async def test_delivery_self_evolution_custom_uses_durable_org_over_payload_and_config(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        waiting_task = Task(
            id="waiting-custom-2",
            project_id="proj1",
            session_id="sess-delivery",
            org_id="selected-org",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "custom",
                "work_item_runtime": True,
                "work_item_projection_id": "delivery",
                "work_item_turn_type": "deliver",
            },
        )
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=waiting_task))
        engine._mark_company_runtime_checkpoint_status = AsyncMock()
        engine.org_engine = None
        engine._company_followup_target_task = MagicMock(
            return_value=SimpleNamespace(assigned_to="lead", metadata={}),
        )
        engine.company_executor = SimpleNamespace()
        engine._self_evolution_assignments_by_role = MagicMock(return_value={})
        engine._create_company_self_evolution_root_work_item = AsyncMock(return_value=None)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-2",
            project_id="proj1",
            session_id="sess-delivery",
            task_id="waiting-custom-2",
            checkpoint_type="company_delivery_feedback",
            payload={
                "waiting_task_id": "waiting-custom-2",
                "task_ids": ["waiting-custom-2"],
                "organization_id": "stale-org",
            },
        )
        await engine._run_company_delivery_self_evolution_consumed(
            checkpoint,
            action="approve",
        )
        call = engine._create_company_self_evolution_root_work_item.await_args
        assert call is not None
        assert call.kwargs["organization_id"] == "selected-org"

    async def test_delivery_self_evolution_corporate_still_uses_config_default(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        waiting_task = Task(
            id="waiting-corporate",
            project_id="proj1",
            session_id="sess-delivery",
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_runtime": True,
                "work_item_projection_id": "delivery",
                "work_item_turn_type": "deliver",
            },
        )
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=waiting_task))
        engine._mark_company_runtime_checkpoint_status = AsyncMock()
        engine.org_engine = None
        engine._company_followup_target_task = MagicMock(
            return_value=SimpleNamespace(assigned_to="lead", metadata={}),
        )
        engine.company_executor = SimpleNamespace()
        engine._self_evolution_assignments_by_role = MagicMock(return_value={})
        engine._create_company_self_evolution_root_work_item = AsyncMock(return_value=None)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-corp",
            project_id="proj1",
            session_id="sess-delivery",
            task_id="waiting-corporate",
            checkpoint_type="company_delivery_feedback",
            payload={"waiting_task_id": "waiting-corporate", "task_ids": ["waiting-corporate"]},
        )
        await engine._run_company_delivery_self_evolution_consumed(
            checkpoint,
            action="approve",
        )
        call = engine._create_company_self_evolution_root_work_item.await_args
        assert call is not None
        assert call.kwargs["organization_id"] == DEFAULT_ORGANIZATION_ID

    async def test_delivery_self_evolution_metadata_only_legacy_custom_uses_metadata_org(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        waiting_task = Task(
            id="waiting-legacy-metadata",
            project_id="proj1",
            session_id="sess-delivery",
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "delivery",
                "work_item_turn_type": "deliver",
                "org_id": "selected-org",
            },
        )
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=waiting_task))
        engine._mark_company_runtime_checkpoint_status = AsyncMock()
        engine.org_engine = None
        engine._company_followup_target_task = MagicMock(
            return_value=SimpleNamespace(assigned_to="lead", metadata={}),
        )
        engine.company_executor = SimpleNamespace()
        engine._self_evolution_assignments_by_role = MagicMock(return_value={})
        engine._create_company_self_evolution_root_work_item = AsyncMock(return_value=None)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-legacy",
            project_id="proj1",
            session_id="sess-delivery",
            task_id="waiting-legacy-metadata",
            checkpoint_type="company_delivery_feedback",
            payload={
                "waiting_task_id": "waiting-legacy-metadata",
                "task_ids": ["waiting-legacy-metadata"],
            },
        )
        await engine._run_company_delivery_self_evolution_consumed(
            checkpoint,
            action="approve",
        )
        call = engine._create_company_self_evolution_root_work_item.await_args
        assert call is not None
        assert call.kwargs["organization_id"] == "selected-org"

    async def test_delivery_self_evolution_conflicting_org_ids_fail_closed(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.config.org.company_profile = "custom"
        engine.config.org.organization_id = "active-org"
        waiting_task = Task(
            id="waiting-conflict",
            project_id="proj1",
            session_id="sess-delivery",
            org_id="selected-org",
            metadata={
                "work_item_runtime": True,
                "work_item_projection_id": "delivery",
                "work_item_turn_type": "deliver",
                "org_id": "other-org",
            },
        )
        engine.store = SimpleNamespace(get_task=AsyncMock(return_value=waiting_task))
        engine._mark_company_runtime_checkpoint_status = AsyncMock()
        engine.org_engine = None
        engine._company_followup_target_task = MagicMock(
            return_value=SimpleNamespace(assigned_to="lead", metadata={}),
        )
        engine.company_executor = SimpleNamespace()
        engine._self_evolution_assignments_by_role = MagicMock(return_value={})
        engine._create_company_self_evolution_root_work_item = AsyncMock(return_value=None)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="cp-delivery-conflict",
            project_id="proj1",
            session_id="sess-delivery",
            task_id="waiting-conflict",
            checkpoint_type="company_delivery_feedback",
            payload={
                "waiting_task_id": "waiting-conflict",
                "task_ids": ["waiting-conflict"],
            },
        )
        result = await engine._run_company_delivery_self_evolution_consumed(
            checkpoint,
            action="approve",
        )
        assert "org identity" in result
        engine._mark_company_runtime_checkpoint_status.assert_awaited_once_with(
            checkpoint,
            status="invalid",
        )
        engine._create_company_self_evolution_root_work_item.assert_not_awaited()

    async def test_company_materialized_work_item_uses_selected_agent_over_template_preference(self) -> None:
        saved_tasks: list[Task] = []

        async def save_task(task: Task) -> None:
            saved_tasks.append(task)

        executor = CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=SimpleNamespace(ensure_session=AsyncMock()),
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        executor.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=None),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(side_effect=save_task),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        root_task = Task(
            id="root-task",
            title="Root",
            project_id="proj1",
            session_id="sess-company",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat-engineer",
                            "team_id": "team::engineering",
                            "role_id": "engineer",
                            "preferred_external_agent": "claude_code",
                            "selected_execution_agent": "cursor",
                            "execution_agent_locked": True,
                            "metadata": {"role_name": "Engineer"},
                        }
                    ]
                },
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="wi-cursor",
            run_id="run-1",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )

        tasks = await executor._materialize_work_item_tasks([root_task], [work_item])
        created = next(task for task in tasks if task.id != root_task.id)

        self.assertEqual(created.assigned_external_agent, "cursor")
        self.assertEqual(created.metadata["selected_execution_agent"], "cursor")
        self.assertEqual(created.metadata["preferred_external_agent"], "cursor")
        self.assertEqual(created.metadata["work_item_execution_strategy"], WorkItemExecutionStrategy.EXTERNAL.value)
        self.assertEqual(saved_tasks[0].assigned_external_agent, "cursor")

    async def test_company_materialization_persists_durable_org_on_new_role_task(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=SimpleNamespace(ensure_session=AsyncMock()),
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        executor.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=None),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
            link_work_item_runtime_task=AsyncMock(return_value=True),
        )
        root_task = Task(
            id="root-custom-org",
            title="Root",
            project_id="proj1",
            session_id="sess-company",
            org_id="selected-org",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "company_profile": "custom",
                "organization_id": "active-org",
                "runtime_topology": {
                    "seats": [
                        {
                            "seat_id": "seat-engineer",
                            "team_id": "team::engineering",
                            "role_id": "engineer",
                            "metadata": {"role_name": "Engineer"},
                        }
                    ]
                },
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="wi-new-custom-org",
            run_id="run-custom-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )

        tasks = await executor._materialize_work_item_tasks([root_task], [work_item])
        created = next(task for task in tasks if task.id != root_task.id)

        self.assertEqual(created.org_id, "selected-org")
        self.assertEqual(created.metadata["org_id"], "selected-org")
        self.assertEqual(created.metadata["organization_id"], "selected-org")

    async def test_company_materialization_repairs_existing_role_task_org_identity(self) -> None:
        existing = Task(
            id="existing-custom-role",
            title="Engineering execution",
            project_id="proj1",
            session_id="sess-company:wi-existing-custom-org",
            assigned_to="engineer",
            org_id="active-org",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_item_projection_id": "engineering-execute",
                "work_item_turn_type": "execute",
                "company_profile": "custom",
                "organization_id": "active-org",
                "delegation_seat_id": "seat-engineer",
            },
        )
        executor = CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=SimpleNamespace(ensure_session=AsyncMock()),
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        executor.store = SimpleNamespace(
            get_runtime_task_for_work_item=AsyncMock(return_value=existing),
            save_delegation_work_item=AsyncMock(),
            save_task=AsyncMock(),
        )
        root_task = Task(
            id="root-custom-org-existing",
            title="Root",
            project_id="proj1",
            session_id="sess-company",
            org_id="selected-org",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "company_profile": "custom",
                "organization_id": "active-org",
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="wi-existing-custom-org",
            run_id="run-custom-org",
            cell_id="team::engineering",
            team_instance_id="team-instance-1",
            role_id="engineer",
            seat_id="seat-engineer",
            title="Engineering execution",
            summary="Implement the requested change.",
            kind="execute",
            projection_id="engineering-execute",
            metadata={"seat_id": "seat-engineer", "team_id": "team::engineering"},
        )

        tasks = await executor._materialize_work_item_tasks([root_task], [work_item])
        repaired = next(task for task in tasks if task.id == existing.id)

        self.assertEqual(repaired.org_id, "selected-org")
        self.assertEqual(repaired.metadata["org_id"], "selected-org")
        self.assertEqual(repaired.metadata["organization_id"], "selected-org")
        executor.store.save_task.assert_any_await(existing)

    async def test_task_mode_external_followup_reuses_primary_session_external_agent_session(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.store = SimpleNamespace(
            get_external_session=AsyncMock(
                return_value=SimpleNamespace(
                    agent_type="claude_code",
                    project_id="proj1",
                    session_id="4f8b0b4e-6ab0-4ac5-9cf6-d7f0b4fbc001",
                    task_id="previous-task",
                    metadata={},
                )
            )
        )
        task = Task(
            id="task-followup",
            project_id="proj1",
            session_id="sess-1",
            assigned_external_agent="claude_code",
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
            },
        )
        adapter = ClaudeCodeAdapter(config=ExternalAgentConfig(command="claude"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "resume")
        self.assertEqual(run_adapter.config.session_id, "4f8b0b4e-6ab0-4ac5-9cf6-d7f0b4fbc001")
        engine.store.get_external_session.assert_awaited_once_with("claude_code", "proj1", opc_session_id="sess-1")
        self.assertEqual(resume_metadata["resume_source_session"], "4f8b0b4e-6ab0-4ac5-9cf6-d7f0b4fbc001")

    async def test_task_mode_followup_reuses_existing_primary_task(self) -> None:
        engine = self._build_task_mode_engine()
        existing = Task(
            id="task-existing",
            title="Task mode execution",
            description="old message",
            assigned_to=TASK_MODE_GENERAL_ROLE_ID,
            project_id="proj1",
            session_id="sess-1",
            status=TaskStatus.IDLE,
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "work_item_projection_id": "task_mode_execution",
                "work_item_turn_type": "execute",
                "work_item_metadata": {"legacy": True},
                "employee_assignment": {"employee_id": "task-general-default-employee"},
            },
        )
        engine.store = SimpleNamespace(
            save_task=AsyncMock(),
            get_task=AsyncMock(return_value=existing),
            get_tasks=AsyncMock(return_value=[existing]),
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]

        result = await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "Follow-up request",
            session_id="sess-1",
            origin_task_id="task-existing",
        )

        self.assertEqual(result, "executed")
        engine.task_scheduler.create_tasks.assert_not_awaited()
        reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(reused.id, "task-existing")
        self.assertEqual(reused.title, "Follow-up request")
        self.assertEqual(reused.description, "Follow-up request")
        self.assertEqual(reused.metadata["execution_task_ids"], ["task-existing"])
        self.assertEqual(reused.metadata["runtime_kind"], "task_mode_agent_turn")
        self.assertNotIn("work_item_projection_id", reused.metadata)
        self.assertNotIn("work_item_turn_type", reused.metadata)
        self.assertNotIn("work_item_metadata", reused.metadata)
        self.assertNotIn("employee_assignment", reused.metadata)
        self.assertNotIn("employee_prompt_context", reused.metadata)
        self.assertNotIn("employee_delta_context", reused.metadata)

    async def test_task_mode_reused_task_updates_current_conversation_turn_id(self) -> None:
        engine = self._build_task_mode_engine()
        existing = Task(
            id="task-existing-turn",
            title="Task mode execution",
            description="old message",
            assigned_to=TASK_MODE_GENERAL_ROLE_ID,
            project_id="proj1",
            session_id="sess-1",
            status=TaskStatus.IDLE,
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "employee_assignment": {"employee_id": "task-general-default-employee"},
                "runtime_v2": {"runtime_session_id": "rt_existing"},
            },
        )
        engine.store = SimpleNamespace(
            save_task=AsyncMock(),
            get_task=AsyncMock(return_value=existing),
            get_tasks=AsyncMock(return_value=[existing]),
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]

        await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "First follow-up",
            session_id="sess-1",
            origin_task_id="task-existing-turn",
            conversation_turn_id="ui-turn:first",
        )
        first_reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(first_reused.metadata["conversation_turn_id"], "ui-turn:first")
        self.assertEqual(first_reused.metadata["current_turn_id"], "ui-turn:first")
        self.assertEqual(first_reused.metadata["runtime_v2"]["current_turn_id"], "ui-turn:first")
        self.assertNotIn("employee_assignment", first_reused.metadata)

        engine._execute_single_agent.reset_mock()
        await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "Second follow-up",
            session_id="sess-1",
            origin_task_id="task-existing-turn",
            conversation_turn_id="ui-turn:second",
        )
        second_reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(second_reused.metadata["conversation_turn_id"], "ui-turn:second")
        self.assertEqual(second_reused.metadata["current_turn_id"], "ui-turn:second")
        self.assertEqual(second_reused.metadata["runtime_v2"]["current_turn_id"], "ui-turn:second")

    async def test_task_mode_first_native_turn_reuses_office_ui_shell_task(self) -> None:
        engine = self._build_task_mode_engine()
        existing = Task(
            id="ui-shell-task",
            title="New Chat",
            description="",
            assigned_to="",
            project_id="proj1",
            session_id="sess-1",
            status=TaskStatus.RUNNING,
            metadata={
                "exec_mode": "task",
                "execution_mode": "task_mode",
                "preferred_agent": "native",
                "origin_task_id": "ui-shell-task",
            },
        )
        engine.store = SimpleNamespace(
            save_task=AsyncMock(),
            get_task=AsyncMock(return_value=existing),
            get_tasks=AsyncMock(return_value=[existing]),
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]

        result = await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[], preferred_agent="native"),
            "First native request",
            session_id="sess-1",
            origin_task_id="ui-shell-task",
        )

        self.assertEqual(result, "executed")
        engine.task_scheduler.create_tasks.assert_not_awaited()
        reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(reused.id, "ui-shell-task")
        self.assertEqual(reused.assigned_to, TASK_MODE_GENERAL_ROLE_ID)
        self.assertEqual(reused.metadata["mode"], "task")
        self.assertEqual(reused.metadata["origin_task_id"], "ui-shell-task")
        self.assertEqual(reused.metadata["task_mode_contract"], "single_full_capability_main_agent")
        self.assertEqual(reused.metadata["selected_execution_agent"], "native")
        self.assertNotIn("employee_assignment", reused.metadata)

    async def test_runtime_v2_assistant_turn_suppresses_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        runtime_message = SessionMessageRecord(
            session_id="sess-1",
            role="assistant",
            task_id="ui-shell-task",
            agent_id=TASK_MODE_GENERAL_ROLE_ID,
            metadata={"kind": "runtime_v2_assistant"},
        )
        runtime_part = SessionPartRecord(
            session_id="sess-1",
            message_id=runtime_message.message_id,
            part_type="text",
            payload={"text": "Done.\n\nVerification: not required."},
        )
        engine.store = SimpleNamespace(
            get_session_transcript=AsyncMock(
                return_value=[{"message": runtime_message, "parts": [runtime_part]}]
            )
        )
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-1",
            "please do it",
            "Done.",
            mode="task",
            origin_task_id="ui-shell-task",
        )

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_native_task_mode_primary_exchange_does_not_synthesize_runtime_fallback(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.store = SimpleNamespace(get_session_transcript=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-1",
            "please do it",
            "Done.",
            mode="task",
            origin_task_id="ui-shell-task",
            preferred_agent="native",
        )

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_external_task_mode_primary_exchange_records_opc_wrapped_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.store = SimpleNamespace(get_session_transcript=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-1",
            "please do it",
            "External result.",
            mode="task",
            origin_task_id="ui-shell-task",
            preferred_agent="codex",
        )

        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["content"], "External result.")
        self.assertEqual(kwargs["metadata"], {"kind": "top_level_reply"})

    async def test_external_task_mode_primary_exchange_records_when_preferred_agent_missing(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        external_task = Task(
            id="runtime-task",
            session_id="sess-1",
            project_id="proj1",
            assigned_external_agent="opencode",
            result={"content": "External result.", "artifacts": {}},
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "origin_task_id": "ui-shell-task",
                "selected_execution_agent": "opencode",
            },
        )
        engine.store = SimpleNamespace(
            get_tasks=AsyncMock(return_value=[external_task]),
            get_session_transcript=AsyncMock(return_value=[]),
        )
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-1",
            "please do it",
            "External result.",
            mode="task",
            origin_task_id="ui-shell-task",
        )

        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["content"], "External result.")
        self.assertEqual(kwargs["metadata"], {"kind": "top_level_reply"})

    async def test_external_task_mode_done_result_records_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        task = Task(
            id="runtime-task",
            session_id="sess-1",
            project_id="proj1",
            assigned_external_agent="opencode",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "task_mode_contract": "single_full_capability_main_agent",
                "selected_execution_agent": "opencode",
                "conversation_turn_id": "ui-turn:external",
            },
        )
        engine.store = SimpleNamespace(get_session_transcript=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_task_mode_external_result_reply(task, "External result.")

        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["session_id"], "sess-1")
        self.assertEqual(kwargs["content"], "External result.")
        self.assertEqual(kwargs["task_id"], "runtime-task")
        self.assertEqual(kwargs["metadata"]["kind"], "top_level_reply")
        self.assertTrue(kwargs["metadata"]["task_mode_external_result"])
        self.assertEqual(kwargs["metadata"]["assigned_external_agent"], "opencode")
        self.assertEqual(kwargs["metadata"]["conversation_turn_id"], "ui-turn:external")
        self.assertEqual(kwargs["metadata"]["ui_message_id"], "task-mode-external-reply:ui-turn:external")

    async def test_external_task_mode_done_result_reply_is_idempotent(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        task = Task(
            id="runtime-task",
            session_id="sess-1",
            project_id="proj1",
            assigned_external_agent="claude_code",
            metadata={
                "mode": "task",
                "execution_mode": "task_mode",
                "task_mode_contract": "single_full_capability_main_agent",
                "conversation_turn_id": "ui-turn:external",
            },
        )
        message = SessionMessageRecord(
            session_id="sess-1",
            role="assistant",
            task_id="runtime-task",
            metadata={
                "kind": "top_level_reply",
                "task_mode_external_result": True,
                "task_id": "runtime-task",
                "conversation_turn_id": "ui-turn:external",
            },
        )
        part = SessionPartRecord(
            session_id="sess-1",
            message_id=message.message_id,
            part_type="text",
            payload={"text": "External result."},
        )
        engine.store = SimpleNamespace(
            get_session_transcript=AsyncMock(return_value=[{"message": message, "parts": [part]}])
        )
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_task_mode_external_result_reply(task, "External result.")

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_company_work_item_external_result_does_not_record_task_mode_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        task = Task(
            id="company-runtime-task",
            session_id="sess-company",
            project_id="proj1",
            assigned_external_agent="opencode",
            metadata={
                "mode": "task",
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "ceo::delivery",
                "selected_execution_agent": "opencode",
            },
        )
        engine.store = SimpleNamespace(get_session_transcript=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_task_mode_external_result_reply(task, "Company role result.")

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_existing_company_runtime_early_reply_records_assistant_turn(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        engine.context_loader = SimpleNamespace()
        engine.memory = SimpleNamespace(
            record_user_turn=AsyncMock(),
            record_assistant_turn=AsyncMock(),
        )
        engine._ensure_primary_session = AsyncMock()
        engine._maybe_resume_checkpoint = AsyncMock(return_value=None)
        engine._maybe_handle_reorg_message = AsyncMock(return_value=None)
        engine._maybe_resume_existing_company_runtime = AsyncMock(
            return_value="Routed the latest user follow-up to the final decider."
        )

        response = await engine._handle_message(UserMessage(
            channel="cli",
            user_id="owner",
            content="检查一下UI",
            session_id="sess-company",
            metadata={
                "mode": "company",
                "origin_task_id": "task-company",
                "company_profile": "corporate",
            },
        ))

        self.assertEqual(response.content, "Routed the latest user follow-up to the final decider.")
        engine.memory.record_user_turn.assert_awaited_once()
        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["session_id"], "sess-company")
        self.assertEqual(kwargs["content"], response.content)
        self.assertEqual(kwargs["metadata"], {"kind": "top_level_reply"})

    async def test_company_internal_dispatch_result_does_not_synthesize_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        dispatch_text = "已创建下游 WorkItem：`wi-ppt`，交给 `report_producer`。"
        intake_task = Task(
            id="intake-task",
            session_id="sess-company",
            status=TaskStatus.DONE,
            result={"content": dispatch_text, "artifacts": {}},
            metadata={
                "execution_model": "multi_team_org",
                "work_item_turn_type": "intake",
                "manager_board_mutation_performed": True,
                "delegation_wait_for_work_item_ids": ["wi-ppt"],
            },
        )
        engine.store = SimpleNamespace(get_tasks=AsyncMock(return_value=[intake_task]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-company",
            "生成一个 PPT",
            dispatch_text,
            mode="company",
            origin_task_id="ui-anchor",
        )

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_company_final_delivery_result_does_not_synthesize_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        final_text = "最终交付内容已经由最高层角色给出，包含可直接展示给用户的结论。"
        delivery_task = Task(
            id="delivery-task",
            session_id="sess-company:delivery",
            parent_session_id="sess-company",
            status=TaskStatus.AWAITING_HUMAN,
            result={"content": final_text, "artifacts": {}},
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "deliver",
                "feedback_scope": "final",
                "authoritative_output": True,
                "company_profile": "custom",
            },
        )
        engine.store = SimpleNamespace(get_tasks=AsyncMock(return_value=[delivery_task]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-company",
            "做一个最终分析",
            final_text,
            mode="company",
            origin_task_id="ui-anchor",
        )

        engine.memory.record_assistant_turn.assert_not_awaited()

    async def test_non_company_marker_text_still_records_top_level_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        dispatch_text = "已创建下游 WorkItem 是 company mode 的内部进度文案。"
        engine.store = SimpleNamespace(get_tasks=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-chat",
            "解释这句话",
            dispatch_text,
            mode="chat",
            origin_task_id="ui-anchor",
            preferred_agent="codex",
        )

        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["content"], dispatch_text)

    async def test_company_marker_text_without_internal_task_still_records_direct_reply(self) -> None:
        engine = OPCEngine(config=OPCConfig(), project_id="proj1")
        dispatch_text = "已创建下游 WorkItem 这句话可以作为最高层显式说明的一部分。"
        engine.store = SimpleNamespace(get_tasks=AsyncMock(return_value=[]))
        engine.memory = SimpleNamespace(record_assistant_turn=AsyncMock())

        await engine._record_primary_exchange(
            "sess-company",
            "解释当前状态",
            dispatch_text,
            mode="company",
            origin_task_id="ui-anchor",
        )

        engine.memory.record_assistant_turn.assert_awaited_once()
        _, kwargs = engine.memory.record_assistant_turn.await_args
        self.assertEqual(kwargs["content"], dispatch_text)

    async def test_task_mode_followup_restores_runtime_resume_on_reused_task(self) -> None:
        engine = self._build_task_mode_engine()
        existing = Task(
            id="task-existing-runtime",
            title="Task mode execution",
            description="old message",
            assigned_to=TASK_MODE_GENERAL_ROLE_ID,
            project_id="proj1",
            session_id="sess-1",
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "employee_assignment": {"employee_id": "task-general-default-employee"},
                "runtime_v2": {"runtime_session_id": "rt_existing", "task_ledger": [{"content": "keep going"}]},
                "_runtime_v2_user_seeded": True,
            },
        )
        engine.store = SimpleNamespace(
            save_task=AsyncMock(),
            get_task=AsyncMock(return_value=existing),
            get_tasks=AsyncMock(return_value=[existing]),
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]

        await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "Another turn",
            session_id="sess-1",
            origin_task_id="task-existing-runtime",
        )

        reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(reused.context_snapshot["runtime_resume"]["runtime_session_id"], "rt_existing")
        self.assertNotIn("_runtime_v2_user_seeded", reused.metadata)
        self.assertNotIn("employee_assignment", reused.metadata)

    async def test_task_mode_followup_can_reuse_existing_task_by_session_without_origin_task_id(self) -> None:
        engine = self._build_task_mode_engine()
        existing = Task(
            id="task-existing-by-session",
            title="Task mode execution",
            description="old message",
            assigned_to=TASK_MODE_GENERAL_ROLE_ID,
            project_id="proj1",
            session_id="sess-1",
            status=TaskStatus.IDLE,
            metadata={
                "mode": "task",
                "task_mode_contract": "single_full_capability_main_agent",
                "employee_assignment": {"employee_id": "task-general-default-employee"},
                "runtime_v2": {"runtime_session_id": "rt_session"},
            },
        )
        engine.store = SimpleNamespace(
            save_task=AsyncMock(),
            get_task=AsyncMock(return_value=None),
            get_tasks=AsyncMock(return_value=[existing]),
        )
        engine._execute_single_agent = AsyncMock(return_value="executed")  # type: ignore[method-assign]

        await engine._continue_task_mode_execution(
            RouterDecision(mode=ExecutionMode.TASK_MODE, domains=[]),
            "Session-only follow-up",
            session_id="sess-1",
        )

        reused = engine._execute_single_agent.await_args.args[0][0]
        self.assertEqual(reused.id, "task-existing-by-session")
        self.assertEqual(reused.context_snapshot["runtime_resume"]["runtime_session_id"], "rt_session")
        self.assertNotIn("employee_assignment", reused.metadata)

    async def test_explicit_native_forces_native_selection(self) -> None:
        engine = OPCEngine(config=OPCConfig())
        engine.org_engine = SimpleNamespace()

        task = Task(
            title="Implement feature",
            assigned_to="executor",
            metadata={"router_preferred_agent": "native"},
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertIsNone(selected)
        self.assertIsNone(task.assigned_external_agent)
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "forced_native")

    async def test_runtime_refresh_updates_timeout_and_reloads_dependencies(self) -> None:
        with _workspace_tempdir() as opc_home:
            config_dir = opc_home / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "system_config.yaml").write_text(
                yaml.safe_dump(
                    {
                        "system": {
                            "task_mode": {
                                "sub_agent_timeout_sec": 86400,
                            }
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            engine = OPCEngine(config=OPCConfig(), opc_home=opc_home)
            engine.company_executor = SimpleNamespace(work_item_timeout=3600)
            engine.approval_engine = SimpleNamespace(config=engine.config.autonomy)
            engine.adapter_registry = SimpleNamespace(config=None, initialize=AsyncMock())
            engine.org_engine = SimpleNamespace(
                config=None,
                reload_from_config=MagicMock(),
                configure_task_mode_tools=MagicMock(),
            )

            await engine._refresh_runtime_config_from_disk()

            self.assertEqual(engine.company_executor.work_item_timeout, 86400)
            engine.adapter_registry.initialize.assert_awaited_once()
            engine.org_engine.reload_from_config.assert_called_once()
            engine.org_engine.configure_task_mode_tools.assert_called_once()

    async def test_auto_mode_keeps_external_agents_for_complex_work(self) -> None:
        engine = OPCEngine(config=OPCConfig())
        engine.org_engine = SimpleNamespace()
        engine._available_external_agents = lambda: ["codex"]  # type: ignore[method-assign]
        role = SimpleNamespace(
            role_id="executor",
            name="Executor",
            responsibility="Engineering implementation, file edits, shell work, and system delivery.",
            preferred_external_agent="codex",
            runtime_policy={"execution_strategy": "auto", "default_turn_type": "work"},
        )
        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            assigned_to="executor",
            metadata={},
        )

        selected = await engine._assign_task_execution_agent(task, role=role)

        self.assertEqual(selected, "codex")
        self.assertEqual(task.assigned_external_agent, "codex")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "fallback_rules")


class AdapterRegistryConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_agents_are_not_marked_available(self) -> None:
        config = AgentsConfig()
        for agent_config in config.agents.values():
            agent_config.enabled = False
        registry = AdapterRegistry(config)

        patches = [
            patch.object(ClaudeCodeAdapter, "is_available", AsyncMock(return_value=True)),
            patch.object(CursorAdapter, "is_available", AsyncMock(return_value=True)),
            patch.object(CodexAdapter, "is_available", AsyncMock(return_value=True)),
            patch.object(OpenCodeAdapter, "is_available", AsyncMock(return_value=True)),
        ]
        with patches[0], patches[1], patches[2], patches[3]:
            await registry.initialize()

        self.assertEqual(registry.list_available(), [])
        self.assertIsNone(registry.get("codex"))


if __name__ == "__main__":
    unittest.main()
