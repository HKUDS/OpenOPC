"""Codex CLI adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import uuid
from typing import Any

from loguru import logger

from opc.core.models import AgentStatus, ApprovalDecision, Task, TaskResult, TaskStatus
from opc.layer3_agent.adapters.base import (
    ExternalAgentAdapter,
    ExternalAgentStdinPolicy,
    ExternalApprovalRequest,
)


class CodexAdapter(ExternalAgentAdapter):
    """Invokes the OpenAI Codex CLI."""

    agent_type = "codex"
    default_command = "codex"
    _COMMAND_OUTPUT_LIMIT = 2000
    _MIRRORED_USER_CONFIG_FILES = ("auth.json", "config.toml")
    _PARENT_CODEX_RUNTIME_ENV_VARS = {
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_THREAD_ID",
    }
    _RUNTIME_SHELL_ENV_KEYS = {"PYTHONPATH"}
    _RUNTIME_SHELL_ENV_PREFIXES = ("OPC_",)
    _PROMPT_SENTINEL = "-"
    _TTY_EOF = b"\x04"
    _INTERACTIVE_ARGV_PROMPT_MAX_BYTES = 16 * 1024

    def __init__(self, config=None) -> None:
        super().__init__(config=config)
        self._process: asyncio.subprocess.Process | None = None
        self._input_fds: dict[int, int] = {}

    async def is_available(self) -> bool:
        return self.resolve_binary() is not None

    async def get_status(self) -> AgentStatus:
        if self._process and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE

    def supports_interactive(self) -> bool:
        return True

    def supports_session_resume(self) -> bool:
        return True

    def agent_isolation_home_slug(self) -> str:
        # Point spawned codex at ``<opc_home>/agent_homes/codex/``. The
        # broker installs the ``opc-collab`` skill under
        # ``skills/opc-collab/`` there; codex exec's native skill
        # discovery picks it up. The user's personal ``~/.codex/`` is
        # not inherited, so codex invoked directly by the user stays
        # separated from the OpenOPC collaboration surface.
        return "codex"

    def agent_home_env_vars(self, home: str) -> dict[str, str]:
        return {"CODEX_HOME": home}

    def post_install_agent_home(self, home: str) -> None:
        # Mirror the user's key Codex config files into the isolated
        # CODEX_HOME so the spawned process uses the same login and model
        # provider settings as the CLI the user runs directly. Prefer
        # symlinks so rotations are tracked; fall back to copying on
        # Windows/filesystems where symlink creation is blocked.
        from pathlib import Path

        user_home = Path.home() / ".codex"
        target_home = Path(home)
        target_home.mkdir(parents=True, exist_ok=True)
        for file_name in self._MIRRORED_USER_CONFIG_FILES:
            self._mirror_user_config_file(user_home / file_name, target_home / file_name)

    @staticmethod
    def _mirror_user_config_file(source: Path, target: Path) -> None:
        if not source.exists() or not source.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.is_symlink() or target.exists():
                if target.is_symlink() and target.resolve() == source.resolve():
                    return
                target.unlink()
            target.symlink_to(source)
        except (OSError, NotImplementedError):
            try:
                if not target.exists() or target.read_bytes() != source.read_bytes():
                    target.write_bytes(source.read_bytes())
            except OSError as exc:
                logger.warning(
                    "Unable to mirror Codex config file {} into isolated home: {}",
                    source.name,
                    exc,
                )

    def build_process_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str] | None:
        env = {
            str(key): str(value)
            for key, value in os.environ.items()
            if not self._is_parent_codex_runtime_env(str(key))
        }
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})
        return env

    @classmethod
    def _is_parent_codex_runtime_env(cls, key: str) -> bool:
        normalized = key.upper()
        return normalized in cls._PARENT_CODEX_RUNTIME_ENV_VARS

    @classmethod
    def _runtime_shell_environment_config_args(
        cls,
        extra_env: dict[str, str] | None,
    ) -> tuple[list[str], list[str]]:
        """Forward only OpenOPC's runtime contract into Codex shell tools.

        Codex can intentionally run with ``shell_environment_policy.inherit=core``.
        The broker process still receives ``OPC_*`` variables, but commands launched
        by Codex do not unless they are added to the policy's ``set`` table.
        """
        forwarded: dict[str, str] = {}
        for raw_key, raw_value in dict(extra_env or {}).items():
            key = str(raw_key).strip()
            if not key:
                continue
            if key not in cls._RUNTIME_SHELL_ENV_KEYS and not key.startswith(
                cls._RUNTIME_SHELL_ENV_PREFIXES
            ):
                continue
            value = str(raw_value)
            if key == "OPC_ALLOWED_COLLAB_TOOLS":
                try:
                    parsed = json.loads(value)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    value = ",".join(
                        str(item).strip() for item in parsed if str(item).strip()
                    )
            forwarded[key] = value

        args: list[str] = []
        for key in sorted(forwarded):
            value = forwarded[key]
            if "'''" in value:
                encoded = json.dumps(value, ensure_ascii=False)
            else:
                encoded = f"'''{value}'''"
            args.extend(["-c", f"shell_environment_policy.set.{key}={encoded}"])
        return args, sorted(forwarded)

    @classmethod
    def _inject_runtime_shell_environment(
        cls,
        cmd: list[str],
        extra_env: dict[str, str] | None,
        launch_metadata: dict[str, Any] | None = None,
    ) -> list[str]:
        config_args, forwarded_keys = cls._runtime_shell_environment_config_args(extra_env)
        if not config_args or not cmd:
            return list(cmd)
        insert_at = 2 if len(cmd) > 1 and str(cmd[1]).strip() == "exec" else 1
        launch_cmd = [*cmd[:insert_at], *config_args, *cmd[insert_at:]]
        if isinstance(launch_metadata, dict):
            launch_metadata["runtime_shell_environment_keys"] = forwarded_keys
        return launch_cmd

    def build_workspace_args(self, workspace_path: str | None = None) -> list[str]:
        args: list[str] = []
        if workspace_path:
            args.extend(["-C", workspace_path, "--add-dir", workspace_path])
        return args

    def _extra_writable_roots(self, task: Task | None) -> list[str]:
        # Surface to Codex's workspace-write sandbox every path outside
        # the main workspace that OpenOPC expects the agent (or a shell
        # it spawns) to be able to write to:
        #
        #   * ``comms_workspace_root`` — sibling of the deliverable
        #     folder; prompts tell the agent to write into
        #     ``.opc-comms/...`` there.
        #   * ``.opc/memory`` — durable global/project Markdown memory.
        #   * the OPC home directory — the ``opc-collab`` CLI (shelled
        #     out from inside the sandbox) writes to ``<opc_home>/
        #     projects/<project>/tasks.db``. Without this entry, SQLite
        #     returns SQLITE_READONLY because Codex mounts the DB path
        #     read-only. (This manifested as "runtime database opened
        #     as read-only" failures on every collab tool call.)
        if task is None:
            return []
        roots: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            normalized = str(path).strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            roots.append(normalized)

        workspace = str((task.metadata or {}).get("target_output_dir") or "").strip()
        _add_except_workspace = lambda path: _add(path) if path and path != workspace else None  # noqa: E731

        comms_root = str((task.metadata or {}).get("comms_workspace_root") or "").strip()
        _add_except_workspace(comms_root)

        # Compute the OPC home lazily so failures here (e.g. a test
        # environment without ``opc.core.config`` fully bootable) never
        # take down the adapter — the agent will still launch, just
        # without the DB writable path, and the broker will surface the
        # SQLITE_READONLY error if it matters.
        try:
            from opc.core.config import get_opc_home

            opc_home_path = get_opc_home()
            memory_root = str(opc_home_path / "memory")
            opc_home = str(opc_home_path)
        except Exception:
            memory_root = ""
            opc_home = ""
        _add_except_workspace(memory_root)
        _add_except_workspace(opc_home)

        return roots

    def _build_extra_dir_args(self, task: Task | None) -> list[str]:
        extra: list[str] = []
        for root in self._extra_writable_roots(task):
            extra.extend(["--add-dir", root])
        return extra

    def _build_writable_roots_config_args(self, task: Task | None) -> list[str]:
        roots = self._extra_writable_roots(task)
        if not roots:
            return []
        return [
            "-c",
            f"sandbox_workspace_write.writable_roots={json.dumps(roots, ensure_ascii=False)}",
        ]

    def _build_stdin_prompt_metadata(self, task: Task) -> dict[str, object]:
        prompt = self.build_task_prompt(task)
        return {
            "prompt_transport": "stdin",
            "prompt_bytes": len(prompt.encode("utf-8")),
        }

    def _build_argv_prompt_metadata(self, prompt: str) -> dict[str, object]:
        return {
            "prompt_transport": "argv",
            "prompt_bytes": len(prompt.encode("utf-8")),
        }

    def _interactive_prompt_transport(self, prompt: str) -> str:
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes <= self._INTERACTIVE_ARGV_PROMPT_MAX_BYTES and self._windows_multiline_argv_is_unsafe(prompt):
            return "stdin"
        return "argv" if prompt_bytes <= self._INTERACTIVE_ARGV_PROMPT_MAX_BYTES else "stdin"

    def _windows_multiline_argv_is_unsafe(self, prompt: str) -> bool:
        if os.name != "nt":
            return False
        if "\n" not in prompt and "\r" not in prompt:
            return False
        command = self.configured_command()
        resolved = shutil.which(command) or command
        suffix = os.path.splitext(str(resolved or ""))[1].lower()
        return suffix in {"", ".cmd", ".bat", ".ps1", ".com", ".exe"}

    @staticmethod
    def _redact_prompt_arg(cmd: list[str], prompt: str) -> list[str]:
        redacted = list(cmd)
        if redacted:
            redacted[-1] = f"<prompt:{len(prompt.encode('utf-8'))}-bytes>"
        return redacted

    def _prompt_text_from_task(self, task: Task | None) -> str:
        if task is None:
            return ""
        return self.build_task_prompt(task)

    @classmethod
    def _tty_prompt_payload(cls, prompt: str) -> bytes:
        payload = prompt.encode("utf-8")
        if payload and not payload.endswith(b"\n"):
            payload += b"\n"
        return payload + cls._TTY_EOF

    async def _seed_tty_prompt(self, proc: asyncio.subprocess.Process, prompt: str) -> None:
        input_fd = self._input_fds.get(proc.pid)
        if input_fd is None:
            return
        await asyncio.to_thread(
            self._write_input_bytes,
            input_fd,
            self._tty_prompt_payload(prompt),
        )

    @staticmethod
    async def _seed_pipe_prompt(
        proc: asyncio.subprocess.Process,
        prompt: str,
    ) -> bool:
        """Write *prompt* to the subprocess stdin pipe and close it.

        Returns ``True`` when the full payload was delivered, ``False``
        if the pipe broke before delivery completed (the child may have
        exited early or rejected the data).
        """
        writer = getattr(proc, "stdin", None)
        if writer is None:
            return False
        payload = prompt.encode("utf-8")
        delivered = True
        try:
            writer.write(payload)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.warning(
                "Pipe prompt delivery failed ({} bytes planned): {}",
                len(payload),
                exc,
            )
            delivered = False
        writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if callable(wait_closed):
            try:
                await wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                delivered = False
        return delivered

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        session_id = str(self.config.session_id or "").strip()
        if str(self.config.session_mode or "").strip().lower() == "resume" and session_id:
            cmd = [
                self.configured_command(),
                "exec",
                "resume",
                "--skip-git-repo-check",
                *self._build_resume_approval_args(),
                *self._build_resume_model_args(),
                session_id,
                self._PROMPT_SENTINEL,
            ]
        else:
            cmd = [
                self.configured_command(),
                "exec",
                *self.build_workspace_args(workspace_path),
                *self._build_extra_dir_args(task),
                *self._build_writable_roots_config_args(task),
                "--skip-git-repo-check",
                *self._build_approval_args(),
                *self.build_common_args(),
                self._PROMPT_SENTINEL,
            ]
        metadata = self.build_invocation_metadata(cmd)
        metadata.update(self._build_stdin_prompt_metadata(task))
        self._record_stdin_policy_metadata(
            metadata,
            self.stdin_policy_for_process(cmd, metadata),
        )
        if (
            metadata.get("stdin_policy") == "pipe_open"
            and self._uses_interactive_input_channel(cmd)
            and self._supports_pty_input_channel()
        ):
            metadata["interactive_input_channel"] = "pty"
        return cmd, metadata

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        session_id = str(self.config.session_id or "").strip()
        prompt = self._prompt_text_from_task(task)
        prompt_transport = self._interactive_prompt_transport(prompt)
        prompt_arg = prompt if prompt_transport == "argv" else self._PROMPT_SENTINEL
        if str(self.config.session_mode or "").strip().lower() == "resume" and session_id:
            cmd = [
                self.configured_command(),
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--json",
                *self._build_resume_approval_args(),
                *self._build_resume_model_args(),
                session_id,
                prompt_arg,
            ]
        else:
            cmd = [
                self.configured_command(),
                "exec",
                *self.build_workspace_args(workspace_path),
                *self._build_extra_dir_args(task),
                *self._build_writable_roots_config_args(task),
                "--skip-git-repo-check",
                "--json",
                *self._build_approval_args(),
                *self.build_common_args(),
                prompt_arg,
            ]
        # Small interactive prompts stay on argv because Codex CLI 0.130+
        # can exit before a PTY-backed `exec --json -` prompt is seeded. Large
        # prompts must not use argv: macOS/Linux command-line limits turn the
        # final-delivery assessment into an `Argument list too long` failure.
        if prompt_transport == "argv":
            metadata = self.build_invocation_metadata(self._redact_prompt_arg(cmd, prompt))
            metadata.update(self._build_argv_prompt_metadata(prompt))
        else:
            metadata = self.build_invocation_metadata(cmd)
            metadata.update(self._build_stdin_prompt_metadata(task))
            metadata["stdin_prompt_channel"] = "pipe"
            metadata["prompt_transport_reason"] = (
                "prompt_too_large_for_argv"
                if len(prompt.encode("utf-8")) > self._INTERACTIVE_ARGV_PROMPT_MAX_BYTES
                else "windows_multiline_argv_unsafe"
            )
            metadata["interactive_input_limitation"] = (
                "initial prompt is delivered through stdin; live approval replies "
                "may be unavailable after stdin closes"
            )
        self._record_stdin_policy_metadata(
            metadata,
            self.stdin_policy_for_process(cmd, metadata),
        )
        return cmd, metadata

    def extract_resume_session_id(self, output: str) -> str:
        for line in output.splitlines():
            event = self._parse_runtime_event(line)
            if not event:
                continue
            if str(event.get("type") or "").strip() != "thread.started":
                continue
            thread_id = str(event.get("thread_id") or "").strip()
            if thread_id:
                return thread_id
        return super().extract_resume_session_id(output)

    @classmethod
    def _parse_runtime_event(cls, text: str) -> dict[str, Any] | None:
        envelope = cls._parse_json_line(text)
        if not isinstance(envelope, dict):
            return None
        event = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else envelope
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip()
        return event if event_type else None

    @staticmethod
    def _trim_text(text: str, *, limit: int) -> str:
        stripped = str(text or "").strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 1].rstrip() + "…"

    @classmethod
    def _command_summary(cls, item: dict[str, Any]) -> str:
        command = cls.normalize_shell_command(item.get("command"))
        if not command:
            command = str(item.get("command") or "").strip()
        if not command:
            return "command execution"
        return cls._trim_text(command.replace("\n", " "), limit=120)

    @classmethod
    def _command_detail(cls, item: dict[str, Any], *, include_output: bool) -> str:
        parts: list[str] = []
        command = cls.normalize_shell_command(item.get("command"))
        if command:
            parts.append(f"$ {command}")
        raw_command = str(item.get("command") or "").strip()
        if not command and raw_command:
            parts.append(f"$ {raw_command}")

        if include_output:
            output = str(item.get("aggregated_output") or "").strip()
            if output:
                parts.append(cls._trim_text(output, limit=cls._COMMAND_OUTPUT_LIMIT))
            status = str(item.get("status") or "").strip()
            exit_code = item.get("exit_code")
            status_bits: list[str] = []
            if status:
                status_bits.append(f"status={status}")
            if exit_code is not None:
                status_bits.append(f"exit_code={exit_code}")
            if status_bits:
                parts.append(", ".join(status_bits))

        return "\n\n".join(part for part in parts if part)

    @classmethod
    def normalize_transcript_text(cls, output: str) -> str:
        last_completed_agent_message = ""
        last_agent_message = ""

        for line in output.splitlines():
            event = cls._parse_runtime_event(line)
            if not event:
                continue
            if str(event.get("type") or "").strip() not in {"item.started", "item.completed"}:
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            if not isinstance(item, dict) or str(item.get("type") or "").strip() != "agent_message":
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            last_agent_message = text
            if str(event.get("type") or "").strip() == "item.completed":
                last_completed_agent_message = text

        return last_completed_agent_message or last_agent_message or output

    def normalize_result_output(self, output: str) -> str:
        return self.normalize_transcript_text(output)

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        event = self._parse_runtime_event(text)
        if not event:
            return super().format_progress_update(text, stream_name)

        event_type = str(event.get("type") or "").strip()
        if event_type in {"exec_approval_request", "apply_patch_approval_request"}:
            return None

        if event_type not in {"item.started", "item.completed"}:
            return None

        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if not isinstance(item, dict):
            return None

        item_type = str(item.get("type") or "").strip()
        if item_type == "agent_message" and event_type == "item.completed":
            message = self._trim_text(str(item.get("text") or ""), limit=2400)
            if not message:
                return None
            return f"[External:{self.agent_type}:thinking] {message}"

        if item_type == "command_execution":
            detail = self._command_detail(item, include_output=event_type == "item.completed")
            if not detail:
                detail = self._command_summary(item)
            return f"[External:{self.agent_type}:tool] {detail}"

        return None

    def detect_runtime_failure(
        self,
        text: str,
        stream_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        _ = stream_name
        if "Reading additional input from stdin..." not in str(text):
            return None
        policy = str((metadata or {}).get("stdin_policy") or "").strip()
        prompt_transport = str((metadata or {}).get("prompt_transport") or "").strip().lower()
        if prompt_transport == "stdin" or policy == "pipe_prompt_then_close":
            return None
        return (
            "Codex entered supplemental stdin intake mode (`Reading additional input from stdin...`) "
            "instead of executing the delegated task prompt."
        )

    def _uses_interactive_input_channel(self, cmd: list[str]) -> bool:
        return "--json" in cmd

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentStdinPolicy:
        prompt_transport = str((metadata or {}).get("prompt_transport") or "").strip().lower()
        if prompt_transport == "stdin" or (cmd and str(cmd[-1]).strip() == self._PROMPT_SENTINEL):
            return "pipe_prompt_then_close"
        if self._uses_interactive_input_channel(cmd):
            return "pipe_open" if self._supports_pty_input_channel() else "inherit"
        return super().stdin_policy_for_process(cmd, metadata)

    @staticmethod
    def _supports_pty_input_channel() -> bool:
        return callable(getattr(os, "openpty", None))

    def _build_resume_model_args(self) -> list[str]:
        if not self.config.model:
            return []
        return [self.config.model_flag or "--model", self.config.model]

    async def start_process(
        self,
        cmd: list[str],
        workspace_path: str,
        extra_env: dict[str, str] | None = None,
        task: Task | None = None,
        launch_metadata: dict[str, Any] | None = None,
    ) -> asyncio.subprocess.Process:
        prompt_transport = str((launch_metadata or {}).get("prompt_transport") or "").strip().lower()
        prompt = self._prompt_text_from_task(task) if prompt_transport == "stdin" else ""
        env = self.build_process_env(extra_env)
        runtime_cmd = self._inject_runtime_shell_environment(
            cmd,
            extra_env,
            launch_metadata=launch_metadata,
        )
        launch_cmd = self._resolve_launch_command(
            runtime_cmd,
            extra_env=extra_env,
            launch_metadata=launch_metadata,
        )
        stdin_prompt_channel = str((launch_metadata or {}).get("stdin_prompt_channel") or "").strip().lower()
        stdin_policy = self.stdin_policy_for_process(launch_cmd, launch_metadata)

        if not self._uses_interactive_input_channel(launch_cmd):
            proc = await super().start_process(
                launch_cmd,
                workspace_path,
                extra_env=extra_env,
                task=task,
                launch_metadata=launch_metadata,
            )
            if prompt_transport == "stdin":
                delivered = await self._seed_pipe_prompt(proc, prompt)
                if not delivered:
                    logger.warning(
                        "Stdin prompt delivery to {} (pid={}) may be incomplete; "
                        "the process might fail with an encoding error",
                        self.agent_type,
                        proc.pid,
                    )
            return proc

        if prompt_transport == "stdin" and stdin_prompt_channel == "pipe":
            if isinstance(launch_metadata, dict):
                self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
            proc = await super().start_process(
                launch_cmd,
                workspace_path,
                extra_env=extra_env,
                task=task,
                launch_metadata=launch_metadata,
            )
            delivered = await self._seed_pipe_prompt(proc, prompt)
            if not delivered:
                if isinstance(launch_metadata, dict):
                    launch_metadata["prompt_delivery_failed"] = True
                logger.warning(
                    "Large stdin prompt delivery to {} (pid={}) may be incomplete",
                    self.agent_type,
                    proc.pid,
                )
            return proc

        if not self._supports_pty_input_channel():
            if prompt_transport == "stdin":
                if isinstance(launch_metadata, dict):
                    self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
                    launch_metadata["interactive_input_limitation"] = (
                        "initial stdin prompt is delivered through a pipe on PTY-less platforms; "
                        "live approval replies require a PTY-capable platform"
                    )
                logger.info(
                    "PTY-backed Codex input is unavailable on this platform; using stdin prompt "
                    "pipe delivery. Live approval replies require a PTY-capable platform."
                )
                proc = await super().start_process(
                    launch_cmd,
                    workspace_path,
                    extra_env=extra_env,
                    task=task,
                    launch_metadata=launch_metadata,
                )
                delivered = await self._seed_pipe_prompt(proc, prompt)
                if not delivered:
                    logger.warning(
                        "Stdin prompt delivery to {} (pid={}) may be incomplete; "
                        "the process might fail with an encoding error",
                        self.agent_type,
                        proc.pid,
                    )
                return proc
            if isinstance(launch_metadata, dict):
                self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
                launch_metadata["interactive_input_limitation"] = (
                    "stdin is inherited for argv prompt delivery on PTY-less platforms; "
                    "live approval replies require a PTY-capable platform"
                )
            logger.info(
                "PTY-backed Codex input is unavailable on this platform; using argv prompt "
                "delivery with inherited stdin. Live approval replies require a PTY-capable platform."
            )
            proc = await asyncio.create_subprocess_exec(
                *launch_cmd,
                stdin=None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
                env=env,
                **self._subprocess_group_kwargs(),
            )
            return proc

        if isinstance(launch_metadata, dict):
            self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
            launch_metadata["interactive_input_channel"] = "pty"
        master_fd, slave_fd = os.openpty()
        try:
            proc = await asyncio.create_subprocess_exec(
                *launch_cmd,
                stdin=slave_fd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
                env=env,
                **self._subprocess_group_kwargs(),
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)

        self._input_fds[proc.pid] = master_fd
        if prompt_transport == "stdin":
            try:
                await self._seed_tty_prompt(proc, prompt)
            except OSError:
                self._input_fds.pop(proc.pid, None)
                with contextlib.suppress(OSError):
                    os.close(master_fd)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
        return proc

    async def send_process_input(
        self,
        proc: asyncio.subprocess.Process,
        text: str,
    ) -> bool:
        if not text:
            return True
        input_fd = self._input_fds.get(proc.pid)
        if input_fd is None:
            return await super().send_process_input(proc, text)
        payload = text.encode("utf-8")
        try:
            await asyncio.to_thread(self._write_input_bytes, input_fd, payload)
        except OSError:
            return False
        return True

    async def cleanup_process(self, proc: asyncio.subprocess.Process) -> None:
        input_fd = self._input_fds.pop(proc.pid, None)
        if input_fd is not None:
            with contextlib.suppress(OSError):
                os.close(input_fd)
        await super().cleanup_process(proc)

    @staticmethod
    def _write_input_bytes(input_fd: int, payload: bytes) -> None:
        written = 0
        while written < len(payload):
            written += os.write(input_fd, payload[written:])

    def parse_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        envelope = self._parse_json_line(text)
        if not isinstance(envelope, dict):
            return None

        event = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else envelope
        if not isinstance(event, dict):
            return None

        event_type = str(event.get("type") or "").strip()
        common_metadata = {
            "stream": stream_name,
            "provider_event_type": event_type,
            "approval_id": str(event.get("call_id") or event.get("id") or ""),
            "turn_id": str(event.get("turn_id") or ""),
            "raw_event": event,
        }

        if event_type == "exec_approval_request":
            command = self.normalize_shell_command(event.get("command"))
            arguments: dict[str, object] = {}
            if command:
                arguments["command"] = command
            cwd = str(event.get("cwd") or "").strip()
            if cwd:
                arguments["working_directory"] = cwd
            prompt_text = str(event.get("reason") or "").strip()
            if not prompt_text and command:
                prompt_text = f"Allow Codex to run `{command}`?"
            metadata = {
                **common_metadata,
                "cwd": cwd,
                "command": command,
                "network_approval_context": event.get("network_approval_context"),
                "additional_permissions": event.get("additional_permissions"),
                "proposed_execpolicy_amendment": event.get("proposed_execpolicy_amendment"),
                "proposed_network_policy_amendments": event.get("proposed_network_policy_amendments"),
                "available_decisions": event.get("available_decisions"),
            }
            return ExternalApprovalRequest(
                approval_scope="tool",
                action_name="shell_exec",
                prompt_text=prompt_text,
                arguments=arguments,
                metadata=metadata,
                raw_text=text,
            )

        if event_type == "apply_patch_approval_request":
            changes = event.get("changes")
            paths = sorted(str(path) for path in changes.keys()) if isinstance(changes, dict) else []
            grant_root = str(event.get("grant_root") or "").strip()
            arguments: dict[str, object] = {}
            if grant_root:
                arguments["path"] = grant_root
            elif len(paths) == 1:
                arguments["path"] = paths[0]
            elif paths:
                arguments["target"] = paths[0]
            prompt_text = str(event.get("reason") or "").strip()
            if not prompt_text:
                prompt_text = "Allow Codex to apply file changes?"
            metadata = {
                **common_metadata,
                "grant_root": grant_root,
                "paths": paths,
                "available_decisions": event.get("available_decisions"),
            }
            return ExternalApprovalRequest(
                approval_scope="tool",
                action_name="file_edit",
                prompt_text=prompt_text,
                arguments=arguments,
                metadata=metadata,
                raw_text=text,
            )

        # Codex interactive runs are launched with `--json`, so real approval
        # prompts arrive as structured approval_request events. Falling back to
        # the generic parser here causes ordinary command logs or sandbox error
        # text to be misclassified as approval prompts.
        return None

    def format_approval_response(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: ApprovalDecision,
    ) -> str:
        event_type = str(request.metadata.get("provider_event_type") or "").strip()
        if event_type not in {"exec_approval_request", "apply_patch_approval_request"}:
            return super().format_approval_response(request, approved, decision)

        approval_id = str(request.metadata.get("approval_id") or "").strip()
        if not approval_id:
            return super().format_approval_response(request, approved, decision)

        op = {
            "type": "exec_approval" if event_type == "exec_approval_request" else "patch_approval",
            "id": approval_id,
            "decision": self._review_decision_payload(request, approved, decision),
        }
        turn_id = str(request.metadata.get("turn_id") or "").strip()
        if event_type == "exec_approval_request" and turn_id:
            op["turn_id"] = turn_id
        return json.dumps({"id": str(uuid.uuid4()), "op": op}, ensure_ascii=False) + "\n"

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="Codex CLI not found")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)

        logger.info(f"Codex executing: {task.title}")

        try:
            self._process = await self.start_process(
                cmd,
                workspace_path,
                task=task,
                launch_metadata=metadata,
            )
            stdout, stderr = await asyncio.wait_for(
                self._process.communicate(), timeout=600
            )
            output = stdout.decode("utf-8", errors="replace")
            errors = stderr.decode("utf-8", errors="replace")

            if self._process.returncode == 0:
                return TaskResult(status=TaskStatus.DONE, content=output, artifacts=metadata)
            else:
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content=f"Codex exited with code {self._process.returncode}\n{errors}\n{output}",
                    artifacts=metadata,
                )
        except asyncio.TimeoutError:
            if self._process:
                self._process.kill()
            return TaskResult(status=TaskStatus.FAILED, content="Codex timed out", artifacts=metadata)
        except Exception as e:
            return TaskResult(status=TaskStatus.FAILED, content=f"Codex error: {e}", artifacts=metadata)
        finally:
            self._process = None

    async def cancel(self, task_id: str) -> bool:
        if self._process and self._process.returncode is None:
            self._process.kill()
            return True
        return False

    def _build_approval_args(self) -> list[str]:
        common_args = self.build_common_args()
        # Skip injection if the user has manually configured sandbox / approval
        # flags via `extra_args` so we don't trample explicit overrides.
        conflict_markers = {
            "--dangerously-bypass-approvals-and-sandbox",
            "--full-auto",
            "-s",
            "--sandbox",
        }
        for arg in common_args:
            if arg in conflict_markers:
                return []
            if arg.startswith("--sandbox="):
                return []

        mode = str(self.config.approval_mode or "auto").strip().lower()
        if mode == "user-settings":
            return []
        if mode == "full-auto":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["--sandbox", "danger-full-access"]

    def _build_resume_approval_args(self) -> list[str]:
        common_args = self.build_common_args()
        if any(arg in {"--dangerously-bypass-approvals-and-sandbox", "--full-auto"} for arg in common_args):
            return []

        mode = str(self.config.approval_mode or "auto").strip().lower()
        if mode == "user-settings":
            return []
        if mode == "full-auto":
            return ["--dangerously-bypass-approvals-and-sandbox"]

        # `codex exec resume` does not accept the `--sandbox` flag that plain
        # `codex exec` supports, but it does accept config overrides.
        return ["-c", 'sandbox_mode="danger-full-access"']

    def _review_decision_payload(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: ApprovalDecision,
    ) -> str | dict[str, object]:
        if not approved:
            return "denied"

        human_reply = str((decision.metadata or {}).get("human_reply") or "").strip().lower()
        if human_reply not in {"always_project", "always_global"}:
            return "approved"

        event_type = str(request.metadata.get("provider_event_type") or "").strip()
        if event_type == "exec_approval_request":
            proposed_execpolicy = request.metadata.get("proposed_execpolicy_amendment")
            if isinstance(proposed_execpolicy, dict):
                return {
                    "approved_execpolicy_amendment": {
                        "proposed_execpolicy_amendment": proposed_execpolicy,
                    }
                }

            amendments = request.metadata.get("proposed_network_policy_amendments")
            if isinstance(amendments, list):
                for amendment in amendments:
                    if isinstance(amendment, dict) and str(amendment.get("action") or "").lower() == "allow":
                        return {"network_policy_amendment": {"network_policy_amendment": amendment}}

            if request.metadata.get("network_approval_context") or request.metadata.get("additional_permissions"):
                return "approved_for_session"

        if event_type == "apply_patch_approval_request" and request.metadata.get("grant_root"):
            return "approved_for_session"
        return "approved"
