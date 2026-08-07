from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import sqlite3
import tempfile
import unittest
import yaml
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import opc
from rich.console import Console
from typer.testing import CliRunner

from opc.cli.app import (
    BusyCommandPolicy,
    ChatTurnController,
    QueuedChatInput,
    _CliRuntimeDisplay,
    _InteractiveChatState,
    _OPCSlashCompleter,
    _chat_bottom_toolbar_text,
    _company_staffing_default_draft,
    _company_staffing_filter_options,
    _company_staffing_resume_metadata,
    _render_company_staffing_context_preview,
    _handle_chat_slash_command,
    _handle_company_staffing_shortcut,
    _handle_kanban_slash,
    _handle_work_items_slash,
    _busy_slash_policy,
    _normalize_interactive_preferred_agent,
    _process_interactive_chat_message,
    _restore_chat_context,
    _run_interactive_startup_selector,
    app,
    main,
    _format_escalation_option,
    _normalize_escalation_reply,
    _progress_callback,
    _project_config_template_dir,
)
from opc.core.config import EmployeeConfig, OPCConfig, RoleConfig, SeatConfig, TeamConfig
from opc.core.models import OPCEvent, TaskStatus
from opc.core.windows_ssl import (
    format_windows_sslkeylog_warning,
    pop_windows_sslkeylogfile,
    sanitize_windows_sslkeylogfile,
)
from opc.layer2_organization.talent_market import TalentMarket
from opc.plugins.office_ui.services.context import OfficeServiceContext
from opc.plugins.office_ui.services.project import ProjectService
from opc.plugins.office_ui.services.session import SessionService
from opc.plugins.office_ui.services.work_item import WorkItemService

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain_output(text: str) -> str:
    """CLI output with ANSI styling stripped.

    Keeps plain-text assertions stable when the invoking shell forces
    color (e.g. FORCE_COLOR) onto rich/typer output.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


class CliEscalationFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.options = [
            {"id": "approve_once", "label": "Approve once"},
            {"id": "deny", "label": "Deny"},
            {"id": "always_project", "label": "Always allow for this project"},
        ]

    def test_format_escalation_option_shows_number_label_and_id(self) -> None:
        rendered = _format_escalation_option(self.options[0], 1)
        self.assertEqual(rendered, "  1. Approve once (approve_once)")

    def test_normalize_escalation_reply_accepts_option_id(self) -> None:
        self.assertEqual(
            _normalize_escalation_reply("approve_once", self.options),
            "approve_once",
        )

    def test_normalize_escalation_reply_accepts_label(self) -> None:
        self.assertEqual(
            _normalize_escalation_reply("Approve once", self.options),
            "approve_once",
        )

    def test_normalize_escalation_reply_is_case_insensitive(self) -> None:
        self.assertEqual(
            _normalize_escalation_reply("ALWAYS ALLOW FOR THIS PROJECT", self.options),
            "always_project",
        )

    def test_normalize_escalation_reply_accepts_numeric_choice(self) -> None:
        self.assertEqual(_normalize_escalation_reply("2", self.options), "deny")

    def test_normalize_escalation_reply_accepts_common_aliases(self) -> None:
        self.assertEqual(_normalize_escalation_reply("yes", self.options), "approve_once")
        self.assertEqual(_normalize_escalation_reply("project", self.options), "always_project")

    def test_normalize_escalation_reply_rejects_unknown_input(self) -> None:
        self.assertIsNone(_normalize_escalation_reply("something else", self.options))


class ConfigModeCompatibilityTests(unittest.TestCase):
    def test_legacy_project_mode_config_loads_as_task_mode_and_saves_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "system_config.yaml").write_text(
                "system:\n"
                "  project_mode:\n"
                "    max_sub_agents: 11\n"
                "autonomy: {}\n"
                "capabilities: {}\n",
                encoding="utf-8",
            )

            config = OPCConfig.load(path)
            self.assertEqual(config.system.task_mode.max_sub_agents, 11)

            config.save(path)
            saved = (path / "system_config.yaml").read_text(encoding="utf-8")
            self.assertIn("task_mode:", saved)
            self.assertNotIn("project_mode:", saved)

    def test_legacy_prompt_profiles_config_is_ignored_and_dropped_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            (path / "system_config.yaml").write_text(
                "system:\n"
                "  native_runtime:\n"
                "    prompt_profiles:\n"
                "      enabled: true\n"
                "      default_task_profile: coding\n"
                "      plan_profile: plan\n"
                "      review_profile: review\n"
                "      verification_profile: verify\n"
                "autonomy: {}\n"
                "capabilities: {}\n",
                encoding="utf-8",
            )

            config = OPCConfig.load(path)
            self.assertFalse(hasattr(config.system.native_runtime, "prompt_profiles"))

            config.save(path)
            saved = (path / "system_config.yaml").read_text(encoding="utf-8")
            self.assertNotIn("prompt_profiles:", saved)
            self.assertNotIn("default_task_profile:", saved)


class CliInitProjectTests(unittest.TestCase):
    def _write_fake_agent_binary(self, bin_dir: Path, name: str) -> None:
        flags = (
            "--version --help exec run --json -C --add-dir --sandbox -c --model "
            "--print --output-format --verbose --include-partial-messages --permission-mode "
            "-p --trust --force --format --dangerously-skip-permissions --permission-handler"
        )
        if os.name == "nt":
            path = bin_dir / f"{name}.cmd"
            path.write_text(
                "@echo off\r\n"
                f"echo {name} smoke 0.0.0 {flags}\r\n"
                "exit /b 0\r\n",
                encoding="utf-8",
            )
        else:
            path = bin_dir / name
            path.write_text(
                "#!/usr/bin/env sh\n"
                f"echo '{name} smoke 0.0.0 {flags}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            path.chmod(0o755)

    def test_external_agent_preflight_accepts_fake_agent_binaries(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            workplace_root = root / "OpenOPC_workplace"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            for binary in ("codex", "claude", "cursor-agent", "opencode"):
                self._write_fake_agent_binary(fake_bin, binary)

            env_path = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
            with patch.dict(os.environ, {"PATH": env_path}, clear=False), patch(
                "opc.cli.app.get_opc_home",
                return_value=opc_home,
            ), patch(
                "opc.core.config.get_opc_home",
                return_value=opc_home,
            ), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                init_result = runner.invoke(app, ["init", "smoke"])
                status_result = runner.invoke(app, ["status", "--project", "smoke"])

            self.assertEqual(init_result.exit_code, 0, init_result.output)
            self.assertEqual(status_result.exit_code, 0, status_result.output)
            self.assertIn("External agent preflight", init_result.output)
            for agent in ("codex", "claude_code", "cursor", "opencode"):
                self.assertIn(agent, status_result.output)
            self.assertIn("writable", status_result.output)

    def test_init_provisions_external_agent_surfaces_and_trust_rules(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            workplace_root = root / "OpenOPC_workplace"

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                result = runner.invoke(app, ["init", "cli_proj"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((opc_home / "bin" / "opc-collab").is_file())
            self.assertTrue((opc_home / "agent_homes" / "codex" / "skills" / "opc-collab" / "SKILL.md").exists())
            allowlist = yaml.safe_load((opc_home / "config" / "approval_allowlist.yaml").read_text(encoding="utf-8"))
            patterns = allowlist["projects"]["cli_proj"]["external_agent"]["codex"]
            self.assertIn("*", patterns)

    def test_project_config_template_dir_falls_back_to_packaged_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / "project" / ".opc"
            package_root = root / "site" / "opc"
            template = package_root / "config_templates"
            template.mkdir(parents=True)
            (template / "llm_config.yaml").write_text("llm:\n  default_model: packaged/model\n", encoding="utf-8")
            (template / "system_config.yaml").write_text("system: {}\nautonomy: {}\ncapabilities: {}\n", encoding="utf-8")

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.cli.app.importlib_resources.files",
                return_value=package_root,
            ):
                self.assertEqual(Path(_project_config_template_dir()), template)

    def test_init_can_load_packaged_config_template_when_repo_template_missing(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / "project" / ".opc"
            workplace_root = root / "workplace"
            package_root = root / "site" / "opc"
            template = package_root / "config_templates"
            template.mkdir(parents=True)
            (template / "llm_config.yaml").write_text(
                "llm:\n  default_model: packaged/model\n  api_key: template-key\n",
                encoding="utf-8",
            )
            (template / "system_config.yaml").write_text("system: {}\nautonomy: {}\ncapabilities: {}\n", encoding="utf-8")
            (template / "agent_config.yaml").write_text("external_agents:\n  preferred_order: []\n", encoding="utf-8")
            (template / "channel_config.yaml").write_text("channels: {}\n", encoding="utf-8")

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ), patch(
                "opc.cli.app.importlib_resources.files",
                return_value=package_root,
            ):
                result = runner.invoke(app, ["init", "--no-external-agent-preflight", "--no-trust-external-agents"])

            self.assertEqual(result.exit_code, 0, result.output)
            saved = yaml.safe_load((opc_home / "config" / "llm_config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(saved["llm"]["default_model"], "packaged/model")
            self.assertEqual(saved["llm"]["api_key"], "")

    def test_init_project_creates_runtime_memory_and_workplace_dirs(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            workplace_root = root / "OpenOPC_workplace"

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                result = runner.invoke(app, ["init", "cli_proj"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue((opc_home / "projects" / "cli_proj").is_dir())
            self.assertTrue((opc_home / "memory" / "projects" / "cli_proj.md").is_file())
            self.assertTrue((workplace_root / "cli_proj").is_dir())

    def test_init_project_rejects_duplicate_memory_or_workplace_artifacts(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            workplace_root = root / "OpenOPC_workplace"
            (opc_home / "memory" / "projects").mkdir(parents=True)
            (opc_home / "memory" / "projects" / "dupe.md").write_text("# Project Memory (dupe)\n", encoding="utf-8")

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                result = runner.invoke(app, ["init", "dupe"])

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Project 'dupe' already exists", _plain_output(result.output))
            self.assertFalse((opc_home / "projects" / "dupe").exists())

    def test_init_existing_config_cancel_preserves_config_and_does_not_create_project(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            config_dir = opc_home / "config"
            workplace_root = root / "OpenOPC_workplace"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "llm_config.yaml"
            config_file.write_text("llm:\n  api_key: keep-me\n", encoding="utf-8")

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                result = runner.invoke(app, ["init", "new_proj"], input="n\n")

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Init cancelled", result.output)
            self.assertEqual(config_file.read_text(encoding="utf-8"), "llm:\n  api_key: keep-me\n")
            self.assertFalse((opc_home / "projects" / "new_proj").exists())
            self.assertFalse((workplace_root / "new_proj").exists())

    def test_init_existing_config_yes_preserves_config_and_creates_project(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            opc_home = root / ".opc"
            config_dir = opc_home / "config"
            workplace_root = root / "OpenOPC_workplace"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "llm_config.yaml"
            config_file.write_text("llm:\n  api_key: keep-me\n", encoding="utf-8")

            with patch("opc.core.config.get_opc_home", return_value=opc_home), patch(
                "opc.core.config.get_project_workplace",
                side_effect=lambda project_id: workplace_root / str(project_id),
            ):
                result = runner.invoke(app, ["init", "new_proj"], input="y\n")

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Existing config preserved", result.output)
            self.assertEqual(config_file.read_text(encoding="utf-8"), "llm:\n  api_key: keep-me\n")
            self.assertTrue((opc_home / "projects" / "new_proj").is_dir())
            self.assertTrue((opc_home / "memory" / "projects" / "new_proj.md").is_file())
            self.assertTrue((workplace_root / "new_proj").is_dir())


class CliExternalProgressDisplayTests(unittest.TestCase):
    def test_progress_callback_shows_external_status_approval_and_denial(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        async def _run() -> None:
            callback = _progress_callback()
            await callback("[External status] codex started pid=42")
            await callback("[External approval] codex requested tool:shell_exec")
            await callback("[External agent denied] user denied codex")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("[External status] codex started pid=42", rendered)
        self.assertIn("[External approval] codex requested tool:shell_exec", rendered)
        self.assertIn("[External agent denied] user denied codex", rendered)

    def test_progress_callback_hides_external_detail_by_default(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        async def _run() -> None:
            callback = _progress_callback()
            await callback("[External status] codex working; last activity 10s ago")
            await callback("[External:codex:thinking] checking tools")
            await callback("[External:codex:tool] $ npm test")

        with patch("opc.cli.app.console", console), patch.dict(os.environ, {"OPC_CLI_VERBOSE_EXTERNAL": ""}, clear=False):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertNotIn("last activity", rendered)
        self.assertNotIn("checking tools", rendered)
        self.assertNotIn("$ npm test", rendered)

    def test_progress_callback_can_show_external_detail_with_verbose_env(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        async def _run() -> None:
            callback = _progress_callback()
            await callback("[External status] codex working; last activity 10s ago")
            await callback("[External:codex:thinking] checking tools")
            await callback("[External:codex:tool] $ npm test")

        with patch("opc.cli.app.console", console), patch.dict(os.environ, {"OPC_CLI_VERBOSE_EXTERNAL": "1"}, clear=False):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("last activity", rendered)
        self.assertIn("checking tools", rendered)
        self.assertIn("$ npm test", rendered)

    def test_runtime_display_streams_assistant_lines_and_status(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        async def _run() -> None:
            display = _CliRuntimeDisplay(console)
            display.set_checkpoint_hint("task_user_input")
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={
                "type": "status_snapshot",
                "agent_id": "owner",
                "task_id": "task-1",
                "queue_depth": 2,
                "context_remaining_pct": 72,
                "turn_cost_usd": 0.0123,
                "session_cost_usd": 0.0456,
                "pending_permission_count": 1,
                "current_tool": "shell_exec",
                "tool_elapsed_ms": 320,
            }))
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={
                "type": "assistant_delta",
                "text": "line one\nline two",
            }))
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={
                "type": "turn_completed",
            }))
            await display.flush()

        asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("line one", rendered)
        self.assertIn("line two", rendered)
        self.assertIn("queue=2", rendered)
        self.assertIn("stream_queue=0", rendered)
        self.assertIn("agent=owner", rendered)
        self.assertIn("task=task-1", rendered)
        self.assertIn("context=72%", rendered)
        self.assertIn("approvals=1", rendered)
        self.assertIn("checkpoint=task_user_input", rendered)

    def test_runtime_display_quiet_mode_suppresses_sidecar_output(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        async def _run() -> int:
            display = _CliRuntimeDisplay(console)
            display.enter_sidecar_quiet()
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={
                "type": "status_snapshot",
                "agent_id": "cto",
                "task_id": "task-quiet",
            }))
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={
                "type": "assistant_delta",
                "text": "hidden output\n",
            }))
            await display.handle_event(OPCEvent(event_type="runtime_event", payload={"type": "turn_completed"}))
            count = display.exit_sidecar_quiet()
            await display.flush()
            self.assertEqual(display.status_snapshot()["agent_id"], "cto")
            return count

        count = asyncio.run(_run())

        rendered = console.export_text()
        self.assertEqual(count, 3)
        self.assertNotIn("hidden output", rendered)


class CliSlashCommandTests(unittest.TestCase):
    class _Display:
        has_streamed_content = False

        def __init__(self) -> None:
            self.checkpoint_hint = ""

        def begin_turn(self) -> None:
            self.has_streamed_content = False

        async def flush(self) -> None:
            return None

        def status_snapshot(self):
            return {
                "agent_id": "owner",
                "task_id": "task-1",
                "current_tool": "shell_exec",
                "queue_depth": 1,
                "stream_queue_depth": 0,
                "context_remaining_pct": 80,
                "turn_cost_usd": 0.01,
                "session_cost_usd": 0.02,
                "pending_permission_count": 1,
                "checkpoint_hint": self.checkpoint_hint,
            }

        def set_checkpoint_hint(self, hint: str) -> None:
            self.checkpoint_hint = hint

        async def render_status(self, *, force: bool = False) -> None:
            return None

    class _Store:
        def __init__(self) -> None:
            now = datetime(2026, 5, 3, 12, 0)
            self.sessions = [
                SimpleNamespace(
                    session_id="sess-1",
                    project_id="demo",
                    title="Demo Session",
                    mode="primary",
                    status="active",
                    updated_at=now,
                )
            ]
            self.tasks = [
                SimpleNamespace(
                    id="task-1",
                    title="Pending Task",
                    description="Needs work",
                    status=TaskStatus.PENDING,
                    priority=3,
                    assigned_to="owner",
                    session_id="sess-1",
                    project_id="demo",
                    created_at=now,
                    tags=["cli"],
                    result={"content": "Pending result", "artifacts": ["artifact.md"]},
                    metadata={
                        "source": "test",
                        "progress_log": ["Started task", "Called shell_exec"],
                        "runtime_v2": {"runtime_session_id": "rt-1", "status": "running"},
                        "attachment_refs": [{
                            "attachment_id": "att-1",
                            "filename": "brief.md",
                            "mime_type": "text/markdown",
                            "size_bytes": 42,
                            "disk_path": "projects/demo/attachments/att-1/brief.md",
                        }],
                        "execution_task_ids": ["task-1"],
                        "structured_review_verdict": {"summary": "Review accepted"},
                    },
                    context_snapshot={"handoff_context": "Worker handoff summary"},
                ),
                SimpleNamespace(
                    id="task-2",
                    title="Done Task",
                    description="Done",
                    status=TaskStatus.DONE,
                    priority=5,
                    assigned_to="owner",
                    session_id="sess-2",
                    project_id="demo",
                    created_at=now,
                    tags=[],
                    result={},
                    metadata={},
                    context_snapshot={},
                ),
                SimpleNamespace(
                    id="task-3",
                    title="Other Project Task",
                    description="Wrong project",
                    status=TaskStatus.PENDING,
                    priority=5,
                    assigned_to="owner",
                    session_id="sess-3",
                    project_id="other",
                    created_at=now,
                    tags=[],
                    result={},
                    metadata={},
                    context_snapshot={},
                ),
            ]
            self.checkpoints = [
                SimpleNamespace(
                    checkpoint_id="cp-1",
                    checkpoint_type="task_user_input",
                    status="pending",
                    task_id="task-1",
                    session_id="sess-1",
                    updated_at=now,
                    payload={"summary": "Waiting for approval"},
                )
            ]
            self.runtime_sessions = [
                {
                    "runtime_session_id": "rt-1",
                    "project_id": "demo",
                    "session_id": "sess-1",
                    "task_id": "task-1",
                    "status": "running",
                    "metadata": {"summary": "Runtime running", "current_tool": "shell_exec"},
                    "created_at": now,
                    "updated_at": now,
                }
            ]
            self.external_sessions = [
                SimpleNamespace(
                    agent_type="codex",
                    project_id="demo",
                    session_id="provider-1",
                    opc_session_id="sess-1",
                    task_id="task-1",
                    workspace_path=r"D:\work",
                    run_mode="interactive",
                    status="running",
                    metadata={},
                    updated_at=now,
                )
            ]
            self.runtime_events = [
                {
                    "event_id": "evt-1",
                    "runtime_session_id": "rt-1",
                    "event_type": "tool_started",
                    "payload": {"tool_name": "shell_exec", "summary": "Running command"},
                    "created_at": now,
                }
            ]
            self.runtime_transcript_entries = [
                {
                    "runtime_session_id": "rt-1",
                    "role": "assistant",
                    "entry_type": "message",
                    "content": "Runtime transcript entry",
                    "metadata": {},
                    "created_at": now,
                }
            ]
            self.runtime_tool_calls = [
                {
                    "runtime_session_id": "rt-1",
                    "tool_name": "shell_exec",
                    "arguments": {"command": "echo ok"},
                    "metadata": {},
                    "created_at": now,
                }
            ]
            self.runtime_tool_results = [
                {
                    "runtime_session_id": "rt-1",
                    "tool_name": "shell_exec",
                    "payload": {"result_summary": "ok"},
                    "metadata": {},
                    "created_at": now,
                }
            ]
            self.runtime_permission_grants = [
                {
                    "runtime_session_id": "rt-1",
                    "scope": "session",
                    "tool_name": "shell_exec",
                    "candidate": "echo ok",
                    "metadata": {},
                    "created_at": now,
                }
            ]
            self.agent_messages = [
                SimpleNamespace(
                    msg_id="msg-comms-1",
                    msg_type="request_review",
                    from_agent="founder",
                    to_agents=["reviewer"],
                    subject="Need review",
                    body="Please review the output.",
                    task_id="task-1",
                    context_ref="task-1",
                    status="sent",
                    timestamp=now,
                )
            ]
            self.handoff_records = [
                SimpleNamespace(
                    handoff_id="handoff-1",
                    project_id="demo",
                    session_id="sess-1",
                    task_id="task-1",
                    from_role="founder",
                    to_role="reviewer",
                    status="sent",
                    summary="Handoff summary",
                    created_at=now,
                )
            ]
            self.save_calls: list[object] = []
            self.delete_calls: list[tuple[str, str | None]] = []
            self.reorg_proposals = [
                SimpleNamespace(
                    proposal_id="reorg-1",
                    title="Reorg One",
                    status=SimpleNamespace(value="pending"),
                    risk_level=SimpleNamespace(value="low"),
                    scope=SimpleNamespace(value="org"),
                    initiated_by="owner",
                    summary="Adjust roles",
                    rationale="Better fit",
                    approval_notes="",
                    changeset=SimpleNamespace(role_changes=[], task_adjustments=[]),
                    impact_summary={"roles": 1},
                    metadata={"source": "test"},
                    updated_at=now,
                )
            ]

        async def list_sessions(self, project_id="default", parent_session_id=None, limit=50):
            return [item for item in self.sessions if item.project_id == project_id][:limit]

        async def get_session(self, session_id: str):
            return next((item for item in self.sessions if item.session_id == session_id), None)

        async def get_tasks(self, project_id=None, status=None, parent_id=None):
            tasks = [item for item in self.tasks if item.project_id == project_id]
            if status is not None:
                tasks = [item for item in tasks if item.status == status]
            return tasks

        async def get_task(self, task_id: str):
            return next((item for item in self.tasks if item.id == task_id), None)

        async def save_task(self, task):
            self.save_calls.append(SimpleNamespace(id=task.id, title=task.title, session_id=task.session_id))

        async def hard_delete_task(self, task_id: str, session_id: str | None = None):
            self.delete_calls.append((task_id, session_id))

        async def get_pending_checkpoints(self, project_id="default", session_id=None, checkpoint_types=None):
            checkpoints = [item for item in self.checkpoints if not session_id or item.session_id == session_id]
            return list(checkpoints)

        async def get_execution_checkpoints(self, project_id="default", session_id=None, checkpoint_types=None, statuses=None):
            checkpoints = [item for item in self.checkpoints if not session_id or item.session_id == session_id]
            if statuses:
                checkpoints = [item for item in checkpoints if getattr(item, "status", "") in statuses]
            return list(checkpoints)

        async def get_session_transcript(self, session_id: str):
            message = SimpleNamespace(
                message_id="msg-1",
                role="assistant",
                agent_id=None,
                summary_flag=False,
                metadata={
                    "attachment_refs": [{
                        "attachment_id": "att-2",
                        "filename": "image.png",
                        "mime_type": "image/png",
                        "size_bytes": 100,
                        "disk_path": "projects/demo/attachments/att-2/image.png",
                    }]
                },
                created_at=datetime(2026, 5, 3, 12, 1),
            )
            part = SimpleNamespace(
                part_type="text",
                payload={
                    "text": f"Transcript for {session_id}",
                    "attachment_refs": [{
                        "attachment_id": "att-3",
                        "filename": "data.json",
                        "mime_type": "application/json",
                        "size_bytes": 20,
                        "disk_path": "projects/demo/attachments/att-3/data.json",
                    }],
                },
            )
            return [{"message": message, "parts": [part]}]

        async def list_reorg_proposals(self, project_id: str, limit: int = 20):
            return self.reorg_proposals[:limit]

        async def list_runtime_sessions(self, *, project_id=None, status=None, task_id=None, session_id=None, limit=50):
            rows = [dict(item) for item in self.runtime_sessions]
            if project_id:
                rows = [item for item in rows if item.get("project_id") == project_id]
            if status:
                rows = [item for item in rows if item.get("status") == status]
            if task_id:
                rows = [item for item in rows if item.get("task_id") == task_id]
            if session_id:
                rows = [item for item in rows if item.get("session_id") == session_id]
            return rows[:limit]

        async def get_runtime_session(self, runtime_session_id: str):
            return next((dict(item) for item in self.runtime_sessions if item["runtime_session_id"] == runtime_session_id), None)

        async def list_external_sessions(self, *, project_id=None, status=None, task_id=None, opc_session_id=None, limit=50):
            rows = list(self.external_sessions)
            if project_id:
                rows = [item for item in rows if item.project_id == project_id]
            if status:
                rows = [item for item in rows if item.status == status]
            if task_id:
                rows = [item for item in rows if item.task_id == task_id]
            if opc_session_id:
                rows = [item for item in rows if item.opc_session_id == opc_session_id]
            return rows[:limit]

        async def list_runtime_events(self, runtime_session_id: str, limit: int = 100):
            return [dict(item) for item in self.runtime_events if item["runtime_session_id"] == runtime_session_id][-limit:]

        async def list_runtime_transcript_entries(self, runtime_session_id: str):
            return [dict(item) for item in self.runtime_transcript_entries if item["runtime_session_id"] == runtime_session_id]

        async def list_runtime_tool_calls(self, runtime_session_id: str):
            return [dict(item) for item in self.runtime_tool_calls if item["runtime_session_id"] == runtime_session_id]

        async def list_runtime_tool_results(self, runtime_session_id: str):
            return [dict(item) for item in self.runtime_tool_results if item["runtime_session_id"] == runtime_session_id]

        async def list_runtime_permission_grants(self, *, runtime_session_id=None, project_id=None, scopes=None, tool_name=None):
            rows = [dict(item) for item in self.runtime_permission_grants]
            if runtime_session_id:
                rows = [item for item in rows if item["runtime_session_id"] == runtime_session_id]
            return rows

        async def list_agent_messages_for_tasks(self, task_ids, limit=50):
            scope = set(task_ids)
            return [
                item for item in self.agent_messages
                if item.task_id in scope or item.context_ref in scope
            ][:limit]

        async def get_handoff_records(
            self,
            project_id: str,
            target_projection_id=None,
            target_work_item_id=None,
            task_id=None,
            status=None,
            limit=20,
        ):
            rows = [item for item in self.handoff_records if item.project_id == project_id]
            if task_id:
                rows = [item for item in rows if item.task_id == task_id]
            if status:
                rows = [item for item in rows if item.status == status]
            return rows[:limit]

    def _make_state(self, store=None):
        config = OPCConfig()
        config.org.organization_name = "Demo Org"
        config.org.company_name = "Demo Company"
        config.org.default_mode = "company"
        config.org.company_profile = "custom"
        config.org.roles = [
            RoleConfig(id="founder", name="Founder", responsibility="Runs the company.", reports_to="owner", role_type="coordinator"),
        ]
        config.org.talent_templates = []
        config.org.employees = [
            EmployeeConfig(employee_id="emp-1", name="Eve Employee", role_id="founder", template_id="tpl-1"),
        ]
        config.org.teams = [
            TeamConfig(
                team_id="team-1",
                name="Core Team",
                description="Main execution team.",
                seat_ids=["seat-1"],
                seats=[SeatConfig(seat_id="seat-1", name="Founder Seat", role_id="founder")],
            )
        ]
        config.org.installed_packages = [
            SimpleNamespace(package_id="pkg-1", name="Package One", version="1.0.0", role_ids=["founder"], template_ids=["tpl-1"]),
        ]
        calls: list[dict] = []
        memory_updates: list[tuple[str, str]] = []
        reorg_approvals: list[tuple[str, bool, str]] = []
        reorg_applies: list[str] = []

        async def process_message(content, **kwargs):
            calls.append({"content": content, **kwargs})
            return "ok"

        async def shutdown():
            return None

        async def update_session_title(session_id: str, title: str):
            memory_updates.append((session_id, title))

        async def show_company_reorg(proposal_id: str):
            proposals = getattr(store, "reorg_proposals", []) if store is not None else []
            return next((item for item in proposals if item.proposal_id == proposal_id), None)

        async def approve_company_reorg(proposal_id: str, *, approved: bool, notes: str = ""):
            reorg_approvals.append((proposal_id, approved, notes))
            return SimpleNamespace(proposal_id=proposal_id)

        async def apply_company_reorg(proposal_id: str):
            reorg_applies.append(proposal_id)
            return {"proposal_id": proposal_id, "applied": True}

        async def get_latest_pending_checkpoint_for_session(session_id: str):
            if store is None:
                return None
            checkpoints = await store.get_pending_checkpoints(project_id="demo", session_id=session_id)
            return checkpoints[0] if checkpoints else None

        engine = SimpleNamespace(
            config=config,
            project_id="demo",
            opc_home=Path(tempfile.gettempdir()),
            store=store,
            llm=SimpleNamespace(stats={"tokens_in": 10, "tokens_out": 5, "estimated_cost": 0.0123}),
            adapter_registry=SimpleNamespace(list_available=lambda: ["codex"]),
            process_message=process_message,
            shutdown=shutdown,
            calls=calls,
            memory=SimpleNamespace(update_session_title=update_session_title, updates=memory_updates),
            org_engine=SimpleNamespace(
                reload_from_config=lambda: None,
                configure_task_mode_tools=lambda tools: None,
            ),
            _task_mode_tool_names=lambda: ["shell"],
            show_company_reorg=show_company_reorg,
            approve_company_reorg=approve_company_reorg,
            apply_company_reorg=apply_company_reorg,
            get_latest_pending_checkpoint_for_session=get_latest_pending_checkpoint_for_session,
            reorg_approvals=reorg_approvals,
            reorg_applies=reorg_applies,
        )
        state = _InteractiveChatState(
            config=config,
            engine=engine,
            runtime_display=self._Display(),
            session_id="sess-1",
            no_markdown=True,
        )
        return state, engine

    def test_slash_commands_update_mode_agent_domains_and_unknown_help_hint(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        state, _engine = self._make_state()

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/mode")
            await _handle_chat_slash_command(state, "/agent")
            await _handle_chat_slash_command(state, "/project")
            await _handle_chat_slash_command(state, "/session")
            await _handle_chat_slash_command(state, "/mode company custom")
            await _handle_chat_slash_command(state, "/agent codex")
            await _handle_chat_slash_command(state, "/domains coding frontend")
            await _handle_chat_slash_command(state, "/does-not-exist")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(state.mode, "company")
        self.assertEqual(state.company_profile, "custom")
        self.assertEqual(state.preferred_agent, "codex")
        self.assertEqual(state.domains, ["coding", "frontend"])
        rendered = console.export_text()
        self.assertIn("/mode task", rendered)
        self.assertIn("/agent native", rendered)
        self.assertIn("/project list", rendered)
        self.assertIn("/project switch <id>", rendered)
        self.assertIn("/project rename <old_id> <new_id>", rendered)
        self.assertIn("/session list", rendered)
        self.assertIn("/session resume <session_id>", rendered)
        self.assertIn("Try /help", rendered)

    def test_mode_and_agent_slash_persist_for_next_chat_start(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        with tempfile.TemporaryDirectory() as tmpdir:
            state, engine = self._make_state()
            engine.opc_home = Path(tmpdir)

            async def _run() -> None:
                await _handle_chat_slash_command(state, "/mode company corporate")
                await _handle_chat_slash_command(state, "/agent codex")

                restored_state, restored_engine = self._make_state()
                restored_engine.opc_home = Path(tmpdir)
                restored_state.mode = "task"
                restored_state.company_profile = "custom"
                restored_state.preferred_agent = "native"
                await _restore_chat_context(
                    restored_state,
                    restore_mode=True,
                    restore_company_profile=True,
                    restore_agent=True,
                )
                self.assertEqual(restored_state.mode, "company")
                self.assertEqual(restored_state.company_profile, "corporate")
                self.assertEqual(restored_state.preferred_agent, "codex")

            with patch("opc.cli.app.console", console):
                asyncio.run(_run())

    def test_interactive_chat_defaults_to_native_agent_and_toolbar_hint(self) -> None:
        state, _engine = self._make_state()

        self.assertEqual(state.preferred_agent, "native")
        self.assertEqual(_normalize_interactive_preferred_agent(None), "native")
        self.assertIsNone(_normalize_interactive_preferred_agent("none"))

        toolbar = "".join(text for _style, text in _chat_bottom_toolbar_text(state))
        self.assertIn("project:demo", toolbar)
        self.assertIn("session:sess-1", toolbar)
        self.assertIn("mode:task", toolbar)
        self.assertIn("agent:native", toolbar)
        self.assertIn("/project", toolbar)

    def test_startup_selector_chooses_session_instead_of_auto_restoring(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state(store=self._Store())
        state.session_id = ""

        async def _run() -> None:
            with patch.object(console, "input", side_effect=["1"]):
                await _run_interactive_startup_selector(state, explicit_project=True)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(state.session_id, "sess-1")
        rendered = console.export_text()
        self.assertIn("Choose Session", rendered)
        self.assertIn("Ready:", rendered)

    def test_startup_selector_blank_session_starts_new_chat(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state(store=self._Store())
        state.session_id = ""

        async def _run() -> None:
            with patch.object(console, "input", side_effect=[""]):
                await _run_interactive_startup_selector(state, explicit_project=True)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertTrue(state.session_id)
        self.assertNotEqual(state.session_id, "sess-1")
        self.assertIn("Started a new session", console.export_text())

    def test_process_message_uses_interactive_state(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        state, engine = self._make_state()

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/mode company corporate")
            await _handle_chat_slash_command(state, "/agent codex")
            await _process_interactive_chat_message(state, "Build the thing")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["mode"], "company")
        self.assertEqual(engine.calls[-1]["company_profile"], "corporate")
        self.assertEqual(engine.calls[-1]["preferred_agent"], "codex")
        self.assertEqual(engine.calls[-1]["session_id"], "sess-1")
        self.assertEqual(engine.calls[-1]["message_metadata"], {"company_preflight": "manual"})

    def test_process_message_prints_final_response_while_hiding_external_detail(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, engine = self._make_state()

        async def process_message(content, **kwargs):
            callback = _progress_callback()
            await callback("[External:codex:thinking] hidden role thinking")
            await callback("[External:codex:tool] $ npm test")
            return "Final owner-facing answer"

        engine.process_message = process_message

        with patch("opc.cli.app.console", console), patch.dict(os.environ, {"OPC_CLI_VERBOSE_EXTERNAL": ""}, clear=False):
            asyncio.run(_process_interactive_chat_message(state, "Build the thing"))

        rendered = console.export_text()
        self.assertIn("Final owner-facing answer", rendered)
        self.assertNotIn("hidden role thinking", rendered)
        self.assertNotIn("$ npm test", rendered)

    def test_chat_turn_controller_queues_followup_and_runs_after_active_turn(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, engine = self._make_state()

        async def _run() -> list[str]:
            gate = asyncio.Event()
            calls: list[str] = []

            async def process_message(content, **kwargs):
                calls.append(content)
                if content == "first":
                    await gate.wait()
                return f"ok {content}"

            engine.process_message = process_message
            controller = ChatTurnController(state)
            await controller.submit_user_message("first")
            await asyncio.sleep(0)
            self.assertTrue(controller.is_busy)
            await controller.submit_user_message("second")
            self.assertEqual(controller.queue_depth(), 1)
            gate.set()
            for _ in range(10):
                await controller.wait_for_active()
                if not controller.is_busy and controller.queue_depth() == 0:
                    break
            await controller.shutdown()
            return calls

        with patch("opc.cli.app.console", console):
            calls = asyncio.run(_run())

        self.assertEqual(calls, ["first", "second"])
        self.assertIn("Queued #1", console.export_text())

    def test_chat_turn_controller_prepares_checkpoint_before_cancelling_active_turn(self) -> None:
        state, _engine = self._make_state()

        async def _run() -> list[str]:
            order: list[str] = []
            started = asyncio.Event()

            async def active_turn() -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append("cancelled")

            async def prepare() -> list[dict[str, Any]]:
                order.append("prepared")
                return []

            state.engine.prepare_active_company_runtimes_for_shutdown = prepare
            controller = ChatTurnController(state)
            controller.active_task = asyncio.create_task(active_turn())
            await started.wait()

            await controller.shutdown()
            return order

        self.assertEqual(asyncio.run(_run()), ["prepared", "cancelled"])

    def test_busy_slash_policy_allows_readonly_and_blocks_mutating_commands(self) -> None:
        self.assertEqual(_busy_slash_policy("kanban", []), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("logs", ["task-1"]), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("stop", []), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("continue", []), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("session", ["list"]), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("session", ["stop"]), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("session", ["continue"]), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("session", ["delete", "task-1"]), BusyCommandPolicy.BLOCKED_WHEN_BUSY)
        self.assertEqual(_busy_slash_policy("staffing", ["context"]), BusyCommandPolicy.IMMEDIATE_READONLY)
        self.assertEqual(_busy_slash_policy("staffing", []), BusyCommandPolicy.BLOCKED_WHEN_BUSY)
        self.assertEqual(_busy_slash_policy("mode", ["company"]), BusyCommandPolicy.BLOCKED_WHEN_BUSY)

    def test_stop_slash_defaults_to_current_session_task(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        state, _engine = self._make_state(store=self._Store())

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/stop")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Stopped", rendered)
        self.assertEqual(state.runtime_control_task_id, "task-1")
        self.assertEqual(state.runtime_control_session_id, "sess-1")
        self.assertIn(state.runtime_control_state, {"stopped", "cancelled"})

    def test_continue_slash_force_resumes_current_session(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        state, engine = self._make_state(store=self._Store())
        state.runtime_control_state = "suspended"

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/continue")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["content"], "Resume the existing runtime.")
        self.assertEqual(engine.calls[-1]["session_id"], "sess-1")
        self.assertEqual(engine.calls[-1]["message_metadata"], {"ui_force_resume": True})

    def test_continue_slash_uses_task_org_identity_not_global_mode(self) -> None:
        store = self._Store()
        store.tasks[0].metadata.update({
            "exec_mode": "org",
            "company_profile": "custom",
            "org_id": "quantum_harbor",
            "preferred_agent": "codex",
        })
        store.checkpoints = [
            SimpleNamespace(
                checkpoint_id="cp-org-interrupted",
                checkpoint_type="company_runtime_interrupted",
                status="pending",
                task_id="task-1",
                session_id="sess-1",
                updated_at=datetime(2026, 5, 17, 12, 0),
                payload={},
            )
        ]
        state, engine = self._make_state(store=store)
        state.mode = "company"
        state.company_profile = "corporate"
        state.org_id = ""
        state.preferred_agent = "native"

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/continue")

        asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["mode"], "org")
        self.assertEqual(engine.calls[-1]["org_id"], "quantum_harbor")
        self.assertIsNone(engine.calls[-1]["company_profile"])
        self.assertEqual(engine.calls[-1]["preferred_agent"], "codex")
        self.assertEqual(engine.calls[-1]["message_metadata"], {
            "ui_force_resume": True,
            "response_to_checkpoint_id": "cp-org-interrupted",
            "response_to_checkpoint_type": "company_runtime_interrupted",
        })

    def test_company_continue_requires_a_durable_runtime_checkpoint(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)
        store = self._Store()
        store.tasks[0].metadata.update({
            "exec_mode": "company",
            "company_profile": "corporate",
        })
        state, engine = self._make_state(store=store)

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/continue")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(engine.calls, [])
        self.assertIn("No suspended or interrupted company runtime", console.export_text())

    def test_continue_slash_routes_to_durable_runtime_checkpoint(self) -> None:
        store = self._Store()
        store.checkpoints = [
            SimpleNamespace(
                checkpoint_id="cp-interrupted",
                checkpoint_type="company_runtime_interrupted",
                status="pending",
                task_id="task-1",
                session_id="sess-1",
                updated_at=datetime(2026, 5, 17, 12, 0),
                payload={},
            )
        ]
        state, engine = self._make_state(store=store)
        state.mode = "company"
        state.runtime_control_state = "suspended"

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/continue")

        asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["session_id"], "sess-1")
        self.assertEqual(engine.calls[-1]["message_metadata"], {
            "ui_force_resume": True,
            "response_to_checkpoint_id": "cp-interrupted",
            "response_to_checkpoint_type": "company_runtime_interrupted",
        })
        self.assertEqual(state.runtime_control_checkpoint_id, "cp-interrupted")

    def test_plain_message_after_stop_routes_to_suspend_checkpoint(self) -> None:
        store = self._Store()
        store.checkpoints = [
            SimpleNamespace(
                checkpoint_id="cp-suspend",
                checkpoint_type="company_runtime_suspended",
                status="pending",
                task_id="task-1",
                session_id="sess-1",
                updated_at=datetime(2026, 5, 17, 12, 0),
                payload={},
            )
        ]
        state, engine = self._make_state(store=store)
        state.mode = "company"
        state.runtime_control_state = "suspended"

        async def _run() -> None:
            await _process_interactive_chat_message(state, "请按这个修改继续")

        asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["message_metadata"]["response_to_checkpoint_id"], "cp-suspend")
        self.assertEqual(engine.calls[-1]["message_metadata"]["response_to_checkpoint_type"], "company_runtime_suspended")
        self.assertEqual(state.runtime_control_state, "suspended")

    def test_plain_message_from_company_child_uses_root_runtime_checkpoint(self) -> None:
        store = self._Store()
        now = datetime(2026, 5, 17, 12, 0)
        store.tasks[0].metadata.update({
            "exec_mode": "company",
            "mode": "company",
            "company_profile": "corporate",
        })
        store.tasks[0].parent_session_id = ""
        store.tasks.append(SimpleNamespace(
            id="worker-child",
            title="Worker",
            description="Worker turn",
            status=TaskStatus.BLOCKED,
            priority=3,
            assigned_to="worker",
            session_id="sess-1:role:worker",
            parent_session_id="sess-1",
            project_id="demo",
            created_at=now,
            tags=[],
            result={},
            linked_work_item_id="worker-item",
            metadata={
                "exec_mode": "company",
                "work_item_runtime": True,
                "work_item_projection_id": "worker",
                "company_runtime_root_session_id": "sess-1",
            },
            context_snapshot={},
        ))
        store.checkpoints = [SimpleNamespace(
            checkpoint_id="cp-child-interrupted",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id="task-1",
            session_id="sess-1",
            project_id="demo",
            updated_at=now,
            payload={"parent_session_id": "sess-1"},
        )]
        state, engine = self._make_state(store=store)
        state.session_id = "sess-1:role:worker"
        # The selected child channel may have left the CLI's ambient mode in
        # task mode.  Runtime config, not ambient state, owns resumed execution.
        state.mode = "task"

        asyncio.run(_process_interactive_chat_message(state, "revise and continue"))

        call = engine.calls[-1]
        self.assertEqual(call["session_id"], "sess-1")
        self.assertEqual(call["origin_task_id"], "task-1")
        self.assertEqual(call["mode"], "company")
        self.assertEqual(call["company_profile"], "corporate")
        self.assertEqual(call["message_metadata"], {
            "response_to_checkpoint_id": "cp-child-interrupted",
            "response_to_checkpoint_type": "company_runtime_interrupted",
        })

    def test_plain_message_during_resuming_checkpoint_fails_closed(self) -> None:
        console = Console(record=True, force_terminal=False, width=140)
        store = self._Store()
        store.tasks[0].metadata.update({
            "exec_mode": "company",
            "mode": "company",
            "company_profile": "corporate",
        })
        store.checkpoints = [SimpleNamespace(
            checkpoint_id="cp-resuming",
            checkpoint_type="company_runtime_suspended",
            status="resuming",
            task_id="task-1",
            session_id="sess-1",
            project_id="demo",
            updated_at=datetime(2026, 5, 17, 12, 0),
            payload={"parent_session_id": "sess-1"},
        )]
        state, engine = self._make_state(store=store)
        state.mode = "company"

        with patch("opc.cli.app.console", console):
            asyncio.run(_process_interactive_chat_message(state, "continue again"))

        self.assertEqual(engine.calls, [])
        self.assertIn("checkpoint is resuming", console.export_text())

    def test_explicit_cli_runtime_checkpoint_mismatch_fails_closed(self) -> None:
        console = Console(record=True, force_terminal=False, width=140)
        store = self._Store()
        store.tasks[0].metadata.update({
            "exec_mode": "company",
            "mode": "company",
            "company_profile": "corporate",
        })
        store.checkpoints = [SimpleNamespace(
            checkpoint_id="cp-current",
            checkpoint_type="company_runtime_interrupted",
            status="pending",
            task_id="task-1",
            session_id="sess-1",
            project_id="demo",
            updated_at=datetime(2026, 5, 17, 12, 0),
            payload={"parent_session_id": "sess-1"},
        )]
        state, engine = self._make_state(store=store)
        state.mode = "company"

        with patch("opc.cli.app.console", console):
            asyncio.run(_process_interactive_chat_message(
                state,
                "continue stale",
                message_metadata={
                    "response_to_checkpoint_id": "cp-stale",
                    "response_to_checkpoint_type": "company_runtime_interrupted",
                },
            ))

        self.assertEqual(engine.calls, [])
        self.assertIn("checkpoint identity mismatch", console.export_text())

    def test_session_resume_still_switches_session(self) -> None:
        state, engine = self._make_state(store=self._Store())
        state.session_id = "other-session"

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/session resume sess-1")

        asyncio.run(_run())

        self.assertEqual(state.session_id, "sess-1")
        self.assertEqual(engine.calls, [])

    def test_session_service_company_stop_suspends_runtime(self) -> None:
        now = datetime(2026, 5, 17, 12, 0)

        class Store:
            is_ready = True

            def __init__(self) -> None:
                self.tasks = [
                    SimpleNamespace(
                        id="task-company",
                        project_id="demo",
                        session_id="sess-company",
                        parent_session_id="",
                        title="Company Task",
                        status=TaskStatus.RUNNING,
                        metadata={"exec_mode": "company", "company_profile": "corporate"},
                        execution_lock=True,
                        execution_locked_at=now,
                    )
                ]
                self.saved: list[Any] = []

            async def get_task(self, task_id: str):
                return next((item for item in self.tasks if item.id == task_id), None)

            async def get_tasks(self, project_id=None, status=None, parent_id=None):
                return [item for item in self.tasks if item.project_id == project_id]

            async def get_session(self, session_id: str):
                return SimpleNamespace(session_id=session_id, project_id="demo")

            async def save_task(self, task):
                self.saved.append(task)

        async def _run() -> dict[str, Any]:
            store = Store()
            suspend_calls: list[dict[str, Any]] = []

            async def suspend_company_runtime(**kwargs):
                suspend_calls.append(kwargs)
                return {
                    "checkpoint_id": "cp-stop",
                    "checkpoint_type": "company_runtime_suspended",
                    "task_ids": ["task-company"],
                }

            engine = SimpleNamespace(project_id="demo", store=store, suspend_company_runtime=suspend_company_runtime)
            context = OfficeServiceContext(engine=engine, agent_store=None, chat_store=None, event_adapter=None)
            cancelled: list[str] = []
            context.cancel_session_tasks = lambda task_id: cancelled.append(task_id)
            result = await SessionService(context).stop(project_id="demo", target="sess-company")
            return {
                "payload": result.payload,
                "task": store.tasks[0],
                "cancelled": cancelled,
                "suspend_calls": suspend_calls,
            }

        output = asyncio.run(_run())

        self.assertEqual(output["payload"]["runtime_control_state"], "suspended")
        self.assertEqual(output["payload"]["checkpoint_id"], "cp-stop")
        self.assertEqual(output["cancelled"], ["task-company"])
        self.assertEqual(output["suspend_calls"][0]["session_id"], "sess-company")

    def test_session_service_continue_uses_force_resume_metadata(self) -> None:
        class Store:
            is_ready = True

            def __init__(self) -> None:
                self.task = SimpleNamespace(
                    id="task-company",
                    project_id="demo",
                    session_id="sess-company",
                    parent_session_id="",
                    title="Company Task",
                    status=TaskStatus.BLOCKED,
                    metadata={
                        "exec_mode": "company",
                        "company_profile": "corporate",
                    },
                )
                self.checkpoint = SimpleNamespace(
                    checkpoint_id="cp-company",
                    checkpoint_type="company_runtime_suspended",
                    status="pending",
                    project_id="demo",
                    session_id="sess-company",
                    task_id="task-company",
                    payload={},
                )

            async def get_task(self, task_id: str):
                return self.task if task_id == self.task.id else None

            async def get_tasks(self, project_id=None, status=None, parent_id=None):
                return [self.task] if project_id == "demo" else []

            async def get_session(self, session_id: str):
                return SimpleNamespace(session_id=session_id, project_id="demo")

            async def get_execution_checkpoints(self, **_kwargs):
                return [self.checkpoint]

            async def save_task(self, task):
                self.task = task

        async def _run() -> list[dict[str, Any]]:
            calls: list[dict[str, Any]] = []

            async def process_message(content, **kwargs):
                calls.append({"content": content, **kwargs})
                return "resumed"

            engine = SimpleNamespace(project_id="demo", store=Store(), process_message=process_message)
            context = OfficeServiceContext(engine=engine, agent_store=None, chat_store=None, event_adapter=None)
            result = await SessionService(context).continue_run(
                project_id="demo",
                target="sess-company",
                runtime_session_id="sess-company",
                checkpoint_id="cp-company",
            )
            self.assertEqual(result.payload["response"], "resumed")
            return calls

        calls = asyncio.run(_run())

        self.assertEqual(calls[-1]["content"], "Resume the existing runtime.")
        self.assertEqual(calls[-1]["session_id"], "sess-company")
        self.assertEqual(calls[-1]["mode"], "company")
        self.assertEqual(calls[-1]["message_metadata"], {
            "ui_force_resume": True,
            "response_to_checkpoint_id": "cp-company",
            "response_to_checkpoint_type": "company_runtime_suspended",
        })

    def test_session_service_continue_preserves_custom_org_id(self) -> None:
        class Store:
            is_ready = True

            def __init__(self) -> None:
                self.task = SimpleNamespace(
                    id="task-org",
                    project_id="demo",
                    session_id="sess-org",
                    parent_session_id="",
                    title="Org Task",
                    status=TaskStatus.BLOCKED,
                    metadata={
                        "exec_mode": "org",
                        "company_profile": "custom",
                        "org_id": "quantum_harbor",
                        "preferred_agent": "codex",
                    },
                )
                self.checkpoint = SimpleNamespace(
                    checkpoint_id="cp-org",
                    checkpoint_type="company_runtime_interrupted",
                    status="pending",
                    project_id="demo",
                    session_id="sess-org",
                    task_id="task-org",
                    payload={},
                )

            async def get_task(self, task_id: str):
                return self.task if task_id == self.task.id else None

            async def get_tasks(self, project_id=None, status=None, parent_id=None):
                return [self.task] if project_id == "demo" else []

            async def get_session(self, session_id: str):
                return SimpleNamespace(session_id=session_id, project_id="demo")

            async def get_execution_checkpoints(self, **_kwargs):
                return [self.checkpoint]

            async def save_task(self, task):
                self.task = task

        async def _run() -> list[dict[str, Any]]:
            calls: list[dict[str, Any]] = []

            async def process_message(content, **kwargs):
                calls.append({"content": content, **kwargs})
                return "resumed"

            engine = SimpleNamespace(project_id="demo", store=Store(), process_message=process_message)
            context = OfficeServiceContext(engine=engine, agent_store=None, chat_store=None, event_adapter=None)
            result = await SessionService(context).continue_run(
                project_id="demo",
                target="sess-org",
                runtime_session_id="sess-org",
                checkpoint_id="cp-org",
            )
            self.assertEqual(result.payload["response"], "resumed")
            return calls

        calls = asyncio.run(_run())

        self.assertEqual(calls[-1]["mode"], "org")
        self.assertEqual(calls[-1]["org_id"], "quantum_harbor")
        self.assertIsNone(calls[-1]["company_profile"])
        self.assertEqual(calls[-1]["message_metadata"], {
            "ui_force_resume": True,
            "response_to_checkpoint_id": "cp-org",
            "response_to_checkpoint_type": "company_runtime_interrupted",
        })

    def test_queue_slash_lists_and_drops_queued_prompts(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state()
        controller = ChatTurnController(state)
        controller.queue.append(
            QueuedChatInput(
                text="queued work",
                project_id="demo",
                session_id="sess-1",
                mode="task",
                company_profile="corporate",
                preferred_agent="native",
                domains=[],
            )
        )

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/queue list", controller=controller)
            await _handle_chat_slash_command(state, "/queue drop 1", controller=controller)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Queued Prompts", rendered)
        self.assertIn("queued work", rendered)
        self.assertEqual(controller.queue_depth(), 0)

    def test_kanban_slash_renders_inline_snapshot_without_board(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state()
        controller = ChatTurnController(state)
        items = [
            {
                "work_item_id": "wi-1234567890",
                "title": "Build puzzle level",
                "role_id": "cto",
                "kanban_column": "todo",
                "phase": "ready",
                "runtime_task_id": "task-123456",
                "updated_at": "2026-05-17T17:00:00",
            }
        ]

        async def fake_fetch(_state, *, limit=100, session_id=None, project_scope=False):
            self.assertEqual(session_id, state.session_id)
            self.assertFalse(project_scope)
            return items

        async def _run() -> None:
            with patch("opc.cli.app._fetch_kanban_items", fake_fetch), patch("asyncio.create_subprocess_exec") as mock_subprocess:
                await _handle_kanban_slash(state, ["once"], controller=controller)
                mock_subprocess.assert_not_called()

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Kanban Work Items", rendered)
        self.assertIn("Build puzzle level", rendered)
        self.assertIn("cto", rendered)
        self.assertFalse(controller.kanban_watch_active())

    def test_kanban_slash_starts_and_stops_live_watch(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state()
        controller = ChatTurnController(state)

        async def fake_fetch(_state, *, limit=100, session_id=None, project_scope=False):
            self.assertEqual(session_id, state.session_id)
            self.assertFalse(project_scope)
            return []

        async def _run() -> None:
            with patch("opc.cli.app._fetch_kanban_items", fake_fetch):
                await _handle_kanban_slash(state, [], controller=controller)
                self.assertTrue(controller.kanban_watch_active())
                await _handle_kanban_slash(state, ["stop"], controller=controller)
                self.assertFalse(controller.kanban_watch_active())

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Kanban live watch started", rendered)
        self.assertIn("Kanban live watch stopped", rendered)

    def test_kanban_slash_can_request_project_scope_explicitly(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state()
        state.session_id = "sess-current"
        controller = ChatTurnController(state)
        calls: list[dict[str, object]] = []

        async def fake_fetch(_state, *, limit=100, session_id=None, project_scope=False):
            calls.append({"session_id": session_id, "project_scope": project_scope})
            return []

        async def _run() -> None:
            with patch("opc.cli.app._fetch_kanban_items", fake_fetch):
                await _handle_kanban_slash(state, ["all", "once"], controller=controller)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(calls, [{"session_id": "sess-current", "project_scope": True}])
        self.assertFalse(controller.kanban_watch_active())

    def test_work_item_service_session_scope_matches_ui_session_board_rules(self) -> None:
        run = SimpleNamespace(session_id="sess-root", metadata={}, recovery_pointer={})
        child_task = SimpleNamespace(
            session_id="sess-root:child-work-item",
            parent_session_id="sess-root",
            metadata={"company_runtime_root_session_id": "sess-root"},
        )
        other_run = SimpleNamespace(session_id="other-session", metadata={}, recovery_pointer={})

        self.assertTrue(WorkItemService._matches_session_scope("sess-root", run=run, linked_task=child_task))
        self.assertTrue(WorkItemService._matches_session_scope("sess-root:child-work-item", run=run, linked_task=child_task))
        self.assertFalse(WorkItemService._matches_session_scope("sess-root", run=other_run))

    def test_work_item_service_list_filters_to_session_visible_kanban_items(self) -> None:
        class Store:
            is_ready = True

            async def list_open_delegation_runs(self, *, project_id):
                return [
                    SimpleNamespace(run_id="run-1", project_id=project_id, session_id="sess-1", metadata={}, recovery_pointer={}),
                    SimpleNamespace(run_id="run-2", project_id=project_id, session_id="sess-2", metadata={}, recovery_pointer={}),
                ]

            async def get_tasks(self, *, project_id):
                return []

            async def list_delegation_work_items(self, run_id):
                rows = {
                    "run-1": [
                        SimpleNamespace(
                            work_item_id="root-1",
                            run_id="run-1",
                            parent_work_item_id="",
                            title="Root intake",
                            role_id="ceo",
                            phase="running",
                            metadata={},
                            created_at="",
                            updated_at="",
                        ),
                        SimpleNamespace(
                            work_item_id="child-1",
                            run_id="run-1",
                            parent_work_item_id="root-1",
                            title="Build game",
                            role_id="cto",
                            phase="ready",
                            metadata={},
                            created_at="",
                            updated_at="",
                        ),
                    ],
                    "run-2": [
                        SimpleNamespace(
                            work_item_id="child-2",
                            run_id="run-2",
                            parent_work_item_id="root-2",
                            title="Other session item",
                            role_id="cto",
                            phase="ready",
                            metadata={},
                            created_at="",
                            updated_at="",
                        )
                    ],
                }
                return rows.get(run_id, [])

        async def _run() -> list[str]:
            engine = SimpleNamespace(project_id="proj1", store=Store())
            context = OfficeServiceContext(engine=engine, agent_store=None, chat_store=None, event_adapter=None)
            result = await WorkItemService(context).list(
                project_id="proj1",
                session_id="sess-1",
                kanban_visible_only=True,
            )
            return [item["work_item_id"] for item in result.payload["work_items"]]

        self.assertEqual(asyncio.run(_run()), ["child-1"])

    def test_work_items_logs_role_renders_runtime_transcript_and_tools(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        now = "2026-05-17T20:00:00"

        class Store:
            is_ready = True

            async def list_open_delegation_runs(self, *, project_id):
                return [SimpleNamespace(run_id="run-1", project_id=project_id, session_id="sess-1", metadata={}, recovery_pointer={})]

            async def get_tasks(self, *, project_id=None, status=None, parent_id=None):
                return [
                    SimpleNamespace(
                        id="task-ceo",
                        project_id=project_id,
                        session_id="sess-1",
                        parent_session_id="",
                        linked_work_item_id="wi-ceo",
                        metadata={"delegation_role_session_id": "role-ceo"},
                        context_snapshot={},
                    )
                ]

            async def list_delegation_work_items(self, run_id):
                return [
                    SimpleNamespace(
                        work_item_id="wi-ceo",
                        run_id=run_id,
                        parent_work_item_id="",
                        title="CEO Intake",
                        role_id="ceo",
                        phase="running",
                        role_runtime_session_id="role-ceo",
                        claimed_by_role_runtime_session_id="",
                        metadata={},
                        created_at=now,
                        updated_at=now,
                    )
                ]

            async def list_delegation_events(self, run_id):
                return [
                    SimpleNamespace(
                        event_id="evt-1",
                        run_id=run_id,
                        work_item_id="wi-ceo",
                        cell_id="team::ceo",
                        role_id="ceo",
                        event_type="work_item_started",
                        payload={"summary": "CEO started"},
                        created_at=now,
                    )
                ]

            async def get_session_transcript(self, session_id):
                if session_id != "sess-1":
                    return []
                return [
                    {
                        "message": SimpleNamespace(
                            role="assistant",
                            agent_id="",
                            created_at=now,
                            summary_flag=False,
                        ),
                        "parts": [{"part_type": "text", "payload": {"text": "Company mode has a pending manual staffing selection before execution."}}],
                    },
                    {
                        "message": SimpleNamespace(
                            role="user",
                            agent_id="",
                            created_at=now,
                            summary_flag=False,
                        ),
                        "parts": [{"part_type": "text", "payload": {"text": "approve"}}],
                    },
                    {
                        "message": SimpleNamespace(
                            role="user",
                            agent_id="",
                            created_at=now,
                            summary_flag=False,
                        ),
                        "parts": [{"part_type": "text", "payload": {"text": "Build the product"}}],
                    },
                    {
                        "message": SimpleNamespace(
                            role="assistant",
                            agent_id="ceo",
                            created_at=now,
                            summary_flag=False,
                        ),
                        "parts": [{"part_type": "text", "payload": {"text": "CEO final output"}}],
                    },
                ]

            async def list_role_runtime_sessions(self, run_id, *, team_id=None, seat_id=None, role_id=None, status=None):
                return [
                    SimpleNamespace(
                        role_session_id="role-ceo",
                        run_id=run_id,
                        project_id="demo",
                        role_id="ceo",
                        focused_work_item_id="wi-ceo",
                        background_work_item_ids=[],
                        pending_work_item_ids=[],
                        status="running",
                        created_at=now,
                        updated_at=now,
                    )
                ]

            async def list_runtime_sessions(self, *, project_id=None, status=None, task_id=None, session_id=None, limit=50):
                return []

            async def list_external_sessions(self, *, project_id=None, status=None, task_id=None, opc_session_id=None, limit=50):
                if opc_session_id and opc_session_id != "role-ceo":
                    return []
                return [
                    SimpleNamespace(
                        agent_type="codex",
                        project_id=project_id or "demo",
                        session_id="provider-ceo",
                        opc_session_id="role-ceo",
                        task_id="task-ceo",
                        workspace_path="/tmp/work",
                        status="running",
                        metadata={"runtime_session_id": "codex:demo:task-ceo", "delegation_role_session_id": "role-ceo"},
                        updated_at=now,
                    )
                ]

            async def list_runtime_events(self, runtime_session_id: str, limit: int = 100):
                return [
                    {
                        "event_id": f"event-{runtime_session_id}",
                        "runtime_session_id": runtime_session_id,
                        "event_type": "status_snapshot",
                        "payload": {"agent": "ceo"},
                        "created_at": now,
                    }
                ]

            async def list_runtime_transcript_entries(self, runtime_session_id: str):
                if runtime_session_id != "role-ceo":
                    return []
                return [
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({"type": "thread.started", "thread_id": "thread_1"}),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "I will inspect the board first."},
                        }),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": "[External:codex:thinking] thinking through delegation",
                        "metadata": {},
                        "created_at": now,
                    }
                ]

            async def list_runtime_tool_calls(self, runtime_session_id: str):
                if runtime_session_id != "role-ceo":
                    return []
                return [
                    {
                        "runtime_session_id": runtime_session_id,
                        "tool_name": "shell_exec",
                        "arguments": {"command": "pwd"},
                        "metadata": {},
                        "created_at": now,
                    }
                ]

            async def list_runtime_tool_results(self, runtime_session_id: str):
                if runtime_session_id != "role-ceo":
                    return []
                return [
                    {
                        "runtime_session_id": runtime_session_id,
                        "tool_name": "shell_exec",
                        "payload": {"stdout": "/tmp/work", "exit_code": 0},
                        "metadata": {},
                        "created_at": now,
                    }
                ]

            async def list_runtime_permission_grants(self, *, runtime_session_id=None, project_id=None, scopes=None, tool_name=None):
                return []

        state, _engine = self._make_state(store=Store())

        async def _run() -> None:
            await _handle_work_items_slash(state, ["logs", "--role", "ceo", "--limit", "20"])

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Work Item Logs", rendered)
        self.assertIn("› Build the product", rendered)
        self.assertNotIn("pending manual staffing", rendered)
        self.assertNotIn("› approve", rendered)
        self.assertIn("• assistant", rendered)
        self.assertIn("CEO final output", rendered)
        self.assertIn("thinking codex", rendered)
        self.assertIn("I will inspect the board first.", rendered)
        self.assertIn("thinking through delegation", rendered)
        self.assertIn("$ pwd", rendered)
        self.assertEqual(rendered.count("$ pwd"), 1)
        self.assertIn("tool result shell_exec (status=completed, exit_code=0)", rendered)
        self.assertIn("/tmp/work", rendered)
        self.assertIn("shell_exec", rendered)
        self.assertNotIn("thread.started", rendered)
        self.assertNotIn("turn.completed", rendered)
        self.assertNotIn("CEO started", rendered)
        self.assertNotIn("status_snapshot", rendered)
        self.assertNotIn("┃", rendered)
        self.assertNotIn("Created", rendered)
        self.assertNotIn("Initializing OPC Engine", rendered)

    def test_work_items_logs_parses_codex_stream_without_raw_json(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        now = "2026-05-17T20:00:00"
        large_json = "{" + '"parent_item": "' + ("x" * 900) + '"}'

        class Store:
            is_ready = True

            async def list_open_delegation_runs(self, *, project_id):
                return [SimpleNamespace(run_id="run-1", project_id=project_id, session_id="sess-1", metadata={}, recovery_pointer={})]

            async def get_tasks(self, *, project_id=None, status=None, parent_id=None):
                return []

            async def list_delegation_work_items(self, run_id):
                return [
                    SimpleNamespace(
                        work_item_id="wi-ceo",
                        run_id=run_id,
                        parent_work_item_id="",
                        title="CEO Intake",
                        role_id="ceo",
                        phase="running",
                        role_runtime_session_id="codex:demo:task-ceo",
                        claimed_by_role_runtime_session_id="",
                        metadata={},
                        created_at=now,
                        updated_at=now,
                    )
                ]

            async def list_delegation_events(self, run_id):
                return []

            async def list_role_runtime_sessions(self, run_id, *, team_id=None, seat_id=None, role_id=None, status=None):
                return []

            async def list_runtime_transcript_entries(self, runtime_session_id: str):
                return [
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({"type": "thread.started", "thread_id": "thread_1"}),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Planning child work."},
                        }),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({
                            "type": "item.started",
                            "item": {
                                "type": "command_execution",
                                "command": '/bin/bash -lc "opc-collab manager_board_read --args-json \'{\"include_children\":true}\'"',
                                "status": "in_progress",
                            },
                        }),
                        "metadata": {},
                        "created_at": now,
                    },
                    {
                        "runtime_session_id": runtime_session_id,
                        "role": "assistant",
                        "entry_type": "stream",
                        "content": json.dumps({
                            "type": "item.completed",
                            "item": {
                                "type": "command_execution",
                                "command": '/bin/bash -lc "opc-collab manager_board_read"',
                                "aggregated_output": large_json,
                                "exit_code": 0,
                                "status": "completed",
                            },
                        }),
                        "metadata": {},
                        "created_at": now,
                    },
                ]

        state, _engine = self._make_state(store=Store())

        async def _run() -> None:
            await _handle_work_items_slash(state, ["logs", "--role", "ceo", "--limit", "20"])

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Planning child work.", rendered)
        self.assertIn("$ /bin/bash -lc", rendered)
        self.assertIn("tool result command_execution (status=completed, exit_code=0)", rendered)
        self.assertIn("output omitted; use --full", rendered)
        self.assertNotIn("thread.started", rendered)
        self.assertNotIn("parent_item", rendered)

    def test_work_items_logs_full_includes_internal_events(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        now = "2026-05-17T20:00:00"

        class Store:
            is_ready = True

            async def list_open_delegation_runs(self, *, project_id):
                return [SimpleNamespace(run_id="run-1", project_id=project_id, session_id="sess-1", metadata={}, recovery_pointer={})]

            async def get_tasks(self, *, project_id=None, status=None, parent_id=None):
                return []

            async def list_delegation_work_items(self, run_id):
                return [
                    SimpleNamespace(
                        work_item_id="wi-ceo",
                        run_id=run_id,
                        parent_work_item_id="",
                        title="CEO Intake",
                        role_id="ceo",
                        phase="running",
                        role_runtime_session_id="role-ceo",
                        claimed_by_role_runtime_session_id="",
                        metadata={},
                        created_at=now,
                        updated_at=now,
                    )
                ]

            async def list_delegation_events(self, run_id):
                return [
                    SimpleNamespace(
                        event_id="evt-1",
                        run_id=run_id,
                        work_item_id="wi-ceo",
                        cell_id="team::ceo",
                        role_id="ceo",
                        event_type="work_item_started",
                        payload={"summary": "CEO started"},
                        created_at=now,
                    )
                ]

            async def list_role_runtime_sessions(self, run_id, *, team_id=None, seat_id=None, role_id=None, status=None):
                return []

        state, _engine = self._make_state(store=Store())

        async def _run() -> None:
            await _handle_work_items_slash(state, ["logs", "--role", "ceo", "--limit", "20", "--full"])

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("event work_item_started", rendered)
        self.assertIn("CEO started", rendered)

    def test_background_company_turn_renders_preflight_without_reading_stdin(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, engine = self._make_state(store=self._Store())
        state.mode = "company"

        async def _run() -> None:
            company_checkpoint = SimpleNamespace(
                checkpoint_id="staff-1",
                checkpoint_type="company_staffing_selection",
                session_id="sess-1",
                payload={
                    "staffing_roles": [
                        {
                            "role_id": "cto",
                            "role_label": "CTO",
                            "default_selection": {"kind": "fallback"},
                            "selected_agent": "codex",
                            "fallback_available": True,
                        }
                    ],
                    "staffing_pool": {"employees": [], "templates": []},
                    "original_message": "Build a game",
                },
            )

            async def get_latest_pending_checkpoint_for_session(session_id: str):
                return company_checkpoint

            engine.get_latest_pending_checkpoint_for_session = get_latest_pending_checkpoint_for_session
            with patch.object(console, "input", side_effect=AssertionError("should not read stdin")):
                await _process_interactive_chat_message(state, "Build a game", interactive_followups=False)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Company Staffing Preflight", rendered)
        self.assertIn("Type a/e/c/r/d at opc>", rendered)

    def test_staffing_approval_uses_controller_background_turn(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, engine = self._make_state(store=self._Store())
        state.mode = "company"
        checkpoint = SimpleNamespace(
            checkpoint_id="staff-1",
            checkpoint_type="company_staffing_selection",
            session_id="sess-1",
            payload={
                "staffing_roles": [
                    {
                        "role_id": "cto",
                        "role_label": "CTO",
                        "default_selection": {"kind": "fallback"},
                        "selected_agent": "codex",
                    }
                ],
                "staffing_pool": {"employees": [], "templates": []},
            },
        )

        async def _run() -> None:
            async def get_latest_pending_checkpoint_for_session(session_id: str):
                return checkpoint

            engine.get_latest_pending_checkpoint_for_session = get_latest_pending_checkpoint_for_session
            controller = ChatTurnController(state)
            with patch.object(console, "input", return_value="a"):
                await _handle_chat_slash_command(state, "/staffing", controller=controller)
            await controller.wait_for_active()
            self.assertFalse(controller.is_busy)

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["content"], "approve")
        self.assertEqual(engine.calls[-1]["message_metadata"]["staffing_action"], "manual_approve")

    def test_bare_staffing_shortcuts_edit_and_approve_pending_checkpoint(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, engine = self._make_state(store=self._Store())
        state.mode = "company"
        checkpoint = SimpleNamespace(
            checkpoint_id="staff-1",
            checkpoint_type="company_staffing_selection",
            session_id="sess-1",
            payload={
                "staffing_roles": [
                    {
                        "role_id": "cto",
                        "role_label": "CTO",
                        "default_selection": {"kind": "fallback"},
                        "selected_agent": "codex",
                    }
                ],
                "staffing_pool": {"employees": [], "templates": []},
            },
        )

        async def _run() -> None:
            async def get_latest_pending_checkpoint_for_session(session_id: str):
                return checkpoint

            async def fake_editor(_state, _checkpoint, draft):
                updated = dict(draft)
                updated["recruitment_role_agents"] = {"cto": "opencode"}
                return updated

            engine.get_latest_pending_checkpoint_for_session = get_latest_pending_checkpoint_for_session
            controller = ChatTurnController(state)
            with patch("opc.cli.app._edit_company_staffing_draft", fake_editor):
                self.assertTrue(await _handle_company_staffing_shortcut(state, "e", controller))
            self.assertEqual(state.company_staffing_drafts["staff-1"]["recruitment_role_agents"]["cto"], "opencode")
            self.assertTrue(await _handle_company_staffing_shortcut(state, "a", controller))
            await controller.wait_for_active()

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(engine.calls[-1]["content"], "approve")
        self.assertEqual(engine.calls[-1]["message_metadata"]["recruitment_role_agents"]["cto"], "opencode")

    def test_company_staffing_draft_metadata_and_search_helpers(self) -> None:
        payload = {
            "staffing_roles": [
                {
                    "role_id": "senior_engineer",
                    "role_label": "Senior Engineer",
                    "default_selection": {"kind": "fallback"},
                    "same_role_employee_ids": ["emp-1"],
                    "selected_agent": "codex",
                }
            ],
            "staffing_pool": {
                "employees": [
                    {
                        "employee_id": "emp-1",
                        "employee_name": "Eve Engineer",
                        "role_id": "senior_engineer",
                        "category": "engineering",
                        "domains": ["frontend"],
                        "tags": ["game"],
                        "description": "Builds browser games.",
                    }
                ],
                "templates": [
                    {
                        "template_id": "tpl-game",
                        "template_name": "Game Developer",
                        "category": "engineering",
                        "domains": ["game"],
                        "tags": ["canvas"],
                        "description": "Implements web games.",
                    }
                ],
            },
        }
        checkpoint = SimpleNamespace(checkpoint_id="cp-1")

        draft = _company_staffing_default_draft(payload)
        draft["staffing_selections"]["senior_engineer"] = {"kind": "template", "id": "tpl-game"}
        metadata = _company_staffing_resume_metadata(draft, checkpoint)

        self.assertEqual(metadata["staffing_action"], "manual_approve")
        self.assertEqual(metadata["response_to_checkpoint_id"], "cp-1")
        self.assertEqual(metadata["response_to_checkpoint_type"], "company_staffing_selection")
        self.assertEqual(metadata["staffing_selections"]["senior_engineer"], {"kind": "template", "id": "tpl-game"})
        self.assertEqual(metadata["recruitment_role_agents"]["senior_engineer"], "codex")
        matches = _company_staffing_filter_options(
            [
                {"label": "template: Game Developer", "search": "engineering canvas"},
                {"label": "fallback: role-only execution", "search": "fallback"},
            ],
            "game canvas",
        )
        self.assertEqual([item["label"] for item in matches], ["template: Game Developer"])

        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state()
        checkpoint.payload = {
            **payload,
            "original_message": "Build a dancing line web game with three levels.",
        }
        with patch("opc.cli.app.console", console):
            _render_company_staffing_context_preview(state, checkpoint, draft)
        rendered = console.export_text()
        self.assertIn("Initial Role Context Preview", rendered)
        self.assertIn("Owner request: Build a dancing line web game", rendered)
        self.assertIn("Runtime note:", rendered)

    def test_chat_one_shot_accepts_mode_agent_and_company_profile_options(self) -> None:
        class _Display:
            has_streamed_content = False

            def begin_turn(self) -> None:
                return None

            async def flush(self) -> None:
                return None

        class _Engine:
            project_id = "demo"

            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def initialize(self) -> None:
                return None

            async def process_message(self, content: str, **kwargs):
                self.calls.append({"content": content, **kwargs})
                return "ok"

            async def shutdown(self) -> None:
                return None

        engine = _Engine()
        runner = CliRunner()
        with patch("opc.cli.app._get_config", return_value=OPCConfig()), patch(
            "opc.cli.app._create_cli_engine",
            return_value=(engine, _Display()),
        ):
            result = runner.invoke(
                app,
                [
                    "chat",
                    "Say hi",
                    "--project",
                    "demo",
                    "--mode",
                    "company",
                    "--agent",
                    "claude_code",
                    "--company-profile",
                    "corporate",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(engine.calls[-1]["mode"], "company")
        self.assertEqual(engine.calls[-1]["preferred_agent"], "claude_code")
        self.assertEqual(engine.calls[-1]["company_profile"], "corporate")
        self.assertEqual(engine.calls[-1]["project_id"], "demo")

    def test_slash_rich_rendering_for_help_status_sessions_tasks_and_checkpoints(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state(store=self._Store())

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/help")
            await _handle_chat_slash_command(state, "/status")
            await _handle_chat_slash_command(state, "/session list")
            await _handle_chat_slash_command(state, "/tasks pending")
            await _handle_chat_slash_command(state, "/checkpoints")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Context", rendered)
        self.assertIn("Current Context", rendered)
        self.assertIn("Demo Session", rendered)
        self.assertIn("Pending Task", rendered)
        self.assertNotIn("Done Task", rendered)
        self.assertIn("cp-1", rendered)

    def test_task_move_and_done_use_shared_transition(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state(store=self._Store())
        transition_calls: list[dict] = []

        async def fake_transition(store, task, *, target_status_or_phase, reason, **kwargs):
            transition_calls.append({
                "task_id": task.id,
                "target": target_status_or_phase,
                "reason": reason,
            })
            task.status = target_status_or_phase
            return True

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/task move task-1 done")
            state.engine.store.tasks[0].status = TaskStatus.PENDING
            await _handle_chat_slash_command(state, "/task done task-1")

        with patch("opc.cli.app.console", console), patch(
            "opc.layer2_organization.work_item_transition.apply_task_status_transition",
            side_effect=fake_transition,
        ):
            asyncio.run(_run())

        self.assertEqual(
            [(call["task_id"], call["target"], call["reason"]) for call in transition_calls],
            [
                ("task-1", TaskStatus.DONE, "cli_chat_task_move"),
                ("task-1", TaskStatus.DONE, "cli_chat_task_done"),
            ],
        )

    def test_task_move_rejects_terminal_to_non_terminal(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        state, _engine = self._make_state(store=self._Store())
        transition_calls: list[dict] = []

        async def fake_transition(store, task, *, target_status_or_phase, reason, **kwargs):
            transition_calls.append({"task_id": task.id, "target": target_status_or_phase})
            return True

        with patch("opc.cli.app.console", console), patch(
            "opc.layer2_organization.work_item_transition.apply_task_status_transition",
            side_effect=fake_transition,
        ):
            asyncio.run(_handle_chat_slash_command(state, "/task move task-2 todo"))

        self.assertEqual(transition_calls, [])
        self.assertIn("Cannot move terminal task", console.export_text())

    def test_task_and_session_rename_save_task_and_update_session_title(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        store = self._Store()
        state, engine = self._make_state(store=store)

        async def _run() -> None:
            await _handle_chat_slash_command(state, '/task rename task-1 "New Title"')
            await _handle_chat_slash_command(state, '/session rename task-1 "New Session Title"')
            await _handle_chat_slash_command(state, '/session rename sess-1 "Session ID Title"')

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual([task.title for task in store.save_calls], ["New Title", "New Session Title", "Session ID Title"])
        self.assertEqual(engine.memory.updates, [("sess-1", "New Title"), ("sess-1", "New Session Title"), ("sess-1", "Session ID Title")])
        self.assertIn("Renamed task task-1", console.export_text())

    def test_task_and_session_delete_require_yes_and_hard_delete(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        store = self._Store()
        state, _engine = self._make_state(store=store)

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/task delete task-1")
            await _handle_chat_slash_command(state, "/task delete task-1 --yes")
            await _handle_chat_slash_command(state, "/session delete task-1 --yes")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        self.assertEqual(store.delete_calls, [("task-1", "sess-1"), ("task-1", "sess-1")])
        rendered = console.export_text()
        self.assertIn("requires --yes", rendered)
        self.assertIn("Deleted task task-1", rendered)

    def test_task_write_operations_reject_cross_project_task(self) -> None:
        console = Console(record=True, force_terminal=False, width=160)
        store = self._Store()
        state, _engine = self._make_state(store=store)

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, '/task rename task-3 "Nope"'))

        self.assertEqual(store.save_calls, [])
        self.assertIn("Task belongs to project 'other'", console.export_text())

    def test_org_slash_renders_org_roles_employees_and_teams(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, "/org"))

        rendered = console.export_text()
        self.assertIn("Demo Org", rendered)
        self.assertIn("Founder", rendered)
        self.assertIn("Eve Employee", rendered)
        self.assertIn("Core Team", rendered)
        self.assertIn("Founder Seat", rendered)

    def test_talent_slash_lists_imports_repo_and_hires(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        class _FakeTalentMarket:
            templates = [
                SimpleNamespace(
                    id="tpl-1",
                    name="Template One",
                    category="engineering",
                    domains=["coding"],
                    preferred_external_agent="codex",
                    description="Builds things.",
                )
            ]
            employees = [
                SimpleNamespace(
                    employee_id="emp-1",
                    name="Eve Employee",
                    role_id="founder",
                    template_id="tpl-1",
                    status="active",
                    preferred_external_agent="codex",
                )
            ]
            imported_local: list[list[str]] = []
            imported_repos: list[Path] = []
            hires: list[tuple[str, str, str | None]] = []

            def __init__(self, opc_home, config):
                self.opc_home = opc_home
                self.config = config

            def list_templates(self):
                return list(self.templates)

            def list_employees(self):
                return list(self.employees)

            def scan_local_talent(self):
                return [
                    SimpleNamespace(
                        id="local-id",
                        name="Local Talent",
                        category="local",
                        domains=[],
                        preferred_external_agent=None,
                        description="Local template.",
                    )
                ]

            def import_local_templates(self, template_ids):
                self.imported_local.append(list(template_ids))
                for template_id in template_ids:
                    if not any(item.id == template_id for item in self.templates):
                        self.templates.append(
                            SimpleNamespace(
                                id=template_id,
                                name="Imported Talent",
                                category="local",
                                domains=[],
                                preferred_external_agent=None,
                                description="Imported.",
                            )
                        )
                return list(self.templates)

            def import_from_repo(self, repo_path):
                self.imported_repos.append(repo_path)
                return list(self.templates)

            def hire_template(self, template_id, role_id, *, employee_name=None, employee_id=None):
                self.hires.append((template_id, role_id, employee_name))
                return SimpleNamespace(name=employee_name or "Hire", role_id=role_id, employee_id="new-employee")

        with patch("opc.cli.app.console", console), patch("opc.cli.app.TalentMarket", _FakeTalentMarket), patch.object(
            OPCConfig,
            "save",
            autospec=True,
        ) as save:
            async def _run() -> None:
                await _handle_chat_slash_command(state, "/talent list")
                await _handle_chat_slash_command(state, "/talent employees")
                await _handle_chat_slash_command(state, "/talent scan")
                await _handle_chat_slash_command(state, "/talent import local-id")
                await _handle_chat_slash_command(state, '/talent import-repo "C:/talent/repo"')
                await _handle_chat_slash_command(state, '/talent hire tpl-1 founder --name "Ada Hire"')

            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Template One", rendered)
        self.assertIn("Eve Employee", rendered)
        self.assertIn("Local Talent", rendered)
        self.assertIn("Hired Ada Hire", rendered)
        self.assertEqual(_FakeTalentMarket.imported_local, [["local-id"]])
        self.assertEqual([str(path) for path in _FakeTalentMarket.imported_repos], ["C:\\talent\\repo"])
        self.assertEqual(_FakeTalentMarket.hires, [("tpl-1", "founder", "Ada Hire")])
        self.assertGreaterEqual(save.call_count, 3)

    def test_market_slash_list_install_uninstall_and_export(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        class _FakeLoader:
            installs: list[str] = []
            uninstalls: list[str] = []

            def __init__(self, config, opc_home):
                self.config = config
                self.opc_home = opc_home

            def load_from_path(self, path):
                return SimpleNamespace(manifest=SimpleNamespace(id="pkg-x", name="Package X"))

            def detect_conflicts(self, package):
                return SimpleNamespace(has_conflicts=False, role_conflicts=[], template_conflicts=[])

            def install(self, package, strategy="namespace"):
                self.installs.append(strategy)
                return SimpleNamespace(package_id="pkg-x", role_ids=["role-x"], template_ids=["tpl-x"])

            def uninstall(self, package_id):
                self.uninstalls.append(package_id)
                return True

        class _FakeChecker:
            def validate(self, package):
                return SimpleNamespace(passed=True, warnings=[], errors=[])

        class _FakeExporter:
            exports: list[tuple[str, str, str, str]] = []
            writes: list[Path] = []

            def __init__(self, config, opc_home):
                self.config = config
                self.opc_home = opc_home

            def export_current(self, package_id, name, description="", version="1.0.0", **kwargs):
                self.exports.append((package_id, name, description, version))
                return SimpleNamespace(manifest=SimpleNamespace(id=package_id))

            def write_to_path(self, package, out_dir):
                self.writes.append(out_dir)
                return out_dir / f"{package.manifest.id}.opcpkg"

        with patch("opc.cli.app.console", console), patch("opc.market.PackageLoader", _FakeLoader), patch(
            "opc.market.SandboxChecker",
            _FakeChecker,
        ), patch("opc.market.PackageExporter", _FakeExporter), patch.object(OPCConfig, "save", autospec=True) as save:
            async def _run() -> None:
                await _handle_chat_slash_command(state, "/market list")
                await _handle_chat_slash_command(state, "/market install pkg-dir --strategy overwrite")
                await _handle_chat_slash_command(state, "/market uninstall pkg-x")
                await _handle_chat_slash_command(state, "/market uninstall pkg-x --yes")
                await _handle_chat_slash_command(state, '/market export --id pkg-out --name "Package Out" --desc "Demo package" --version 2.0.0 --output-dir out')

            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Package One", rendered)
        self.assertIn("Installed pkg-x", rendered)
        self.assertIn("requires --yes", rendered)
        self.assertIn("Uninstalled package pkg-x", rendered)
        self.assertIn("Exported package pkg-out", rendered)
        self.assertEqual(_FakeLoader.installs, ["overwrite"])
        self.assertEqual(_FakeLoader.uninstalls, ["pkg-x"])
        self.assertEqual(_FakeExporter.exports, [("pkg-out", "Package Out", "Demo package", "2.0.0")])
        self.assertGreaterEqual(save.call_count, 2)

    def test_market_slash_presets_and_apply_vc_preset(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console), patch.object(OPCConfig, "save", autospec=True) as save:
            async def _run() -> None:
                await _handle_chat_slash_command(state, "/market presets")
                await _handle_chat_slash_command(state, "/market apply-preset vc-investment-firm --strategy overwrite")

            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("VC Investment Firm", rendered)
        self.assertIn("Applied preset vc-investment-firm", rendered)
        self.assertEqual(state.mode, "company")
        self.assertEqual(state.company_profile, "custom")
        role_by_id = {role.id: role for role in state.config.org.roles}
        self.assertIn("managing_partner", role_by_id)
        self.assertIn("startup_scout", role_by_id)
        self.assertIn("web_search", role_by_id["startup_scout"].tools)
        self.assertEqual(state.config.org.final_decider_role_id, "managing_partner")
        self.assertGreaterEqual(save.call_count, 1)

    def test_reorg_slash_list_show_approve_deny_and_apply(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        store = self._Store()
        state, engine = self._make_state(store=store)

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/reorg list")
            await _handle_chat_slash_command(state, "/reorg show reorg-1")
            await _handle_chat_slash_command(state, '/reorg approve reorg-1 --notes "Looks good"')
            await _handle_chat_slash_command(state, "/reorg deny reorg-1")
            await _handle_chat_slash_command(state, "/reorg apply reorg-1")
            await _handle_chat_slash_command(state, "/reorg apply reorg-1 --yes")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Reorg One", rendered)
        self.assertIn("Adjust roles", rendered)
        self.assertIn("requires --yes", rendered)
        self.assertIn("Applied reorg reorg-1", rendered)
        self.assertEqual(engine.reorg_approvals, [("reorg-1", True, "Looks good"), ("reorg-1", False, "Denied via CLI.")])
        self.assertEqual(engine.reorg_applies, ["reorg-1"])

    def test_aliases_completion_and_full_view_args(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/p list")
            await _handle_chat_slash_command(state, "/s list --limit 1 --full")
            await _handle_chat_slash_command(state, "/t show task-1 --full")
            await _handle_chat_slash_command(state, "/unknown-command")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Projects", rendered)
        self.assertIn("Demo Session", rendered)
        self.assertIn("Pending Task", rendered)
        self.assertIn("Try /help", rendered)

        completer = _OPCSlashCompleter()
        command_completions = list(completer.get_completions(SimpleNamespace(text_before_cursor="/ta"), None))
        self.assertIn("/task", [item.text for item in command_completions])
        task_completions = list(completer.get_completions(SimpleNamespace(text_before_cursor="/task m"), None))
        self.assertIn("move", [item.text for item in task_completions])
        project_root_completions = list(completer.get_completions(SimpleNamespace(text_before_cursor="/project"), None))
        self.assertIn("/project switch", [item.text for item in project_root_completions])
        self.assertIn("/project rename", [item.text for item in project_root_completions])
        session_root_completions = list(completer.get_completions(SimpleNamespace(text_before_cursor="/session"), None))
        self.assertIn("/session resume", [item.text for item in session_root_completions])
        agent_completions = list(completer.get_completions(SimpleNamespace(text_before_cursor="/agent c"), None))
        self.assertIn("codex", [item.text for item in agent_completions])

        async def _async_completion_texts(text_before_cursor: str) -> list[str]:
            return [
                item.text
                async for item in completer.get_completions_async(
                    SimpleNamespace(text_before_cursor=text_before_cursor),
                    None,
                )
            ]

        self.assertIn("move", asyncio.run(_async_completion_texts("/task m")))

    def test_runtime_slash_renders_live_state_sessions_external_and_checkpoint(self) -> None:
        console = Console(record=True, force_terminal=False, width=200)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, "/runtime --limit 5"))

        rendered = console.export_text()
        self.assertIn("Current Runtime Snapshot", rendered)
        self.assertIn("Active Tasks", rendered)
        self.assertIn("Runtime Sessions", rendered)
        self.assertIn("rt-1", rendered)
        self.assertIn("External Agent Sessions", rendered)
        self.assertIn("provider-1", rendered)
        self.assertIn("Pending Checkpoints", rendered)
        self.assertIn("cp-1", rendered)

    def test_legacy_recover_slash_is_not_registered(self) -> None:
        console = Console(record=True, force_terminal=False, width=200)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, "/recover"))

        rendered = console.export_text()
        self.assertIn("Unknown command: /recover", rendered)
        self.assertNotIn("Interrupted Company Runtimes", rendered)

    def test_logs_slash_renders_task_and_session_runtime_details(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        state, _engine = self._make_state(store=self._Store())

        async def _run() -> None:
            await _handle_chat_slash_command(state, "/logs task-1 --limit 5")
            await _handle_chat_slash_command(state, "/logs sess-1 --limit 5")

        with patch("opc.cli.app.console", console):
            asyncio.run(_run())

        rendered = console.export_text()
        self.assertIn("Task Log Target task-1", rendered)
        self.assertIn("Started task", rendered)
        self.assertIn("Runtime Events: rt-1", rendered)
        self.assertIn("Tool Calls: rt-1", rendered)
        self.assertIn("Tool Results: rt-1", rendered)
        self.assertIn("Permission Grants: rt-1", rendered)
        self.assertIn("Session Log Target sess-1", rendered)
        self.assertIn("Transcript for sess-1", rendered)

    def test_comms_slash_renders_messages_handoffs_and_review_notes(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, "/comms task-1 --limit 5"))

        rendered = console.export_text()
        self.assertIn("Company Messages", rendered)
        self.assertIn("Need review", rendered)
        self.assertIn("Handoffs", rendered)
        self.assertIn("Handoff summary", rendered)
        self.assertIn("Review and Handoff Notes", rendered)
        self.assertIn("Worker handoff summary", rendered)

    def test_attachments_slash_lists_current_session_attachment_refs(self) -> None:
        console = Console(record=True, force_terminal=False, width=220)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_handle_chat_slash_command(state, "/attachments --limit 10"))

        rendered = console.export_text()
        self.assertIn("Attachments", rendered)
        self.assertIn("brief.md", rendered)
        self.assertIn("image.png", rendered)
        self.assertIn("data.json", rendered)

    def test_process_message_updates_checkpoint_hint_after_turn(self) -> None:
        console = Console(record=True, force_terminal=False, width=180)
        state, _engine = self._make_state(store=self._Store())

        with patch("opc.cli.app.console", console):
            asyncio.run(_process_interactive_chat_message(state, "Continue"))

        self.assertEqual(state.runtime_display.checkpoint_hint, "task_user_input")


class CliWindowsSSLTests(unittest.TestCase):
    def test_pop_windows_sslkeylogfile_removes_variable_on_windows(self) -> None:
        with patch("opc.core.windows_ssl.os.name", "nt"), patch(
            "opc.core.windows_ssl._REMOVED_SSLKEYLOGFILE",
            None,
        ), patch.dict(
            os.environ,
            {"SSLKEYLOGFILE": r"C:\temp\sslkeys.log"},
            clear=False,
        ):
            removed = pop_windows_sslkeylogfile()
            self.assertNotIn("SSLKEYLOGFILE", os.environ)

        self.assertEqual(removed, r"C:\temp\sslkeys.log")

    def test_sanitize_windows_sslkeylogfile_caches_path_for_one_later_warning(self) -> None:
        with patch("opc.core.windows_ssl.os.name", "nt"), patch(
            "opc.core.windows_ssl._REMOVED_SSLKEYLOGFILE",
            None,
        ), patch.dict(
            os.environ,
            {"SSLKEYLOGFILE": r"C:\temp\sslkeys.log"},
            clear=False,
        ):
            removed = sanitize_windows_sslkeylogfile()
            self.assertEqual(removed, r"C:\temp\sslkeys.log")
            self.assertNotIn("SSLKEYLOGFILE", os.environ)
            self.assertEqual(pop_windows_sslkeylogfile(), r"C:\temp\sslkeys.log")
            self.assertIsNone(pop_windows_sslkeylogfile())

    def test_format_windows_sslkeylog_warning_mentions_command_and_path(self) -> None:
        rendered = format_windows_sslkeylog_warning("opc chat", r"C:\temp\sslkeys.log")
        self.assertIn("opc chat", rendered)
        self.assertIn(r"C:\temp\sslkeys.log", rendered)
        self.assertIn("aiohttp/OpenSSL", rendered)

    def test_main_warns_once_when_sslkeylogfile_is_removed(self) -> None:
        console = Console(record=True, force_terminal=False, width=120)

        with patch("opc.cli.app.pop_windows_sslkeylogfile", return_value=r"C:\temp\sslkeys.log"), patch(
            "opc.cli.app._current_command_label",
            return_value="opc chat",
        ), patch("opc.cli.app.console", console), patch("opc.cli.app.app") as mock_app:
            main()

        rendered = console.export_text()
        self.assertIn("ignoring SSLKEYLOGFILE", rendered)
        self.assertIn("opc chat", rendered)
        mock_app.assert_called_once_with()

    def test_opc_package_import_sanitizes_sslkeylogfile_early(self) -> None:
        try:
            with patch("opc.core.windows_ssl.sanitize_windows_sslkeylogfile") as sanitize:
                importlib.reload(opc)
                sanitize.assert_called_once_with()
        finally:
            importlib.reload(opc)


class ProjectServiceRenameTests(unittest.TestCase):
    def test_project_rename_moves_project_memory_workplace_and_rewrites_ids(self) -> None:
        class _ChatStore:
            def __init__(self) -> None:
                self.renames: list[tuple[str, str]] = []

            async def project_data_exists(self, project_id: str) -> bool:
                return False

            async def rename_project_data(self, old_project_id: str, new_project_id: str):
                self.renames.append((old_project_id, new_project_id))
                return {"channels": 1}

        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                opc_home = root / ".opc"
                old_dir = opc_home / "projects" / "old"
                old_dir.mkdir(parents=True)
                db_path = old_dir / "tasks.db"
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT)")
                conn.execute("INSERT INTO tasks (id, project_id) VALUES ('task-1', 'old')")
                conn.commit()
                conn.close()
                memory_dir = opc_home / "memory" / "projects"
                memory_dir.mkdir(parents=True)
                (memory_dir / "old.md").write_text("# Project Memory (old)", encoding="utf-8")
                workplace_root = root / "workplace"
                (workplace_root / "old").mkdir(parents=True)
                (workplace_root / "old" / "note.txt").write_text("hello", encoding="utf-8")

                engine = SimpleNamespace(opc_home=opc_home, project_id="default")
                chat_store = _ChatStore()
                context = OfficeServiceContext(
                    engine=engine,
                    agent_store=SimpleNamespace(),
                    chat_store=chat_store,
                    event_adapter=SimpleNamespace(),
                )
                context.project_workplace_hook = lambda project_id: workplace_root / project_id

                result = await ProjectService(context).rename("old", "new")

                self.assertTrue((opc_home / "projects" / "new").is_dir())
                self.assertFalse(old_dir.exists())
                self.assertTrue((memory_dir / "new.md").exists())
                self.assertTrue((workplace_root / "new" / "note.txt").exists())
                conn = sqlite3.connect(opc_home / "projects" / "new" / "tasks.db")
                row = conn.execute("SELECT project_id FROM tasks WHERE id = 'task-1'").fetchone()
                conn.close()
                self.assertEqual(row[0], "new")
                self.assertEqual(chat_store.renames, [("old", "new")])
                self.assertEqual(result.payload["old_project_id"], "old")
                self.assertEqual(result.payload["new_project_id"], "new")

        asyncio.run(_run())


class CliChannelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_channels_login_includes_config_guidance(self) -> None:
        result = self.runner.invoke(app, ["channels", "login", "slack"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn(".opc/config/channel_config.yaml", result.stdout)
        self.assertIn("opc channels start", result.stdout)

    def test_channels_start_runs_runtime_helper(self) -> None:
        config = OPCConfig()
        seen: list[tuple[OPCConfig, str | None]] = []

        async def fake_run_channel_runtime(passed_config, project):
            seen.append((passed_config, project))

        with patch("opc.cli.app._get_config", return_value=config), patch(
            "opc.cli.app._run_channel_runtime",
            side_effect=fake_run_channel_runtime,
        ):
            result = self.runner.invoke(app, ["channels", "start", "--project", "demo"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(seen, [(config, "demo")])

    def test_channels_stop_sends_sigterm_to_runtime_pid(self) -> None:
        with patch("opc.cli.app._read_channel_runtime_state", return_value={"pid": 4321, "channels": ["slack"]}), patch(
            "opc.cli.app._pid_is_running",
            return_value=True,
        ), patch("opc.cli.app.os.kill") as mock_kill:
            result = self.runner.invoke(app, ["channels", "stop"])

        self.assertEqual(result.exit_code, 0)
        mock_kill.assert_called_once()
        self.assertIn("PID 4321", _plain_output(result.stdout))

    def test_channels_status_reports_runtime_and_capabilities(self) -> None:
        config = OPCConfig()
        config.channels.slack.enabled = True

        class _Channel:
            def describe_capability(self):
                return {"available": True, "delivery_mode": "socket"}

        class _Manager:
            def __init__(self, config, bus):
                self.config = config
                self.bus = bus

            def get_channel(self, name: str):
                if name == "slack":
                    return _Channel()
                return None

        with patch("opc.cli.app._get_config", return_value=config), patch(
            "opc.cli.app._read_channel_runtime_state",
            return_value={"pid": 1234, "channels": ["slack"]},
        ), patch("opc.cli.app._pid_is_running", return_value=True), patch(
            "opc.channels.manager.ChannelManager",
            _Manager,
        ), patch(
            "opc.layer0_interaction.message_bus.MessageBus",
            return_value=object(),
        ):
            result = self.runner.invoke(app, ["channels", "status"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Runtime:", result.stdout)
        self.assertIn("running", result.stdout)
        self.assertIn("slack: enabled, available, configured, socket, runtime-active", result.stdout)


class CliAutomationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_exec_help_exposes_automation_options(self) -> None:
        result = self.runner.invoke(app, ["exec", "--help"])

        self.assertEqual(result.exit_code, 0)
        help_text = _plain_output(result.output)
        self.assertIn("--session-id", help_text)
        self.assertIn("--resume", help_text)
        self.assertIn("--stream-json", help_text)

    def test_exec_rejects_json_and_stream_json_together(self) -> None:
        result = self.runner.invoke(app, ["exec", "hello", "--json", "--stream-json"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("Use either --json or --stream-json", result.output)


class CliTalentCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_talent_import_and_hire_persist_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "agency-agents"
            (repo / "engineering").mkdir(parents=True)
            (repo / "engineering" / "backend-architect.md").write_text(
                "---\n"
                "name: Backend Architect\n"
                "description: Designs backend systems and API implementations.\n"
                "---\n\n"
                "# Backend Architect\n\n"
                "Focus on backend architecture, APIs, and implementation quality.\n",
                encoding="utf-8",
            )
            opc_home = tmp / ".opc"

            with patch.dict(os.environ, {"OPC_HOME": str(opc_home)}, clear=False):
                import_result = self.runner.invoke(app, ["talent", "import", str(repo)])
                hire_result = self.runner.invoke(
                    app,
                    ["talent", "hire", "engineering-backend-architect", "senior_engineer", "--name", "Bea Backend"],
                )
                list_result = self.runner.invoke(app, ["talent", "list"])

            self.assertEqual(import_result.exit_code, 0)
            self.assertEqual(hire_result.exit_code, 0)
            self.assertEqual(list_result.exit_code, 0)
            self.assertIn("Imported 1 talent templates", _plain_output(import_result.stdout))
            self.assertIn("Bea Backend", _plain_output(hire_result.stdout))
            self.assertIn("engineering-backend-architect", _plain_output(list_result.stdout))

            config = OPCConfig.load(opc_home / "config")
            self.assertEqual(config.org.talent_templates, [])
            template = TalentMarket(opc_home, config).get_template("engineering-backend-architect")
            self.assertIsNotNone(template)
            assert template is not None
            self.assertEqual(template.prompt_ref, "prompts/talent/engineering-backend-architect.md")
            self.assertEqual(len(config.org.employees), 1)
            self.assertEqual(config.org.employees[0].name, "Bea Backend")
            self.assertEqual(config.org.employees[0].role_id, "senior_engineer")
            prompt_path = opc_home / "prompts" / "talent" / "engineering-backend-architect.md"
            self.assertTrue(prompt_path.exists())
            prompt_text = prompt_path.read_text(encoding="utf-8")
            self.assertIn("name: Backend Architect", prompt_text)
            self.assertIn("description: Designs backend systems and API implementations.", prompt_text)
            self.assertIn("# Backend Architect", prompt_text)

    def test_talent_import_accepts_relaxed_frontmatter_with_extra_colons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "agency-agents"
            (repo / "specialized").mkdir(parents=True)
            (repo / "specialized" / "zk-steward.md").write_text(
                "---\n"
                "name: ZK Steward\n"
                "description: Knowledge-base steward. Default perspective: Luhmann; switches to domain experts by task.\n"
                "tools: WebFetch, WebSearch, Read, Write, Edit\n"
                "---\n\n"
                "# ZK Steward\n\n"
                "Maintains a Zettelkasten knowledge base.\n",
                encoding="utf-8",
            )
            opc_home = tmp / ".opc"

            with patch.dict(os.environ, {"OPC_HOME": str(opc_home)}, clear=False):
                result = self.runner.invoke(app, ["talent", "import", str(repo)])

            self.assertEqual(result.exit_code, 0)
            config = OPCConfig.load(opc_home / "config")
            self.assertEqual(config.org.talent_templates, [])
            template = TalentMarket(opc_home, config).get_template("specialized-zk-steward")
            self.assertIsNotNone(template)
            assert template is not None
            self.assertEqual(template.name, "ZK Steward")
            self.assertIn("Default perspective: Luhmann", template.description)
            prompt_text = (opc_home / "prompts" / "talent" / "specialized-zk-steward.md").read_text(encoding="utf-8")
            self.assertIn("tools: WebFetch, WebSearch, Read, Write, Edit", prompt_text)

    def test_talent_import_recurses_named_templates_without_nested_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = tmp / "agency-agents"
            (repo / "game-development" / "godot").mkdir(parents=True)
            (repo / "strategy" / "playbooks").mkdir(parents=True)
            (repo / "game-development" / "godot" / "godot-gameplay-scripter.md").write_text(
                "---\n"
                "name: Godot Gameplay Scripter\n"
                "description: Builds typed Godot gameplay systems.\n"
                "---\n\n"
                "# Godot Gameplay Scripter\n",
                encoding="utf-8",
            )
            (repo / "strategy" / "playbooks" / "phase-0-discovery.md").write_text(
                "# Phase 0 Discovery\n\n"
                "This is a playbook, not a staff template.\n",
                encoding="utf-8",
            )
            opc_home = tmp / ".opc"

            with patch.dict(os.environ, {"OPC_HOME": str(opc_home)}, clear=False):
                result = self.runner.invoke(app, ["talent", "import", str(repo)])

            self.assertEqual(result.exit_code, 0)
            config = OPCConfig.load(opc_home / "config")
            template_ids = [template.id for template in TalentMarket(opc_home, config).scan_local_talent()]
            self.assertEqual(template_ids, ["game-development-godot-gameplay-scripter"])
            prompt_path = opc_home / "prompts" / "talent" / "game-development-godot-gameplay-scripter.md"
            self.assertTrue(prompt_path.exists())
            self.assertFalse((opc_home / "prompts" / "talent" / "strategy-phase-0-discovery.md").exists())


class CliBoardCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_board_command_dispatches_to_plugin_entry(self) -> None:
        with patch("opc.plugins.cli_board.entry.launch_board") as mock_launch:
            result = self.runner.invoke(app, ["board", "--project", "demo", "--refresh-interval", "1.5"])

        self.assertEqual(result.exit_code, 0)
        mock_launch.assert_called_once_with(
            project_id="demo",
            refresh_interval=1.5,
            attach=False,
            readonly=False,
            initial_view="kanban",
            initial_session_id=None,
            initial_work_item_id=None,
            initial_role_id=None,
            initial_target=None,
        )

    def test_board_command_accepts_inspector_options(self) -> None:
        with patch("opc.plugins.cli_board.entry.launch_board") as mock_launch:
            result = self.runner.invoke(
                app,
                [
                    "board",
                    "--project",
                    "demo",
                    "--attach",
                    "--readonly",
                    "--view",
                    "role",
                    "--role",
                    "cto",
                    "--session",
                    "sess-1",
                ],
            )

        self.assertEqual(result.exit_code, 0)
        mock_launch.assert_called_once()
        self.assertTrue(mock_launch.call_args.kwargs["attach"])
        self.assertTrue(mock_launch.call_args.kwargs["readonly"])
        self.assertEqual(mock_launch.call_args.kwargs["initial_view"], "role")
        self.assertEqual(mock_launch.call_args.kwargs["initial_role_id"], "cto")
        self.assertEqual(mock_launch.call_args.kwargs["initial_session_id"], "sess-1")


if __name__ == "__main__":
    unittest.main()
