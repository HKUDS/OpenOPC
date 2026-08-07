from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.config import get_project_workplace
from opc.core.models import ExecutionMode, RouterDecision, SessionRecord
from opc.core.models import Task
from opc.engine import OPCEngine
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager
from tests._temp_paths import WorkspaceTemporaryDirectory, workspace_path

_REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


def setUpModule() -> None:
    tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory  # type: ignore[assignment]


def tearDownModule() -> None:
    tempfile.TemporaryDirectory = _REAL_TEMPORARY_DIRECTORY  # type: ignore[assignment]


class _StubStore:
    def __init__(self, sessions: dict[str, SessionRecord] | None = None) -> None:
        self.sessions = dict(sessions or {})

    async def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    async def save_session(self, session: SessionRecord) -> None:
        self.sessions[session.session_id] = session


class EngineSessionDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_output_dir_reuses_session_default(self) -> None:
        sticky_root = workspace_path("existing-workspace", "app", base="session-defaults")
        engine = OPCEngine(opc_home=workspace_path("opc-test", base="session-defaults"), project_id="proj1")
        engine.store = _StubStore(
            {
                "sess-1": SessionRecord(
                    session_id="sess-1",
                    project_id="proj1",
                    metadata={
                        "execution_defaults": {
                            "target_output_dir": str(sticky_root),
                        }
                    },
                )
            }
        )

        resolved = await engine._resolve_target_output_dir("请继续完善这个项目", "sess-1")

        self.assertEqual(resolved, str(sticky_root))

    async def test_resolve_workspace_contract_defaults_to_project_workplace_without_guessing_output_dir(self) -> None:
        approved_root = workspace_path("approved-root", base="session-defaults")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            opc_home = Path(tmpdir)
            engine = OPCEngine(opc_home=opc_home, project_id="proj1")
            engine.store = _StubStore()
            engine.secretary_policies = SecretaryPolicyManager(opc_home)
            engine.secretary_policies.add_rule(
                "workspace_guardrails",
                {
                    "allowed_roots": [str(approved_root)],
                    "risky_tool_names": ["file_write", "shell_exec"],
                },
                project_id="proj1",
            )

            contract = await engine._resolve_workspace_contract("请继续完善这个项目", "sess-2")

            expected = str(get_project_workplace("proj1").resolve(strict=False))
            self.assertEqual(contract["workspace_root"], expected)
            self.assertEqual(contract["comms_workspace_root"], expected)
            self.assertEqual(contract["output_root"], "")

    async def test_resolve_target_output_dir_returns_none_until_run_scopes_an_output_root(self) -> None:
        approved_root = workspace_path("approved-root-2", base="session-defaults")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            opc_home = Path(tmpdir)
            engine = OPCEngine(opc_home=opc_home, project_id="proj1")
            engine.store = _StubStore()
            engine.secretary_policies = SecretaryPolicyManager(opc_home)
            engine.secretary_policies.add_rule(
                "workspace_guardrails",
                {
                    "allowed_roots": [str(approved_root)],
                    "risky_tool_names": ["file_write", "shell_exec"],
                },
                project_id="proj1",
            )

            resolved = await engine._resolve_target_output_dir("请继续完善这个项目", "sess-3")

            self.assertIsNone(resolved)

    async def test_resolve_workspace_contract_reuses_existing_session_output_root(self) -> None:
        approved_root = workspace_path("approved-root-3", base="session-defaults")
        deliverable_root = approved_root / "chosen-project"
        engine = OPCEngine(opc_home=workspace_path("opc-test-2", base="session-defaults"), project_id="proj1")
        engine.store = _StubStore(
            {
                "sess-4": SessionRecord(
                    session_id="sess-4",
                    project_id="proj1",
                    metadata={
                        "execution_defaults": {
                            "workspace_root": str(approved_root),
                            "comms_workspace_root": str(approved_root),
                            "target_output_dir": str(deliverable_root),
                        }
                    },
                )
            }
        )

        contract = await engine._resolve_workspace_contract("继续", "sess-4")

        self.assertEqual(contract["workspace_root"], str(approved_root))
        self.assertEqual(contract["comms_workspace_root"], str(approved_root))
        self.assertEqual(contract["output_root"], str(deliverable_root))

    async def test_company_mode_defaults_resume_for_followup_requests(self) -> None:
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
            "继续给这个项目再加一个排行榜页面",
        )

        self.assertEqual(updated.mode, ExecutionMode.COMPANY_MODE)
        self.assertEqual(updated.company_profile, "corporate")
        self.assertEqual(updated.preferred_agent, "native")

    async def test_explicit_single_agent_request_overrides_company_default(self) -> None:
        engine = OPCEngine()
        decision = RouterDecision(
            mode=ExecutionMode.SINGLE_AGENT,
            preferred_agent="native",
            domains=[],
        )

        updated = engine._apply_session_execution_defaults(
            decision,
            {
                "mode": ExecutionMode.COMPANY_MODE.value,
                "company_profile": "corporate",
                "preferred_agent": "codex",
            },
            "这次用 single agent 模式直接改一下",
        )

        self.assertEqual(updated.mode, ExecutionMode.SINGLE_AGENT)
        self.assertEqual(updated.preferred_agent, "native")

    def test_resolve_progress_identity_prefers_role_task_for_work_item_projection(self) -> None:
        engine = OPCEngine()
        task = Task(
            id="work-item-task-1",
            title="CEO Intake",
            session_id="work-item-session-1",
            parent_session_id="work-item-session-1",
            assigned_to="ceo",
            metadata={
                "origin_task_id": "root-task-1",
                "work_item_projection_id": "ceo__intake",
                "employee_assignment": {"name": "CEO Default Employee"},
                "work_item_runtime": True,
            },
        )

        progress_task_id, agent_role_id, agent_name = engine._resolve_progress_identity(task)

        self.assertEqual(progress_task_id, "work-item-task-1")
        self.assertEqual(agent_role_id, "ceo")
        self.assertEqual(agent_name, "CEO Default Employee")


if __name__ == "__main__":
    unittest.main()
