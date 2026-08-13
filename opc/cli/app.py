"""OPC CLI — the primary user interface for the One-Person Company system."""

from __future__ import annotations

import asyncio
from collections import deque
from importlib import resources as importlib_resources
import json
import os
import shlex
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Optional
import re

import typer
from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.theme import Theme

from opc import __version__
from opc.core.config import OPCConfig, get_opc_home
from opc.core.windows_ssl import (
    format_windows_sslkeylog_warning,
    pop_windows_sslkeylogfile,
)
from opc.database.store import OPCStore
from opc.layer2_organization.talent_market import TalentMarket
from opc.core.models import OPCEvent

# Proxy is configured via llm_config.yaml or shell environment — not hardcoded.
# os.environ['https_proxy'] = 'http://127.0.0.1:7890'
# os.environ['http_proxy'] = 'http://127.0.0.1:7890'

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "red bold",
    "success": "green bold",
    "agent": "blue",
    "tool": "magenta",
})
console = Console(theme=custom_theme)
app = typer.Typer(
    name="opc",
    help="OPC — One-Person Company: Autonomous AI Agent system with natural language interface.",
    no_args_is_help=True,
)

_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


class _CliRepoPath:
    """Native filesystem path with stable Windows-drive display semantics."""

    def __init__(self, value: str) -> None:
        self._native = Path(value)
        self._display = str(PureWindowsPath(value))

    def __fspath__(self) -> str:
        return os.fspath(self._native)

    def __str__(self) -> str:
        return self._display

    def __getattr__(self, name: str) -> Any:
        return getattr(self._native, name)


def _talent_repo_path(value: str) -> Path | _CliRepoPath:
    if _WINDOWS_DRIVE_PATH_RE.match(str(value or "")):
        return _CliRepoPath(value)
    return Path(value)


def _current_command_label(argv: list[str] | None = None) -> str:
    args = list(argv or sys.argv)
    if len(args) > 1 and not args[1].startswith("-"):
        return f"opc {args[1]}"
    return "opc"


def _get_config() -> OPCConfig:
    config_dir = get_opc_home() / "config"
    if config_dir.exists():
        return OPCConfig.load(config_dir)
    return OPCConfig()


def _channel_runtime_pid_path() -> Path:
    from opc.core.config import get_opc_home, get_project_workplace

    return get_opc_home() / "run" / "channels.pid"


def _read_channel_runtime_state() -> dict | None:
    path = _channel_runtime_pid_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_channel_runtime_state(enabled_channels: list[str]) -> None:
    path = _channel_runtime_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "channels": enabled_channels}, ensure_ascii=False))


def _clear_channel_runtime_state() -> None:
    path = _channel_runtime_pid_path()
    if path.exists():
        path.unlink()


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _create_default_skills(opc_home: Path) -> None:
    """Copy bundled skills to the OPC home skills directory."""
    import shutil

    repo_skills = Path(__file__).parent.parent.parent / "skills" / "core"
    target_skills = opc_home / "skills" / "core"
    target_skills.mkdir(parents=True, exist_ok=True)

    copied = False
    if repo_skills.exists():
        for skill_file in repo_skills.glob("*.md"):
            dest = target_skills / skill_file.name
            if not dest.exists():
                shutil.copy2(skill_file, dest)
            copied = True
    if copied:
        return

    try:
        resource_skills = importlib_resources.files("opc").joinpath("skills_assets", "core")
    except Exception:
        return
    if not resource_skills.is_dir():
        return
    for child in resource_skills.iterdir():
        if child.is_file() and child.name.endswith(".md"):
            dest = target_skills / child.name
            if not dest.exists():
                dest.write_bytes(child.read_bytes())


def _progress_callback():
    """Create an async progress callback for streaming agent output."""
    async def callback(text: str, **_: Any) -> None:
        if text.startswith("[Tool:"):
            console.print(f"  [tool]{escape(text)}[/tool]")
        elif text.startswith("[Suggestion]"):
            console.print(Panel(text, title="Suggestion", border_style="yellow"))
        elif text.startswith("[Delegating") or text.startswith("[Autonomy]"):
            console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External approval]"):
            console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External status]"):
            if _should_show_external_status(text):
                console.print(f"  [info]{escape(text)}[/info]")
        elif text.startswith("[External agent denied]"):
            console.print(f"  [warning]{escape(text)}[/warning]")
        elif text.startswith("[External agent failed]") or text.startswith("[External agents exhausted]"):
            console.print(f"  [warning]{escape(text)}[/warning]")
        elif text.startswith("[External:"):
            if _cli_verbose_external_progress():
                console.print(f"  [warning]{escape(text)}[/warning]")
        else:
            pass
    return callback


def _cli_verbose_external_progress() -> bool:
    value = os.environ.get("OPC_CLI_VERBOSE_EXTERNAL", "")
    return value.strip().lower() in {"1", "true", "yes", "on", "debug"}


def _should_show_external_status(text: str) -> bool:
    if _cli_verbose_external_progress():
        return True
    detail = text.split("]", 1)[1].strip().lower() if "]" in text else text.lower()
    if "working; last activity" in detail:
        return False
    return any(token in detail for token in ("started", "approval", "timeout", "failed", "denied", "exhausted", "awaiting", "requires"))


@dataclass
class _QueuedAssistantLine:
    text: str
    enqueued_at: float


class _CliRuntimeDisplay:
    def __init__(self, rich_console: Console) -> None:
        self.console = rich_console
        self._assistant_buffer = ""
        self._line_queue: deque[_QueuedAssistantLine] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        self._mode = "smooth"
        self._below_exit_since: float | None = None
        self._last_exit_at: float | None = None
        self._status_snapshot: dict[str, object] = {}
        self._checkpoint_hint = ""
        self._last_status_render = ""
        self._last_status_render_at = 0.0
        self._sidecar_quiet_depth = 0
        self._sidecar_quiet_event_count = 0
        self.has_streamed_content = False

    def begin_turn(self) -> None:
        self.has_streamed_content = False
        self._assistant_buffer = ""
        self._line_queue.clear()
        self._mode = "smooth"
        self._below_exit_since = None
        self._last_exit_at = None
        self._checkpoint_hint = ""

    def status_snapshot(self) -> dict[str, object]:
        snapshot = dict(self._status_snapshot)
        snapshot["stream_queue_depth"] = len(self._line_queue)
        snapshot["stream_drain_mode"] = self._mode
        if self._checkpoint_hint:
            snapshot["checkpoint_hint"] = self._checkpoint_hint
        return snapshot

    def set_checkpoint_hint(self, hint: str) -> None:
        self._checkpoint_hint = hint.strip()

    async def render_status(self, *, force: bool = False) -> None:
        await self._render_status(force=force)

    async def handle_event(self, event: OPCEvent) -> None:
        if getattr(event, "event_type", "") != "runtime_event":
            return
        payload = dict(getattr(event, "payload", {}) or {})
        runtime_type = str(payload.get("type", "") or "").strip()
        if self._sidecar_quiet_depth > 0:
            self._sidecar_quiet_event_count += 1
            if runtime_type == "status_snapshot":
                self._status_snapshot = payload
            elif runtime_type in {"turn_completed", "turn_failed"}:
                self._assistant_buffer = ""
                self._line_queue.clear()
            return
        if runtime_type == "assistant_delta":
            self._enqueue_assistant_delta(str(payload.get("text", "") or ""))
            self._ensure_drain_task()
            return
        if runtime_type in {"turn_completed", "turn_failed"}:
            self._finalize_assistant_buffer()
            await self.flush()
            await self._render_status(force=True)
            return
        if runtime_type == "status_snapshot":
            self._status_snapshot = payload
            await self._render_status()
            return
        if runtime_type == "tool_started":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            self.console.print(f"  [tool]Running {escape(tool_name)}[/tool]")
            return
        if runtime_type == "tool_completed":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            elapsed_ms = int(payload.get("elapsed_ms", 0) or 0)
            summary = str(payload.get("result_summary", "") or "").strip()
            suffix = f" ({elapsed_ms}ms)" if elapsed_ms > 0 else ""
            if summary:
                self.console.print(f"  [tool]{escape(tool_name)} finished{suffix}: {escape(summary)}[/tool]")
            else:
                self.console.print(f"  [tool]{escape(tool_name)} finished{suffix}[/tool]")
            return
        if runtime_type == "permission_requested":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            rationale = str(payload.get("rationale", "") or "").strip()
            self.console.print(
                f"  [warning]Approval needed for {escape(tool_name)}: {escape(rationale or 'awaiting review')}[/warning]"
            )
            return
        if runtime_type == "sandbox_retry_requested":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            self.console.print(
                f"  [info]Retrying {escape(tool_name)} with sandbox upgrade "
                f"{escape(str(payload.get('from_mode', 'unknown')))} -> "
                f"{escape(str(payload.get('to_mode', 'unknown')))}[/info]"
            )
            return
        if runtime_type == "sandbox_retry_completed":
            tool_name = str(payload.get("tool_name", "") or "tool").strip()
            state = "succeeded" if bool(payload.get("success", False)) else "failed"
            self.console.print(f"  [info]Sandbox retry {state} for {escape(tool_name)}[/info]")
            return
        if runtime_type == "context_warning":
            pct = int(payload.get("context_remaining_pct", 0) or 0)
            self.console.print(f"  [warning]Context window low: {pct}% remaining[/warning]")

    def _enqueue_assistant_delta(self, text: str) -> None:
        if not text:
            return
        self._assistant_buffer += text
        while "\n" in self._assistant_buffer:
            line, self._assistant_buffer = self._assistant_buffer.split("\n", 1)
            self._line_queue.append(_QueuedAssistantLine(line, time.monotonic()))

    def _finalize_assistant_buffer(self) -> None:
        if self._assistant_buffer:
            self._line_queue.append(_QueuedAssistantLine(self._assistant_buffer, time.monotonic()))
            self._assistant_buffer = ""

    def _ensure_drain_task(self) -> None:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop())

    def _drain_count(self, now: float) -> int:
        queued = len(self._line_queue)
        if queued == 0:
            self._mode = "smooth"
            self._below_exit_since = None
            return 0
        oldest_age_ms = int(max(0.0, now - self._line_queue[0].enqueued_at) * 1000)
        severe = queued >= 64 or oldest_age_ms >= 300
        if self._mode == "smooth":
            if (queued >= 8 or oldest_age_ms >= 120) and (not self._last_exit_at or severe or (now - self._last_exit_at) >= 0.25):
                self._mode = "catch_up"
                self._below_exit_since = None
        else:
            below_exit = queued <= 2 and oldest_age_ms <= 40
            if below_exit:
                if self._below_exit_since is None:
                    self._below_exit_since = now
                elif (now - self._below_exit_since) >= 0.25:
                    self._mode = "smooth"
                    self._below_exit_since = None
                    self._last_exit_at = now
            else:
                self._below_exit_since = None
        return queued if self._mode == "catch_up" else 1

    async def _drain_loop(self) -> None:
        try:
            while self._line_queue:
                now = time.monotonic()
                count = self._drain_count(now)
                for _ in range(min(count, len(self._line_queue))):
                    line = self._line_queue.popleft()
                    self.console.print(line.text, markup=False)
                    self.has_streamed_content = True
                await self._render_status()
                await asyncio.sleep(0.05)
        finally:
            self._drain_task = None

    async def _render_status(self, *, force: bool = False) -> None:
        now = time.monotonic()
        snapshot = dict(self._status_snapshot)
        stream_queue_depth = len(self._line_queue)
        runtime_queue_depth = snapshot.get("queue_depth", 0)
        current_tool = str(snapshot.get("current_tool", "") or "").strip()
        agent_id = str(snapshot.get("agent_id", "") or snapshot.get("role_id", "") or "").strip()
        task_id = str(snapshot.get("task_id", "") or "").strip()
        context_remaining_pct = snapshot.get("context_remaining_pct")
        turn_cost = snapshot.get("turn_cost_usd")
        session_cost = snapshot.get("session_cost_usd")
        pending_permission_count = snapshot.get("pending_permission_count")
        parts: list[str] = []
        parts.append(f"queue={int(runtime_queue_depth or 0)}")
        parts.append(f"stream_queue={stream_queue_depth}")
        parts.append(f"drain={self._mode}")
        if agent_id:
            parts.append(f"agent={agent_id}")
        if task_id:
            parts.append(f"task={task_id}")
        if current_tool:
            elapsed_ms = int(snapshot.get("tool_elapsed_ms", 0) or 0)
            tool_part = f"tool={current_tool}"
            if elapsed_ms > 0:
                tool_part += f" {elapsed_ms}ms"
            parts.append(tool_part)
        if context_remaining_pct not in (None, ""):
            parts.append(f"context={int(context_remaining_pct)}%")
        if turn_cost not in (None, ""):
            parts.append(f"turn=${float(turn_cost):.4f}")
        if session_cost not in (None, ""):
            parts.append(f"session=${float(session_cost):.4f}")
        if pending_permission_count not in (None, "") and int(pending_permission_count or 0) > 0:
            parts.append(f"approvals={int(pending_permission_count or 0)}")
        if self._checkpoint_hint:
            parts.append(f"checkpoint={self._checkpoint_hint}")
        message = " | ".join(parts)
        if not message:
            return
        if not force and (message == self._last_status_render or (now - self._last_status_render_at) < 0.75):
            return
        self.console.print(f"[dim][status][/dim] {message}")
        self._last_status_render = message
        self._last_status_render_at = now

    async def flush(self) -> None:
        self._finalize_assistant_buffer()
        if self._drain_task is not None:
            await self._drain_task
        while self._line_queue:
            line = self._line_queue.popleft()
            self.console.print(line.text)
            self.has_streamed_content = True

    def enter_sidecar_quiet(self) -> None:
        self._sidecar_quiet_depth += 1

    def exit_sidecar_quiet(self) -> int:
        if self._sidecar_quiet_depth > 0:
            self._sidecar_quiet_depth -= 1
        count = self._sidecar_quiet_event_count
        if self._sidecar_quiet_depth == 0:
            self._sidecar_quiet_event_count = 0
        return count


def _create_cli_engine(config, project: str | None):
    from opc.engine import OPCEngine

    runtime_display = _CliRuntimeDisplay(console)
    engine = OPCEngine(
        config=config,
        project_id=project,
        on_progress=_progress_callback(),
        on_runtime_event=runtime_display.handle_event,
        on_escalation=_escalation_callback(),
    )
    return engine, runtime_display


def _attach_cli_runtime_callbacks(state: _InteractiveChatState) -> None:
    def on_company_runtime_children(session_id: str, task_ids: list[str]) -> None:
        parent_session_id = str(session_id or "").strip()
        clean_task_ids = [str(item or "").strip() for item in list(task_ids or []) if str(item or "").strip()]
        if parent_session_id and clean_task_ids:
            state.session_to_task[parent_session_id] = clean_task_ids[0]
            for task_id in clean_task_ids:
                state.active_runtime_children[task_id] = clean_task_ids[0]

    try:
        state.engine.on_company_runtime_children = on_company_runtime_children
    except Exception:
        pass


def _normalize_escalation_key(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip()).strip("_").casefold()


def _format_escalation_option(option: dict, index: int) -> str:
    option_id = str(option.get("id", "")).strip()
    label = str(option.get("label", option_id)).strip() or option_id
    # Use an explicit numeric prefix and escape markup so Rich never hides the id.
    return f"  {index}. {escape(label)} ({escape(option_id)})"


def _normalize_escalation_reply(reply: str, options: list[dict]) -> str | None:
    raw_reply = reply.strip()
    if not raw_reply:
        return None

    normalized_map: dict[str, str] = {}
    for idx, option in enumerate(options, start=1):
        option_id = str(option.get("id", "")).strip()
        label = str(option.get("label", option_id)).strip()
        if not option_id:
            continue
        normalized_map[option_id.casefold()] = option_id
        normalized_map[_normalize_escalation_key(option_id)] = option_id
        if label:
            normalized_map[label.casefold()] = option_id
            normalized_map[_normalize_escalation_key(label)] = option_id
        normalized_map[str(idx)] = option_id

    alias_map = {
        "y": "approve_once",
        "yes": "approve_once",
        "approve": "approve_once",
        "allow": "approve_once",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "reject": "deny",
        "project": "always_project",
        "global": "always_global",
    }
    if alias := alias_map.get(_normalize_escalation_key(raw_reply)):
        if alias in normalized_map.values():
            return alias

    return normalized_map.get(raw_reply.casefold()) or normalized_map.get(_normalize_escalation_key(raw_reply))


