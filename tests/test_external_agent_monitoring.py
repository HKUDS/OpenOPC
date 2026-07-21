from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.config import ExternalAgentConfig
from opc.core.models import (
    AgentStatus,
    ApprovalAction,
    ApprovalDecision,
    DelegationRoleSession,
    DelegationWorkItem,
    ExecutionMode,
    Phase,
    RiskLevel,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer1_perception.context_assembler import ContextAssembler, ExternalContextLayers
from opc.layer2_organization import comms as file_comms
from opc.layer2_organization.prompt_contract import make_prompt_contract
from opc.layer2_organization.company_mode import serialize_company_work_item_runtime_plan
from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.adapters.claude_code import ClaudeCodeAdapter
from opc.layer3_agent.adapters.codex_adapter import CodexAdapter
from opc.layer3_agent.adapters.cursor_adapter import CursorAdapter
from opc.layer3_agent.adapters.opencode_adapter import OpenCodeAdapter
from opc.layer3_agent.adapters.base import ExternalAgentAdapter, ExternalApprovalRequest
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer5_memory.skill_library import SkillLibrary


class _SessionStoreStub:
    def __init__(self) -> None:
        self.sessions: list[object] = []

    async def save_external_session(self, session) -> None:
        self.sessions.append(session)

    async def get_external_session(
        self,
        agent_type: str,
        project_id: str = "default",
        *,
        opc_session_id: str | None = None,
        task_id: str | None = None,
    ):
        matches = [
            session
            for session in self.sessions
            if session.agent_type == agent_type
            and session.project_id == project_id
            and (opc_session_id is None or session.opc_session_id == opc_session_id)
            and (task_id is None or session.task_id == task_id)
        ]
        return matches[-1] if matches else None

    async def close(self) -> None:
        return None


class _ApprovalStub:
    async def authorize_external_action(self, task, agent_name, metadata, on_progress=None):
        return True, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="test",
        )

    async def authorize_tool_call(self, task, tool_name, arguments, metadata=None, on_progress=None):
        return True, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="test",
            metadata=metadata or {},
        )


def _make_test_dir(name: str) -> str:
    path = Path.cwd() / ".tmp-test" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return str(path)


def _cleanup_test_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _process_is_running(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _ApprovalPromptStub:
    def __init__(self, *, allow_tool: bool = True, human_reply: str = "approve_once") -> None:
        self.allow_tool = allow_tool
        self.human_reply = human_reply
        self.external_calls: list[dict[str, object]] = []
        self.tool_calls: list[dict[str, object]] = []

    async def authorize_external_action(self, task, agent_name, metadata, on_progress=None):
        self.external_calls.append({"agent_name": agent_name, "metadata": metadata})
        return True, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="external ok",
            confidence=1.0,
            policy_source="test_external",
            metadata=metadata,
        )

    async def authorize_tool_call(self, task, tool_name, arguments, metadata=None, on_progress=None):
        self.tool_calls.append(
            {"tool_name": tool_name, "arguments": arguments, "metadata": metadata or {}}
        )
        action = ApprovalAction.AUTO_APPROVE if self.allow_tool else ApprovalAction.REJECT
        return self.allow_tool, ApprovalDecision(
            action=action,
            risk_level=RiskLevel.LOW if self.allow_tool else RiskLevel.MEDIUM,
            rationale="tool ok" if self.allow_tool else "tool denied",
            confidence=1.0,
            policy_source="test_tool",
            metadata={**(metadata or {}), "human_reply": self.human_reply},
        )


class _ScriptAdapter(ExternalAgentAdapter):
    agent_type = "script_agent"
    default_command = sys.executable

    def __init__(self, script: str, idle_timeout_seconds: int = 900) -> None:
        super().__init__()
        self.script = script
        self.config.command = sys.executable
        self.config.idle_timeout_seconds = idle_timeout_seconds
        self.config.status_heartbeat_seconds = 1
        self._process = None

    async def is_available(self) -> bool:
        return True

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        raise NotImplementedError("Broker monitoring path should be used")

    def build_invocation(self, task: Task, workspace_path: str | None = None):
        cmd = [self.configured_command(), "-c", self.script]
        return cmd, self.build_invocation_metadata(cmd)

    async def get_status(self) -> AgentStatus:
        if self._process and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE


class _PromptScriptAdapter(_ScriptAdapter):
    def __init__(
        self,
        script: str,
        request_line: str,
        approval_request: ExternalApprovalRequest,
    ) -> None:
        super().__init__(script)
        self.request_line = request_line
        self.approval_request = approval_request
        self.config.run_mode = "interactive"

    def supports_interactive(self) -> bool:
        return True

    def stdin_policy_for_process(self, cmd: list[str], metadata: dict[str, Any] | None = None) -> str:
        _ = cmd
        _ = metadata
        return "pipe_open"

    def build_interactive_invocation(self, task: Task, workspace_path: str | None = None):
        return self.build_invocation(task, workspace_path=workspace_path)

    def parse_approval_request(self, text: str, stream_name: str) -> ExternalApprovalRequest | None:
        _ = stream_name
        if text.strip() == self.request_line:
            return self.approval_request
        return None

    def format_approval_response(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: ApprovalDecision,
    ) -> str:
        _ = request
        _ = decision
        return "ALLOW\n" if approved else "DENY\n"


class _CollabSurfaceScriptAdapter(_ScriptAdapter):
    def __init__(self, script: str, *, home_slug: str | None = "script") -> None:
        super().__init__(script)
        self.home_slug = home_slug
        self.started_commands: list[list[str]] = []
        self.started_envs: list[dict[str, str]] = []
        self.started_tasks: list[Task | None] = []

    def agent_isolation_home_slug(self) -> str | None:
        return self.home_slug

    async def start_process(
        self,
        cmd: list[str],
        workspace_path: str,
        extra_env: dict[str, str] | None = None,
        task: Task | None = None,
        launch_metadata: dict[str, object] | None = None,
    ) -> asyncio.subprocess.Process:
        self.started_commands.append(list(cmd))
        self.started_envs.append(dict(extra_env or {}))
        self.started_tasks.append(task)
        return await super().start_process(
            cmd,
            workspace_path,
            extra_env=extra_env,
            task=task,
            launch_metadata=launch_metadata,
        )


class _FatalOutputScriptAdapter(_ScriptAdapter):
    def __init__(self, script: str, fatal_text: str, reason: str) -> None:
        super().__init__(script)
        self.fatal_text = fatal_text
        self.reason = reason
        self.config.run_mode = "interactive"

    def detect_runtime_failure(self, text: str, stream_name: str) -> str | None:
        _ = stream_name
        if self.fatal_text in text:
            return self.reason
        return None


class ExternalAgentMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_agent_audit_uses_sanitized_display_command(self) -> None:
        engine = OPCEngine()
        progress: list[str] = []
        prompt = "hello\nFULL ROLE PROMPT\n" + ("x" * 220)
        full_command = f"opencode run --format json {prompt!r}"

        async def capture_progress(text: str) -> None:
            progress.append(text)

        with patch("opc.engine.logger.info") as info_log:
            await engine._emit_external_agent_audit(
                Task(title="Ask OpenCode"),
                {
                    "agent": "opencode",
                    "model": "opencode/minimax-m2.5-free",
                    "session_mode": "auto",
                    "new_session": False,
                    "command": full_command,
                    "display_command": "opencode run --format json '<prompt:240-chars>'",
                },
                "/tmp/work",
                capture_progress,
            )

        self.assertEqual(len(progress), 1)
        self.assertIn("opencode run --format json '<prompt:240-chars>'", progress[0])
        self.assertIn("<prompt:", progress[0])
        self.assertNotIn(full_command, progress[0])
        self.assertNotIn("FULL ROLE PROMPT", progress[0])
        info_log.assert_not_called()

    def test_external_trace_helpers_are_opt_in(self) -> None:
        task = Task(id="trace-task", title="trace task")
        adapter = OpenCodeAdapter()
        started_at = datetime(2026, 5, 12, 12, 0, 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {}, clear=False), \
                patch("opc.layer3_agent.external_broker.get_opc_home", return_value=Path(tmpdir)):
                os.environ.pop("OPC_EXTERNAL_AGENT_TRACE", None)
                self.assertIsNone(ExternalAgentBroker._external_trace_path(adapter, task, started_at))

                os.environ["OPC_EXTERNAL_AGENT_TRACE"] = "1"
                trace_path = ExternalAgentBroker._external_trace_path(adapter, task, started_at)
                self.assertIsNotNone(trace_path)
                assert trace_path is not None
                ExternalAgentBroker._write_external_trace_line(
                    trace_path,
                    adapter=adapter,
                    task=task,
                    stream_name="stdout",
                    text='{"type":"tool_use"}\n',
                )

            payload = json.loads(trace_path.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["agent"], "opencode")
            self.assertEqual(payload["task_id"], "trace-task")
            self.assertEqual(payload["stream"], "stdout")
            self.assertEqual(payload["text"], '{"type":"tool_use"}')

    def test_external_tool_progress_bypasses_stream_throttle(self) -> None:
        self.assertTrue(
            ExternalAgentBroker._should_emit_stream_progress(
                "[External:opencode:tool] $ ls -la",
                now_monotonic=10.0,
                last_progress_monotonic=9.9,
            )
        )
        self.assertFalse(
            ExternalAgentBroker._should_emit_stream_progress(
                "[External:opencode:thinking] reading files",
                now_monotonic=10.0,
                last_progress_monotonic=9.9,
            )
        )

    async def test_runtime_session_save_skips_closed_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            await store.close()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            adapter = _ScriptAdapter("print('noop')\n")
            task = Task(id="task-closed-store", title="Closed", project_id="proj1")

            await broker._save_runtime_session(
                adapter,
                task,
                tmpdir,
                "runtime-session",
                "cancelled",
                metadata={},
                extra={},
            )

            await broker._persist_session(
                adapter,
                task,
                tmpdir,
                metadata={},
                result=TaskResult(status=TaskStatus.FAILED, content="cancelled"),
            )

    def test_collect_external_unread_messages_normalizes_worker_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            layout = file_comms.resolve_layout(tmpdir, "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Approve plan",
                body="Please approve the staged rollout.",
                extra_frontmatter={
                    "message_class": "protocol",
                    "protocol_type": "approval_request",
                },
            )
            task = Task(
                id="task-1",
                session_id="work-item-session",
                parent_session_id="root-session",
                project_id="proj1",
                assigned_to="executor",
                metadata={"target_output_dir": tmpdir},
            )

            unread = ExternalAgentBroker._collect_external_unread_messages(task)

        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["message_class"], "protocol")
        self.assertEqual(unread[0]["protocol_type"], "approval_request")
        self.assertEqual(unread[0]["origin_task_id"], "task-1")
        self.assertTrue(unread[0]["worker_id"])

    async def test_broker_work_item_inbox_updates_do_not_archive_new_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            adapter = _ScriptAdapter("print('noop')\n")
            task = Task(
                id="work-item-inbox",
                title="work item inbox",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="executor",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "executor",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
                context_snapshot={},
            )
            await store.save_task(task)
            layout = file_comms.resolve_layout(tmpdir, "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Review needed",
                body="Please validate the rollout notes.",
            )

            fresh = await broker._queue_external_inbox_updates(
                adapter=adapter,
                task=task,
                workspace_path=tmpdir,
                session_id="runtime-session",
                metadata={},
                seen_ids=set(),
            )

            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]["subject"], "Review needed")
            self.assertTrue(list(layout.role_new_dir("executor").glob("*.md")))
            self.assertFalse(list(layout.role_seen_dir("executor").glob("*.md")))
            refreshed = await store.get_task(task.id)
            assert refreshed is not None
            self.assertEqual(len(list(refreshed.context_snapshot.get("broker_pending_inbox", []) or [])), 1)
            await store.close()

    async def test_active_external_agent_is_not_killed_by_idle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="demo", project_id="proj1")
            adapter = _ScriptAdapter(
                "import sys,time\n"
                "for i in range(4):\n"
                "    print(f\"tick {i}\")\n"
                "    sys.stdout.flush()\n"
                "    time.sleep(0.4)\n",
                idle_timeout_seconds=1,
            )
            progress: list[str] = []

            async def on_progress(text: str) -> None:
                progress.append(text)

            result = await broker.run(adapter, task, tmpdir, on_progress=on_progress)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.DONE, result.content)
            self.assertIn("tick 0", result.content)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.DONE.value)
            self.assertGreaterEqual(int(session.metadata.get("activity_count", 0)), 1)
            self.assertTrue(any("started pid=" in text for text in progress))
            await store.close()

    async def test_external_agent_long_single_line_output_does_not_crash_monitor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="long line", project_id="proj1")
            adapter = _ScriptAdapter(
                "import json, sys\n"
                "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'x' * 70000}}))\n"
                "sys.stdout.flush()\n",
            )
            adapter.config.run_mode = "interactive"

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertIn('"type": "item.completed"', result.content)
            self.assertIn('"agent_message"', result.content)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.DONE.value)
            await store.close()

    async def test_external_agent_fast_failure_drains_stdout_and_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="fast fail", project_id="proj1")
            adapter = _ScriptAdapter(
                "import sys\n"
                "sys.stdout.write('fast stdout\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stderr.write('fast stderr\\n')\n"
                "sys.stderr.flush()\n"
                "sys.exit(1)\n",
            )

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("fast stdout", result.content)
            self.assertIn("fast stderr", result.content)
            await store.close()

    async def test_broker_persists_provider_resume_session_id_from_external_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(id="provider-session", title="provider session", project_id="proj1")
            adapter = _ScriptAdapter(
                "import json, sys\n"
                "print(json.dumps({'sessionID': 'ses_1'}))\n"
                "sys.stdout.flush()\n",
            )
            adapter.config.run_mode = "interactive"

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(result.artifacts["resume_session_id"], "ses_1")
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.session_id, "ses_1")
            self.assertEqual(session.metadata.get("resume_session_id"), "ses_1")
            await store.close()

    async def test_live_provider_thread_is_durable_before_stop_checkpoint_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            role_session_id = "role-runtime::run-live::executor"
            parent_session_id = "company-live-session"
            await store.save_delegation_role_session(DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="run-live",
                role_id="executor",
                seat_id="seat-live",
            ))
            await store.save_task(Task(
                id="ui-live",
                title="Company chat",
                session_id=parent_session_id,
                project_id="proj1",
                status=TaskStatus.IDLE,
                metadata={"exec_mode": "company", "company_profile": "corporate"},
            ))
            work_item = DelegationWorkItem(
                work_item_id="work-live",
                run_id="run-live",
                role_id="executor",
                seat_id="seat-live",
                role_runtime_session_id=role_session_id,
                title="Live work",
                projection_id="live",
                phase=Phase.RUNNING,
                claimed_by_role_runtime_session_id=role_session_id,
                claimed_by_seat_id="seat-live",
                metadata={"work_item_projection_id": "live"},
            )
            await store.save_delegation_work_item(work_item)
            plan = CompanyWorkItemRuntimePlan(
                profile="corporate",
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                },
            )
            task = Task(
                id="live-provider-task",
                title="Live provider task",
                session_id="live-child-session",
                parent_session_id=parent_session_id,
                project_id="proj1",
                assigned_to="executor",
                assigned_external_agent="script_agent",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_runtime": True,
                    "work_item_projection_id": "live",
                    "delegation_run_id": "run-live",
                    "delegation_role_session_id": role_session_id,
                    "delegation_seat_id": "seat-live",
                    "selected_execution_agent": "script_agent",
                    "selected_execution_agent_source": "fallback_rules",
                    "company_profile": "corporate",
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                },
            )
            set_linked_work_item_id(task, work_item.work_item_id)
            await store.save_task(task)
            await store.link_work_item_runtime_task(work_item.work_item_id, task.id)

            broker = ExternalAgentBroker(store, _ApprovalStub())
            adapter = _ScriptAdapter(
                "import json,sys,time\n"
                "print(json.dumps({'sessionID': 'provider-live-thread'}))\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n"
            )
            run_task = asyncio.create_task(broker.run(adapter, task, tmpdir))
            role_state = None
            for _ in range(200):
                role_state = await store.get_role_session_adapter_state(
                    role_session_id,
                    "script_agent",
                )
                if role_state and role_state.get("resume_session_id"):
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(role_state)
            assert role_state is not None
            self.assertEqual(
                role_state["resume_session_id"],
                "provider-live-thread",
            )
            self.assertFalse(run_task.done())

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            await engine.suspend_company_runtime(
                origin_task_id="ui-live",
                session_id=parent_session_id,
                reason="user_stop",
            )
            checkpoint = (
                await store.get_pending_checkpoints(
                    project_id="proj1",
                    session_id=parent_session_id,
                    checkpoint_types=["company_runtime_suspended"],
                )
            )[0]
            captured = checkpoint.payload["external_sessions"][task.id]
            self.assertEqual(
                captured["resume_session_id"],
                "provider-live-thread",
            )
            self.assertEqual(
                captured["provider_session_id"],
                "provider-live-thread",
            )

            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            resumed_task = await store.get_task(task.id)
            assert resumed_task is not None
            resume_adapter = _ScriptAdapter("print('unused')")
            resume_adapter.config.resume_session_flag = "--resume"
            await broker._restore_session_resume_from_store(
                resume_adapter,
                resumed_task,
            )
            self.assertEqual(resume_adapter.config.session_mode, "resume")
            self.assertEqual(
                resume_adapter.config.session_id,
                "provider-live-thread",
            )
            await store.close()

    async def test_codex_thread_started_stream_restores_real_resume_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            role_session_id = "role-runtime::codex-live::executor"
            await store.save_delegation_role_session(DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="codex-live",
                project_id="proj1",
                role_id="executor",
                seat_id="seat-codex-live",
            ))
            task = Task(
                id="codex-live-task",
                title="Codex live task",
                description="Keep the same Codex thread.",
                project_id="proj1",
                session_id="codex-live-child",
                parent_session_id="codex-live-parent",
                assigned_to="executor",
                assigned_external_agent="codex",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_runtime": True,
                    "delegation_role_session_id": role_session_id,
                    "delegation_seat_id": "seat-codex-live",
                },
            )
            await store.save_task(task)

            class ScriptedCodexAdapter(CodexAdapter):
                async def start_process(
                    self,
                    cmd: list[str],
                    workspace_path: str,
                    extra_env: dict[str, str] | None = None,
                    task: Task | None = None,
                    launch_metadata: dict[str, object] | None = None,
                ) -> asyncio.subprocess.Process:
                    return await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        (
                            "import json,sys,time\n"
                            "print(json.dumps({'type':'thread.started','thread_id':'thread-real-codex'}))\n"
                            "sys.stdout.flush()\n"
                            "time.sleep(30)\n"
                        ),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=workspace_path,
                    )

            broker = ExternalAgentBroker(store, _ApprovalStub())
            live_adapter = ScriptedCodexAdapter()
            live_adapter.config.run_mode = "exec"
            run_task = asyncio.create_task(
                broker.run(live_adapter, task, tmpdir)
            )
            state = None
            for _ in range(200):
                state = await store.get_role_session_adapter_state(
                    role_session_id,
                    "codex",
                )
                if state and state.get("resume_session_id"):
                    break
                await asyncio.sleep(0.01)
            assert state is not None
            self.assertEqual(state["resume_session_id"], "thread-real-codex")

            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            resume_adapter = CodexAdapter()
            resumed_task = await store.get_task(task.id)
            assert resumed_task is not None
            await broker._restore_session_resume_from_store(
                resume_adapter,
                resumed_task,
            )
            self.assertEqual(resume_adapter.config.session_mode, "resume")
            self.assertEqual(resume_adapter.config.session_id, "thread-real-codex")
            cmd, _metadata = resume_adapter.build_invocation(
                resumed_task,
                workspace_path=tmpdir,
            )
            self.assertEqual(cmd[1:3], ["exec", "resume"])
            self.assertIn("thread-real-codex", cmd)
            self.assertEqual(cmd[-1], "-")
            await store.close()

    async def test_cleanup_cancellation_terminalizes_checkpoint_provider_token(self) -> None:
        """A Stop checkpoint's working token cannot outlive a failed process."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            role_session_id = "role-runtime::cleanup-race::executor"
            await store.save_delegation_role_session(DelegationRoleSession(
                role_session_id=role_session_id,
                run_id="cleanup-race",
                project_id="proj1",
                role_id="executor",
                seat_id="seat-cleanup-race",
            ))
            task = Task(
                id="cleanup-race-task",
                title="Cleanup cancellation race",
                description="Do not resume a provider thread that later failed.",
                project_id="proj1",
                session_id="cleanup-race-child",
                parent_session_id="cleanup-race-parent",
                assigned_to="executor",
                assigned_external_agent="codex",
                status=TaskStatus.RUNNING,
                metadata={"delegation_role_session_id": role_session_id},
            )
            await store.save_task(task)
            cleanup_started = asyncio.Event()
            allow_cleanup = asyncio.Event()

            class CleanupRaceCodexAdapter(CodexAdapter):
                async def start_process(
                    self,
                    cmd: list[str],
                    workspace_path: str,
                    extra_env: dict[str, str] | None = None,
                    task: Task | None = None,
                    launch_metadata: dict[str, object] | None = None,
                ) -> asyncio.subprocess.Process:
                    return await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        (
                            "import json,sys,time\n"
                            "print(json.dumps({'type':'thread.started','thread_id':'thread-cleanup-race'}))\n"
                            "sys.stdout.flush()\n"
                            "time.sleep(.2)\n"
                            "sys.exit(7)\n"
                        ),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=workspace_path,
                    )

                async def cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
                    cleanup_started.set()
                    await allow_cleanup.wait()
                    await super().cleanup_process(proc)

            broker = ExternalAgentBroker(store, _ApprovalStub())
            live_adapter = CleanupRaceCodexAdapter()
            run_task = asyncio.create_task(broker.run(live_adapter, task, tmpdir))
            checkpoint_session = None
            for _ in range(200):
                checkpoint_session = await store.get_external_session(
                    "codex", "proj1", task_id=task.id
                )
                if (
                    checkpoint_session is not None
                    and checkpoint_session.status == "working"
                    and checkpoint_session.metadata.get("resume_session_id")
                    == "thread-cleanup-race"
                ):
                    break
                await asyncio.sleep(0.01)
            assert checkpoint_session is not None
            self.assertEqual(checkpoint_session.status, "working")

            await asyncio.wait_for(cleanup_started.wait(), timeout=5)
            run_task.cancel()
            await asyncio.sleep(0)
            allow_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            terminal_session = await store.get_external_session(
                "codex", "proj1", task_id=task.id
            )
            assert terminal_session is not None
            self.assertEqual(terminal_session.status, "failed")
            self.assertGreater(
                terminal_session.updated_at,
                checkpoint_session.updated_at,
            )

            task.metadata.update({
                "work_item_runtime": True,
                "external_resume_session_id": "thread-cleanup-race",
                "external_resume_agent_type": "codex",
                "external_resume_session_scope_id": "cleanup-race-parent",
                "external_resume_checkpoint_session_updated_at": (
                    checkpoint_session.updated_at.isoformat()
                ),
                "external_resume_checkpoint_session_status": "working",
            })
            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            resume_adapter, _resume_metadata = (
                await engine._configure_external_adapter_for_task(
                    task,
                    CodexAdapter(),
                )
            )
            self.assertEqual(resume_adapter.config.session_mode, "new")
            self.assertEqual(resume_adapter.config.session_id, "")
            self.assertEqual(
                task.metadata.get("external_resume_fallback"),
                "context_replay_provider_terminal",
            )
            await store.close()

    async def test_silent_external_agent_times_out_with_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="idle", project_id="proj1")
            adapter = _ScriptAdapter("import time\ntime.sleep(2)\n", idle_timeout_seconds=1)

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("startup timed out after 1s", result.content)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.FAILED.value)
            self.assertIn("startup timed out after 1s", session.metadata.get("failure_reason", ""))
            self.assertEqual(session.metadata.get("timeout_kind"), "startup")
            self.assertIn("startup timed out after 1s", session.metadata.get("timeout_reason", ""))
            await store.close()

    async def test_silent_interactive_agent_hits_startup_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(
                title="silent startup",
                project_id="proj1",
                metadata={
                    "external_startup_timeout_seconds": 1,
                    "external_idle_timeout_seconds": 10,
                    "external_hard_timeout_seconds": 20,
                },
            )
            adapter = _ScriptAdapter("import time\ntime.sleep(2)\n", idle_timeout_seconds=10)
            adapter.config.run_mode = "interactive"

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("startup timed out after 1s", result.content)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.FAILED.value)
            self.assertIn("startup timed out after 1s", session.metadata.get("failure_reason", ""))
            self.assertEqual(session.metadata.get("timeout_kind"), "startup")
            self.assertIn("startup timed out after 1s", session.metadata.get("timeout_reason", ""))
            await store.close()

    async def test_busy_external_agent_honors_hard_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(
                title="busy hard timeout",
                project_id="proj1",
                metadata={
                    "external_startup_timeout_seconds": 1,
                    "external_idle_timeout_seconds": 10,
                    "external_hard_timeout_seconds": 1,
                },
            )
            adapter = _ScriptAdapter(
                "import sys,time\n"
                "print('tick 0')\n"
                "sys.stdout.flush()\n"
                "time.sleep(2)\n",
                idle_timeout_seconds=10,
            )
            adapter.config.run_mode = "interactive"

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("execution timed out after 1s", result.content)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.FAILED.value)
            self.assertIn("execution timed out after 1s", session.metadata.get("failure_reason", ""))
            await store.close()

    async def test_windows_process_cleanup_uses_taskkill_process_tree(self) -> None:
        class _Proc:
            pid = 123
            returncode = None

            def __init__(self) -> None:
                self.terminated = False
                self.killed = False

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> int:
                self.returncode = 1
                return self.returncode

        run_result = SimpleNamespace(returncode=0, stdout=b"SUCCESS", stderr=b"")
        proc = _Proc()

        with patch("opc.layer3_agent.external_broker.os.name", "nt"), \
            patch("opc.layer3_agent.external_broker.subprocess.run", return_value=run_result) as run_mock:
            result = await ExternalAgentBroker._terminate_process(proc)  # type: ignore[arg-type]

        run_mock.assert_called_once_with(
            ["taskkill", "/PID", "123", "/T", "/F"],
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result["method"], "taskkill_tree")
        self.assertEqual(result["taskkill_returncode"], 0)
        self.assertEqual(result["returncode_after"], 1)
        self.assertTrue(result["ok"])
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)

    async def test_cancelled_broker_run_kills_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="cancel me", project_id="proj1")
            adapter = _ScriptAdapter("import time\ntime.sleep(30)\n", idle_timeout_seconds=30)
            adapter.config.run_mode = "interactive"

            run_task = asyncio.create_task(broker.run(adapter, task, tmpdir))
            await asyncio.sleep(0.2)

            self.assertIsNotNone(adapter._process)
            pid = adapter._process.pid

            run_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run_task

            await asyncio.sleep(0.1)
            self.assertIsNone(adapter._process)
            self.assertFalse(_process_is_running(pid))

            session = await store.get_external_session("script_agent", "proj1")
            self.assertIsNotNone(session)
            self.assertEqual(session.status, "cancelled")
            await store.close()

    async def test_execute_task_marks_task_cancelled_when_run_is_cancelled(self) -> None:
        engine = OPCEngine()

        class _TaskStore:
            def __init__(self) -> None:
                self.tasks: dict[str, Task] = {}

            async def save_task(self, task: Task) -> None:
                self.tasks[task.id] = task

            async def get_task(self, task_id: str) -> Task | None:
                return self.tasks.get(task_id)

        store = _TaskStore()
        engine.store = store

        task = Task(id="cancelled-task", title="cancelled task", project_id="proj1")
        await store.save_task(task)

        async def _cancelled_run(_task: Task) -> TaskResult:
            raise asyncio.CancelledError()

        engine._run_task_once = _cancelled_run  # type: ignore[method-assign]

        with self.assertRaises(asyncio.CancelledError):
            await engine._execute_task(task)

        saved = await store.get_task(task.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, TaskStatus.CANCELLED)

    async def test_engine_records_external_failure_reason(self) -> None:
        engine = OPCEngine()
        progress: list[str] = []

        class _Adapter:
            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return ["demo"], {"command": "demo"}

        adapter = _Adapter()
        task = Task(
            title="needs fallback",
            project_id="proj1",
            assigned_external_agent="codex",
            metadata={"target_output_dir": "/tmp/out"},
        )

        async def build_external_agent_task(original: Task) -> Task:
            return original

        async def run_native_agent(original: Task) -> TaskResult:
            return TaskResult(status=TaskStatus.DONE, content="native ok", artifacts={})

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                return TaskResult(status=TaskStatus.FAILED, content="Codex idle timed out after 900s", artifacts={})

        async def on_progress(text: str) -> None:
            progress.append(text)

        engine.external_broker = _Broker()
        engine.on_progress = on_progress
        engine._get_external_candidates = lambda _task: [("codex", adapter)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]
        engine._run_native_agent = run_native_agent  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        attempt = result.artifacts["external_attempts"][0]
        self.assertEqual(attempt["failure_reason"], "Codex idle timed out after 900s")
        self.assertTrue(any("reason=Codex idle timed out after 900s" in text for text in progress))

    async def test_engine_scopes_external_progress_to_child_task(self) -> None:
        engine = OPCEngine()
        progress: list[tuple[str, dict[str, object]]] = []

        class _Adapter:
            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return ["demo"], {"command": "demo", "agent": "codex"}

        adapter = _Adapter()
        task = Task(
            id="work-item-task-1",
            title="external work item",
            project_id="proj1",
            session_id="child-session-1",
            parent_session_id="parent-session-1",
            assigned_to="executor",
            assigned_external_agent="codex",
            metadata={
                "target_output_dir": "/tmp/out",
                "employee_assignment": {"name": "Backend Engineer"},
            },
        )

        async def build_external_agent_task(original: Task) -> Task:
            original.description = "FULL ROLE PROMPT\n\n## Task Brief\nExecute the assigned work."
            return original

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                if on_progress:
                    await on_progress("[External:codex:stdout] planning patch")
                    await on_progress("[External status] codex still running")
                return TaskResult(status=TaskStatus.DONE, content="external ok", artifacts={})

        async def on_progress(text: str, **kw) -> None:
            progress.append((text, kw))

        engine.external_broker = _Broker()
        engine.on_progress = on_progress
        engine._get_external_candidates = lambda _task: [("codex", adapter)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.DONE)
        external_progress = [item for item in progress if item[0].startswith("[External")]
        self.assertGreaterEqual(len(external_progress), 2)
        self.assertTrue(all(item[1].get("task_id") == "work-item-task-1" for item in external_progress))
        self.assertTrue(all(item[1].get("agent_role_id") == "executor" for item in external_progress))
        self.assertTrue(all(item[1].get("agent_name") == "Backend Engineer" for item in external_progress))
        delegation_messages = [text for text, _ in progress if text.startswith("[Delegating to codex]")]
        self.assertTrue(delegation_messages)
        self.assertNotIn("FULL ROLE PROMPT", delegation_messages[0])
        self.assertNotIn("## Task Brief", delegation_messages[0])

    async def test_engine_keeps_provider_resume_session_id_for_future_external_followups(self) -> None:
        engine = OPCEngine()

        class _Adapter:
            agent_type = "codex"
            config = SimpleNamespace(
                command="codex",
                model="",
                model_flag="--model",
                session_mode="auto",
                session_id="",
                new_session_flag="",
                resume_session_flag="",
                run_mode="interactive",
                idle_timeout_seconds=900,
                status_heartbeat_seconds=30,
                extra_args=[],
            )

            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return ["codex", "exec", task.title], {"command": "codex exec", "agent": "codex"}

        adapter = _Adapter()
        task = Task(
            id="task-provider-session",
            title="followup",
            project_id="proj1",
            session_id="sess-1",
            assigned_external_agent="codex",
            metadata={"target_output_dir": "/tmp/out"},
        )

        async def build_external_agent_task(original: Task) -> Task:
            return original

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                return TaskResult(
                    status=TaskStatus.DONE,
                    content="external ok",
                    artifacts={
                        "session_id": "codex:proj1:task-provider-session",
                        "resume_session_id": "thread_1",
                    },
                )

        engine.external_broker = _Broker()
        engine.on_progress = AsyncMock()
        engine._get_external_candidates = lambda _task: [("codex", adapter)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]
        engine._configure_external_adapter_for_task = AsyncMock(return_value=(adapter, {}))  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(task.metadata["external_resume_session_id"], "thread_1")
        self.assertEqual(task.metadata["external_resume_agent_type"], "codex")

    async def test_engine_does_not_persist_resume_token_from_failed_external_attempt(self) -> None:
        engine = OPCEngine()

        class _Adapter:
            agent_type = "codex"
            config = SimpleNamespace(
                command="codex",
                model="",
                model_flag="--model",
                session_mode="auto",
                session_id="",
                new_session_flag="",
                resume_session_flag="",
                run_mode="interactive",
                idle_timeout_seconds=900,
                status_heartbeat_seconds=30,
                extra_args=[],
            )

            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return ["codex", "exec", task.title], {"command": "codex exec", "agent": "codex"}

        adapter = _Adapter()
        task = Task(
            id="task-failed-provider-session",
            title="followup",
            project_id="proj1",
            session_id="sess-1",
            assigned_external_agent="codex",
            metadata={"target_output_dir": "/tmp/out"},
        )

        async def build_external_agent_task(original: Task) -> Task:
            return original

        async def run_native(native_task: Task) -> TaskResult:
            _ = native_task
            return TaskResult(status=TaskStatus.DONE, content="native ok", artifacts={})

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content="external failed",
                    artifacts={
                        "session_id": "codex:proj1:task-failed-provider-session",
                        "resume_session_id": "thread_failed",
                    },
                )

        engine.external_broker = _Broker()
        engine.on_progress = AsyncMock()
        engine._get_external_candidates = lambda _task: [("codex", adapter)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]
        engine._configure_external_adapter_for_task = AsyncMock(return_value=(adapter, {}))  # type: ignore[method-assign]
        engine._run_native_agent = run_native  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertNotIn("external_resume_session_id", task.metadata)
        self.assertNotIn("external_resume_agent_type", task.metadata)

    async def test_engine_ignores_resume_token_when_agent_type_mismatches(self) -> None:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = SimpleNamespace(
            get_latest_external_session_for_task=AsyncMock(return_value=None),
            get_external_session=AsyncMock(return_value=None),
        )
        task = Task(
            id="task-mismatched-resume-agent",
            project_id="proj1",
            session_id="child-session",
            parent_session_id="parent-session",
            assigned_external_agent="codex",
            metadata={
                "work_item_runtime": True,
                "delegation_role_session_id": "role-runtime::run-1::ceo",
                "external_resume_session_id": "claude-session-id",
                "external_resume_agent_type": "claude_code",
            },
        )
        adapter = CodexAdapter(config=ExternalAgentConfig(command="codex"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "auto")
        self.assertEqual(run_adapter.config.session_id, "")
        self.assertEqual(resume_metadata, {})

    async def test_engine_ignores_legacy_untyped_resume_token_without_agent_state(self) -> None:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = SimpleNamespace(
            get_latest_external_session_for_task=AsyncMock(return_value=None),
            get_external_session=AsyncMock(return_value=None),
        )
        task = Task(
            id="task-untyped-resume-token",
            project_id="proj1",
            session_id="child-session",
            parent_session_id="parent-session",
            assigned_external_agent="codex",
            metadata={
                "work_item_runtime": True,
                "delegation_role_session_id": "role-runtime::run-1::ceo",
                "external_resume_session_id": "legacy-session-id",
            },
        )
        adapter = CodexAdapter(config=ExternalAgentConfig(command="codex"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "auto")
        self.assertEqual(run_adapter.config.session_id, "")
        self.assertEqual(resume_metadata, {})

    async def test_engine_skips_collaboration_context_when_resuming_external_session_without_rework_delta(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "SHOULD NOT APPEAR"

        engine.context_assembler = _Assembler()
        task = Task(
            id="resume-task",
            title="今天美国开盘吗？大概几点",
            description="今天美国开盘吗？大概几点",
            project_id="proj1",
            metadata={"__external_resume_session": True},
        )

        external_task = await engine._build_external_agent_task(task)

        self.assertEqual(external_task.description, "今天美国开盘吗？大概几点")
        self.assertNotIn("Collaboration Context", external_task.description)

    async def test_engine_injects_memory_skill_into_task_mode_external_prompt(self) -> None:
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
            engine = OPCEngine(opc_home=root, project_id="proj1")
            engine.skills = SkillLibrary(root)
            engine.skills.load_all("proj1")
            task = Task(
                title="External task",
                description="Do the work.",
                project_id="proj1",
                metadata={"execution_mode": ExecutionMode.TASK_MODE.value},
            )

            external_task = await engine._build_external_agent_task(task)

            self.assertIn("## Skill: memory", external_task.description)
            self.assertIn(".opc/memory/global.md", external_task.description)
            self.assertIn(f"OPC_GLOBAL_MEMORY_PATH={root / 'memory' / 'global.md'}", external_task.description)
            self.assertIn(f"OPC_PROJECT_MEMORY_PATH={root / 'memory' / 'projects' / 'proj1.md'}", external_task.description)

    async def test_task_mode_external_prompts_include_memory_once_for_all_adapters(self) -> None:
        from opc.layer5_memory.memory_manager import MemoryManager

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
            memory = MemoryManager(root, "proj1")
            memory.save_memory("## Smoke Context\n- GLOBAL_ONLY_IN_MEMORY\n", project=False)
            memory.save_memory("## Smoke Context\n- PROJECT_ONLY_IN_MEMORY\n", project=True)
            engine = OPCEngine(opc_home=root, project_id="proj1")
            engine.memory = memory
            engine.context_assembler = ContextAssembler(memory=memory, store=None)
            engine.skills = SkillLibrary(root)
            engine.skills.load_all("proj1")
            task = Task(
                title="External task",
                description="Use smoke context.",
                project_id="proj1",
                metadata={"execution_mode": ExecutionMode.TASK_MODE.value},
            )

            external_task = await engine._build_external_agent_task(task)

            self.assertEqual(
                external_task.metadata["external_prompt_contract"],
                "description_is_full_prompt",
            )
            for adapter in (CodexAdapter(), ClaudeCodeAdapter(), CursorAdapter(), OpenCodeAdapter()):
                prompt = adapter.build_task_prompt(external_task)
                self.assertEqual(prompt, external_task.description, adapter.agent_type)
                self.assertEqual(prompt.count("## Skill: memory"), 1, adapter.agent_type)
                self.assertEqual(prompt.count("GLOBAL_ONLY_IN_MEMORY"), 1, adapter.agent_type)
                self.assertEqual(prompt.count("PROJECT_ONLY_IN_MEMORY"), 1, adapter.agent_type)
                self.assertIn(".opc/memory/global.md", prompt)
                self.assertIn(".opc/memory/projects/<current_project_id>.md", prompt)
                self.assertEqual(prompt.count("OPC_GLOBAL_MEMORY_PATH="), 1, adapter.agent_type)
                self.assertEqual(prompt.count("OPC_PROJECT_MEMORY_PATH="), 1, adapter.agent_type)
                self.assertIn(str(root / "memory" / "global.md"), prompt)
                self.assertIn(str(root / "memory" / "projects" / "proj1.md"), prompt)

    async def test_engine_injects_memory_skill_only_for_user_facing_final_decider_external_prompt(self) -> None:
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
            engine = OPCEngine(opc_home=root, project_id="proj1")
            engine.skills = SkillLibrary(root)
            engine.skills.load_all("proj1")
            worker = Task(
                title="Worker",
                description="Do worker work.",
                project_id="proj1",
                assigned_to="engineer",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "final_decider_role_id": "ceo",
                    "user_visible": True,
                },
            )
            final_decider = Task(
                title="Final",
                description="Talk to user.",
                project_id="proj1",
                assigned_to="ceo",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "final_decider_role_id": "ceo",
                    "user_visible": True,
                },
            )

            worker_task = await engine._build_external_agent_task(worker)
            final_task = await engine._build_external_agent_task(final_decider)

            self.assertNotIn("## Skill: memory", worker_task.description)
            self.assertIn("## Skill: memory", final_task.description)

    async def test_engine_stages_uploaded_attachments_for_external_resume_prompt(self) -> None:
        engine = OPCEngine()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            engine.opc_home = root / "opc-home"
            engine.project_id = "proj1"
            source = engine.opc_home / "projects" / "proj1" / "attachments" / "att-text" / "notes.txt"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("uploaded secret", encoding="utf-8")
            workspace = root / "workspace"
            ref = {
                "attachment_id": "att-text",
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "size_bytes": len("uploaded secret"),
                "disk_path": "projects/proj1/attachments/att-text/notes.txt",
            }
            task = Task(
                id="resume-task",
                title="Read the uploaded note",
                description="Read the uploaded note",
                project_id="proj1",
                metadata={
                    "__external_resume_session": True,
                    "workspace_root": str(workspace),
                    "attachment_refs": [ref],
                },
            )

            external_task = await engine._build_external_agent_task(task)

            staged_files = list((workspace / ".opc-attachments").rglob("*notes.txt"))
            self.assertEqual(len(staged_files), 1)
            self.assertEqual(staged_files[0].read_text(encoding="utf-8"), "uploaded secret")
            self.assertIn("## Runtime Context", external_task.description)
            self.assertIn("Agent path:", external_task.description)
            self.assertIn("Workspace relative path:", external_task.description)
            self.assertIn("uploaded secret", external_task.description)
            self.assertEqual(
                Path(external_task.metadata["external_attachment_refs"][0]["agent_path"]).resolve(),
                staged_files[0].resolve(),
            )

    async def test_engine_prepends_reviewer_delta_when_resuming_external_session_rework(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "SHOULD NOT APPEAR"

            async def build_rework_feedback_context(self, task: Task) -> str:
                _ = task
                return (
                    "## Reviewer Feedback (Rework Required)\n"
                    "Reviewer: cmo\n"
                    "Rework attempt: #2\n\n"
                    "### Reviewer's Reject Reason\n"
                    "Remove unsupported claims and unify canonical naming."
                )

        class _Store:
            async def get_delegation_work_item(self, work_item_id: str):
                _ = work_item_id
                return SimpleNamespace(
                    metadata={
                        "rework_feedback": "Remove unsupported claims and unify canonical naming.",
                        "review_feedback_version": 2,
                        "review_owner_role_id": "cmo",
                        "review_rework_count": 2,
                        "structured_review_verdict": {
                            "label": "reject",
                            "summary": "claim cleanup required",
                            "blocking_issues": ["Remove unsupported claims."],
                            "followups": ["Unify canonical naming."],
                        },
                    }
                )

        engine.context_assembler = _Assembler()
        engine.store = _Store()
        task = Task(
            id="resume-task-rework",
            title="继续修宣传稿",
            description="继续沿用已有外部 session 完成本阶段。",
            project_id="proj1",
            metadata={
                "__external_resume_session": True,
                "external_resume_review_feedback_version": 1,
                "external_resume_review_feedback_digest": "stale",
            },
        )
        set_linked_work_item_id(task, "wi-1")

        external_task = await engine._build_external_agent_task(task)

        self.assertIn("## Task Brief", external_task.description)
        self.assertIn("## External Resume Delta", external_task.description)
        self.assertIn("## Reviewer Delta (MANDATORY NEW CONTEXT)", external_task.description)
        self.assertIn("review_feedback_version: 2", external_task.description)
        self.assertIn("Remove unsupported claims and unify canonical naming.", external_task.description)
        self.assertNotIn("SHOULD NOT APPEAR", external_task.description)
        self.assertNotIn("## Collaboration Context", external_task.description)
        self.assertEqual(external_task.metadata["external_resume_review_feedback_version"], 2)

    async def test_company_resume_rework_injects_reviewer_feedback_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="wi-rework",
                        run_id="run-1",
                        cell_id="team::cto",
                        role_id="engineer",
                        seat_id="seat::team::cto::engineer",
                        title="Worker slice",
                        summary="Worker summary",
                        phase=Phase.RUNNING,
                        metadata={
                            "runtime_model": "multi_team_org",
                            "execution_mode": "company_mode",
                            "prompt_contract": make_prompt_contract(
                                task_brief="Fix the worker slice.",
                                deliverables=["fixed slice"],
                            ),
                            "rework_feedback": "Fix the missing asset manifest.",
                            "review_feedback_version": 2,
                            "review_rework_count": 2,
                            "review_owner_role_id": "cto",
                        },
                    )
                )
                engine = OPCEngine()
                engine.store = store
                memory = SimpleNamespace(build_memory_context=AsyncMock(return_value=""))
                engine.context_assembler = ContextAssembler(memory=memory, store=store)
                task = Task(
                    id="company-resume-rework",
                    title="Resume worker",
                    description="legacy task body must not own company Task Brief",
                    project_id="proj1",
                    assigned_to="engineer",
                    metadata={
                        "__external_resume_session": True,
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "external_resume_review_feedback_version": 1,
                        "external_resume_review_feedback_digest": "stale",
                    },
                )
                set_linked_work_item_id(task, "wi-rework")

                external_task = await engine._build_external_agent_task(task)

                self.assertIn("## External Resume Delta", external_task.description)
                self.assertEqual(external_task.description.count("## Reviewer Feedback (Rework Required)"), 1)
                self.assertIn("Fix the missing asset manifest.", external_task.description)
                self.assertIn("## Task Brief\nFix the worker slice.", external_task.description)
                self.assertNotIn("legacy task body must not own company Task Brief", external_task.description)
            finally:
                await store.close()

    async def test_engine_does_not_duplicate_reviewer_delta_for_same_feedback_version(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_rework_feedback_context(self, task: Task) -> str:
                _ = task
                return "## Reviewer Feedback (Rework Required)\nfix it"

        class _Store:
            async def get_delegation_work_item(self, work_item_id: str):
                _ = work_item_id
                return SimpleNamespace(
                    metadata={
                        "rework_feedback": "fix it",
                        "review_feedback_version": 3,
                    }
                )

        engine.context_assembler = _Assembler()
        engine.store = _Store()
        task = Task(
            id="resume-task-same-feedback",
            title="resume",
            description="plain task body",
            project_id="proj1",
            metadata={
                "__external_resume_session": True,
                "external_resume_review_feedback_version": 3,
                "external_resume_review_feedback_digest": hashlib.sha1("fix it".encode("utf-8")).hexdigest(),
            },
        )
        set_linked_work_item_id(task, "wi-2")

        external_task = await engine._build_external_agent_task(task)

        self.assertEqual(external_task.description, "plain task body")

    async def test_engine_prepends_shared_runtime_contract_for_external_company_dispatch_turn(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context_layers(self, task: Task, role_id: str = "") -> ExternalContextLayers:
                _ = (task, role_id)
                return ExternalContextLayers(
                    primary_task_brief="Frame the mission and route it.",
                    company_runtime_context="## Topology\n### Direct Reports\n- cto (CTO)",
                    prepared_mailbox_context="### Mailbox\nMailbox is prepared.",
                )

        engine.context_assembler = _Assembler()
        task = Task(
            id="dispatch-task",
            title="CEO Dispatch",
            description="Frame the mission and route it.",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "direct_report_seat_ids": ["seat::team::ceo::cto"],
            },
        )

        external_task = await engine._build_external_agent_task(task)

        self.assertTrue(external_task.description.startswith("## Runtime Contract (MANDATORY)"))
        self.assertIn("## Organization Runtime Contract", external_task.description)
        self.assertIn("## Manager Runtime Contract", external_task.description)
        self.assertIn("## Dispatch Planning Contract", external_task.description)
        self.assertNotIn("## Leader Delegation Planning Overlay", external_task.description)
        self.assertIn("Scope first", external_task.description)
        self.assertIn("requested deliverable form", external_task.description)
        self.assertIn("outcome-based child WorkItems", external_task.description)
        self.assertIn("startable preparation", external_task.description)
        self.assertIn("must not replace requested production work", external_task.description)
        self.assertIn("NO_DELEGATION_JUSTIFICATION", external_task.description)
        self.assertIn("## Task Brief", external_task.description)
        self.assertIn("## Company Runtime Context", external_task.description)
        self.assertIn("## Collaboration Context", external_task.description)
        self.assertNotIn("## Prepared Mailbox Context", external_task.description)
        self.assertIn("## Topology", external_task.description)
        self.assertIn("### Mailbox", external_task.description)
        self.assertIn("Mailbox is prepared.", external_task.description)
        self.assertNotIn("agent_spawn(profile='explore')", external_task.description)
        self.assertNotIn("file_read", external_task.description)
        self.assertNotIn("git_*", external_task.description)
        self.assertNotIn("OpenOPC task execution agent", external_task.description)
        self.assertNotIn("## Core Operating Principles", external_task.description)
        self.assertNotIn("## Native Working Contract", external_task.description)
        self.assertNotIn("Tool Strategy", external_task.description)

    async def test_engine_dispatch_contract_uses_allowed_delegate_role_fallback(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "## Topology\n### Direct Reports\n- cto (CTO)"

        engine.context_assembler = _Assembler()
        task = Task(
            id="dispatch-fallback-task",
            title="CEO Dispatch",
            description="Frame the mission and route it.",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "allowed_delegate_role_ids": ["cto", "cmo"],
            },
        )

        external_task = await engine._build_external_agent_task(task)

        self.assertIn("## Dispatch Planning Contract", external_task.description)
        self.assertIn("## Manager Runtime Contract", external_task.description)
        self.assertNotIn("Leader Delegation Planning Overlay", external_task.description)
        self.assertIn("hard dependencies", external_task.description)
        self.assertIn("NO_DELEGATION_JUSTIFICATION", external_task.description)

    async def test_engine_resume_company_task_still_gets_runtime_contract_and_assignment_context(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context_layers(self, task: Task, role_id: str = "") -> ExternalContextLayers:
                _ = (task, role_id)
                return ExternalContextLayers(
                    primary_task_brief="CTO must decompose the implementation work for direct reports.",
                    company_runtime_context=(
                        "## Work Item Assignment Context\n"
                        "### Upstream Intent Summary\n"
                        "Build the multilingual realtime translator app.\n"
                        "### Manager Planning Handoff\n"
                        "CEO mapped app, marketing, and QA outcomes."
                    ),
                )

        engine.context_assembler = _Assembler()
        task = Task(
            id="resume-company-manager",
            title="CTO Dispatch",
            description="Continue the manager turn.",
            project_id="proj1",
            assigned_to="cto",
            metadata={
                "__external_resume_session": True,
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "delegation_seat_id": "seat::team::ceo::cto",
                "direct_report_seat_ids": ["seat::team::cto::senior_engineer"],
            },
        )
        set_linked_work_item_id(task, "cto-work-item")

        external_task = await engine._build_external_agent_task(task)

        self.assertTrue(external_task.description.startswith("## Runtime Contract (MANDATORY)"))
        self.assertIn("Manager Runtime Contract", external_task.description)
        self.assertIn("Dispatch Planning Contract", external_task.description)
        self.assertNotIn("Leader Delegation Planning Overlay", external_task.description)
        self.assertIn("## Task Brief", external_task.description)
        self.assertIn("CTO must decompose the implementation work for direct reports.", external_task.description)
        self.assertIn("## Company Runtime Context", external_task.description)
        self.assertIn("## Work Item Assignment Context", external_task.description)
        self.assertNotIn("Original User Request", external_task.description)
        self.assertIn("Upstream Intent Summary", external_task.description)
        self.assertIn("Manager Planning Handoff", external_task.description)
        self.assertNotIn("Assigned Work Item Brief", external_task.description)

    async def test_engine_adds_opc_collab_cli_instructions_for_company_mode(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "## Topology\n### Direct Reports\n- cto (CTO)"

        engine.context_assembler = _Assembler()
        task = Task(
            id="codex-company-hints",
            title="CEO Dispatch",
            description="Frame the mission and route it.",
            project_id="proj1",
            assigned_to="ceo",
            assigned_external_agent="codex",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "work_item_turn_type": "intake",
                "managed_team_id": "team::ceo",
                "delegation_seat_id": "seat::team::ceo::ceo",
            },
            context_snapshot={},
        )
        set_linked_work_item_id(task, "wi-ceo-current")

        external_task = await engine._build_external_agent_task(task)

        # OpenOPC-spawned agents (codex, claude, opencode, cursor) all use
        # the `opc-collab` CLI. The prompt deliberately teaches only that
        # supported collaboration surface.
        self.assertIn("## Company Runtime Context", external_task.description)
        self.assertIn("## Collaboration Context", external_task.description)
        self.assertIn("### Collaboration Tools", external_task.description)
        self.assertIn("opc-collab <tool> --args-stdin", external_task.description)
        self.assertIn("Allowed tools this turn:", external_task.description)
        self.assertIn("WorkItem ID is the collaboration identity", external_task.description)
        self.assertIn("`$OPC_WORK_ITEM_ID` is already set for this run", external_task.description)
        self.assertIn("never use `$OPC_TASK_ID`", external_task.description)
        self.assertNotIn("Current `OPC_WORK_ITEM_ID`", external_task.description)
        self.assertNotIn("wi-ceo-current", external_task.description)
        self.assertIn("- `delegate_work`", external_task.description)
        self.assertIn("- `manager_board_read`", external_task.description)
        self.assertIn("- `send_dm`", external_task.description)
        self.assertIn("Primary argument contracts", external_task.description)
        self.assertNotIn("```bash", external_task.description)
        self.assertNotIn("opc-collab manager_board_read --args-json '{", external_task.description)
        self.assertIn("`manager_board_read` arguments: `parent_work_item_id`, `include_children`", external_task.description)
        self.assertIn(
            "omit `parent_work_item_id`; attention/monitor turns automatically resolve to the underlying business parent board",
            external_task.description,
        )
        self.assertNotIn("<FULL_PARENT_WORK_ITEM_ID>", external_task.description)
        self.assertIn("Do NOT use legacy aliases such as `parent_id`", external_task.description)
        self.assertIn("`delegate_work` item fields: `role_id` (required), `title` (required)", external_task.description)
        self.assertIn("`task_brief` owns the child prompt's Task Brief", external_task.description)
        self.assertIn("`scope_key` is optional but idempotent on a manager board", external_task.description)
        self.assertNotIn("## Core Operating Principles", external_task.description)
        self.assertNotIn("## Native Working Contract", external_task.description)
        self.assertNotIn("Tool Strategy", external_task.description)
        # Prompt must not mention unsupported collaboration transports.
        self.assertNotIn("Fallback", external_task.description)
        self.assertNotIn("broker", external_task.description)
        self.assertNotIn("tool_search", external_task.description)

    async def test_report_prompt_uses_minimal_profile_and_target_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                target_contract = make_prompt_contract(
                    task_brief="Produce the render script and manifest.",
                    deliverables=["render.py", "asset_manifest.json"],
                    acceptance_criteria=["render command succeeds"],
                    non_overlap_guard="Do not rewrite marketing copy.",
                )
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="report::wi-worker::v1",
                        run_id="run-1",
                        cell_id="team::cto",
                        role_id="engineer",
                        seat_id="seat::team::cto::engineer",
                        title="Report worker slice",
                        summary="Write handoff report.",
                        kind="report",
                        phase=Phase.RUNNING,
                        metadata={
                            "runtime_model": "multi_team_org",
                            "execution_mode": "company_mode",
                            "report_execution_work_item": True,
                            "current_turn_mode": "report_required",
                            "report_target_work_item_id": "wi-worker",
                            "report_target_prompt_contract": target_contract,
                            "prompt_contract": make_prompt_contract(
                                task_brief="Write a structured handoff report.",
                                target_contract=target_contract,
                            ),
                        },
                    )
                )
                engine = OPCEngine()
                engine.store = store
                memory = SimpleNamespace(build_memory_context=AsyncMock(return_value=""))
                engine.context_assembler = ContextAssembler(memory=memory, store=store)
                task = Task(
                    id="report-task",
                    title="Report",
                    description="legacy report task body",
                    project_id="proj1",
                    assigned_to="engineer",
                    assigned_external_agent="codex",
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "report_execution_work_item": True,
                        "current_turn_mode": "report_required",
                        "secretary_context": "SECRETARY SHOULD NOT APPEAR",
                        "report_source_summary": "Implemented render.py and generated asset_manifest.json.",
                        "report_source_evidence": {
                            "artifact_manifest": [
                                {"kind": "file", "label": "Renderer", "value": "render.py"},
                            ],
                            "verification_results": {
                                "status": {"label": "verified", "summary": "Render command passed."},
                                "checks": [
                                    {"command": "python render.py", "status": "pass", "summary": "Manifest emitted."},
                                ],
                            },
                            "output_paths": ["asset_manifest.json"],
                        },
                        "runtime_topology": {"seats": [{"role_id": "engineer"}]},
                    },
                )
                set_linked_work_item_id(task, "report::wi-worker::v1")

                external_task = await engine._build_external_agent_task(task)

                self.assertIn("## Work Item Turn: Report Generation", external_task.description)
                self.assertIn("## Task Brief\nWrite a structured handoff report.", external_task.description)
                self.assertIn("### Work Item Contract To Report Against", external_task.description)
                self.assertIn("Produce the render script and manifest.", external_task.description)
                self.assertIn("render command succeeds", external_task.description)
                self.assertIn("### Last Execute-Turn Summary", external_task.description)
                self.assertIn("Implemented render.py", external_task.description)
                self.assertIn("### Verification Evidence From Execute Turn", external_task.description)
                self.assertIn("python render.py", external_task.description)
                self.assertNotIn("## Team Collaboration (opc-collab CLI)", external_task.description)
                self.assertNotIn("## Collaboration Context", external_task.description)
                self.assertNotIn("## Prepared Mailbox Context", external_task.description)
                self.assertNotIn("## Topology", external_task.description)
                self.assertNotIn("## Team Memory", external_task.description)
                self.assertNotIn("SECRETARY SHOULD NOT APPEAR", external_task.description)
                self.assertNotIn("legacy report task body", external_task.description)
            finally:
                await store.close()

    async def test_engine_adds_cli_instructions_for_non_codex_company_agents(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "plain context"

        engine.context_assembler = _Assembler()
        task = Task(
            id="claude-company-no-codex-hints",
            title="CEO Dispatch",
            description="Frame the mission and route it.",
            project_id="proj1",
            assigned_to="ceo",
            assigned_external_agent="claude_code",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
                "work_item_turn_type": "intake",
                "managed_team_id": "team::ceo",
                "delegation_seat_id": "seat::team::ceo::ceo",
            },
            context_snapshot={},
        )

        external_task = await engine._build_external_agent_task(task)

        self.assertIn("## Collaboration Context", external_task.description)
        self.assertIn("### Collaboration Tools", external_task.description)
        self.assertIn("opc-collab <tool> --args-stdin", external_task.description)
        self.assertIn("- `delegate_work`", external_task.description)
        self.assertNotIn("```bash", external_task.description)
        # No unrelated tool-search language regardless of agent type.
        self.assertNotIn("tool_search", external_task.description)
        self.assertNotIn("Fallback", external_task.description)

    async def test_engine_omits_opc_collab_cli_outside_company_mode(self) -> None:
        engine = OPCEngine()

        class _Assembler:
            async def build_external_context(self, task: Task, role_id: str = "") -> str:
                _ = (task, role_id)
                return "plain context"

        engine.context_assembler = _Assembler()
        task = Task(
            id="task-mode-no-collab-cli",
            title="Task Mode Run",
            description="Handle the task directly.",
            project_id="proj1",
            assigned_to="task_generalist",
            assigned_external_agent="codex",
            metadata={
                "execution_mode": "task_mode",
                "work_item_role_id": "task_generalist",
            },
            context_snapshot={},
        )

        external_task = await engine._build_external_agent_task(task)

        self.assertNotIn("## Team Collaboration", external_task.description)
        self.assertNotIn("opc-collab", external_task.description)
        self.assertIn("## Task Brief", external_task.description)
        self.assertIn("## OpenOPC Context", external_task.description)
        self.assertNotIn("## Collaboration Context", external_task.description)
        self.assertNotIn("OpenOPC task execution agent", external_task.description)
        self.assertNotIn("## Core Operating Principles", external_task.description)
        self.assertNotIn("## Native Working Contract", external_task.description)
        self.assertNotIn("Tool Strategy", external_task.description)

    async def test_user_denied_external_agent_falls_back_directly_to_native(self) -> None:
        engine = OPCEngine()
        progress: list[str] = []
        attempted_agents: list[str] = []
        native_calls = 0

        class _Adapter:
            def __init__(self, name: str) -> None:
                self.name = name

            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return [self.name], {"command": self.name, "agent": self.name}

        codex = _Adapter("codex")
        claude = _Adapter("claude_code")
        task = Task(
            title="denied external",
            project_id="proj1",
            assigned_external_agent="codex",
            metadata={"target_output_dir": "/tmp/out"},
        )

        async def build_external_agent_task(original: Task) -> Task:
            return original

        async def run_native_agent(original: Task) -> TaskResult:
            nonlocal native_calls
            native_calls += 1
            return TaskResult(status=TaskStatus.DONE, content="native ok", artifacts={})

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                attempted_agents.append(adapter.name)
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content="External action blocked by autonomy policy: User decision: deny",
                    artifacts={
                        "approval": {
                            "action": ApprovalAction.REJECT.value,
                            "policy_source": "human_escalation",
                            "rationale": "User decision: deny",
                        }
                    },
                )

        async def on_progress(text: str, **kw) -> None:
            progress.append(text)

        engine.external_broker = _Broker()
        engine.on_progress = on_progress
        engine._get_external_candidates = lambda _task: [("codex", codex), ("claude_code", claude)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]
        engine._run_native_agent = run_native_agent  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(attempted_agents, ["codex"])
        self.assertEqual(native_calls, 1)
        self.assertTrue(any(text.startswith("[External agent denied]") for text in progress))

    async def test_external_agent_waiting_for_user_review_does_not_fall_back(self) -> None:
        engine = OPCEngine()
        attempted_agents: list[str] = []

        class _Adapter:
            def __init__(self, name: str) -> None:
                self.name = name

            def build_invocation(self, task: Task, workspace_path: str | None = None):
                return [self.name], {"command": self.name, "agent": self.name}

        codex = _Adapter("codex")
        claude = _Adapter("claude_code")
        task = Task(
            title="awaiting approval",
            project_id="proj1",
            assigned_external_agent="codex",
            metadata={"target_output_dir": "/tmp/out"},
        )

        async def build_external_agent_task(original: Task) -> Task:
            return original

        async def run_native_agent(original: Task) -> TaskResult:
            raise AssertionError("native fallback should not happen while waiting for user review")

        class _Broker:
            async def run(self, adapter, task, workspace_path, on_progress=None):
                attempted_agents.append(adapter.name)
                return TaskResult(
                    status=TaskStatus.AWAITING_REVIEW,
                    content="External action blocked by autonomy policy: Awaiting user input.",
                    artifacts={
                        "requires_user_input": True,
                        "approval": {
                            "action": ApprovalAction.REQUIRE_INPUT.value,
                            "policy_source": "human_escalation",
                            "rationale": "Awaiting user input.",
                        }
                    },
                )

        engine.external_broker = _Broker()
        engine._get_external_candidates = lambda _task: [("codex", codex), ("claude_code", claude)]  # type: ignore[method-assign]
        engine._resolve_external_workspace = lambda _task: "/tmp/out"  # type: ignore[method-assign]
        engine._build_external_agent_task = build_external_agent_task  # type: ignore[method-assign]
        engine._run_native_agent = run_native_agent  # type: ignore[method-assign]

        result = await engine._run_task_once(task)

        self.assertEqual(result.status, TaskStatus.AWAITING_REVIEW)
        self.assertEqual(attempted_agents, ["codex"])

    async def test_broker_handles_tool_approval_and_writes_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            approval = _ApprovalPromptStub(allow_tool=True, human_reply="always_global")
            broker = ExternalAgentBroker(store, approval)
            task = Task(title="approval", project_id="proj1")
            request_line = "PROMPT shell_exec"
            adapter = _PromptScriptAdapter(
                "import sys\n"
                "print('PROMPT shell_exec')\n"
                "sys.stdout.flush()\n"
                "reply = sys.stdin.readline().strip()\n"
                "print(f'reply={reply}')\n"
                "sys.stdout.flush()\n",
                request_line=request_line,
                approval_request=ExternalApprovalRequest(
                    approval_scope="tool",
                    action_name="shell_exec",
                    prompt_text="Allow `git status --short`?",
                    arguments={"command": "git status --short"},
                    metadata={"source": "test"},
                ),
            )

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertIn("reply=ALLOW", result.content)
            self.assertEqual(len(approval.external_calls), 1)
            self.assertEqual(len(approval.tool_calls), 1)
            self.assertEqual(approval.tool_calls[0]["tool_name"], "shell_exec")
            self.assertEqual(approval.tool_calls[0]["arguments"]["command"], "git status --short")
            self.assertEqual(approval.tool_calls[0]["arguments"]["working_directory"], tmpdir)
            prompt = result.artifacts["approval_prompts"][0]
            self.assertEqual(prompt["approval_scope"], "tool")
            self.assertEqual(prompt["action_name"], "shell_exec")
            self.assertTrue(prompt["approved"])
            self.assertEqual(prompt["human_reply"], "always_global")
            await store.close()

    async def test_broker_skips_approval_prompts_when_adapter_bridge_is_disabled(self) -> None:
        class _NoBridgePromptAdapter(_PromptScriptAdapter):
            def supports_approval_prompt_handling(self, cmd, metadata=None) -> bool:
                _ = cmd
                _ = metadata
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            approval = _ApprovalPromptStub(allow_tool=True, human_reply="always_global")
            broker = ExternalAgentBroker(store, approval)
            request_line = "PROMPT shell_exec"
            adapter = _NoBridgePromptAdapter(
                "print('PROMPT shell_exec')\n",
                request_line=request_line,
                approval_request=ExternalApprovalRequest(
                    approval_scope="tool",
                    action_name="shell_exec",
                    prompt_text="Allow `type opencode`?",
                    arguments={"command": "type"},
                ),
            )

            result = await broker.run(adapter, Task(title="approval skipped", project_id="proj1"), tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(approval.tool_calls, [])
            self.assertEqual(result.artifacts["approval_prompts"], [])
            self.assertFalse(result.artifacts["approval_prompt_bridge"])
            await store.close()

    async def test_broker_denies_tool_approval_when_policy_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            approval = _ApprovalPromptStub(allow_tool=False, human_reply="deny")
            broker = ExternalAgentBroker(store, approval)
            task = Task(title="approval deny", project_id="proj1")
            request_line = "PROMPT shell_exec"
            adapter = _PromptScriptAdapter(
                "import sys\n"
                "print('PROMPT shell_exec')\n"
                "sys.stdout.flush()\n"
                "reply = sys.stdin.readline().strip()\n"
                "print(f'reply={reply}')\n"
                "sys.stdout.flush()\n",
                request_line=request_line,
                approval_request=ExternalApprovalRequest(
                    approval_scope="tool",
                    action_name="shell_exec",
                    prompt_text="Allow `git push --force`?",
                    arguments={"command": "git push --force"},
                ),
            )

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertIn("reply=DENY", result.content)
            prompt = result.artifacts["approval_prompts"][0]
            self.assertFalse(prompt["approved"])
            self.assertEqual(prompt["decision_action"], ApprovalAction.REJECT.value)
            await store.close()

    async def test_broker_fails_when_approval_response_cannot_be_delivered(self) -> None:
        class _UndeliverablePromptAdapter(_PromptScriptAdapter):
            async def send_process_input(self, proc, text: str) -> bool:
                _ = proc
                _ = text
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            approval = _ApprovalPromptStub(allow_tool=True, human_reply="approve_once")
            broker = ExternalAgentBroker(store, approval)
            adapter = _UndeliverablePromptAdapter(
                "import sys, time\n"
                "print('PROMPT shell_exec')\n"
                "sys.stdout.flush()\n"
                "time.sleep(30)\n",
                request_line="PROMPT shell_exec",
                approval_request=ExternalApprovalRequest(
                    approval_scope="tool",
                    action_name="shell_exec",
                    prompt_text="Allow `git status --short`?",
                    arguments={"command": "git status --short"},
                ),
            )

            result = await broker.run(adapter, Task(title="approval write fail", project_id="proj1"), tmpdir)

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("approval_response_not_delivered", result.content)
            prompt = result.artifacts["approval_prompts"][0]
            self.assertFalse(prompt["response_sent"])
            self.assertIn("approval_response_not_delivered", prompt["failure_reason"])
            await store.close()

    async def test_broker_passes_prepared_launch_task_into_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            adapter = _CollabSurfaceScriptAdapter("print('ok')", home_slug=None)

            async def _prepare(task: Task) -> Task:
                return Task(
                    title=task.title,
                    description=f"{task.description}\n\n## Collaboration Context\nexpanded context",
                    project_id=task.project_id,
                )

            broker = ExternalAgentBroker(store, _ApprovalStub(), task_preparer=_prepare)
            task = Task(title="launch", description="base body", project_id="proj1")

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertEqual(len(adapter.started_tasks), 1)
            started_task = adapter.started_tasks[0]
            self.assertIsNotNone(started_task)
            assert started_task is not None
            self.assertIn("## Collaboration Context", started_task.description)
            self.assertIn("expanded context", started_task.description)
            await store.close()

    async def test_broker_fails_fast_on_fatal_runtime_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            task = Task(title="stdin wait", project_id="proj1")
            reason = "agent entered stdin wait mode"
            adapter = _FatalOutputScriptAdapter(
                "import sys,time\n"
                "print('Reading additional input from stdin...')\n"
                "sys.stdout.flush()\n"
                "time.sleep(5)\n",
                fatal_text="Reading additional input from stdin...",
                reason=reason,
            )

            result = await broker.run(adapter, task, tmpdir)
            session = await store.get_external_session("script_agent", "proj1")

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertEqual(result.content, reason)
            self.assertIsNotNone(session)
            self.assertEqual(session.status, TaskStatus.FAILED.value)
            self.assertEqual(session.metadata.get("failure_reason"), reason)
            self.assertEqual(int(session.metadata.get("activity_count", 0)), 0)
            await store.close()

    def test_codex_adapter_parses_exec_approval_request(self) -> None:
        adapter = CodexAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "id": "evt_1",
                    "msg": {
                        "type": "exec_approval_request",
                        "call_id": "approval_1",
                        "turn_id": "turn_1",
                        "command": ["bash", "-lc", "git status --short"],
                        "cwd": "/repo",
                        "reason": "Need repo status",
                        "proposed_execpolicy_amendment": {"command": ["git", "status"]},
                    }
                }
            ),
            "stdout",
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.approval_scope, "tool")
        self.assertEqual(request.action_name, "shell_exec")
        self.assertEqual(request.arguments["command"], "git status --short")
        self.assertEqual(request.arguments["working_directory"], "/repo")

    def test_codex_adapter_ignores_completed_command_event_for_approval_parsing(self) -> None:
        adapter = CodexAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "id": "evt_done_1",
                    "msg": {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "command_execution",
                            "command": [
                                "/bin/bash",
                                "-lc",
                                "sed -n '1,220p' /repo/deliverables/plan.md",
                            ],
                            "aggregated_output": "Project root: `/repo/app`\n",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNone(request)

    def test_codex_adapter_ignores_sandbox_error_text_for_approval_parsing(self) -> None:
        adapter = CodexAdapter()
        request = adapter.parse_approval_request(
            (
                "2026-04-06T15:13:00Z ERROR codex_core::tools::router: "
                "error=exec_command failed for `/bin/bash -lc 'python3 -m http.server 4173'`: "
                "SandboxDenied { message: \"PermissionError: [Errno 1] Operation not permitted\" }"
            ),
            "stderr",
        )

        self.assertIsNone(request)

    def test_codex_adapter_formats_structured_approval_response(self) -> None:
        adapter = CodexAdapter()
        request = ExternalApprovalRequest(
            approval_scope="tool",
            action_name="shell_exec",
            prompt_text="Allow `git status --short`?",
            arguments={"command": "git status --short"},
            metadata={
                "provider_event_type": "exec_approval_request",
                "approval_id": "approval_1",
                "turn_id": "turn_1",
                "proposed_execpolicy_amendment": {"command": ["git", "status"]},
            },
        )
        decision = ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="human_escalation",
            metadata={"human_reply": "always_global"},
        )

        payload = json.loads(adapter.format_approval_response(request, True, decision))

        self.assertEqual(payload["op"]["type"], "exec_approval")
        self.assertEqual(payload["op"]["id"], "approval_1")
        self.assertEqual(payload["op"]["turn_id"], "turn_1")
        self.assertEqual(
            payload["op"]["decision"],
            {
                "approved_execpolicy_amendment": {
                    "proposed_execpolicy_amendment": {"command": ["git", "status"]}
                }
            },
        )

    def test_codex_adapter_auto_uses_danger_full_access_sandbox_without_full_auto_flag(self) -> None:
        adapter = CodexAdapter()
        task = Task(title="demo", description="body")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "danger-full-access")
        self.assertNotIn("--full-auto", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("sandbox_workspace_write.network_access=true", cmd)
        self.assertEqual(metadata["approval_mode"], "auto")

    def test_codex_adapter_user_settings_injects_no_approval_flags(self) -> None:
        adapter = CodexAdapter(
            config=ExternalAgentConfig(command="codex", approval_mode="user-settings")
        )
        task = Task(title="demo", description="body")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertNotIn("--full-auto", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertEqual(metadata["approval_mode"], "user-settings")

    def test_codex_adapter_resolves_launch_binary_before_spawn(self) -> None:
        metadata: dict[str, object] = {}

        with patch("opc.layer3_agent.adapters.base.os.name", "posix"), \
            patch("opc.layer3_agent.adapters.base.shutil.which", return_value=r"C:\Tools\codex.cmd"):
            cmd = CodexAdapter._resolve_launch_command(
                ["codex", "exec", "--json", "-"],
                launch_metadata=metadata,
            )

        self.assertEqual(cmd[0], r"C:\Tools\codex.cmd")
        self.assertEqual(metadata["configured_binary"], "codex")
        self.assertEqual(metadata["resolved_binary"], r"C:\Tools\codex.cmd")

    def test_codex_adapter_wraps_windows_cmd_shim_before_spawn(self) -> None:
        metadata: dict[str, object] = {}
        which_results = {
            "codex": r"D:\nodejs\codex",
            "cmd.exe": r"C:\Windows\System32\cmd.exe",
        }

        with patch("opc.layer3_agent.adapters.base.os.name", "nt"), \
            patch("opc.layer3_agent.adapters.base.os.path.isfile", side_effect=lambda path: path == r"D:\nodejs\codex.cmd"), \
            patch("opc.layer3_agent.adapters.base.shutil.which", side_effect=lambda name, path=None: which_results.get(name)), \
            patch.dict(os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}, clear=False):
            cmd = CodexAdapter._resolve_launch_command(
                ["codex", "exec", "--json", "-"],
                launch_metadata=metadata,
            )

        self.assertEqual(
            cmd,
            [
                r"C:\Windows\System32\cmd.exe",
                "/d",
                "/s",
                "/c",
                r"D:\nodejs\codex.cmd",
                "exec",
                "--json",
                "-",
            ],
        )
        self.assertEqual(metadata["configured_binary"], "codex")
        self.assertEqual(metadata["resolved_binary"], r"D:\nodejs\codex.cmd")
        self.assertEqual(metadata["launch_wrapper"], r"C:\Windows\System32\cmd.exe")

    def test_codex_adapter_resolves_launch_binary_with_extra_env_path(self) -> None:
        with patch("opc.layer3_agent.adapters.base.shutil.which", return_value=r"D:\bin\codex.cmd") as which_mock:
            CodexAdapter._resolve_launch_command(
                ["codex", "exec", "-"],
                extra_env={"PATH": r"D:\bin"},
            )

        self.assertEqual(which_mock.call_args.kwargs["path"], r"D:\bin")

    def test_codex_adapter_forwards_only_openopc_runtime_env_to_shell_policy(self) -> None:
        metadata: dict[str, object] = {}

        cmd = CodexAdapter._inject_runtime_shell_environment(
            ["codex", "exec", "--json", "-"],
            {
                "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(["delegate_work", "inbox"]),
                "OPC_COLLAB_RPC_TOKEN": "rpc-token",
                "PYTHONPATH": r"D:\OpenOPC",
                "UNRELATED_SECRET": "do-not-forward",
            },
            launch_metadata=metadata,
        )

        self.assertEqual(cmd[:2], ["codex", "exec"])
        config_values = [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "-c"]
        self.assertIn(
            "shell_environment_policy.set.OPC_ALLOWED_COLLAB_TOOLS='''delegate_work,inbox'''",
            config_values,
        )
        self.assertIn(
            "shell_environment_policy.set.OPC_COLLAB_RPC_TOKEN='''rpc-token'''",
            config_values,
        )
        self.assertIn(
            "shell_environment_policy.set.PYTHONPATH='''D:\\OpenOPC'''",
            config_values,
        )
        self.assertFalse(any("UNRELATED_SECRET" in value for value in config_values))
        self.assertEqual(
            metadata["runtime_shell_environment_keys"],
            ["OPC_ALLOWED_COLLAB_TOOLS", "OPC_COLLAB_RPC_TOKEN", "PYTHONPATH"],
        )

    def test_codex_adapter_full_auto_uses_dangerous_bypass_flag(self) -> None:
        adapter = CodexAdapter(
            config=ExternalAgentConfig(command="codex", approval_mode="full-auto")
        )
        task = Task(title="demo", description="body")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("--full-auto", cmd)
        self.assertEqual(metadata["approval_mode"], "full-auto")

    def test_codex_adapter_builds_exec_resume_command_when_session_is_available(self) -> None:
        adapter = CodexAdapter(
            config=ExternalAgentConfig(
                command="codex",
                session_mode="resume",
                session_id="thread_1",
            )
        )
        task = Task(title="demo", description="body")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[:3], ["codex", "exec", "resume"])
        self.assertIn("--json", cmd)
        self.assertIn("thread_1", cmd)
        self.assertEqual(metadata["session_mode"], "resume")
        self.assertEqual(metadata["session_id"], "thread_1")
        self.assertNotIn("--add-dir", cmd)
        self.assertNotIn("--sandbox", cmd)
        self.assertIn("-c", cmd)
        self.assertIn('sandbox_mode="danger-full-access"', cmd)

    def test_codex_adapter_adds_comms_workspace_root_when_present(self) -> None:
        adapter = CodexAdapter()
        task = Task(
            title="demo",
            description="body",
            metadata={
                "target_output_dir": "/repo/project",
                "comms_workspace_root": "/repo",
            },
        )

        cmd, _ = adapter.build_interactive_invocation(task, workspace_path="/repo/project")

        add_dir_values = [cmd[idx + 1] for idx, value in enumerate(cmd[:-1]) if value == "--add-dir"]
        self.assertIn("/repo/project", add_dir_values)
        self.assertIn("/repo", add_dir_values)

    def test_codex_adapter_resume_prompt_does_not_duplicate_identical_user_message(self) -> None:
        adapter = CodexAdapter(
            config=ExternalAgentConfig(
                command="codex",
                session_mode="resume",
                session_id="thread_1",
            )
        )
        task = Task(title="今天美国开盘吗？大概几点", description="今天美国开盘吗？大概几点")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[-1], "今天美国开盘吗？大概几点")
        self.assertEqual(metadata["prompt_transport"], "argv")
        self.assertEqual(
            metadata["prompt_bytes"],
            len("今天美国开盘吗？大概几点".encode("utf-8")),
        )
        self.assertNotIn("今天美国开盘吗", metadata["command"])

    def test_codex_adapter_large_interactive_prompt_uses_stdin_sentinel(self) -> None:
        adapter = CodexAdapter()
        prompt = "x" * (CodexAdapter._INTERACTIVE_ARGV_PROMPT_MAX_BYTES + 1)
        task = Task(title="large", description=prompt)

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[-1], "-")
        self.assertEqual(metadata["prompt_transport"], "stdin")
        self.assertEqual(metadata["stdin_prompt_channel"], "pipe")
        self.assertEqual(metadata["prompt_bytes"], len(f"large\n\n{prompt}".encode("utf-8")))
        self.assertNotIn(prompt[:100], metadata["command"])

    def test_codex_adapter_normalizes_json_output_to_final_agent_message(self) -> None:
        adapter = CodexAdapter()
        output = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread_1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": "先查看仓库结构。"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", "git status --short"],
                    "aggregated_output": "",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item_3", "type": "agent_message", "text": "已修复显示格式问题。"},
            }),
            json.dumps({"type": "turn.completed"}),
        ])

        self.assertEqual(adapter.normalize_result_output(output), "已修复显示格式问题。")

    def test_codex_adapter_extracts_resume_session_id_from_thread_started_event(self) -> None:
        adapter = CodexAdapter()
        output = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread_1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        ])

        self.assertEqual(adapter.extract_resume_session_id(output), "thread_1")

    def test_codex_adapter_formats_progress_for_agent_message_and_tool(self) -> None:
        adapter = CodexAdapter()

        thinking = adapter.format_progress_update(
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": "我先检查渲染链路。"},
            }),
            "stdout",
        )
        tool = adapter.format_progress_update(
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", "git status --short"],
                    "aggregated_output": " M opc/plugins/office_ui/ws_handler.py",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            "stdout",
        )

        self.assertEqual(thinking, "[External:codex:thinking] 我先检查渲染链路。")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertTrue(tool.startswith("[External:codex:tool] $ git status --short"))
        self.assertIn("status=completed, exit_code=0", tool)

    def test_codex_adapter_builds_argv_prompt_command_for_interactive_runs(self) -> None:
        adapter = CodexAdapter()
        prompt_text = "body " * 400
        task = Task(title=prompt_text, description=prompt_text)
        prompt = adapter.build_task_prompt(task)

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[-1], prompt)
        self.assertEqual(metadata["prompt_transport"], "argv")
        self.assertEqual(metadata["prompt_bytes"], len(prompt.encode("utf-8")))
        self.assertNotIn("body", metadata["command"])
        self.assertIn("<prompt:", metadata["command"])

    async def test_codex_adapter_does_not_seed_argv_prompt_over_tty(self) -> None:
        adapter = CodexAdapter()
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()
        task = Task(title="demo", description="demo")
        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        async_to_thread = AsyncMock(return_value=None)
        tmpdir = _make_test_dir("codex-pty-argv-prompt")
        try:
            with patch("opc.layer3_agent.adapters.codex_adapter.os.openpty", return_value=(41, 42), create=True), \
                patch("opc.layer3_agent.adapters.codex_adapter.os.close"), \
                patch(
                    "opc.layer3_agent.adapters.codex_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ), \
                patch("opc.layer3_agent.adapters.codex_adapter.asyncio.to_thread", async_to_thread):
                started = await adapter.start_process(
                    cmd,
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        self.assertEqual(adapter._input_fds[321], 41)
        async_to_thread.assert_not_awaited()

    async def test_codex_adapter_no_pty_argv_prompt_inherits_stdin(self) -> None:
        adapter = CodexAdapter()
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()
        task = Task(title="demo", description="demo")
        prompt = adapter.build_task_prompt(task)
        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        tmpdir = _make_test_dir("codex-no-pty-argv-prompt")
        try:
            with patch.object(CodexAdapter, "_supports_pty_input_channel", return_value=False), \
                patch(
                    "opc.layer3_agent.adapters.codex_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                started = await adapter.start_process(
                    cmd,
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        spawn_mock.assert_awaited_once()
        self.assertEqual(cmd[-1], prompt)
        self.assertIsNone(spawn_mock.await_args.kwargs["stdin"])
        self.assertNotIn(321, adapter._input_fds)
        self.assertEqual(metadata["prompt_transport"], "argv")
        self.assertEqual(metadata["interactive_input_channel"], "inherit")
        self.assertEqual(metadata["stdin_policy"], "inherit")

    def test_codex_adapter_windows_multiline_prompt_uses_stdin_transport(self) -> None:
        adapter = CodexAdapter()
        task = Task(
            title="demo",
            description="## Task Brief\n如何看待agent的发展趋势\n\n## OpenOPC Context\nctx",
            metadata={"external_prompt_contract": "description_is_full_prompt"},
        )

        with patch("opc.layer3_agent.adapters.codex_adapter.os.name", "nt"), \
            patch("opc.layer3_agent.adapters.codex_adapter.shutil.which", return_value=r"C:\Users\me\AppData\Roaming\npm\codex.CMD"):
            cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[-1], "-")
        self.assertEqual(metadata["prompt_transport"], "stdin")
        self.assertEqual(metadata["stdin_policy"], "pipe_prompt_then_close")
        self.assertEqual(metadata["stdin_prompt_channel"], "pipe")
        self.assertEqual(metadata["prompt_transport_reason"], "windows_multiline_argv_unsafe")

    def test_codex_supplemental_stdin_failure_is_allowed_for_explicit_stdin_prompt(self) -> None:
        adapter = CodexAdapter()
        text = "Reading additional input from stdin..."

        self.assertIsNone(
            adapter.detect_runtime_failure(
                text,
                "stderr",
                {"prompt_transport": "stdin", "stdin_policy": "pipe_prompt_then_close"},
            )
        )
        self.assertIn(
            "supplemental stdin intake mode",
            adapter.detect_runtime_failure(text, "stderr", {"prompt_transport": "argv"}) or "",
        )

    async def test_codex_adapter_uses_tty_backed_input_channel_for_interactive_runs(self) -> None:
        adapter = CodexAdapter()
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()

        tmpdir = _make_test_dir("codex-pty-input")
        try:
            with patch("opc.layer3_agent.adapters.codex_adapter.os.openpty", return_value=(41, 42), create=True), \
                patch("opc.layer3_agent.adapters.codex_adapter.os.close") as close_mock, \
                patch(
                    "opc.layer3_agent.adapters.codex_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                started = await adapter.start_process(
                    [sys.executable, "-c", "print('demo')", "--json"],
                    tmpdir,
                )

                self.assertIs(started, proc)
                spawn_mock.assert_awaited_once()
                self.assertEqual(spawn_mock.await_args.kwargs["stdin"], 42)
                self.assertEqual(spawn_mock.await_args.kwargs["stdout"], asyncio.subprocess.PIPE)
                self.assertEqual(spawn_mock.await_args.kwargs["stderr"], asyncio.subprocess.PIPE)
                self.assertEqual(adapter._input_fds[321], 41)

                await adapter.cleanup_process(proc)
                self.assertNotIn(321, adapter._input_fds)
                close_mock.assert_any_call(42)
                close_mock.assert_any_call(41)
        finally:
            _cleanup_test_dir(tmpdir)

    async def test_codex_adapter_seeds_initial_prompt_over_tty(self) -> None:
        adapter = CodexAdapter()
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()
        task = Task(title="demo", description="body")
        prompt = adapter.build_task_prompt(task)
        expected = prompt.encode("utf-8") + b"\n\x04"

        async_to_thread = AsyncMock(return_value=None)
        tmpdir = _make_test_dir("codex-pty-seed")
        try:
            with patch("opc.layer3_agent.adapters.codex_adapter.os.openpty", return_value=(41, 42), create=True), \
                patch("opc.layer3_agent.adapters.codex_adapter.os.close"), \
                patch(
                    "opc.layer3_agent.adapters.codex_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ), \
                patch("opc.layer3_agent.adapters.codex_adapter.asyncio.to_thread", async_to_thread):
                await adapter.start_process(
                    [sys.executable, "-c", "print('demo')", "--json", "-"],
                    tmpdir,
                    task=task,
                    launch_metadata={"prompt_transport": "stdin"},
                )
        finally:
            _cleanup_test_dir(tmpdir)

        async_to_thread.assert_awaited_once()
        args = async_to_thread.await_args.args
        self.assertIs(args[0], adapter._write_input_bytes)
        self.assertEqual(args[1], 41)
        self.assertEqual(args[2], expected)

    async def test_codex_adapter_falls_back_to_pipe_when_pty_unavailable(self) -> None:
        adapter = CodexAdapter()
        task = Task(title="demo", description="body")

        class _Writer:
            def __init__(self) -> None:
                self.writes: list[bytes] = []
                self.closed = False

            def write(self, payload: bytes) -> None:
                self.writes.append(payload)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self.closed

        writer = _Writer()
        proc = type("Proc", (), {"pid": 321, "stdin": writer, "stdout": object(), "stderr": object()})()
        metadata = {"prompt_transport": "stdin"}

        tmpdir = _make_test_dir("codex-pipe-fallback")
        try:
            with patch.object(CodexAdapter, "_supports_pty_input_channel", return_value=False), \
                patch(
                    "opc.layer3_agent.adapters.base.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                started = await adapter.start_process(
                    [sys.executable, "-c", "print('demo')", "--json", "-"],
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        spawn_mock.assert_awaited_once()
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.PIPE)
        self.assertEqual(writer.writes, [adapter.build_task_prompt(task).encode("utf-8")])
        self.assertTrue(writer.closed)
        self.assertNotIn(321, adapter._input_fds)
        self.assertEqual(metadata["interactive_input_channel"], "pipe")
        self.assertEqual(metadata["stdin_policy"], "pipe_prompt_then_close")
        self.assertIn("live approval replies", metadata["interactive_input_limitation"])

    async def test_codex_adapter_writes_approval_response_to_input_fd(self) -> None:
        adapter = CodexAdapter()
        proc = type("Proc", (), {"pid": 654, "stdin": None})()
        adapter._input_fds[654] = 77

        async_to_thread = AsyncMock(return_value=None)
        with patch("opc.layer3_agent.adapters.codex_adapter.asyncio.to_thread", async_to_thread):
            sent = await adapter.send_process_input(proc, '{"decision":"approved"}\n')

        self.assertTrue(sent)
        async_to_thread.assert_awaited_once()
        args = async_to_thread.await_args.args
        self.assertIs(args[0], adapter._write_input_bytes)
        self.assertEqual(args[1], 77)
        self.assertEqual(args[2], b'{"decision":"approved"}\n')

    def test_external_prompt_contract_uses_description_as_full_prompt(self) -> None:
        adapter = CodexAdapter()
        task = Task(
            title="old session title...",
            description="current user instruction",
            metadata={
                "external_prompt_contract": "description_is_full_prompt",
            },
        )

        self.assertEqual(adapter.build_task_prompt(task), "current user instruction")

    async def test_codex_adapter_execute_streams_prompt_over_stdin_for_noninteractive_runs(self) -> None:
        adapter = CodexAdapter()
        task = Task(title="demo", description="body")

        class _Writer:
            def __init__(self) -> None:
                self.writes: list[bytes] = []
                self.closed = False

            def write(self, payload: bytes) -> None:
                self.writes.append(payload)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self.closed

        writer = _Writer()
        proc = type(
            "Proc",
            (),
            {
                "pid": 987,
                "stdin": writer,
                "stdout": object(),
                "stderr": object(),
                "returncode": 0,
                "communicate": AsyncMock(return_value=(b"done", b"")),
            },
        )()

        tmpdir = _make_test_dir("codex-noninteractive-stdin")
        try:
            with patch.object(adapter, "is_available", AsyncMock(return_value=True)), \
                patch(
                    "opc.layer3_agent.adapters.base.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                result = await adapter.execute(task, tmpdir)
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertEqual(result.status, TaskStatus.DONE)
        spawn_cmd = list(spawn_mock.await_args.args)
        self.assertEqual(spawn_cmd[-1], "-")
        self.assertEqual(writer.writes, [adapter.build_task_prompt(task).encode("utf-8")])
        self.assertTrue(writer.closed)

    def test_codex_adapter_tool_progress_keeps_full_command_in_detail(self) -> None:
        adapter = CodexAdapter()
        long_suffix = "x" * 2600
        command = f"printf '{long_suffix}'"

        tool = adapter.format_progress_update(
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", command],
                    "aggregated_output": "",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            "stdout",
        )

        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertIn(command, tool)
        self.assertIn("status=completed, exit_code=0", tool)

    def test_claude_adapter_parses_permission_request_as_shell_exec(self) -> None:
        adapter = ClaudeCodeAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "permission_request",
                    "tool_name": "Bash",
                    "input": {"command": "git status -sb", "cwd": "/repo"},
                    "message": "Allow Bash to run `git status -sb`?",
                }
            ),
            "stdout",
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.action_name, "shell_exec")
        self.assertEqual(request.arguments["command"], "git status -sb")
        self.assertEqual(request.arguments["working_directory"], "/repo")

    def test_claude_adapter_ignores_completed_command_event_for_approval_parsing(self) -> None:
        adapter = ClaudeCodeAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": ["/bin/bash", "-lc", "sed -n '1,220p' /repo/plan.md"],
                        "aggregated_output": "Permission denied was mentioned in prior logs.",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNone(request)

    def test_claude_adapter_small_interactive_prompt_stays_on_argv(self) -> None:
        adapter = ClaudeCodeAdapter()
        task = Task(title="demo", description="body")
        prompt = adapter.build_task_prompt(task)

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertEqual(cmd[-2:], ["--", prompt])
        self.assertEqual(metadata["prompt_transport"], "argv")
        self.assertEqual(metadata["prompt_bytes"], len(prompt.encode("utf-8")))
        self.assertNotIn(prompt, metadata["command"])
        self.assertTrue(adapter.supports_approval_prompt_handling(cmd, metadata))

    async def test_claude_adapter_full_auto_argv_prompt_uses_devnull_stdin(self) -> None:
        adapter = ClaudeCodeAdapter(
            config=ExternalAgentConfig(command="claude", approval_mode="full-auto")
        )
        task = Task(title="demo", description="body")
        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()

        tmpdir = _make_test_dir("claude-full-auto-devnull")
        try:
            with patch.object(ClaudeCodeAdapter, "_user_shell_proxy_env", return_value={}), \
                patch(
                    "opc.layer3_agent.adapters.claude_code.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                started = await adapter.start_process(
                    cmd,
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        self.assertFalse(adapter.supports_approval_prompt_handling(cmd, metadata))
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertEqual(metadata["stdin_policy"], "devnull")

    async def test_claude_adapter_auto_argv_prompt_keeps_stdin_for_approval_bridge(self) -> None:
        adapter = ClaudeCodeAdapter(
            config=ExternalAgentConfig(command="claude", approval_mode="auto")
        )
        task = Task(title="demo", description="body")
        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")
        proc = type("Proc", (), {"pid": 321, "stdin": None, "stdout": object(), "stderr": object()})()

        tmpdir = _make_test_dir("claude-auto-approval-stdin")
        try:
            with patch.object(ClaudeCodeAdapter, "_user_shell_proxy_env", return_value={}), \
                patch(
                    "opc.layer3_agent.adapters.claude_code.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                started = await adapter.start_process(
                    cmd,
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        self.assertTrue(adapter.supports_approval_prompt_handling(cmd, metadata))
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.PIPE)
        self.assertEqual(metadata["stdin_policy"], "pipe_open")

    def test_claude_adapter_large_interactive_prompt_uses_stdin(self) -> None:
        adapter = ClaudeCodeAdapter()
        prompt = "x" * (ClaudeCodeAdapter._INTERACTIVE_ARGV_PROMPT_MAX_BYTES + 1)
        task = Task(title="large", description=prompt)
        full_prompt = adapter.build_task_prompt(task)

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertNotIn("--", cmd)
        self.assertNotIn(full_prompt, cmd)
        self.assertIn("--input-format", cmd)
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "text")
        self.assertEqual(metadata["prompt_transport"], "stdin")
        self.assertEqual(metadata["stdin_prompt_channel"], "pipe")
        self.assertEqual(metadata["prompt_bytes"], len(full_prompt.encode("utf-8")))
        self.assertNotIn(prompt[:100], metadata["command"])
        self.assertFalse(adapter.supports_approval_prompt_handling(cmd, metadata))

    def test_claude_adapter_large_batch_prompt_uses_stdin(self) -> None:
        adapter = ClaudeCodeAdapter()
        prompt = "x" * (ClaudeCodeAdapter._INTERACTIVE_ARGV_PROMPT_MAX_BYTES + 1)
        task = Task(title="large", description=prompt)

        cmd, metadata = adapter.build_invocation(task, workspace_path="/repo")

        self.assertNotIn("--", cmd)
        self.assertIn("--input-format", cmd)
        self.assertEqual(metadata["prompt_transport"], "stdin")
        self.assertNotIn(prompt[:100], metadata["command"])

    async def test_claude_adapter_seeds_large_prompt_over_stdin_pipe(self) -> None:
        adapter = ClaudeCodeAdapter()
        prompt = "x" * (ClaudeCodeAdapter._INTERACTIVE_ARGV_PROMPT_MAX_BYTES + 1)
        task = Task(title="large", description=prompt)
        full_prompt = adapter.build_task_prompt(task)
        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        class _Writer:
            def __init__(self) -> None:
                self.writes: list[bytes] = []
                self.closed = False

            def write(self, payload: bytes) -> None:
                self.writes.append(payload)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self.closed

        writer = _Writer()
        proc = type("Proc", (), {"pid": 321, "stdin": writer, "stdout": object(), "stderr": object()})()

        tmpdir = _make_test_dir("claude-large-stdin")
        try:
            with patch(
                "opc.layer3_agent.adapters.claude_code.asyncio.create_subprocess_exec",
                AsyncMock(return_value=proc),
            ) as spawn_mock:
                started = await adapter.start_process(
                    cmd,
                    tmpdir,
                    task=task,
                    launch_metadata=metadata,
                )
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertIs(started, proc)
        spawn_cmd = list(spawn_mock.await_args.args)
        self.assertNotIn(full_prompt, spawn_cmd)
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.PIPE)
        self.assertEqual(writer.writes, [full_prompt.encode("utf-8")])
        self.assertTrue(writer.closed)
        self.assertEqual(metadata["interactive_input_channel"], "pipe")
        self.assertEqual(metadata["stdin_policy"], "pipe_prompt_then_close")

    def test_claude_adapter_builds_resume_command_when_session_is_available(self) -> None:
        adapter = ClaudeCodeAdapter(
            config=ExternalAgentConfig(
                command="claude",
                session_mode="resume",
                session_id="sess-claude-1",
            )
        )
        task = Task(title="demo", description="body")

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("--resume", cmd)
        resume_index = cmd.index("--resume")
        self.assertEqual(cmd[resume_index + 1], "sess-claude-1")
        self.assertEqual(metadata["session_mode"], "resume")
        self.assertEqual(metadata["session_id"], "sess-claude-1")

    def test_claude_adapter_uses_continue_when_resume_session_id_is_missing(self) -> None:
        adapter = ClaudeCodeAdapter(
            config=ExternalAgentConfig(
                command="claude",
                session_mode="resume",
            )
        )
        task = Task(title="demo", description="body")

        cmd, _ = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("--continue", cmd)
        self.assertNotIn("--resume", cmd)

    def test_claude_adapter_surfaces_memory_root_to_sandbox_add_dir(self) -> None:
        tmp_opc_home = _make_test_dir("claude-opc-home")
        try:
            opc_home_path = Path(tmp_opc_home).resolve()
            with patch("opc.core.config.get_opc_home", return_value=opc_home_path):
                adapter = ClaudeCodeAdapter()
                task = Task(
                    title="memory scope",
                    metadata={
                        "target_output_dir": "/workspace/task",
                        "comms_workspace_root": "/workspace/comms",
                    },
                )
                cmd, _ = adapter.build_interactive_invocation(task, workspace_path="/workspace/task")

            add_dir_values = [cmd[idx + 1] for idx, value in enumerate(cmd[:-1]) if value == "--add-dir"]
            self.assertIn("/workspace/task", add_dir_values)
            self.assertIn("/workspace/comms", add_dir_values)
            self.assertIn(str(opc_home_path / "memory"), add_dir_values)
        finally:
            _cleanup_test_dir(tmp_opc_home)

    def test_codex_and_claude_allow_explicit_outside_workspace_as_primary_workspace(self) -> None:
        tmp_opc_home = _make_test_dir("outside-workspace-opc-home")
        try:
            opc_home_path = Path(tmp_opc_home).resolve()
            outside_workspace = "/user/specified/outside-workspace"
            task = Task(
                title="outside workspace",
                project_id="proj1",
                metadata={
                    "target_output_dir": outside_workspace,
                    "comms_workspace_root": "/default/project-workplace",
                },
            )

            with patch("opc.core.config.get_opc_home", return_value=opc_home_path):
                codex_cmd, _ = CodexAdapter().build_interactive_invocation(
                    task,
                    workspace_path=outside_workspace,
                )
                claude_cmd, _ = ClaudeCodeAdapter().build_interactive_invocation(
                    task,
                    workspace_path=outside_workspace,
                )

            codex_add_dirs = [codex_cmd[idx + 1] for idx, value in enumerate(codex_cmd[:-1]) if value == "--add-dir"]
            claude_add_dirs = [claude_cmd[idx + 1] for idx, value in enumerate(claude_cmd[:-1]) if value == "--add-dir"]
            self.assertIn("-C", codex_cmd)
            self.assertEqual(codex_cmd[codex_cmd.index("-C") + 1], outside_workspace)
            self.assertIn(outside_workspace, codex_add_dirs)
            self.assertIn(outside_workspace, claude_add_dirs)
            self.assertIn(str(opc_home_path / "memory"), codex_add_dirs)
            self.assertIn(str(opc_home_path / "memory"), claude_add_dirs)
        finally:
            _cleanup_test_dir(tmp_opc_home)

    def test_claude_adapter_approval_modes_map_to_three_openopc_modes(self) -> None:
        task = Task(title="demo", description="body")

        auto_cmd, auto_meta = ClaudeCodeAdapter(
            config=ExternalAgentConfig(command="claude", approval_mode="auto")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertIn("--permission-mode", auto_cmd)
        self.assertEqual(auto_cmd[auto_cmd.index("--permission-mode") + 1], "auto")
        self.assertEqual(auto_meta["approval_mode"], "auto")

        user_cmd, user_meta = ClaudeCodeAdapter(
            config=ExternalAgentConfig(command="claude", approval_mode="user-settings")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertNotIn("--permission-mode", user_cmd)
        self.assertEqual(user_meta["approval_mode"], "user-settings")

        full_cmd, full_meta = ClaudeCodeAdapter(
            config=ExternalAgentConfig(command="claude", approval_mode="full-auto")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertIn("--permission-mode", full_cmd)
        self.assertEqual(full_cmd[full_cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(full_meta["approval_mode"], "full-auto")

    def test_claude_adapter_declares_isolated_home_for_skill_install(self) -> None:
        # Claude Code still participates in the broker collab-surface install
        # so PATH gets the `opc-collab` shim, but it must not set
        # ``CLAUDE_CONFIG_DIR``. That would bypass the user's normal Claude
        # login/keychain state and can strand company mode on stale isolated
        # credentials.
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter.agent_isolation_home_slug(), "claude")
        env = adapter.agent_home_env_vars("/opc/home/agent_homes/claude")
        self.assertEqual(env, {})

    def test_claude_adapter_installs_collab_skill_in_user_claude_home(self) -> None:
        adapter = ClaudeCodeAdapter()
        tmpdir = _make_test_dir("claude-global-skill-install")
        try:
            fake_profile = Path(tmpdir) / "profile"
            agent_home = Path(tmpdir) / "agent-home"

            with patch("pathlib.Path.home", return_value=fake_profile), patch(
                "opc.layer3_agent.adapters.claude_code.install_opc_collab_skill"
            ) as install_mock:
                adapter.post_install_agent_home(str(agent_home))

            self.assertTrue(agent_home.exists())
            install_mock.assert_called_once_with(fake_profile / ".claude")
        finally:
            _cleanup_test_dir(tmpdir)

    def test_claude_adapter_resolves_launch_binary_before_spawn(self) -> None:
        metadata: dict[str, object] = {}

        with patch("opc.layer3_agent.adapters.base.shutil.which", return_value=r"C:\Tools\claude.cmd"):
            cmd = ClaudeCodeAdapter._resolve_launch_command(
                ["claude", "--print"],
                launch_metadata=metadata,
            )

        self.assertEqual(cmd[0], r"C:\Tools\claude.cmd")
        self.assertEqual(metadata["configured_binary"], "claude")
        self.assertEqual(metadata["resolved_binary"], r"C:\Tools\claude.cmd")

    def test_codex_adapter_declares_isolated_home_for_skill_install(self) -> None:
        adapter = CodexAdapter()
        self.assertEqual(adapter.agent_isolation_home_slug(), "codex")
        env = adapter.agent_home_env_vars("/opc/home/agent_homes/codex")
        self.assertEqual(env, {"CODEX_HOME": "/opc/home/agent_homes/codex"})

    def test_cursor_adapter_declares_isolated_home_for_skill_install(self) -> None:
        adapter = CursorAdapter()
        self.assertEqual(adapter.agent_isolation_home_slug(), "cursor")
        self.assertEqual(adapter.agent_home_env_vars("/opc/home/agent_homes/cursor"), {})

    def test_cursor_adapter_prefers_cursor_agent_over_editor_cli(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor"))

        def _which(name: str) -> str | None:
            if name == "cursor-agent":
                return "/usr/local/bin/cursor-agent"
            if name == "cursor":
                return "/usr/local/bin/cursor"
            return None

        with patch("opc.layer3_agent.adapters.cursor_adapter.shutil.which", side_effect=_which):
            self.assertEqual(adapter.resolve_binary(), "/usr/local/bin/cursor-agent")

    def test_cursor_adapter_rejects_editor_cli_without_cursor_agent(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor"))

        def _which(name: str) -> str | None:
            if name == "cursor":
                return "/usr/local/bin/cursor"
            return None

        with patch("opc.layer3_agent.adapters.cursor_adapter.shutil.which", side_effect=_which):
            self.assertIsNone(adapter.resolve_binary())
            self.assertFalse(adapter.supports_interactive())

    async def test_cursor_execute_uses_devnull_stdin(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor-agent"))
        task = Task(title="demo", description="body")
        proc = type(
            "Proc",
            (),
            {
                "pid": 321,
                "returncode": 0,
                "communicate": AsyncMock(return_value=(b"ok", b"")),
            },
        )()

        tmpdir = _make_test_dir("cursor-execute-devnull")
        try:
            with patch.object(adapter, "is_available", AsyncMock(return_value=True)), \
                patch(
                    "opc.layer3_agent.adapters.cursor_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                result = await adapter.execute(task, tmpdir)
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)

    def test_codex_adapter_mirrors_user_auth_and_config_with_copy_fallback(self) -> None:
        adapter = CodexAdapter()
        tmpdir = _make_test_dir("codex-mirror-user-config")
        try:
            fake_profile = Path(tmpdir) / "profile"
            user_codex = fake_profile / ".codex"
            agent_home = Path(tmpdir) / "agent-home"
            user_codex.mkdir(parents=True)
            (user_codex / "auth.json").write_text('{"OPENAI_API_KEY":"sk-test"}')
            (user_codex / "config.toml").write_text('model_provider = "codex"\n')

            def _blocked_symlink(self: Path, target: Path) -> None:
                _ = (self, target)
                raise OSError("symlink blocked")

            with patch("pathlib.Path.home", return_value=fake_profile), \
                patch("pathlib.Path.symlink_to", _blocked_symlink):
                adapter.post_install_agent_home(str(agent_home))

            self.assertEqual((agent_home / "auth.json").read_text(), '{"OPENAI_API_KEY":"sk-test"}')
            self.assertEqual((agent_home / "config.toml").read_text(), 'model_provider = "codex"\n')
        finally:
            _cleanup_test_dir(tmpdir)

    def test_codex_adapter_filters_parent_codex_runtime_env(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(
            os.environ,
            {
                "CODEX_THREAD_ID": "parent-thread",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "codex_vscode",
                "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                "CODEX_CUSTOM_CONFIG": "keep-me",
                "OPENAI_API_KEY": "sk-parent",
                "PATH": "base-path",
            },
            clear=True,
        ):
            env = adapter.build_process_env({"CODEX_HOME": "/opc/home/agent_homes/codex"})

        assert env is not None
        self.assertEqual(env["CODEX_HOME"], "/opc/home/agent_homes/codex")
        self.assertEqual(env["CODEX_CUSTOM_CONFIG"], "keep-me")
        self.assertEqual(env["OPENAI_API_KEY"], "sk-parent")
        self.assertEqual(env["PATH"], "base-path")
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", env)
        self.assertNotIn("CODEX_SANDBOX_NETWORK_DISABLED", env)

    def test_codex_adapter_surfaces_opc_home_to_sandbox_add_dir(self) -> None:
        # Codex's workspace-write sandbox mounts everything outside the
        # workspace read-only. Without an explicit ``--add-dir
        # <opc_home>``, the ``opc-collab`` CLI cannot write the runtime DB;
        # without ``--add-dir <opc_home>/memory``, spawned agents cannot edit
        # canonical durable memory.
        tmp_opc_home = _make_test_dir("codex-opc-home")
        try:
            opc_home_path = str(Path(tmp_opc_home).resolve())
            with patch("opc.layer3_agent.adapters.codex_adapter.__getattr__", create=True):
                pass  # no-op; keeps linter happy
            # Patch get_opc_home inside the adapter module to a tmp dir.
            with patch("opc.core.config.get_opc_home", return_value=Path(opc_home_path)):
                adapter = CodexAdapter()
                task = Task(
                    title="sandbox-scope",
                    project_id="proj1",
                    metadata={
                        "target_output_dir": "/workspace/task",
                        "comms_workspace_root": "/workspace/comms",
                    },
                )
                extra = adapter._build_extra_dir_args(task)
                config_args = adapter._build_writable_roots_config_args(task)
            memory_root = str(Path(opc_home_path) / "memory")
            # The comms root, memory root, and opc home end up on --add-dir,
            # and the workspace itself is NOT duplicated here (it was
            # already added via build_workspace_args).
            self.assertIn("/workspace/comms", extra)
            self.assertIn(memory_root, extra)
            self.assertIn(opc_home_path, extra)
            self.assertNotIn("/workspace/task", extra)
            # Codex currently requires the workspace-write roots to be
            # surfaced through config as well as --add-dir for shell writes.
            self.assertEqual(config_args[0], "-c")
            self.assertIn("/workspace/comms", config_args[1])
            self.assertIn(memory_root, config_args[1])
            self.assertIn(opc_home_path, config_args[1])
            self.assertNotIn("/workspace/task", config_args[1])
            # Every --add-dir must be paired with a path argument.
            self.assertEqual(extra.count("--add-dir"), (len(extra)) // 2)
        finally:
            _cleanup_test_dir(tmp_opc_home)

    def test_opencode_adapter_declares_isolated_home_for_skill_install(self) -> None:
        adapter = OpenCodeAdapter()
        self.assertEqual(adapter.agent_isolation_home_slug(), "opencode")
        self.assertEqual(
            adapter.agent_home_env_vars("/opc/home/agent_homes/opencode"),
            {"OPENCODE_CONFIG_DIR": "/opc/home/agent_homes/opencode"},
        )

    def test_opencode_adapter_mirrors_user_config_with_copy_fallback(self) -> None:
        adapter = OpenCodeAdapter()
        tmpdir = _make_test_dir("opencode-mirror-user-config")
        try:
            fake_profile = Path(tmpdir) / "profile"
            user_config = fake_profile / ".config" / "opencode"
            agent_home = Path(tmpdir) / "agent-home"
            user_config.mkdir(parents=True)
            (user_config / "opencode.json").write_text('{"model":"test"}')

            def _blocked_symlink(self: Path, target: Path, target_is_directory: bool = False) -> None:
                _ = (self, target, target_is_directory)
                raise OSError("symlink blocked")

            with patch("pathlib.Path.home", return_value=fake_profile), \
                patch("pathlib.Path.symlink_to", _blocked_symlink):
                adapter.post_install_agent_home(str(agent_home))

            self.assertEqual((agent_home / "opencode.json").read_text(), '{"model":"test"}')
        finally:
            _cleanup_test_dir(tmpdir)


    def test_install_collab_surface_is_idempotent(self) -> None:
        # The skill installer runs before every launch; repeated calls
        # must reconcile symlinks instead of rewriting, and the CLI
        # shim must end up executable and self-contained.
        from opc.layer3_agent.skill_installer import (
            install_collab_surface,
            prepend_to_path,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            opc_home = Path(tmpdir)
            home1, bin1 = install_collab_surface("codex", opc_home=opc_home)
            home2, bin2 = install_collab_surface("codex", opc_home=opc_home)

            self.assertEqual(home1, home2)
            self.assertEqual(bin1, bin2)
            shim = bin1 / "opc-collab"
            self.assertTrue(shim.exists())
            self.assertTrue(os.access(shim, os.X_OK))
            cmd_shim = bin1 / "opc-collab.cmd"
            if os.name == "nt":
                self.assertTrue(cmd_shim.exists())
                self.assertIn("opc.cli_collab", cmd_shim.read_text())
            else:
                self.assertFalse(cmd_shim.exists())
            skill = home1 / "skills" / "opc-collab" / "SKILL.md"
            self.assertTrue(skill.exists())
            # The skill file should be reachable as the packaged asset.
            self.assertIn("opc-collab", skill.read_text())

            # PATH prepend de-duplicates and places bin dir first.
            path = prepend_to_path(f"/usr/bin:{bin1}", bin1)
            self.assertTrue(path.startswith(str(bin1) + os.pathsep))
            self.assertEqual(path.count(str(bin1)), 1)

    def test_cursor_adapter_text_fallback_extracts_shell_command(self) -> None:
        adapter = CursorAdapter()
        request = adapter.parse_approval_request(
            "Approve terminal command `git status --short`? [Y/N]\n",
            "stdout",
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.action_name, "shell_exec")
        self.assertEqual(request.arguments["command"], "git status --short")

    def test_cursor_adapter_ignores_permission_error_text_for_approval_parsing(self) -> None:
        adapter = CursorAdapter()
        request = adapter.parse_approval_request(
            "PermissionError: [Errno 1] Operation not permitted while running `python3 -m http.server 4173`\n",
            "stderr",
        )

        self.assertIsNone(request)

    def test_cursor_adapter_approval_modes_only_force_full_auto(self) -> None:
        task = Task(title="demo", description="body")

        auto_cmd, auto_meta = CursorAdapter(
            config=ExternalAgentConfig(command="cursor-agent", approval_mode="auto")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertNotIn("--force", auto_cmd)
        self.assertIn("--trust", auto_cmd)
        self.assertEqual(auto_meta["approval_mode"], "auto")

        user_cmd, user_meta = CursorAdapter(
            config=ExternalAgentConfig(command="cursor-agent", approval_mode="user-settings")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertNotIn("--force", user_cmd)
        self.assertIn("--trust", user_cmd)
        self.assertEqual(user_meta["approval_mode"], "user-settings")

        full_cmd, full_meta = CursorAdapter(
            config=ExternalAgentConfig(command="cursor-agent", approval_mode="full-auto")
        ).build_interactive_invocation(task, workspace_path="/repo")
        self.assertIn("--force", full_cmd)
        self.assertIn("--trust", full_cmd)
        self.assertEqual(full_meta["approval_mode"], "full-auto")

        self.assertFalse(
            CursorAdapter(
                config=ExternalAgentConfig(command="cursor-agent", approval_mode="auto")
            ).supports_approval_prompt_handling(auto_cmd, auto_meta)
        )
        self.assertFalse(
            CursorAdapter(
                config=ExternalAgentConfig(command="cursor-agent", approval_mode="full-auto")
            ).supports_approval_prompt_handling(full_cmd, full_meta)
        )

    def test_cursor_adapter_resume_and_stream_json_result_parsing(self) -> None:
        task = Task(title="demo", description="body")
        adapter = CursorAdapter(
            config=ExternalAgentConfig(
                command="cursor-agent",
                session_mode="resume",
                session_id="chat_123",
            )
        )

        cmd, _ = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "chat_123")
        self.assertEqual(cmd[-1], "demo\n\nbody")

        output = "\n".join([
            json.dumps({"type": "system", "session_id": "chat_123"}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "draft"}]}}),
            json.dumps({"type": "result", "result": "final answer", "session_id": "chat_123"}),
        ])

        self.assertEqual(adapter.extract_resume_session_id(output), "chat_123")
        self.assertEqual(adapter.normalize_result_output(output), "final answer")

        progress = adapter.format_progress_update(
            json.dumps({"type": "tool_call", "name": "shell", "input": {"command": "git status --short"}}),
            "stdout",
        )
        self.assertEqual(progress, "[External:cursor:tool] $ git status --short")

    def test_cursor_adapter_formats_nested_stream_json_tool_calls(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor-agent"))

        shell_started = {
            "type": "tool_call",
            "subtype": "started",
            "tool_call": {
                "shellToolCall": {
                    "args": {
                        "command": "pwd && ls -la",
                        "description": "Print working directory and list all files",
                    },
                    "description": "Print working directory and list all files",
                },
            },
        }
        shell_rejected = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "shellToolCall": {
                    "result": {
                        "rejected": {
                            "command": "pwd && ls -la",
                            "workingDirectory": "/tmp/opc_external_trace_sample",
                            "reason": "",
                        },
                    },
                },
            },
        }
        glob_completed = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "globToolCall": {
                    "args": {
                        "targetDirectory": "/tmp/opc_external_trace_sample",
                        "globPattern": "*",
                    },
                    "result": {
                        "success": {
                            "files": ["alpha.txt", "beta.md"],
                            "totalFiles": 2,
                        },
                    },
                },
            },
        }
        shell_completed = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "shellToolCall": {
                    "args": {
                        "command": "ls -la /tmp/opc_external_trace_sample",
                        "description": "List directory contents with details",
                    },
                    "result": {
                        "success": {
                            "stdout": "total 8\n-rw-r--r-- alpha.txt\n-rw-r--r-- beta.md\n",
                            "exitCode": 0,
                        },
                    },
                    "description": "List directory contents with details",
                },
            },
        }
        web_search_completed = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "webSearchToolCall": {
                    "args": {
                        "searchTerm": "Cursor pricing plans",
                        "toolCallId": "tool_search",
                    },
                    "result": {
                        "success": {
                            "references": [
                                {
                                    "title": "Web search results",
                                    "url": "",
                                    "chunk": (
                                        "Links:\n"
                                        "1. [Cursor · Pricing](https://www.cursor.com/pricing)\n"
                                        "2. [Pricing and plans | Cursor Docs](https://cursor.com/help/account-and-billing/pricing)\n"
                                    ),
                                }
                            ],
                        },
                    },
                },
            },
        }
        web_fetch_completed = {
            "type": "tool_call",
            "subtype": "completed",
            "tool_call": {
                "webFetchToolCall": {
                    "args": {
                        "url": "https://www.cursor.com/pricing",
                        "toolCallId": "tool_fetch",
                    },
                    "result": {
                        "success": {
                            "url": "https://www.cursor.com/pricing",
                            "markdown": (
                                "Cursor · Pricing\n\n"
                                "# Pricing\n\n"
                                "MonthlyYearly\n\n"
                                "## Individual Plans\n\n"
                                "### Pro\n\n"
                                "$20 / mo.\n"
                            ),
                        },
                    },
                },
            },
        }

        self.assertEqual(
            adapter.format_progress_update(json.dumps(shell_started), "stdout"),
            "[External:cursor:tool] $ pwd && ls -la\nPrint working directory and list all files",
        )
        self.assertEqual(
            adapter.format_progress_update(json.dumps(shell_rejected), "stdout"),
            "[External:cursor:tool] shell\nrejected: pwd && ls -la",
        )
        self.assertEqual(
            adapter.format_progress_update(json.dumps(glob_completed), "stdout"),
            "[External:cursor:tool] glob /tmp/opc_external_trace_sample\nalpha.txt\nbeta.md (2 total)",
        )
        self.assertEqual(
            adapter.format_progress_update(json.dumps(shell_completed), "stdout"),
            "[External:cursor:tool] $ ls -la /tmp/opc_external_trace_sample\nList directory contents with details\ntotal 8\n-rw-r--r-- alpha.txt\n-rw-r--r-- beta.md",
        )
        self.assertEqual(
            adapter.format_progress_update(json.dumps(web_search_completed), "stdout"),
            "[External:cursor:tool] web search: Cursor pricing plans\n"
            "- Cursor · Pricing — https://www.cursor.com/pricing\n"
            "- Pricing and plans | Cursor Docs — https://cursor.com/help/account-and-billing/pricing",
        )
        self.assertEqual(
            adapter.format_progress_update(json.dumps(web_fetch_completed), "stdout"),
            "[External:cursor:tool] web fetch: https://www.cursor.com/pricing\n"
            "Cursor · Pricing\n# Pricing\nMonthlyYearly\n## Individual Plans\n### Pro\n$20 / mo.",
        )

    def test_cursor_adapter_buffers_thinking_delta_until_completed(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor-agent"))

        first = adapter.format_progress_update(
            json.dumps({"type": "thinking", "subtype": "delta", "text": "checking "}),
            "stdout",
        )
        second = adapter.format_progress_update(
            json.dumps({"type": "thinking", "subtype": "delta", "text": "files"}),
            "stdout",
        )
        completed = adapter.format_progress_update(
            json.dumps({"type": "thinking", "subtype": "completed"}),
            "stdout",
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(completed, "[External:cursor:thinking] checking files")

    def test_cursor_adapter_ignores_stream_json_events_for_approval_parsing(self) -> None:
        adapter = CursorAdapter(config=ExternalAgentConfig(command="cursor-agent"))

        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "tool_call": {
                        "webFetchToolCall": {
                            "args": {
                                "url": "https://www.cursor.com/pricing",
                                "toolCallId": "tool_fetch",
                            },
                            "result": {
                                "success": {
                                    "markdown": "### Can I buy Cursor from a reseller?\nNo.",
                                },
                            },
                        },
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNone(request)

    def test_opencode_adapter_parses_bash_approval_request(self) -> None:
        adapter = OpenCodeAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "approval_request",
                    "sessionID": "ses_1",
                    "permission": {
                        "id": "perm_1",
                        "sessionID": "ses_1",
                        "permission": "bash",
                        "patterns": ["git status --short"],
                        "metadata": {"cwd": "/repo"},
                        "always": ["git status *"],
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.approval_scope, "tool")
        self.assertEqual(request.action_name, "shell_exec")
        self.assertEqual(request.arguments["command"], "git status --short")
        self.assertEqual(request.arguments["working_directory"], "/repo")

    def test_opencode_adapter_ignores_completed_command_event_for_approval_parsing(self) -> None:
        adapter = OpenCodeAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "command_execution",
                        "command": ["/bin/bash", "-lc", "sed -n '1,200p' /repo/notes.md"],
                        "aggregated_output": "Allowlisted paths are documented here.",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNone(request)

    def test_opencode_adapter_extracts_resume_session_id_from_json_events(self) -> None:
        adapter = OpenCodeAdapter()
        output = "\n".join([
            json.dumps({"type": "approval_request", "sessionID": "ses_1", "permission": {"id": "perm_1"}}),
            json.dumps({"type": "assistant_message", "sessionID": "ses_1", "message": "done"}),
        ])

        self.assertEqual(adapter.extract_resume_session_id(output), "ses_1")

    def test_opencode_adapter_full_auto_uses_inline_permission_allow(self) -> None:
        adapter = OpenCodeAdapter(
            config=ExternalAgentConfig(command="opencode", approval_mode="full-auto")
        )

        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"model":"test"}'}, clear=False):
            env = adapter.build_process_env({"PATH": "/tmp/bin"})

        assert env is not None
        inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(inline["model"], "test")
        self.assertEqual(inline["permission"], "allow")
        self.assertEqual(env["PATH"], "/tmp/bin")

        cmd, _ = adapter.build_interactive_invocation(Task(title="demo"), workspace_path="/repo")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertFalse(adapter.supports_approval_prompt_handling(cmd, {"approval_mode": "full-auto"}))

    def test_opencode_adapter_only_bridges_approvals_with_permission_handler(self) -> None:
        adapter = OpenCodeAdapter(
            config=ExternalAgentConfig(command="opencode", approval_mode="auto")
        )

        self.assertFalse(
            adapter.supports_approval_prompt_handling(
                ["opencode", "run", "--format", "json", "prompt"],
                {"approval_mode": "auto"},
            )
        )
        self.assertTrue(
            adapter.supports_approval_prompt_handling(
                [
                    "opencode",
                    "run",
                    "--format",
                    "json",
                    "--permission-handler",
                    "stdio-json",
                    "prompt",
                ],
                {"approval_mode": "auto"},
            )
        )

    def test_opencode_adapter_show_thinking_adds_flag(self) -> None:
        adapter = OpenCodeAdapter(
            config=ExternalAgentConfig(
                command="opencode",
                show_thinking=True,
            )
        )

        cmd, _ = adapter.build_interactive_invocation(Task(title="demo"), workspace_path="/repo")

        self.assertIn("--thinking", cmd)

    def test_external_invocation_metadata_uses_sanitized_display_command(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))
        task = Task(title="demo", description="## Task Brief\ndemo\n\n" + ("x" * 240))

        cmd, metadata = adapter.build_interactive_invocation(task, workspace_path="/repo")

        self.assertIn("x" * 80, metadata["command"])
        self.assertNotIn("x" * 80, metadata["display_command"])
        self.assertIn("<prompt:", metadata["display_command"])
        self.assertEqual(cmd[-1], task.description)

    def test_external_task_prompt_does_not_duplicate_existing_task_brief(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))
        task = Task(title="hello", description="## Task Brief\nhello\n\n## OpenOPC Context\nctx")

        prompt = adapter.build_task_prompt(task)

        self.assertEqual(prompt, task.description)
        self.assertEqual(prompt.count("hello"), 1)

    def test_opencode_process_env_creates_config_dir_when_isolated(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "missing-opencode-config"

            env = adapter.build_process_env({"OPENCODE_CONFIG_DIR": str(config_dir)})

            self.assertIsNotNone(env)
            assert env is not None
            self.assertTrue(config_dir.is_dir())
            self.assertEqual(env["OPENCODE_CONFIG_DIR"], str(config_dir))

    def test_opencode_adapter_resolves_user_install_path_when_path_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            binary = home / ".opencode" / "bin" / "opencode"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))

            with patch("opc.layer3_agent.adapters.opencode_adapter.Path.home", return_value=home), \
                patch("opc.layer3_agent.adapters.opencode_adapter.shutil.which", return_value=None):
                self.assertEqual(adapter.resolve_binary(), str(binary))
                cmd, metadata = adapter.build_invocation(Task(title="demo"), workspace_path="/repo")

            self.assertEqual(cmd[0], str(binary))
            self.assertEqual(metadata["binary"], str(binary))

    def test_opencode_adapter_auto_keeps_default_permission_config(self) -> None:
        adapter = OpenCodeAdapter(
            config=ExternalAgentConfig(command="opencode", approval_mode="auto")
        )

        with patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": '{"permission":"ask"}'}, clear=False):
            env = adapter.build_process_env(None)

        self.assertIsNone(env)

    def test_opencode_adapter_parses_edit_approval_request(self) -> None:
        adapter = OpenCodeAdapter()
        request = adapter.parse_approval_request(
            json.dumps(
                {
                    "type": "approval_request",
                    "sessionID": "ses_1",
                    "permission": {
                        "id": "perm_2",
                        "sessionID": "ses_1",
                        "permission": "edit",
                        "patterns": ["src/main.py"],
                        "metadata": {"filepath": "/repo/src/main.py", "diff": "@@ ..."},
                        "always": ["*"],
                    },
                }
            ),
            "stdout",
        )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.approval_scope, "tool")
        self.assertEqual(request.action_name, "file_edit")
        self.assertEqual(request.arguments["path"], "/repo/src/main.py")

    def test_opencode_adapter_permission_handler_flag_is_capability_gated(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))
        OpenCodeAdapter._permission_handler_support_cache.clear()

        with patch("opc.layer3_agent.adapters.opencode_adapter.shutil.which", return_value="/usr/bin/opencode"), \
            patch(
                "opc.layer3_agent.adapters.opencode_adapter.subprocess.run",
                return_value=SimpleNamespace(stdout="usage: opencode run\n  --format", stderr=""),
            ):
            cmd, _ = adapter.build_interactive_invocation(Task(title="demo"), workspace_path="/repo")
        self.assertNotIn("--permission-handler", cmd)

        OpenCodeAdapter._permission_handler_support_cache.clear()
        with patch("opc.layer3_agent.adapters.opencode_adapter.shutil.which", return_value="/usr/bin/opencode"), \
            patch(
                "opc.layer3_agent.adapters.opencode_adapter.subprocess.run",
                return_value=SimpleNamespace(stdout="--permission-handler <handler>", stderr=""),
            ):
            cmd, _ = adapter.build_interactive_invocation(Task(title="demo"), workspace_path="/repo")
        self.assertIn("--permission-handler", cmd)
        self.assertEqual(cmd[cmd.index("--permission-handler") + 1], "stdio-json")

    def test_opencode_keeps_stdin_only_for_permission_handler(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))

        self.assertFalse(
            adapter.keep_process_stdin_open([
                "opencode",
                "run",
                "--format",
                "json",
                "prompt",
            ])
        )
        self.assertTrue(
            adapter.keep_process_stdin_open([
                "opencode",
                "run",
                "--format",
                "json",
                "--permission-handler",
                "stdio-json",
                "prompt",
            ])
        )

    async def test_opencode_execute_uses_devnull_without_permission_handler(self) -> None:
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))
        task = Task(title="demo", description="body")
        proc = type(
            "Proc",
            (),
            {
                "pid": 321,
                "returncode": 0,
                "communicate": AsyncMock(return_value=(b"ok", b"")),
            },
        )()

        tmpdir = _make_test_dir("opencode-execute-devnull")
        try:
            with patch.object(adapter, "is_available", AsyncMock(return_value=True)), \
                patch(
                    "opc.layer3_agent.adapters.opencode_adapter.asyncio.create_subprocess_exec",
                    AsyncMock(return_value=proc),
                ) as spawn_mock:
                result = await adapter.execute(task, tmpdir)
        finally:
            _cleanup_test_dir(tmpdir)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(spawn_mock.await_args.kwargs["stdin"], asyncio.subprocess.DEVNULL)

    def test_opencode_adapter_normalizes_json_result_and_progress(self) -> None:
        adapter = OpenCodeAdapter()
        output = "\n".join([
            json.dumps({"type": "assistant_message", "sessionID": "ses_1", "message": "draft"}),
            json.dumps({"type": "result", "sessionID": "ses_1", "result": "final"}),
        ])

        self.assertEqual(adapter.extract_resume_session_id(output), "ses_1")
        self.assertEqual(adapter.normalize_result_output(output), "final")

        progress = adapter.format_progress_update(
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", "pytest -q"],
                },
            }),
            "stdout",
        )
        self.assertEqual(progress, "[External:opencode:tool] $ pytest -q")

    def test_opencode_adapter_normalizes_text_part_events(self) -> None:
        adapter = OpenCodeAdapter()
        output = "\n".join([
            json.dumps({
                "type": "step_start",
                "sessionID": "ses_1e8f73badffeGWsMS7O3kBWr3Y",
                "part": {"type": "step-start"},
            }),
            json.dumps({
                "type": "text",
                "sessionID": "ses_1e8f73badffeGWsMS7O3kBWr3Y",
                "part": {"type": "text", "text": "\n\nOK"},
            }),
            json.dumps({
                "type": "step_finish",
                "sessionID": "ses_1e8f73badffeGWsMS7O3kBWr3Y",
                "part": {"type": "step-finish"},
            }),
        ])

        self.assertEqual(adapter.extract_resume_session_id(output), "ses_1e8f73badffeGWsMS7O3kBWr3Y")
        self.assertEqual(adapter.normalize_result_output(output), "OK")
        self.assertEqual(
            adapter.format_progress_update(output.splitlines()[0], "stdout"),
            "[External:opencode:init] session=ses_1e8f",
        )
        self.assertEqual(
            adapter.format_progress_update(output.splitlines()[1], "stdout"),
            "[External:opencode:thinking] OK",
        )

    def test_opencode_adapter_formats_tool_use_part_events(self) -> None:
        adapter = OpenCodeAdapter()
        event = {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {
                        "command": "pwd && ls -la",
                        "description": "Show current directory and list files",
                    },
                    "output": "/tmp/opc_external_trace_sample\nalpha.txt\nbeta.md\n",
                },
            },
        }

        self.assertEqual(
            adapter.format_progress_update(json.dumps(event), "stdout"),
            "[External:opencode:tool] $ pwd && ls -la\nShow current directory and list files\n/tmp/opc_external_trace_sample\nalpha.txt\nbeta.md",
        )

    def test_opencode_adapter_summarizes_websearch_tool_events(self) -> None:
        adapter = OpenCodeAdapter()
        event = {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "type": "tool",
                "tool": "websearch",
                "state": {
                    "status": "completed",
                    "input": {"query": "SK Hynix stock price today"},
                    "output": json.dumps(
                        {
                            "results": [
                                {"title": "SK Hynix Inc. Quote", "url": "https://example.test/a"},
                                {"title": "Samsung Electronics Quote", "url": "https://example.test/b"},
                            ]
                        }
                    ),
                    "title": "Parallel Web Search",
                },
            },
        }

        progress = adapter.format_progress_update(json.dumps(event), "stdout")

        self.assertEqual(
            progress,
            "[External:opencode:tool] web search: SK Hynix stock price today\n"
            "Parallel Web Search\n"
            "results: SK Hynix Inc. Quote; Samsung Electronics Quote",
        )

    def test_opencode_adapter_never_returns_raw_json_when_final_text_is_missing(self) -> None:
        adapter = OpenCodeAdapter()
        output = "\n".join([
            json.dumps({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}}),
            json.dumps({
                "type": "reasoning",
                "sessionID": "ses_1",
                "part": {"type": "reasoning", "text": "I will search."},
            }),
            json.dumps({
                "type": "tool_use",
                "sessionID": "ses_1",
                "part": {
                    "type": "tool",
                    "tool": "websearch",
                    "state": {
                        "status": "completed",
                        "input": {"query": "SK Hynix 000660 stock price"},
                        "output": json.dumps({"results": [{"title": "SK Hynix Quote"}]}),
                    },
                },
            }),
        ])

        normalized = adapter.normalize_result_output(output)

        self.assertIn("OpenCode completed but did not emit a final assistant message.", normalized)
        self.assertIn("Tool activity:", normalized)
        self.assertIn("web search: SK Hynix 000660 stock price", normalized)
        self.assertNotIn('{"type"', normalized)

    def test_opencode_adapter_formats_structured_approval_response(self) -> None:
        adapter = OpenCodeAdapter()
        request = ExternalApprovalRequest(
            approval_scope="tool",
            action_name="shell_exec",
            prompt_text="Allow OpenCode to run `git status --short`?",
            arguments={"command": "git status --short"},
            metadata={"approval_id": "perm_1"},
        )
        decision = ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="human_escalation",
            metadata={"human_reply": "always_project"},
        )

        payload = json.loads(adapter.format_approval_response(request, True, decision))

        self.assertEqual(
            payload,
            {
                "type": "approval_response",
                "permission_id": "perm_1",
                "reply": "always",
            },
        )

    def test_broker_recognizes_staffing_selected_agent_as_explicit_choice(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())

        self.assertTrue(broker._task_explicitly_selected_external_agent(
            Task(
                title="Research",
                project_id="proj1",
                metadata={
                    "execution_agent_locked": True,
                    "selected_execution_agent": "cursor",
                    "selected_execution_agent_source": "recruitment_user_override",
                },
            ),
            "cursor",
        ))
        self.assertTrue(broker._task_explicitly_selected_external_agent(
            Task(
                title="Report",
                project_id="proj1",
                assigned_external_agent="opencode",
                metadata={"selected_execution_agent_source": "explicit_user_agent"},
            ),
            "opencode",
        ))
        self.assertFalse(broker._task_explicitly_selected_external_agent(
            Task(
                title="Fallback",
                project_id="proj1",
                assigned_external_agent="cursor",
                metadata={"selected_execution_agent_source": "router_fallback"},
            ),
            "cursor",
        ))

    def test_broker_extracts_structured_company_work_item_fields_from_external_result(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-structured",
            title="QA Review",
            description="Review the external execution output.",
            project_id="proj1",
            assigned_to="reviewer",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "review",
                "work_item_runtime_plan": {"turn_type": "review", "summary": "Fallback plan"},
            },
        )

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace"},
            normalized_output=json.dumps(
                {
                    "work_item_runtime_plan": {"turn_type": "review", "summary": "Review the execution output."},
                    "artifact_index": [{"kind": "file", "label": "review_report", "value": "reports/review.md"}],
                    "review_verdict": {
                        "review_verdict": "reject",
                        "summary": "Rollback coverage missing.",
                        "blocking_issues": ["Add rollback tests."],
                    },
                },
                ensure_ascii=False,
            ),
            base_artifacts={},
        )

        self.assertEqual(artifacts["work_item_runtime_plan"]["turn_type"], "review")
        self.assertEqual(artifacts["artifact_index"][0]["value"], "reports/review.md")
        self.assertEqual(artifacts["structured_review_verdict"]["label"], "reject")
        self.assertEqual(artifacts["review_verdict"]["label"], "reject")

    def test_broker_falls_back_to_task_plan_without_inferring_review_verdict_from_text(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-fallback",
            title="QA Review",
            description="Review the external execution output.",
            project_id="proj1",
            assigned_to="reviewer",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "review",
                "target_output_dir": "D:/repo/out",
                "work_item_runtime_plan": {
                    "turn_type": "review",
                    "summary": "Review the execution output.",
                    "deliverables": ["A review verdict."],
                },
            },
        )

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace", "target_output_dir": "D:/repo/out"},
            normalized_output="APPROVED. Artifacts ready at reports/final_review.md",
            base_artifacts={},
        )

        self.assertEqual(artifacts["work_item_runtime_plan"]["summary"], "Review the execution output.")
        self.assertNotIn("structured_review_verdict", artifacts)
        self.assertNotIn("review_verdict", artifacts)
        self.assertTrue(
            any(item["value"].endswith("reports/final_review.md") for item in artifacts["artifact_index"])
        )

    def test_broker_extracts_verification_evidence_from_structured_external_result(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-verification-structured",
            title="DevOps Execution",
            description="Run deploy validation.",
            project_id="proj1",
            assigned_to="devops_engineer",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "execute",
            },
        )

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace"},
            normalized_output=json.dumps(
                {
                    "verification_evidence": {
                        "status": "provided",
                        "verdict": "pass",
                        "summary": "Smoke checks passed.",
                        "checks": [
                            {
                                "check": "smoke",
                                "command": "pytest -q",
                                "observed_output": "1 passed",
                                "result": "PASS",
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            base_artifacts={},
        )

        self.assertEqual(artifacts["verification_evidence"]["status"], "provided")
        self.assertEqual(artifacts["verification_evidence"]["verdict"], "pass")

    def test_broker_infers_verification_evidence_from_plaintext_external_result(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-verification-plaintext",
            title="DevOps Execution",
            description="Run deploy validation.",
            project_id="proj1",
            assigned_to="devops_engineer",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "execute",
            },
        )

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace"},
            normalized_output=(
                "VERIFIED: deploy checks passed\n"
                "Check: smoke\n"
                "Command: pytest -q\n"
                "Observed Output: 1 passed\n"
                "Result: PASS\n"
                "VERDICT: PASS"
            ),
            base_artifacts={},
        )

        self.assertEqual(artifacts["verification_evidence"]["status"], "provided")
        self.assertEqual(artifacts["verification_evidence"]["checks"][0]["command"], "pytest -q")

    def test_broker_infers_verification_evidence_from_codex_command_events(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-verification-events",
            title="DevOps Execution",
            description="Run deploy validation.",
            project_id="proj1",
            assigned_to="devops_engineer",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "execute",
            },
        )

        raw_output = "\n".join([
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "cmd-1",
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", "pytest -q"],
                    "aggregated_output": "1 passed",
                    "exit_code": 0,
                    "status": "completed",
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"id": "item-2", "type": "agent_message", "text": "Checks completed."},
            }),
        ])

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace"},
            normalized_output="Checks completed.",
            raw_output=raw_output,
            base_artifacts={},
        )

        self.assertEqual(artifacts["verification_evidence"]["status"], "provided")
        self.assertEqual(artifacts["verification_evidence"]["verdict"], "pass")
        self.assertIn("pytest -q", artifacts["verification_evidence"]["checks"][0]["command"])

    def test_broker_extracts_collaboration_infrastructure_failure_from_command_events(self) -> None:
        broker = ExternalAgentBroker(_SessionStoreStub(), _ApprovalStub())
        adapter = CodexAdapter()
        task = Task(
            id="external-collab-infra-events",
            title="CEO Dispatch",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_turn_type": "intake",
            },
        )

        raw_output = "\n".join([
            json.dumps({
                "type": "item.completed",
                "item": {
                    "id": "cmd-1",
                    "type": "command_execution",
                    "command": ["/bin/bash", "-lc", "opc-collab manager_board_read --args-json '{}'"],
                    "aggregated_output": json.dumps(
                        {
                            "error": "collaboration broker RPC failed: disk I/O error",
                            "error_type": "infrastructure",
                            "retryable": True,
                            "tool_name": "manager_board_read",
                        }
                    ),
                    "exit_code": 1,
                    "status": "failed",
                },
            })
        ])

        artifacts = broker._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata={"workspace": "D:/repo/workspace"},
            normalized_output="NO_DELEGATION_JUSTIFICATION: manager_board_read failed with disk I/O error",
            raw_output=raw_output,
            base_artifacts={},
        )

        failure = artifacts["collaboration_infrastructure_failure"]
        self.assertEqual(failure["error_type"], "infrastructure")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["tool_name"], "manager_board_read")
        self.assertIn("disk I/O error", failure["observed_output"])

    async def test_broker_does_not_load_collaboration_cli_in_task_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            broker = ExternalAgentBroker(store, _ApprovalStub())
            adapter = _CollabSurfaceScriptAdapter("print('task mode external run')\n", home_slug=None)
            task = Task(
                id="task-mode-no-collab-cli",
                title="task mode no collab cli",
                project_id="proj1",
                assigned_to="task_generalist",
                metadata={
                    "mode": "task",
                    "execution_mode": ExecutionMode.TASK_MODE.value,
                    "work_item_role_id": "task_generalist",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
            )

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            # No collab env/tool surface in task mode.
            env = adapter.started_envs[-1]
            for collab_only_key in (
                "OPC_COMMS_FROM",
                "OPC_COMMS_PROJECT",
                "OPC_COMMS_SESSION",
                "OPC_COLLAB_PROFILE",
                "OPC_ALLOWED_COLLAB_TOOLS",
                "OPC_MAILBOX_MODE",
                "OPC_COLLAB_CLI",
                "OPC_WORK_ITEM_ID",
                "OPC_RUNTIME_TASK_ID",
                "CODEX_HOME",
                "CLAUDE_CONFIG_DIR",
            ):
                self.assertNotIn(collab_only_key, env)
            self.assertIn("OPC_MEMORY_ROOT", env)
            self.assertIn("OPC_GLOBAL_MEMORY_PATH", env)
            self.assertIn("OPC_PROJECT_MEMORY_PATH", env)
            self.assertEqual(Path(env["OPC_PROJECT_MEMORY_PATH"]).name, "proj1.md")
            await store.close()

    async def test_collab_cli_dispatch_rejects_removed_send_dm_and_read_inbox_args(self) -> None:
        from opc.layer4_tools.collaboration_dispatch import dispatch_collaboration_tool

        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            task = Task(
                id="cli-collab",
                title="cli collab",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="executor",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "executor",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
            )
            await store.save_task(task)
            env = {
                "OPC_COMMS_FROM": "executor",
                "OPC_COMMS_PROJECT": "proj1",
                "OPC_COMMS_SESSION": "root-session",
                "OPC_WORKSPACE_ROOT": tmpdir,
                "OPC_TASK_ID": task.id,
                "OPC_PROJECT_DB_PATH": store.db_path,
                "OPC_COLLAB_PROFILE": "debug_admin",
                "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(["send_dm", "read_inbox"]),
                "OPC_MAILBOX_MODE": "runtime_owned",
            }
            with patch.dict(os.environ, env, clear=False):
                send_payload, send_error = await dispatch_collaboration_tool(
                    "send_dm",
                    {
                        "to_agent": "reviewer",
                        "subject": "Need review",
                        "body": "Please review.",
                        "blocking": True,
                    },
                )
                read_payload, read_error = await dispatch_collaboration_tool(
                    "read_inbox",
                    {"limit": 5, "mark_read": False},
                )

            self.assertTrue(send_error)
            self.assertTrue(read_error)
            self.assertIn("no longer accepts `blocking`", send_payload["error"])
            self.assertIn("no longer accepts `mark_read`", read_payload["error"])
            await store.close()

    async def test_collab_cli_dispatch_error_response_includes_mailbox_notice(self) -> None:
        from opc.layer4_tools.collaboration_dispatch import dispatch_collaboration_tool

        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            task = Task(
                id="cli-collab-notice",
                title="cli collab notice",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="executor",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "executor",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
            )
            await store.save_task(task)
            layout = file_comms.resolve_layout(tmpdir, "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Please respond",
                body="This should show up as a notice.",
            )
            env = {
                "OPC_COMMS_FROM": "executor",
                "OPC_COMMS_PROJECT": "proj1",
                "OPC_COMMS_SESSION": "root-session",
                "OPC_WORKSPACE_ROOT": tmpdir,
                "OPC_TASK_ID": task.id,
                "OPC_PROJECT_DB_PATH": store.db_path,
                "OPC_COLLAB_PROFILE": "worker_default",
                "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(["send_dm", "inbox"]),
                "OPC_MAILBOX_MODE": "runtime_owned",
            }
            with patch.dict(os.environ, env, clear=False):
                payload, is_error = await dispatch_collaboration_tool(
                    "send_dm",
                    {
                        "to_agent": "reviewer",
                        "subject": "Need review",
                        "body": "Please review.",
                        "blocking": True,
                    },
                )

            self.assertTrue(is_error)
            self.assertIn("mailbox_notice", payload)
            notice = payload["mailbox_notice"]
            self.assertTrue(notice["has_actionable_unread"])
            self.assertEqual(notice["actionable_count"], 1)
            self.assertFalse(list(layout.role_seen_dir("executor").glob("*.md")))
            await store.close()

    def test_collab_cli_exposes_manager_board_mutation_commands(self) -> None:
        from opc import cli_collab
        from opc.layer4_tools.collaboration_dispatch import HANDLERS

        self.assertIn("modify_work_item", cli_collab.TOOL_SUBCOMMANDS)
        self.assertIn("delete_work_item", cli_collab.TOOL_SUBCOMMANDS)
        self.assertIn("modify_work_item", HANDLERS)
        self.assertIn("delete_work_item", HANDLERS)

    async def test_collab_dispatch_uses_allowed_tool_env(self) -> None:
        from opc.layer4_tools.collaboration_dispatch import (
            _allowed_tool_names,
            _build_runtime,
            dispatch_collaboration_tool,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            task = Task(
                id="cli-list-tools",
                title="cli list tools",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="cto",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "cto",
                    "work_item_projection_id": "cto_plan",
                    "work_item_turn_type": "plan",
                    "managed_team_id": "team::cto",
                    "delegation_seat_id": "seat::team::ceo::cto",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
            )
            await store.save_task(task)
            env = {
                "OPC_COMMS_FROM": "cto",
                "OPC_COMMS_PROJECT": "proj1",
                "OPC_COMMS_SESSION": "root-session",
                "OPC_WORKSPACE_ROOT": tmpdir,
                "OPC_TASK_ID": task.id,
                "OPC_PROJECT_DB_PATH": store.db_path,
                "OPC_COLLAB_PROFILE": "manager_default",
                "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(
                    ["send_dm", "delegate_work", "manager_board_read"]
                ),
                "OPC_MAILBOX_MODE": "runtime_owned",
            }
            with patch.dict(os.environ, env, clear=False):
                _service, context, client_store, manager = await _build_runtime()
                try:
                    allowed = _allowed_tool_names(task=context.task, context=context, manager=manager)
                finally:
                    if client_store is not None:
                        await client_store.close()
                payload, is_error = await dispatch_collaboration_tool("read_inbox", {"limit": 5})

            self.assertEqual(allowed, {"delegate_work", "manager_board_read", "send_dm"})
            self.assertTrue(is_error)
            self.assertIn("not available", payload["error"])
            await store.close()

    async def test_external_broker_injects_profile_sliced_collaboration_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            adapter = _CollabSurfaceScriptAdapter("print('company-mode external run')\n")
            broker = ExternalAgentBroker(store, _ApprovalStub())
            fake_home = Path(tmpdir) / "agent-home"
            fake_bin = Path(tmpdir) / "bin"
            fake_bin.mkdir()
            fake_cli = fake_bin / "opc-collab"
            task = Task(
                id="collab-env",
                title="collab env",
                project_id="proj1",
                assigned_to="cto",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "cto",
                    "work_item_projection_id": "cto_plan",
                    "work_item_turn_type": "plan",
                    "managed_team_id": "team::cto",
                    "delegation_seat_id": "seat::team::ceo::cto",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
                context_snapshot={},
            )
            set_linked_work_item_id(task, "wi-cto-current")

            with patch(
                "opc.layer3_agent.external_broker.install_collab_surface",
                return_value=(fake_home, fake_bin),
            ), patch(
                "opc.layer3_agent.external_broker.opc_collab_executable",
                return_value=fake_cli,
            ):
                result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            env = adapter.started_envs[-1]
            self.assertEqual(env["OPC_COLLAB_PROFILE"], "manager_default")
            self.assertEqual(env["OPC_MAILBOX_MODE"], "runtime_owned")
            self.assertEqual(env["OPC_WORK_ITEM_ID"], "wi-cto-current")
            self.assertEqual(env["OPC_RUNTIME_TASK_ID"], task.id)
            allowed = set(json.loads(env["OPC_ALLOWED_COLLAB_TOOLS"]))
            self.assertIn("delegate_work", allowed)
            self.assertIn("modify_work_item", allowed)
            self.assertIn("delete_work_item", allowed)
            self.assertIn("manager_board_read", allowed)
            self.assertNotIn("read_inbox", allowed)
            self.assertEqual(env["OPC_COLLAB_CLI"], str(fake_cli))
            self.assertTrue(env["PATH"].startswith(str(fake_bin) + os.pathsep))
            self.assertIn("OPC_COLLAB_RPC_TRANSPORT", env)
            self.assertIn("OPC_COLLAB_RPC_TOKEN", env)
            if env["OPC_COLLAB_RPC_TRANSPORT"] == "tcp":
                self.assertEqual(env["OPC_COLLAB_RPC_HOST"], "127.0.0.1")
                self.assertTrue(int(env["OPC_COLLAB_RPC_PORT"]) > 0)
                self.assertNotIn("OPC_COLLAB_RPC_PATH", env)
            else:
                self.assertEqual(env["OPC_COLLAB_RPC_TRANSPORT"], "fifo")
                self.assertIn("OPC_COLLAB_RPC_PATH", env)
            self.assertIn("OPC_MEMORY_ROOT", env)
            self.assertIn("OPC_GLOBAL_MEMORY_PATH", env)
            self.assertIn("OPC_PROJECT_MEMORY_PATH", env)
            self.assertEqual(Path(env["OPC_PROJECT_MEMORY_PATH"]).name, "proj1.md")

    async def test_external_broker_reports_collaboration_rpc_setup_failure_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            adapter = _CollabSurfaceScriptAdapter("print('should not launch')\n")
            broker = ExternalAgentBroker(store, _ApprovalStub())
            fake_home = Path(tmpdir) / "agent-home"
            fake_bin = Path(tmpdir) / "bin"
            fake_bin.mkdir()
            fake_cli = fake_bin / "opc-collab"
            task = Task(
                id="collab-rpc-setup-failure",
                title="collab rpc setup failure",
                project_id="proj1",
                assigned_to="cto",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "cto",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
                context_snapshot={},
            )
            set_linked_work_item_id(task, "wi-cto-current")

            with patch(
                "opc.layer3_agent.external_broker.install_collab_surface",
                return_value=(fake_home, fake_bin),
            ), patch(
                "opc.layer3_agent.external_broker.opc_collab_executable",
                return_value=fake_cli,
            ), patch(
                "opc.layer3_agent.external_broker.start_collaboration_rpc_server",
                side_effect=RuntimeError("FIFO collaboration RPC is unavailable on this platform"),
            ):
                result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertIn("Company collaboration RPC setup failed", result.content)
            self.assertEqual(adapter.started_envs, [])
            self.assertFalse(result.artifacts["collaboration_rpc"]["enabled"])
            self.assertIn("FIFO collaboration RPC is unavailable", result.artifacts["collaboration_rpc"]["error"])

    async def test_collab_cli_routes_all_tools_through_broker_rpc_when_available(self) -> None:
        from opc import cli_collab
        from opc.layer4_tools.collaboration_rpc import start_collaboration_rpc_server

        calls: list[tuple[str, dict[str, object]]] = []

        async def _dispatch(tool_name: str, args: dict[str, object]):
            calls.append((tool_name, dict(args)))
            return {"ok": True, "tool": tool_name}, False

        server = await start_collaboration_rpc_server(_dispatch)
        self.assertIsNotNone(server)
        assert server is not None
        try:
            with patch.dict(os.environ, server.client_env, clear=False):
                delegated, delegated_error = await cli_collab._dispatch(
                    "delegate_work",
                    {"items": [{"role_id": "cto", "title": "Build"}]},
                )
                dm, dm_error = await cli_collab._dispatch(
                    "send_dm",
                    {"to_agent": "cto", "subject": "Ping", "body": "Hello"},
                )
        finally:
            await server.close()

        self.assertFalse(delegated_error)
        self.assertFalse(dm_error)
        self.assertEqual(delegated["tool"], "delegate_work")
        self.assertEqual(dm["tool"], "send_dm")
        self.assertEqual([name for name, _args in calls], ["delegate_work", "send_dm"])

    async def test_collab_rpc_unavailable_returns_typed_infrastructure_error(self) -> None:
        from opc.layer4_tools.collaboration_rpc import call_collaboration_rpc

        payload, is_error = await call_collaboration_rpc(
            "manager_board_read",
            {"include_children": True},
            env={},
        )

        self.assertTrue(is_error)
        self.assertEqual(payload["error_type"], "infrastructure")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["tool_name"], "manager_board_read")

    def test_collab_cli_exposes_manager_board_mutation_tools(self) -> None:
        from opc import cli_collab
        from opc.layer4_tools.collaboration_dispatch import HANDLERS

        self.assertIn("modify_work_item", cli_collab.TOOL_SUBCOMMANDS)
        self.assertIn("delete_work_item", cli_collab.TOOL_SUBCOMMANDS)
        self.assertIn("modify_work_item", HANDLERS)
        self.assertIn("delete_work_item", HANDLERS)
        parser = cli_collab._build_parser()

        modified = parser.parse_args(
            [
                "modify_work_item",
                "--args-json",
                '{"work_item_id": "wi-1", "reason": "revise"}',
            ]
        )
        deleted = parser.parse_args(
            [
                "delete_work_item",
                "--args-json",
                '{"work_item_id": "wi-1", "reason": "obsolete"}',
            ]
        )

        self.assertEqual(modified.tool, "modify_work_item")
        self.assertEqual(deleted.tool, "delete_work_item")

    def test_collab_cli_reads_args_json_file(self) -> None:
        from opc import cli_collab

        with tempfile.TemporaryDirectory() as tmpdir:
            args_path = Path(tmpdir) / "args.json"
            args_path.write_text('{"action": "status", "limit": 2}', encoding="utf-8")
            parser = cli_collab._build_parser()
            opts = parser.parse_args(["inbox", "--args-json-file", str(args_path)])

            self.assertEqual(
                cli_collab._collect_tool_args(opts),
                {"action": "status", "limit": 2},
            )

    async def test_broker_rpc_delegate_work_does_not_use_child_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Dispatch the project.",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Dispatch",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "work_item_role_id": "ceo",
                    "work_item_projection_id": "ceo_intake",
                    "work_item_turn_type": "intake",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cto"],
                    "allowed_delegate_role_ids": ["cto"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cto",
                                "seat_id": "seat::team::ceo::cto",
                                "team_id": "team::cto",
                            },
                        ]
                    },
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
                context_snapshot={},
            )
            set_linked_work_item_id(task, "parent-item")
            await store.save_task(task)

            fake_home = Path(tmpdir) / "agent-home"
            fake_bin = Path(tmpdir) / "bin"
            fake_bin.mkdir()
            if os.name == "nt":
                fake_cli = fake_bin / "opc-collab.cmd"
                fake_cli.write_text(
                    "@echo off\r\n"
                    f'"{sys.executable}" -m opc.cli_collab %*\r\n',
                    encoding="utf-8",
                )
            else:
                fake_cli = fake_bin / "opc-collab"
                fake_cli.write_text(
                    "#!/bin/sh\n"
                    f'exec "{sys.executable}" -m opc.cli_collab "$@"\n',
                    encoding="utf-8",
                )
                fake_cli.chmod(0o755)

            payload = {
                "planning_context": "Dispatch a minimal engineering slice.",
                "items": [
                    {
                        "role_id": "cto",
                        "title": "Build the app",
                        "brief": "Context: build. Mission: deliver runnable code.",
                        "outputs": ["app source"],
                        "done_when": ["app runs"],
                    }
                ],
            }
            read_payload = {"parent_work_item_id": "parent-item", "include_children": True}
            script = (
                "import json, os, subprocess, sys, tempfile\n"
                "os.environ['OPC_PROJECT_DB_PATH'] = '/definitely/not/the/real/tasks.db'\n"
                "collab_cli = os.environ.get('OPC_COLLAB_CLI') or 'opc-collab'\n"
                f"read_payload = {json.dumps(read_payload)!r}\n"
                f"payload = {json.dumps(payload)!r}\n"
                "with tempfile.TemporaryDirectory() as tmpdir:\n"
                "    read_path = os.path.join(tmpdir, 'read.json')\n"
                "    payload_path = os.path.join(tmpdir, 'payload.json')\n"
                "    with open(read_path, 'w', encoding='utf-8') as fh:\n"
                "        fh.write(read_payload)\n"
                "    with open(payload_path, 'w', encoding='utf-8') as fh:\n"
                "        fh.write(payload)\n"
                "    read_proc = subprocess.run(\n"
                "        [collab_cli, 'manager_board_read', '--args-json-file', read_path],\n"
                "        text=True,\n"
                "        capture_output=True,\n"
                "    )\n"
                "    sys.stdout.write(read_proc.stdout)\n"
                "    sys.stderr.write(read_proc.stderr)\n"
                "    if read_proc.returncode:\n"
                "        sys.exit(read_proc.returncode)\n"
                "    proc = subprocess.run(\n"
                "        [collab_cli, 'delegate_work', '--args-json-file', payload_path],\n"
                "        text=True,\n"
                "        capture_output=True,\n"
                "    )\n"
                "    sys.stdout.write(proc.stdout)\n"
                "    sys.stderr.write(proc.stderr)\n"
                "    sys.exit(proc.returncode)\n"
            )
            adapter = _CollabSurfaceScriptAdapter(script)
            from opc.core.events import EventBus
            from opc.layer2_organization.communication import CommunicationManager

            hook_calls: list[str] = []
            communication = CommunicationManager(store, EventBus())
            communication.on_work_items_created = lambda: hook_calls.append("wake")

            async def _on_kanban_changed() -> None:
                hook_calls.append("kanban")

            communication.on_kanban_changed = _on_kanban_changed
            broker = ExternalAgentBroker(store, _ApprovalStub(), communication=communication)
            with patch(
                "opc.layer3_agent.external_broker.install_collab_surface",
                return_value=(fake_home, fake_bin),
            ), patch(
                "opc.layer3_agent.external_broker.opc_collab_executable",
                return_value=fake_cli,
            ), patch(
                "opc.layer4_tools.collaboration_dispatch.OPCStore",
                side_effect=AssertionError("RPC handler reopened project DB"),
            ):
                result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE, result.content)
            self.assertIn('"parent_board_scope"', result.content)
            self.assertIn('"delegated"', result.content)
            children = await store.list_manager_board(
                "run-1",
                manager_seat_id="seat::team::ceo::ceo",
                parent_work_item_id="parent-item",
            )
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].role_id, "cto")
            self.assertIn("wake", hook_calls)
            self.assertIn("kanban", hook_calls)
            await store.close()

    async def test_collab_runtime_client_open_does_not_sweep_claims(self) -> None:
        from opc.layer4_tools.collaboration_dispatch import _build_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            task = Task(
                id="runtime-task",
                title="runtime task",
                project_id="proj1",
                session_id="work-item-session",
                parent_session_id="root-session",
                assigned_to="cto",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "work_item_role_id": "cto",
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
            )
            await store.save_task(task)
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="wi-running",
                    run_id="run-1",
                    cell_id="cell-1",
                    role_id="cto",
                    seat_id="seat::cto",
                    title="Running card",
                    phase=Phase.RUNNING,
                    claimed_by_role_runtime_session_id="dead-role-session",
                    claimed_by_seat_id="seat::cto",
                )
            )
            env = {
                "OPC_COMMS_FROM": "cto",
                "OPC_COMMS_PROJECT": "proj1",
                "OPC_COMMS_SESSION": "root-session",
                "OPC_WORKSPACE_ROOT": tmpdir,
                "OPC_TASK_ID": task.id,
                "OPC_RUNTIME_TASK_ID": task.id,
                "OPC_WORK_ITEM_ID": "wi-running",
                "OPC_PROJECT_DB_PATH": store.db_path,
                "OPC_COLLAB_PROFILE": "manager_default",
                "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(["manager_board_read"]),
                "OPC_MAILBOX_MODE": "runtime_owned",
            }
            with patch.dict(os.environ, env, clear=False):
                _service, _context, client_store, _manager = await _build_runtime()
                if client_store is not None:
                    await client_store.close()

            refreshed = await store.get_delegation_work_item("wi-running")
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.claimed_by_role_runtime_session_id, "dead-role-session")
            self.assertEqual(refreshed.claimed_by_seat_id, "seat::cto")
            await store.close()

    async def test_external_broker_strips_coordinator_only_collab_tools_in_multi_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _SessionStoreStub()
            adapter = _CollabSurfaceScriptAdapter("print('company-mode external run')\n")
            from opc.layer2_organization.org_engine import OrgEngine
            from opc.core.config import OPCConfig
            org_engine = OrgEngine(OPCConfig(), Path(tmpdir))
            broker = ExternalAgentBroker(store, _ApprovalStub(), org_engine=org_engine)
            self.assertIs(broker.org_engine, org_engine)
            task = Task(
                id="collab-env-ceo",
                title="collab env ceo",
                project_id="proj1",
                assigned_to="ceo",
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "runtime_model": "multi_team_org",
                    "work_item_role_id": "ceo",
                    "work_item_projection_id": "ceo_intake",
                    "work_item_turn_type": "intake",
                    "current_turn_mode": "dispatch_required",
                    "managed_team_id": "team::ceo",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "runtime_topology": {
                        "seats": [
                            {
                                "seat_id": "seat::team::ceo::ceo",
                                "team_id": "team::ceo",
                                "role_id": "ceo",
                                "managed_team_id": "team::ceo",
                            },
                            {
                                "seat_id": "seat::team::ceo::cto",
                                "team_id": "team::ceo",
                                "role_id": "cto",
                                "manager_role_id": "ceo",
                            },
                        ]
                    },
                    "workspace_root": tmpdir,
                    "comms_workspace_root": tmpdir,
                    "comms_root": os.path.join(tmpdir, ".opc-comms"),
                },
                context_snapshot={},
            )

            result = await broker.run(adapter, task, tmpdir)

            self.assertEqual(result.status, TaskStatus.DONE)
            env = adapter.started_envs[-1]
            allowed = set(json.loads(env["OPC_ALLOWED_COLLAB_TOOLS"]))
            self.assertIn("delegate_work", allowed)
            self.assertIn("manager_board_read", allowed)
            self.assertNotIn("route_work", allowed)
            self.assertNotIn("propose_runtime_replan", allowed)
            self.assertNotIn("propose_task_adjustment", allowed)
            self.assertNotIn("find_and_ask_expert", allowed)
            self.assertNotIn("ask_peer_and_wait", allowed)

    async def test_execute_task_preserves_task_result_when_retry_is_cancelled(self) -> None:
        engine = OPCEngine()

        class _RetryStore:
            def __init__(self) -> None:
                self.calls = 0
                self.saved: list[Task] = []

            async def get_task(self, task_id: str) -> Task:
                self.calls += 1
                status = TaskStatus.PENDING if self.calls == 1 else TaskStatus.CANCELLED
                return Task(
                    id=task_id,
                    title="retry task",
                    project_id="proj1",
                    status=status,
                )

            async def save_task(self, task: Task) -> None:
                self.saved.append(task)

        task = Task(id="retry-task", title="retry task", project_id="proj1")
        task.max_retries = 1
        attempt_results = [
            TaskResult(status=TaskStatus.FAILED, content="first failure", artifacts={}),
            TaskResult(status=TaskStatus.DONE, content="second result", artifacts={"source": "retry"}),
        ]

        async def _run_task_once(_task: Task) -> TaskResult:
            return attempt_results.pop(0)

        engine.store = _RetryStore()
        engine.memory = None
        engine._run_task_once = _run_task_once  # type: ignore[method-assign]
        engine._attempt_capability_recovery = AsyncMock()

        result = await engine._execute_task(task)

        self.assertIsInstance(result, TaskResult)
        self.assertEqual(result.content, "second result")
        self.assertEqual(result.artifacts["source"], "retry")


if __name__ == "__main__":
    unittest.main()
