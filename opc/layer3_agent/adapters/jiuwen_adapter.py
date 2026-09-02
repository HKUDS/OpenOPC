"""Jiuwen/OpenJiuwen adapters.

``JiuwenAdapter`` behaves like Codex/Claude Code and owns one OpenOPC Task.
``JiuwenSwarmAdapter`` uses Jiuwen Team mode but remains one opaque OpenOPC
execution unit; internal teammates never leak into OpenOPC's org or Kanban.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from opc.core.config import ExternalAgentConfig
from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer3_agent.adapters.base import (
    ExternalAgentAdapter,
    ExternalAgentStdinPolicy,
    ExternalApprovalRequest,
)
from opc.layer3_agent.jiuwen_gateway import (
    probe_jiuwen_gateway,
    resolve_jiuwen_gateway_url,
)


_TEAM_MODES = frozenset(
    {
        "team",
        "team.plan",
        "team.plan.normal",
        "team.plan.code",
        "team.work.normal",
        "team.work.plan",
        "team.code.normal",
        "team.code.plan",
        "code.team",
    }
)


class JiuwenAdapter(ExternalAgentAdapter):
    """Single-agent Jiuwen adapter using CLI or Gateway transport."""

    agent_type = "jiuwen"
    default_command = "jiuwenswarm"
    default_provider_mode = "code.normal"
    display_name = "JiuwenSwarm-single"

    def __init__(self, config=None) -> None:
        if config is None:
            config = ExternalAgentConfig(
                command=self.default_command,
                run_mode="interactive",
                transport="gateway",
                gateway_url="ws://127.0.0.1:19001/tui",
                provider_mode=self.default_provider_mode,
                interactive_timeout_seconds=21600,
                approval_mode="auto",
            )
        super().__init__(config=config)
        self._process: asyncio.subprocess.Process | None = None
        # Jiuwen's Gateway emits ``chat.delta`` one token at a time.  Keep the
        # accumulator scoped by provider session/turn so OpenOPC can surface
        # readable snapshots instead of either flooding the UI or displaying
        # isolated tokens such as "leader".
        self._progress_text: dict[str, str] = {}
        self._progress_text_last_emitted: dict[str, str] = {}
        self._progress_text_last_emit_at: dict[str, float] = {}

    def _transport(self) -> str:
        value = str(getattr(self.config, "transport", "gateway") or "gateway").strip().lower()
        return value if value in {"cli", "gateway"} else "gateway"

    def _provider_mode(self, task: Task | None = None) -> str:
        task_mode = str(
            (
                (getattr(task, "metadata", {}) or {}).get("external_provider_mode")
                or (getattr(task, "metadata", {}) or {}).get("jiuwen_provider_mode", "")
            )
            if task is not None
            else ""
        ).strip()
        mode = str(task_mode or getattr(self.config, "provider_mode", "") or self.default_provider_mode).strip().lower()
        if not mode:
            mode = self.default_provider_mode
        team_mode = mode in _TEAM_MODES or "team" in mode.split(".")
        if self.execution_unit_kind() == "opaque_external_team":
            return mode if team_mode else "team"
        return self.default_provider_mode if team_mode else mode

    def resolve_binary(self) -> str | None:
        if not self.config.enabled:
            return None
        if self._transport() == "gateway":
            return sys.executable if importlib.util.find_spec("websockets") else None
        return shutil.which(self.configured_command())

    async def is_available(self) -> bool:
        if self.resolve_binary() is None:
            return False
        if self._transport() != "gateway":
            return True
        gateway_url = resolve_jiuwen_gateway_url(
            str(getattr(self.config, "gateway_url", "") or "")
        )
        issue = await asyncio.to_thread(
            probe_jiuwen_gateway,
            gateway_url,
            timeout=0.75,
        )
        return not issue

    async def get_status(self) -> AgentStatus:
        if self._process and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE

    def supports_interactive(self) -> bool:
        return self._transport() == "gateway"

    def supports_session_resume(self) -> bool:
        return True

    def supports_company_execution(self) -> bool:
        # The engine only honors this capability together with the durable
        # company external-execution fence and an explicit runtime binding.
        return True

    def agent_isolation_home_slug(self) -> str:
        # Jiuwen authentication and runtime configuration stay in Jiuwen's own
        # process. This OpenOPC-owned directory only hosts the opc-collab shim;
        # the broker prepends its bin directory to PATH for company runs.
        return self.agent_type

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        workspace = str(Path(workspace_path or os.getcwd()).expanduser().resolve())
        company_fenced = bool(
            (task.metadata or {}).get("external_company_execution_allowed") is True
            and str((task.metadata or {}).get("external_company_execution_fence") or "").strip()
            == "validated_workspace"
        )
        project_dir = (
            workspace
            if company_fenced
            else str(Path(getattr(self.config, "project_dir", "") or workspace).expanduser().resolve())
        )
        trusted_dirs = (
            [workspace]
            if company_fenced
            else [
                str(Path(item).expanduser().resolve())
                for item in list(getattr(self.config, "trusted_dirs", []) or [])
                if str(item or "").strip()
            ]
        )
        if project_dir not in trusted_dirs:
            trusted_dirs.insert(0, project_dir)
        session_id = str(self.config.session_id or "").strip()
        if not session_id:
            session_id = f"opc-{self.agent_type}-{uuid.uuid4().hex}"
        provider_mode = self._provider_mode(task)

        if self._transport() == "gateway":
            cmd = [
                sys.executable,
                "-m",
                "opc.layer3_agent.jiuwen_gateway_runner",
                "--mode",
                provider_mode,
                "--session",
                session_id,
                "--cwd",
                workspace,
                "--project-dir",
                project_dir,
            ]
            gateway_url = resolve_jiuwen_gateway_url(
                str(getattr(self.config, "gateway_url", "") or "")
            )
            if gateway_url:
                cmd.extend(["--gateway-url", gateway_url])
            for trusted_dir in trusted_dirs:
                cmd.extend(["--trusted-dir", trusted_dir])
            group = str(getattr(self.config, "agent_group_name", "") or "").strip()
            if group:
                cmd.extend(["--agent-group-name", group])
            if int(self.config.interactive_timeout_seconds or 0) > 0:
                cmd.extend(["--timeout", str(int(self.config.interactive_timeout_seconds))])
            cmd.extend(list(self.config.extra_args))
        else:
            cmd = [
                self.configured_command(),
                "chat",
                "--mode",
                provider_mode,
                "--session",
                session_id,
                "--cwd",
                workspace,
                "--project-dir",
                project_dir,
                "--jsonl",
            ]
            gateway_url = resolve_jiuwen_gateway_url(
                str(getattr(self.config, "gateway_url", "") or "")
            )
            if gateway_url:
                cmd.extend(["--gateway-url", gateway_url])
            for trusted_dir in trusted_dirs:
                cmd.extend(["--trusted-dir", trusted_dir])
            cmd.extend(list(self.config.extra_args))

        metadata = self.build_invocation_metadata(cmd)
        metadata.update(
            {
                "display_name": self.display_name,
                "transport": self._transport(),
                "provider_mode": provider_mode,
                "session_id": session_id,
                "resume_session_id": session_id,
                "provider_session_id": session_id,
                "project_dir": project_dir,
                "trusted_dirs": trusted_dirs,
                "prompt_transport": "stdio_json" if self._transport() == "gateway" else "stdin",
                "prompt_bytes": len(self.build_task_prompt(task).encode("utf-8")),
            }
        )
        self._record_stdin_policy_metadata(
            metadata,
            self.stdin_policy_for_process(cmd, metadata),
        )
        return cmd, metadata

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        return self.build_invocation(task, workspace_path=workspace_path)

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentStdinPolicy:
        _ = cmd
        _ = metadata
        return "pipe_open" if self._transport() == "gateway" else "pipe_prompt_then_close"

    def supports_approval_prompt_handling(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        _ = cmd
        _ = metadata
        return self._transport() == "gateway"

    async def start_process(
        self,
        cmd: list[str],
        workspace_path: str,
        extra_env: dict[str, str] | None = None,
        task: Task | None = None,
        launch_metadata: dict[str, Any] | None = None,
    ) -> asyncio.subprocess.Process:
        proc = await super().start_process(
            cmd,
            workspace_path,
            extra_env=extra_env,
            task=task,
            launch_metadata=launch_metadata,
        )
        prompt = self.build_task_prompt(task) if task is not None else ""
        if self._transport() == "gateway":
            payload = json.dumps(
                {"type": "start", "prompt": prompt},
                ensure_ascii=False,
            ) + "\n"
            delivered = await self.send_process_input(proc, payload)
            if not delivered:
                raise RuntimeError("failed to deliver the OpenOPC prompt to Jiuwen Gateway bridge")
        else:
            writer = getattr(proc, "stdin", None)
            if writer is None:
                raise RuntimeError("Jiuwen CLI stdin is unavailable")
            writer.write(prompt.encode("utf-8"))
            await writer.drain()
            writer.close()
        return proc

    @classmethod
    def _event(cls, text: str) -> tuple[str, dict[str, Any]]:
        value = cls._parse_json_line(text)
        if not isinstance(value, dict):
            return "", {}
        event_type = str(value.get("event") or value.get("type") or "").strip()
        if event_type == "event":
            event_type = str(value.get("event") or "").strip()
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
        return event_type, dict(payload or {})

    @staticmethod
    def _payload_text(payload: dict[str, Any]) -> str:
        for key in ("content", "message", "text", "summary", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for nested_key in ("content", "message", "text"):
                    nested = value.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
        return ""

    @staticmethod
    def _delta_text(payload: dict[str, Any]) -> str:
        """Return a streamed token without destroying Markdown boundaries."""
        for key in ("content", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                return value.replace("\r\n", "\n")
        message = payload.get("message")
        if isinstance(message, dict):
            for key in ("content", "text"):
                value = message.get(key)
                if isinstance(value, str):
                    return value.replace("\r\n", "\n")
        return ""

    @staticmethod
    def _restore_stream_markdown(value: str) -> str:
        """Repair Markdown separators dropped by Jiuwen's stream bridge.

        JiuwenSwarm 0.2.x filters whitespace-only ``llm_output`` chunks before
        emitting ``chat.delta``.  Models commonly stream Markdown newlines as
        standalone chunks, so a valid multi-paragraph response can arrive as
        one physical line (for example ``answer## Heading1. item2. item``).

        Only run the repair when structural Markdown is visibly collapsed.
        This keeps ordinary prose, URLs, dates, inline code, and already-valid
        Markdown byte-for-byte unchanged.
        """
        text = str(value or "").replace("\r\n", "\n").strip()
        if not text:
            return ""

        collapsed_heading = re.search(r"[^\n][ \t]*#{2,6}[ \t]*\S", text)
        collapsed_heading_body = re.search(
            r"(?m)^#{1,6}[ \t]*[^*|\n]{1,119}\S"
            r"(?=(?:\*\*[^*\n]{1,80}\*\*[：:]|\|[^|\n]{1,80}\|))",
            text,
        )
        collapsed_ordered = re.findall(
            r"(?<![\d.])\d{1,2}\.[ \t]+(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]))",
            text,
        )
        collapsed_bullets = re.findall(
            r"(?<![-\sA-Za-z0-9/])-(?!-)[ \t]*"
            r"(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]|[✅❌⚠☑🔹▪•]))",
            text,
        )
        if (
            not collapsed_heading
            and not collapsed_heading_body
            and len(collapsed_ordered) < 2
            and len(collapsed_bullets) < 2
        ):
            return text

        # Headings are the strongest boundary signal.  Jiuwen outputs heading
        # levels 2+ for answer sections; a single ``#`` is intentionally not
        # treated as collapsed because it is common in identifiers/fragments.
        text = re.sub(
            r"([^\n])[ \t]*(#{2,6})[ \t]*(?=\S)",
            r"\1\n\n\2 ",
            text,
        )
        text = re.sub(r"(?m)^(#{1,6})[ \t]*(?=\S)", r"\1 ", text)

        # A collapsed heading often runs directly into the first bold field
        # or a GFM table.  Leaving it on the same physical line makes the
        # Markdown renderer style the entire answer as a heading (the large,
        # bold Jiuwen cards seen in the UI).
        text = re.sub(
            r"(?m)^(#{1,6} [^*|\n]{1,119}?\S)(?=\*\*[^*\n]{1,80}\*\*[：:])",
            r"\1\n\n",
            text,
        )
        text = re.sub(
            r"(?m)^(#{1,6} [^|\n]{1,119}?\S)(?=\|[^|\n]{1,80}\|)",
            r"\1\n",
            text,
        )

        # Chinese headings frequently end in a full-width parenthesized date.
        # Restore the paragraph break when the following body was glued to it.
        text = re.sub(
            r"(?m)^(#{1,6} [^\n]{1,80}?[）)])(?=[A-Za-z0-9\u4e00-\u9fff])",
            r"\1\n\n",
            text,
        )

        if len(collapsed_ordered) >= 2:
            text = re.sub(
                r"([^\n])[ \t]*(\d{1,2}\.)[ \t]+(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]))",
                r"\1\n\2 ",
                text,
            )

        if len(collapsed_bullets) >= 2:
            # Whitespace-only newline loss leaves a list marker immediately
            # adjacent to the previous item.  Requiring a non-whitespace,
            # non-URL character on the left avoids changing ordinary
            # ``Source - URL - description`` separators.
            text = re.sub(
                r"(?<![-\sA-Za-z0-9/])-(?!-)[ \t]*"
                r"(?=(?:\*\*|[A-Za-z\u4e00-\u9fff]|[✅❌⚠☑🔹▪•]))",
                "\n- ",
                text,
            )

        # GFM table rows collapse as ``| header ||---|| value |``.  The
        # separator row is a strong enough signal to restore row boundaries
        # without touching prose that happens to contain double pipes.
        def repair_collapsed_table(match: re.Match[str]) -> str:
            table = match.group(0)
            if not re.search(r"\|\|[ \t]*:?-{3,}", table):
                return table
            # A normal row-ending pipe plus the lost-newline boundary can
            # produce either ``||`` or ``|||``. Collapse the whole run once;
            # pair-wise replacement leaves a leading ``||`` on the next row.
            return re.sub(r"\|{2,}", "|\n|", table)

        text = re.sub(r"(?m)^\|[^\n#]+", repair_collapsed_table, text)

        # Restore a horizontal-rule boundary when it was glued to the prior
        # paragraph.  Restrict the left edge to punctuation / inline code so
        # identifiers containing three hyphens stay unchanged.
        text = re.sub(
            r"(?<=[`。！？；：）)])---(?=\S)",
            "\n\n---\n\n",
            text,
        )

        return text.strip()

    @staticmethod
    def _trim_progress_text(value: Any, *, limit: int = 4000) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 16)].rstrip() + "\n… (truncated)"

    @staticmethod
    def _progress_stream_key(payload: dict[str, Any]) -> str:
        return "|".join(
            (
                str(payload.get("session_id") or "").strip(),
                str(payload.get("rid") or "").strip(),
                str(payload.get("role") or "assistant").strip(),
            )
        )

    def _append_progress_delta(self, payload: dict[str, Any]) -> str:
        key = self._progress_stream_key(payload)
        content = self._delta_text(payload)
        if content:
            self._progress_text[key] = self._progress_text.get(key, "") + content
        return key

    def _thinking_progress(
        self,
        payload: dict[str, Any],
        *,
        force: bool = False,
        reset: bool = False,
    ) -> str | None:
        exact_key = self._progress_stream_key(payload)
        keys = [exact_key] if exact_key in self._progress_text else []
        if not keys:
            session_id = str(payload.get("session_id") or "").strip()
            role = str(payload.get("role") or "").strip()
            rid = str(payload.get("rid") or "").strip()
            candidates: list[str] = []
            for candidate in self._progress_text:
                candidate_session, candidate_rid, candidate_role = candidate.split("|", 2)
                if session_id and candidate_session != session_id:
                    continue
                if role and candidate_role != role:
                    continue
                candidates.append(candidate)
            # Jiuwen commonly omits ``rid`` from chat.delta but restores it on
            # the usage/final boundary.  Prefer the same rid, then the empty
            # delta rid, before falling back to every stream in this session.
            if rid:
                preferred = [key for key in candidates if key.split("|", 2)[1] in {rid, ""}]
                keys = preferred or candidates
            else:
                keys = candidates
        contents = [
            self._trim_progress_text(self._progress_text.get(key, ""), limit=6000)
            for key in keys
        ]
        content = "\n\n".join(part for part in contents if part)
        content = self._restore_stream_markdown(content)
        if not content:
            return None
        last_content = "\n\n".join(
            self._progress_text_last_emitted.get(key, "") for key in keys
        )
        now = time.monotonic()
        last_at = max((self._progress_text_last_emit_at.get(key, 0.0) for key in keys), default=0.0)
        should_emit = content != last_content and (
            force
            or (len(content) >= 40 and (not last_content or now - last_at >= 1.25))
        )
        update = None
        if should_emit:
            for key in keys:
                self._progress_text_last_emitted[key] = self._progress_text.get(key, "")
                self._progress_text_last_emit_at[key] = now
            # ``thinking_snapshot`` is an OpenOPC transport hint: the broker
            # must not apply its generic line throttle to an already-coalesced
            # snapshot.  The UI maps it to the ordinary Thinking card.
            update = f"[External:{self.agent_type}:thinking_snapshot] {content}"
        if reset:
            for key in keys:
                self._progress_text.pop(key, None)
                self._progress_text_last_emitted.pop(key, None)
                self._progress_text_last_emit_at.pop(key, None)
        return update

    @classmethod
    def _tool_arguments(cls, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        call = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
        name = str(call.get("name") or payload.get("tool_name") or "tool").strip() or "tool"
        raw_arguments = call.get("arguments", payload.get("arguments", {}))
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError):
                parsed = {"input": raw_arguments}
        elif isinstance(raw_arguments, dict):
            parsed = dict(raw_arguments)
        else:
            parsed = {"input": raw_arguments} if raw_arguments not in (None, "") else {}
        return name, parsed

    @classmethod
    def _tool_call_progress(cls, payload: dict[str, Any]) -> str:
        name, arguments = cls._tool_arguments(payload)
        summary = name
        detail_lines: list[str] = []

        command = str(arguments.get("command") or arguments.get("cmd") or "").strip()
        description = str(
            arguments.get("description")
            or arguments.get("task_description")
            or arguments.get("activeForm")
            or ""
        ).strip()
        path = str(arguments.get("file_path") or arguments.get("path") or "").strip()
        query = str(arguments.get("query") or arguments.get("url") or "").strip()
        skill = str(arguments.get("skill_name") or "").strip()
        subagent = str(arguments.get("subagent_type") or "").strip()

        if command:
            summary = f"$ {command}"
            detail_lines.append(f"$ {command}")
        elif path:
            summary = f"{name} {path}"
        elif query:
            summary = f"{name} {query}"
        elif skill:
            summary = f"{name} {skill}"
        elif subagent:
            summary = f"{name} → {subagent}"

        if description and description not in summary:
            detail_lines.append(description)
        if not detail_lines:
            try:
                detail_lines.append(json.dumps(arguments, ensure_ascii=False, default=str, indent=2))
            except TypeError:
                detail_lines.append(str(arguments))
        detail = cls._trim_progress_text("\n".join(line for line in detail_lines if line), limit=4000)
        compact_summary = cls._trim_progress_text(summary, limit=240).replace("\n", " ")
        return f"{compact_summary}\n{detail}" if detail and detail != compact_summary else compact_summary

    @classmethod
    def _tool_result_progress(cls, payload: dict[str, Any]) -> str:
        name = str(payload.get("tool_name") or "tool").strip() or "tool"
        result = payload.get("result")
        if result in (None, "") and payload.get("raw_output") not in (None, ""):
            result = payload.get("raw_output")
        if isinstance(result, (dict, list)):
            result_text = json.dumps(result, ensure_ascii=False, default=str, indent=2)
        else:
            result_text = str(result or "").strip()
        lowered = result_text.lower()
        failed = (
            "success=false" in lowered
            or "success: false" in lowered
            or "\"success\": false" in lowered
            or ("error=" in lowered and "error=none" not in lowered)
        )
        status = "failed" if failed else "result"
        summary = f"{name} {status}"
        detail = cls._trim_progress_text(result_text or status, limit=5000)
        return f"{summary}\n{detail}"

    @staticmethod
    def _todo_progress(payload: dict[str, Any]) -> str:
        todos = [item for item in list(payload.get("todos") or []) if isinstance(item, dict)]
        if not todos:
            return ""
        completed = sum(str(item.get("status") or "").lower() == "completed" for item in todos)
        active = next(
            (
                str(item.get("activeForm") or item.get("content") or "").strip()
                for item in todos
                if str(item.get("status") or "").lower() == "in_progress"
            ),
            "",
        )
        if active:
            return f"Plan {completed}/{len(todos)} complete · {active}"
        return f"Plan {completed}/{len(todos)} complete"

    def extract_resume_session_id(self, output: str) -> str:
        candidates: list[str] = []
        for line in output.splitlines():
            _event_type, payload = self._event(line)
            for key in ("session_id", "sessionId", "conversation_id"):
                token = str(payload.get(key) or "").strip()
                if token:
                    candidates.append(token)
        return candidates[-1] if candidates else super().extract_resume_session_id(output)

    def normalize_result_output(self, output: str) -> str:
        final = ""
        current_deltas: list[str] = []
        last_completed_turn = ""

        def complete_delta_turn() -> None:
            nonlocal last_completed_turn
            content = "".join(current_deltas).strip()
            if content:
                last_completed_turn = content
            current_deltas.clear()

        for line in output.splitlines():
            event_type, payload = self._event(line)
            if event_type == "chat.delta":
                content = self._delta_text(payload)
                if content:
                    current_deltas.append(content)
                continue
            if event_type == "chat.usage_metadata":
                complete_delta_turn()
                continue
            if event_type == "chat.final":
                inner_type = str(payload.get("event_type") or "").strip()
                if inner_type == "team.error":
                    continue
                if inner_type == "chat.llm_usage":
                    complete_delta_turn()
                    continue
                if inner_type in {"", "chat.final"}:
                    content = self._payload_text(payload)
                    if content:
                        final = content
                continue
            # Older Jiuwen releases don't always send a usage boundary before
            # starting a tool.  Treat the call as the end of that narration so
            # only the last assistant turn becomes the final OPC reply.
            if event_type == "chat.tool_call":
                complete_delta_turn()

        complete_delta_turn()
        return self._restore_stream_markdown(final or last_completed_turn or output)

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        if stream_name != "stdout":
            return super().format_progress_update(text, stream_name)
        event_type, payload = self._event(text)
        if not event_type or event_type in {
            "chat.ask_user_question",
            "plan.approval_required",
            "opc.jiuwen.session",
        }:
            return None
        if event_type == "chat.delta":
            self._append_progress_delta(payload)
            return self._thinking_progress(payload)
        if event_type == "chat.tool_call":
            return f"[External:{self.agent_type}:tool] {self._tool_call_progress(payload)}"
        if event_type == "chat.tool_update":
            # The current Gateway sends the same arguments immediately after
            # ``chat.tool_call``.  Rendering both creates duplicate tool cards.
            return None
        if event_type == "chat.tool_result":
            return f"[External:{self.agent_type}:tool] {self._tool_result_progress(payload)}"
        if event_type == "todo.updated":
            summary = self._todo_progress(payload)
            return (
                f"[External:{self.agent_type}:thinking_snapshot] {summary}"
                if summary
                else None
            )
        if event_type == "chat.usage_metadata":
            # Single-agent mode uses this outer event (rather than the Team
            # ``chat.final/chat.llm_usage`` envelope) as the LLM-turn boundary.
            return self._thinking_progress(payload, force=True, reset=True)
        if event_type == "context.usage":
            return None
        if event_type == "chat.processing_status":
            if payload.get("is_processing", False) is False:
                flushed = self._thinking_progress(payload, force=True, reset=True)
                if flushed:
                    return flushed
            label = "working" if payload.get("is_processing", False) else "completed"
            return f"[External:{self.agent_type}:status] {label}"
        if event_type == "chat.final":
            inner_type = str(payload.get("event_type") or "").strip()
            if inner_type == "team.error":
                return None
            if inner_type in {
                "keepalive",
                "chat.processing_status_deferred",
                "chat.tracer_agent",
            }:
                return None
            if inner_type == "chat.llm_usage":
                return self._thinking_progress(payload, force=True, reset=True)
            if inner_type == "team.runtime_ready":
                team_name = str(payload.get("team_name") or "Jiuwen team").strip()
                return f"[External:{self.agent_type}:init] {team_name} ready"
            content = self._payload_text(payload)
            if inner_type in {"", "chat.final"}:
                # The same text was already coalesced from chat.delta at the
                # usage boundary and is also delivered as the final assistant
                # message.  Rendering it here creates a duplicate Thinking row.
                return None
            if content:
                return f"[External:{self.agent_type}:thinking_snapshot] {self._trim_progress_text(content, limit=6000)}"
            if inner_type:
                # Preserve meaningful provider lifecycle events, but never
                # regress to the unhelpful payload role (usually "leader").
                label = inner_type.replace("_", " ").replace(".", " ")
                return f"[External:{self.agent_type}:team] {label}"
            return None
        if event_type.startswith("team."):
            summary = self._payload_text(payload)
            if not summary:
                for key in ("task_name", "member_name", "name", "status"):
                    value = str(payload.get(key) or "").strip()
                    if value:
                        summary = value
                        break
            label = event_type.replace("_", " ").replace(".", " ")
            detail = f"{label}: {summary}" if summary else label
            return f"[External:{self.agent_type}:thinking_snapshot] Team · {detail}"
        summary = self._payload_text(payload)
        if not summary:
            for key in ("task_name", "name", "member_name", "status", "event_type"):
                value = str(payload.get(key) or "").strip()
                if value:
                    summary = value
                    break
        if not summary:
            summary = event_type
        channel = "team" if event_type.startswith("team.") else "event"
        return f"[External:{self.agent_type}:{channel}] {summary[:1200]}"

    def detect_runtime_failure(
        self,
        text: str,
        stream_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        _ = stream_name
        _ = metadata
        event_type, payload = self._event(text)
        if event_type == "chat.error" or (
            event_type == "chat.final" and str(payload.get("event_type") or "") == "team.error"
        ):
            return self._payload_text(payload) or "JiuwenSwarm-single runtime failed"
        return None

    def parse_approval_request(
        self,
        text: str,
        stream_name: str,
    ) -> ExternalApprovalRequest | None:
        event_type, payload = self._event(text)
        if event_type not in {"chat.ask_user_question", "plan.approval_required"}:
            return None
        question = str(payload.get("question") or payload.get("message") or "Input needed").strip()
        return ExternalApprovalRequest(
            approval_scope="external_agent",
            action_name=f"{self.agent_type}:user_input",
            prompt_text=question,
            metadata={
                "stream": stream_name,
                "provider_event_type": event_type,
                "request_id": str(payload.get("request_id") or ""),
                "source": str(payload.get("source") or ""),
                "options": list(payload.get("options") or []),
                "raw_event": payload,
            },
            raw_text=text,
        )

    def format_approval_response(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: Any,
    ) -> str:
        metadata = dict(request.metadata or {})
        options = [item for item in list(metadata.get("options") or []) if isinstance(item, dict)]
        decision_metadata = dict(getattr(decision, "metadata", {}) or {})
        human_reply = decision_metadata.get("human_reply")
        if isinstance(human_reply, dict):
            answer = str(
                human_reply.get("selected")
                or human_reply.get("answer")
                or human_reply.get("value")
                or human_reply.get("custom_input")
                or ""
            ).strip()
        else:
            answer = str(human_reply or "").strip()
        if not answer:
            desired = "approve" if approved else "reject"
            selected = ""
            for option in options:
                value = str(option.get("value") or option.get("label") or "").strip()
                normalized = value.lower()
                if desired in normalized or (approved and normalized in {"yes", "allow", "continue"}):
                    selected = value
                    break
                if not approved and normalized in {"no", "deny", "cancel"}:
                    selected = value
                    break
            answer = selected or desired
        return json.dumps(
            {
                "type": "answer",
                "selected": answer,
                "custom_input": answer,
                "approved": bool(approved),
            },
            ensure_ascii=False,
        ) + "\n"

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="JiuwenSwarm-single transport is unavailable")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)
        try:
            self._process = await self.start_process(
                cmd,
                workspace_path,
                task=task,
                launch_metadata=metadata,
            )
            stdout, stderr = await asyncio.wait_for(
                self._process.communicate(),
                timeout=max(1, int(self.config.interactive_timeout_seconds or 900)),
            )
            output = stdout.decode("utf-8", errors="replace")
            errors = stderr.decode("utf-8", errors="replace")
            if self._process.returncode == 0:
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=self.normalize_result_output(output),
                    artifacts=metadata,
                )
            return TaskResult(
                status=TaskStatus.FAILED,
                content=f"{self.agent_type} exited with code {self._process.returncode}\n{errors}\n{output}",
                artifacts=metadata,
            )
        except asyncio.TimeoutError:
            if self._process and self._process.returncode is None:
                self._process.kill()
            return TaskResult(status=TaskStatus.FAILED, content="JiuwenSwarm-single timed out", artifacts=metadata)
        except Exception as exc:
            return TaskResult(status=TaskStatus.FAILED, content=f"JiuwenSwarm-single error: {exc}", artifacts=metadata)
        finally:
            self._process = None

    async def cancel(self, task_id: str) -> bool:
        _ = task_id
        if self._process and self._process.returncode is None:
            self._process.terminate()
            return True
        return False


class JiuwenSwarmAdapter(JiuwenAdapter):
    """Opaque Jiuwen Team adapter (one OpenOPC execution unit)."""

    agent_type = "jiuwenswarm"
    default_provider_mode = "team"
    display_name = "JiuwenSwarm-team"

    def execution_unit_kind(self) -> str:
        return "opaque_external_team"

    @staticmethod
    def _artifact_index_from_deliverables(value: Any) -> list[dict[str, str]]:
        artifacts: list[dict[str, str]] = []
        for item in list(value or []):
            if isinstance(item, str) and item.strip():
                artifacts.append({"kind": "deliverable", "label": "deliverable", "value": item.strip()})
                continue
            if not isinstance(item, dict):
                continue
            location = str(
                item.get("path")
                or item.get("location")
                or item.get("value")
                or item.get("url")
                or item.get("directory_path")
                or ""
            ).strip()
            if location:
                artifacts.append({
                    "kind": str(
                        item.get("kind")
                        or ("directory" if item.get("directory_path") else "deliverable")
                    ).strip() or "deliverable",
                    "label": str(item.get("name") or item.get("label") or "deliverable").strip() or "deliverable",
                    "value": location,
                })
            for nested_key in ("files", "artifacts", "outputs", "deliverables"):
                nested = item.get(nested_key)
                if nested not in (None, "", [], {}):
                    artifacts.extend(
                        JiuwenSwarmAdapter._artifact_index_from_deliverables(
                            nested if isinstance(nested, list) else [nested]
                        )
                    )
        return artifacts[:24]

    @staticmethod
    def _normalize_team_sequence(value: Any) -> list[Any]:
        """Normalize Jiuwen's semantically plural envelope fields.

        Team leaders commonly return a structured object for a single grouped
        deliverable (for example ``{directory_path, files}``) and concise prose
        for a single risk or open question.  These are transport variants of a
        sequence, not provider failures.
        """
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if isinstance(value, (dict, str)):
            return [value] if value not in ("", {}) else []
        return [value]

    @staticmethod
    def _verification_evidence(value: Any) -> dict[str, Any]:
        def normalize_verdict(raw: Any) -> str:
            verdict = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
            if verdict in {
                "pass",
                "passed",
                "success",
                "successful",
                "ok",
                "verified",
                "all_passed",
                "complete",
                "completed",
            }:
                return "pass"
            if verdict in {"fail", "failed", "error", "not_passed", "verification_failed"}:
                return "fail"
            return ""

        if isinstance(value, list):
            checks = [dict(item) for item in value if isinstance(item, dict)]
            check_verdicts = [
                normalize_verdict(item.get("verdict") or item.get("status") or item.get("result"))
                for item in checks
            ]
            return {
                "status": "provided",
                "verdict": (
                    "fail"
                    if "fail" in check_verdicts
                    else "pass"
                    if checks and all(item == "pass" for item in check_verdicts)
                    else ""
                ),
                "summary": "Verification evidence supplied by JiuwenSwarm-team.",
                "checks": checks,
            }
        if isinstance(value, str):
            summary = value.strip()
            if not summary:
                return {}
            lowered = summary.lower()
            verdict = ""
            if any(token in lowered for token in ("failed", "failure", "error", "not pass", "失败", "未通过")):
                verdict = "fail"
            elif any(token in lowered for token in ("passed", "pass", "verified", "success", "确认", "通过", "成功")):
                verdict = "pass"
            return {
                "status": "provided",
                "verdict": verdict,
                "summary": summary,
                "checks": [],
                "raw_output": summary,
            }
        if not isinstance(value, dict):
            return {}
        evidence = dict(value)
        checks = [
            dict(item)
            for item in list(evidence.get("checks") or evidence.get("checklist") or [])
            if isinstance(item, dict)
        ]
        verdict = normalize_verdict(
            evidence.get("verdict")
            or evidence.get("status")
            or evidence.get("verification_status")
        )
        if not verdict:
            overall_signals = [
                bool(evidence[key])
                for key in (
                    "all_passed",
                    "requirements_met",
                    "passed",
                    "verified",
                    "success",
                )
                if key in evidence
            ]
            if overall_signals:
                verdict = "pass" if all(overall_signals) else "fail"
        if not verdict and checks:
            check_verdicts = [
                normalize_verdict(item.get("verdict") or item.get("status") or item.get("result"))
                for item in checks
            ]
            if "fail" in check_verdicts:
                verdict = "fail"
            elif all(item == "pass" for item in check_verdicts):
                verdict = "pass"
        return {
            "status": "provided",
            "verdict": verdict,
            "summary": str(evidence.get("summary") or "JiuwenSwarm-team verification evidence.").strip(),
            "checks": checks,
            "raw_output": str(evidence.get("raw_output") or "").strip(),
        }

    def _team_result_envelope(self, output: str) -> dict[str, Any]:
        required = {
            "work_item_id",
            "attempt_id",
            "status",
            "summary",
            "deliverables",
            "verification",
            "risks",
            "open_questions",
            "handoff",
        }
        envelope: dict[str, Any] = {}
        for candidate in self._iter_json_object_candidates(output):
            if required.issubset(candidate):
                envelope = dict(candidate)
        if not envelope:
            # Jiuwen occasionally emits an otherwise complete Team envelope
            # with an unescaped quoted phrase inside a prose list item.  The
            # strict decoder then skips the outer object and only sees nested
            # fragments, which used to turn a successful provider run into a
            # duplicate retry.  ``json_repair`` is part of the Jiuwen optional
            # integration; use it only as a boundary fallback and still demand
            # the full, explicit Team contract below.
            try:
                from json_repair import loads as repair_json_loads

                repaired = repair_json_loads(str(output or ""))
            except (ImportError, TypeError, ValueError, OSError):
                repaired = None
            repaired_candidates = (
                [repaired]
                if isinstance(repaired, dict)
                else list(repaired)
                if isinstance(repaired, list)
                else []
            )
            for candidate in repaired_candidates:
                if isinstance(candidate, dict) and required.issubset(candidate):
                    envelope = dict(candidate)
        if envelope:
            for key in ("deliverables", "risks", "open_questions"):
                envelope[key] = self._normalize_team_sequence(envelope.get(key))
        return envelope

    def extract_structured_result_fields(self, output: str) -> dict[str, Any]:
        structured = super().extract_structured_result_fields(output)
        envelope = self._team_result_envelope(output)
        if not envelope:
            return structured
        structured["opaque_external_team_result"] = envelope
        artifact_index = self._artifact_index_from_deliverables(envelope.get("deliverables"))
        if artifact_index:
            structured["work_item_artifact_index"] = artifact_index
        verification = self._verification_evidence(envelope.get("verification"))
        if verification:
            structured["verification_evidence"] = verification
        return structured

    def validate_result_output(self, output: str, task: Task) -> str | None:
        metadata = dict(task.metadata or {})
        company_mode = bool(
            str(metadata.get("execution_mode") or "").strip() == "company_mode"
            or str(metadata.get("runtime_model") or "").strip() == "multi_team_org"
            or str(metadata.get("mode") or "").strip() in {"company", "org", "custom"}
        )
        if (
            str(metadata.get("execution_unit_kind", "") or "").strip()
            != "opaque_external_team"
            or not company_mode
        ):
            return None
        envelope = self._team_result_envelope(output)
        if not envelope:
            return (
                "JiuwenSwarm-team completed without the required OpenOPC output envelope "
                "(attempt_id, deliverables, handoff, open_questions, risks, status, summary, "
                "verification, work_item_id)"
            )
        expected_work_item_id = linked_work_item_id_for_task(task)
        actual_work_item_id = str(envelope.get("work_item_id") or "").strip()
        if expected_work_item_id and actual_work_item_id != expected_work_item_id:
            return (
                "JiuwenSwarm-team output envelope work_item_id mismatch: "
                f"expected {expected_work_item_id!r}, got {actual_work_item_id!r}"
            )
        expected_attempt_id = str(
            (task.metadata or {}).get("attempt_id")
            or (task.metadata or {}).get("claimed_work_item_attempt_seq")
            or ""
        ).strip()
        actual_attempt_id = str(envelope.get("attempt_id") or "").strip()
        if not actual_attempt_id:
            return "JiuwenSwarm-team output envelope attempt_id must not be empty"
        if expected_attempt_id and actual_attempt_id != expected_attempt_id:
            return (
                "JiuwenSwarm-team output envelope attempt_id mismatch: "
                f"expected {expected_attempt_id!r}, got {actual_attempt_id!r}"
            )
        status = str(envelope.get("status") or "").strip().lower()
        if status not in {"complete", "completed", "done", "success", "partial", "blocked", "failed"}:
            return f"JiuwenSwarm-team output envelope has unsupported status {status!r}"
        if status in {"blocked", "failed"}:
            return f"JiuwenSwarm-team reported terminal status {status!r}: {str(envelope.get('summary') or '').strip()}"
        if not str(envelope.get("summary") or "").strip():
            return "JiuwenSwarm-team output envelope summary must not be empty"
        # Semantically plural fields are normalized at the transport boundary:
        # a single object/string is one item, and JSON null is an empty list.
        # Jiuwen teams use both structured evidence and concise prose, and may
        # emit JSON null when verification is not applicable.  The WorkItem
        # acceptance layer owns evidence sufficiency; this adapter only guards
        # the transport shape and must not retry a completed provider run for a
        # representational difference.
        if not isinstance(envelope.get("verification"), (dict, list, str, type(None))):
            return "JiuwenSwarm-team output envelope verification must be an object, list, string, or null"
        # A terminal boundary may have nobody left to hand off to.  Jiuwen emits
        # JSON null in that case, while some teams use a list of handoff notes.
        # Both shapes are already understood by the WorkItem capture path (empty
        # values are omitted), so validation must not reject an otherwise valid
        # completed result and trigger a duplicate provider run.
        if not isinstance(envelope.get("handoff"), (dict, list, str, type(None))):
            return "JiuwenSwarm-team output envelope handoff must be an object, list, string, or null"
        return None
