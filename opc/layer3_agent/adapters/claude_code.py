"""Claude Code CLI adapter."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer3_agent.adapters.base import (
    ExternalAgentAdapter,
    ExternalAgentStdinPolicy,
    ExternalApprovalRequest,
)
from opc.layer3_agent.skill_installer import install_opc_collab_skill


class ClaudeCodeAdapter(ExternalAgentAdapter):
    """Invokes the Claude Code CLI (claude command) for task execution."""

    agent_type = "claude_code"
    default_command = "claude"
    _INTERACTIVE_ARGV_PROMPT_MAX_BYTES = 16 * 1024
    # Lazily populated by ``_user_shell_proxy_env``. Cached at class level
    # so the (slow) ``zsh -i -c`` probe runs at most once per process.
    # ``None`` means "not probed yet"; ``{}`` means "probed, no proxy found".
    _user_shell_proxy_env_cache: dict[str, str] | None = None

    def __init__(self, config=None) -> None:
        super().__init__(config=config)
        self._process: asyncio.subprocess.Process | None = None

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
        # Small prompts stay on argv. Keep stdin open only when Claude's
        # permission mode can emit live prompts; otherwise DEVNULL avoids
        # provider-side stdin probes on Windows wrappers.
        env = self.build_process_env(extra_env)
        env = self._merge_user_shell_proxy(env)
        launch_cmd = self._resolve_launch_command(
            cmd,
            extra_env=extra_env,
            launch_metadata=launch_metadata,
        )
        stdin_policy = self.stdin_policy_for_process(launch_cmd, launch_metadata)
        stdin_target = self._stdin_target_for_policy(stdin_policy)
        if isinstance(launch_metadata, dict):
            self._record_stdin_policy_metadata(launch_metadata, stdin_policy)
        proc = await asyncio.create_subprocess_exec(
            *launch_cmd,
            stdin=stdin_target,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_path,
            env=env,
            **self._subprocess_group_kwargs(),
        )
        if prompt_transport == "stdin":
            if isinstance(launch_metadata, dict):
                launch_metadata["interactive_input_channel"] = "pipe"
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

    @classmethod
    def _merge_user_shell_proxy(
        cls, env: dict[str, str] | None
    ) -> dict[str, str] | None:
        """Layer extracted user-shell proxy vars onto the spawn env as a
        fallback (only fills in vars that are not already set).

        Returns ``None`` unchanged if there were no extracted vars and no
        ``env`` was supplied — that preserves the "inherit os.environ"
        behavior expected by :func:`asyncio.create_subprocess_exec`.
        """
        proxy = cls._user_shell_proxy_env()
        if not proxy:
            return env
        if env is None:
            import os
            env = {**os.environ}
        for key, value in proxy.items():
            env.setdefault(key, value)
        return env

    @classmethod
    def _user_shell_proxy_env(cls) -> dict[str, str]:
        """Probe the user's login shell for proxy env vars defined inside a
        ``claude`` shell function.

        Developers behind a national firewall commonly wrap ``claude`` in
        a shell function that injects ``HTTPS_PROXY`` per invocation::

            claude() {
                HTTPS_PROXY=http://... command claude "$@"
            }

        Those vars are only set when ``claude`` is invoked *through the
        shell*. OpenOPC spawns claude via
        :func:`asyncio.create_subprocess_exec`, which bypasses the shell
        and the function — so the proxy never reaches claude and the API
        call lands on a network gateway that returns ``403 Request not
        allowed``.

        Run ``$SHELL -i -c 'declare -f claude'`` once at first call and
        parse any ``*_PROXY=`` assignments out of the function body. The
        result is cached at class level. Failures (no shell function,
        unusual shell, timeout, non-zero exit) silently degrade to an
        empty dict so the spawn path is unchanged.
        """
        if cls._user_shell_proxy_env_cache is not None:
            return cls._user_shell_proxy_env_cache

        import os
        import re
        import subprocess

        shell = os.environ.get("SHELL", "").strip() or "/bin/zsh"
        try:
            result = subprocess.run(
                [shell, "-i", "-c", "declare -f claude 2>/dev/null || true"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug(
                "Skipping user-shell proxy probe ({}): {}", shell, exc
            )
            cls._user_shell_proxy_env_cache = {}
            return cls._user_shell_proxy_env_cache

        text = result.stdout or ""
        extracted: dict[str, str] = {}
        # Match `VAR="value"` or `VAR='value'` for the standard proxy
        # env names. Unquoted assignments are intentionally skipped —
        # they may contain shell variable expansions we cannot resolve
        # without actually executing the function.
        pattern = re.compile(
            r"\b("
            r"HTTPS?_PROXY|https?_proxy|"
            r"NO_PROXY|no_proxy|"
            r"ALL_PROXY|all_proxy"
            r")="
            r"([\"'])([^\"']*)\2"
        )
        for var, _, val in pattern.findall(text):
            extracted[var] = val
        if extracted:
            logger.info(
                "Extracted {} proxy var(s) from user `claude` shell function: {}",
                len(extracted),
                sorted(extracted.keys()),
            )
        cls._user_shell_proxy_env_cache = extracted
        return cls._user_shell_proxy_env_cache

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
        # Keep a broker-owned home so the shared ``opc-collab`` CLI shim is
        # installed and PATH is wired consistently. Claude Code authentication
        # intentionally remains global; setting CLAUDE_CONFIG_DIR makes Claude
        # ignore the user's normal login/keychain state and can leave company
        # mode stuck on stale isolated OAuth credentials.
        return "claude"

    def agent_home_env_vars(self, home: str) -> dict[str, str]:
        _ = home
        return {}

    def post_install_agent_home(self, home: str) -> None:
        # Claude Code discovers skills from the active user config directory.
        # Because we do not set CLAUDE_CONFIG_DIR, install the OpenOPC skill
        # into the user's normal Claude home and let the CLI use its existing
        # authenticated state.
        Path(home).mkdir(parents=True, exist_ok=True)
        user_home = Path.home() / ".claude"
        try:
            install_opc_collab_skill(user_home)
        except OSError as exc:
            logger.warning("Unable to install opc-collab into Claude user home: {}", exc)

    def can_resume_without_session_id(self) -> bool:
        return True

    def build_workspace_args(self, workspace_path: str | None = None) -> list[str]:
        if not workspace_path:
            return []
        return ["--add-dir", workspace_path]

    def _build_extra_dir_args(self, task: Task | None) -> list[str]:
        # Surface extra roots that Claude may need to edit outside the
        # primary workspace: collaboration files and durable memory.
        if task is None:
            return []
        extra: list[str] = []
        seen: set[str] = set()
        workspace = str((task.metadata or {}).get("target_output_dir") or "").strip()

        def _add(path: str) -> None:
            normalized = str(path or "").strip()
            if normalized and normalized != workspace and normalized not in seen:
                seen.add(normalized)
                extra.extend(["--add-dir", normalized])

        comms_root = str((task.metadata or {}).get("comms_workspace_root") or "").strip()
        _add(comms_root)
        try:
            from opc.core.config import get_opc_home

            _add(str(get_opc_home() / "memory"))
        except Exception:
            pass
        return extra

    def _build_argv_prompt_metadata(self, prompt: str) -> dict[str, object]:
        return {
            "prompt_transport": "argv",
            "prompt_bytes": len(prompt.encode("utf-8")),
        }

    def _build_stdin_prompt_metadata(self, prompt: str) -> dict[str, object]:
        prompt_bytes = len(prompt.encode("utf-8"))
        return {
            "prompt_transport": "stdin",
            "prompt_bytes": prompt_bytes,
            "stdin_prompt_channel": "pipe",
            "prompt_transport_reason": (
                "prompt_too_large_for_argv"
                if prompt_bytes > self._INTERACTIVE_ARGV_PROMPT_MAX_BYTES
                else "windows_multiline_argv_unsafe"
            ),
            "interactive_input_limitation": (
                "large initial prompt is delivered through stdin; live approval replies "
                "are unavailable after stdin closes"
            ),
        }

    def _interactive_prompt_transport(self, prompt: str) -> str:
        if len(prompt.encode("utf-8")) > self._INTERACTIVE_ARGV_PROMPT_MAX_BYTES:
            return "stdin"
        return "stdin" if self._windows_multiline_argv_is_unsafe(prompt) else "argv"

    def _windows_multiline_argv_is_unsafe(self, prompt: str) -> bool:
        # `claude` installed via npm resolves to a `.cmd` shim on Windows, so
        # the spawn path wraps it in `cmd.exe /d /s /c ...`. cmd.exe treats a
        # newline inside an argument as end-of-command, which silently
        # truncates a multiline prompt at the first line break (the agent then
        # sees just "## Task Brief"). Route such prompts over stdin instead.
        if os.name != "nt":
            return False
        if "\n" not in prompt and "\r" not in prompt:
            return False
        command = self.configured_command()
        resolved = shutil.which(command)
        if not resolved:
            # Unresolvable here means _resolve_launch_command will also fail to
            # resolve it and spawn the bare name, which cmd.exe never wraps.
            return False
        resolved = self._resolve_windows_command_shim(command, resolved)
        return os.path.splitext(str(resolved))[1].lower() in {".cmd", ".bat"}

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

    @staticmethod
    async def _seed_pipe_prompt(
        proc: asyncio.subprocess.Process,
        prompt: str,
    ) -> bool:
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
        prompt = self.build_task_prompt(task)
        prompt_transport = self._interactive_prompt_transport(prompt)
        cmd = [
            self.configured_command(),
            "--print",
            "--output-format", "text",
            *(["--input-format", "text"] if prompt_transport == "stdin" else []),
            *self.build_workspace_args(workspace_path),
            *self._build_extra_dir_args(task),
            *self._build_session_args(),
            *self.build_common_args(),
        ]
        if prompt_transport == "argv":
            cmd.extend(["--", prompt])
            metadata = self.build_invocation_metadata(self._redact_prompt_arg(cmd, prompt))
            metadata.update(self._build_argv_prompt_metadata(prompt))
        else:
            metadata = self.build_invocation_metadata(cmd)
            metadata.update(self._build_stdin_prompt_metadata(prompt))
        return cmd, metadata

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        prompt = self.build_task_prompt(task)
        prompt_transport = self._interactive_prompt_transport(prompt)
        # Claude Code 2.x rejects `--print --output-format stream-json` unless
        # `--verbose` is also passed (`Error: When using --print,
        # --output-format=stream-json requires --verbose`). Inject it when the
        # user has not already supplied it via `extra_args`.
        verbose_args: list[str] = []
        if not any(arg == "--verbose" for arg in self.config.extra_args):
            verbose_args = ["--verbose"]
        cmd = [
            self.configured_command(),
            "--print",
            "--output-format", "stream-json",
            *(["--input-format", "text"] if prompt_transport == "stdin" else []),
            *verbose_args,
            "--include-partial-messages",
            *self.build_workspace_args(workspace_path),
            *self._build_extra_dir_args(task),
            *self._build_permission_args(),
            *self._build_session_args(),
            *self.build_common_args(),
        ]
        if prompt_transport == "argv":
            cmd.extend(["--", prompt])
            metadata = self.build_invocation_metadata(self._redact_prompt_arg(cmd, prompt))
            metadata.update(self._build_argv_prompt_metadata(prompt))
        else:
            metadata = self.build_invocation_metadata(cmd)
            metadata.update(self._build_stdin_prompt_metadata(prompt))
        return cmd, metadata

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="Claude Code CLI not found")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)

        logger.info(f"Claude Code executing: {task.title}")

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
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=output,
                    artifacts={**metadata, "stderr": errors} if errors else metadata,
                )
            else:
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content=f"Claude Code exited with code {self._process.returncode}\n{errors}\n{output}",
                    artifacts=metadata,
                )
        except asyncio.TimeoutError:
            if self._process:
                self._process.kill()
            return TaskResult(
                status=TaskStatus.FAILED,
                content="Claude Code timed out after 600s",
                artifacts=metadata,
            )
        except Exception as e:
            return TaskResult(
                status=TaskStatus.FAILED,
                content=f"Claude Code error: {e}",
                artifacts=metadata,
            )
        finally:
            self._process = None

    def supports_approval_prompt_handling(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        return self.stdin_policy_for_process(cmd, metadata) == "pipe_open"

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentStdinPolicy:
        if str((metadata or {}).get("prompt_transport") or "").strip().lower() == "stdin":
            return "pipe_prompt_then_close"
        for index, arg in enumerate(cmd):
            value = str(arg or "").strip()
            if value == "--dangerously-skip-permissions":
                return "devnull"
            if value == "--permission-mode":
                mode = str(cmd[index + 1] if index + 1 < len(cmd) else "").strip()
                return "devnull" if mode in {"bypassPermissions", "dontAsk"} else "pipe_open"
            if value.startswith("--permission-mode="):
                mode = value.split("=", 1)[1].strip()
                return "devnull" if mode in {"bypassPermissions", "dontAsk"} else "pipe_open"
        return "pipe_open"

    # ──────────────────────────────────────────────────────────────────────
    # stream-json parsing for UI progress / transcript / approval suppression
    # ──────────────────────────────────────────────────────────────────────

    @classmethod
    def _parse_runtime_event(cls, text: str) -> dict[str, Any] | None:
        envelope = cls._parse_json_line(text)
        if not isinstance(envelope, dict):
            return None
        return envelope

    @staticmethod
    def _trim_text(text: str, *, limit: int) -> str:
        stripped = str(text or "").strip()
        if len(stripped) <= limit:
            return stripped
        return stripped[: limit - 1].rstrip() + "…"

    @classmethod
    def _summarize_tool_use(cls, block: dict[str, Any]) -> str:
        name = str(block.get("name") or "").strip() or "tool"
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        if name == "Bash":
            command = str(inp.get("command") or "").strip()
            if command:
                return f"$ {cls._trim_text(command.replace(chr(10), ' '), limit=240)}"
        if name in {"Write", "Edit", "NotebookEdit"}:
            path = str(inp.get("file_path") or inp.get("notebook_path") or "").strip()
            if path:
                return f"{name} {path}"
        if name == "Read":
            path = str(inp.get("file_path") or "").strip()
            if path:
                return f"Read {path}"
        if name in {"Glob", "Grep"}:
            pat = str(inp.get("pattern") or "").strip()
            path = str(inp.get("path") or "").strip()
            tail = f" in {path}" if path else ""
            return f"{name} {pat}{tail}".strip()
        if name in {"WebFetch", "WebSearch"}:
            target = str(inp.get("url") or inp.get("query") or "").strip()
            if target:
                return f"{name} {target}"
        # Fallback: name + compact JSON of inputs
        try:
            import json as _json
            payload = _json.dumps(inp, ensure_ascii=False, default=str)
        except Exception:
            payload = ""
        if payload and payload != "{}":
            return f"{name} {cls._trim_text(payload, limit=200)}"
        return name

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        # stderr lines: only surface real warnings/errors, drop noise.
        if stream_name != "stdout":
            stripped = str(text or "").strip()
            if not stripped:
                return None
            # Drop Claude's harmless stdin-probe message; OpenOPC owns initial
            # prompt transport explicitly via argv or stdin metadata.
            if "no stdin data received" in stripped.lower():
                return None
            return f"[External:{self.agent_type}:stderr] {self._trim_text(stripped, limit=400)}"

        envelope = self._parse_runtime_event(text)
        if not envelope:
            return None

        envelope_type = str(envelope.get("type") or "").strip()

        # Suppress all the partial-streaming chatter — we surface the full
        # message once `assistant` arrives. Without this filter the UI gets
        # flooded with raw `content_block_delta` JSON.
        if envelope_type in {
            "stream_event",
            "user",            # tool_result echoes — too verbose for the UI
            "rate_limit_event",
        }:
            return None

        if envelope_type == "system" and str(envelope.get("subtype") or "") == "init":
            session_id = str(envelope.get("session_id") or "").strip()
            model = str(envelope.get("model") or "").strip()
            bits = [b for b in (model, f"session={session_id[:8]}" if session_id else "") if b]
            return f"[External:{self.agent_type}:init] {' '.join(bits) or 'started'}"

        if envelope_type == "assistant":
            message = envelope.get("message") if isinstance(envelope.get("message"), dict) else None
            if not isinstance(message, dict):
                return None
            content = message.get("content") if isinstance(message.get("content"), list) else []
            text_parts: list[str] = []
            tool_lines: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "").strip()
                if btype == "text":
                    chunk = str(block.get("text") or "").strip()
                    if chunk:
                        text_parts.append(chunk)
                elif btype == "tool_use":
                    tool_lines.append(self._summarize_tool_use(block))
                elif btype == "thinking":
                    chunk = str(block.get("thinking") or "").strip()
                    if chunk:
                        text_parts.append(chunk)
            if tool_lines:
                # When the assistant turn ends with one or more tool calls,
                # surface the tool call(s) — that's the actionable signal.
                return f"[External:{self.agent_type}:tool] " + "\n".join(tool_lines)
            if text_parts:
                joined = "\n\n".join(text_parts)
                return f"[External:{self.agent_type}:thinking] {self._trim_text(joined, limit=2400)}"
            return None

        if envelope_type == "result":
            subtype = str(envelope.get("subtype") or "").strip()
            result_text = str(envelope.get("result") or "").strip()
            if subtype and subtype != "success":
                return f"[External:{self.agent_type}:result] {subtype}: {self._trim_text(result_text, limit=600)}"
            if result_text:
                return f"[External:{self.agent_type}:thinking] {self._trim_text(result_text, limit=2400)}"
            return None

        return None

    def normalize_result_output(self, output: str) -> str:
        """Extract the final assistant message from a stream-json transcript.

        Falls back to the raw output if no `result` envelope is found, so
        downstream gate logic still has *something* to work with.
        """
        last_result_text = ""
        last_assistant_text = ""
        for line in output.splitlines():
            envelope = self._parse_runtime_event(line)
            if not envelope:
                continue
            etype = str(envelope.get("type") or "").strip()
            if etype == "result":
                rt = str(envelope.get("result") or "").strip()
                if rt:
                    last_result_text = rt
            elif etype == "assistant":
                message = envelope.get("message") if isinstance(envelope.get("message"), dict) else None
                if not isinstance(message, dict):
                    continue
                parts: list[str] = []
                for block in message.get("content") or []:
                    if isinstance(block, dict) and str(block.get("type") or "") == "text":
                        chunk = str(block.get("text") or "").strip()
                        if chunk:
                            parts.append(chunk)
                if parts:
                    last_assistant_text = "\n\n".join(parts)
        return last_result_text or last_assistant_text or output

    def parse_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        # Claude Code's stream-json transport should only surface
        # structured approval events here. We intentionally do *not*
        # fall back to the base class's free-text prompt parser because
        # ordinary assistant output that mentions "allow"/"approve"
        # would otherwise stall the broker waiting for human input.
        event = self._parse_runtime_event(text)
        if not isinstance(event, dict):
            return None
        if str(event.get("type") or "").strip() == "permission_denial":
            return None
        return self._parse_generic_json_approval_request(text, stream_name)

    async def cancel(self, task_id: str) -> bool:
        if self._process and self._process.returncode is None:
            self._process.kill()
            return True
        return False

    def _build_permission_args(self) -> list[str]:
        common_args = self.build_common_args()
        if any(
            arg == "--permission-mode" or arg.startswith("--permission-mode=")
            for arg in common_args
        ):
            return []

        mode = str(self.config.approval_mode or "auto").strip().lower()
        if mode == "user-settings":
            return []
        if mode == "full-auto":
            return ["--permission-mode", "bypassPermissions"]
        return ["--permission-mode", "auto"]

    def _build_session_args(self) -> list[str]:
        extra_args = list(self.config.extra_args)
        common_args = self.build_common_args()
        if any(
            arg in {"-c", "--continue", "-r", "--resume"} or arg.startswith("--resume=")
            for arg in [*extra_args, *common_args]
        ):
            return []

        mode = str(self.config.session_mode or "auto").strip().lower()
        if mode != "resume":
            return []

        session_id = str(self.config.session_id or "").strip()
        if session_id:
            return ["--resume", session_id]
        return ["--continue"]
