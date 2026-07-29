"""Cursor adapter."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer3_agent.adapters.base import ExternalAgentAdapter, ExternalApprovalRequest


class CursorAdapter(ExternalAgentAdapter):
    """Invokes local Cursor for programming tasks via CLI."""

    agent_type = "cursor"
    default_command = "cursor-agent"
    # cursor-agent takes the prompt as a positional argv token (not stdin in
    # --print mode). Large company/report prompts routinely exceed OS ARG_MAX
    # and crash spawn with ``OSError: [Errno 7] Argument list too long``. Keep
    # small prompts on argv; spill larger ones to a workspace file and pass a
    # short pointer prompt instead.
    _ARGV_PROMPT_MAX_BYTES = 16 * 1024

    def __init__(self, config=None) -> None:
        super().__init__(config=config)
        self._process: asyncio.subprocess.Process | None = None
        self._thinking_buffers: dict[str, list[str]] = {}

    def resolve_binary(self) -> str | None:
        if not self.config.enabled:
            return None
        for candidate in self._candidate_commands():
            resolved = shutil.which(candidate)
            if not resolved:
                continue
            if candidate == "cursor":
                # The editor CLI is not sufficient for headless agent execution.
                continue
            return resolved
        return None

    def _runtime_command(self) -> str | None:
        for candidate in self._candidate_commands():
            if candidate == "cursor":
                continue
            if shutil.which(candidate):
                return candidate
        return None

    def _candidate_commands(self) -> list[str]:
        configured = str(self.configured_command() or "").strip()
        candidates: list[str] = []
        if configured:
            if configured == "cursor":
                candidates.append("cursor-agent")
            candidates.append(configured)
        else:
            candidates.extend(["cursor-agent", "cursor"])
        if "cursor-agent" not in candidates:
            candidates.insert(0, "cursor-agent")
        return list(dict.fromkeys(candidates))

    async def is_available(self) -> bool:
        return self.resolve_binary() is not None

    async def get_status(self) -> AgentStatus:
        if self._process and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE

    def supports_interactive(self) -> bool:
        return self._runtime_command() is not None

    def supports_session_resume(self) -> bool:
        return True

    def supports_approval_prompt_handling(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Cursor's stream-json interaction events are not stdin prompts.

        Cursor emits ``interaction_query`` request/response JSON for internal
        tool approvals, and the CLI answers those events itself. Treating that
        stream as a generic stdin approval prompt creates stale OpenOPC cards.
        """
        _ = cmd
        _ = metadata
        return False

    def agent_isolation_home_slug(self) -> str:
        return "cursor"

    def _prompt_arg_for_invocation(
        self,
        prompt: str,
        *,
        workspace_path: str | None = None,
        task: Task | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Return argv-safe prompt text plus transport metadata.

        Why file spill exists: ``cursor-agent -p`` consumes the prompt as a
        positional CLI argument. Putting a 100k+ company handoff prompt on
        argv trips Linux/macOS ``Argument list too long`` before the process
        starts, which previously crashed report cards and triggered an
        infinite Report #N reconcile storm.
        """
        prompt_text = str(prompt or "")
        prompt_bytes = len(prompt_text.encode("utf-8"))
        if prompt_bytes <= self._ARGV_PROMPT_MAX_BYTES:
            return prompt_text, {
                "prompt_transport": "argv",
                "prompt_bytes": prompt_bytes,
            }

        root = Path(str(workspace_path or "").strip() or ".").expanduser()
        try:
            root = root.resolve()
        except OSError:
            root = Path(".").resolve()
        prompt_dir = root / ".opc" / "external_prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        task_id = str(getattr(task, "id", "") or "").strip() or "task"
        digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]
        prompt_path = prompt_dir / f"cursor_{task_id}_{digest}.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")
        pointer = (
            "Open and follow the complete task instructions in this file exactly:\n"
            f"{prompt_path}\n\n"
            "Treat the file contents as your full prompt. Do not ask for confirmation "
            "before starting; do not recreate the file."
        )
        return pointer, {
            "prompt_transport": "file",
            "prompt_bytes": prompt_bytes,
            "prompt_file": str(prompt_path),
            "prompt_transport_reason": "prompt_too_large_for_argv",
        }

    @staticmethod
    def _redact_prompt_arg(cmd: list[str], prompt: str) -> list[str]:
        redacted = list(cmd)
        if redacted:
            redacted[-1] = f"<prompt:{len(prompt.encode('utf-8'))}-bytes>"
        return redacted

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        full_prompt = self.build_task_prompt(task)
        prompt_arg, transport_meta = self._prompt_arg_for_invocation(
            full_prompt,
            workspace_path=workspace_path,
            task=task,
        )
        command = self._runtime_command() or self.configured_command()
        cmd = [
            command,
            "-p",
            "--output-format",
            "text",
            *self._build_workspace_trust_args(),
            *self._build_approval_args(),
            *self._build_model_args(),
            *self._build_session_args(),
            *list(self.config.extra_args),
            prompt_arg,
        ]
        # Redact large/file-backed prompts from audit command strings so logs
        # stay small and never re-inflate ARG_MAX-sized text into metadata.
        display_cmd = (
            self._redact_prompt_arg(cmd, full_prompt)
            if transport_meta.get("prompt_transport") == "file" or len(full_prompt) > 160
            else cmd
        )
        metadata = self.build_invocation_metadata(display_cmd)
        metadata["binary"] = command
        metadata.update(transport_meta)
        return cmd, metadata

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        full_prompt = self.build_task_prompt(task)
        prompt_arg, transport_meta = self._prompt_arg_for_invocation(
            full_prompt,
            workspace_path=workspace_path,
            task=task,
        )
        command = self._runtime_command() or self.configured_command()
        cmd = [
            command,
            "-p",
            "--output-format",
            "stream-json",
            *self._build_workspace_trust_args(),
            *self._build_approval_args(),
            *self._build_model_args(),
            *self._build_session_args(),
            *list(self.config.extra_args),
            prompt_arg,
        ]
        display_cmd = (
            self._redact_prompt_arg(cmd, full_prompt)
            if transport_meta.get("prompt_transport") == "file" or len(full_prompt) > 160
            else cmd
        )
        metadata = self.build_invocation_metadata(display_cmd)
        metadata["binary"] = command
        metadata.update(transport_meta)
        return cmd, metadata

    def extract_resume_session_id(self, output: str) -> str:
        for line in output.splitlines():
            event = self._parse_json_line(line)
            if not isinstance(event, dict):
                continue
            token = self._session_id_from_event(event)
            if token:
                return token
        return super().extract_resume_session_id(output)

    def normalize_result_output(self, output: str) -> str:
        last_result = ""
        last_assistant = ""
        for line in output.splitlines():
            event = self._parse_json_line(line)
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or event.get("event") or "").strip()
            if event_type == "result":
                text = self._event_text(event)
                if text:
                    last_result = text
            elif self._event_role(event) == "assistant" or event_type in {"assistant", "assistant_message"}:
                text = self._event_text(event)
                if text:
                    last_assistant = text
        return last_result or last_assistant or output

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        if stream_name != "stdout":
            stripped = str(text or "").strip()
            return f"[External:{self.agent_type}:stderr] {stripped[:500]}" if stripped else None

        event = self._parse_json_line(text)
        if not isinstance(event, dict):
            return super().format_progress_update(text, stream_name)

        event_type = str(event.get("type") or event.get("event") or "").strip()
        if event_type in {"system", "init", "session"}:
            session_id = self._session_id_from_event(event)
            return (
                f"[External:{self.agent_type}:init] session={session_id[:8]}"
                if session_id
                else None
            )
        if "approval" in event_type or "permission" in event_type:
            return None
        if "tool" in event_type or "command" in event_type:
            summary = self._tool_summary(event)
            return f"[External:{self.agent_type}:tool] {summary}" if summary else None
        if event_type == "thinking":
            return self._format_thinking_progress(event)
        if event_type == "result":
            result = self._event_text(event)
            return f"[External:{self.agent_type}:thinking] {result[:2400]}" if result else None
        if self._event_role(event) == "assistant" or event_type in {"assistant", "assistant_message"}:
            message = self._event_text(event)
            return f"[External:{self.agent_type}:thinking] {message[:2400]}" if message else None
        return None

    def parse_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        event = self._parse_json_line(text)
        if isinstance(event, dict):
            event_type = str(event.get("type") or event.get("event") or "").strip()
            # Cursor stream-json uses these event types for normal execution.
            # Some payloads contain words such as "approved" or "allow" in web
            # content, so the generic parser must not infer an OpenOPC approval
            # card from them.
            if event_type in {
                "assistant",
                "assistant_message",
                "interaction_query",
                "result",
                "system",
                "thinking",
                "tool_call",
                "user",
            }:
                return None
        return super().parse_approval_request(text, stream_name)

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="Cursor agent CLI not found")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)

        logger.info(f"Cursor executing: {task.title}")

        try:
            stdin_policy = self.stdin_policy_for_process(cmd, metadata)
            self._record_stdin_policy_metadata(metadata, stdin_policy)
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=self._stdin_target_for_policy(stdin_policy),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
                **self._subprocess_group_kwargs(),
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
                    content=f"Cursor exited with code {self._process.returncode}\n{errors}\n{output}",
                    artifacts=metadata,
                )
        except asyncio.TimeoutError:
            if self._process:
                self._process.kill()
            return TaskResult(
                status=TaskStatus.FAILED,
                content="Cursor timed out after 600s",
                artifacts=metadata,
            )
        except Exception as e:
            return TaskResult(
                status=TaskStatus.FAILED,
                content=f"Cursor error: {e}",
                artifacts=metadata,
            )
        finally:
            self._process = None

    async def cancel(self, task_id: str) -> bool:
        if self._process and self._process.returncode is None:
            self._process.kill()
            return True
        return False

    def _build_model_args(self) -> list[str]:
        if not self.config.model:
            return []
        extra_args = list(self.config.extra_args)
        if self.config.model_flag and any(
            arg == self.config.model_flag or arg.startswith(f"{self.config.model_flag}=")
            for arg in extra_args
        ):
            return []
        flag = self.config.model_flag or "-m"
        return [flag, self.config.model]

    def _build_session_args(self) -> list[str]:
        extra_args = list(self.config.extra_args)
        if any(
            arg == "--resume" or arg.startswith("--resume=")
            for arg in extra_args
        ):
            return []

        mode = str(self.config.session_mode or "auto").strip().lower()
        if mode == "resume":
            session_id = str(self.config.session_id or "").strip()
            if session_id:
                return ["--resume", session_id]
            return []
        if mode == "new" and self.config.new_session_flag:
            return [self.config.new_session_flag]
        return []

    def _build_workspace_trust_args(self) -> list[str]:
        extra_args = list(self.config.extra_args)
        if any(arg in {"--trust", "--yolo", "-f", "--force"} for arg in extra_args):
            return []
        return ["--trust"]

    def _build_approval_args(self) -> list[str]:
        extra_args = list(self.config.extra_args)
        if any(arg in {"-f", "--force"} for arg in extra_args):
            return []

        mode = str(self.config.approval_mode or "auto").strip().lower()
        if mode == "full-auto":
            return ["--force"]
        return []

    @classmethod
    def _session_id_from_event(cls, event: dict[str, Any]) -> str:
        for key in ("session_id", "sessionId", "sessionID", "chat_id", "chatId", "conversation_id", "thread_id"):
            token = str(event.get(key) or "").strip()
            if token:
                return token
        for key in ("message", "data", "result"):
            nested = event.get(key)
            if isinstance(nested, dict):
                token = cls._session_id_from_event(nested)
                if token:
                    return token
        return ""

    @staticmethod
    def _event_role(event: dict[str, Any]) -> str:
        role = str(event.get("role") or "").strip().lower()
        if role:
            return role
        message = event.get("message")
        if isinstance(message, dict):
            return str(message.get("role") or "").strip().lower()
        return ""

    @classmethod
    def _event_text(cls, event: Any) -> str:
        if isinstance(event, str):
            return event.strip()
        if isinstance(event, list):
            parts = [cls._event_text(item) for item in event]
            return "\n".join(part for part in parts if part).strip()
        if not isinstance(event, dict):
            return ""

        for key in ("result", "text", "message", "content", "summary"):
            value = event.get(key)
            if key == "message" and isinstance(value, dict):
                nested = cls._event_text(value)
                if nested:
                    return nested
            elif isinstance(value, (str, list, dict)):
                nested = cls._event_text(value)
                if nested:
                    return nested
        return ""

    @classmethod
    def _tool_summary(cls, event: dict[str, Any]) -> str:
        source = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else event
        nested_name, nested_payload = cls._nested_cursor_tool_payload(source)
        if nested_payload:
            return cls._nested_cursor_tool_summary(nested_name, nested_payload)

        name = str(
            source.get("name")
            or source.get("tool_name")
            or source.get("toolName")
            or source.get("type")
            or "tool"
        ).strip()
        args = source.get("input") or source.get("arguments") or source.get("params") or {}
        if isinstance(args, dict):
            command = cls.normalize_shell_command(
                args.get("command") or args.get("cmd") or args.get("argv")
            )
            if command:
                return f"$ {command[:240]}"
            path = str(args.get("path") or args.get("file_path") or args.get("target") or "").strip()
            if path:
                return f"{name} {path}"
        return name

    @staticmethod
    def _nested_cursor_tool_payload(source: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        for key, value in source.items():
            if not isinstance(value, dict):
                continue
            normalized = key[:-8] if key.endswith("ToolCall") else key
            if key.endswith("ToolCall") or "args" in value or "result" in value:
                return normalized, value
        return "", {}

    @classmethod
    def _nested_cursor_tool_summary(cls, name: str, payload: dict[str, Any]) -> str:
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        lines: list[str] = []
        normalized_name = cls._normalize_name(name)
        readable_name = cls._readable_tool_name(name)
        command = cls.normalize_shell_command(
            args.get("command") or args.get("cmd") or args.get("argv")
        )
        if command:
            lines.append(f"$ {command[:240]}")
        elif normalized_name in {"websearch", "websearchrequest"}:
            query = str(args.get("searchTerm") or args.get("query") or args.get("q") or "").strip()
            lines.append(f"web search: {query}".strip())
        elif normalized_name in {"webfetch", "webfetchrequest"}:
            url = str(args.get("url") or args.get("uri") or args.get("target") or "").strip()
            lines.append(f"web fetch: {url}".strip())
        elif name:
            target = str(
                args.get("path")
                or args.get("filePath")
                or args.get("targetDirectory")
                or args.get("target")
                or args.get("globPattern")
                or ""
            ).strip()
            lines.append(f"{readable_name} {target}".strip())

        description = str(payload.get("description") or args.get("description") or "").strip()
        if description and (not lines or description not in lines[0]):
            lines.append(description[:240])

        result_text = cls._cursor_result_text(result, tool_name=name)
        if result_text:
            lines.append(result_text[:1200])
        return "\n".join(line for line in lines if line).strip() or readable_name or "tool"

    @classmethod
    def _cursor_result_text(cls, result: dict[str, Any], tool_name: str = "") -> str:
        if not result:
            return ""
        success = result.get("success") if isinstance(result.get("success"), dict) else None
        rejected = result.get("rejected") if isinstance(result.get("rejected"), dict) else None
        error = result.get("error") if isinstance(result.get("error"), dict) else None
        payload = success or rejected or error or result
        normalized_name = cls._normalize_name(tool_name)
        if rejected is not None:
            command = str(payload.get("command") or "").strip()
            reason = str(payload.get("reason") or "rejected").strip()
            return f"rejected: {command or reason}".strip()
        if normalized_name in {"websearch", "websearchrequest"}:
            return cls._summarize_web_search_result(payload)
        if normalized_name in {"webfetch", "webfetchrequest"}:
            return cls._summarize_web_fetch_result(payload)
        for key in ("stdout", "output", "text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        files = payload.get("files")
        if isinstance(files, list):
            shown = [str(item) for item in files[:20]]
            total = payload.get("totalFiles")
            suffix = f" ({total} total)" if total is not None else ""
            return "\n".join(shown) + suffix
        if error is not None:
            return f"error: {payload}"
        return ""

    def _format_thinking_progress(self, event: dict[str, Any]) -> str | None:
        subtype = str(event.get("subtype") or event.get("status") or "").strip().lower()
        message = self._event_text(event)
        key = self._thinking_buffer_key(event)
        if subtype == "delta":
            delta = self._thinking_delta_text(event)
            if delta:
                self._thinking_buffers.setdefault(key, []).append(delta)
            return None
        if subtype in {"completed", "complete", "done"}:
            buffered = "".join(self._thinking_buffers.pop(key, []))
            message = (message or buffered).strip()
            return f"[External:{self.agent_type}:thinking] {message[:2400]}" if message else None
        return f"[External:{self.agent_type}:thinking] {message[:2400]}" if message else None

    @staticmethod
    def _thinking_delta_text(event: dict[str, Any]) -> str:
        value = event.get("text")
        if isinstance(value, str):
            return value
        return CursorAdapter._event_text(event)

    @classmethod
    def _thinking_buffer_key(cls, event: dict[str, Any]) -> str:
        parts = [
            str(event.get(key) or "").strip()
            for key in ("session_id", "sessionId", "sessionID", "model_call_id", "request_id")
            if str(event.get(key) or "").strip()
        ]
        return ":".join(parts) or "default"

    @staticmethod
    def _readable_tool_name(name: str) -> str:
        raw = str(name or "tool").strip()
        raw = raw[:-8] if raw.endswith("ToolCall") else raw
        spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", raw).strip().lower()
        return spaced or "tool"

    @classmethod
    def _summarize_web_search_result(cls, payload: dict[str, Any]) -> str:
        references = payload.get("references")
        if not isinstance(references, list):
            return cls._summarize_markdown_text(payload.get("markdown") or payload.get("text") or "")

        lines: list[str] = []
        for ref in references[:5]:
            if not isinstance(ref, dict):
                continue
            title = str(ref.get("title") or "").strip()
            url = str(ref.get("url") or "").strip()
            chunk = str(ref.get("chunk") or "").strip()
            if title and title.lower() != "web search results" and url:
                lines.append(f"- {title} — {url}")
            elif title and title.lower() != "web search results":
                lines.append(f"- {title}")
            elif url:
                lines.append(f"- {url}")
            else:
                lines.extend(cls._extract_markdown_links(chunk, limit=max(0, 5 - len(lines))))
            if len(lines) >= 5:
                break

        if lines:
            return "\n".join(lines[:5])
        chunks = [
            cls._summarize_markdown_text(str(ref.get("chunk") or ""))
            for ref in references[:2]
            if isinstance(ref, dict)
        ]
        return "\n".join(chunk for chunk in chunks if chunk).strip()

    @classmethod
    def _summarize_web_fetch_result(cls, payload: dict[str, Any]) -> str:
        markdown = str(
            payload.get("markdown")
            or payload.get("content")
            or payload.get("text")
            or ""
        ).strip()
        return cls._summarize_markdown_text(markdown)

    @staticmethod
    def _extract_markdown_links(text: str, limit: int = 5) -> list[str]:
        if limit <= 0:
            return []
        links: list[str] = []
        for title, url in re.findall(r"\[[^\]\n]*?([^\]\n]+)\]\((https?://[^)\s]+)\)", text):
            clean_title = str(title or "").strip()
            clean_url = str(url or "").strip()
            if clean_title and clean_url:
                links.append(f"- {clean_title} — {clean_url}")
            if len(links) >= limit:
                break
        return links

    @staticmethod
    def _summarize_markdown_text(text: str, max_lines: int = 8) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > 180:
                line = line[:177].rstrip() + "..."
            fingerprint = line.lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            lines.append(line)
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)