def _escalation_callback():
    """Create an async escalation callback for human-in-the-loop."""
    async def callback(message: str, options: list[dict]) -> str | None:
        console.print(Panel(message, title="Action Required", border_style="yellow"))
        if options:
            for idx, opt in enumerate(options, start=1):
                console.print(_format_escalation_option(opt, idx))
            console.print("  Enter the number, label, or internal id.")
            while True:
                try:
                    reply = console.input("[bold]Your choice: [/bold]")
                except (EOFError, KeyboardInterrupt):
                    return None
                normalized = _normalize_escalation_reply(reply, options)
                if normalized is not None:
                    return normalized
                console.print("[warning]Invalid choice. Please enter a listed number, label, or id.[/warning]")
        else:
            try:
                reply = console.input("[bold]Your reply: [/bold]")
                return reply.strip()
            except (EOFError, KeyboardInterrupt):
                return None
    return callback


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def chat(
    message: Optional[str] = typer.Argument(None, help="Single message to process"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID to work in"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default LLM model"),
    mode: str = typer.Option("task", "--mode", help="Execution mode: task or company"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent: native, claude_code, codex, cursor, opencode"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile for company mode"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed logs"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
):
    """Chat with the OPC system — interactive or single message mode."""
    config = _get_config()
    if model:
        config.llm.default_model = model
    if verbose:
        config.system.log_level = "DEBUG"

    if message:
        asyncio.run(_single_message(
            config,
            message,
            project,
            no_markdown,
            mode=mode,
            preferred_agent=agent,
            company_profile=company_profile,
        ))
    else:
        asyncio.run(_interactive_mode(
            config,
            project,
            no_markdown,
            mode=mode,
            preferred_agent=agent,
            company_profile=company_profile,
            explicit_mode=_cli_option_present("--mode"),
            explicit_agent=_cli_option_present("--agent"),
            explicit_company_profile=_cli_option_present("--company-profile"),
        ))


@app.command("exec")
def exec_command(
    prompt: Optional[str] = typer.Argument(None, help="Prompt to run non-interactively. Reads stdin when omitted."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID to work in"),
    mode: str = typer.Option("task", "--mode", help="Execution mode: task, company, or org"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile for company mode"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent: native, claude_code, codex, cursor, opencode"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id for org/custom mode"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Existing task-backed session id to reuse"),
    resume: bool = typer.Option(False, "--resume", help="Resume the latest task-backed session in the project"),
    json_output: bool = typer.Option(False, "--json", help="Print final JSON payload"),
    stream_json: bool = typer.Option(False, "--stream-json", help="Print newline-delimited JSON runtime events"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
):
    """Run one non-interactive OPC task for scripts and CI."""
    if json_output and stream_json:
        console.print("[error]Use either --json or --stream-json, not both.[/error]")
        raise typer.Exit(code=2)
    if session_id and resume:
        console.print("[error]Use either --session-id or --resume, not both.[/error]")
        raise typer.Exit(code=2)
    message = prompt
    if message is None and not sys.stdin.isatty():
        message = sys.stdin.read()
    config = _get_config()
    try:
        asyncio.run(_exec_message(
            config=config,
            prompt=str(message or "").strip(),
            project=project,
            mode=mode,
            company_profile=company_profile,
            preferred_agent=agent,
            org_id=org_id,
            session_id=session_id,
            resume=resume,
            json_output=json_output,
            stream_json=stream_json,
            no_markdown=no_markdown,
        ))
    except KeyboardInterrupt as exc:
        if stream_json:
            _print_exec_event(
                {"seq": 0},
                "error",
                project_id=project or "default",
                payload={"code": "cancelled", "error": "Interrupted"},
            )
        else:
            console.print("\n[warning]Interrupted.[/warning]")
        raise typer.Exit(code=130) from exc


@app.command()
def secretary(
    message: Optional[str] = typer.Argument(None, help="Single secretary message"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID for secretary context"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override default LLM model"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed logs"),
    no_markdown: bool = typer.Option(False, "--no-markdown", help="Plain text output"),
):
    """Talk directly with the long-term secretary interface."""
    config = _get_config()
    if model:
        config.llm.default_model = model
    if verbose:
        config.system.log_level = "DEBUG"

    if message:
        asyncio.run(_single_secretary_message(config, message, project, no_markdown))
    else:
        asyncio.run(_interactive_secretary_mode(config, project, no_markdown))


@app.command("propose-reorg")
def propose_reorg(
    payload: str = typer.Argument(..., help="JSON payload with summary/rationale/changeset"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Create a runtime company reorg proposal."""
    config = _get_config()
    asyncio.run(_propose_reorg(config, payload, project))


@app.command("approve-reorg")
def approve_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Approve a pending company reorg proposal."""
    config = _get_config()
    asyncio.run(_approve_reorg(config, proposal_id, project, approved=True))


@app.command("deny-reorg")
def deny_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Deny a pending company reorg proposal."""
    config = _get_config()
    asyncio.run(_approve_reorg(config, proposal_id, project, approved=False))


@app.command("apply-reorg")
def apply_reorg(
    proposal_id: str = typer.Argument(..., help="Approved reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Apply an approved company reorg proposal."""
    config = _get_config()
    asyncio.run(_apply_reorg(config, proposal_id, project))


@app.command("show-reorg")
def show_reorg(
    proposal_id: str = typer.Argument(..., help="Reorg proposal ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Show a company reorg proposal."""
    config = _get_config()
    asyncio.run(_show_reorg(config, proposal_id, project))


def _template_has_required_config_files(source: Any) -> bool:
    try:
        return (
            source.is_dir()
            and source.joinpath("llm_config.yaml").is_file()
            and source.joinpath("system_config.yaml").is_file()
        )
    except Exception:
        return False


def _project_config_template_dir() -> Any | None:
    """Return the best config template source.

    Source checkouts use ``project_root/config``. Installed packages fall back
    to ``opc/config_templates`` shipped as package data via ``importlib.resources``.
    """
    from opc.core.config import get_opc_home

    opc_home = get_opc_home()
    project_root = opc_home.parent
    repo_config = project_root / "config"
    if _template_has_required_config_files(repo_config):
        return repo_config

    try:
        packaged_config = importlib_resources.files("opc").joinpath("config_templates")
    except Exception:
        packaged_config = None
    if packaged_config is not None and _template_has_required_config_files(packaged_config):
        return packaged_config
    return None


def _copy_template_tree(source: Any, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        dest = target / child.name
        if child.is_dir():
            _copy_template_tree(child, dest)
        elif child.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(child.read_bytes())


def _load_config_template(source: Any) -> OPCConfig:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        _copy_template_tree(source, tmp_path)
        return OPCConfig.load(tmp_path)


def _opc_config_initialized(opc_home: Path) -> bool:
    config_dir = opc_home / "config"
    if not config_dir.exists():
        return False
    expected = (
        "llm_config.yaml",
        "system_config.yaml",
        "agent_config.yaml",
        "org_config.yaml",
    )
    if any((config_dir / name).exists() for name in expected):
        return True
    return any(config_dir.iterdir())


def _trust_configured_external_agents(
    config: OPCConfig,
    opc_home: Path,
    *,
    project_id: str | None,
) -> None:
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager

    manager = ApprovalAllowlistManager(opc_home)
    manager.ensure_file()
    scope_project = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
    for agent_name, agent_config in config.agents.agents.items():
        if not getattr(agent_config, "enabled", True):
            continue
        manager.add_patterns(
            "external_agent",
            agent_name,
            ["*"],
            project_id=scope_project,
        )


def _run_init_external_agent_preflight(
    config: OPCConfig,
    *,
    project_id: str,
    opc_home: Path,
    workspace_path: Path,
) -> None:
    from opc.layer3_agent.preflight import run_external_agent_preflight

    results = run_external_agent_preflight(
        config,
        project_id=project_id,
        workspace_path=workspace_path,
        opc_home=opc_home,
        probe_commands=False,
        prepare_surfaces=True,
    )
    available = sum(1 for item in results if item.available)
    configured = sum(1 for item in results if item.enabled)
    failures = [item for item in results if item.enabled and item.issues]
    console.print(
        f"[info]External agent preflight:[/info] {available}/{configured} configured agents found; "
        f"collab surfaces and workspace permissions checked."
    )
    for item in failures:
        console.print(f"  [warning]- {item.agent}: {'; '.join(item.issues[:2])}[/warning]")


def _render_external_agent_detection(config: OPCConfig) -> None:
    import shutil

    from opc.layer3_agent.adapters.registry import ADAPTER_CLASSES

    for agent_name, agent_config in config.agents.agents.items():
        adapter_cls = ADAPTER_CLASSES.get(agent_name)
        if adapter_cls is not None:
            adapter = adapter_cls(config=agent_config)
            command = adapter.configured_command()
            found = adapter.resolve_binary()
        else:
            command = agent_config.command
            found = shutil.which(command)
        if found:
            console.print(f"  - {agent_name}: [success]found[/success] ({found})")
        else:
            console.print(f"  - {agent_name}: [warning]not found[/warning] (command={command})")


def _render_external_agent_preflight_table(results: list[Any]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Agent")
    table.add_column("Binary")
    table.add_column("Mode")
    table.add_column("Stdin")
    table.add_column("Workspace")
    table.add_column("Collab")
    table.add_column("Notes")
    for item in results:
        if not item.enabled:
            binary = "[warning]disabled[/warning]"
        elif item.available:
            binary = f"[success]found[/success]\n{item.binary}"
        else:
            binary = f"[error]missing[/error]\n{item.command}"
        mode = ""
        if item.launch_command:
            mode = _truncate_status_cell(item.launch_command, limit=180)
        elif item.version:
            mode = item.version
        workspace_ok = all(check.ok for check in item.write_checks)
        workspace = "[success]writable[/success]" if workspace_ok else "[error]blocked[/error]"
        collab = "[success]ready[/success]" if item.collab_cli and not item.issues else "[warning]check[/warning]"
        rpc_transport = str(getattr(item, "collaboration_rpc_transport", "") or "").strip()
        if rpc_transport:
            collab = f"{collab}\nRPC: {rpc_transport}"
        notes = []
        if item.issues:
            notes.extend(item.issues[:2])
        if item.warnings:
            notes.extend(item.warnings[:2])
        table.add_row(
            item.agent,
            binary,
            mode,
            str(getattr(item, "stdin_policy", "") or "-"),
            workspace,
            collab,
            _truncate_status_cell("; ".join(notes) if notes else "ok", limit=160),
        )
    console.print(table)


def _truncate_status_cell(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


@app.command()
def init(
    project: Optional[str] = typer.Argument(None, help="Project ID to initialize"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Continue when OPC is already initialized; existing config is preserved."),
    external_agent_preflight: bool = typer.Option(
        True,
        "--external-agent-preflight/--no-external-agent-preflight",
        help="Provision and validate external-agent homes, collab CLI, and workspace permissions.",
    ),
    trust_external_agents: bool = typer.Option(
        True,
        "--trust-external-agents/--no-trust-external-agents",
        help="Persist OpenOPC approval rules for configured external-agent launches.",
    ),
):
    """Initialize OPC configuration and workspace."""
    from opc.core.config import get_opc_home, get_project_workplace

    opc_home = get_opc_home()
    template_dir = _project_config_template_dir()
    already_initialized = _opc_config_initialized(opc_home)
    config: OPCConfig

    if already_initialized:
        if not yes and not typer.confirm(
            f"OPC is already initialized at {opc_home}. Continue without overwriting existing config?",
            default=False,
        ):
            console.print("[warning]Init cancelled. Existing config was left unchanged.[/warning]")
            raise typer.Exit(1)
        console.print(f"[info]Existing config preserved: {opc_home / 'config'}[/info]")
        config = OPCConfig.load(opc_home / "config")
    elif template_dir is not None:
        # Use repo config template (same setup as maintainers, keys left for user to set)
        config = _load_config_template(template_dir)
        config.llm.api_key = ""
        config.save(opc_home / "config")
    else:
        config = OPCConfig()
        if not config.org.roles:
            from opc.core.config import RoleConfig, EscalationRule
            browser_tools = [
                "browser_navigate",
                "browser_navigate_back",
                "browser_click",
                "browser_snapshot",
                "browser_type",
                "browser_wait_for",
                "browser_scroll",
                "browser_select_option",
                "browser_take_screenshot",
                "browser_close",
            ]
            planning_tools = [
                "file_read",
                "file_search",
                "list_dir",
                "todo_write",
                "todo_read",
            ]
            engineering_tools = [
                "shell_exec",
                "file_read",
                "file_write",
                "file_edit",
                "file_search",
                "list_dir",
                "web_search",
                "web_fetch",
                "todo_write",
                "todo_read",
                *browser_tools,
            ]
            review_tools = [
                "file_read",
                "file_search",
                "list_dir",
                "shell_exec",
                "browser_navigate",
                "browser_navigate_back",
                "browser_snapshot",
                "browser_wait_for",
                "browser_scroll",
                "browser_select_option",
                "browser_take_screenshot",
            ]
            config.org.roles = [
                RoleConfig(
                    id="ceo",
                    name="CEO",
                    icon="leader",
                    responsibility="Strategic intake, high-level routing, final aggregation and delivery to the owner",
                    reports_to="owner",
                    can_spawn=["cto", "cmo", "coo"],
                    tools=planning_tools,
                ),
                RoleConfig(
                    id="cto",
                    name="CTO",
                    icon="code",
                    responsibility="Technical planning, architecture decisions, code review, and engineering oversight",
                    reports_to="ceo",
                    can_spawn=["senior_engineer", "devops_engineer"],
                    tools=[*planning_tools, "shell_exec", "web_search", "web_fetch"],
                ),
                RoleConfig(
                    id="cmo",
                    name="CMO",
                    icon="marketing",
                    responsibility="Marketing strategy, content planning, UX review, and brand oversight",
                    reports_to="ceo",
                    can_spawn=["content_specialist", "designer"],
                    tools=[
                        *planning_tools,
                        "web_search",
                        "web_fetch",
                        "browser_navigate",
                        "browser_navigate_back",
                        "browser_snapshot",
                        "browser_wait_for",
                        "browser_scroll",
                        "browser_take_screenshot",
                    ],
                ),
                RoleConfig(
                    id="coo",
                    name="COO",
                    icon="strategy",
                    responsibility="Operations coordination, process management, cross-team alignment, and quality assurance",
                    reports_to="ceo",
                    can_spawn=["qa_analyst"],
                    tools=[
                        *planning_tools,
                        "shell_exec",
                        "browser_navigate",
                        "browser_navigate_back",
                        "browser_snapshot",
                        "browser_wait_for",
                        "browser_scroll",
                        "browser_take_screenshot",
                    ],
                ),
                RoleConfig(
                    id="senior_engineer",
                    name="Senior Engineer",
                    icon="terminal",
                    responsibility="Code implementation, system development, and technical execution",
                    reports_to="cto",
                    preferred_external_agent="codex",
                    tools=engineering_tools,
                ),
                RoleConfig(
                    id="devops_engineer",
                    name="DevOps Engineer",
                    icon="settings",
                    responsibility="Infrastructure, deployment, CI/CD, monitoring, and operational hardening",
                    reports_to="cto",
                    preferred_external_agent="cursor",
                    tools=engineering_tools,
                ),
                RoleConfig(
                    id="content_specialist",
                    name="Content Specialist",
                    icon="writing",
                    responsibility="Documentation, copywriting, presentations, and user-facing writing",
                    reports_to="cmo",
                    tools=[
                        "file_read",
                        "file_write",
                        "file_edit",
                        "file_search",
                        "list_dir",
                        "web_search",
                        "web_fetch",
                        "todo_write",
                        "todo_read",
                        *browser_tools,
                    ],
                ),
                RoleConfig(
                    id="designer",
                    name="Designer",
                    icon="design",
                    responsibility="Visual design, UX artifacts, wireframes, and design system work",
                    reports_to="cmo",
                    tools=[
                        "file_read",
                        "file_write",
                        "file_edit",
                        "file_search",
                        "list_dir",
                        "web_search",
                        "web_fetch",
                        "todo_write",
                        "todo_read",
                        *browser_tools,
                    ],
                ),
                RoleConfig(
                    id="qa_analyst",
                    name="QA Analyst",
                    icon="bug",
                    responsibility="Testing, security review, compliance checks, and acceptance validation",
                    reports_to="coo",
                    tools=review_tools,
                ),
            ]
            config.org.escalation_rules = [
                EscalationRule(condition="3 consecutive failures with no progress", action="Escalate to owner with failure reason"),
                EscalationRule(condition="External account or credentials required", action="Escalate to owner"),
                EscalationRule(condition="Security vulnerability severity >= HIGH", action="Immediately halt and escalate"),
                EscalationRule(condition="Budget exceeds 80% of limit", action="Alert owner and await instructions"),
            ]
        config.save(opc_home / "config")

    # Create default directories. ``agent_homes/`` and ``bin/`` get
    # provisioned lazily by the skill installer the first time an
    # external agent launches.
    for subdir in ["memory", "skills/core", "skills/cache", "skills/learned", "logs", "prompts/talent"]:
        (opc_home / subdir).mkdir(parents=True, exist_ok=True)

    # Create default skills
    _create_default_skills(opc_home)

    # Create default global memory.
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from opc.layer5_memory.markdown_memory import MarkdownMemoryStore
    memory_store = MarkdownMemoryStore(opc_home)
    if not memory_store.load_raw_text():
        memory_store.save_visible_text("# Global Memory", None)
    ApprovalAllowlistManager(opc_home).ensure_file()

    if project:
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project or ""):
            console.print("[error]Invalid project ID (use alphanumeric, hyphens, underscores).[/error]")
            raise typer.Exit(1)
        proj_dir = opc_home / "projects" / project
        project_memory = memory_store.memory_path(project)
        workplace = get_project_workplace(project)
        if proj_dir.exists() or project_memory.exists() or workplace.exists():
            console.print(f"[error]Project '{project}' already exists.[/error]")
            raise typer.Exit(1)
        proj_dir.mkdir(parents=True, exist_ok=False)
        workplace.mkdir(parents=True, exist_ok=False)
        memory_store.ensure_memory_file(project, f"# Project Memory ({project})")
        console.print(f"[success]Project '{project}' initialized at {proj_dir}[/success]")
        console.print(f"  Workplace: {workplace}")

    if trust_external_agents:
        _trust_configured_external_agents(config, opc_home, project_id=project)

    if external_agent_preflight:
        _run_init_external_agent_preflight(
            config,
            project_id=project or "default",
            opc_home=opc_home,
            workspace_path=get_project_workplace(project or "default"),
        )

    console.print(f"[success]OPC initialized at {opc_home}[/success]")
    console.print(f"  Config: {opc_home / 'config'}")
    console.print(f"  Memory: {opc_home / 'memory'}")
    console.print(f"  Skills: {opc_home / 'skills'}")
    console.print(f"\nEdit [bold]{opc_home / 'config' / 'llm_config.yaml'}[/bold] to set your API key.")


@app.command()
def status(
    project: str = typer.Option("default", "--project", "-p", help="Project ID used for workspace preflight."),
    external_agent_preflight: bool = typer.Option(
        True,
        "--external-agent-preflight/--no-external-agent-preflight",
        help="Run external-agent command, flag, collab, and workspace checks.",
    ),
    probe_agent_commands: bool = typer.Option(
        True,
        "--probe-agent-commands/--no-probe-agent-commands",
        help="Run short --version/--help probes to detect unsupported CLI flags.",
    ),
):
    """Show OPC system status."""
    from opc.core.config import get_opc_home

    config = _get_config()
    opc_home = get_opc_home()

    console.print(Panel(f"[bold]OPC v{__version__}[/bold]", border_style="blue"))
    console.print(f"  Home: {opc_home}")
    console.print(f"  Model: {config.llm.default_model}")
    console.print(f"  Log level: {config.system.log_level}")
    console.print(f"  Max iterations: {config.system.max_agent_iterations}")
    console.print(f"  Autonomy mode: {config.autonomy.mode}")
    console.print(f"  Max auto-approve risk: {config.autonomy.max_auto_approve_risk}")

    # Roles
    console.print("\n[bold]Organization:[/bold]")
    console.print(f"  Company: {config.org.company_name}")
    for role in config.org.roles:
        console.print(f"  - {role.name} ({role.id}): {role.responsibility[:60]}...")

    # External agents
    console.print("\n[bold]External Agents:[/bold]")
    for name, agent_config in config.agents.agents.items():
        status_str = "[success]enabled[/success]" if agent_config.enabled else "[warning]disabled[/warning]"
        console.print(f"  - {name}: {status_str}")

    if not config.agents.agents:
        console.print("  (none configured)")

    console.print("\n[bold]External Agent Preflight:[/bold]")
    if external_agent_preflight:
        from opc.layer3_agent.preflight import run_external_agent_preflight

        results = run_external_agent_preflight(
            config,
            project_id=project,
            probe_commands=probe_agent_commands,
            prepare_surfaces=True,
        )
        _render_external_agent_preflight_table(results)
    else:
        _render_external_agent_detection(config)

    # Projects
    projects_dir = opc_home / "projects"
    if projects_dir.exists():
        projects = [p.name for p in projects_dir.iterdir() if p.is_dir()]
        console.print(f"\n[bold]Projects:[/bold] {', '.join(projects) if projects else '(none)'}")

    # Cost
    asyncio.run(_show_cost_summary(config))
    asyncio.run(_show_autonomy_summary(config))


@app.command()
def autonomy_status(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Show autonomy policy, learned preferences, and approval stats."""
    config = _get_config()
    console.print(Panel("[bold]Autonomy Status[/bold]", border_style="blue"))
    console.print(f"  Mode: {config.autonomy.mode}")
    console.print(f"  Enabled: {config.autonomy.enabled}")
    console.print(f"  Max auto-approve risk: {config.autonomy.max_auto_approve_risk}")
    console.print(f"  Confidence threshold: {config.autonomy.approval_confidence_threshold}")
    asyncio.run(_show_autonomy_summary(config, project=project))


@app.command()
def autonomy_reset(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
):
    """Reset learned autonomy preferences globally or for a project."""
    from opc.core.config import get_opc_home
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from opc.layer5_memory.preference import PreferenceManager

    opc_home = get_opc_home()
    prefs = PreferenceManager(opc_home)
    prefs.reset_autonomy_preferences(project_id=project)
    ApprovalAllowlistManager(opc_home).reset(project_id=project)
    scope = f"project '{project}'" if project else "global"
    console.print(f"[success]Reset learned autonomy preferences and allowlist rules for {scope}.[/success]")


@app.command()
def autonomy_configure(
    mode: Optional[str] = typer.Option(None, "--mode", help="Autonomy mode, e.g. bounded"),
    max_auto_approve_risk: Optional[str] = typer.Option(None, "--max-risk", help="Max auto-approve risk"),
    approval_confidence_threshold: Optional[float] = typer.Option(None, "--confidence-threshold", help="Approval confidence threshold"),
):
    """Update autonomy configuration settings."""
    from opc.core.config import get_opc_home

    config = _get_config()
    if mode is not None:
        config.autonomy.mode = mode
    if max_auto_approve_risk is not None:
        config.autonomy.max_auto_approve_risk = max_auto_approve_risk
    if approval_confidence_threshold is not None:
        config.autonomy.approval_confidence_threshold = approval_confidence_threshold
    config.save(get_opc_home() / "config")
    console.print("[success]Updated autonomy configuration.[/success]")


@app.command()
def projects():
    """List all projects."""
    from opc.core.config import get_opc_home, get_project_workplace
    from opc.layer5_memory.markdown_memory import MarkdownMemoryStore
    opc_home = get_opc_home()
    memory_store = MarkdownMemoryStore(opc_home)
    projects_dir = opc_home / "projects"

    if not projects_dir.exists() or not any(projects_dir.iterdir()):
        console.print("[warning]No projects found.[/warning]")
        console.print("Create one with: opc init <project-name>")
        return

    for p in sorted(projects_dir.iterdir()):
        if p.is_dir():
            has_tasks = (p / "tasks.db").exists()
            has_memory = memory_store.memory_path(p.name).exists()
            has_workplace = get_project_workplace(p.name).exists()
            markers = []
            if has_tasks:
                markers.append("tasks")
            if has_memory:
                markers.append("memory")
            if has_workplace:
                markers.append("workplace")
            info = f" ({', '.join(markers)})" if markers else ""
            console.print(f"  - [bold]{p.name}[/bold]{info}")


@app.command()
def skills():
    """List available skills."""
    from opc.core.config import get_opc_home
    from opc.layer5_memory.skill_library import SkillLibrary

    opc_home = get_opc_home()
    lib = SkillLibrary(opc_home)
    lib.load_all()

    skill_list = lib.list_skills()
    if not skill_list:
        console.print("[warning]No skills found.[/warning]")
        console.print(f"Add skills to {opc_home / 'skills'}")
        return

    console.print(f"[bold]Skills ({len(skill_list)}):[/bold]")
    for s in skill_list:
        always = " [always]" if s.always else ""
        level = f" [{s.level}]" if s.level else ""
        console.print(f"  - [bold]{s.name}[/bold]{always}{level}")
        if s.description:
            console.print(f"    {s.description}")


@app.command()
def config_show():
    """Show current configuration."""
    import yaml
    config = _get_config()
    console.print(yaml.dump(config.model_dump(), default_flow_style=False))


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _emit_payload(payload: dict[str, Any], *, json_output: bool = False) -> None:
    if json_output:
        console.print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe))
        return
    if not payload:
        console.print("[success]OK[/success]")
        return
    table = Table(show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in payload.items():
        if isinstance(value, (list, dict)):
            rendered = json.dumps(value, ensure_ascii=False, default=_json_safe)
        else:
            rendered = str(_json_safe(value))
        table.add_row(str(key), _clip_text(rendered, 400))
    console.print(table)


async def _run_service_command(
    project: str | None,
    operation: Any,
    *,
    json_output: bool = False,
    render: Any | None = None,
) -> None:
    from opc.plugins.office_ui.services import ServiceError
    from opc.plugins.office_ui.services.factory import OfficeServiceFactory

    async with OfficeServiceFactory(config=_get_config(), project_id=project) as services:
        try:
            result = await operation(services)
        except ServiceError as exc:
            if json_output:
                console.print(json.dumps({"ok": False, **exc.to_payload()}, ensure_ascii=False, indent=2))
            else:
                console.print(f"[error]{escape(exc.message)}[/error]")
            raise typer.Exit(code=1) from exc
        payload = {"ok": True, **dict(result.payload)}
        if render is not None and not json_output:
            render(payload)
        else:
            _emit_payload(payload, json_output=json_output)


def _load_structured_payload(*, payload: str | None = None, file_path: str | None = None) -> Any:
    raw = ""
    if file_path:
        raw = Path(file_path).read_text(encoding="utf-8")
    elif payload:
        raw = payload
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import yaml

        return yaml.safe_load(raw) or {}


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _exec_event_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return _json_safe(value)


def _print_exec_event(
    event_state: dict[str, Any],
    event_type: str,
    *,
    project_id: str,
    task_id: str = "",
    session_id: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    event_state["seq"] = int(event_state.get("seq", 0) or 0) + 1
    row = {
        "type": event_type,
        "seq": event_state["seq"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "task_id": task_id,
        "session_id": session_id,
        "payload": payload or {},
    }
    sys.stdout.write(json.dumps(row, ensure_ascii=False, default=_json_safe) + "\n")
    sys.stdout.flush()


def _exec_title(prompt: str) -> str:
    for line in str(prompt or "").splitlines():
        title = line.strip()
        if title:
            return title[:120]
    return "Exec Session"


async def _task_id_for_session(services: Any, *, project_id: str, session_id: str) -> str:
    engine = await services.context.engine_for_project(project_id)
    store = getattr(engine, "store", None)
    if not store or not session_id:
        return ""
    tasks = await store.get_tasks(project_id=project_id) if hasattr(store, "get_tasks") else []
    for task in tasks:
        if str(getattr(task, "session_id", "") or "") == session_id:
            return str(getattr(task, "id", "") or "")
    return ""


async def _resolve_exec_session(
    services: Any,
    *,
    project_id: str,
    prompt: str,
    mode: str,
    company_profile: str,
    preferred_agent: str | None,
    org_id: str | None,
    session_id: str | None,
    resume: bool,
) -> dict[str, Any]:
    from opc.plugins.office_ui.services import ServiceError

    normalized_mode = str(mode or "task").strip().lower()
    if normalized_mode == "custom":
        normalized_mode = "org"
    if normalized_mode not in {"task", "company", "org"}:
        raise ServiceError("invalid_mode", "mode must be task, company, or org", {"mode": mode})

    if session_id:
        task_id = await _task_id_for_session(services, project_id=project_id, session_id=session_id)
        if not task_id:
            raise ServiceError("session_not_task_backed", "Session is not linked to a task-backed CLI/UI session", {"session_id": session_id})
        detail = await services.session.detail(project_id=project_id, task_id=task_id, session_id=session_id, limit=1)
        return {**detail.payload, "task_id": task_id, "session_id": session_id, "restored": True}

    if resume:
        starting = await services.session.resolve_starting_session(project_id=project_id)
        payload = dict(starting.payload)
        restored_session_id = str(payload.get("session_id", "") or "")
        restored_task_id = str(payload.get("task_id", "") or "")
        if restored_session_id and not restored_task_id:
            restored_task_id = await _task_id_for_session(services, project_id=project_id, session_id=restored_session_id)
        if not restored_task_id:
            raise ServiceError("session_not_task_backed", "Latest session is not task-backed; create a new exec session without --resume", {"session_id": restored_session_id})
        payload["task_id"] = restored_task_id
        payload["session_id"] = restored_session_id
        payload["restored"] = True
        return payload

    created = await services.session.create(
        project_id=project_id,
        title=_exec_title(prompt),
        exec_mode=normalized_mode,
        company_profile="custom" if normalized_mode == "org" else company_profile,
        preferred_agent=preferred_agent,
        org_id=org_id,
        interface="cli_exec",
    )
    return dict(created.payload)


async def _exec_message(
    *,
    config: OPCConfig,
    prompt: str,
    project: str | None,
    mode: str,
    company_profile: str,
    preferred_agent: str | None,
    org_id: str | None,
    session_id: str | None,
    resume: bool,
    json_output: bool,
    stream_json: bool,
    no_markdown: bool,
) -> None:
    from opc.plugins.office_ui.services import ServiceError
    from opc.plugins.office_ui.services.factory import OfficeServiceFactory

    project_id = str(project or "default").strip() or "default"
    event_state: dict[str, Any] = {"seq": 0, "task_id": "", "session_id": ""}

    async def on_progress(*args: Any, **kwargs: Any) -> None:
        if not stream_json:
            return
        _print_exec_event(
            event_state,
            "runtime_update",
            project_id=project_id,
            task_id=str(event_state.get("task_id", "") or ""),
            session_id=str(event_state.get("session_id", "") or ""),
            payload={"args": [_exec_event_payload(arg) for arg in args], "kwargs": kwargs},
        )

    async def on_runtime_event(event: Any) -> None:
        if not stream_json:
            return
        _print_exec_event(
            event_state,
            "runtime_update",
            project_id=project_id,
            task_id=str(event_state.get("task_id", "") or ""),
            session_id=str(event_state.get("session_id", "") or ""),
            payload=_exec_event_payload(event),
        )

    try:
        async with OfficeServiceFactory(
            config=config,
            project_id=project_id,
            on_progress=on_progress,
            on_runtime_event=on_runtime_event,
            on_escalation=_escalation_callback(),
        ) as services:
            target = await _resolve_exec_session(
                services,
                project_id=project_id,
                prompt=prompt,
                mode=mode,
                company_profile=company_profile,
                preferred_agent=preferred_agent,
                org_id=org_id,
                session_id=session_id,
                resume=resume,
            )
            task_id = str(target.get("task_id", "") or "")
            resolved_session_id = str(target.get("session_id", "") or "")
            event_state["task_id"] = task_id
            event_state["session_id"] = resolved_session_id
            if stream_json:
                _print_exec_event(
                    event_state,
                    "session_created" if not target.get("restored") else "session_resumed",
                    project_id=project_id,
                    task_id=task_id,
                    session_id=resolved_session_id,
                    payload=target,
                )

            response = ""
            if prompt:
                normalized_mode = str(mode or "task").strip().lower()
                send_mode = "company" if normalized_mode in {"company", "org", "custom"} else "task"
                send_profile = "custom" if normalized_mode in {"org", "custom"} else company_profile
                sent = await services.session.send(
                    project_id=project_id,
                    task_id=task_id,
                    content=prompt,
                    mode=send_mode,
                    company_profile=send_profile,
                    preferred_agent=preferred_agent,
                )
                response = str(sent.payload.get("response", "") or "")
                if stream_json:
                    _print_exec_event(
                        event_state,
                        "message",
                        project_id=project_id,
                        task_id=task_id,
                        session_id=resolved_session_id,
                        payload={"role": "assistant", "content": response},
                    )

            final_payload = {
                "ok": True,
                "project_id": project_id,
                "task_id": task_id,
                "session_id": resolved_session_id,
                "mode": mode,
                "company_profile": company_profile,
                "response": response,
            }
            if stream_json:
                _print_exec_event(
                    event_state,
                    "final",
                    project_id=project_id,
                    task_id=task_id,
                    session_id=resolved_session_id,
                    payload=final_payload,
                )
                return
            if json_output:
                console.print(json.dumps(final_payload, ensure_ascii=False, indent=2, default=_json_safe))
                return
            if response:
                _print_response(response, no_markdown=no_markdown)
            else:
                _emit_payload(final_payload)
    except ServiceError as exc:
        payload = {"ok": False, **exc.to_payload()}
        if stream_json:
            _print_exec_event(event_state, "error", project_id=project_id, payload=payload)
        elif json_output:
            console.print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe))
        else:
            console.print(f"[error]{escape(exc.message)}[/error]")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        payload = {"ok": False, "code": "invalid_argument", "error": str(exc)}
        if stream_json:
            _print_exec_event(event_state, "error", project_id=project_id, payload=payload)
        elif json_output:
            console.print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe))
        else:
            console.print(f"[error]{escape(str(exc))}[/error]")
        raise typer.Exit(code=2) from exc


project_app = typer.Typer(help="Manage OPC projects")
app.add_typer(project_app, name="project")


@project_app.command("list")
def project_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List projects."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.list(active_project_id=project), json_output=json_output))


@project_app.command("show")
def project_show(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Show project index payload."""
    target = project or "default"
    asyncio.run(_run_service_command(project, lambda svc: svc.project.project_index(target, include_snapshot=False), json_output=json_output))


@project_app.command("create")
def project_create(
    project_id: str = typer.Argument(..., help="Project ID to create"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Create a project."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.create(project_id, active_project_id=project), json_output=json_output))


@project_app.command("switch")
def project_switch(
    project_id: str = typer.Argument(..., help="Project ID to prepare/switch"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Current project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Prepare and validate switching to a project."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.switch(project_id, include_snapshot=False), json_output=json_output))


@project_app.command("rename")
def project_rename(
    old_project_id: str = typer.Argument(..., help="Existing project ID"),
    new_project_id: str = typer.Argument(..., help="New project ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Rename a project id and move its persisted project data."""
    asyncio.run(_run_service_command(project, lambda svc: svc.project.rename(old_project_id, new_project_id), json_output=json_output))


@project_app.command("delete")
def project_delete(
    project_id: str = typer.Argument(..., help="Project ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deletion"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Active project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Delete a project and its persisted UI/runtime data."""
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.project.delete(project_id), json_output=json_output))


session_app = typer.Typer(help="Manage OPC sessions")
app.add_typer(session_app, name="session")


@session_app.command("list")
def session_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.list(project_id=project or "default", limit=limit), json_output=json_output))


@session_app.command("create")
def session_create(
    title: str = typer.Argument("New Chat", help="Session title"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: str = typer.Option("task", "--mode", help="task, company, or org"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="corporate or custom"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred task-mode agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.create(project_id=project or "default", title=title, exec_mode=mode, company_profile=company_profile, preferred_agent=agent, org_id=org_id, interface="cli"), json_output=json_output))


@session_app.command("show")
def session_show(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    limit: int = typer.Option(200, "--limit", "-n", help="Transcript limit"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.detail(project_id=project or "default", task_id=target, session_id=target, limit=limit), json_output=json_output))


@session_app.command("config")
def session_config(
    task_id: str = typer.Argument(..., help="Task ID"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: Optional[str] = typer.Option(None, "--mode", help="task, company, or org"),
    company_profile: Optional[str] = typer.Option(None, "--company-profile", help="corporate or custom"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred task-mode agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id", help="Saved org id"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.update_config(project_id=project or "default", task_id=task_id, exec_mode=mode, company_profile=company_profile, preferred_agent=agent, org_id=org_id), json_output=json_output))


@session_app.command("send")
def session_send(
    task_id: str = typer.Argument(..., help="Task ID"),
    message: str = typer.Argument(..., help="Message to send"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID"),
    mode: str = typer.Option("task", "--mode", help="task or company"),
    company_profile: str = typer.Option("corporate", "--company-profile", help="Company profile"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred agent"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.send(project_id=project or "default", task_id=task_id, content=message, mode=mode, company_profile=company_profile, preferred_agent=agent), json_output=json_output))


@session_app.command("rename")
def session_rename(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    title: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.rename(project_id=project or "default", task_id=target, session_id=target, title=title), json_output=json_output))


@session_app.command("delete")
def session_delete(
    task_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.session.delete(project_id=project or "default", task_id=task_id), json_output=json_output))


@session_app.command("stop")
def session_stop(target: str = typer.Argument(..., help="Task ID or session ID"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.stop(project_id=project or "default", target=target), json_output=json_output))


@session_app.command("continue")
def session_continue(
    target: str = typer.Argument(..., help="Task ID or session ID"),
    message: Optional[str] = typer.Argument(None, help="Optional resume instruction"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.continue_run(project_id=project or "default", target=target, content=message or ""), json_output=json_output))


@session_app.command("resume")
def session_resume(target: str = typer.Argument(..., help="Task ID or session ID"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.resume(project_id=project or "default", target=target), json_output=json_output))


@session_app.command("complete")
def session_complete(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.session.complete(project_id=project or "default", task_id=task_id), json_output=json_output))


mode_app = typer.Typer(help="Manage default execution mode")
app.add_typer(mode_app, name="mode")


@mode_app.command("show")
def mode_show(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.mode_show(), json_output=json_output))


@mode_app.command("set")
def mode_set(
    mode: str = typer.Argument(..., help="task, company, or org"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    profile: str = typer.Option("corporate", "--profile", "--company-profile"),
    agent: Optional[str] = typer.Option(None, "--agent"),
    org_id: Optional[str] = typer.Option(None, "--org", "--org-id"),
    json_output: bool = typer.Option(False, "--json"),
):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.mode_set(mode=mode, profile=profile, preferred_agent=agent, org_id=org_id), json_output=json_output))


kanban_app = typer.Typer(help="Manage kanban views and tasks")
kanban_task_app = typer.Typer(help="Manage kanban tasks")
kanban_app.add_typer(kanban_task_app, name="task")
app.add_typer(kanban_app, name="kanban")


@kanban_task_app.command("create")
def kanban_task_create(title: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), description: str = typer.Option("", "--description", "-d"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.create_task(project_id=project or "default", title=title, description=description), json_output=json_output))


@kanban_task_app.command("update")
def kanban_task_update(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), title: Optional[str] = typer.Option(None, "--title"), description: Optional[str] = typer.Option(None, "--description"), json_output: bool = typer.Option(False, "--json")):
    updates = {k: v for k, v in {"title": title, "description": description}.items() if v is not None}
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.update_task(project_id=project or "default", task_id=task_id, updates=updates), json_output=json_output))


@kanban_task_app.command("move")
def kanban_task_move(task_id: str = typer.Argument(...), column: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.move_task(project_id=project or "default", task_id=task_id, column_id=column), json_output=json_output))


@kanban_task_app.command("delete")
def kanban_task_delete(task_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.delete_task(project_id=project or "default", task_id=task_id), json_output=json_output))


@kanban_task_app.command("assign")
def kanban_task_assign(task_id: str = typer.Argument(...), agent_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.assign(project_id=project or "default", task_id=task_id, agent_id=agent_id), json_output=json_output))


@kanban_task_app.command("status")
def kanban_task_status(task_id: str = typer.Argument(...), status: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.kanban.status(project_id=project or "default", task_id=task_id, status=status), json_output=json_output))


@kanban_app.command("view")
def kanban_view(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.project.project_index(project or "default", include_snapshot=False), json_output=json_output))


agent_app = typer.Typer(help="Manage UI agents")
app.add_typer(agent_app, name="agent")


@agent_app.command("list")
def agent_list(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.list(), json_output=json_output))


@agent_app.command("create")
def agent_create(name: str = typer.Argument(...), role_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), description: str = typer.Option("", "--description"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.create(name=name, role_id=role_id, office_id=office_id, description=description), json_output=json_output))


@agent_app.command("create-from-template")
def agent_create_from_template(template_id: str = typer.Argument(...), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.create_from_template(template_id=template_id, role_id=role_id or template_id, office_id=office_id), json_output=json_output))


@agent_app.command("import-employee")
def agent_import_employee(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.import_employee(employee_id=employee_id, office_id=office_id), json_output=json_output))


@agent_app.command("detail")
def agent_detail(agent_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.detail(project_id=project or "default", agent_id=agent_id), json_output=json_output))


@agent_app.command("delete")
def agent_delete(agent_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.delete(agent_id), json_output=json_output))


@agent_app.command("move")
def agent_move(agent_id: str = typer.Argument(...), office_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), seat_zone: Optional[str] = typer.Option(None, "--seat-zone"), desk_id: Optional[str] = typer.Option(None, "--desk"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.agent.move(agent_id=agent_id, office_id=office_id, seat_zone=seat_zone, desk_id=desk_id), json_output=json_output))


org_app = typer.Typer(help="Manage organization configuration")
app.add_typer(org_app, name="org")


@org_app.command("info")
def org_info(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.info(), json_output=json_output))


@org_app.command("export")
def org_export(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(True, "--json/--no-json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.export_config(), json_output=json_output))


@org_app.command("import")
def org_import(
    path: str = typer.Argument(..., help="YAML or JSON org config payload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without applying"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    json_output: bool = typer.Option(False, "--json"),
):
    raw = Path(path).read_text(encoding="utf-8")
    asyncio.run(_run_service_command(project, lambda svc: svc.org.import_config(raw, dry_run=dry_run), json_output=json_output))


org_saved_app = typer.Typer(help="Manage saved org architectures")
org_app.add_typer(org_saved_app, name="saved")


@org_saved_app.command("list")
def org_saved_list(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_list(), json_output=json_output))


@org_saved_app.command("save")
def org_saved_save(name: str = typer.Argument(...), overwrite: bool = typer.Option(False, "--overwrite"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_save_as(name, overwrite=overwrite), json_output=json_output))


@org_saved_app.command("load")
def org_saved_load(name: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_load(name), json_output=json_output))


@org_saved_app.command("delete")
def org_saved_delete(name: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.saved_delete(name), json_output=json_output))


org_role_app = typer.Typer(help="Manage organization roles")
org_app.add_typer(org_role_app, name="role")


@org_role_app.command("add")
def org_role_add(role_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), name: Optional[str] = typer.Option(None, "--name"), responsibility: str = typer.Option("", "--responsibility"), reports_to: str = typer.Option("owner", "--reports-to"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.add_role({"role_id": role_id, "name": name or role_id, "responsibility": responsibility, "reports_to": reports_to}), json_output=json_output))


@org_role_app.command("update")
def org_role_update(
    role_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    name: Optional[str] = typer.Option(None, "--name"),
    responsibility: Optional[str] = typer.Option(None, "--responsibility"),
    reports_to: Optional[str] = typer.Option(None, "--reports-to"),
    can_spawn: Optional[str] = typer.Option(None, "--can-spawn", help="Comma-separated role ids"),
    tools: Optional[str] = typer.Option(None, "--tools", help="Comma-separated tool names"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Preferred external agent"),
    json_output: bool = typer.Option(False, "--json"),
):
    updates = {
        key: value
        for key, value in {
            "name": name,
            "responsibility": responsibility,
            "reports_to": reports_to,
            "preferred_external_agent": agent,
        }.items()
        if value is not None
    }
    if can_spawn is not None:
        updates["can_spawn"] = _split_csv(can_spawn)
    if tools is not None:
        updates["tools"] = _split_csv(tools)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_role(role_id, updates), json_output=json_output))


@org_role_app.command("bulk-add")
def org_role_bulk_add(path: str = typer.Argument(..., help="JSON/YAML list of role objects or object with roles"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    loaded = _load_structured_payload(file_path=path)
    roles = loaded.get("roles", []) if isinstance(loaded, dict) else loaded if isinstance(loaded, list) else []
    asyncio.run(_run_service_command(project, lambda svc: svc.org.bulk_add_roles(list(roles or [])), json_output=json_output))


@org_role_app.command("delete")
def org_role_delete(role_id: str = typer.Argument(...), yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.delete_role(role_id), json_output=json_output))


org_policy_app = typer.Typer(help="Manage organization runtime policy")
org_app.add_typer(org_policy_app, name="policy")


@org_policy_app.command("update")
def org_policy_update(payload: Optional[str] = typer.Option(None, "--payload", help="JSON/YAML object"), file_path: Optional[str] = typer.Option(None, "--file", "-f"), profile: str = typer.Option("custom", "--profile"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    data = _load_structured_payload(payload=payload, file_path=file_path)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_runtime_policy(data, profile=profile), json_output=json_output))


org_strategy_app = typer.Typer(help="Manage organization strategy")
org_app.add_typer(org_strategy_app, name="strategy")


@org_strategy_app.command("update")
def org_strategy_update(final_decider_role_id: Optional[str] = typer.Option(None, "--final-decider", "--final-decider-role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.org.update_org_strategy(final_decider_role_id=final_decider_role_id), json_output=json_output))


@org_app.command("reset")
def org_reset(yes: bool = typer.Option(False, "--yes", "-y"), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.org.reset_architecture(), json_output=json_output))


runtime_app = typer.Typer(help="Inspect and control runtime state")
app.add_typer(runtime_app, name="runtime")


@runtime_app.command("status")
def runtime_status(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.status(project_id=project or "default"), json_output=json_output))


@runtime_app.command("checkpoints")
def runtime_checkpoints(project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(50, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.checkpoints(project_id=project or "default", limit=limit), json_output=json_output))


@runtime_app.command("logs")
def runtime_logs(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.logs(project_id=project or "default", task_id=task_id, limit=limit), json_output=json_output))


@runtime_app.command("run")
def runtime_run(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.runtime.run_task(project_id=project or "default", task_id=task_id), json_output=json_output))


comms_app = typer.Typer(help="Inspect company-mode comms")
app.add_typer(comms_app, name="comms")


@comms_app.command("state")
def comms_state(task_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.comms.state(project_id=project or "default", task_id=task_id), json_output=json_output))


@comms_app.command("read")
def comms_read(task_id: str = typer.Argument(...), path: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.comms.read(project_id=project or "default", task_id=task_id, path=path), json_output=json_output))


work_item_app = typer.Typer(help="Inspect company-mode work items")
app.add_typer(work_item_app, name="work-item")


@work_item_app.command("list")
def work_item_list(project: Optional[str] = typer.Option(None, "--project", "-p"), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), status: Optional[str] = typer.Option(None, "--status"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.list(project_id=project or "default", role_id=role_id, status=status, limit=limit), json_output=json_output))


@work_item_app.command("show")
def work_item_show(work_item_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.show(project_id=project or "default", work_item_id=work_item_id, limit=limit), json_output=json_output))


@work_item_app.command("logs")
def work_item_logs(work_item_id: str = typer.Argument(""), role_id: Optional[str] = typer.Option(None, "--role", "--role-id"), project: Optional[str] = typer.Option(None, "--project", "-p"), limit: int = typer.Option(100, "--limit", "-n"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.logs(project_id=project or "default", work_item_id=work_item_id, role_id=role_id or "", limit=limit), json_output=json_output))


@work_item_app.command("role-status")
def work_item_role_status(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.work_item.status_by_role(project_id=project or "default"), json_output=json_output))


channels_app = typer.Typer(help="Manage external messaging channels")
app.add_typer(channels_app, name="channels")

talent_app = typer.Typer(help="Manage recruitable talent templates and employees")
app.add_typer(talent_app, name="talent")


@talent_app.command("list")
def talent_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List imported talent templates."""
    config = _get_config()
    market = TalentMarket(get_opc_home(), config)
    templates = market.list_templates()
    if json_output:
        _emit_payload({"templates": templates}, json_output=True)
        return
    if not templates:
        console.print("[warning]No imported talent templates found.[/warning]")
        return
    console.print(f"[bold]Talent Templates ({len(templates)}):[/bold]")
    for template in templates:
        domains = ", ".join(template.domains[:4]) if template.domains else "general"
        console.print(
            f"  - [bold]{template.id}[/bold] :: {template.name} "
            f"[{template.category}] domains={domains}"
        )


@talent_app.command("employees")
def talent_employees(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List hired employees."""
    config = _get_config()
    market = TalentMarket(get_opc_home(), config)
    employees = market.list_employees()
    if json_output:
        _emit_payload({"employees": employees}, json_output=True)
        return
    if not employees:
        console.print("[warning]No employees hired yet.[/warning]")
        return
    console.print(f"[bold]Employees ({len(employees)}):[/bold]")
    for employee in employees:
        domains = ", ".join(employee.domains[:4]) if employee.domains else "general"
        console.print(
            f"  - [bold]{employee.employee_id}[/bold] :: {employee.name} "
            f"role={employee.role_id} domains={domains}"
        )


@talent_app.command("import")
def talent_import(
    repo_path: str = typer.Argument(..., help="Local path to the agency-agents repository"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Import local agency-agent markdown files into recruitable talent templates."""
    config = _get_config()
    market = TalentMarket(get_opc_home(), config)
    imported = market.import_from_repo(_talent_repo_path(repo_path))
    config.save(get_opc_home() / "config")
    if json_output:
        _emit_payload({"count": len(imported), "imported": imported}, json_output=True)
        return
    console.print(f"[success]Imported {len(imported)} talent templates.[/success]")


@talent_app.command("hire")
def talent_hire(
    template_id: str = typer.Argument(..., help="Imported talent template id"),
    role_id: str = typer.Argument(..., help="Company role id to staff"),
    employee_name: Optional[str] = typer.Option(None, "--name", help="Optional hired employee display name"),
    employee_id: Optional[str] = typer.Option(None, "--employee-id", help="Optional employee id override"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Hire an imported template into a company role."""
    config = _get_config()
    market = TalentMarket(get_opc_home(), config)
    employee = market.hire_template(
        template_id,
        role_id,
        employee_name=employee_name,
        employee_id=employee_id,
    )
    config.save(get_opc_home() / "config")
    if json_output:
        _emit_payload({"employee": employee}, json_output=True)
        return
    console.print(
        f"[success]Hired {employee.name} into role `{employee.role_id}` "
        f"as `{employee.employee_id}`.[/success]"
    )


@talent_app.command("scan")
def talent_scan(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Scan local talent templates."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.scan(), json_output=json_output))


@talent_app.command("import-selected")
def talent_import_selected(template_ids: list[str] = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Import selected local talent templates."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_selected(template_ids), json_output=json_output))


@talent_app.command("employee-detail")
def talent_employee(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Show employee detail."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.employee_detail(employee_id), json_output=json_output))


@talent_app.command("import-agent")
def talent_import_agent(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Import an employee as a visual office agent."""
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_employee_as_agent(employee_id=employee_id), json_output=json_output))


talent_employee_app = typer.Typer(help="Manage hired employees")
talent_app.add_typer(talent_employee_app, name="employee")


@talent_employee_app.command("detail")
def talent_employee_detail(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.employee_detail(employee_id), json_output=json_output))


@talent_employee_app.command("import-agent")
def talent_employee_import_agent(employee_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), office_id: str = typer.Option("office-0", "--office"), json_output: bool = typer.Option(False, "--json")):
    asyncio.run(_run_service_command(project, lambda svc: svc.talent.import_employee_as_agent(employee_id=employee_id, office_id=office_id), json_output=json_output))


# ---------------------------------------------------------------------------
# Market subcommands
# ---------------------------------------------------------------------------
market_app = typer.Typer(help="OPC Market — export, import, and manage architecture packages")
app.add_typer(market_app, name="market")


def _render_architecture_presets(presets: list[Any]) -> None:
    if not presets:
        console.print("[warning]No architecture presets available.[/warning]")
        return
    table = Table(title=f"Architecture Presets ({len(presets)})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Roles", justify="right")
    table.add_column("Templates", justify="right")
    table.add_column("Pattern")
    for preset in presets:
        table.add_row(
            str(getattr(preset, "id", "") or ""),
            str(getattr(preset, "name", "") or ""),
            str(getattr(preset, "category", "") or ""),
            str(len(getattr(preset, "roles", []) or [])),
            str(len(getattr(preset, "work_item_templates", []) or [])),
            str(getattr(preset, "collaboration_pattern", "") or ""),
        )
    console.print(table)


@market_app.command("export")
def market_export(
    package_id: str = typer.Option(..., "--id", help="Package identifier (slug)"),
    name: str = typer.Option(..., "--name", help="Display name"),
    description: str = typer.Option("", "--desc", help="Package description"),
    version: str = typer.Option("1.0.0", "--version", help="Semantic version"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Output directory"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Export the current org as an .opcpkg package."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.export(package_id=package_id, name=name, description=description, version=version, output_dir=output_dir), json_output=json_output))


@market_app.command("browse")
def market_browse(project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Browse market architecture presets."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.browse(), json_output=json_output))


@market_app.command("preview")
def market_preview(preset_id: str = typer.Argument(...), project: Optional[str] = typer.Option(None, "--project", "-p"), json_output: bool = typer.Option(False, "--json")):
    """Preview a built-in architecture preset."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.preview(preset_id), json_output=json_output))


@market_app.command("presets")
def market_presets(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List built-in architecture presets."""
    from opc.market.architecture_registry import get_all_presets

    if json_output:
        _emit_payload({"presets": get_all_presets()}, json_output=True)
        return
    _render_architecture_presets(get_all_presets())


@market_app.command("apply-preset")
def market_apply_preset(
    preset_id: str = typer.Argument(..., help="Built-in architecture preset id"),
    strategy: str = typer.Option("overwrite", "--strategy", "-s", help="Role id strategy: namespace or overwrite"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Apply a built-in architecture preset as the active custom organization."""
    if strategy not in {"namespace", "overwrite"}:
        console.print("[error]Strategy must be namespace or overwrite.[/error]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.market.apply_preset(preset_id=preset_id, strategy=strategy), json_output=json_output))


@market_app.command("install")
def market_install(
    path: str = typer.Argument(..., help="Path to .opcpkg directory"),
    strategy: str = typer.Option("namespace", "--strategy", "-s", help="Conflict strategy: namespace or overwrite"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Install an .opcpkg package from a local path."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.install(path=path, strategy=strategy), json_output=json_output))


@market_app.command("list")
def market_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """List installed OPC Market packages."""
    asyncio.run(_run_service_command(project, lambda svc: svc.market.list_installed(), json_output=json_output))


@market_app.command("uninstall")
def market_uninstall(
    package_id: str = typer.Argument(..., help="Package id to uninstall"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm uninstall"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project context"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Uninstall an installed OPC Market package."""
    if not yes:
        console.print("[warning]Destructive command requires --yes.[/warning]")
        raise typer.Exit(code=1)
    asyncio.run(_run_service_command(project, lambda svc: svc.market.uninstall(package_id), json_output=json_output))


@channels_app.command("status")
def channels_status():
    """Show configured channel status."""
    config = _get_config()
    from opc.channels.manager import ChannelManager
    from opc.channels.provider_registry import ordered_provider_specs
    from opc.layer0_interaction.message_bus import MessageBus

    runtime_state = _read_channel_runtime_state()
    runtime_running = False
    runtime_pid = None
    runtime_channels: list[str] = []
    if runtime_state:
        runtime_pid = int(runtime_state.get("pid", 0) or 0)
        runtime_channels = list(runtime_state.get("channels", []) or [])
        runtime_running = runtime_pid > 0 and _pid_is_running(runtime_pid)
        if runtime_pid > 0 and not runtime_running:
            _clear_channel_runtime_state()

    manager = ChannelManager(config, MessageBus())
    console.print(f"[bold]Runtime:[/bold] {'running' if runtime_running else 'stopped'}")
    if runtime_pid:
        console.print(f"  PID: {runtime_pid}")
    console.print("[bold]Channels:[/bold]")
    for spec in ordered_provider_specs():
        if hasattr(manager, "get_status"):
            status = manager.get_status(spec.name)
        else:
            channel = manager.get_channel(spec.name)
            capability = channel.describe_capability() if channel is not None else {}
            status = {
                "name": spec.name,
                "enabled": bool(getattr(getattr(config.channels, spec.name), "enabled", False)),
                "available": capability.get("available", channel is not None),
                "configured": capability.get("configured", channel is not None),
                "delivery_mode": capability.get("delivery_mode", spec.delivery_mode),
                "bridge_required": spec.bridge_required,
                "last_error": capability.get("last_error", ""),
            }
        details: list[str] = ["enabled" if status["enabled"] else "disabled"]
        if status["enabled"]:
            details.append("available" if status["available"] else "dependency-missing")
            details.append("configured" if status["configured"] else "config-missing")
            if status["bridge_required"]:
                details.append("bridge-required")
            if status["delivery_mode"]:
                details.append(str(status["delivery_mode"]))
        if runtime_running and spec.name in runtime_channels:
            details.append("runtime-active")
        if status["last_error"]:
            details.append(f"error={status['last_error']}")
        console.print(f"  - {spec.name}: {', '.join(details)}")


@channels_app.command("start")
def channels_start(project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID")):
    """Start enabled channels in foreground."""
    config = _get_config()
    asyncio.run(_run_channel_runtime(config, project))


@channels_app.command("stop")
def channels_stop():
    """Stop foreground channel runtime."""
    runtime_state = _read_channel_runtime_state()
    if not runtime_state:
        console.print("[warning]No running channel runtime found.[/warning]")
        return

    pid = int(runtime_state.get("pid", 0) or 0)
    if pid <= 0 or not _pid_is_running(pid):
        _clear_channel_runtime_state()
        console.print("[warning]Channel runtime is not running.[/warning]")
        return

    os.kill(pid, signal.SIGTERM)
    console.print(f"[success]Sent stop signal to channel runtime PID {pid}.[/success]")


@channels_app.command("login")
def channels_login(channel: Optional[str] = typer.Argument(None, help="Optional channel name")):
    """Show login/setup guidance for channels."""
    from opc.channels.provider_registry import PROVIDER_SPECS, ordered_provider_specs

    if channel:
        spec = PROVIDER_SPECS.get(channel)
        if spec is None:
            console.print(f"[warning]Unknown channel `{channel}`.[/warning]")
            return
        requirements = ", ".join(spec.required_config_fields) if spec.required_config_fields else "no required config fields"
        bridge = " Requires a companion bridge/runtime." if spec.bridge_required else ""
        console.print(
            f"[info]Configure `{channel}` in `.opc/config/channel_config.yaml`. "
            f"{escape(spec.login_summary)} Install extras with `{escape(f'pip install -e .[{spec.extra_name}]')}`. "
            f"Required config: {escape(requirements)}.{bridge} Then run `opc channels start`.[/info]"
        )
        return
    console.print("[info]Set channel credentials in .opc/config/channel_config.yaml and start the runtime with `opc channels start` or `opc run`.[/info]")
    for spec in ordered_provider_specs():
        bridge = " bridge-required;" if spec.bridge_required else ""
        console.print(
            f"  - {spec.name}: install `.[{spec.extra_name}]`; required fields: "
            f"{', '.join(spec.required_config_fields) or 'none'}; mode: {spec.delivery_mode};{bridge} {spec.login_summary}"
        )


@app.command()
def run(project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID")):
    """Run the long-lived engine + channel runtime in foreground."""
    config = _get_config()
    asyncio.run(_run_channel_runtime(config, project))


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

async def _single_message(
    config,
    message: str,
    project: str | None,
    no_markdown: bool,
    *,
    mode: str = "task",
    preferred_agent: str | None = None,
    company_profile: str = "corporate",
) -> None:
    engine, runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        console.print(f"\n[info]Processing:[/info] {message}\n")
        runtime_display.begin_turn()
        response = await engine.process_message(
            message,
            project_id=project,
            session_id=str(uuid.uuid4()),
            mode=mode,
            company_profile=company_profile if mode == "company" else None,
            preferred_agent=preferred_agent,
        )
        await runtime_display.flush()
        if not runtime_display.has_streamed_content:
            _print_response(response, no_markdown)
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
    except Exception as e:
        console.print(f"\n[error]Error: {escape(str(e))}[/error]")
        if config.system.log_level == "DEBUG":
            import traceback
            console.print(escape(traceback.format_exc()))
    finally:
        await engine.shutdown()


async def _single_secretary_message(config, message: str, project: str | None, no_markdown: bool) -> None:
    engine, runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        console.print(f"\n[info]Secretary processing:[/info] {message}\n")
        runtime_display.begin_turn()
        payload = await engine.process_secretary_message(
            message,
            project_id=project,
            session_id=str(uuid.uuid4()),
        )
        await runtime_display.flush()
        if not runtime_display.has_streamed_content:
            _print_response(payload.get("response", ""), no_markdown)
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
    except Exception as e:
        console.print(f"\n[error]Error: {escape(str(e))}[/error]")
        if config.system.log_level == "DEBUG":
            import traceback
            console.print(escape(traceback.format_exc()))
    finally:
        await engine.shutdown()


async def _propose_reorg(config, payload: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        data = json.loads(payload)
        proposal = await engine.propose_company_reorg(
            summary=str(data.get("summary", "Runtime company reorg")),
            rationale=str(data.get("rationale", data.get("summary", ""))),
            title=str(data.get("title", "")),
            changeset=data.get("changeset", {}),
            session_id=str(uuid.uuid4()),
            task_id=data.get("task_id"),
            initiated_by=str(data.get("initiated_by", "owner")),
            source_role_id=str(data.get("source_role_id", "")),
            metadata={"source": "cli"},
        )
        console.print(f"[success]Created reorg proposal:[/success] {proposal.proposal_id}")
        console.print(f"Status: {proposal.status.value}")
        console.print(f"Scope: {proposal.scope.value}")
        console.print(f"Risk: {proposal.risk_level.value}")
        console.print(f"Summary: {proposal.summary}")
        if proposal.user_confirmation_required:
            await engine._save_reorg_checkpoint(proposal)  # noqa: SLF001
            console.print("[warning]User confirmation is required before applying this reorg.[/warning]")
    finally:
        await engine.shutdown()


async def _approve_reorg(config, proposal_id: str, project: str | None, *, approved: bool) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        proposal = await engine.approve_company_reorg(
            proposal_id,
            approved=approved,
            notes=f"{'Approved' if approved else 'Denied'} via CLI.",
        )
        console.print(f"[success]Updated reorg:[/success] {proposal.proposal_id}")
        console.print(f"Status: {proposal.status.value}")
    finally:
        await engine.shutdown()


async def _apply_reorg(config, proposal_id: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        result = await engine.apply_company_reorg(proposal_id)
        console.print(f"[success]Applied reorg:[/success] {proposal_id}")
        console.print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await engine.shutdown()


async def _show_reorg(config, proposal_id: str, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        proposal = await engine.show_company_reorg(proposal_id)
        if not proposal:
            console.print(f"[warning]Unknown reorg proposal:[/warning] {proposal_id}")
            return
        console.print(json.dumps({
            "proposal_id": proposal.proposal_id,
            "status": proposal.status.value,
            "scope": proposal.scope.value,
            "risk_level": proposal.risk_level.value,
            "summary": proposal.summary,
            "rationale": proposal.rationale,
            "impact_summary": proposal.impact_summary,
            "approval_notes": proposal.approval_notes,
            "metadata": proposal.metadata,
        }, ensure_ascii=False, indent=2))
    finally:
        await engine.shutdown()


_SLASH_DEFAULT_LIMIT = 20
_SLASH_MAX_LIMIT = 100
_VALID_CHAT_MODES = {"task", "company"}
_VALID_COMPANY_PROFILES = {"corporate", "custom"}
_VALID_PREFERRED_AGENTS = {"native", "codex", "claude_code", "cursor", "opencode"}


@dataclass(frozen=True)
class _SlashCommandSpec:
    group: str
    command: str
    description: str
    subcommands: tuple[str, ...] = ()


@dataclass
class _InteractiveChatState:
    config: OPCConfig
    engine: Any
    runtime_display: Any
    session_id: str
    no_markdown: bool
    mode: str = "task"
    company_profile: str = "corporate"
    org_id: str = ""
    preferred_agent: str | None = "native"
    domains: list[str] = field(default_factory=list)
    company_staffing_drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_to_task: dict[str, str] = field(default_factory=dict)
    active_runtime_children: dict[str, str] = field(default_factory=dict)
    runtime_control_state: str = ""
    runtime_control_task_id: str = ""
    runtime_control_session_id: str = ""
    runtime_control_checkpoint_id: str = ""


class BusyCommandPolicy(str, Enum):
    IMMEDIATE_READONLY = "immediate_readonly"
    BLOCKED_WHEN_BUSY = "blocked_when_busy"
    NORMAL_WHEN_IDLE = "normal_when_idle"


@dataclass
class QueuedChatInput:
    text: str
    project_id: str
    session_id: str
    mode: str
    company_profile: str
    preferred_agent: str | None
    domains: list[str]
    org_id: str = ""
    message_metadata: dict[str, Any] | None = None
    allow_interactive_followups: bool = False
    enqueue_time: float = field(default_factory=time.time)


class ChatTurnController:
    """Keep interactive chat responsive while a turn is running."""

    def __init__(self, state: _InteractiveChatState) -> None:
        self.state = state
        self.queue: deque[QueuedChatInput] = deque()
        self.active_task: asyncio.Task[None] | None = None
        self.kanban_watch_task: asyncio.Task[None] | None = None
        self._kanban_watch_fingerprint = ""
        self._lock: asyncio.Lock | None = None
        self._closing = False

    @property
    def is_busy(self) -> bool:
        return self.active_task is not None and not self.active_task.done()

    def queue_depth(self) -> int:
        return len(self.queue)

    def kanban_watch_active(self) -> bool:
        return self.kanban_watch_task is not None and not self.kanban_watch_task.done()

    def queued_items(self) -> list[QueuedChatInput]:
        return list(self.queue)

    async def submit_user_message(self, text: str) -> None:
        item = QueuedChatInput(
            text=text,
            project_id=_current_project_id(self.state.engine),
            session_id=self.state.session_id,
            mode=self.state.mode,
            company_profile=self.state.company_profile,
            org_id=self.state.org_id,
            preferred_agent=self.state.preferred_agent,
            domains=list(self.state.domains),
        )
        await self.submit_item(item)

    async def submit_control_message(
        self,
        text: str,
        *,
        message_metadata: dict[str, Any],
        mode: str | None = None,
        company_profile: str | None = None,
        org_id: str | None = None,
        preferred_agent: str | None = None,
        domains: list[str] | None = None,
    ) -> None:
        item = QueuedChatInput(
            text=text,
            project_id=_current_project_id(self.state.engine),
            session_id=self.state.session_id,
            mode=mode or self.state.mode,
            company_profile=company_profile or self.state.company_profile,
            org_id=self.state.org_id if org_id is None else org_id,
            preferred_agent=self.state.preferred_agent if preferred_agent is None else preferred_agent,
            domains=list(self.state.domains if domains is None else domains),
            message_metadata=dict(message_metadata),
        )
        await self.submit_item(item)

    async def submit_item(self, item: QueuedChatInput) -> None:
        async with self._ensure_lock():
            if self.is_busy:
                self.queue.append(item)
                console.print(
                    f"[dim]Queued #{len(self.queue)}; it will run after the current turn. "
                    "Use /queue list or /queue drop <n>.[/dim]"
                )
                return
            self._start_item_locked(item)

    async def wait_for_active(self) -> None:
        task = self.active_task
        if task is not None:
            await task

    async def shutdown(self) -> None:
        self._closing = True
        self.queue.clear()
        await self.stop_kanban_watch(silent=True)
        prepare = getattr(
            self.state.engine,
            "prepare_active_company_runtimes_for_shutdown",
            None,
        )
        if callable(prepare):
            # Persist company checkpoints and put the engine into
            # infrastructure-shutdown mode before cancelling the active turn.
            # Engine.shutdown() repeats this idempotently when it closes the
            # remaining stores and subsystems.
            await prepare()
        task = self.active_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    def drop(self, index: int) -> QueuedChatInput | None:
        if index < 1 or index > len(self.queue):
            return None
        items = list(self.queue)
        item = items.pop(index - 1)
        self.queue = deque(items)
        return item

    def clear(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    async def start_kanban_watch(
        self,
        *,
        interval: float = 2.0,
        limit: int = 100,
        initial_fingerprint: str = "",
        session_id: str | None = None,
        project_scope: bool = False,
    ) -> None:
        if self.kanban_watch_active():
            console.print("[dim]Kanban live watch is already running. Use /kanban stop to stop it.[/dim]")
            return
        self._kanban_watch_fingerprint = initial_fingerprint
        self.kanban_watch_task = asyncio.create_task(
            self._kanban_watch_loop(
                interval=interval,
                limit=limit,
                session_id=session_id,
                project_scope=project_scope,
            )
        )
        scope_label = "project" if project_scope else f"session {(session_id or self.state.session_id)[:8]}"
        console.print(
            f"[dim]Kanban live watch started for {escape(scope_label)}. "
            "New or changed work items will appear here. Exit with /kanban stop.[/dim]"
        )

    async def stop_kanban_watch(self, *, silent: bool = False) -> None:
        task = self.kanban_watch_task
        self.kanban_watch_task = None
        if task is None or task.done():
            if not silent:
                console.print("[dim]Kanban live watch is not running.[/dim]")
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if not silent:
            console.print("[dim]Kanban live watch stopped.[/dim]")

    def _start_item_locked(self, item: QueuedChatInput) -> None:
        self.active_task = asyncio.create_task(self._run_item(item))

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _run_item(self, item: QueuedChatInput) -> None:
        self.state.session_id = item.session_id
        self.state.mode = item.mode
        self.state.company_profile = item.company_profile
        self.state.org_id = item.org_id
        self.state.preferred_agent = item.preferred_agent
        self.state.domains = list(item.domains)
        try:
            await _process_interactive_chat_message(
                self.state,
                item.text,
                message_metadata=item.message_metadata,
                interactive_followups=item.allow_interactive_followups,
            )
        except asyncio.CancelledError:
            raise
        finally:
            async with self._ensure_lock():
                self.active_task = None
                if not self._closing and self.queue:
                    next_item = self.queue.popleft()
                    console.print(f"[dim]Running queued input; {len(self.queue)} remaining.[/dim]")
                    self._start_item_locked(next_item)

    async def _kanban_watch_loop(
        self,
        *,
        interval: float,
        limit: int,
        session_id: str | None,
        project_scope: bool,
    ) -> None:
        try:
            while True:
                items = await _fetch_kanban_items(
                    self.state,
                    limit=limit,
                    session_id=session_id,
                    project_scope=project_scope,
                )
                fingerprint = _kanban_items_fingerprint(items)
                if fingerprint != self._kanban_watch_fingerprint:
                    self._kanban_watch_fingerprint = fingerprint
                    title = "Kanban Live Update (project)" if project_scope else f"Kanban Live Update (session {(session_id or self.state.session_id)[:8]})"
                    _render_kanban_items(items, title=title)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            console.print(f"[warning]Kanban live watch stopped: {escape(str(exc))}[/warning]")


_SLASH_COMMANDS: tuple[_SlashCommandSpec, ...] = (
    _SlashCommandSpec("Chat", "/help", "Show this command list."),
    _SlashCommandSpec("Chat", "/quit", "Exit interactive chat."),
    _SlashCommandSpec("Chat", "/queue list|drop|clear", "Inspect or edit queued prompts while a turn is running.", ("list", "drop", "clear")),
    _SlashCommandSpec("Context", "/status", "Show project, session, mode, agent, domains, model, and cost."),
    _SlashCommandSpec("Context", "/mode [task|company] [corporate|custom]", "Set execution mode for future messages.", ("task", "company", "corporate", "custom")),
    _SlashCommandSpec("Context", "/agent [native|codex|claude_code|cursor|opencode|none]", "Set preferred external agent.", ("native", "codex", "claude_code", "cursor", "opencode", "none")),
    _SlashCommandSpec("Context", "/domains [domain ...|clear]", "Set domain hints for future messages.", ("clear",)),
    _SlashCommandSpec("Project", "/project", "Show current project, known projects, and switch/create/delete usage.", ("list", "switch", "create", "rename", "delete")),
    _SlashCommandSpec("Project", "/project list", "List known projects."),
    _SlashCommandSpec("Project", "/project create <id>", "Create a project through the shared Office service."),
    _SlashCommandSpec("Project", "/project switch <id>", "Switch project and restore its latest session."),
    _SlashCommandSpec("Project", "/project rename <old_id> <new_id>", "Rename a project id and move persisted project data."),
    _SlashCommandSpec("Project", "/project delete <id> --yes", "Delete a project through the shared Office service."),
    _SlashCommandSpec("Session", "/stop [task_id|session_id]", "Stop the current or selected runtime and preserve history for follow-up."),
    _SlashCommandSpec("Session", "/continue [task_id|session_id] [message]", "Continue a stopped runtime without re-planning."),
    _SlashCommandSpec("Session", "/session", "Show current session, recent sessions, and switch/create/delete usage.", ("list", "new", "create", "resume", "show", "config", "send", "rename", "delete", "stop", "continue", "complete")),
    _SlashCommandSpec("Session", "/session list [limit]", "List recent sessions."),
    _SlashCommandSpec("Session", "/session new", "Start a fresh conversation session."),
    _SlashCommandSpec("Session", "/session create [title] [--mode task|company|org] [--agent ...]", "Create a task-backed session through the shared Office service."),
    _SlashCommandSpec("Session", "/session resume <session_id>", "Resume a session in this project."),
    _SlashCommandSpec("Session", "/session show <session_id|task_id> [--limit N] [--full]", "Show session metadata and transcript."),
    _SlashCommandSpec("Session", "/session config <task_id> [--mode ...] [--agent ...] [--org ...]", "Update session execution config through the shared Office service."),
    _SlashCommandSpec("Session", "/session send <task_id> <message>", "Send a message to a task-backed session through the shared Office service."),
    _SlashCommandSpec("Session", "/session rename <task_id|session_id> <title>", "Rename a task-backed or plain session."),
    _SlashCommandSpec("Session", "/session delete <task_id> --yes", "Hard-delete a task-backed session."),
    _SlashCommandSpec("Session", "/session stop [task_id|session_id]", "Stop the current or selected runtime through the shared Office service."),
    _SlashCommandSpec("Session", "/session continue [task_id|session_id] [message]", "Continue a stopped runtime through the shared Office service."),
    _SlashCommandSpec("Session", "/session complete <task_id>", "Complete a task-backed session through the shared Office service."),
    _SlashCommandSpec("Tasks", "/tasks [status] [--limit N] [--full]", "List current project tasks."),
    _SlashCommandSpec("Tasks", "/task show <task_id> [--limit N] [--full]", "Show task detail and transcript.", ("show", "move", "done", "rename", "delete")),
    _SlashCommandSpec("Tasks", "/task move <task_id> todo|in-progress|done|blocked|failed|cancelled", "Move a task through the shared transition API."),
    _SlashCommandSpec("Tasks", "/task done <task_id>", "Mark a task done through the shared transition API."),
    _SlashCommandSpec("Tasks", "/task rename <task_id> <title>", "Rename a task and its session title."),
    _SlashCommandSpec("Tasks", "/task delete <task_id> --yes", "Hard-delete a task and its persisted lifecycle data."),
    _SlashCommandSpec("Runtime", "/runtime [--limit N] [--full]", "Show live runtime, active tasks, external sessions, and checkpoints."),
    _SlashCommandSpec("Runtime", "/logs <task_id|session_id> [--limit N] [--full]", "Show execution logs, runtime events, tools, and transcript."),
    _SlashCommandSpec("Runtime", "/comms <task_id> [--limit N] [--full]", "Show company-mode messages, handoffs, and review notes."),
    _SlashCommandSpec("Runtime", "/attachments [--limit N] [--full]", "List current session attachment references."),
    _SlashCommandSpec("Runtime", "/staffing [context]", "Open the pending company staffing/agent editor or role context preview.", ("context",)),
    _SlashCommandSpec("Board", "/kanban [once|stop|all]", "Show current-session work-item status inline and optionally watch live updates.", ("once", "stop", "all", "project", "--once", "--all")),
    _SlashCommandSpec("Board", "/board kanban|pipeline|work-item|role|logs", "Open opc board in read-only inspector mode.", ("kanban", "pipeline", "work-item", "role", "logs")),
    _SlashCommandSpec("Work Items", "/work-items list|show|logs|role-status", "Inspect company work items and role progress.", ("list", "show", "logs", "role-status")),
    _SlashCommandSpec("Org", "/org", "Show or edit organization config.", ("info", "role", "policy", "strategy", "reset", "saved", "export", "import")),
    _SlashCommandSpec("Org", "/org role add|update|delete|bulk-add", "Manage company roles through the shared Office service."),
    _SlashCommandSpec("Org", "/org policy update --payload ...", "Update runtime policy through the shared Office service."),
    _SlashCommandSpec("Org", "/org strategy update --final-decider <role>", "Update organization strategy."),
    _SlashCommandSpec("Org", "/org saved list|save|load|delete", "Manage saved organization architectures."),
    _SlashCommandSpec("Agent", "/agent list|detail|create|delete|move|import-employee", "Manage visual office agents.", ("list", "detail", "create", "delete", "move", "create-from-template", "import-employee")),
    _SlashCommandSpec("Talent", "/talent list|employees|scan", "Browse talent templates and employees.", ("list", "employees", "scan", "import", "import-repo", "hire", "employee", "import-agent")),
    _SlashCommandSpec("Talent", "/talent import <template_id...>", "Import local talent templates."),
    _SlashCommandSpec("Talent", "/talent import-repo <path>", "Import talent templates from a local repo."),
    _SlashCommandSpec("Talent", "/talent hire <template_id> <role_id> [--name ...]", "Hire a template into a role."),
    _SlashCommandSpec("Talent", "/talent employee <employee_id>", "Show employee detail."),
    _SlashCommandSpec("Talent", "/talent import-agent <employee_id>", "Import an employee as a visual office agent."),
    _SlashCommandSpec("Market", "/market list", "List installed OPC Market packages.", ("browse", "preview", "list", "presets", "apply-preset", "install", "uninstall", "export")),
    _SlashCommandSpec("Market", "/market browse|preview <preset_id>", "Browse and preview architecture presets."),
    _SlashCommandSpec("Market", "/market presets", "List built-in architecture presets."),
    _SlashCommandSpec("Market", "/market apply-preset <preset_id> [--strategy namespace|overwrite]", "Apply a built-in architecture preset as the active custom organization."),
    _SlashCommandSpec("Market", "/market install <path> [--strategy namespace|overwrite]", "Install a local .opcpkg package."),
    _SlashCommandSpec("Market", "/market uninstall <package_id> --yes", "Uninstall an OPC Market package."),
    _SlashCommandSpec("Market", "/market export --id ... --name ...", "Export the current org as a package."),
    _SlashCommandSpec("Reorg", "/reorg list|show|approve|deny|apply", "Manage reorg proposals.", ("list", "show", "approve", "deny", "apply")),
    _SlashCommandSpec("Diagnostics", "/cost", "Show token and cost counters."),
    _SlashCommandSpec("Diagnostics", "/checkpoints [--limit N] [--full]", "List pending execution checkpoints."),
)
CommandSpec = _SlashCommandSpec
_SLASH_ALIASES = {"p": "project", "s": "session", "t": "task", "checkpoint": "checkpoints", "work-item": "work-items", "workitems": "work-items"}


def _initial_company_profile(config: OPCConfig) -> str:
    value = str(getattr(getattr(config, "org", None), "company_profile", "") or "corporate").strip().lower()
    return value if value in _VALID_COMPANY_PROFILES else "corporate"


def _normalize_interactive_preferred_agent(value: str | None) -> str | None:
    normalized = str(value or "native").strip().lower().replace("-", "_")
    if normalized in {"none", "auto", "default", "system"}:
        return None
    return normalized if normalized in _VALID_PREFERRED_AGENTS else "native"


def _cli_option_present(*names: str) -> bool:
    argv = list(sys.argv[1:])
    for idx, arg in enumerate(argv):
        if arg in names:
            return True
        if any(arg.startswith(f"{name}=") for name in names):
            return True
    return False


async def _read_persisted_chat_context(state: _InteractiveChatState) -> dict[str, str]:
    try:
        import sqlite3

        db_path = state.engine.opc_home / "ui_state.db"
        if not db_path.exists():
            return {}
        with sqlite3.connect(str(db_path), timeout=1) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            rows = db.execute(
                "SELECT key, value FROM server_state WHERE key IN (?, ?, ?)",
                ("exec_mode", "company_profile", "task_preferred_agent"),
            ).fetchall()
        values = {str(key): str(value) for key, value in rows}
        return {
            "mode": values.get("exec_mode", ""),
            "company_profile": values.get("company_profile", ""),
            "preferred_agent": values.get("task_preferred_agent", ""),
        }
    except Exception:
        return {}


async def _persist_chat_context(state: _InteractiveChatState) -> None:
    try:
        import sqlite3

        db_path = state.engine.opc_home / "ui_state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path), timeout=1) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.executemany(
                "INSERT OR REPLACE INTO server_state (key, value) VALUES (?, ?)",
                (
                    ("exec_mode", state.mode),
                    ("company_profile", state.company_profile),
                    ("task_preferred_agent", state.preferred_agent or "native"),
                ),
            )
            db.commit()
    except Exception as exc:
        console.print(f"[warning]Could not persist CLI mode state: {escape(str(exc))}[/warning]")


async def _restore_chat_context(
    state: _InteractiveChatState,
    *,
    restore_mode: bool,
    restore_company_profile: bool,
    restore_agent: bool,
) -> None:
    persisted = await _read_persisted_chat_context(state)
    raw_mode = str(persisted.get("mode", "") or "").strip().lower()
    raw_profile = str(persisted.get("company_profile", "") or "").strip().lower()
    raw_agent = str(persisted.get("preferred_agent", "") or "").strip().lower().replace("-", "_")
    if restore_mode:
        if raw_mode in {"company", "company_mode"}:
            state.mode = "company"
        elif raw_mode in {"org", "custom"}:
            state.mode = "company"
            if restore_company_profile:
                state.company_profile = "custom"
        elif raw_mode in {"task", "project", "task_mode", "project_mode"}:
            state.mode = "task"
    if restore_company_profile and raw_profile in _VALID_COMPANY_PROFILES:
        state.company_profile = raw_profile
    if restore_agent and raw_agent:
        state.preferred_agent = _normalize_interactive_preferred_agent(raw_agent)


def _current_project_id(engine: Any) -> str:
    return str(getattr(engine, "project_id", None) or "default").strip() or "default"


def _safe_project_id(project_id: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project_id or ""))


def _value_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _format_datetime(value: Any) -> str:
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _clip_text(value: Any, limit: int = 240, *, full: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if full:
        return text
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _json_summary(value: Any, limit: int = 240, *, full: bool = False) -> str:
    if isinstance(value, dict):
        for key in ("summary", "title", "message", "prompt", "original_message"):
            if value.get(key):
                return _clip_text(value.get(key), limit, full=full)
    try:
        return _clip_text(json.dumps(value, ensure_ascii=False, default=str), limit, full=full)
    except TypeError:
        return _clip_text(value, limit, full=full)


def _coerce_limit(value: str, *, default: int = _SLASH_DEFAULT_LIMIT) -> int:
    if not str(value or "").strip():
        return default
    limit = int(str(value).strip())
    return max(1, min(_SLASH_MAX_LIMIT, limit))


def _parse_limit_args(args: list[str], *, default: int = _SLASH_DEFAULT_LIMIT) -> tuple[list[str], int]:
    remaining: list[str] = []
    limit = default
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--limit":
            if idx + 1 >= len(args):
                raise ValueError("Missing value for --limit.")
            limit = _coerce_limit(args[idx + 1], default=default)
            idx += 2
            continue
        if token.startswith("--limit="):
            limit = _coerce_limit(token.split("=", 1)[1], default=default)
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    return remaining, limit


def _parse_view_args(args: list[str], *, default: int = _SLASH_DEFAULT_LIMIT) -> tuple[list[str], int, bool]:
    remaining: list[str] = []
    full = False
    idx = 0
    while idx < len(args):
        token = args[idx]
        if token == "--full":
            full = True
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    remaining, limit = _parse_limit_args(remaining, default=default)
    return remaining, limit, full


def _slash_parts(raw_input: str) -> list[str]:
    command = raw_input[1:] if raw_input.startswith("/") else raw_input
    return shlex.split(command)


def _canonical_slash_command(command: str) -> str:
    return _SLASH_ALIASES.get(command.strip().lower(), command.strip().lower())


def _slash_command_names() -> list[str]:
    names = {
        spec.command.split()[0].lstrip("/")
        for spec in _SLASH_COMMANDS
        if spec.command.startswith("/")
    }
    names.update(_SLASH_ALIASES.keys())
    return sorted(names)


def _slash_subcommands(command: str) -> list[str]:
    canonical = _canonical_slash_command(command)
    values: set[str] = set()
    for spec in _SLASH_COMMANDS:
        root = spec.command.split()[0].lstrip("/")
        if root == canonical:
            values.update(spec.subcommands)
            parts = spec.command.split()
            if len(parts) > 1 and not parts[1].startswith("[") and not parts[1].startswith("<"):
                values.add(parts[1].split("|", 1)[0])
    return sorted(item for item in values if item)


class _OPCSlashCompleter:
    def get_completions(self, document: Any, complete_event: Any):  # noqa: ANN201 - prompt-toolkit protocol
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:
            return
        text = str(getattr(document, "text_before_cursor", "") or "")
        if not text.startswith("/"):
            return
        body = text[1:]
        if " " not in body:
            prefix = body.lower()
            if prefix in _slash_command_names():
                for suggestion in _slash_subcommands(prefix):
                    yield Completion(f"/{prefix} {suggestion}", start_position=-len(text))
                return
            for command in _slash_command_names():
                if command.startswith(prefix):
                    yield Completion(f"/{command}", start_position=-len(text))
            return
        tokens = body.split()
        command = _canonical_slash_command(tokens[0] if tokens else "")
        prefix = "" if body.endswith(" ") else (tokens[-1].lower() if len(tokens) > 1 else "")
        suggestions = _slash_subcommands(command)
        if command == "agent":
            suggestions.extend(sorted([*_VALID_PREFERRED_AGENTS, "none"]))
        if command == "mode":
            suggestions.extend(["task", "company", "corporate", "custom"])
        for suggestion in sorted(set(suggestions)):
            if suggestion.startswith(prefix):
                yield Completion(suggestion, start_position=-len(prefix))

    async def get_completions_async(self, document: Any, complete_event: Any):  # noqa: ANN201 - prompt-toolkit protocol
        for completion in self.get_completions(document, complete_event):
            yield completion


def _print_slash_help() -> None:
    table = Table(title="OPC Slash Commands", show_lines=False)
    table.add_column("Group", style="cyan", no_wrap=True)
    table.add_column("Command", style="bold")
    table.add_column("What it does")
    for spec in _SLASH_COMMANDS:
        table.add_row(spec.group, spec.command, spec.description)
    table.add_row("Aliases", "/p /s /t", "Short aliases for /project, /session, and /task.")
    console.print(table)


def _print_interactive_cost(engine: Any) -> None:
    if not getattr(engine, "llm", None):
        console.print("[warning]LLM runtime is not available.[/warning]")
        return
    stats = getattr(engine.llm, "stats", {}) or {}
    console.print(
        f"Session: tokens_in={stats.get('tokens_in', 0)}, "
        f"tokens_out={stats.get('tokens_out', 0)}, "
        f"cost=${float(stats.get('estimated_cost', 0.0) or 0.0):.4f}"
    )


def _print_context_status(state: _InteractiveChatState) -> None:
    table = Table(title="Current Context", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Project", _current_project_id(state.engine))
    table.add_row("Session", state.session_id or "(none)")
    table.add_row("Mode", state.mode)
    table.add_row("Company profile", state.company_profile if state.mode == "company" else "(inactive)")
    if state.org_id:
        table.add_row("Org", state.org_id)
    table.add_row("Preferred agent", state.preferred_agent or "(system default)")
    table.add_row("Domains", ", ".join(state.domains) if state.domains else "(none)")
    table.add_row("Runtime control", state.runtime_control_state or "(running/idle)")
    if state.runtime_control_task_id:
        table.add_row("Runtime task", state.runtime_control_task_id)
    if state.runtime_control_checkpoint_id:
        table.add_row("Runtime checkpoint", state.runtime_control_checkpoint_id)
    console.print(table)
    console.print("[dim]Use /mode, /agent, /project, /session, /stop, or /continue to inspect and control context.[/dim]")


def _list_cli_project_ids(engine: Any) -> list[str]:
    project_ids = {"default", _current_project_id(engine)}
    projects_dir = Path(getattr(engine, "opc_home", get_opc_home())) / "projects"
    if projects_dir.is_dir():
        for entry in sorted(projects_dir.iterdir()):
            if entry.is_dir():
                project_ids.add(entry.name)
    return sorted(project_ids, key=lambda item: (item != "default", item.lower()))


def _cli_project_paths(engine: Any, project_id: str) -> tuple[Path, Path, Path]:
    from opc.core.config import get_project_workplace

    opc_home = Path(getattr(engine, "opc_home", get_opc_home()))
    return (
        opc_home / "projects" / project_id,
        opc_home / "memory" / "projects" / f"{project_id}.md",
        get_project_workplace(project_id),
    )


def _cli_project_exists(engine: Any, project_id: str) -> bool:
    if project_id == "default":
        return True
    return any(path.exists() for path in _cli_project_paths(engine, project_id))


def _render_project_list(state: _InteractiveChatState) -> None:
    current = _current_project_id(state.engine)
    table = Table(title="Projects")
    table.add_column("Project")
    table.add_column("Current", justify="center")
    table.add_column("Markers")
    for project_id in _list_cli_project_ids(state.engine):
        project_dir, project_memory, workplace = _cli_project_paths(state.engine, project_id)
        markers: list[str] = []
        if (project_dir / "tasks.db").exists():
            markers.append("tasks")
        if project_memory.exists():
            markers.append("memory")
        if workplace.exists():
            markers.append("workplace")
        table.add_row(project_id, "*" if project_id == current else "", ", ".join(markers) or "")
    console.print(table)
    console.print("[dim]Switch with /project switch <id> or /project <id>. Create with /project create <id>. Rename with /project rename <old_id> <new_id>.[/dim]")


def _render_project_picker(state: _InteractiveChatState, project_ids: list[str]) -> None:
    current = _current_project_id(state.engine)
    table = Table(title="Choose Project")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Project")
    table.add_column("Current", justify="center")
    for idx, project_id in enumerate(project_ids, start=1):
        table.add_row(str(idx), project_id, "*" if project_id == current else "")
    console.print(table)
    console.print("[dim]Enter a number or project id. Use `new <id>` to create a project. Blank selects the current project.[/dim]")


def _render_session_picker(sessions: list[Any]) -> None:
    if not sessions:
        console.print("[info]No sessions found for this project.[/info]")
        console.print("[dim]Press Enter to create a new session.[/dim]")
        return
    table = Table(title="Choose Session")
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Session ID")
    table.add_column("Title")
    table.add_column("Mode")
    table.add_column("Updated")
    for idx, item in enumerate(sessions, start=1):
        table.add_row(
            str(idx),
            str(getattr(item, "session_id", "") or ""),
            _clip_text(getattr(item, "title", "") or "(untitled)", 72),
            str(getattr(item, "mode", "") or ""),
            _format_datetime(getattr(item, "updated_at", None)),
        )
    console.print(table)
    console.print("[dim]Enter a number or session id. Press Enter, or type `new`, to create a new session.[/dim]")


async def _load_recent_primary_sessions(store: OPCStore | None, project_id: str | None, *, limit: int = 20) -> list[Any]:
    if not store:
        return []
    return await store.list_sessions(project_id=project_id or "default", parent_session_id=None, limit=limit)


def _session_short_id(session_id: str) -> str:
    text = str(session_id or "").strip()
    if len(text) <= 12:
        return text or "(none)"
    return f"{text[:8]}..."


def _chat_bottom_toolbar_text(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> list[tuple[str, str]]:
    profile = f" {state.company_profile}" if state.mode == "company" else ""
    org = f" org:{state.org_id}" if state.org_id else ""
    busy_hint = ""
    if controller is not None:
        if controller.is_busy:
            kanban_hint = " kanban:live" if controller.kanban_watch_active() else ""
            busy_hint = f"  running queue:{controller.queue_depth()}{kanban_hint}  Enter=queue /kanban /logs /runtime"
        elif controller.kanban_watch_active():
            busy_hint = "  kanban:live exit:/kanban stop"
        elif controller.queue_depth():
            busy_hint = f"  queue:{controller.queue_depth()}"
    runtime_hint = f"  runtime:{state.runtime_control_state}" if state.runtime_control_state else ""
    text = (
        f" project:{_current_project_id(state.engine)}"
        f"  session:{_session_short_id(state.session_id)}"
        f"  mode:{state.mode}{profile}"
        f"{org}"
        f"  agent:{state.preferred_agent or 'system'}"
        f"{runtime_hint}"
        f"{busy_hint}"
        "    /mode /agent /project /session /help"
    )
    return [("class:bottom-toolbar", text)]


def _print_chat_hint() -> None:
    console.print("[dim]Hint: /mode switches mode, /agent switches agent, /project switches project, /session switches session.[/dim]")


async def _run_chat_office_service(state: _InteractiveChatState, operation: Any) -> dict[str, Any] | None:
    from opc.plugins.office_ui.services import ServiceError
    from opc.plugins.office_ui.services.factory import OfficeServiceFactory

    try:
        async with OfficeServiceFactory(config=state.config, project_id=_current_project_id(state.engine)) as services:
            result = await operation(services)
            return {"ok": True, **dict(result.payload)}
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return None
    except Exception as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        return None


async def _run_chat_current_office_service(state: _InteractiveChatState, operation: Any) -> dict[str, Any] | None:
    from opc.plugins.office_ui.services import ModeState, OfficeServiceContext, OfficeServices, ServiceError

    try:
        context = OfficeServiceContext(
            engine=state.engine,
            agent_store=None,
            chat_store=None,
            event_adapter=None,
            mode_state=ModeState(
                exec_mode=state.mode,
                company_profile=state.company_profile,
                task_preferred_agent=state.preferred_agent or "native",
            ),
        )
        context.session_to_task = state.session_to_task
        context.active_runtime_children = state.active_runtime_children
        result = await operation(OfficeServices(context))
        return {"ok": True, **dict(result.payload)}
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return None
    except Exception as exc:
        console.print(f"[error]{escape(str(exc))}[/error]")
        return None


async def _switch_chat_project(state: _InteractiveChatState, project_id: str, *, restore_session: bool = True) -> None:
    if not project_id:
        console.print("[warning]Usage: /project switch <id>[/warning]")
        return
    if not _safe_project_id(project_id):
        console.print("[warning]Invalid project id. Use letters, numbers, hyphens, and underscores.[/warning]")
        return
    if not _cli_project_exists(state.engine, project_id):
        console.print(f"[warning]Project does not exist: {project_id}[/warning]")
        return
    await state.engine.shutdown()
    engine, runtime_display = _create_cli_engine(state.config, project_id)
    await engine.initialize()
    state.engine = engine
    state.runtime_display = runtime_display
    state.session_to_task.clear()
    state.active_runtime_children.clear()
    state.runtime_control_state = ""
    state.runtime_control_task_id = ""
    state.runtime_control_session_id = ""
    state.runtime_control_checkpoint_id = ""
    state.org_id = ""
    _attach_cli_runtime_callbacks(state)
    console.print(f"[info]Switched to project: {project_id}[/info]")
    if not restore_session:
        state.session_id = ""
        return
    state.session_id, restored_latest = await _resolve_starting_session_id(engine)
    if restored_latest:
        console.print(f"[info]Restored recent session: {state.session_id}[/info]")
    else:
        console.print(f"[info]Started a new session: {state.session_id}[/info]")


async def _create_and_switch_chat_project(state: _InteractiveChatState, project_id: str) -> bool:
    if not _safe_project_id(project_id):
        console.print("[warning]Invalid project id. Use letters, numbers, hyphens, and underscores.[/warning]")
        return False
    payload = await _run_chat_office_service(
        state,
        lambda svc: svc.project.create(project_id, active_project_id=_current_project_id(state.engine)),
    )
    if not payload:
        return False
    console.print(f"[success]Created project: {payload.get('project_id') or project_id}[/success]")
    await _switch_chat_project(state, project_id, restore_session=False)
    return True


async def _choose_initial_project(state: _InteractiveChatState, *, explicit_project: bool = False) -> None:
    if explicit_project:
        console.print(f"[info]Project: {_current_project_id(state.engine)}[/info]")
        return
    while True:
        project_ids = _list_cli_project_ids(state.engine)
        _render_project_picker(state, project_ids)
        try:
            choice = console.input("[bold]Project[/bold] [current]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if not choice:
            console.print(f"[info]Project: {_current_project_id(state.engine)}[/info]")
            return
        lowered = choice.lower()
        if lowered in {"quit", "exit", "/quit", "/exit"}:
            raise KeyboardInterrupt
        if lowered.startswith("/project"):
            try:
                parts = _slash_parts(choice)
                project_args = parts[1:]
                before_project = _current_project_id(state.engine)
                await _handle_project_slash(state, project_args)
            except ValueError as exc:
                console.print(f"[warning]Could not parse command: {exc}[/warning]")
                continue
            subcommand = project_args[0].lower() if project_args else ""
            if subcommand in {"switch", "use"} and _current_project_id(state.engine) != before_project:
                return
            if len(project_args) == 1 and subcommand not in {"list", "ls", "create", "new", "delete", "rm"} and _current_project_id(state.engine) != before_project:
                return
            continue
        new_prefixes = ("new ", "create ", "+ ")
        if any(lowered.startswith(prefix) for prefix in new_prefixes):
            project_id = choice.split(maxsplit=1)[1].strip() if " " in choice else ""
            if not project_id:
                console.print("[warning]Usage: new <project_id>[/warning]")
                continue
            if await _create_and_switch_chat_project(state, project_id):
                return
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(project_ids):
                await _switch_chat_project(state, project_ids[idx - 1], restore_session=False)
                return
            console.print("[warning]Invalid project number.[/warning]")
            continue
        if choice in project_ids or _cli_project_exists(state.engine, choice):
            await _switch_chat_project(state, choice, restore_session=False)
            return
        console.print(f"[warning]Project not found: {choice}. Type `new {choice}` to create it.[/warning]")


async def _choose_initial_session(state: _InteractiveChatState) -> None:
    sessions = await _load_recent_primary_sessions(
        getattr(state.engine, "store", None),
        _current_project_id(state.engine),
        limit=20,
    )
    while True:
        _render_session_picker(sessions)
        try:
            choice = console.input("[bold]Session[/bold] [new]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        lowered = choice.lower()
        if lowered in {"quit", "exit", "/quit", "/exit"}:
            raise KeyboardInterrupt
        if not choice or lowered in {"new", "create", "+"}:
            _start_new_chat_session(state)
            return
        if lowered.startswith("/session"):
            try:
                await _handle_session_slash(state, _slash_parts(choice)[1:])
            except ValueError as exc:
                console.print(f"[warning]Could not parse command: {exc}[/warning]")
            if state.session_id:
                return
            sessions = await _load_recent_primary_sessions(
                getattr(state.engine, "store", None),
                _current_project_id(state.engine),
                limit=20,
            )
            continue
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(sessions):
                await _resume_chat_session(state, str(getattr(sessions[idx - 1], "session_id", "") or ""))
                return
            console.print("[warning]Invalid session number.[/warning]")
            continue
        matching = next((item for item in sessions if str(getattr(item, "session_id", "") or "") == choice), None)
        if matching:
            await _resume_chat_session(state, choice)
            return
        console.print(f"[warning]Session not found: {choice}. Type `new` to create a new session.[/warning]")


async def _run_interactive_startup_selector(state: _InteractiveChatState, *, explicit_project: bool = False) -> None:
    await _choose_initial_project(state, explicit_project=explicit_project)
    state.session_id = ""
    await _choose_initial_session(state)
    profile = f" company_profile={state.company_profile}" if state.mode == "company" else ""
    console.print(
        f"[success]Ready:[/success] project={_current_project_id(state.engine)} "
        f"session={state.session_id} mode={state.mode}{profile} agent={state.preferred_agent or 'system'}"
    )
    _print_chat_hint()


async def _handle_project_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print(f"[info]Current project: {_current_project_id(state.engine)}[/info]")
        _render_project_list(state)
        console.print("[dim]Use /project list, /project switch <id>, /project <id>, /project create <id>, /project rename <old_id> <new_id>, or /project delete <id> --yes.[/dim]")
        return
    subcommand = args[0].lower()
    if subcommand in {"list", "ls"}:
        _render_project_list(state)
        return
    if subcommand in {"create", "new"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project create <id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.project.create(args[1], active_project_id=_current_project_id(state.engine)))
        if payload:
            console.print(f"[success]Created project: {payload.get('project_id')}[/success]")
        return
    if subcommand in {"rename", "mv"}:
        if len(args) < 3:
            console.print("[warning]Usage: /project rename <old_id> <new_id>[/warning]")
            return
        old_id, new_id = args[1], args[2]
        renaming_current = old_id == _current_project_id(state.engine)
        payload = await _run_chat_office_service(state, lambda svc: svc.project.rename(old_id, new_id))
        if payload:
            console.print(f"[success]Renamed project:[/success] {payload.get('old_project_id')} -> {payload.get('new_project_id') or payload.get('project_id')}")
            if renaming_current:
                await _switch_chat_project(state, new_id)
        return
    if subcommand in {"delete", "rm"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project delete <id> --yes[/warning]")
            return
        remaining, ok = _require_yes_arg(args[2:], usage="/project delete <id> --yes")
        if remaining or not ok:
            return
        deleting_current = args[1] == _current_project_id(state.engine)
        payload = await _run_chat_office_service(state, lambda svc: svc.project.delete(args[1]))
        if payload:
            console.print(f"[success]Deleted project: {payload.get('project_id')}[/success]")
            if deleting_current:
                await _switch_chat_project(state, "default")
        return
    if subcommand in {"switch", "use"}:
        if len(args) < 2:
            console.print("[warning]Usage: /project switch <id>[/warning]")
            return
        await _switch_chat_project(state, args[1])
        return
    if len(args) == 1:
        await _switch_chat_project(state, args[0])
        return
    console.print("[warning]Usage: /project [list|create <id>|rename <old_id> <new_id>|switch <id>|delete <id> --yes][/warning]")


async def _render_sessions(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
        if args and args[0].isdigit():
            limit = _coerce_limit(args[0])
            args = args[1:]
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if args:
        console.print("[warning]Usage: /session list [limit][/warning]")
        return
    sessions = await state.engine.store.list_sessions(
        project_id=_current_project_id(state.engine),
        parent_session_id=None,
        limit=limit,
    )
    if not sessions:
        console.print("[info]No sessions found for this project.[/info]")
        return
    table = Table(title=f"Recent Sessions ({len(sessions)})")
    table.add_column("Session ID")
    table.add_column("Current", justify="center")
    table.add_column("Title")
    table.add_column("Mode")
    table.add_column("Status")
    table.add_column("Updated")
    current_session = str(state.session_id or "")
    for item in sessions:
        session_id = str(getattr(item, "session_id", "") or "")
        table.add_row(
            session_id,
            "*" if session_id == current_session else "",
            _clip_text(getattr(item, "title", "") or "(untitled)", 80, full=full),
            str(getattr(item, "mode", "") or ""),
            str(getattr(item, "status", "") or ""),
            _format_datetime(getattr(item, "updated_at", None)),
        )
    console.print(table)
    console.print("[dim]Switch with /session resume <session_id> or /session <session_id>. Create with /session new.[/dim]")


async def _resume_chat_session(state: _InteractiveChatState, session_id: str) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    if not session_id:
        console.print("[warning]Usage: /session resume <session_id>[/warning]")
        return
    target = await state.engine.store.get_session(session_id)
    if not target:
        console.print(f"[warning]Session not found: {session_id}[/warning]")
        return
    current_project = _current_project_id(state.engine)
    target_project = str(getattr(target, "project_id", "") or "default")
    if target_project != current_project:
        console.print(
            "[warning]"
            f"Session {session_id} belongs to project '{target_project}'. "
            f"Switch project first with /project {target_project}."
            "[/warning]"
        )
        return
    state.session_id = session_id
    title = getattr(target, "title", "") or "(untitled)"
    console.print(f"[info]Resumed session: {state.session_id} :: {title}[/info]")


def _start_new_chat_session(state: _InteractiveChatState) -> None:
    state.session_id = str(uuid.uuid4())
    console.print(f"[info]Started a new session: {state.session_id}[/info]")


def _render_transcript_parts(parts: list[Any]) -> str:
    lines: list[str] = []
    for part in parts:
        part_type = getattr(part, "part_type", "")
        payload = getattr(part, "payload", {})
        if isinstance(part, dict):
            part_type = str(part.get("part_type", part_type) or "")
            payload = part.get("payload", payload)
        if not isinstance(payload, dict):
            payload = {}
        if part_type == "text":
            text = payload.get("text", "")
            if text:
                lines.append(str(text))
        elif part_type in {"subtask_result", "task_result"}:
            title = payload.get("task_title", "Task")
            summary = payload.get("summary", "")
            lines.append(f"{title}: {summary}".strip(": "))
    return "\n".join(line for line in lines if line).strip()


def _render_transcript_table(transcript: list[dict[str, Any]], *, limit: int) -> None:
    rows: list[tuple[str, str, str, str]] = []
    for item in transcript:
        message = item.get("message") if isinstance(item, dict) else None
        if not message or getattr(message, "summary_flag", False):
            continue
        content = _render_transcript_parts(item.get("parts", []))
        if not content:
            continue
        role = str(getattr(message, "role", "") or "").strip().lower()
        agent_id = str(getattr(message, "agent_id", "") or "").strip()
        sender = {
            "user": "You",
            "assistant": "OPC",
            "system": "System",
            "subagent": agent_id.replace("_", " ").replace("-", " ").title() if agent_id else "Subagent",
        }.get(role, agent_id or role.title() or "OPC")
        rows.append((
            _format_datetime(getattr(message, "created_at", None)),
            role or "assistant",
            sender,
            _clip_text(content),
        ))
    rows = rows[-limit:]
    if not rows:
        console.print("[info]No transcript messages found.[/info]")
        return
    table = Table(title=f"Transcript (last {len(rows)})")
    table.add_column("Created")
    table.add_column("Role")
    table.add_column("Sender")
    table.add_column("Content")
    for row in rows:
        table.add_row(*row)
    console.print(table)


async def _resolve_session_or_task(state: _InteractiveChatState, token: str) -> tuple[Any | None, Any | None, str]:
    store = getattr(state.engine, "store", None)
    if not store:
        return None, None, ""
    session = await store.get_session(token) if hasattr(store, "get_session") else None
    task = None
    session_id = str(getattr(session, "session_id", "") or "") if session else ""
    if not session and hasattr(store, "get_task"):
        task = await store.get_task(token)
        session_id = str(getattr(task, "session_id", "") or "") if task else ""
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
    return session, task, session_id


async def _resolve_runtime_control_target(state: _InteractiveChatState, target: str = "") -> tuple[str, str]:
    from opc.layer2_organization.company_runtime_identity import load_company_runtime_identity_index

    store = _require_chat_store(state, label="Session store")
    if store is None:
        return "", ""
    raw_target = str(target or "").strip() or str(state.session_id or "").strip()
    if not raw_target:
        console.print("[warning]No current session. Use /session list or /session create first.[/warning]")
        return "", ""
    project_id = _current_project_id(state.engine)
    identity_index = await load_company_runtime_identity_index(store, project_id)
    task = await store.get_task(raw_target) if hasattr(store, "get_task") else None
    if task is not None:
        task_project_id = str(getattr(task, "project_id", "") or "default")
        if task_project_id != project_id:
            console.print(f"[warning]Target belongs to project '{task_project_id}'. Switch project first.[/warning]")
            return "", ""
        runtime_identity = identity_index.resolve(task_id=raw_target)
        if runtime_identity is not None:
            return raw_target, runtime_identity.runtime_session_id
        return str(getattr(task, "id", "") or ""), str(getattr(task, "session_id", "") or getattr(task, "parent_session_id", "") or "")
    runtime_identity = (
        identity_index.resolve(runtime_session_id=raw_target)
        or identity_index.resolve(task_session_id=raw_target)
    )
    if runtime_identity is not None:
        control_task_id = (
            runtime_identity.ui_anchor_task_id
            or runtime_identity.config_source_task_id
        )
        if control_task_id:
            return control_task_id, runtime_identity.runtime_session_id
    session = await store.get_session(raw_target) if hasattr(store, "get_session") else None
    if session is None:
        console.print(f"[warning]Task or session not found: {raw_target}[/warning]")
        return "", ""
    session_project_id = str(getattr(session, "project_id", "") or "default")
    if session_project_id != project_id:
        console.print(f"[warning]Target belongs to project '{session_project_id}'. Switch project first.[/warning]")
        return "", ""
    tasks = list(identity_index.tasks)
    candidates = [
        item for item in tasks
        if str(getattr(item, "session_id", "") or "") == raw_target
    ]
    if not candidates:
        console.print(f"[warning]Session is not task-backed: {raw_target}[/warning]")
        return "", raw_target
    task_mode_anchor = min(
        candidates,
        key=lambda item: (
            bool(str(getattr(item, "parent_session_id", "") or "")),
            str(getattr(item, "created_at", "") or ""),
            str(getattr(item, "id", "") or ""),
        ),
    )
    return str(getattr(task_mode_anchor, "id", "") or ""), raw_target


def _make_cli_runtime_control_context(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> Any:
    from opc.plugins.office_ui.services import ModeState, OfficeServiceContext

    context = OfficeServiceContext(
        engine=state.engine,
        agent_store=None,
        chat_store=None,
        event_adapter=None,
        mode_state=ModeState(
            exec_mode=state.mode,
            company_profile=state.company_profile,
            task_preferred_agent=state.preferred_agent or "native",
        ),
    )
    context.session_to_task = state.session_to_task
    context.active_runtime_children = state.active_runtime_children

    def cancel_session_tasks(_task_id: str) -> None:
        if controller is not None and controller.active_task is not None and not controller.active_task.done():
            controller.active_task.cancel()

    context.cancel_session_tasks = cancel_session_tasks
    return context


async def _company_runtime_identity_for_session(
    state: _InteractiveChatState,
    session_id: str | None = None,
) -> Any | None:
    from opc.layer2_organization.company_runtime_identity import (
        load_company_runtime_identity_index,
    )

    runtime_session_id = str(session_id or state.session_id or "").strip()
    store = getattr(state.engine, "store", None)
    if not runtime_session_id or store is None:
        return None
    try:
        identity_index = await load_company_runtime_identity_index(
            store,
            _current_project_id(state.engine),
        )
        # The interactive session may be a role/work-item session.  Resolve it
        # as a task session after trying the canonical root session so both UI
        # roots and child channels converge on the same company runtime.
        identity = (
            identity_index.resolve(runtime_session_id=runtime_session_id)
            or identity_index.resolve(task_session_id=runtime_session_id)
        )
    except Exception:
        return None
    return identity


async def _latest_company_suspend_checkpoint(
    state: _InteractiveChatState,
    session_id: str | None = None,
) -> Any | None:
    identity = await _company_runtime_identity_for_session(state, session_id)
    return identity.checkpoint if identity is not None and identity.resumable else None


async def _company_runtime_execution_identity(
    state: _InteractiveChatState,
    runtime_identity: Any,
) -> Any:
    """Read execution configuration from the durable runtime config source."""
    from opc.plugins.office_ui.execution_identity import execution_identity_from_task

    config_source_task_id = str(
        getattr(runtime_identity, "config_source_task_id", "") or ""
    ).strip()
    store = getattr(state.engine, "store", None)
    if not config_source_task_id or store is None or not hasattr(store, "get_task"):
        raise RuntimeError("Company runtime has no durable configuration source.")
    config_task = await store.get_task(config_source_task_id)
    if config_task is None:
        raise RuntimeError("Company runtime configuration source no longer exists.")
    return execution_identity_from_task(
        config_task,
        default_exec_mode=state.mode,
        default_company_profile=state.company_profile,
        default_preferred_agent=state.preferred_agent or "native",
        default_org_id=state.org_id,
    )


async def _handle_stop_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if len(args) > 1:
        console.print("[warning]Usage: /stop [task_id|session_id][/warning]")
        return
    target = args[0] if args else ""
    task_id, session_id = await _resolve_runtime_control_target(state, target)
    if not task_id:
        return
    from opc.plugins.office_ui.services import OfficeServices, ServiceError

    context = _make_cli_runtime_control_context(state, controller)
    try:
        result = await OfficeServices(context).session.stop(project_id=_current_project_id(state.engine), target=target or session_id or task_id)
    except ServiceError as exc:
        console.print(f"[warning]{escape(exc.message)}[/warning]")
        return
    payload = dict(result.payload)
    state.runtime_control_state = str(payload.get("runtime_control_state") or payload.get("status") or "stopped")
    state.runtime_control_task_id = str(payload.get("task_id") or task_id)
    state.runtime_control_session_id = str(payload.get("resume_parent_session_id") or payload.get("session_id") or session_id)
    state.runtime_control_checkpoint_id = str(payload.get("checkpoint_id") or "")
    console.print("[success]Stopped.[/success] [dim]Send a message to revise, or /continue to resume.[/dim]")


async def _runtime_control_execution_identity(state: _InteractiveChatState, task_id: str) -> Any:
    from opc.layer2_organization.company_runtime_identity import load_company_runtime_identity_index
    from opc.plugins.office_ui.execution_identity import execution_identity_from_task

    store = getattr(state.engine, "store", None)
    task = None
    if store is not None and task_id:
        try:
            identity_index = await load_company_runtime_identity_index(
                store,
                _current_project_id(state.engine),
            )
            runtime_identity = identity_index.resolve(task_id=task_id)
            task = identity_index.task(
                runtime_identity.config_source_task_id if runtime_identity is not None else task_id
            )
        except Exception:
            task = None
        if task is None and hasattr(store, "get_task"):
            try:
                task = await store.get_task(task_id)
            except Exception:
                task = None
    return execution_identity_from_task(
        task,
        default_exec_mode=state.mode,
        default_company_profile=state.company_profile,
        default_preferred_agent=state.preferred_agent or "native",
        default_org_id=state.org_id,
    )


async def _handle_continue_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    target = ""
    message_parts = list(args)
    if message_parts:
        first = message_parts[0]
        store = getattr(state.engine, "store", None)
        is_target = False
        if store is not None:
            try:
                is_target = bool((hasattr(store, "get_task") and await store.get_task(first)) or (hasattr(store, "get_session") and await store.get_session(first)))
            except Exception:
                is_target = False
        if is_target:
            target = message_parts.pop(0)
    task_id, session_id = await _resolve_runtime_control_target(state, target)
    if not task_id:
        return
    identity = await _runtime_control_execution_identity(state, task_id)
    content = " ".join(message_parts).strip() or "Resume the existing runtime."
    checkpoint = await _latest_company_suspend_checkpoint(state, session_id)
    if identity.exec_mode in {"company", "org", "custom"} and checkpoint is None:
        console.print(
            "[warning]No suspended or interrupted company runtime is available to continue.[/warning]"
        )
        return
    if controller is not None and controller.is_busy:
        if checkpoint is None and state.runtime_control_state not in {"suspended", "stopped"}:
            console.print("[warning]Busy: wait for the current turn or /stop it before /continue.[/warning]")
            return
    metadata: dict[str, Any] = {"ui_force_resume": True}
    if checkpoint is not None:
        metadata["response_to_checkpoint_id"] = str(getattr(checkpoint, "checkpoint_id", "") or "")
        metadata["response_to_checkpoint_type"] = str(
            getattr(checkpoint, "checkpoint_type", "") or "company_runtime_suspended"
        )
    item = QueuedChatInput(
        text=content,
        project_id=_current_project_id(state.engine),
        session_id=session_id or state.session_id,
        mode=identity.exec_mode,
        company_profile=identity.company_profile,
        org_id=identity.org_id,
        preferred_agent=identity.preferred_agent,
        domains=list(state.domains),
        message_metadata=metadata,
    )
    state.runtime_control_state = "resuming"
    state.runtime_control_task_id = task_id
    state.runtime_control_session_id = session_id or state.session_id
    state.runtime_control_checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
    if controller is not None:
        await controller.submit_item(item)
    else:
        previous_session_id = state.session_id
        previous_mode = state.mode
        previous_profile = state.company_profile
        previous_org_id = state.org_id
        previous_agent = state.preferred_agent
        state.session_id = session_id or state.session_id
        state.mode = identity.exec_mode
        state.company_profile = identity.company_profile
        state.org_id = identity.org_id
        state.preferred_agent = identity.preferred_agent
        try:
            await _process_interactive_chat_message(state, content, message_metadata=metadata)
        finally:
            state.session_id = previous_session_id
            state.mode = previous_mode
            state.company_profile = previous_profile
            state.org_id = previous_org_id
            state.preferred_agent = previous_agent


async def _show_session_detail(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Session store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if not args:
        console.print("[warning]Usage: /session show <session_id|task_id> [--limit N] [--full][/warning]")
        return
    session, task, session_id = await _resolve_session_or_task(state, args[0])
    if not session_id:
        console.print(f"[warning]Session or task not found: {args[0]}[/warning]")
        return
    project_id = str(getattr(session, "project_id", None) or getattr(task, "project_id", None) or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Target belongs to project '{project_id}'. Switch project first.[/warning]")
        return
    table = Table(title=f"Session {session_id}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Session ID", session_id)
    table.add_row("Project", project_id)
    if task:
        table.add_row("Task", str(getattr(task, "id", "") or ""))
        table.add_row("Task title", _clip_text(getattr(task, "title", "") or "(untitled)", 120, full=full))
    if session:
        table.add_row("Title", _clip_text(getattr(session, "title", "") or "(untitled)", 120, full=full))
        table.add_row("Mode", str(getattr(session, "mode", "") or ""))
        table.add_row("Status", str(getattr(session, "status", "") or ""))
        table.add_row("Updated", _format_datetime(getattr(session, "updated_at", None)))
    console.print(table)
    transcript = await state.engine.store.get_session_transcript(session_id)
    _render_transcript_table(transcript, limit=limit)


async def _handle_session_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if not args:
        console.print(f"[info]Current session: {state.session_id}[/info]")
        await _render_sessions(state, [])
        console.print("[dim]Use /session list, /session resume <session_id>, /session <session_id>, /session new, /session create [title], or /session delete <task_id> --yes.[/dim]")
        return
    subcommand = args[0].lower()
    rest = args[1:]
    known_subcommands = {
        "list", "ls", "new", "create", "resume", "show", "config", "send", "rename", "delete", "stop", "continue", "complete",
    }
    if len(args) == 1 and subcommand not in known_subcommands:
        await _resume_chat_session(state, args[0])
        return
    if subcommand in {"list", "ls"}:
        await _render_sessions(state, rest)
        return
    if subcommand == "new":
        _start_new_chat_session(state)
        return
    if subcommand == "create":
        try:
            rest, mode = _extract_option(rest, "--mode", default=state.mode)
            rest, profile = _extract_option(rest, "--company-profile", default=state.company_profile)
            rest, agent = _extract_option(rest, "--agent", default=state.preferred_agent)
            rest, org_id = _extract_option(rest, "--org", default=None)
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        title = " ".join(rest).strip() or "New Chat"
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.create(
                project_id=_current_project_id(state.engine),
                title=title,
                exec_mode=mode,
                company_profile=profile,
                preferred_agent=agent,
                org_id=org_id,
                interface="cli",
            ),
        )
        if payload:
            state.session_id = str(payload.get("session_id") or state.session_id)
            console.print(f"[success]Created session:[/success] {state.session_id} task={payload.get('task_id')}")
        return
    if subcommand == "resume":
        await _resume_chat_session(state, rest[0] if rest else "")
        return
    if subcommand == "show":
        await _show_session_detail(state, rest)
        return
    if subcommand == "config":
        if not rest:
            console.print("[warning]Usage: /session config <task_id> [--mode ...] [--agent ...] [--org ...][/warning]")
            return
        task_id = rest[0]
        opts = rest[1:]
        try:
            opts, mode = _extract_option(opts, "--mode", default=None)
            opts, profile = _extract_option(opts, "--company-profile", default=None)
            opts, agent = _extract_option(opts, "--agent", default=None)
            opts, org_id = _extract_option(opts, "--org", default=None)
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if opts:
            console.print("[warning]Usage: /session config <task_id> [--mode ...] [--agent ...] [--org ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.update_config(
                project_id=_current_project_id(state.engine),
                task_id=task_id,
                exec_mode=mode,
                company_profile=profile,
                preferred_agent=agent,
                org_id=org_id,
            ),
        )
        if payload:
            console.print(f"[success]Updated session config:[/success] {task_id}")
        return
    if subcommand == "send":
        if len(rest) < 2:
            console.print("[warning]Usage: /session send <task_id> <message>[/warning]")
            return
        task_id = rest[0]
        message = " ".join(rest[1:]).strip()
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.session.send(
                project_id=_current_project_id(state.engine),
                task_id=task_id,
                content=message,
                mode=state.mode,
                company_profile=state.company_profile,
                preferred_agent=state.preferred_agent,
                domains=list(state.domains),
            ),
        )
        if payload and payload.get("response"):
            _print_response(str(payload["response"]), state.no_markdown)
        return
    if subcommand == "rename":
        if len(rest) < 2:
            console.print("[warning]Usage: /session rename <task_id|session_id> <title>[/warning]")
            return
        await _rename_task_and_session(state, rest[0], " ".join(rest[1:]))
        return
    if subcommand == "delete":
        if not rest:
            console.print("[warning]Usage: /session delete <task_id> --yes[/warning]")
            return
        await _delete_task_and_session(state, rest[0], rest[1:], usage="/session delete <task_id> --yes")
        return
    if subcommand == "stop":
        await _handle_stop_slash(state, rest, controller=controller)
        return
    if subcommand == "continue":
        await _handle_continue_slash(state, rest, controller=controller)
        return
    if subcommand == "complete":
        if not rest:
            console.print("[warning]Usage: /session complete <task_id>[/warning]")
            return
        operation = lambda svc: svc.session.complete(project_id=_current_project_id(state.engine), task_id=rest[0])
        payload = await _run_chat_office_service(state, operation)
        if payload:
            console.print(f"[success]Completed session:[/success] {rest[0]}")
        return
    console.print("[warning]Usage: /session [list|new|create|resume <session_id>|show <session_id|task_id>|config <task_id>|send <task_id> <message>|rename <task_id|session_id> <title>|delete <task_id> --yes|stop [target]|continue [target] [message]|complete <task_id>][/warning]")


def _task_title(task: Any) -> str:
    return _clip_text(getattr(task, "title", "") or getattr(task, "description", "") or "(untitled)", 80)


def _task_title_for_view(task: Any, *, full: bool = False) -> str:
    return _clip_text(getattr(task, "title", "") or getattr(task, "description", "") or "(untitled)", 80, full=full)


def _require_chat_store(state: _InteractiveChatState, *, label: str = "Task store") -> Any | None:
    store = getattr(state.engine, "store", None)
    if not store:
        console.print(f"[warning]{label} is not available.[/warning]")
        return None
    return store


async def _get_task_for_current_project(state: _InteractiveChatState, task_id: str) -> Any | None:
    store = _require_chat_store(state)
    if store is None:
        return None
    if not task_id:
        console.print("[warning]Missing task id.[/warning]")
        return None
    task = await store.get_task(task_id)
    if not task:
        console.print(f"[warning]Task not found: {task_id}[/warning]")
        return None
    project_id = str(getattr(task, "project_id", "") or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Task belongs to project '{project_id}'. Switch project first with /project {project_id}.[/warning]")
        return None
    return task


def _parse_delete_yes(args: list[str], *, usage: str) -> bool:
    unknown = [arg for arg in args if arg != "--yes"]
    if unknown:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return False
    if "--yes" not in args:
        console.print(f"[warning]Destructive command requires --yes. Usage: {usage}[/warning]")
        return False
    return True


def _extract_option(args: list[str], name: str, *, default: str | None = None) -> tuple[list[str], str | None]:
    remaining: list[str] = []
    value = default
    idx = 0
    prefix = f"{name}="
    while idx < len(args):
        token = args[idx]
        if token == name:
            if idx + 1 >= len(args):
                raise ValueError(f"Missing value for {name}.")
            value = args[idx + 1]
            idx += 2
            continue
        if token.startswith(prefix):
            value = token.split("=", 1)[1]
            idx += 1
            continue
        remaining.append(token)
        idx += 1
    return remaining, value


def _require_yes_arg(args: list[str], *, usage: str) -> tuple[list[str], bool]:
    remaining = [arg for arg in args if arg != "--yes"]
    if "--yes" not in args:
        console.print(f"[warning]Destructive command requires --yes. Usage: {usage}[/warning]")
        return remaining, False
    return remaining, True


def _save_cli_config(state: _InteractiveChatState) -> None:
    state.config.save(get_opc_home() / "config")


def _refresh_cli_org_runtime(state: _InteractiveChatState) -> None:
    engine = state.engine
    if hasattr(engine, "config"):
        engine.config = state.config
    market = getattr(engine, "talent_market", None)
    if market is not None and hasattr(market, "config"):
        market.config = state.config
    org_engine = getattr(engine, "org_engine", None)
    if org_engine is None:
        return
    try:
        if hasattr(org_engine, "config"):
            org_engine.config = state.config
        reload_from_config = getattr(org_engine, "reload_from_config", None)
        if callable(reload_from_config):
            reload_from_config()
        configure_tools = getattr(org_engine, "configure_task_mode_tools", None)
        tool_names = getattr(engine, "_task_mode_tool_names", None)
        if callable(configure_tools) and callable(tool_names):
            configure_tools(tool_names())
    except Exception as exc:
        console.print(f"[warning]Saved config, but current org runtime refresh failed: {exc}. Restart opc chat if needed.[/warning]")


def _coerce_task_status(value: Any) -> Any | None:
    from opc.core.models import TaskStatus

    raw = value.value if hasattr(value, "value") else str(value or "")
    try:
        return TaskStatus(raw)
    except ValueError:
        return None


async def _transition_task_status(
    state: _InteractiveChatState,
    *,
    task_id: str,
    target_status: Any,
    reason: str,
    target_label: str,
) -> None:
    from opc.core.models import TaskStatus
    from opc.layer2_organization.work_item_transition import apply_task_status_transition

    store = _require_chat_store(state)
    if store is None:
        return
    task = await _get_task_for_current_project(state, task_id)
    if task is None:
        return

    old_status = _coerce_task_status(getattr(task, "status", None))
    terminal_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    if old_status in terminal_statuses and target_status not in terminal_statuses:
        old_label = old_status.value if old_status else _value_text(getattr(task, "status", ""))
        console.print(f"[warning]Cannot move terminal task {old_label} back to {target_label}.[/warning]")
        return

    old_label = _value_text(getattr(task, "status", ""))
    try:
        await apply_task_status_transition(
            store,
            task,
            target_status_or_phase=target_status,
            reason=reason,
        )
    except Exception as exc:
        console.print(f"[error]Failed to transition task {task_id}: {exc}[/error]")
        return
    console.print(f"[success]Task {task_id} moved: {old_label} -> {target_status.value}[/success]")


async def _rename_task_and_session(
    state: _InteractiveChatState,
    target: str,
    title: str,
    *,
    usage: str = "/session rename <task_id|session_id> <title>",
) -> None:
    store = _require_chat_store(state)
    if store is None:
        return
    title = str(title or "").strip()
    if not title:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return
    target = str(target or "").strip()
    if not target:
        console.print(f"[warning]Usage: {usage}[/warning]")
        return
    current_project = _current_project_id(state.engine)
    task = await store.get_task(target) if hasattr(store, "get_task") else None
    if task and str(getattr(task, "project_id", "") or "default") != current_project:
        console.print(f"[warning]Task belongs to project '{getattr(task, 'project_id', '')}'. Switch project first with /project {getattr(task, 'project_id', '')}.[/warning]")
        return
    session = None
    session_id = ""
    if task is None and hasattr(store, "get_session"):
        session = await store.get_session(target)
        if session and str(getattr(session, "project_id", "") or "default") != current_project:
            console.print(f"[warning]Session belongs to project '{getattr(session, 'project_id', '')}'. Switch project first with /project {getattr(session, 'project_id', '')}.[/warning]")
            return
        if session and hasattr(store, "get_tasks"):
            session_id = str(getattr(session, "session_id", "") or target)
            tasks = await store.get_tasks(project_id=current_project)
            task = next((item for item in tasks if str(getattr(item, "session_id", "") or "") == session_id), None)
    if task is None and session is None:
        console.print(f"[warning]Task or session not found: {target}[/warning]")
        return
    if task is not None:
        if not hasattr(store, "save_task"):
            console.print("[warning]Task store cannot save tasks.[/warning]")
            return
        task.title = title
        await store.save_task(task)
        session_id = str(getattr(task, "session_id", "") or session_id)
    memory = getattr(state.engine, "memory", None)
    if session_id and memory and hasattr(memory, "update_session_title"):
        await memory.update_session_title(session_id, title)
    elif session is not None and hasattr(store, "save_session"):
        session.title = title
        await store.save_session(session)
        session_id = str(getattr(session, "session_id", "") or session_id)
    task_id = str(getattr(task, "id", "") or "") if task is not None else ""
    console.print(f"[success]Renamed {f'task {task_id}' if task_id else 'session'}{f' / session {session_id}' if session_id else ''}: {title}[/success]")


async def _delete_task_and_session(state: _InteractiveChatState, task_id: str, args: list[str], *, usage: str) -> None:
    store = _require_chat_store(state)
    if store is None:
        return
    if not _parse_delete_yes(args, usage=usage):
        return
    if not hasattr(store, "hard_delete_task"):
        console.print("[warning]Task store cannot hard-delete tasks.[/warning]")
        return
    task = await _get_task_for_current_project(state, task_id)
    if task is None:
        return
    session_id = str(getattr(task, "session_id", "") or "")
    await store.hard_delete_task(task_id, session_id or None)
    console.print(f"[success]Deleted task {task_id}{f' / session {session_id}' if session_id else ''}.[/success]")


async def _handle_task_move_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if len(args) != 2:
        console.print("[warning]Usage: /task move <task_id> todo|in-progress|done|blocked|failed|cancelled[/warning]")
        return
    from opc.presentation.kanban import column_to_task_status

    target_label = args[1].strip().lower().replace("_", "-")
    target_status = column_to_task_status(target_label)
    if target_status is None:
        console.print("[warning]Unsupported target. Use todo, in-progress, done, blocked, failed, or cancelled.[/warning]")
        return
    await _transition_task_status(
        state,
        task_id=args[0],
        target_status=target_status,
        reason="cli_chat_task_move",
        target_label=target_label,
    )


async def _handle_task_done_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if len(args) != 1:
        console.print("[warning]Usage: /task done <task_id>[/warning]")
        return
    from opc.core.models import TaskStatus

    await _transition_task_status(
        state,
        task_id=args[0],
        target_status=TaskStatus.DONE,
        reason="cli_chat_task_done",
        target_label="done",
    )


async def _handle_tasks_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Task store is not available.[/warning]")
        return
    from opc.core.models import TaskStatus

    try:
        args, limit, full = _parse_view_args(args)
        if args and args[0].isdigit():
            limit = _coerce_limit(args[0])
            args = args[1:]
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    status = None
    if args:
        status_token = args[0].strip().lower().replace("-", "_")
        try:
            status = TaskStatus(status_token)
        except ValueError:
            valid = ", ".join(item.value for item in TaskStatus)
            console.print(f"[warning]Unknown task status '{args[0]}'. Valid: {valid}[/warning]")
            return
    tasks = await state.engine.store.get_tasks(project_id=_current_project_id(state.engine), status=status)
    tasks = tasks[:limit]
    if not tasks:
        console.print("[info]No tasks found for this project.[/info]")
        return
    table = Table(title=f"Tasks ({len(tasks)})")
    table.add_column("Task ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority", justify="right")
    table.add_column("Assigned")
    table.add_column("Session ID")
    table.add_column("Created")
    for task in tasks:
        table.add_row(
            str(getattr(task, "id", "") or ""),
            _task_title_for_view(task, full=full),
            _value_text(getattr(task, "status", "")),
            str(getattr(task, "priority", "") or ""),
            str(getattr(task, "assigned_to", "") or ""),
            str(getattr(task, "session_id", "") or ""),
            _format_datetime(getattr(task, "created_at", None)),
        )
    console.print(table)


async def _show_task_detail(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Task store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if not args:
        console.print("[warning]Usage: /task show <task_id> [--limit N] [--full][/warning]")
        return
    task = await state.engine.store.get_task(args[0])
    if not task:
        console.print(f"[warning]Task not found: {args[0]}[/warning]")
        return
    project_id = str(getattr(task, "project_id", "") or "default")
    if project_id != _current_project_id(state.engine):
        console.print(f"[warning]Task belongs to project '{project_id}'. Switch project first.[/warning]")
        return
    table = Table(title=f"Task {getattr(task, 'id', '')}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Title", _clip_text(getattr(task, "title", "") or "(untitled)", 120, full=full))
    table.add_row("Description", _clip_text(getattr(task, "description", "") or "", 240, full=full))
    table.add_row("Status", _value_text(getattr(task, "status", "")))
    table.add_row("Project", project_id)
    table.add_row("Session", str(getattr(task, "session_id", "") or ""))
    table.add_row("Assignee", str(getattr(task, "assigned_to", "") or ""))
    table.add_row("Priority", str(getattr(task, "priority", "") or ""))
    tags = getattr(task, "tags", []) or []
    table.add_row("Tags", ", ".join(str(tag) for tag in tags) if tags else "")
    result = getattr(task, "result", None) or {}
    if isinstance(result, dict):
        table.add_row("Result", _clip_text(result.get("content", "") or result.get("summary", ""), 240, full=full))
        artifacts = result.get("artifacts", []) or []
        if not isinstance(artifacts, list):
            artifacts = [artifacts]
        table.add_row("Artifacts", _clip_text(", ".join(str(item) for item in artifacts), 240, full=full))
    metadata = getattr(task, "metadata", {}) or {}
    table.add_row("Metadata keys", ", ".join(sorted(str(key) for key in metadata.keys())) if isinstance(metadata, dict) else "")
    console.print(table)
    session_id = str(getattr(task, "session_id", "") or "")
    if session_id:
        transcript = await state.engine.store.get_session_transcript(session_id)
        _render_transcript_table(transcript, limit=limit)


async def _handle_task_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /task [show|move|done|rename|delete] ...[/warning]")
        return
    subcommand = args[0].lower()
    if subcommand == "show":
        await _show_task_detail(state, args[1:])
        return
    if subcommand == "move":
        await _handle_task_move_slash(state, args[1:])
        return
    if subcommand == "done":
        await _handle_task_done_slash(state, args[1:])
        return
    if subcommand == "rename":
        rest = args[1:]
        if len(rest) < 2:
            console.print("[warning]Usage: /task rename <task_id> <title>[/warning]")
            return
        await _rename_task_and_session(state, rest[0], " ".join(rest[1:]), usage="/task rename <task_id> <title>")
        return
    if subcommand == "delete":
        rest = args[1:]
        if not rest:
            console.print("[warning]Usage: /task delete <task_id> --yes[/warning]")
            return
        await _delete_task_and_session(state, rest[0], rest[1:], usage="/task delete <task_id> --yes")
        return
    await _show_task_detail(state, args)


async def _handle_checkpoints_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not getattr(state.engine, "store", None):
        console.print("[warning]Checkpoint store is not available.[/warning]")
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    if args:
        console.print("[warning]Usage: /checkpoints [--limit N] [--full][/warning]")
        return
    checkpoints = await state.engine.store.get_pending_checkpoints(project_id=_current_project_id(state.engine))
    checkpoints = checkpoints[:limit]
    if not checkpoints:
        console.print("[info]No pending checkpoints for this project.[/info]")
        return
    table = Table(title=f"Pending Checkpoints ({len(checkpoints)})")
    table.add_column("Checkpoint ID")
    table.add_column("Type")
    table.add_column("Task ID")
    table.add_column("Session ID")
    table.add_column("Updated")
    table.add_column("Payload")
    for checkpoint in checkpoints:
        table.add_row(
            str(getattr(checkpoint, "checkpoint_id", "") or ""),
            str(getattr(checkpoint, "checkpoint_type", "") or ""),
            str(getattr(checkpoint, "task_id", "") or ""),
            str(getattr(checkpoint, "session_id", "") or ""),
            _format_datetime(getattr(checkpoint, "updated_at", None)),
            _json_summary(getattr(checkpoint, "payload", {}) or {}, full=full),
        )
    console.print(table)


def _render_org_slash(state: _InteractiveChatState) -> None:
    org = state.config.org
    summary = Table(title="Organization", show_header=False)
    summary.add_column("Field", style="cyan", no_wrap=True)
    summary.add_column("Value")
    summary.add_row("Organization", str(getattr(org, "organization_name", "") or getattr(org, "company_name", "") or ""))
    summary.add_row("Company", str(getattr(org, "company_name", "") or ""))
    summary.add_row("Default mode", str(getattr(org, "default_mode", "") or ""))
    summary.add_row("Company profile", str(getattr(org, "company_profile", "") or ""))
    summary.add_row("Topology", str(getattr(org, "topology", "") or ""))
    summary.add_row("Execution model", str(getattr(org, "execution_model", "") or ""))
    summary.add_row("Final decider", str(getattr(org, "final_decider_role_id", "") or ""))
    console.print(summary)

    roles = list(getattr(org, "roles", []) or [])
    if roles:
        table = Table(title=f"Roles ({len(roles)})")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Reports To")
        table.add_column("Type")
        table.add_column("Agent")
        table.add_column("Responsibility")
        for role in roles:
            table.add_row(
                str(getattr(role, "id", "") or ""),
                str(getattr(role, "name", "") or ""),
                str(getattr(role, "reports_to", "") or ""),
                str(getattr(role, "role_type", "") or ""),
                str(getattr(role, "preferred_external_agent", "") or ""),
                _clip_text(getattr(role, "responsibility", "") or "", 120),
            )
        console.print(table)

    employees = list(getattr(org, "employees", []) or [])
    if employees:
        table = Table(title=f"Employees ({len(employees)})")
        table.add_column("Employee ID")
        table.add_column("Name")
        table.add_column("Role")
        table.add_column("Template")
        table.add_column("Status")
        table.add_column("Agent")
        for employee in employees:
            table.add_row(
                str(getattr(employee, "employee_id", "") or ""),
                str(getattr(employee, "name", "") or ""),
                str(getattr(employee, "role_id", "") or ""),
                str(getattr(employee, "template_id", "") or ""),
                str(getattr(employee, "status", "") or ""),
                str(getattr(employee, "preferred_external_agent", "") or ""),
            )
        console.print(table)

    teams = list(getattr(org, "teams", []) or [])
    if teams:
        table = Table(title=f"Teams ({len(teams)})")
        table.add_column("Team ID")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Seat IDs")
        for team in teams:
            table.add_row(
                str(getattr(team, "team_id", "") or ""),
                str(getattr(team, "name", "") or ""),
                _clip_text(getattr(team, "description", "") or "", 100),
                ", ".join(str(item) for item in (getattr(team, "seat_ids", []) or [])),
            )
        console.print(table)

        seats = []
        for team in teams:
            for seat in getattr(team, "seats", []) or []:
                seats.append((team, seat))
        if seats:
            seat_table = Table(title=f"Seats ({len(seats)})")
            seat_table.add_column("Team")
            seat_table.add_column("Seat ID")
            seat_table.add_column("Name")
            seat_table.add_column("Role")
            seat_table.add_column("Manager")
            seat_table.add_column("Kind")
            for team, seat in seats:
                seat_table.add_row(
                    str(getattr(team, "team_id", "") or ""),
                    str(getattr(seat, "seat_id", "") or ""),
                    str(getattr(seat, "name", "") or ""),
                    str(getattr(seat, "role_id", "") or ""),
                    str(getattr(seat, "manager_role_id", "") or getattr(seat, "manager_seat_id", "") or ""),
                    str(getattr(seat, "seat_kind", "") or ""),
                )
            console.print(seat_table)


async def _handle_org_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args or args[0].lower() in {"info", "show"}:
        if not args:
            _render_org_slash(state)
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.info())
        if payload:
            _emit_payload(payload)
        return
    command = args[0].lower()
    rest = args[1:]

    if command == "role":
        if not rest:
            console.print("[warning]Usage: /org role add|update|delete|bulk-add ...[/warning]")
            return
        action = rest[0].lower()
        values = rest[1:]
        if action == "add":
            try:
                values, name = _extract_option(values, "--name")
                values, responsibility = _extract_option(values, "--responsibility", default="")
                values, reports_to = _extract_option(values, "--reports-to", default="owner")
            except ValueError as exc:
                console.print(f"[warning]{exc}[/warning]")
                return
            if len(values) != 1:
                console.print("[warning]Usage: /org role add <role_id> [--name ...] [--responsibility ...] [--reports-to ...][/warning]")
                return
            role_id = values[0]
            payload = await _run_chat_office_service(
                state,
                lambda svc: svc.org.add_role({
                    "role_id": role_id,
                    "name": name or role_id,
                    "responsibility": responsibility or "",
                    "reports_to": reports_to or "owner",
                }),
            )
        elif action == "update":
            try:
                values, name = _extract_option(values, "--name")
                values, responsibility = _extract_option(values, "--responsibility")
                values, reports_to = _extract_option(values, "--reports-to")
                values, can_spawn = _extract_option(values, "--can-spawn")
                values, tools = _extract_option(values, "--tools")
                values, agent = _extract_option(values, "--agent")
            except ValueError as exc:
                console.print(f"[warning]{exc}[/warning]")
                return
            if len(values) != 1:
                console.print("[warning]Usage: /org role update <role_id> [--name ...] [--responsibility ...][/warning]")
                return
            updates = {
                key: value
                for key, value in {
                    "name": name,
                    "responsibility": responsibility,
                    "reports_to": reports_to,
                    "preferred_external_agent": agent,
                }.items()
                if value is not None
            }
            if can_spawn is not None:
                updates["can_spawn"] = _split_csv(can_spawn)
            if tools is not None:
                updates["tools"] = _split_csv(tools)
            payload = await _run_chat_office_service(state, lambda svc: svc.org.update_role(values[0], updates))
        elif action == "delete":
            if not values:
                console.print("[warning]Usage: /org role delete <role_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(values[1:], usage="/org role delete <role_id> --yes")
            if not ok or remaining:
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.delete_role(values[0]))
        elif action == "bulk-add":
            if len(values) != 1:
                console.print("[warning]Usage: /org role bulk-add <json-or-yaml-file>[/warning]")
                return
            loaded = _load_structured_payload(file_path=values[0])
            roles = loaded.get("roles", []) if isinstance(loaded, dict) else loaded if isinstance(loaded, list) else []
            payload = await _run_chat_office_service(state, lambda svc: svc.org.bulk_add_roles(list(roles or [])))
        else:
            console.print("[warning]Usage: /org role add|update|delete|bulk-add ...[/warning]")
            return
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "policy" and rest[:1] == ["update"]:
        values = rest[1:]
        try:
            values, payload_text = _extract_option(values, "--payload")
            values, file_path = _extract_option(values, "--file")
            values, profile = _extract_option(values, "--profile", default="custom")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if values:
            console.print("[warning]Usage: /org policy update [--payload JSON/YAML|--file path] [--profile custom][/warning]")
            return
        data = _load_structured_payload(payload=payload_text, file_path=file_path)
        payload = await _run_chat_office_service(state, lambda svc: svc.org.update_runtime_policy(data, profile=profile or "custom"))
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "strategy" and rest[:1] == ["update"]:
        values = rest[1:]
        try:
            values, final_decider = _extract_option(values, "--final-decider")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if values:
            console.print("[warning]Usage: /org strategy update --final-decider <role_id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.update_org_strategy(final_decider_role_id=final_decider))
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "reset":
        remaining, ok = _require_yes_arg(rest, usage="/org reset --yes")
        if not ok or remaining:
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.org.reset_architecture())
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "saved":
        if not rest:
            console.print("[warning]Usage: /org saved list|save|load|delete ...[/warning]")
            return
        action = rest[0].lower()
        values = rest[1:]
        if action == "list":
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_list())
        elif action == "save":
            if not values:
                console.print("[warning]Usage: /org saved save <name> [--overwrite][/warning]")
                return
            overwrite = "--overwrite" in values[1:]
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_save_as(values[0], overwrite=overwrite))
        elif action == "load":
            if len(values) != 1:
                console.print("[warning]Usage: /org saved load <name>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_load(values[0]))
        elif action == "delete":
            if not values:
                console.print("[warning]Usage: /org saved delete <name> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(values[1:], usage="/org saved delete <name> --yes")
            if not ok or remaining:
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.org.saved_delete(values[0]))
        else:
            console.print("[warning]Usage: /org saved list|save|load|delete ...[/warning]")
            return
        if payload:
            _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    if command == "export":
        payload = await _run_chat_office_service(state, lambda svc: svc.org.export_config())
        if payload:
            _emit_payload(payload)
        return

    if command == "import":
        if not rest:
            console.print("[warning]Usage: /org import <path> [--dry-run][/warning]")
            return
        dry_run = "--dry-run" in rest[1:]
        raw = Path(rest[0]).read_text(encoding="utf-8")
        payload = await _run_chat_office_service(state, lambda svc: svc.org.import_config(raw, dry_run=dry_run))
        if payload:
            if not dry_run:
                _refresh_cli_org_runtime(state)
            _emit_payload(payload)
        return

    console.print("[warning]Usage: /org [info|role|policy|strategy|reset|saved|export|import] ...[/warning]")


def _render_talent_templates(templates: list[Any], *, title: str = "Talent Templates") -> None:
    if not templates:
        console.print("[info]No talent templates found.[/info]")
        return
    table = Table(title=f"{title} ({len(templates)})")
    table.add_column("Template ID")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Domains")
    table.add_column("Agent")
    table.add_column("Description")
    for template in templates:
        table.add_row(
            str(getattr(template, "id", "") or ""),
            str(getattr(template, "name", "") or ""),
            str(getattr(template, "category", "") or ""),
            ", ".join(str(item) for item in (getattr(template, "domains", []) or [])[:4]),
            str(getattr(template, "preferred_external_agent", "") or ""),
            _clip_text(getattr(template, "description", "") or "", 100),
        )
    console.print(table)


def _render_talent_employees(employees: list[Any]) -> None:
    if not employees:
        console.print("[info]No employees hired yet.[/info]")
        return
    table = Table(title=f"Employees ({len(employees)})")
    table.add_column("Employee ID")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Template")
    table.add_column("Status")
    table.add_column("Agent")
    for employee in employees:
        table.add_row(
            str(getattr(employee, "employee_id", "") or ""),
            str(getattr(employee, "name", "") or ""),
            str(getattr(employee, "role_id", "") or ""),
            str(getattr(employee, "template_id", "") or ""),
            str(getattr(employee, "status", "") or ""),
            str(getattr(employee, "preferred_external_agent", "") or ""),
        )
    console.print(table)


async def _handle_talent_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /talent [list|employees|scan|import|import-repo|hire|employee|import-agent] ...[/warning]")
        return
    market = TalentMarket(get_opc_home(), state.config)
    command = args[0].lower()
    rest = args[1:]
    try:
        if command in {"list", "templates"}:
            _render_talent_templates(market.list_templates())
            return
        if command == "employees":
            _render_talent_employees(market.list_employees())
            return
        if command == "scan":
            _render_talent_templates(market.scan_local_talent(), title="Local Talent Templates")
            return
        if command == "import":
            if not rest:
                console.print("[warning]Usage: /talent import <template_id...>[/warning]")
                return
            templates = market.import_local_templates(rest)
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Resolved {len(templates)} local talent templates.[/success]")
            _render_talent_templates(templates)
            return
        if command == "import-repo":
            if len(rest) != 1:
                console.print("[warning]Usage: /talent import-repo <path>[/warning]")
                return
            templates = market.import_from_repo(_talent_repo_path(rest[0]))
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Imported {len(templates)} talent templates from repo.[/success]")
            _render_talent_templates(templates)
            return
        if command == "hire":
            rest, employee_name = _extract_option(rest, "--name")
            if len(rest) != 2:
                console.print("[warning]Usage: /talent hire <template_id> <role_id> [--name ...][/warning]")
                return
            employee = market.hire_template(rest[0], rest[1], employee_name=employee_name)
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Hired {employee.name} into role `{employee.role_id}` as `{employee.employee_id}`.[/success]")
            return
        if command in {"employee", "detail"}:
            if len(rest) != 1:
                console.print("[warning]Usage: /talent employee <employee_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.talent.employee_detail(rest[0]))
            if payload:
                _emit_payload(payload)
            return
        if command in {"import-agent", "import-employee-as-agent"}:
            if len(rest) != 1:
                console.print("[warning]Usage: /talent import-agent <employee_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.talent.import_employee_as_agent(employee_id=rest[0]))
            if payload:
                _emit_payload(payload)
            return
    except Exception as exc:
        console.print(f"[error]Talent command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /talent [list|employees|scan|import|import-repo|hire|employee|import-agent] ...[/warning]")


def _package_value(package: Any, name: str, default: Any = "") -> Any:
    if hasattr(package, name):
        return getattr(package, name)
    if isinstance(package, dict):
        return package.get(name, default)
    return default


def _render_market_packages(packages: list[Any]) -> None:
    if not packages:
        console.print("[info]No packages installed.[/info]")
        return
    table = Table(title=f"Installed Packages ({len(packages)})")
    table.add_column("Package ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Roles")
    table.add_column("Templates")
    for package in packages:
        role_ids = _package_value(package, "role_ids", []) or []
        template_ids = _package_value(package, "template_ids", []) or []
        table.add_row(
            str(_package_value(package, "package_id", "?") or "?"),
            str(_package_value(package, "name", "") or ""),
            str(_package_value(package, "version", "") or ""),
            str(len(role_ids)),
            str(len(template_ids)),
        )
    console.print(table)


async def _handle_market_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /market [browse|preview|list|presets|apply-preset|install|uninstall|export] ...[/warning]")
        return
    command = args[0].lower()
    rest = args[1:]
    try:
        if command == "browse":
            payload = await _run_chat_office_service(state, lambda svc: svc.market.browse())
            if payload:
                _emit_payload(payload)
            return
        if command == "preview":
            if len(rest) != 1:
                console.print("[warning]Usage: /market preview <preset_id>[/warning]")
                return
            payload = await _run_chat_office_service(state, lambda svc: svc.market.preview(rest[0]))
            if payload:
                _emit_payload(payload)
            return
        if command == "list":
            _render_market_packages(list(getattr(state.config.org, "installed_packages", []) or []))
            return
        if command == "presets":
            from opc.market.architecture_registry import get_all_presets

            _render_architecture_presets(get_all_presets())
            return
        if command == "apply-preset":
            rest, strategy = _extract_option(rest, "--strategy", default="overwrite")
            if len(rest) != 1 or strategy not in {"namespace", "overwrite"}:
                console.print("[warning]Usage: /market apply-preset <preset_id> [--strategy namespace|overwrite][/warning]")
                return
            from opc.market.architecture_registry import apply_architecture_preset_to_config

            info = apply_architecture_preset_to_config(
                state.config,
                rest[0],
                strategy=strategy or "overwrite",
                clear_existing=True,
            )
            state.mode = "company"
            state.company_profile = "custom"
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(
                f"[success]Applied preset {info.package_id}: "
                f"{len(info.role_ids)} roles, {len(info.work_item_template_ids or info.template_ids)} templates.[/success]"
            )
            console.print("[info]Mode set to company; company_profile=custom[/info]")
            return
        if command == "install":
            rest, strategy = _extract_option(rest, "--strategy", default="namespace")
            if len(rest) != 1 or strategy not in {"namespace", "overwrite"}:
                console.print("[warning]Usage: /market install <path> [--strategy namespace|overwrite][/warning]")
                return
            from opc.market import PackageLoader, SandboxChecker

            loader = PackageLoader(state.config, get_opc_home())
            package = loader.load_from_path(Path(rest[0]))
            report = SandboxChecker().validate(package)
            for warning in report.warnings:
                console.print(f"[warning]{warning}[/warning]")
            if not report.passed:
                for error in report.errors:
                    console.print(f"[error]{error}[/error]")
                console.print("[error]Package failed security check. Installation aborted.[/error]")
                return
            conflicts = loader.detect_conflicts(package)
            if conflicts.has_conflicts:
                for role_id in conflicts.role_conflicts:
                    console.print(f"[warning]Role conflict: {role_id}[/warning]")
                for template_id in conflicts.template_conflicts:
                    console.print(f"[warning]Template conflict: {template_id}[/warning]")
            info = loader.install(package, strategy=strategy or "namespace")
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Installed {info.package_id}: {len(info.role_ids)} roles, {len(info.template_ids)} templates.[/success]")
            return
        if command == "uninstall":
            if not rest:
                console.print("[warning]Usage: /market uninstall <package_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(rest[1:], usage="/market uninstall <package_id> --yes")
            if not ok or remaining:
                if remaining:
                    console.print("[warning]Usage: /market uninstall <package_id> --yes[/warning]")
                return
            from opc.market import PackageLoader

            loader = PackageLoader(state.config, get_opc_home())
            if not loader.uninstall(rest[0]):
                console.print(f"[warning]Package not found: {rest[0]}[/warning]")
                return
            _save_cli_config(state)
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Uninstalled package {rest[0]}.[/success]")
            return
        if command == "export":
            rest, package_id = _extract_option(rest, "--id")
            rest, name = _extract_option(rest, "--name")
            rest, description = _extract_option(rest, "--desc", default="")
            rest, version = _extract_option(rest, "--version", default="1.0.0")
            rest, output_dir = _extract_option(rest, "--output-dir", default=".")
            if rest or not package_id or not name:
                console.print("[warning]Usage: /market export --id <package_id> --name <name> [--desc ...] [--version ...] [--output-dir ...][/warning]")
                return
            from opc.market import PackageExporter

            exporter = PackageExporter(state.config, get_opc_home())
            package = exporter.export_current(
                package_id=package_id,
                name=name,
                description=description or "",
                version=version or "1.0.0",
            )
            out_path = exporter.write_to_path(package, Path(output_dir or "."))
            console.print(f"[success]Exported package {package_id} to {out_path}.[/success]")
            return
    except Exception as exc:
        console.print(f"[error]Market command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /market [browse|preview|list|presets|apply-preset|install|uninstall|export] ...[/warning]")


def _render_reorg_proposals(proposals: list[Any]) -> None:
    if not proposals:
        console.print("[info]No reorg proposals found.[/info]")
        return
    table = Table(title=f"Reorg Proposals ({len(proposals)})")
    table.add_column("Proposal ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Risk")
    table.add_column("Scope")
    table.add_column("Updated")
    table.add_column("Summary")
    for proposal in proposals:
        table.add_row(
            str(getattr(proposal, "proposal_id", "") or ""),
            _clip_text(getattr(proposal, "title", "") or "", 50),
            _value_text(getattr(proposal, "status", "")),
            _value_text(getattr(proposal, "risk_level", "")),
            _value_text(getattr(proposal, "scope", "")),
            _format_datetime(getattr(proposal, "updated_at", None)),
            _clip_text(getattr(proposal, "summary", "") or "", 100),
        )
    console.print(table)


def _render_reorg_detail(proposal: Any) -> None:
    table = Table(title=f"Reorg {getattr(proposal, 'proposal_id', '')}", show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for field_name in ("proposal_id", "title", "status", "scope", "risk_level", "initiated_by", "summary", "rationale", "approval_notes"):
        table.add_row(field_name, _clip_text(_value_text(getattr(proposal, field_name, "")), 260))
    console.print(table)
    detail = {
        "changeset": getattr(getattr(proposal, "changeset", None), "__dict__", getattr(proposal, "changeset", None)),
        "impact_summary": getattr(proposal, "impact_summary", {}) or {},
        "metadata": getattr(proposal, "metadata", {}) or {},
    }
    console.print(json.dumps(detail, ensure_ascii=False, indent=2, default=str))


async def _handle_reorg_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print("[warning]Usage: /reorg [list|show|approve|deny|apply] ...[/warning]")
        return
    command = args[0].lower()
    rest = args[1:]
    try:
        if command == "list":
            if not getattr(state.engine, "store", None):
                console.print("[warning]Reorg store is not available.[/warning]")
                return
            rest, limit = _parse_limit_args(rest)
            if rest:
                console.print("[warning]Usage: /reorg list [--limit N][/warning]")
                return
            proposals = await state.engine.store.list_reorg_proposals(_current_project_id(state.engine), limit=limit)
            _render_reorg_proposals(proposals)
            return
        if command == "show":
            if len(rest) != 1:
                console.print("[warning]Usage: /reorg show <proposal_id>[/warning]")
                return
            proposal = await state.engine.show_company_reorg(rest[0])
            if not proposal:
                console.print(f"[warning]Reorg proposal not found: {rest[0]}[/warning]")
                return
            _render_reorg_detail(proposal)
            return
        if command in {"approve", "deny"}:
            rest, notes = _extract_option(rest, "--notes", default="")
            if len(rest) != 1:
                console.print(f"[warning]Usage: /reorg {command} <proposal_id> [--notes ...][/warning]")
                return
            approved = command == "approve"
            proposal = await state.engine.approve_company_reorg(
                rest[0],
                approved=approved,
                notes=notes or ("Approved via CLI." if approved else "Denied via CLI."),
            )
            console.print(f"[success]Reorg {proposal.proposal_id} {'approved' if approved else 'denied'}.[/success]")
            return
        if command == "apply":
            if not rest:
                console.print("[warning]Usage: /reorg apply <proposal_id> --yes[/warning]")
                return
            remaining, ok = _require_yes_arg(rest[1:], usage="/reorg apply <proposal_id> --yes")
            if not ok or remaining:
                if remaining:
                    console.print("[warning]Usage: /reorg apply <proposal_id> --yes[/warning]")
                return
            result = await state.engine.apply_company_reorg(rest[0])
            _refresh_cli_org_runtime(state)
            console.print(f"[success]Applied reorg {rest[0]}.[/success]")
            console.print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return
    except Exception as exc:
        console.print(f"[error]Reorg command failed: {exc}[/error]")
        return
    console.print("[warning]Usage: /reorg [list|show|approve|deny|apply] ...[/warning]")


def _render_key_value_table(title: str, rows: list[tuple[str, Any]], *, full: bool = False) -> None:
    table = Table(title=title, show_header=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in rows:
        table.add_row(key, _clip_text(_value_text(value), 260, full=full))
    console.print(table)


def _runtime_session_id_from_task(task: Any) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {})
    runtime_v2 = dict(metadata.get("runtime_v2", {}) or {})
    return str(runtime_v2.get("runtime_session_id", "") or "").strip()


def _row_runtime_session_id(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("runtime_session_id", "") or "").strip()
    return str(getattr(row, "runtime_session_id", "") or "").strip()


def _dedupe_runtime_rows(rows: list[Any]) -> list[Any]:
    seen: set[str] = set()
    results: list[Any] = []
    for row in rows:
        runtime_session_id = _row_runtime_session_id(row)
        if not runtime_session_id or runtime_session_id in seen:
            continue
        seen.add(runtime_session_id)
        results.append(row)
    return results


def _runtime_row_metadata(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row.get("metadata", {}) or {})
    return dict(getattr(row, "metadata", {}) or {})


def _runtime_row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


async def _handle_work_items_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        args = ["list"]
    command = args[0].lower()
    rest = args[1:]
    try:
        if command in {"list", "ls"}:
            rest, limit, full = _parse_view_args(rest, default=20)
            role_id = None
            status = None
            rest, role_id = _extract_option(rest, "--role", default=None)
            rest, status = _extract_option(rest, "--status", default=None)
            if rest:
                console.print("[warning]Usage: /work-items list [--role <role_id>] [--status <status>] [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.list(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                    role_id=role_id,
                    status=status,
                    limit=limit,
                ),
            )
            if payload:
                items = list(payload.get("items", []) or payload.get("work_items", []) or [])
                if not items:
                    console.print("[info]No work items found.[/info]")
                    return
                table = Table(title=f"Work Items ({len(items)})")
                table.add_column("Work Item")
                table.add_column("Title")
                table.add_column("Role")
                table.add_column("Status")
                table.add_column("Task")
                table.add_column("Session")
                for item in items:
                    table.add_row(
                        str(item.get("work_item_id", "") or item.get("id", "") or ""),
                        _clip_text(item.get("title", "") or "", 70, full=full),
                        str(item.get("role_id", "") or ""),
                        str(item.get("status", "") or item.get("phase", "") or ""),
                        str(item.get("task_id", "") or item.get("runtime_task_id", "") or ""),
                        str(item.get("session_id", "") or ""),
                    )
                console.print(table)
            return
        if command == "show":
            rest, limit, _full = _parse_view_args(rest, default=50)
            if len(rest) != 1:
                console.print("[warning]Usage: /work-items show <work_item_id> [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.show(project_id=_current_project_id(state.engine), work_item_id=rest[0], limit=limit),
            )
            if payload:
                _emit_payload(payload)
            return
        if command == "logs":
            rest, limit, _full = _parse_view_args(rest, default=50)
            rest, role_id = _extract_option(rest, "--role", default=None)
            work_item_id = rest[0] if rest else ""
            if len(rest) > 1:
                console.print("[warning]Usage: /work-items logs [work_item_id] [--role <role_id>] [--limit N][/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.logs(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                    work_item_id=work_item_id,
                    role_id=role_id or "",
                    limit=limit,
                ),
            )
            if payload:
                _render_work_item_logs_payload(payload, limit=limit, full=_full)
            return
        if command in {"role-status", "status-by-role"}:
            if rest:
                console.print("[warning]Usage: /work-items role-status[/warning]")
                return
            payload = await _run_chat_current_office_service(
                state,
                lambda svc: svc.work_item.status_by_role(
                    project_id=_current_project_id(state.engine),
                    session_id=state.session_id,
                ),
            )
            if payload:
                _emit_payload(payload)
            return
    except ValueError as exc:
        console.print(f"[warning]{exc}[/warning]")
        return
    console.print("[warning]Usage: /work-items list|show|logs|role-status ...[/warning]")


async def _list_runtime_sessions_for_scope(
    store: Any,
    *,
    project_id: str,
    task_id: str | None = None,
    session_id: str | None = None,
    limit: int = _SLASH_DEFAULT_LIMIT,
) -> list[Any]:
    if not hasattr(store, "list_runtime_sessions"):
        return []
    try:
        return await store.list_runtime_sessions(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
        )
    except TypeError:
        rows = await store.list_runtime_sessions(project_id=project_id)
        if task_id:
            rows = [row for row in rows if str(_runtime_row_value(row, "task_id", "") or "") == task_id]
        if session_id:
            rows = [row for row in rows if str(_runtime_row_value(row, "session_id", "") or "") == session_id]
        return rows[:limit]


def _render_runtime_sessions(rows: list[Any], *, title: str = "Runtime Sessions", full: bool = False) -> None:
    if not rows:
        console.print("[info]No runtime sessions found.[/info]")
        return
    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("Runtime ID")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Session")
    table.add_column("Updated")
    table.add_column("Summary")
    for row in rows:
        metadata = _runtime_row_metadata(row)
        table.add_row(
            _row_runtime_session_id(row),
            str(_runtime_row_value(row, "status", "") or ""),
            str(_runtime_row_value(row, "task_id", "") or ""),
            str(_runtime_row_value(row, "session_id", "") or ""),
            _format_datetime(_runtime_row_value(row, "updated_at", "")),
            _json_summary(metadata, full=full),
        )
    console.print(table)


def _render_external_sessions(rows: list[Any], *, full: bool = False) -> None:
    if not rows:
        console.print("[info]No external agent sessions found.[/info]")
        return
    table = Table(title=f"External Agent Sessions ({len(rows)})")
    table.add_column("Agent")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("OPC Session")
    table.add_column("Provider Session")
    table.add_column("Workspace")
    table.add_column("Updated")
    for row in rows:
        table.add_row(
            str(getattr(row, "agent_type", "") or ""),
            str(getattr(row, "status", "") or ""),
            str(getattr(row, "task_id", "") or ""),
            str(getattr(row, "opc_session_id", "") or ""),
            _clip_text(getattr(row, "session_id", "") or "", 48, full=full),
            _clip_text(getattr(row, "workspace_path", "") or "", 80, full=full),
            _format_datetime(getattr(row, "updated_at", None)),
        )
    console.print(table)


def _render_checkpoint_table(checkpoints: list[Any], *, title: str, full: bool = False) -> None:
    if not checkpoints:
        console.print("[info]No checkpoints found.[/info]")
        return
    table = Table(title=f"{title} ({len(checkpoints)})")
    table.add_column("Checkpoint ID")
    table.add_column("Status")
    table.add_column("Type")
    table.add_column("Task")
    table.add_column("Session")
    table.add_column("Updated")
    table.add_column("Payload")
    for checkpoint in checkpoints:
        table.add_row(
            str(getattr(checkpoint, "checkpoint_id", "") or ""),
            str(getattr(checkpoint, "status", "") or ""),
            str(getattr(checkpoint, "checkpoint_type", "") or ""),
            str(getattr(checkpoint, "task_id", "") or ""),
            str(getattr(checkpoint, "session_id", "") or ""),
            _format_datetime(getattr(checkpoint, "updated_at", None)),
            _json_summary(getattr(checkpoint, "payload", {}) or {}, full=full),
        )
    console.print(table)


async def _handle_runtime_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Runtime store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /runtime --limit 20.[/warning]")
        return
    if args:
        console.print("[warning]Usage: /runtime [--limit N] [--full]. Try /help.[/warning]")
        return

    snapshot = {}
    if hasattr(state.runtime_display, "status_snapshot"):
        snapshot = state.runtime_display.status_snapshot()
    if snapshot:
        _render_key_value_table(
            "Current Runtime Snapshot",
            [
                ("agent", snapshot.get("agent_id") or snapshot.get("role_id") or ""),
                ("task", snapshot.get("task_id") or ""),
                ("tool", snapshot.get("current_tool") or ""),
                ("queue", snapshot.get("queue_depth", 0)),
                ("stream_queue", snapshot.get("stream_queue_depth", 0)),
                ("context_remaining", f"{snapshot.get('context_remaining_pct')}%" if snapshot.get("context_remaining_pct") not in (None, "") else ""),
                ("turn_cost", f"${float(snapshot.get('turn_cost_usd', 0.0) or 0.0):.4f}"),
                ("session_cost", f"${float(snapshot.get('session_cost_usd', 0.0) or 0.0):.4f}"),
                ("approvals", snapshot.get("pending_permission_count", 0)),
                ("checkpoint", snapshot.get("checkpoint_hint", "")),
            ],
            full=full,
        )

    from opc.core.models import TaskStatus

    tasks = await store.get_tasks(project_id=_current_project_id(state.engine))
    terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    active_tasks = [task for task in tasks if getattr(task, "status", None) not in terminal]
    if active_tasks:
        table = Table(title=f"Active Tasks ({len(active_tasks[:limit])})")
        table.add_column("Task ID")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Agent")
        table.add_column("Session")
        for task in active_tasks[:limit]:
            table.add_row(
                str(getattr(task, "id", "") or ""),
                _clip_text(getattr(task, "title", "") or "", 70, full=full),
                _value_text(getattr(task, "status", "")),
                str(getattr(task, "assigned_to", "") or ""),
                str(getattr(task, "session_id", "") or ""),
            )
        console.print(table)
    else:
        console.print("[info]No active tasks for this project.[/info]")

    runtime_rows = await _list_runtime_sessions_for_scope(
        store,
        project_id=_current_project_id(state.engine),
        limit=limit,
    )
    _render_runtime_sessions(_dedupe_runtime_rows(runtime_rows), full=full)

    if hasattr(store, "list_external_sessions"):
        external = await store.list_external_sessions(project_id=_current_project_id(state.engine), limit=limit)
    else:
        external = []
    _render_external_sessions(external, full=full)

    checkpoints = await store.get_pending_checkpoints(project_id=_current_project_id(state.engine))
    _render_checkpoint_table(checkpoints[:limit], title="Pending Checkpoints", full=full)


async def _resolve_logs_target(
    state: _InteractiveChatState,
    target: str,
    *,
    limit: int,
) -> tuple[Any | None, Any | None, list[Any], str | None]:
    store = _require_chat_store(state, label="Log store")
    if store is None:
        return None, None, [], None
    project_id = _current_project_id(state.engine)
    task = await store.get_task(target) if hasattr(store, "get_task") else None
    session = None
    runtime_rows: list[Any] = []
    runtime_row = None
    if task:
        task_project = str(getattr(task, "project_id", "") or "default")
        if task_project != project_id:
            console.print(f"[warning]Task belongs to project '{task_project}'. Switch project first with /project {task_project}.[/warning]")
            return None, None, [], None
        session_id = str(getattr(task, "session_id", "") or "")
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
        runtime_rows.extend(await _list_runtime_sessions_for_scope(store, project_id=project_id, task_id=target, limit=limit))
        runtime_id = _runtime_session_id_from_task(task)
        if runtime_id and hasattr(store, "get_runtime_session"):
            row = await store.get_runtime_session(runtime_id)
            if row:
                runtime_rows.append(row)
        return task, session, _dedupe_runtime_rows(runtime_rows), None
    if hasattr(store, "get_session"):
        session = await store.get_session(target)
    if session:
        session_project = str(getattr(session, "project_id", "") or "default")
        if session_project != project_id:
            console.print(f"[warning]Session belongs to project '{session_project}'. Switch project first with /project {session_project}.[/warning]")
            return None, None, [], None
        runtime_rows.extend(await _list_runtime_sessions_for_scope(store, project_id=project_id, session_id=target, limit=limit))
        return None, session, _dedupe_runtime_rows(runtime_rows), None
    if hasattr(store, "get_runtime_session"):
        runtime_row = await store.get_runtime_session(target)
    if runtime_row:
        runtime_project = str(runtime_row.get("project_id", "") or "default")
        if runtime_project != project_id:
            console.print(f"[warning]Runtime session belongs to project '{runtime_project}'. Switch project first with /project {runtime_project}.[/warning]")
            return None, None, [], None
        task_id = str(runtime_row.get("task_id", "") or "")
        session_id = str(runtime_row.get("session_id", "") or "")
        if task_id and hasattr(store, "get_task"):
            task = await store.get_task(task_id)
        if session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
        return task, session, [runtime_row], None
    return None, None, [], target


def _render_task_progress(task: Any, *, limit: int, full: bool = False) -> None:
    if not task:
        return
    metadata = dict(getattr(task, "metadata", {}) or {})
    progress = list(metadata.get("progress_log", []) or [])
    if not progress:
        console.print("[info]No task progress log found.[/info]")
        return
    table = Table(title=f"Task Progress ({len(progress[-limit:])})")
    table.add_column("Entry")
    for entry in progress[-limit:]:
        table.add_row(_clip_text(entry, 300, full=full))
    console.print(table)


async def _render_runtime_log_tables(store: Any, runtime_rows: list[Any], *, limit: int, full: bool = False) -> None:
    runtime_ids = [_row_runtime_session_id(row) for row in runtime_rows if _row_runtime_session_id(row)]
    if not runtime_ids:
        console.print("[info]No runtime session logs found.[/info]")
        return
    _render_runtime_sessions(runtime_rows, title="Runtime Log Scope", full=full)
    for runtime_id in runtime_ids[:limit]:
        if hasattr(store, "list_runtime_events"):
            events = await store.list_runtime_events(runtime_id, limit=limit)
            if events:
                table = Table(title=f"Runtime Events: {runtime_id} ({len(events)})")
                table.add_column("Created")
                table.add_column("Type")
                table.add_column("Payload")
                for event in events:
                    table.add_row(
                        _format_datetime(event.get("created_at")),
                        str(event.get("event_type", "") or ""),
                        _json_summary(event.get("payload", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_transcript_entries"):
            entries = (await store.list_runtime_transcript_entries(runtime_id))[-limit:]
            if entries:
                table = Table(title=f"Runtime Transcript: {runtime_id} ({len(entries)})")
                table.add_column("Created")
                table.add_column("Role")
                table.add_column("Type")
                table.add_column("Content")
                for entry in entries:
                    table.add_row(
                        _format_datetime(entry.get("created_at")),
                        str(entry.get("role", "") or ""),
                        str(entry.get("entry_type", "") or ""),
                        _clip_text(entry.get("content", "") or "", 300, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_tool_calls"):
            calls = (await store.list_runtime_tool_calls(runtime_id))[-limit:]
            if calls:
                table = Table(title=f"Tool Calls: {runtime_id} ({len(calls)})")
                table.add_column("Created")
                table.add_column("Tool")
                table.add_column("Arguments")
                for call in calls:
                    table.add_row(
                        _format_datetime(call.get("created_at")),
                        str(call.get("tool_name", "") or ""),
                        _json_summary(call.get("arguments", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_tool_results"):
            results = (await store.list_runtime_tool_results(runtime_id))[-limit:]
            if results:
                table = Table(title=f"Tool Results: {runtime_id} ({len(results)})")
                table.add_column("Created")
                table.add_column("Tool")
                table.add_column("Payload")
                for result in results:
                    table.add_row(
                        _format_datetime(result.get("created_at")),
                        str(result.get("tool_name", "") or ""),
                        _json_summary(result.get("payload", {}) or {}, full=full),
                    )
                console.print(table)
        if hasattr(store, "list_runtime_permission_grants"):
            grants = (await store.list_runtime_permission_grants(runtime_session_id=runtime_id))[-limit:]
            if grants:
                table = Table(title=f"Permission Grants: {runtime_id} ({len(grants)})")
                table.add_column("Scope")
                table.add_column("Tool")
                table.add_column("Candidate")
                table.add_column("Created")
                for grant in grants:
                    table.add_row(
                        str(grant.get("scope", "") or ""),
                        str(grant.get("tool_name", "") or ""),
                        _clip_text(grant.get("candidate", "") or "", 120, full=full),
                        _format_datetime(grant.get("created_at")),
                    )
                console.print(table)


def _clip_log_text(value: Any, limit: int = 1600, *, full: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if full or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


_EXTERNAL_TRANSCRIPT_RE = re.compile(r"^\[External:(?P<agent>[^:\]]+):(?P<kind>[^\]]+)\]\s*(?P<body>.*)$", re.S)
_STAFFING_CONTROL_REPLIES = {"approve", "approved", "auto", "auto recruit", "deny", "stop", "cancel"}
_IMPORTANT_WORK_ITEM_EVENTS = {
    "approval_requested",
    "approval_resolved",
    "checkpoint_created",
    "checkpoint_resolved",
    "escalation_created",
    "escalation_resolved",
    "work_item_failed",
    "work_item_blocked",
    "work_item_completed",
    "runtime_failed",
    "error",
}


@dataclass
class _WorkItemLogCell:
    kind: str
    created_at: Any
    label: str
    content: str
    order: int = 0
    status: str = ""
    exit_code: Any = None


def _runtime_tool_payload_text(value: dict[str, Any], *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    for key in ("command", "cmd", "summary", "result_summary", "stdout", "stderr", "output", "text", "message"):
        if value.get(key):
            text = str(value.get(key) or "")
            if key in {"command", "cmd"}:
                return f"$ {text}"
            return text
    return _json_summary(value, limit=1200, full=full)


def _runtime_tool_command_text(value: Any, *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    for key in ("command", "cmd"):
        if value.get(key):
            return f"$ {str(value.get(key) or '').strip()}"
    return _runtime_tool_payload_text(value, full=full)


def _runtime_tool_result_status(value: Any) -> tuple[str, Any]:
    if not isinstance(value, dict):
        return "", None
    status = str(value.get("status", "") or "").strip()
    exit_code = value.get("exit_code")
    if exit_code is None:
        exit_code = value.get("returncode")
    if not status and "success" in value:
        status = "completed" if bool(value.get("success")) else "failed"
    if not status and exit_code is not None:
        status = "completed" if str(exit_code) == "0" else "failed"
    return status, exit_code


def _runtime_tool_result_text(value: Any, *, full: bool = False) -> str:
    if not isinstance(value, dict):
        return _clip_log_text(value, full=full)
    parts: list[str] = []
    for key in ("stdout", "stderr", "output", "result_summary", "summary", "text", "message"):
        if value.get(key):
            parts.append(str(value.get(key) or "").strip())
    if parts:
        return "\n".join(part for part in parts if part)
    return _json_summary(value, limit=1200, full=full)


def _runtime_agent_label(entry: dict[str, Any]) -> str:
    runtime_id = str(entry.get("runtime_session_id", "") or "")
    if ":" in runtime_id:
        prefix = runtime_id.split(":", 1)[0].strip()
        if prefix:
            return prefix
    return str(entry.get("role", "") or "runtime").strip() or "runtime"


def _parse_runtime_stream_event(content: str) -> dict[str, Any] | None:
    try:
        event = json.loads(str(content or "").strip())
    except Exception:
        return None
    if isinstance(event, dict) and isinstance(event.get("msg"), dict):
        event = event["msg"]
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type", "") or "").strip()
    return event if event_type else None


def _command_text_from_codex_item(item: dict[str, Any], *, full: bool = False) -> str:
    command = item.get("command")
    if isinstance(command, list):
        text = " ".join(str(part) for part in command if str(part).strip())
    else:
        text = str(command or "").strip()
    if not text:
        text = "command execution"
    return f"$ {_clip_log_text(text, limit=420, full=full)}"


def _codex_command_output_text(item: dict[str, Any], *, full: bool = False) -> str:
    output = str(item.get("aggregated_output", "") or "").strip()
    if not output:
        return ""
    if full:
        return output
    if len(output) > 600 and output[:1] in {"{", "["}:
        return "output omitted; use --full to inspect raw JSON output"
    return _clip_log_text(output, limit=900, full=False)


def _runtime_stream_event_cells(
    entry: dict[str, Any],
    *,
    full: bool = False,
    skip_tool_events: bool = False,
) -> list[_WorkItemLogCell]:
    event = _parse_runtime_stream_event(str(entry.get("content", "") or ""))
    if not event:
        return []
    event_type = str(event.get("type", "") or "").strip()
    created_at = entry.get("created_at")
    agent = _runtime_agent_label(entry)
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = str(item.get("type", "") or "").strip()

    if event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed"}:
        if not full:
            return []
        return [_WorkItemLogCell(
            kind="event",
            created_at=created_at,
            label=f"runtime {event_type}",
            content=_json_summary(event, limit=1000, full=full),
            order=50,
        )]

    if item_type == "agent_message" and event_type in {"item.completed", "item.started"}:
        text = str(item.get("text", "") or "").strip()
        if not text:
            return []
        return [_WorkItemLogCell(
            kind="thinking",
            created_at=created_at,
            label=agent,
            content=text,
            order=15,
        )]

    if item_type == "command_execution":
        if skip_tool_events:
            return []
        if event_type == "item.started":
            return [_WorkItemLogCell(
                kind="tool_call",
                created_at=created_at,
                label="command_execution",
                content=_command_text_from_codex_item(item, full=full),
                order=30,
            )]
        if event_type == "item.completed":
            status = str(item.get("status", "") or "").strip()
            exit_code = item.get("exit_code")
            content = _codex_command_output_text(item, full=full)
            return [_WorkItemLogCell(
                kind="tool_result",
                created_at=created_at,
                label="command_execution",
                content=content,
                order=35,
                status=status,
                exit_code=exit_code,
            )]

    if not full:
        return []
    return [_WorkItemLogCell(
        kind="event",
        created_at=created_at,
        label=f"runtime {event_type}",
        content=_json_summary(event, limit=1000, full=full),
        order=50,
    )]


def _is_staffing_transcript_noise(role: str, content: str) -> bool:
    normalized = " ".join(str(content or "").strip().lower().split())
    if not normalized:
        return False
    if role == "assistant" and normalized.startswith("company mode has a pending manual staffing selection"):
        return True
    if role == "user" and normalized in _STAFFING_CONTROL_REPLIES:
        return True
    return False


def _short_runtime_ref(value: str, *, full: bool = False) -> str:
    text = str(value or "").strip()
    if full or len(text) <= 48:
        return text
    parts = text.split("::")
    if len(parts) >= 3 and parts[0] == "role-runtime":
        return f"{parts[0]}::{parts[1][:8]}::{parts[-1]}"
    return _clip_text(text, 48, full=False)


def _event_is_high_signal(event_type: str) -> bool:
    normalized = str(event_type or "").strip().lower()
    if not normalized:
        return False
    if normalized in _IMPORTANT_WORK_ITEM_EVENTS:
        return True
    return any(token in normalized for token in ("approval", "checkpoint", "escalation", "fail", "error", "blocked"))


def _render_codex_block(content: Any, *, prefix: str = "  ", style: str = "", full: bool = False) -> None:
    body = _clip_log_text(content, full=full)
    if not body:
        return
    for line in body.splitlines():
        text = f"{prefix}{line}" if line else ""
        if style:
            console.print(f"[{style}]{escape(text)}[/{style}]")
        else:
            console.print(escape(text))


def _render_codex_assistant_body(content: str, *, full: bool = False) -> None:
    body = _clip_log_text(content, full=full)
    if not body:
        return
    try:
        console.print(Markdown(body))
    except Exception:
        _render_codex_block(body, prefix="  ", full=full)


def _render_work_item_log_cell(cell: _WorkItemLogCell, *, full: bool = False) -> None:
    time_text = _format_datetime(cell.created_at)
    timestamp = f"[dim]{escape(time_text)}[/dim] " if time_text else ""
    label = escape(cell.label)
    if cell.kind == "user":
        console.print(f"{timestamp}[success]user[/success]")
        _render_codex_block(cell.content, prefix="› ", style="success", full=full)
    elif cell.kind == "assistant":
        console.print(f"{timestamp}[agent]• {label}[/agent]")
        _render_codex_assistant_body(cell.content, full=full)
    elif cell.kind == "thinking":
        console.print(f"{timestamp}[dim]thinking {label}[/dim]")
        _render_codex_block(cell.content, prefix="  ", style="dim", full=full)
    elif cell.kind == "tool_call":
        text = cell.content.strip()
        if text.startswith("$ "):
            console.print(f"{timestamp}[tool]{escape(text)}[/tool]")
        else:
            console.print(f"{timestamp}[tool]tool {label}[/tool]")
            _render_codex_block(text, prefix="  ", full=full)
    elif cell.kind == "tool_result":
        heading = f"tool result {cell.label}".strip()
        status_bits: list[str] = []
        if cell.status:
            status_bits.append(f"status={cell.status}")
        if cell.exit_code is not None and str(cell.exit_code) != "":
            status_bits.append(f"exit_code={cell.exit_code}")
        suffix = f" ({', '.join(status_bits)})" if status_bits else ""
        style = "success" if cell.status in {"", "completed", "success", "succeeded"} else "error"
        console.print(f"{timestamp}[{style}]{escape(heading + suffix)}[/{style}]")
        _render_codex_block(cell.content, prefix="  ", full=full)
    elif cell.kind in {"permission", "event", "handoff"}:
        style = "warning" if cell.kind == "permission" else "info"
        console.print(f"{timestamp}[{style}]• {label}[/{style}]")
        _render_codex_block(cell.content, prefix="  ", full=full)
    else:
        console.print(f"{timestamp}[info]• {label}[/info]")
        _render_codex_block(cell.content, prefix="  ", full=full)


def _render_work_item_logs_payload(payload: dict[str, Any], *, limit: int, full: bool = False) -> None:
    title_bits = []
    if payload.get("role_id"):
        title_bits.append(f"role={payload['role_id']}")
    if payload.get("work_item_id"):
        title_bits.append(f"work_item={str(payload['work_item_id'])[:12]}")
    if payload.get("session_id"):
        title_bits.append(f"session={str(payload['session_id'])[:8]}")
    scope_title = "Work Item Logs" + (f" ({', '.join(title_bits)})" if title_bits else "")
    console.print(f"[bold]{escape(scope_title)}[/bold]")

    work_items = list(payload.get("work_items", []) or [])[:limit]
    for item in work_items:
        status = str(item.get("phase", "") or item.get("kanban_column", "") or "")
        runtime = str(item.get("role_runtime_session_id", "") or item.get("claimed_by_role_runtime_session_id", "") or "").strip()
        suffix = f" [{status}]" if status else ""
        if runtime:
            suffix += f" runtime={_short_runtime_ref(runtime, full=full)}"
        console.print(
            f"[dim]work-item[/dim] [bold]{escape(str(item.get('work_item_id', '') or '')[:12])}[/bold] "
            f"{escape(str(item.get('role_id', '') or ''))} "
            f"{escape(_clip_text(item.get('title', '') or '', 96, full=full))}{escape(suffix)}"
        )
    if work_items:
        console.print()

    timeline: list[_WorkItemLogCell] = []
    structured_tool_calls = list(payload.get("runtime_tool_calls", []) or [])

    for item in list(payload.get("transcript", []) or [])[-limit:]:
        message = item.get("message") if isinstance(item, dict) else None
        if not message or getattr(message, "summary_flag", False):
            continue
        content = _render_transcript_parts(item.get("parts", []))
        if not content:
            continue
        role = str(getattr(message, "role", "") or "assistant").strip().lower()
        if not full and _is_staffing_transcript_noise(role, content):
            continue
        agent_id = str(getattr(message, "agent_id", "") or "").strip()
        sender = {
            "user": "user",
            "assistant": "assistant",
            "system": "system",
            "subagent": agent_id or "subagent",
        }.get(role, agent_id or role or "assistant")
        timeline.append(_WorkItemLogCell(
            kind="user" if role == "user" else "assistant",
            created_at=getattr(message, "created_at", None),
            label=sender,
            content=content,
            order=10,
        ))

    for entry in list(payload.get("runtime_transcript_entries", []) or [])[-limit:]:
        content = str(entry.get("content", "") or "")
        if not content:
            continue
        stream_cells = _runtime_stream_event_cells(entry, full=full, skip_tool_events=bool(structured_tool_calls))
        if stream_cells:
            timeline.extend(stream_cells)
            continue
        if _parse_runtime_stream_event(content):
            continue
        label = str(entry.get("entry_type", "") or "runtime")
        kind = "assistant"
        order = 20
        match = _EXTERNAL_TRANSCRIPT_RE.match(content)
        if match:
            agent = match.group("agent")
            external_kind = match.group("kind").strip().lower()
            content = match.group("body").strip()
            label = agent
            if external_kind == "tool":
                if structured_tool_calls:
                    continue
                kind = "tool_call"
                order = 30
            elif external_kind == "thinking":
                kind = "thinking"
                order = 15
            elif external_kind in {"error", "failed", "failure"}:
                kind = "event"
                label = f"{agent} {external_kind}"
                order = 55
            else:
                kind = "assistant"
                label = agent
        timeline.append(_WorkItemLogCell(
            kind=kind,
            created_at=entry.get("created_at"),
            label=label,
            content=content,
            order=order,
        ))

    for call in structured_tool_calls[-limit:]:
        tool_name = str(call.get("tool_name", "") or "tool")
        timeline.append(_WorkItemLogCell(
            kind="tool_call",
            created_at=call.get("created_at"),
            label=tool_name,
            content=_runtime_tool_command_text(call.get("arguments", {}) or {}, full=full),
            order=30,
        ))

    for result in list(payload.get("runtime_tool_results", []) or [])[-limit:]:
        tool_name = str(result.get("tool_name", "") or "tool")
        status, exit_code = _runtime_tool_result_status(result.get("payload", {}) or {})
        timeline.append(_WorkItemLogCell(
            kind="tool_result",
            created_at=result.get("created_at"),
            label=tool_name,
            content=_runtime_tool_result_text(result.get("payload", {}) or {}, full=full),
            order=35,
            status=status,
            exit_code=exit_code,
        ))

    for event in list(payload.get("events", []) or [])[-limit:]:
        event_type = str(event.get("event_type", "") or "event")
        if not full and not _event_is_high_signal(event_type):
            continue
        work_item_id = str(event.get("work_item_id", "") or "")[:12]
        timeline.append(_WorkItemLogCell(
            kind="event",
            created_at=event.get("created_at"),
            label=f"event {event_type}",
            content=f"{work_item_id} {_json_summary(event.get('payload', {}) or {}, limit=1000, full=full)}".strip(),
            order=50,
        ))

    for event in list(payload.get("runtime_events", []) or [])[-limit:]:
        event_type = str(event.get("event_type", "") or "runtime")
        if not full and not _event_is_high_signal(event_type):
            continue
        timeline.append(_WorkItemLogCell(
            kind="event",
            created_at=event.get("created_at"),
            label=f"runtime {event_type}",
            content=_json_summary(event.get("payload", {}) or {}, limit=1000, full=full),
            order=50,
        ))

    for grant in list(payload.get("runtime_permission_grants", []) or [])[-limit:]:
        timeline.append(_WorkItemLogCell(
            kind="permission",
            created_at=grant.get("created_at"),
            label="permission",
            content=f"{grant.get('scope', '')} {grant.get('tool_name', '')}: {grant.get('candidate', '')}".strip(),
            order=45,
        ))

    for handoff in list(payload.get("handoffs", []) or [])[-limit:]:
        timeline.append(_WorkItemLogCell(
            kind="handoff",
            created_at=handoff.get("created_at"),
            label=f"handoff {handoff.get('from_role', '')}->{handoff.get('to_role', '')}",
            content=str(handoff.get("summary", "") or _json_summary(handoff, limit=1000, full=full)),
            order=40,
        ))

    if not timeline and not work_items:
        console.print("[info]No role/work-item logs found for the current session.[/info]")
        return

    timeline.sort(key=lambda item: (str(item.created_at or ""), item.order, item.label))
    for index, item in enumerate(timeline[-limit:]):
        if index:
            console.print()
        _render_work_item_log_cell(item, full=full)


async def _handle_logs_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Log store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /logs <task_id|session_id> --limit 20.[/warning]")
        return
    if len(args) != 1:
        console.print("[warning]Usage: /logs <task_id|session_id> [--limit N] [--full]. Try /session list or /tasks.[/warning]")
        return
    task, session, runtime_rows, missing = await _resolve_logs_target(state, args[0], limit=limit)
    if missing:
        console.print(f"[warning]No task, session, or runtime session found for {missing}. Try /session list or /tasks.[/warning]")
        return
    if task:
        _render_key_value_table(
            f"Task Log Target {getattr(task, 'id', '')}",
            [
                ("title", getattr(task, "title", "")),
                ("status", _value_text(getattr(task, "status", ""))),
                ("session", getattr(task, "session_id", "")),
                ("runtime", _runtime_session_id_from_task(task)),
            ],
            full=full,
        )
        _render_task_progress(task, limit=limit, full=full)
    if session:
        session_id = str(getattr(session, "session_id", "") or "")
        _render_key_value_table(
            f"Session Log Target {session_id}",
            [
                ("title", getattr(session, "title", "")),
                ("status", getattr(session, "status", "")),
                ("mode", getattr(session, "mode", "")),
            ],
            full=full,
        )
        if hasattr(store, "get_session_transcript"):
            transcript = await store.get_session_transcript(session_id)
            _render_transcript_table(transcript, limit=limit)
    await _render_runtime_log_tables(store, runtime_rows, limit=limit, full=full)


async def _task_scope_ids_for_comms(state: _InteractiveChatState, task: Any) -> list[str]:
    task_id = str(getattr(task, "id", "") or "").strip()
    metadata = dict(getattr(task, "metadata", {}) or {})
    scope = {task_id}
    scope.update(str(item).strip() for item in list(metadata.get("execution_task_ids", []) or []) if str(item).strip())
    parent_session_id = str(getattr(task, "session_id", "") or "").strip()
    store = getattr(state.engine, "store", None)
    if parent_session_id and store and hasattr(store, "get_tasks"):
        try:
            tasks = await store.get_tasks(project_id=_current_project_id(state.engine))
            for candidate in tasks:
                if str(getattr(candidate, "parent_session_id", "") or "").strip() == parent_session_id:
                    scope.add(str(getattr(candidate, "id", "") or "").strip())
        except Exception:
            pass
    scope.discard("")
    return sorted(scope)


def _render_agent_messages(messages: list[Any], *, full: bool = False) -> None:
    if not messages:
        console.print("[info]No company-mode messages found for this task scope.[/info]")
        return
    table = Table(title=f"Company Messages ({len(messages)})")
    table.add_column("Time")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Subject")
    table.add_column("Body")
    for message in messages:
        to_agents = getattr(message, "to_agents", []) or []
        table.add_row(
            _format_datetime(getattr(message, "timestamp", None)),
            str(getattr(message, "from_agent", "") or ""),
            ", ".join(str(item) for item in to_agents),
            _value_text(getattr(message, "msg_type", "")),
            _value_text(getattr(message, "status", "")),
            _clip_text(getattr(message, "subject", "") or "", 80, full=full),
            _clip_text(getattr(message, "body", "") or "", 180, full=full),
        )
    console.print(table)


def _render_handoff_records(records: list[Any], *, full: bool = False) -> None:
    if not records:
        console.print("[info]No handoff records found for this task scope.[/info]")
        return
    table = Table(title=f"Handoffs ({len(records)})")
    table.add_column("Handoff ID")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Summary")
    table.add_column("Created")
    for record in records:
        table.add_row(
            str(getattr(record, "handoff_id", "") or ""),
            str(getattr(record, "from_role", "") or ""),
            str(getattr(record, "to_role", "") or ""),
            str(getattr(record, "status", "") or ""),
            str(getattr(record, "task_id", "") or ""),
            _clip_text(getattr(record, "summary", "") or "", 180, full=full),
            _format_datetime(getattr(record, "created_at", None)),
        )
    console.print(table)


async def _handle_comms_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Comms store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /comms <task_id>.[/warning]")
        return
    if len(args) != 1:
        console.print("[warning]Usage: /comms <task_id> [--limit N] [--full]. Try /tasks.[/warning]")
        return
    task = await _get_task_for_current_project(state, args[0])
    if task is None:
        return
    scope_ids = await _task_scope_ids_for_comms(state, task)
    console.print(f"[info]Comms task scope: {', '.join(scope_ids)}[/info]")
    messages = await store.list_agent_messages_for_tasks(scope_ids, limit=limit) if hasattr(store, "list_agent_messages_for_tasks") else []
    _render_agent_messages(messages, full=full)

    records: list[Any] = []
    if hasattr(store, "get_handoff_records"):
        seen: set[str] = set()
        for task_id in scope_ids:
            for record in await store.get_handoff_records(project_id=_current_project_id(state.engine), task_id=task_id, limit=limit):
                handoff_id = str(getattr(record, "handoff_id", "") or "")
                if handoff_id and handoff_id not in seen:
                    seen.add(handoff_id)
                    records.append(record)
    _render_handoff_records(records[:limit], full=full)

    metadata = dict(getattr(task, "metadata", {}) or {})
    context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
    notes = []
    for key in ("structured_review_verdict", "review_verdict", "review_notes", "handoff_context", "handoff_to"):
        if metadata.get(key):
            notes.append((key, _json_summary(metadata.get(key), full=full)))
        if context_snapshot.get(key):
            notes.append((f"context.{key}", _json_summary(context_snapshot.get(key), full=full)))
    if notes:
        _render_key_value_table("Review and Handoff Notes", notes, full=full)
    else:
        console.print("[info]No review notes or handoff context found on the task.[/info]")


def _collect_attachment_refs(value: Any, source: str, out: list[tuple[str, dict[str, Any]]]) -> None:
    if isinstance(value, dict):
        if value.get("attachment_id") or value.get("disk_path"):
            if value.get("filename") or value.get("mime_type") or value.get("disk_path"):
                out.append((source, dict(value)))
        for key, nested in value.items():
            if key in {"attachment_refs", "attachments"} or isinstance(nested, (dict, list)):
                _collect_attachment_refs(nested, source, out)
        return
    if isinstance(value, list):
        for item in value:
            _collect_attachment_refs(item, source, out)


async def _handle_attachments_slash(state: _InteractiveChatState, args: list[str]) -> None:
    store = _require_chat_store(state, label="Attachment store")
    if store is None:
        return
    try:
        args, limit, full = _parse_view_args(args)
    except ValueError as exc:
        console.print(f"[warning]{exc}. Try /attachments --limit 20.[/warning]")
        return
    if args:
        console.print("[warning]Usage: /attachments [--limit N] [--full].[/warning]")
        return
    refs: list[tuple[str, dict[str, Any]]] = []
    if state.session_id and hasattr(store, "get_session"):
        session = await store.get_session(state.session_id)
        if session:
            _collect_attachment_refs(getattr(session, "metadata", {}) or {}, f"session:{state.session_id}", refs)
    if state.session_id and hasattr(store, "get_session_transcript"):
        transcript = await store.get_session_transcript(state.session_id)
        for idx, item in enumerate(transcript):
            if isinstance(item, dict):
                message = item.get("message")
                _collect_attachment_refs(getattr(message, "metadata", {}) or {}, f"message:{idx}", refs)
                for part in item.get("parts", []) or []:
                    payload = part.get("payload", {}) if isinstance(part, dict) else getattr(part, "payload", {})
                    _collect_attachment_refs(payload, f"message:{idx}", refs)
    if hasattr(store, "get_tasks"):
        for task in await store.get_tasks(project_id=_current_project_id(state.engine)):
            if str(getattr(task, "session_id", "") or "") == state.session_id:
                _collect_attachment_refs(getattr(task, "metadata", {}) or {}, f"task:{getattr(task, 'id', '')}", refs)
                _collect_attachment_refs(getattr(task, "result", {}) or {}, f"task:{getattr(task, 'id', '')}", refs)
    deduped: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for source, ref in refs:
        key = str(ref.get("attachment_id") or ref.get("disk_path") or ref)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((source, ref))
    if not deduped:
        console.print("[info]No attachments found for the current session.[/info]")
        return
    table = Table(title=f"Attachments ({len(deduped[:limit])})")
    table.add_column("Attachment ID")
    table.add_column("Filename")
    table.add_column("MIME")
    table.add_column("Size")
    table.add_column("Source")
    table.add_column("Path")
    for source, ref in deduped[:limit]:
        table.add_row(
            str(ref.get("attachment_id", "") or ""),
            str(ref.get("filename", "") or ""),
            str(ref.get("mime_type", "") or ""),
            str(ref.get("size_bytes", "") or ""),
            source,
            _clip_text(ref.get("disk_path", "") or "", 100, full=full),
        )
    console.print(table)


async def _handle_mode_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        table = Table(title="Mode")
        table.add_column("Mode", style="cyan")
        table.add_column("Current", justify="center")
        table.add_column("Command")
        table.add_row("task", "*" if state.mode == "task" else "", "/mode task")
        table.add_row("company/corporate", "*" if state.mode == "company" and state.company_profile == "corporate" else "", "/mode company corporate")
        table.add_row("company/custom", "*" if state.mode == "company" and state.company_profile == "custom" else "", "/mode company custom")
        console.print(table)
        console.print(f"[info]Current mode: {state.mode}; company_profile={state.company_profile}[/info]")
        return
    mode = args[0].strip().lower()
    if mode == "project":
        mode = "task"
    if mode not in _VALID_CHAT_MODES:
        console.print("[warning]Usage: /mode [task|company] [corporate|custom][/warning]")
        return
    next_company_profile = state.company_profile
    if mode == "company":
        if len(args) >= 2:
            profile = args[1].strip().lower()
            if profile not in _VALID_COMPANY_PROFILES:
                console.print("[warning]Company profile must be corporate or custom.[/warning]")
                return
            next_company_profile = profile
    state.mode = mode
    state.company_profile = next_company_profile
    await _persist_chat_context(state)
    console.print(f"[success]Mode set to {state.mode}[/success]")
    if state.mode == "company":
        console.print(f"[info]Company profile: {state.company_profile}[/info]")


async def _handle_agent_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        table = Table(title="Agent")
        table.add_column("Agent", style="cyan")
        table.add_column("Current", justify="center")
        table.add_column("Command")
        for agent in ["native", "codex", "claude_code", "cursor", "opencode"]:
            table.add_row(agent, "*" if state.preferred_agent == agent else "", f"/agent {agent}")
        table.add_row("system default", "*" if state.preferred_agent is None else "", "/agent none")
        console.print(table)
        console.print(f"[info]Preferred agent: {state.preferred_agent or '(system default)'}[/info]")
        return
    command = args[0].strip().lower()
    if command in {"list", "ls"}:
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.list())
        if payload:
            _emit_payload(payload)
        return
    if command == "detail":
        if len(args) != 2:
            console.print("[warning]Usage: /agent detail <agent_id>[/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.detail(project_id=_current_project_id(state.engine), agent_id=args[1]))
        if payload:
            _emit_payload(payload)
        return
    if command == "create":
        rest = args[1:]
        try:
            rest, description = _extract_option(rest, "--description", default="")
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 2:
            console.print("[warning]Usage: /agent create <name> <role_id> [--description ...] [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.create(name=rest[0], role_id=rest[1], office_id=office_id or "office-0", description=description or ""),
        )
        if payload:
            _emit_payload(payload)
        return
    if command == "create-from-template":
        rest = args[1:]
        try:
            rest, role_id = _extract_option(rest, "--role")
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 1:
            console.print("[warning]Usage: /agent create-from-template <template_id> [--role <role_id>] [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.create_from_template(template_id=rest[0], role_id=role_id or rest[0], office_id=office_id or "office-0"),
        )
        if payload:
            _emit_payload(payload)
        return
    if command in {"import-employee", "import-agent"}:
        rest = args[1:]
        try:
            rest, office_id = _extract_option(rest, "--office", default="office-0")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 1:
            console.print("[warning]Usage: /agent import-employee <employee_id> [--office ...][/warning]")
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.import_employee(employee_id=rest[0], office_id=office_id or "office-0"))
        if payload:
            _emit_payload(payload)
        return
    if command == "delete":
        if len(args) < 2:
            console.print("[warning]Usage: /agent delete <agent_id> --yes[/warning]")
            return
        remaining, ok = _require_yes_arg(args[2:], usage="/agent delete <agent_id> --yes")
        if not ok or remaining:
            return
        payload = await _run_chat_office_service(state, lambda svc: svc.agent.delete(args[1]))
        if payload:
            _emit_payload(payload)
        return
    if command == "move":
        rest = args[1:]
        try:
            rest, seat_zone = _extract_option(rest, "--seat-zone")
            rest, desk_id = _extract_option(rest, "--desk")
        except ValueError as exc:
            console.print(f"[warning]{exc}[/warning]")
            return
        if len(rest) != 2:
            console.print("[warning]Usage: /agent move <agent_id> <office_id> [--seat-zone ...] [--desk ...][/warning]")
            return
        payload = await _run_chat_office_service(
            state,
            lambda svc: svc.agent.move(agent_id=rest[0], office_id=rest[1], seat_zone=seat_zone, desk_id=desk_id),
        )
        if payload:
            _emit_payload(payload)
        return
    agent = args[0].strip().lower().replace("-", "_")
    if agent in {"none", "auto", "default"}:
        state.preferred_agent = None
        await _persist_chat_context(state)
        console.print("[success]Preferred agent cleared; using system default.[/success]")
        return
    if agent not in _VALID_PREFERRED_AGENTS:
        valid = ", ".join(sorted([*_VALID_PREFERRED_AGENTS, "none"]))
        console.print(f"[warning]Unknown agent '{args[0]}'. Valid: {valid}[/warning]")
        return
    state.preferred_agent = agent
    await _persist_chat_context(state)
    console.print(f"[success]Preferred agent set to {agent}[/success]")


def _handle_domains_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        console.print(f"[info]Domains: {', '.join(state.domains) if state.domains else '(none)'}[/info]")
        return
    if len(args) == 1 and args[0].strip().lower() in {"clear", "none", "reset"}:
        state.domains = []
        console.print("[success]Domain hints cleared.[/success]")
        return
    state.domains = [arg.strip().lower() for arg in args if arg.strip()]
    console.print(f"[success]Domains set to: {', '.join(state.domains) if state.domains else '(none)'}[/success]")


def _staffing_checkpoint_key(checkpoint: Any) -> str:
    return str(
        getattr(checkpoint, "checkpoint_id", "")
        or getattr(checkpoint, "session_id", "")
        or "company_staffing_selection"
    )


def _company_staffing_payload(checkpoint: Any) -> dict[str, Any]:
    payload = getattr(checkpoint, "payload", {}) or {}
    return dict(payload) if isinstance(payload, dict) else {}


def _normalize_cli_staffing_selection(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"kind": "fallback", "id": ""}
    kind = str(value.get("kind", "") or "").strip().lower()
    if kind in {"employee", "emp"}:
        selected_id = str(value.get("id") or value.get("employee_id") or "").strip()
        return {"kind": "employee", "id": selected_id} if selected_id else {"kind": "fallback", "id": ""}
    if kind in {"template", "tpl"}:
        selected_id = str(value.get("id") or value.get("template_id") or "").strip()
        return {"kind": "template", "id": selected_id} if selected_id else {"kind": "fallback", "id": ""}
    return {"kind": "fallback", "id": ""}


def _company_staffing_default_draft(payload: dict[str, Any]) -> dict[str, Any]:
    selections: dict[str, dict[str, str]] = {}
    role_agents: dict[str, str] = {}
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        selections[role_id] = _normalize_cli_staffing_selection(role.get("default_selection"))
        role_agents[role_id] = (
            str(role.get("selected_agent") or role.get("default_agent") or "codex").strip().lower().replace("-", "_")
            or "codex"
        )
    return {"staffing_selections": selections, "recruitment_role_agents": role_agents}


def _company_staffing_resume_metadata(draft: dict[str, Any], checkpoint: Any | None = None) -> dict[str, Any]:
    metadata = {
        "staffing_action": "manual_approve",
        "staffing_selections": dict(draft.get("staffing_selections", {}) or {}),
        "recruitment_role_agents": dict(draft.get("recruitment_role_agents", {}) or {}),
    }
    if checkpoint is not None:
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        if checkpoint_id:
            metadata["response_to_checkpoint_id"] = checkpoint_id
        metadata["response_to_checkpoint_type"] = "company_staffing_selection"
    return metadata


def _staffing_pool_maps(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    pool = dict(payload.get("staffing_pool", {}) or {})
    employees = {
        str(item.get("employee_id", "") or "").strip(): dict(item)
        for item in list(pool.get("employees", []) or [])
        if str(item.get("employee_id", "") or "").strip()
    }
    templates = {
        str(item.get("template_id", "") or "").strip(): dict(item)
        for item in list(pool.get("templates", []) or [])
        if str(item.get("template_id", "") or "").strip()
    }
    return employees, templates


def _company_staffing_selection_label(selection: Any, payload: dict[str, Any]) -> str:
    normalized = _normalize_cli_staffing_selection(selection)
    employees, templates = _staffing_pool_maps(payload)
    selected_id = normalized.get("id", "")
    if normalized["kind"] == "employee":
        employee = employees.get(selected_id, {})
        name = str(employee.get("employee_name", "") or selected_id)
        return f"{name} ({selected_id})"
    if normalized["kind"] == "template":
        template = templates.get(selected_id, {})
        name = str(template.get("template_name", "") or selected_id)
        return f"Hire template: {name} ({selected_id})"
    return "role-only fallback"


def _company_staffing_agent_choices(state: _InteractiveChatState) -> list[dict[str, Any]]:
    available: set[str] = {"native"}
    registry = getattr(state.engine, "adapter_registry", None)
    if registry and callable(getattr(registry, "list_available", None)):
        try:
            available.update(str(item).strip().lower().replace("-", "_") for item in registry.list_available())
        except Exception:
            pass
    ordered = ["native", "codex", "claude_code", "cursor", "opencode"]
    return [
        {
            "agent": agent,
            "label": f"{agent} ({'available' if agent in available else 'unavailable'})",
            "available": agent in available,
            "search": agent.replace("_", " "),
        }
        for agent in ordered
    ]


def _company_staffing_employee_choices(payload: dict[str, Any], role: dict[str, Any]) -> list[dict[str, Any]]:
    employees, templates = _staffing_pool_maps(payload)
    role_id = str(role.get("role_id", "") or "").strip()
    same_role_ids = [
        str(item or "").strip()
        for item in list(role.get("same_role_employee_ids", []) or [])
        if str(item or "").strip()
    ]
    choices: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_employee(employee_id: str, prefix: str) -> None:
        employee = employees.get(employee_id)
        if not employee or employee_id in seen:
            return
        seen.add(employee_id)
        label = f"{prefix}: {employee.get('employee_name') or employee_id} ({employee_id})"
        choices.append(
            {
                "kind": "employee",
                "id": employee_id,
                "label": label,
                "search": " ".join(
                    str(part or "")
                    for part in [
                        employee_id,
                        employee.get("employee_name"),
                        employee.get("role_id"),
                        employee.get("category"),
                        " ".join(employee.get("domains", []) or []),
                        " ".join(employee.get("tags", []) or []),
                        employee.get("description"),
                    ]
                ).lower(),
            }
        )

    for employee_id in same_role_ids:
        _append_employee(employee_id, "same role")
    for employee_id, employee in employees.items():
        employee_role = str(employee.get("role_id", "") or "").strip()
        prefix = "same role" if employee_role == role_id else "employee"
        _append_employee(employee_id, prefix)
    for template_id, template in templates.items():
        choices.append(
            {
                "kind": "template",
                "id": template_id,
                "label": f"template: {template.get('template_name') or template_id} ({template_id})",
                "search": " ".join(
                    str(part or "")
                    for part in [
                        template_id,
                        template.get("template_name"),
                        template.get("category"),
                        " ".join(template.get("domains", []) or []),
                        " ".join(template.get("tags", []) or []),
                        template.get("description"),
                    ]
                ).lower(),
            }
        )
    if bool(role.get("fallback_available", True)):
        choices.append({"kind": "fallback", "id": "", "label": "fallback: role-only execution", "search": f"fallback role only {role_id}"})
    return choices


def _company_staffing_filter_options(options: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized = str(query or "").strip().lower()
    if not normalized:
        return options
    tokens = [token for token in re.split(r"\s+", normalized) if token]
    if not tokens:
        return options
    return [
        option
        for option in options
        if all(token in f"{option.get('label', '')} {option.get('search', '')}".lower() for token in tokens)
    ]


def _render_company_staffing_summary(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> None:
    payload = _company_staffing_payload(checkpoint)
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    agent_status = {item["agent"]: item for item in _company_staffing_agent_choices(state)}
    table = Table(title="Company Staffing Preflight")
    table.add_column("Role", style="cyan")
    table.add_column("Employee / Template")
    table.add_column("Agent")
    table.add_column("Status")
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        role_label = str(role.get("role_label", "") or role_id)
        agent = str(role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex").strip().lower().replace("-", "_")
        status = "available" if agent_status.get(agent, {}).get("available") else "unavailable"
        table.add_row(
            f"{role_label}\n[dim]{role_id}[/dim]",
            _company_staffing_selection_label(selections.get(role_id), payload),
            agent,
            status,
        )
    console.print(table)
    recommended = str(payload.get("recommended_action", "") or "").strip()
    summary = str(payload.get("summary", "") or "").strip()
    if summary:
        console.print(f"[dim]{summary}[/dim]")
    if recommended == "auto_recruit":
        console.print("[info]r auto recruit (recommended) | e edit | a approve selections | c context | d deny | /staffing reopen this editor later[/info]")
    else:
        console.print("[info]a approve selections | e edit | c context | r auto recruit | d deny | /staffing reopen this editor later[/info]")


def _render_company_staffing_context_preview(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> None:
    payload = _company_staffing_payload(checkpoint)
    original_message = _clip_text(payload.get("original_message"), 360, full=False) or "(empty request)"
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    table = Table(title="Initial Role Context Preview")
    table.add_column("Role", style="cyan")
    table.add_column("Agent")
    table.add_column("Staffing")
    table.add_column("Context Preview")
    for role in list(payload.get("staffing_roles", []) or []):
        role_id = str(role.get("role_id", "") or "").strip()
        if not role_id:
            continue
        role_label = str(role.get("role_label", "") or role_id).strip()
        role_responsibility = _clip_text(role.get("role_responsibility"), 180, full=False)
        selected_agent = str(role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex").strip()
        staffing_label = _company_staffing_selection_label(selections.get(role_id), payload)
        context_lines = [
            f"Owner request: {original_message}",
        ]
        if role_responsibility:
            context_lines.append(f"Role responsibility: {role_responsibility}")
        context_lines.append(
            "Runtime note: exact child WorkItem briefs are created by the manager at execution time; "
            "inspect them later with /work-items show or role logs."
        )
        table.add_row(
            f"{role_label}\n[dim]{role_id}[/dim]",
            selected_agent,
            _clip_text(staffing_label, 120, full=False),
            "\n".join(context_lines),
        )
    console.print(table)


async def _latest_company_staffing_checkpoint(state: _InteractiveChatState) -> Any | None:
    getter = getattr(state.engine, "get_latest_pending_checkpoint_for_session", None)
    if not callable(getter) or not state.session_id:
        return None
    try:
        checkpoint = await getter(state.session_id)
    except Exception:
        return None
    if str(getattr(checkpoint, "checkpoint_type", "") or "") != "company_staffing_selection":
        return None
    return checkpoint


def _apply_staffing_choice(draft: dict[str, Any], role_id: str, choice: dict[str, Any], *, column: str) -> None:
    if column == "agent":
        agent = str(choice.get("agent", "") or "").strip().lower().replace("-", "_")
        if agent:
            draft.setdefault("recruitment_role_agents", {})[role_id] = agent
        return
    kind = str(choice.get("kind", "") or "fallback").strip().lower()
    selected_id = str(choice.get("id", "") or "").strip()
    if kind not in {"employee", "template"}:
        draft.setdefault("staffing_selections", {})[role_id] = {"kind": "fallback", "id": ""}
    else:
        draft.setdefault("staffing_selections", {})[role_id] = {"kind": kind, "id": selected_id}


def _render_staffing_editor_text(
    state: _InteractiveChatState,
    payload: dict[str, Any],
    draft: dict[str, Any],
    row_index: int,
    column_index: int,
    query: str,
    choice_index: int,
) -> str:
    roles = [dict(role) for role in list(payload.get("staffing_roles", []) or []) if str(role.get("role_id", "") or "").strip()]
    if not roles:
        return "No staffing roles are available.\nPress q to exit."
    row_index = max(0, min(row_index, len(roles) - 1))
    role = roles[row_index]
    role_id = str(role.get("role_id", "") or "")
    column_name = "Employee/Template" if column_index == 0 else "Agent"
    options = (
        _company_staffing_employee_choices(payload, role)
        if column_index == 0
        else _company_staffing_agent_choices(state)
    )
    matches = _company_staffing_filter_options(options, query)[:8]
    lines = [
        "Company staffing editor",
        "Up/Down: role or match | Left/Right/Tab: column | type to search | Enter: select | Esc/q: save | Ctrl-C: cancel",
        "",
    ]
    selections = dict(draft.get("staffing_selections", {}) or {})
    role_agents = dict(draft.get("recruitment_role_agents", {}) or {})
    for idx, item in enumerate(roles):
        item_role_id = str(item.get("role_id", "") or "")
        marker = ">" if idx == row_index else " "
        selected = _company_staffing_selection_label(selections.get(item_role_id), payload)
        agent = str(role_agents.get(item_role_id) or item.get("selected_agent") or item.get("default_agent") or "codex")
        lines.append(f"{marker} {item.get('role_label') or item_role_id} [{item_role_id}]")
        lines.append(f"    Employee/Template: {selected}")
        lines.append(f"    Agent: {agent}")
    lines.extend(["", f"Editing {role.get('role_label') or role_id} / {column_name}", f"Search: {query or '(all)'}"])
    if not matches:
        lines.append("  no matches")
    for idx, option in enumerate(matches):
        marker = ">" if idx == choice_index else " "
        lines.append(f"  {marker} {option.get('label')}")
    return "\n".join(lines)


async def _edit_company_staffing_draft(
    state: _InteractiveChatState,
    checkpoint: Any,
    draft: dict[str, Any],
) -> dict[str, Any] | None:
    payload = _company_staffing_payload(checkpoint)
    roles = [dict(role) for role in list(payload.get("staffing_roles", []) or []) if str(role.get("role_id", "") or "").strip()]
    if not roles:
        console.print("[warning]This staffing checkpoint has no roles to edit.[/warning]")
        return draft
    working = {
        "staffing_selections": dict(draft.get("staffing_selections", {}) or {}),
        "recruitment_role_agents": dict(draft.get("recruitment_role_agents", {}) or {}),
    }
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
    except Exception:
        return _edit_company_staffing_draft_fallback(payload, working)

    row_index = 0
    column_index = 0
    query = ""
    choice_index = 0
    kb = KeyBindings()

    def _current_options() -> list[dict[str, Any]]:
        role = roles[row_index]
        options = (
            _company_staffing_employee_choices(payload, role)
            if column_index == 0
            else _company_staffing_agent_choices(state)
        )
        return _company_staffing_filter_options(options, query)

    def _reset_choice() -> None:
        nonlocal choice_index
        choice_index = 0

    def _render() -> str:
        return _render_staffing_editor_text(state, payload, working, row_index, column_index, query, choice_index)

    @kb.add("up")
    def _up(event) -> None:  # pragma: no cover - exercised through prompt_toolkit in real terminals
        nonlocal row_index, choice_index
        if query:
            choice_index = max(0, choice_index - 1)
        else:
            row_index = max(0, row_index - 1)
            _reset_choice()

    @kb.add("down")
    def _down(event) -> None:  # pragma: no cover
        nonlocal row_index, choice_index
        if query:
            choice_index = min(max(0, len(_current_options()) - 1), choice_index + 1)
        else:
            row_index = min(len(roles) - 1, row_index + 1)
            _reset_choice()

    @kb.add("left")
    def _left(event) -> None:  # pragma: no cover
        nonlocal column_index, query
        column_index = 0
        query = ""
        _reset_choice()

    @kb.add("right")
    @kb.add("tab")
    def _right(event) -> None:  # pragma: no cover
        nonlocal column_index, query
        column_index = 1 if column_index == 0 else 0
        query = ""
        _reset_choice()

    @kb.add("backspace")
    def _backspace(event) -> None:  # pragma: no cover
        nonlocal query
        query = query[:-1]
        _reset_choice()

    @kb.add("enter")
    def _enter(event) -> None:  # pragma: no cover
        nonlocal query
        options = _current_options()
        if not options:
            return
        role_id = str(roles[row_index].get("role_id", "") or "")
        _apply_staffing_choice(working, role_id, options[min(choice_index, len(options) - 1)], column="agent" if column_index else "employee")
        query = ""
        _reset_choice()

    @kb.add("escape")
    @kb.add("q")
    def _save(event) -> None:  # pragma: no cover
        event.app.exit(result=True)

    @kb.add("c-c")
    def _cancel(event) -> None:  # pragma: no cover
        event.app.exit(result=False)

    @kb.add("/")
    def _slash(event) -> None:  # pragma: no cover
        nonlocal query
        query = ""
        _reset_choice()

    @kb.add("<any>")
    def _typing(event) -> None:  # pragma: no cover
        nonlocal query
        data = getattr(event, "data", "") or ""
        if data and data.isprintable():
            query += data
            _reset_choice()

    app_editor = Application(
        layout=Layout(HSplit([Window(content=FormattedTextControl(_render), wrap_lines=False)])),
        key_bindings=kb,
        full_screen=False,
    )
    try:
        saved = await app_editor.run_async()
    except Exception:
        return _edit_company_staffing_draft_fallback(payload, working)
    return working if saved else None


def _edit_company_staffing_draft_fallback(payload: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    console.print("[warning]Interactive editor unavailable. Use text overrides; blank line saves.[/warning]")
    console.print("Examples: senior_engineer=emp:employee-id | senior_engineer=tpl:template-id | senior_engineer=fallback | agent senior_engineer=codex")
    roles = {
        str(role.get("role_id", "") or "").strip()
        for role in list(payload.get("staffing_roles", []) or [])
        if str(role.get("role_id", "") or "").strip()
    }
    while True:
        try:
            raw = console.input("[bold]staffing edit> [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return draft
        if not raw or raw.lower() in {"q", "quit", "save", "done"}:
            return draft
        if raw.lower().startswith("agent "):
            raw = raw.split(" ", 1)[1].strip()
            if "=" not in raw:
                console.print("[warning]Usage: agent <role_id>=<agent>[/warning]")
                continue
            role_id, agent = [part.strip() for part in raw.split("=", 1)]
            if role_id not in roles:
                console.print(f"[warning]Unknown role: {role_id}[/warning]")
                continue
            draft.setdefault("recruitment_role_agents", {})[role_id] = agent.lower().replace("-", "_")
            continue
        if "=" not in raw:
            console.print("[warning]Usage: <role_id>=emp:<id> | <role_id>=tpl:<id> | <role_id>=fallback[/warning]")
            continue
        role_id, selection = [part.strip() for part in raw.split("=", 1)]
        if role_id not in roles:
            console.print(f"[warning]Unknown role: {role_id}[/warning]")
            continue
        if selection.lower().startswith("emp:"):
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "employee", "id": selection.split(":", 1)[1].strip()}
        elif selection.lower().startswith("tpl:"):
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "template", "id": selection.split(":", 1)[1].strip()}
        else:
            draft.setdefault("staffing_selections", {})[role_id] = {"kind": "fallback", "id": ""}


async def _resume_company_staffing_checkpoint(
    state: _InteractiveChatState,
    checkpoint: Any,
    *,
    action: str,
    draft: dict[str, Any] | None = None,
    controller: ChatTurnController | None = None,
) -> None:
    metadata: dict[str, Any]
    content: str
    if action == "auto":
        content = "auto"
        metadata = {"staffing_action": "auto_recruit"}
    elif action == "deny":
        content = "deny"
        metadata = {"staffing_action": "deny"}
    else:
        content = "approve"
        metadata = _company_staffing_resume_metadata(draft or _company_staffing_default_draft(_company_staffing_payload(checkpoint)), checkpoint)
    if action in {"auto", "deny"}:
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        if checkpoint_id:
            metadata["response_to_checkpoint_id"] = checkpoint_id
        metadata["response_to_checkpoint_type"] = "company_staffing_selection"

    if controller is not None:
        await controller.submit_control_message(
            content,
            message_metadata=metadata,
            mode="company",
            company_profile=state.company_profile,
            preferred_agent=state.preferred_agent,
            domains=list(state.domains),
        )
        return

    state.runtime_display.begin_turn()
    response = await state.engine.process_message(
        content,
        project_id=getattr(state.engine, "project_id", None),
        session_id=state.session_id,
        mode="company",
        company_profile=state.company_profile,
        preferred_agent=state.preferred_agent,
        domains=list(state.domains),
        message_metadata=metadata,
    )
    await state.runtime_display.flush()
    await _sync_runtime_checkpoint_hint(state)
    if hasattr(state.runtime_display, "render_status"):
        await state.runtime_display.render_status(force=True)
    if not state.runtime_display.has_streamed_content:
        _print_response(response, state.no_markdown)


async def _maybe_run_company_staffing_preflight(state: _InteractiveChatState, controller: ChatTurnController | None = None) -> bool:
    checkpoint = await _latest_company_staffing_checkpoint(state)
    if checkpoint is None:
        return False
    key = _staffing_checkpoint_key(checkpoint)
    draft = state.company_staffing_drafts.setdefault(key, _company_staffing_default_draft(_company_staffing_payload(checkpoint)))
    while True:
        _render_company_staffing_summary(state, checkpoint, draft)
        try:
            choice = console.input("[bold]Company preflight [a/e/c/r/d]: [/bold]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("[warning]Staffing selection left pending. Reopen with /staffing.[/warning]")
            return True
        if choice in {"a", "approve", "y", "yes", ""}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="approve", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        if choice in {"e", "edit"}:
            edited = await _edit_company_staffing_draft(state, checkpoint, draft)
            if edited is not None:
                draft.clear()
                draft.update(edited)
                state.company_staffing_drafts[key] = draft
            continue
        if choice in {"c", "context", "ctx", "preview"}:
            _render_company_staffing_context_preview(state, checkpoint, draft)
            continue
        if choice in {"r", "auto", "auto recruit", "autorecruit"}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="auto", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        if choice in {"d", "deny", "cancel", "stop", "n", "no"}:
            await _resume_company_staffing_checkpoint(state, checkpoint, action="deny", draft=draft, controller=controller)
            state.company_staffing_drafts.pop(key, None)
            return True
        console.print("[warning]Choose a, e, c, r, or d.[/warning]")


async def _handle_company_staffing_shortcut(
    state: _InteractiveChatState,
    user_input: str,
    controller: ChatTurnController | None,
) -> bool:
    choice = str(user_input or "").strip().lower()
    if choice not in {
        "a",
        "approve",
        "y",
        "yes",
        "e",
        "edit",
        "c",
        "context",
        "ctx",
        "preview",
        "r",
        "auto",
        "auto recruit",
        "autorecruit",
        "d",
        "deny",
        "cancel",
        "stop",
        "n",
        "no",
    }:
        return False
    checkpoint = await _latest_company_staffing_checkpoint(state)
    if checkpoint is None:
        return False
    key = _staffing_checkpoint_key(checkpoint)
    draft = state.company_staffing_drafts.setdefault(
        key,
        _company_staffing_default_draft(_company_staffing_payload(checkpoint)),
    )
    if choice in {"a", "approve", "y", "yes"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="approve", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    if choice in {"e", "edit"}:
        edited = await _edit_company_staffing_draft(state, checkpoint, draft)
        if edited is not None:
            draft.clear()
            draft.update(edited)
            state.company_staffing_drafts[key] = draft
        _render_company_staffing_summary(state, checkpoint, draft)
        return True
    if choice in {"c", "context", "ctx", "preview"}:
        _render_company_staffing_context_preview(state, checkpoint, draft)
        return True
    if choice in {"r", "auto", "auto recruit", "autorecruit"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="auto", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    if choice in {"d", "deny", "cancel", "stop", "n", "no"}:
        await _resume_company_staffing_checkpoint(state, checkpoint, action="deny", draft=draft, controller=controller)
        state.company_staffing_drafts.pop(key, None)
        return True
    return False


async def _handle_staffing_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    if args and args[0].strip().lower() not in {"context", "ctx", "preview"}:
        console.print("[warning]Usage: /staffing [context][/warning]")
        return
    if not args and not await _maybe_run_company_staffing_preflight(state, controller=controller):
        console.print("[info]No pending company staffing selection for this session.[/info]")
        return
    if args:
        checkpoint = await _latest_company_staffing_checkpoint(state)
        if checkpoint is None:
            console.print("[info]No pending company staffing selection for this session.[/info]")
            return
        key = _staffing_checkpoint_key(checkpoint)
        draft = state.company_staffing_drafts.setdefault(key, _company_staffing_default_draft(_company_staffing_payload(checkpoint)))
        _render_company_staffing_context_preview(state, checkpoint, draft)


def _busy_slash_policy(command: str, args: list[str]) -> BusyCommandPolicy:
    readonly_roots = {
        "help",
        "?",
        "status",
        "cost",
        "runtime",
        "tasks",
        "checkpoints",
        "checkpoint",
        "logs",
        "comms",
        "attachments",
        "work-items",
        "work-item",
        "queue",
        "kanban",
        "board",
        "stop",
        "continue",
    }
    if command in readonly_roots:
        return BusyCommandPolicy.IMMEDIATE_READONLY
    if command == "task":
        if args and args[0].lower() == "show":
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "project":
        if not args or args[0].lower() in {"list", "show"}:
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "session":
        if not args or args[0].lower() in {"list", "show", "stop", "continue"}:
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    if command == "staffing":
        if args and args[0].lower() == "context":
            return BusyCommandPolicy.IMMEDIATE_READONLY
        return BusyCommandPolicy.BLOCKED_WHEN_BUSY
    return BusyCommandPolicy.BLOCKED_WHEN_BUSY


def _print_busy_slash_block(command: str) -> None:
    console.print(
        f"[warning]Busy: /{escape(command)} changes chat context or runtime state, so it is blocked while a turn is running.[/warning]"
    )
    console.print("[dim]Available now: /stop, /kanban, /runtime, /work-items, /logs, /comms, /queue, /status.[/dim]")


async def _handle_queue_slash(controller: ChatTurnController | None, args: list[str]) -> None:
    if controller is None:
        console.print("[warning]/queue is only available in interactive chat.[/warning]")
        return
    command = args[0].lower() if args else "list"
    if command in {"list", "ls"}:
        items = controller.queued_items()
        if not items:
            console.print("[info]No queued prompts.[/info]")
            return
        table = Table(title=f"Queued Prompts ({len(items)})")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Project")
        table.add_column("Session")
        table.add_column("Mode")
        table.add_column("Prompt")
        for idx, item in enumerate(items, start=1):
            table.add_row(
                str(idx),
                item.project_id,
                _session_short_id(item.session_id),
                item.mode,
                _clip_text(item.text, 100),
            )
        console.print(table)
        return
    if command == "drop":
        if len(args) != 2:
            console.print("[warning]Usage: /queue drop <n>[/warning]")
            return
        try:
            index = int(args[1])
        except ValueError:
            console.print("[warning]Queue index must be a number.[/warning]")
            return
        item = controller.drop(index)
        if item is None:
            console.print("[warning]No queued prompt at that index.[/warning]")
            return
        console.print(f"[success]Dropped queued prompt #{index}:[/success] {_clip_text(item.text, 80)}")
        return
    if command == "clear":
        count = controller.clear()
        console.print(f"[success]Cleared {count} queued prompt(s).[/success]")
        return
    console.print("[warning]Usage: /queue list|drop <n>|clear[/warning]")


def _board_launch_args(state: _InteractiveChatState, args: list[str], *, default_view: str = "kanban") -> list[str]:
    view = default_view
    work_item = ""
    role = ""
    target = ""
    rest = list(args)
    if rest:
        candidate = rest.pop(0).lower()
        if candidate in {"kanban", "pipeline", "work-item", "role", "logs"}:
            view = candidate
        else:
            rest.insert(0, candidate)
    if view == "work-item" and rest:
        work_item = rest[0]
    elif view == "role" and rest:
        role = rest[0]
    elif view == "logs" and rest:
        target = rest[0]

    cmd = [
        sys.executable,
        "-m",
        "opc.cli.app",
        "board",
        "--project",
        _current_project_id(state.engine),
        "--attach",
        "--readonly",
        "--refresh-interval",
        "1.0",
        "--view",
        view,
    ]
    if state.session_id:
        cmd.extend(["--session", state.session_id])
    if work_item:
        cmd.extend(["--work-item", work_item])
    if role:
        cmd.extend(["--role", role])
    if target:
        cmd.extend(["--target", target])
    return cmd


async def _launch_board_inspector(state: _InteractiveChatState, args: list[str], *, default_view: str = "kanban") -> None:
    cmd = _board_launch_args(state, args, default_view=default_view)
    console.print("[dim]Opening read-only board inspector. Press q or Ctrl-Q in board to return to chat.[/dim]")
    display = state.runtime_display
    if hasattr(display, "enter_sidecar_quiet"):
        display.enter_sidecar_quiet()
    try:
        process = await asyncio.create_subprocess_exec(*cmd)
        await process.wait()
    except FileNotFoundError as exc:
        console.print(f"[warning]Could not launch board inspector: {escape(str(exc))}[/warning]")
    finally:
        quiet_events = 0
        if hasattr(display, "exit_sidecar_quiet"):
            quiet_events = int(display.exit_sidecar_quiet() or 0)
        suffix = f" Suppressed {quiet_events} runtime event(s); use /logs or /board logs to inspect them." if quiet_events else ""
        console.print(f"[dim]Returned to chat.{suffix}[/dim]")


async def _fetch_kanban_items(
    state: _InteractiveChatState,
    *,
    limit: int = 100,
    session_id: str | None = None,
    project_scope: bool = False,
) -> list[dict[str, Any]]:
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.work_item import WorkItemService

    context = OfficeServiceContext(
        engine=state.engine,
        agent_store=None,
        chat_store=None,
        event_adapter=None,
        mode_state=ModeState(
            exec_mode=state.mode,
            company_profile=state.company_profile,
            task_preferred_agent=state.preferred_agent or "native",
        ),
    )
    scoped_session_id = None if project_scope else str(session_id or state.session_id or "").strip()
    result = await WorkItemService(context).list(
        project_id=_current_project_id(state.engine),
        session_id=scoped_session_id,
        limit=limit,
        kanban_visible_only=True,
    )
    return list(result.payload.get("work_items", []) or [])


def _kanban_items_fingerprint(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        rows.append((
            str(item.get("work_item_id", "") or ""),
            str(item.get("title", "") or ""),
            str(item.get("role_id", "") or ""),
            str(item.get("phase", "") or ""),
            str(item.get("kanban_column", "") or ""),
            str(item.get("runtime_task_id", "") or ""),
            str(item.get("session_id", "") or ""),
            str(item.get("updated_at", "") or ""),
        ))
    return json.dumps(sorted(rows), ensure_ascii=False, sort_keys=True)


def _render_kanban_items(items: list[dict[str, Any]], *, title: str = "Kanban Work Items") -> None:
    if not items:
        console.print("[info]No company work items found yet.[/info]")
        return
    counts: dict[str, int] = {}
    for item in items:
        column = str(item.get("kanban_column") or item.get("phase") or "unknown")
        counts[column] = counts.get(column, 0) + 1
    summary = ", ".join(f"{column}:{count}" for column, count in sorted(counts.items()))
    table = Table(title=f"{title} ({len(items)} | {summary})")
    table.add_column("Work Item")
    table.add_column("Title")
    table.add_column("Role")
    table.add_column("Column")
    table.add_column("Phase")
    table.add_column("Runtime Task")
    table.add_column("Updated")
    for item in items:
        table.add_row(
            str(item.get("work_item_id", "") or "")[:12],
            _clip_text(item.get("title", "") or "", 64),
            str(item.get("role_id", "") or ""),
            str(item.get("kanban_column", "") or ""),
            str(item.get("phase", "") or ""),
            str(item.get("runtime_task_id", "") or "")[:12],
            _clip_text(item.get("updated_at", "") or "", 19),
        )
    console.print(table)


async def _handle_kanban_slash(state: _InteractiveChatState, args: list[str], controller: ChatTurnController | None = None) -> None:
    normalized = [arg.strip().lower() for arg in args]
    if normalized and normalized[0] in {"stop", "off", "clear"}:
        if controller is None:
            console.print("[warning]Kanban live watch is only available in interactive chat.[/warning]")
            return
        await controller.stop_kanban_watch()
        return
    watch = True
    project_scope = False
    for token in normalized:
        if token in {"once", "--once", "--summary", "summary"}:
            watch = False
        elif token in {"all", "project", "--all", "--project"}:
            project_scope = True
        else:
            console.print("[warning]Usage: /kanban [once|stop|all][/warning]")
            return
    session_id = str(state.session_id or "").strip()
    title = "Kanban Work Items (project)" if project_scope else f"Kanban Work Items (session {session_id[:8]})"
    try:
        items = await _fetch_kanban_items(state, limit=100, session_id=session_id, project_scope=project_scope)
    except Exception as exc:
        console.print(f"[warning]Could not load kanban work items: {escape(str(exc))}[/warning]")
        return
    _render_kanban_items(items, title=title)
    if watch and controller is not None:
        await controller.start_kanban_watch(
            initial_fingerprint=_kanban_items_fingerprint(items),
            session_id=session_id,
            project_scope=project_scope,
        )
    elif watch:
        console.print("[dim]Live watch is only available in interactive chat.[/dim]")


async def _handle_board_slash(state: _InteractiveChatState, args: list[str]) -> None:
    if not args:
        args = ["kanban"]
    await _launch_board_inspector(state, args, default_view="kanban")


async def _handle_chat_slash_command(state: _InteractiveChatState, user_input: str, controller: ChatTurnController | None = None) -> bool:
    if not user_input.startswith("/"):
        return False
    try:
        parts = _slash_parts(user_input)
    except ValueError as exc:
        console.print(f"[warning]Could not parse command: {exc}[/warning]")
        return True
    if not parts:
        _print_slash_help()
        return True

    command = _canonical_slash_command(parts[0])
    args = parts[1:]
    if controller is not None and controller.is_busy and command in _slash_command_names():
        policy = _busy_slash_policy(command, args)
        if policy == BusyCommandPolicy.BLOCKED_WHEN_BUSY:
            _print_busy_slash_block(command)
            return True
    if command in {"help", "?"}:
        _print_slash_help()
    elif command == "queue":
        await _handle_queue_slash(controller, args)
    elif command == "status":
        _print_interactive_status(state.engine, state)
    elif command == "cost":
        _print_interactive_cost(state.engine)
    elif command == "mode":
        await _handle_mode_slash(state, args)
    elif command == "agent":
        await _handle_agent_slash(state, args)
    elif command == "domains":
        _handle_domains_slash(state, args)
    elif command == "project":
        await _handle_project_slash(state, args)
    elif command in {"new-session", "new_session"}:
        _start_new_chat_session(state)
    elif command == "sessions":
        await _render_sessions(state, args)
    elif command == "resume":
        await _resume_chat_session(state, args[0] if args else "")
    elif command == "session":
        await _handle_session_slash(state, args, controller=controller)
    elif command == "stop":
        await _handle_stop_slash(state, args, controller=controller)
    elif command == "continue":
        await _handle_continue_slash(state, args, controller=controller)
    elif command == "tasks":
        await _handle_tasks_slash(state, args)
    elif command == "task":
        await _handle_task_slash(state, args)
    elif command in {"checkpoints", "checkpoint"}:
        await _handle_checkpoints_slash(state, args)
    elif command == "org":
        await _handle_org_slash(state, args)
    elif command == "talent":
        await _handle_talent_slash(state, args)
    elif command == "market":
        await _handle_market_slash(state, args)
    elif command == "reorg":
        await _handle_reorg_slash(state, args)
    elif command == "runtime":
        await _handle_runtime_slash(state, args)
    elif command in {"work-items", "work-item"}:
        await _handle_work_items_slash(state, args)
    elif command == "logs":
        await _handle_logs_slash(state, args)
    elif command == "comms":
        await _handle_comms_slash(state, args)
    elif command == "attachments":
        await _handle_attachments_slash(state, args)
    elif command == "staffing":
        await _handle_staffing_slash(state, args, controller=controller)
    elif command == "kanban":
        await _handle_kanban_slash(state, args, controller=controller)
    elif command == "board":
        await _handle_board_slash(state, args)
    else:
        console.print(f"[warning]Unknown command: /{escape(command)}. Try /help for available commands.[/warning]")
    return True


async def _sync_runtime_checkpoint_hint(state: _InteractiveChatState) -> None:
    display = state.runtime_display
    if not hasattr(display, "set_checkpoint_hint"):
        return
    runtime_identity = await _company_runtime_identity_for_session(state)
    if runtime_identity is not None and runtime_identity.checkpoint is not None:
        checkpoint = runtime_identity.checkpoint
        checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "pending")
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
        display.set_checkpoint_hint(checkpoint_type or checkpoint_id)
        return
    getter = getattr(state.engine, "get_latest_pending_checkpoint_for_session", None)
    if not callable(getter) or not state.session_id:
        display.set_checkpoint_hint("")
        return
    try:
        checkpoint = await getter(state.session_id)
    except Exception:
        return
    if not checkpoint:
        display.set_checkpoint_hint("")
        return
    checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "pending")
    checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "")
    display.set_checkpoint_hint(checkpoint_type or checkpoint_id)


async def _process_interactive_chat_message(
    state: _InteractiveChatState,
    user_input: str,
    *,
    message_metadata: dict[str, Any] | None = None,
    interactive_followups: bool = True,
) -> None:
    console.print()
    try:
        state.runtime_display.begin_turn()
        effective_metadata = message_metadata
        execution_session_id = state.session_id
        execution_origin_task_id: str | None = None
        execution_mode = state.mode
        execution_company_profile = state.company_profile
        execution_org_id = state.org_id
        execution_preferred_agent = state.preferred_agent
        handoff_identity = None
        runtime_identity = await _company_runtime_identity_for_session(state)
        runtime_checkpoint = (
            runtime_identity.checkpoint
            if runtime_identity is not None
            else None
        )
        explicit_runtime_type = str(
            (effective_metadata or {}).get("response_to_checkpoint_type", "") or ""
        ).strip()
        explicit_runtime_id = str(
            (effective_metadata or {}).get("response_to_checkpoint_id", "") or ""
        ).strip()
        is_explicit_runtime_handoff = explicit_runtime_type in {
            "company_runtime_suspended",
            "company_runtime_interrupted",
        }

        if is_explicit_runtime_handoff:
            if runtime_identity is None or runtime_checkpoint is None:
                raise RuntimeError(
                    "Company runtime checkpoint does not match the current session."
                )
            checkpoint_id = str(
                getattr(runtime_checkpoint, "checkpoint_id", "") or ""
            ).strip()
            checkpoint_type = str(
                getattr(runtime_checkpoint, "checkpoint_type", "") or ""
            ).strip()
            checkpoint_status = str(
                getattr(runtime_checkpoint, "status", "") or ""
            ).strip().lower()
            if (
                not explicit_runtime_id
                or explicit_runtime_id != checkpoint_id
                or explicit_runtime_type != checkpoint_type
            ):
                raise RuntimeError(
                    "Company runtime checkpoint identity mismatch; refresh before continuing."
                )
            if checkpoint_status != "pending":
                raise RuntimeError(
                    f"Company runtime checkpoint is {checkpoint_status or 'not pending'}."
                )
            execution_session_id = runtime_identity.runtime_session_id
            execution_origin_task_id = runtime_identity.ui_anchor_task_id or None
            handoff_identity = runtime_identity
        elif effective_metadata is None and runtime_checkpoint is not None:
            checkpoint_status = str(
                getattr(runtime_checkpoint, "status", "") or ""
            ).strip().lower()
            if checkpoint_status != "pending":
                raise RuntimeError(
                    f"Company runtime checkpoint is {checkpoint_status or 'not pending'}."
                )
            if runtime_identity is None:
                raise RuntimeError(
                    "Company runtime checkpoint does not match the current session."
                )
            effective_metadata = {
                "response_to_checkpoint_id": str(getattr(runtime_checkpoint, "checkpoint_id", "") or ""),
                "response_to_checkpoint_type": str(getattr(runtime_checkpoint, "checkpoint_type", "") or "company_runtime_suspended"),
            }
            execution_session_id = runtime_identity.runtime_session_id
            execution_origin_task_id = runtime_identity.ui_anchor_task_id or None
            handoff_identity = runtime_identity
        elif effective_metadata is None:
            if runtime_identity is not None and runtime_identity.pending_checkpoint_id:
                # An active checkpoint may only be pending or resuming.  Never
                # let a malformed identity/status fall through as a new turn.
                raise RuntimeError(
                    "Company runtime checkpoint is not available for a new turn."
                )
            if state.mode == "company":
                effective_metadata = {"company_preflight": "manual"}
        if handoff_identity is not None:
            runtime_execution_identity = await _company_runtime_execution_identity(
                state,
                handoff_identity,
            )
            execution_mode = runtime_execution_identity.exec_mode
            execution_company_profile = runtime_execution_identity.company_profile
            execution_org_id = runtime_execution_identity.org_id
            execution_preferred_agent = runtime_execution_identity.preferred_agent
        response = await state.engine.process_message(
            user_input,
            project_id=getattr(state.engine, "project_id", None),
            session_id=execution_session_id,
            mode=execution_mode,
            org_id=execution_org_id or None,
            company_profile=(
                execution_company_profile
                if execution_mode == "company"
                else None
            ),
            preferred_agent=execution_preferred_agent,
            domains=list(state.domains),
            origin_task_id=execution_origin_task_id,
            message_metadata=effective_metadata,
        )
        await state.runtime_display.flush()
        await _sync_runtime_checkpoint_hint(state)
        handled_staffing_preflight = False
        if state.mode == "company":
            if interactive_followups:
                handled_staffing_preflight = await _maybe_run_company_staffing_preflight(state)
            else:
                checkpoint = await _latest_company_staffing_checkpoint(state)
                if checkpoint is not None:
                    key = _staffing_checkpoint_key(checkpoint)
                    draft = state.company_staffing_drafts.setdefault(
                        key,
                        _company_staffing_default_draft(_company_staffing_payload(checkpoint)),
                    )
                    _render_company_staffing_summary(state, checkpoint, draft)
                    console.print("[dim]Type a/e/c/r/d at opc>; /staffing is only a fallback reopen command.[/dim]")
                    handled_staffing_preflight = True
        if hasattr(state.runtime_display, "render_status") and not handled_staffing_preflight:
            await state.runtime_display.render_status(force=True)
        if not state.runtime_display.has_streamed_content and not handled_staffing_preflight:
            _print_response(response, state.no_markdown)
        if effective_metadata and (
            effective_metadata.get("ui_force_resume")
            or str(effective_metadata.get("response_to_checkpoint_type", "") or "") in {
                "company_runtime_suspended",
                "company_runtime_interrupted",
            }
        ):
            remaining_checkpoint = await _latest_company_suspend_checkpoint(state)
            if remaining_checkpoint is not None:
                state.runtime_control_state = "suspended"
                state.runtime_control_checkpoint_id = str(
                    getattr(remaining_checkpoint, "checkpoint_id", "") or ""
                )
            else:
                state.runtime_control_state = "idle"
                state.runtime_control_checkpoint_id = ""
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted. Type /quit to exit.[/warning]")
    except Exception as e:
        console.print(f"[error]Error: {escape(str(e))}[/error]")


async def _interactive_mode(
    config,
    project: str | None,
    no_markdown: bool,
    *,
    mode: str = "task",
    preferred_agent: str | None = None,
    company_profile: str | None = None,
    explicit_mode: bool = False,
    explicit_agent: bool = False,
    explicit_company_profile: bool = False,
) -> None:
    engine, runtime_display = _create_cli_engine(config, project)

    try:
        await engine.initialize()
    except Exception as e:
        console.print(f"[error]Failed to initialize: {escape(str(e))}[/error]")
        return

    state = _InteractiveChatState(
        config=config,
        engine=engine,
        runtime_display=runtime_display,
        session_id="",
        no_markdown=no_markdown,
        mode="company" if str(mode or "").strip().lower() == "company" else "task",
        company_profile=company_profile or _initial_company_profile(config),
        preferred_agent=_normalize_interactive_preferred_agent(preferred_agent),
    )
    _attach_cli_runtime_callbacks(state)
    await _restore_chat_context(
        state,
        restore_mode=not explicit_mode,
        restore_company_profile=not explicit_company_profile,
        restore_agent=not explicit_agent,
    )

    console.print(Panel(
        f"[bold]OPC v{__version__}[/bold] — One-Person Company\n"
        f"Model: {config.llm.default_model}\n"
        f"Current mode: {state.mode}"
        f"{f' ({state.company_profile})' if state.mode == 'company' else ''}\n"
        f"Current agent: {state.preferred_agent or 'system'}\n"
        f"Choose a project and session to begin.",
        border_style="blue",
    ))
    try:
        await _run_interactive_startup_selector(state, explicit_project=bool(project))
    except KeyboardInterrupt:
        console.print("\n[warning]Interrupted.[/warning]")
        await state.engine.shutdown()
        return

    controller = ChatTurnController(state)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style

        history_path = state.engine.opc_home / "cli_history"
        session = PromptSession(
            history=FileHistory(str(history_path)),
            completer=_OPCSlashCompleter(),
            complete_while_typing=True,
            bottom_toolbar=lambda: _chat_bottom_toolbar_text(state, controller),
            style=Style.from_dict({"bottom-toolbar": "fg:#888888"}),
        )

        while True:
            await _print_pending_checkpoint_hint(state.engine, state.session_id)
            try:
                with patch_stdout(raw=True):
                    user_input = await session.prompt_async("opc> ")
            except (EOFError, KeyboardInterrupt):
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if user_input.startswith("/"):
                await _handle_chat_slash_command(state, user_input, controller=controller)
                continue
            if await _handle_company_staffing_shortcut(state, user_input, controller):
                continue

            await controller.submit_user_message(user_input)
            continue

    except ImportError:
        console.print("[warning]prompt-toolkit not available, using basic input[/warning]")
        while True:
            await _print_pending_checkpoint_hint(state.engine, state.session_id)
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("opc> ")
                )
                user_input = user_input.strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if not user_input:
                continue
            if user_input.startswith("/"):
                await _handle_chat_slash_command(state, user_input, controller=controller)
                continue
            if await _handle_company_staffing_shortcut(state, user_input, controller):
                continue
            await controller.submit_user_message(user_input)
            continue

    finally:
        await controller.shutdown()
        await state.engine.shutdown()
        console.print("[info]Goodbye![/info]")


async def _interactive_secretary_mode(config, project: str | None, no_markdown: bool) -> None:
    engine, runtime_display = _create_cli_engine(config, project)

    try:
        await engine.initialize()
    except Exception as e:
        console.print(f"[error]Failed to initialize: {escape(str(e))}[/error]")
        return

    console.print(Panel(
        f"[bold]OPC Secretary[/bold]\n"
        f"Model: {config.llm.default_model}\n"
        f"Project: {project or '(none)'}\n"
        f"Commands: /quit, /project <id>, /new-session, /sessions, /resume <session_id>, /policies, /help",
        border_style="green",
    ))

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        history_path = engine.opc_home / "secretary_cli_history"
        prompt = PromptSession(history=FileHistory(str(history_path)))
        current_session_id, restored_latest = await _resolve_secretary_session_id(engine)
        if restored_latest:
            console.print(f"[info]Restored recent secretary session: {current_session_id}[/info]")
        else:
            console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")

        while True:
            try:
                user_input = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: prompt.prompt("secretary> ")
                )
            except (EOFError, KeyboardInterrupt):
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input in ("/quit", "/exit", "exit", "quit"):
                break
            if user_input.startswith("/project "):
                new_project = user_input.split(" ", 1)[1].strip()
                await engine.shutdown()
                engine, runtime_display = _create_cli_engine(config, new_project)
                await engine.initialize()
                current_session_id, restored_latest = await _resolve_secretary_session_id(engine)
                console.print(f"[info]Switched to project: {new_project}[/info]")
                if restored_latest:
                    console.print(f"[info]Restored recent secretary session: {current_session_id}[/info]")
                else:
                    console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/new-session":
                current_session_id = str(uuid.uuid4())
                console.print(f"[info]Started a new secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/sessions":
                sessions = await engine.secretary.list_sessions(engine.project_id, limit=20) if engine.secretary else []
                if not sessions:
                    console.print("[info]No secretary sessions found for this project.[/info]")
                else:
                    console.print("[bold]Recent secretary sessions:[/bold]")
                    for item in sessions:
                        title = item.title or "(untitled)"
                        console.print(f"  - {item.session_id} :: {title}")
                continue
            if user_input.startswith("/resume "):
                requested_session_id = user_input.split(" ", 1)[1].strip()
                target = await engine.store.get_session(requested_session_id) if engine.store else None
                if not target or target.metadata.get("interface") != "secretary":
                    console.print(f"[warning]Secretary session not found: {requested_session_id}[/warning]")
                    continue
                current_project = engine.project_id or "default"
                target_project = target.project_id or "default"
                if target_project != current_project:
                    console.print(
                        "[warning]"
                        f"Secretary session {requested_session_id} belongs to project '{target_project}'. "
                        f"Switch project first with /project {target_project}."
                        "[/warning]"
                    )
                    continue
                current_session_id = requested_session_id
                console.print(f"[info]Resumed secretary session: {current_session_id}[/info]")
                continue
            if user_input == "/policies":
                summary = engine.secretary.describe_policies(engine.project_id) if engine.secretary else ""
                _print_response(summary, no_markdown)
                continue
            if user_input == "/help":
                console.print(
                    "/quit — Exit\n"
                    "/project <id> — Switch project\n"
                    "/new-session — Start a fresh secretary session\n"
                    "/sessions — List secretary sessions for this project\n"
                    "/resume <session_id> — Resume an existing secretary session\n"
                    "/policies — Show active secretary policies\n"
                    "/help — This help"
                )
                continue

            runtime_display.begin_turn()
            payload = await engine.process_secretary_message(
                user_input,
                project_id=engine.project_id,
                session_id=current_session_id,
            )
            await runtime_display.flush()
            if not runtime_display.has_streamed_content:
                _print_response(payload.get("response", ""), no_markdown)

    except ImportError:
        console.print("[warning]prompt-toolkit not available, using basic input[/warning]")
    finally:
        await engine.shutdown()
        console.print("[info]Goodbye![/info]")


async def _show_cost_summary(config) -> None:
    from opc.core.config import get_opc_home
    db_path = get_opc_home() / "global.db"
    if not db_path.exists():
        return
    store = OPCStore(db_path)
    await store.initialize()
    try:
        costs = await store.get_total_cost()
        if costs["total_calls"] > 0:
            console.print(f"\n[bold]Cost Summary:[/bold]")
            console.print(f"  Total calls: {costs['total_calls']}")
            console.print(f"  Total tokens: {costs['total_tokens_in'] + costs['total_tokens_out']}")
            console.print(f"  Total cost: ${costs['total_cost']:.4f}")
    finally:
        await store.close()


async def _show_autonomy_summary(config, project: str | None = None) -> None:
    from opc.core.config import get_opc_home
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from opc.layer5_memory.preference import PreferenceManager

    db_path = get_opc_home() / "global.db"
    if not db_path.exists():
        return
    store = OPCStore(db_path)
    await store.initialize()
    try:
        stats = await store.get_autonomy_stats(project_id=project)
        console.print(f"\n[bold]Autonomy Summary:[/bold]")
        console.print(f"  Decisions: {stats['total']}")
        console.print(f"  Auto-approved: {stats['auto_approved']}")
        console.print(f"  Escalated: {stats['escalated']}")
        console.print(f"  Rejected: {stats['rejected']}")
        console.print(f"  Auto-approval rate: {stats['auto_approval_rate']:.0%}")

        prefs = PreferenceManager(get_opc_home())
        learned = prefs.get_autonomy_preferences(project_id=project).get("learned_actions", {})
        if learned:
            console.print("  Learned actions:")
            for name, data in list(learned.items())[:10]:
                console.print(
                    f"    - {name}: approvals={data.get('approvals', 0)}, "
                    f"rejections={data.get('rejections', 0)}, "
                    f"explicit_allow={data.get('explicit_allow', False)}, "
                    f"explicit_deny={data.get('explicit_deny', False)}"
                )
        allowlist_lines = ApprovalAllowlistManager(get_opc_home()).summarize(project_id=project, limit=10)
        if allowlist_lines:
            console.print("  Persisted allowlist:")
            for line in allowlist_lines:
                console.print(f"    {line}")
    finally:
        await store.close()


def _print_response(text: str, no_markdown: bool = False) -> None:
    if no_markdown:
        console.print(text)
    else:
        try:
            md = Markdown(text)
            console.print(md)
        except Exception:
            console.print(text)
    console.print()


async def _print_pending_checkpoint_hint(engine, session_id: str) -> None:
    checkpoint = await engine.get_latest_pending_checkpoint_for_session(session_id)
    if not checkpoint:
        return
    if checkpoint.checkpoint_type == "company_recruitment_confirmation":
        console.print(
            "[info]Pending staffing confirmation for this session. "
            "Reply `1` or `approve` to continue, `2` or `deny` to cancel. "
            "Any other input will be treated as feedback/suggestions for revising the proposal.[/info]"
        )
        return
    if checkpoint.checkpoint_type == "company_staffing_selection":
        payload = dict(getattr(checkpoint, "payload", {}) or {})
        recommended = str(payload.get("recommended_action", "") or "").strip()
        if recommended == "auto_recruit":
            console.print(
                "[info]Pending company staffing preflight for this session. "
                "`auto recruit` is recommended; use `/staffing` to review/edit employee, template, and agent choices.[/info]"
            )
        else:
            console.print(
                "[info]Pending manual staffing selection for this session. "
                "Use `/staffing` to review/edit employee and agent choices, reply `approve` to use defaults, "
                "or `auto` / `auto recruit` to run automatic recruitment.[/info]"
            )
        return
    if checkpoint.checkpoint_type == "company_work_item_gate":
        console.print(
            "[info]Pending work-item confirmation for this session. "
            "Reply with `approve` / `continue` or `deny` / `stop`.[/info]"
        )
        return
    if checkpoint.checkpoint_type == "company_delivery_feedback":
        console.print(
            "[info]Pending delivery self-evolution review for this session. "
            "Reply with `approve` to record agreement, or explicit feedback to update employee experience. "
            "Start normal follow-up work from the regular conversation after this review is handled.[/info]"
        )
        return
    if checkpoint.checkpoint_type in {"task_user_input", "task_peer_wait", "company_peer_wait"}:
        console.print("[info]Pending checkpoint for this session. Your next reply will resume that confirmation flow.[/info]")


def _print_interactive_status(engine, state: _InteractiveChatState | None = None) -> None:
    if state is not None:
        _print_context_status(state)
    if getattr(engine, "llm", None):
        stats = getattr(engine.llm, "stats", {}) or {}
        model = getattr(getattr(getattr(engine, "config", None), "llm", None), "default_model", "unknown")
        console.print(f"  Model: {model}")
        console.print(f"  Project: {_current_project_id(engine)}")
        console.print(f"  Tokens: in={stats.get('tokens_in', 0)}, out={stats.get('tokens_out', 0)}")
        console.print(f"  Cost: ${float(stats.get('estimated_cost', 0.0) or 0.0):.4f}")
    if getattr(engine, "adapter_registry", None):
        available = engine.adapter_registry.list_available()
        console.print(f"  External agents: {', '.join(available) if available else 'none'}")


async def _load_recent_primary_session(store: OPCStore | None, project_id: str | None) -> tuple[str | None, str]:
    sessions = await _load_recent_primary_sessions(store, project_id, limit=1)
    if not sessions:
        return None, ""
    latest = sessions[0]
    return latest.session_id, (latest.title or "").strip()


async def _resolve_starting_session_id(engine) -> tuple[str, bool]:
    session_id, _ = await _load_recent_primary_session(engine.store, engine.project_id)
    if session_id:
        return session_id, True
    return str(uuid.uuid4()), False


async def _load_recent_secretary_session(engine) -> tuple[str | None, str]:
    if not engine.secretary:
        return None, ""
    sessions = await engine.secretary.list_sessions(engine.project_id, limit=1)
    if not sessions:
        return None, ""
    latest = sessions[0]
    return latest.session_id, (latest.title or "").strip()


async def _resolve_secretary_session_id(engine) -> tuple[str, bool]:
    session_id, _ = await _load_recent_secretary_session(engine)
    if session_id:
        return session_id, True
    return str(uuid.uuid4()), False


async def _run_channel_runtime(config, project: str | None) -> None:
    engine, _runtime_display = _create_cli_engine(config, project)
    try:
        await engine.initialize()
        if not engine.channel_manager:
            console.print("[warning]Channel manager is not available.[/warning]")
            return
        await engine.channel_manager.start_all()
        if not engine.channel_manager.enabled_channels:
            console.print("[warning]No channels enabled. Update .opc/config/channel_config.yaml first.[/warning]")
            return
        _write_channel_runtime_state(engine.channel_manager.enabled_channels)
        console.print(f"[success]Channel runtime started:[/success] {', '.join(engine.channel_manager.enabled_channels)}")
        await engine.message_bus.start()
    except KeyboardInterrupt:
        console.print("\n[warning]Stopping channel runtime...[/warning]")
    finally:
        _clear_channel_runtime_state()
        await engine.shutdown()


# ── Plugins ────────────────────────────────────────────────────────────────
try:
    from opc.plugins.office_ui import register_cli
    register_cli(app)
except ImportError:
    pass

try:
    from opc.plugins.cli_board import register_cli as register_cli_board
    register_cli_board(app)
except ImportError:
    pass


def main():
    keylog_path = pop_windows_sslkeylogfile()
    if keylog_path:
        console.print(
            f"[warning]{escape(format_windows_sslkeylog_warning(_current_command_label(), keylog_path))}[/warning]"
        )
    app()


if __name__ == "__main__":
    main()
