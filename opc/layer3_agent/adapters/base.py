"""Base class for external agent adapters."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Literal

from opc.core.config import ExternalAgentConfig

from opc.core.models import AgentStatus, Task, TaskResult


_APPROVAL_MARKERS = (
    "approve",
    "approval",
    "allow",
    "permission",
    "authorize",
    "confirm",
    "[y/n]",
    "(y/n)",
)
_SHELL_WRAPPER_FLAGS = {"-c", "-lc", "-command", "/c", "/command"}
_POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_POWERSHELL_SHELLS = {"pwsh", "powershell"}
_SHELL_TOOL_NAMES = {
    "bash",
    "bashtoolcall",
    "command",
    "commandexecution",
    "commandtoolcall",
    "exec",
    "execcommand",
    "execcommandtoolcall",
    "runcommand",
    "shell",
    "shellcommand",
    "shelltoolcall",
    "terminal",
    "terminalcommand",
    "terminaltoolcall",
}
ExternalAgentStdinPolicy = Literal[
    "inherit",
    "devnull",
    "pipe_open",
    "pipe_prompt_then_close",
]
_FULL_PROMPT_CONTRACT = "description_is_full_prompt"
_FILE_EDIT_TOOL_NAMES = {
    "applypatch",
    "applypatchtoolcall",
    "edit",
    "editfile",
    "edittoolcall",
    "multiedit",
    "multiedittoolcall",
    "replace",
    "rewrite",
}
_FILE_WRITE_TOOL_NAMES = {
    "createfile",
    "filewrite",
    "newfile",
    "write",
    "writefile",
    "writetoolcall",
}


@dataclass
class ExternalApprovalRequest:
    """Normalized approval request emitted by an external agent CLI."""

    approval_scope: str
    action_name: str
    prompt_text: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""


class ExternalAgentAdapter(abc.ABC):
    """Unified abstract interface for all external agents (Cursor, Claude Code, Codex, etc.)."""

    agent_type: str = ""
    default_command: str = ""

    def __init__(self, config: ExternalAgentConfig | None = None) -> None:
        self.config = config or ExternalAgentConfig(command=self.default_command)

    def configured_command(self) -> str:
        return self.config.command or self.default_command

    def resolve_binary(self) -> str | None:
        if not self.config.enabled:
            return None
        return shutil.which(self.configured_command())

    def is_new_session(self) -> bool:
        return self.config.session_mode == "new"

    def build_common_args(self) -> list[str]:
        args: list[str] = []
        if self.config.model and self.config.model_flag:
            args.extend([self.config.model_flag, self.config.model])

        if self.config.session_mode == "new" and self.config.new_session_flag:
            args.append(self.config.new_session_flag)
        elif self.config.session_mode == "resume" and self.config.resume_session_flag:
            args.append(self.config.resume_session_flag)
            if self.config.session_id:
                args.append(self.config.session_id)

        args.extend(self.config.extra_args)
        return args

    def build_invocation_metadata(self, cmd: list[str]) -> dict[str, Any]:
        return {
            "agent": self.agent_type,
            "command": shlex.join(cmd),
            "display_command": self._display_command(cmd),
            "binary": self.configured_command(),
            "model": self.config.model or "(cli default)",
            "model_flag": self.config.model_flag or "",
            "session_mode": self.config.session_mode,
            "session_id": self.config.session_id or "",
            "new_session": self.is_new_session(),
            "run_mode": self.config.run_mode,
            "approval_mode": self.config.approval_mode,
            "idle_timeout_seconds": self.config.idle_timeout_seconds,
            "status_heartbeat_seconds": self.config.status_heartbeat_seconds,
            "extra_args": list(self.config.extra_args),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "agent": self.agent_type,
            "enabled": self.config.enabled,
            "command": self.configured_command(),
            "model": self.config.model or "(cli default)",
            "model_flag": self.config.model_flag or "",
            "session_mode": self.config.session_mode,
            "session_id": self.config.session_id or "",
            "run_mode": self.config.run_mode,
            "approval_mode": self.config.approval_mode,
            "idle_timeout_seconds": self.config.idle_timeout_seconds,
            "status_heartbeat_seconds": self.config.status_heartbeat_seconds,
            "new_session_flag": self.config.new_session_flag or "",
            "resume_session_flag": self.config.resume_session_flag or "",
            "extra_args": list(self.config.extra_args),
        }

    def supports_interactive(self) -> bool:
        return False

    def supports_session_resume(self) -> bool:
        return bool(str(self.config.resume_session_flag or "").strip())

    def can_resume_without_session_id(self) -> bool:
        return False

    def supports_live_inbox_delivery(self) -> bool:
        return False

    def supports_resume_inbox_delivery(self) -> bool:
        return self.supports_session_resume() or self.supports_interactive()

    def supports_approval_prompt_handling(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Whether OpenOPC should bridge this process' live approval prompts.

        The broker can only answer a provider approval prompt when the child
        process has a live stdin transport that accepts the formatted response.
        Adapters with a different permission model can override this to avoid
        surfacing stale UI approval cards.
        """
        return self.stdin_policy_for_process(cmd, metadata) == "pipe_open"

    # ------------------------------------------------------------------
    # Skill-based collaboration surface.
    #
    # OpenOPC-spawned agents can use a dedicated home dir
    # (``<opc_home>/agent_homes/<slug>/``) so OpenOPC can install the
    # ``opc-collab`` skill + CLI shim there. Adapters may still choose to keep
    # native user config for authentication-sensitive CLIs.
    # ------------------------------------------------------------------

    def agent_isolation_home_slug(self) -> str | None:
        """Short identifier for this agent's isolated home dir, or ``None``
        when this adapter cannot host the opc-collab CLI surface.

        Returning e.g. ``"codex"`` tells the broker to (a) provision
        ``<opc_home>/agent_homes/codex/``, (b) install the ``opc-collab``
        skill there, (c) merge :meth:`agent_home_env_vars` into the launch
        env.
        """
        return None

    def agent_home_env_vars(self, home: str) -> dict[str, str]:
        """Env vars that point this agent at its isolated home dir.

        Paired with :meth:`agent_isolation_home_slug`. For codex this is
        ``{"CODEX_HOME": home}``; for opencode ``{"OPENCODE_CONFIG_DIR": home}``.
        Claude Code intentionally returns no config-dir override so it can use
        the user's existing authenticated login.
        """
        _ = home
        return {}

    def post_install_agent_home(self, home: str) -> None:
        """Agent-specific finishing touches after the skill installer has
        provisioned ``home``. Default: no-op. Codex overrides to symlink
        the user's ``~/.codex/auth.json`` so the spawned process can log
        in without re-authenticating.
        """
        _ = home
        return None

    def build_workspace_args(self, workspace_path: str | None = None) -> list[str]:
        _ = workspace_path
        return []

    def build_task_prompt(self, task: Task) -> str:
        title = str(getattr(task, "title", "") or "").strip()
        description = str(getattr(task, "description", "") or "").strip()
        metadata = dict(getattr(task, "metadata", {}) or {})
        if str(metadata.get("external_prompt_contract") or "").strip() == _FULL_PROMPT_CONTRACT:
            return description
        if not title:
            return description
        if not description:
            return title
        if title == description:
            return description
        if description.startswith(title):
            return description
        if self._description_starts_with_task_brief(description, title):
            return description
        return f"{title}\n\n{description}"

    @staticmethod
    def _description_starts_with_task_brief(description: str, title: str) -> bool:
        if not description or not title or "Task Brief" not in description:
            return False
        pattern = rf"(?ms)^##+\s+Task Brief\s*\n\s*{re.escape(title)}(?:\s*$|\s*\n)"
        return re.search(pattern, description.strip()) is not None

    @staticmethod
    def _display_command(cmd: list[str]) -> str:
        display_cmd = [str(part) for part in cmd]
        if display_cmd:
            last = display_cmd[-1]
            if "\n" in last or len(last) > 160:
                display_cmd[-1] = f"<prompt:{len(last)}-chars>"
        return shlex.join(display_cmd)

    def extract_resume_session_id(self, output: str) -> str:
        candidates: list[str] = []
        for candidate in self._iter_json_object_candidates(output):
            candidates.extend(self._collect_session_id_candidates(candidate))
        return candidates[-1] if candidates else ""

    def parse_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        request = self._parse_generic_json_approval_request(text, stream_name)
        if request:
            return request
        return self._parse_text_approval_request(text, stream_name)

    def format_approval_response(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: Any,
    ) -> str:
        _ = request
        _ = decision
        return "y\n" if approved else "n\n"

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        return self.build_invocation(task, workspace_path=workspace_path)

    def normalize_result_output(self, output: str) -> str:
        """Convert raw stdout into the user-facing task result text."""
        return output

    def extract_structured_result_fields(self, output: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for candidate in self._iter_json_object_candidates(output):
            for key in (
                "work_item_runtime_plan",
                "runtime_plan",
                "work_item_artifact_index",
                "artifact_index",
                "verification_evidence",
                "verification",
                "structured_review_verdict",
            ):
                if key in candidate and key not in payload:
                    payload[key] = candidate[key]
            # Fix 4: a flat JSON envelope of the canonical shape
            #   {"review_verdict":"reject","summary":"...","blocking_issues":[...],"followups":[...]}
            # used to degenerate into ``payload["review_verdict"] = "reject"``
            # (the inner string) because the old extractor pulled
            # ``candidate[key]`` instead of ``candidate`` when the key was
            # already present in the top-level dict. That silently dropped
            # the reviewer's actual blocking_issues/followups. Now we
            # preserve the whole candidate whenever review_verdict carries
            # a verdict label (string OR dict) AND there are sibling fields
            # that make the candidate a structured envelope.
            if "review_verdict" in candidate and "review_verdict" not in payload:
                inner = candidate["review_verdict"]
                has_sibling_fields = any(
                    key in candidate
                    for key in ("summary", "blocking_issues", "followups")
                )
                if isinstance(inner, dict):
                    payload["review_verdict"] = inner
                elif has_sibling_fields:
                    # Preserve the full envelope so downstream gets
                    # ``{review_verdict, summary, blocking_issues, followups}``.
                    payload["review_verdict"] = candidate
                else:
                    payload["review_verdict"] = inner
            if "review_verdict" not in payload and any(
                key in candidate for key in ("verdict", "decision", "status")
            ):
                payload["review_verdict"] = candidate
        return payload

    def infer_review_verdict(self, output: str) -> dict[str, Any]:
        """Extract an ``approve``/``reject`` verdict from a reviewer's output.

        The runtime treats the reviewer agent as the authoritative judge
        and does NOT second-guess verdict shape or content. This helper
        is purely a JSON parser: when the reviewer emits a structured
        verdict (per the prompt's suggested schema), we extract its
        label and pass-through fields. When no parseable verdict is
        present, we return ``{}`` and let the runtime spawn a verdict-
        parse-retry attempt.

        Returns a dict with at least ``label`` ∈ {"approve", "reject"}
        on success, or ``{}`` if no parseable verdict was found.
        """
        structured = self.extract_structured_result_fields(output)
        explicit = structured.get("review_verdict") or structured.get("structured_review_verdict")
        if isinstance(explicit, str):
            normalized = explicit.strip().lower()
            if normalized in {"approve", "approved", "pass", "passed", "accept", "accepted"}:
                return {"label": "approve", "summary": explicit.strip()}
            if normalized in {"reject", "rejected", "fail", "failed", "rework"}:
                return {"label": "reject", "summary": explicit.strip()}
            return {}
        if isinstance(explicit, dict):
            raw = str(
                explicit.get("review_verdict")
                or explicit.get("verdict")
                or explicit.get("decision")
                or explicit.get("status")
                or explicit.get("label")
                or ""
            ).strip().lower()
            if raw in {"approved", "pass", "passed", "accept", "accepted"}:
                raw = "approve"
            elif raw in {"rejected", "fail", "failed", "rework"}:
                raw = "reject"
            if raw in {"approve", "reject"}:
                blocking = explicit.get("blocking_issues", [])
                followups = explicit.get("followups", [])
                return {
                    "label": raw,
                    "summary": str(explicit.get("summary", "") or "").strip(),
                    "blocking_issues": [
                        str(item).strip()
                        for item in (blocking if isinstance(blocking, list) else [])
                        if str(item).strip()
                    ][:8],
                    "followups": [
                        str(item).strip()
                        for item in (followups if isinstance(followups, list) else [])
                        if str(item).strip()
                    ][:8],
                }
        return {}

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        """Convert a raw stream line into a user-facing progress update."""
        stripped = text.strip()
        if not stripped:
            return None
        return f"[External:{self.agent_type}:{stream_name}] {stripped[:500]}"

    def detect_runtime_failure(
        self,
        text: str,
        stream_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Return a fatal runtime failure reason if a stream line is unrecoverable."""
        _ = stream_name
        _ = metadata
        _ = text
        return None

    async def start_process(
        self,
        cmd: list[str],
        workspace_path: str,
        extra_env: dict[str, str] | None = None,
        task: Task | None = None,
        launch_metadata: dict[str, Any] | None = None,
    ) -> asyncio.subprocess.Process:
        _ = task
        _ = launch_metadata
        env = self.build_process_env(extra_env)
        launch_cmd = self._resolve_launch_command(
            cmd,
            extra_env=extra_env,
            launch_metadata=launch_metadata,
        )
        stdin_policy = self.stdin_policy_for_process(launch_cmd, launch_metadata)
        stdin_target = self._stdin_target_for_policy(stdin_policy)
        if isinstance(launch_metadata, dict):
            self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
        return await asyncio.create_subprocess_exec(
            *launch_cmd,
            stdin=stdin_target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_path,
            env=env,
            **self._subprocess_group_kwargs(),
        )

    def keep_process_stdin_open(self, cmd: list[str]) -> bool:
        return self.stdin_policy_for_process(cmd) == "pipe_open"

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentStdinPolicy:
        _ = cmd
        _ = metadata
        return "devnull"

    @staticmethod
    def _stdin_target_for_policy(policy: ExternalAgentStdinPolicy) -> Any:
        if policy == "inherit":
            return None
        if policy in {"pipe_open", "pipe_prompt_then_close"}:
            return asyncio.subprocess.PIPE
        return asyncio.subprocess.DEVNULL

    @staticmethod
    def _record_stdin_policy_metadata(
        metadata: dict[str, Any],
        policy: ExternalAgentStdinPolicy,
    ) -> None:
        metadata["stdin_policy"] = policy
        if "interactive_input_channel" in metadata:
            return
        if policy == "inherit":
            metadata["interactive_input_channel"] = "inherit"
        elif policy in {"pipe_open", "pipe_prompt_then_close"}:
            metadata["interactive_input_channel"] = "pipe"
        else:
            metadata["interactive_input_channel"] = "devnull"

    def build_process_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str] | None:
        if not extra_env:
            return None
        return {**os.environ, **{str(k): str(v) for k, v in extra_env.items()}}

    @staticmethod
    def _subprocess_group_kwargs() -> dict[str, Any]:
        if os.name == "posix":
            return {"start_new_session": True}
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {}

    @staticmethod
    def _resolve_launch_command(
        cmd: list[str],
        *,
        extra_env: dict[str, str] | None = None,
        launch_metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        if not cmd:
            return cmd
        executable = str(cmd[0] or "").strip()
        if not executable:
            return cmd
        path_value = ""
        if extra_env:
            merged = {**os.environ, **{str(k): str(v) for k, v in extra_env.items()}}
            path_value = merged.get("PATH") or merged.get("Path") or merged.get("path") or ""
        resolved = shutil.which(executable, path=path_value or None)
        if not resolved:
            return cmd

        if os.name == "nt":
            resolved = ExternalAgentAdapter._resolve_windows_command_shim(
                executable,
                resolved,
                path_value=path_value,
            )

        if os.name == "nt" and os.path.splitext(resolved)[1].lower() in {".cmd", ".bat"}:
            comspec = str(os.environ.get("COMSPEC") or "").strip()
            if not comspec:
                comspec = shutil.which("cmd.exe") or "cmd.exe"
            wrapped_cmd = [comspec, "/d", "/s", "/c", resolved, *cmd[1:]]
            if isinstance(launch_metadata, dict):
                launch_metadata.setdefault("configured_binary", executable)
                launch_metadata["resolved_binary"] = resolved
                launch_metadata["launch_wrapper"] = comspec
            return wrapped_cmd

        if resolved == executable:
            return cmd
        resolved_cmd = list(cmd)
        resolved_cmd[0] = resolved
        if isinstance(launch_metadata, dict):
            launch_metadata.setdefault("configured_binary", executable)
            launch_metadata["resolved_binary"] = resolved
        return resolved_cmd

    @staticmethod
    def _resolve_windows_command_shim(
        executable: str,
        resolved: str,
        *,
        path_value: str = "",
    ) -> str:
        """Prefer a Windows-launchable sibling over an extensionless POSIX shim."""
        if os.path.splitext(resolved)[1]:
            return resolved

        for extension in (".exe", ".com", ".cmd", ".bat"):
            sibling = f"{resolved}{extension}"
            if os.path.isfile(sibling):
                return sibling

        if os.path.dirname(executable):
            return resolved

        for extension in (".exe", ".com", ".cmd", ".bat"):
            candidate = shutil.which(f"{executable}{extension}", path=path_value or None)
            if candidate:
                return candidate
        return resolved

    async def send_process_input(
        self,
        proc: asyncio.subprocess.Process,
        text: str,
    ) -> bool:
        if not text:
            return True
        writer = getattr(proc, "stdin", None)
        if writer is None:
            return False
        if hasattr(writer, "is_closing") and writer.is_closing():
            return False
        try:
            writer.write(text.encode("utf-8"))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    async def cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
        writer = getattr(proc, "stdin", None)
        if writer is None:
            return
        if hasattr(writer, "is_closing") and writer.is_closing():
            return
        writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if callable(wait_closed):
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await wait_closed()

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Check whether this agent is installed and configured."""
        ...

    @abc.abstractmethod
    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        """Execute task in the specified workspace."""
        ...

    @abc.abstractmethod
    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build the CLI command and audit metadata for a task."""
        ...

    @abc.abstractmethod
    async def get_status(self) -> AgentStatus:
        ...

    async def get_fallback(self) -> ExternalAgentAdapter | None:
        """When this agent is unavailable, return the next preferred agent."""
        return None

    async def cancel(self, task_id: str) -> bool:
        """Interrupt an in-progress task."""
        return False

    def _parse_generic_json_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        event = self._parse_json_line(text)
        if not isinstance(event, dict):
            return None

        event_type = self._normalize_name(
            event.get("type") or event.get("event") or event.get("kind")
        )
        subtype = self._normalize_name(event.get("subtype") or event.get("status") or event.get("state"))
        explicit_event = any(
            marker in event_type or marker in subtype
            for marker in ("approval", "permission", "authorize", "confirm")
        )
        tool_name, tool_args = self._extract_structured_tool_call(event)
        prompt_text = self._extract_prompt_text(event) or text.strip()
        approval_metadata_present = any(
            key in event for key in ("approval", "approval_id", "available_decisions", "permission", "permission_id")
        )
        if not explicit_event and not (
            self._looks_like_textual_approval_prompt(prompt_text)
            and (bool(tool_name) or bool(tool_args) or approval_metadata_present)
        ):
            return None

        mapped = self._map_external_tool_request(tool_name, tool_args, prompt_text)
        metadata = {
            "stream": stream_name,
            "provider_event_type": str(event.get("type") or ""),
            "provider_event_subtype": str(event.get("subtype") or ""),
            "provider_tool_name": tool_name,
            "provider_tool_args": tool_args,
            "raw_event": event,
        }
        if mapped:
            action_name, arguments = mapped
            return ExternalApprovalRequest(
                approval_scope="tool",
                action_name=action_name,
                prompt_text=prompt_text,
                arguments=arguments,
                metadata=metadata,
                raw_text=text,
            )

        return ExternalApprovalRequest(
            approval_scope="external_agent",
            action_name=f"{self.agent_type}:prompt",
            prompt_text=prompt_text,
            metadata=metadata,
            raw_text=text,
        )

    def _parse_text_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        stripped = text.strip()
        if not stripped:
            return None
        if not self._looks_like_textual_approval_prompt(stripped):
            return None

        command = self._extract_command_from_text(stripped)
        metadata = {"stream": stream_name}
        if command:
            return ExternalApprovalRequest(
                approval_scope="tool",
                action_name="shell_exec",
                prompt_text=stripped,
                arguments={"command": command},
                metadata=metadata,
                raw_text=text,
            )
        return ExternalApprovalRequest(
            approval_scope="external_agent",
            action_name=f"{self.agent_type}:prompt",
            prompt_text=stripped,
            metadata=metadata,
            raw_text=text,
        )

    def _looks_like_textual_approval_prompt(self, text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        lowered = stripped.lower()
        if "[y/n]" in lowered or "(y/n)" in lowered:
            return True
        if re.match(r"^(approve|allow|authorize|confirm)\b", lowered):
            return True
        if re.search(r"\b(approve|allow|authorize|confirm)\b", lowered) and "?" in stripped:
            return True
        if "permission request" in lowered:
            return True
        return False

    def _extract_structured_tool_call(self, event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        tool_name = event.get("tool_name") or event.get("toolName") or event.get("name")
        tool_args = event.get("input") or event.get("arguments") or event.get("params") or event.get("tool_input")
        if tool_name:
            return str(tool_name), tool_args if isinstance(tool_args, dict) else {}

        tool_call = event.get("tool_call")
        if isinstance(tool_call, dict):
            nested_name = tool_call.get("tool_name") or tool_call.get("toolName") or tool_call.get("name")
            nested_args = tool_call.get("input") or tool_call.get("arguments") or tool_call.get("params")
            if nested_name:
                return str(nested_name), nested_args if isinstance(nested_args, dict) else {}
            if len(tool_call) == 1:
                name, payload = next(iter(tool_call.items()))
                if isinstance(payload, dict):
                    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
                    return str(name), args if isinstance(args, dict) else {}

        return "", {}

    def _map_external_tool_request(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        prompt_text: str,
    ) -> tuple[str, dict[str, Any]] | None:
        normalized_name = self._normalize_name(tool_name)
        command_value = (
            tool_args.get("command")
            or tool_args.get("cmd")
            or tool_args.get("argv")
            or tool_args.get("script")
        )
        if normalized_name in _SHELL_TOOL_NAMES or command_value:
            command = self.normalize_shell_command(command_value)
            if command:
                arguments: dict[str, Any] = {"command": command}
                working_directory = (
                    tool_args.get("cwd")
                    or tool_args.get("working_directory")
                    or tool_args.get("workdir")
                    or tool_args.get("directory")
                )
                if working_directory:
                    arguments["working_directory"] = str(working_directory)
                return "shell_exec", arguments

        path = tool_args.get("path") or tool_args.get("file_path") or tool_args.get("target") or tool_args.get("filepath")
        if normalized_name in _FILE_WRITE_TOOL_NAMES:
            arguments = {"path": str(path)} if path else {}
            return "file_write", arguments
        if normalized_name in _FILE_EDIT_TOOL_NAMES:
            arguments = {"path": str(path)} if path else {}
            return "file_edit", arguments

        prompt_command = self._extract_command_from_text(prompt_text)
        if prompt_command:
            arguments = {"command": prompt_command}
            if path:
                arguments["target"] = str(path)
            return "shell_exec", arguments
        return None

    @staticmethod
    def _parse_json_line(text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())

    @classmethod
    def normalize_shell_command(cls, command: Any) -> str:
        if isinstance(command, (list, tuple)):
            tokens = [str(item).strip() for item in command if str(item).strip()]
            raw_text = shlex.join(tokens) if tokens else ""
        else:
            raw_text = str(command or "").strip()
            if not raw_text:
                return ""
            try:
                tokens = shlex.split(raw_text)
            except ValueError:
                tokens = raw_text.split()

        if not tokens:
            return ""

        head = tokens[0].lower()
        head_name = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if head_name in _POSIX_SHELLS | _POWERSHELL_SHELLS:
            for index, token in enumerate(tokens[1:], start=1):
                lowered = token.lower()
                if lowered not in _SHELL_WRAPPER_FLAGS:
                    continue
                if index + 1 >= len(tokens):
                    break
                inner = str(tokens[index + 1]).strip()
                if not inner:
                    break
                return inner
        return raw_text if raw_text else shlex.join(tokens)

    def _extract_prompt_text(self, value: Any) -> str:
        fragments = self._collect_text_fragments(value)
        if not fragments:
            return ""
        return " | ".join(fragments[:6])[:1000]

    def _collect_text_fragments(self, value: Any) -> list[str]:
        if isinstance(value, dict):
            fragments: list[str] = []
            for key in ("message", "prompt", "reason", "summary", "description", "text", "title"):
                item = value.get(key)
                if item:
                    fragments.extend(self._collect_text_fragments(item))
            if not fragments:
                for item in value.values():
                    fragments.extend(self._collect_text_fragments(item))
            return fragments
        if isinstance(value, list):
            fragments: list[str] = []
            for item in value:
                fragments.extend(self._collect_text_fragments(item))
            return fragments
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return []

    def _collect_session_id_candidates(self, value: Any) -> list[str]:
        candidates: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = self._normalize_name(key)
                if normalized in {"sessionid", "conversationid", "threadid", "chatid"}:
                    token = str(item or "").strip()
                    if token:
                        candidates.append(token)
                candidates.extend(self._collect_session_id_candidates(item))
            return candidates
        if isinstance(value, list):
            for item in value:
                candidates.extend(self._collect_session_id_candidates(item))
        return candidates

    def _extract_command_from_text(self, text: str) -> str:
        backtick_match = re.search(r"`([^`]+)`", text)
        if backtick_match:
            return self.normalize_shell_command(backtick_match.group(1))

        quoted_match = re.search(r'"([^"\n]+)"', text)
        if quoted_match and any(keyword in text.lower() for keyword in ("command", "bash", "shell", "terminal")):
            return self.normalize_shell_command(quoted_match.group(1))

        command_match = re.search(
            r"(?:run|execute|command)\s*:\s*(.+?)(?:\?|$)",
            text,
            flags=re.IGNORECASE,
        )
        if command_match:
            return self.normalize_shell_command(command_match.group(1).strip())
        return ""

    @staticmethod
    def _iter_json_object_candidates(text: str) -> list[dict[str, Any]]:
        stripped = str(text or "").strip()
        if not stripped:
            return []
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        start = stripped.find("{")
        while start != -1:
            try:
                value, consumed = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                start = stripped.find("{", start + 1)
                continue
            if isinstance(value, dict):
                candidates.append(value)
            start = stripped.find("{", start + max(consumed, 1))
        return candidates
