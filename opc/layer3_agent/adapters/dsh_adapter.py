"""DeepSeek Harness (dsh) external agent adapter — ACP wrapper transport.

v2 drives ``dsh --profile acp`` through ``dsh_acp_wrapper`` (a small
out-of-process ACP client). The wrapper restores the OpenOPC approval-card
bridge (``session/request_permission`` -> ``ExternalApprovalRequest``),
forwards semantic progress (``session/update`` text), supports session resume,
and exits when the prompt settles so the broker's "process exit marks
completion" contract holds. ``transport: cli`` falls back to the v1 one-shot
headless CLI (no approval bridge).

Deferred:
- Company-mode external-execution workspace fence (``supports_company_execution``).
- Opaque-team execution unit (``execution_unit_kind = "opaque_external_team"``).
- Automatic provider-session resume via the broker's persisted session tokens
  (``--resume`` is already wired; session-id plumbing lands with it).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from opc.core.config import ExternalAgentConfig, get_opc_home
from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer3_agent.adapters.base import (
    ExternalAgentAdapter,
    ExternalApprovalRequest,
)

_ACP_PROFILE = "acp"
_HEADLESS_PROFILE = "headless"
_WRAPPER_MODULE = "opc.layer3_agent.adapters.dsh_acp_wrapper"
_TRANSPORT_ACP = "acp"
_TRANSPORT_CLI = "cli"


class DshAdapter(ExternalAgentAdapter):
    """Drive a DeepSeek Harness agent through its ACP stdio server."""

    agent_type = "dsh"
    default_command = "dsh"

    def __init__(self, config: ExternalAgentConfig | None = None) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # Transport selection
    # ------------------------------------------------------------------

    def _transport(self) -> str:
        configured = str(getattr(self.config, "transport", "") or "").strip().lower()
        if configured in {_TRANSPORT_ACP, _TRANSPORT_CLI}:
            return configured
        return _TRANSPORT_ACP

    def _resume_session_id(self, task: Task | None = None) -> str:
        session_id = str(self.config.session_id or "").strip()
        if session_id:
            return session_id
        if task is not None:
            metadata = dict(getattr(task, "metadata", {}) or {})
            return str(metadata.get("provider_session_id") or "").strip()
        return ""

    # ------------------------------------------------------------------
    # Capability surface
    # ------------------------------------------------------------------

    def execution_unit_kind(self) -> str:
        return "external_agent"

    def supports_company_execution(self) -> bool:
        # The broker honors this together with the durable company
        # external-execution fence (capture -> run -> validate); the wrapper's
        # --cwd already pins the validated workspace for each session/new.
        return True

    def build_task_prompt(self, task: Task) -> str:
        """Prefix resumed conversations so dsh treats the message as a follow-up.

        OpenOPC task mode sends each user message as its own task brief with no
        conversation history; continuity comes from the resumed dsh ACP session.
        When the task snapshot marks this run as a same-session resume, instruct
        dsh to treat the brief as the latest turn of the ongoing conversation
        and to use its session memory of earlier messages.
        """
        prompt = super().build_task_prompt(task)
        snapshot = dict(getattr(task, "context_snapshot", None) or {})
        runtime_resume = dict(snapshot.get("runtime_resume") or {})
        if runtime_resume.get("restored_from_same_session"):
            return (
                "You are continuing an ongoing OpenOPC conversation; earlier "
                "messages and your replies are in your session memory. Treat the "
                "following as the user's latest message in that conversation. "
                "Reply in Simplified Chinese.\n\n"
                f"{prompt}"
            )
        return prompt

    def supports_interactive(self) -> bool:
        return False

    def supports_session_resume(self) -> bool:
        return self._transport() == _TRANSPORT_ACP

    def supports_approval_prompt_handling(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        # The ACP wrapper relays approval decisions over its stdin pipe; the
        # headless transport has no live approval channel.
        return self._transport() == _TRANSPORT_ACP

    def agent_isolation_home_slug(self) -> str:
        # Isolated DSH_HOME keeps dsh settings, credentials, and session state
        # out of the user's default dsh installation.
        return self.agent_type

    def agent_home_env_vars(self, home: str) -> dict[str, str]:
        return {"DSH_HOME": str(home), "DSH_CMD": self.configured_command()}

    def build_process_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        # Always pin DSH_HOME to the isolated agent home (opc_home/agent_homes/
        # dsh/) so dsh resolves credentials, settings, and sessions there even
        # when the broker's opc-collab env injection is not active.
        env = dict(os.environ)
        if extra_env:
            env.update({str(key): str(value) for key, value in extra_env.items()})
        home = get_opc_home() / "agent_homes" / self.agent_isolation_home_slug()
        env["DSH_HOME"] = str(home)
        return env

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        prompt = self.build_task_prompt(task)
        workspace = str(Path(workspace_path or os.getcwd()).expanduser().resolve())
        if self._transport() == _TRANSPORT_CLI:
            cmd = [self.configured_command(), "--profile", _HEADLESS_PROFILE]
            cmd.extend(str(arg) for arg in (self.config.extra_args or []))
            cmd.append(prompt)
            metadata: dict[str, Any] = {
                "transport": "cli",
                "profile": _HEADLESS_PROFILE,
                "prompt_transport": "argv",
            }
            return cmd, metadata
        cmd = [sys.executable, "-m", _WRAPPER_MODULE, "--cwd", workspace, "--dsh-cmd", self.configured_command()]
        resume_id = self._resume_session_id(task)
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.extend(str(arg) for arg in (self.config.extra_args or []))
        cmd.extend(["--", prompt])
        metadata = {
            "transport": "acp",
            "profile": _ACP_PROFILE,
            "prompt_transport": "wrapper_argv",
            "resume_session_id": resume_id,
            "stdin_policy": "pipe_open",
        }
        return cmd, metadata

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if self._transport() == _TRANSPORT_ACP:
            return "pipe_open"
        return "devnull"

    async def is_available(self) -> bool:
        return self.resolve_binary() is not None

    async def get_status(self) -> AgentStatus:
        if self._process is not None and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE

    # ------------------------------------------------------------------
    # Line protocol bridging (ACP wrapper)
    # ------------------------------------------------------------------

    def parse_approval_request(
        self,
        text: str,
        stream_name: str = "",
    ) -> ExternalApprovalRequest | None:
        """Parse an ACP-wrapper approval event line into a normalized request."""
        _ = stream_name
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict) or obj.get("type") != "approval":
            return None
        tool_name = str(obj.get("toolName") or "dsh_tool_call")
        return ExternalApprovalRequest(
            approval_scope="tool",
            action_name=tool_name,
            prompt_text=str(obj.get("promptText") or ""),
            arguments=dict(obj.get("arguments") or {}),
            metadata={"call_id": str(obj.get("callId") or ""), "transport": "dsh_acp"},
            raw_text=text,
        )

    def format_approval_response(
        self,
        request: ExternalApprovalRequest,
        approved: bool,
        decision: Any,
    ) -> str:
        """Format an approval-response line the ACP wrapper relays to dsh."""
        call_id = str((request.metadata or {}).get("call_id") or "")
        if not call_id:
            return super().format_approval_response(request, approved, decision)
        return json.dumps(
            {"type": "approval_response", "callId": call_id, "allowed": bool(approved)},
            ensure_ascii=False,
        ) + "\n"

    def format_progress_update(self, text: str, stream_name: str = "") -> str | None:
        """Extract a progress text from an ACP-wrapper progress event line."""
        _ = stream_name
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(obj, dict) or obj.get("type") != "progress":
            return None
        progress = str(obj.get("text") or "")
        return progress.strip() or None

    def normalize_result_output(self, output: str) -> str:
        """Project the wrapper's protocol lines back to plain user-facing text.

        The ACP wrapper emits one JSON object per line (session / progress /
        approval) and the broker collects those raw lines into the final
        response. Keep semantic progress text, drop protocol-only lines
        (session, approval, resume bookkeeping), surface fatal reasons, and
        pass non-JSON lines through unchanged.
        """
        parts: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, TypeError):
                parts.append(stripped)
                continue
            if not isinstance(obj, dict):
                parts.append(stripped)
                continue
            kind = obj.get("type")
            if kind == "progress":
                text = str(obj.get("text") or "").strip()
                if text:
                    parts.append(text)
                continue
            if kind == "fatal":
                reason = str(obj.get("reason") or "").strip()
                if reason:
                    parts.append(f"dsh fatal: {reason}")
                continue
            # session / approval / other protocol lines are not user-facing
        return "\n".join(parts)

    def extract_resume_session_id(self, output: str) -> str:
        """Grab the ACP session id emitted by the wrapper on handshake."""
        for line in output.splitlines():
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("type") == "session":
                session_id = str(obj.get("id") or "")
                if session_id:
                    return session_id
        return ""

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="dsh executable not found")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)
        approval_mode = str(self.config.approval_mode or "auto").strip().lower()
        if self._transport() == _TRANSPORT_ACP:
            # Without the broker approval bridge, decide locally so the wrapper
            # never waits on an approval response nobody will send: auto-allow
            # permissive modes, reject everything else.
            approval_flag = "auto" if approval_mode in {"auto", "full-auto"} else "reject"
            cmd = [*cmd[:-2], "--approval", approval_flag, *cmd[-2:]]
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
                result = self.normalize_result_output(output)
                session_id = self.extract_resume_session_id(output)
                if session_id:
                    metadata["provider_session_id"] = session_id
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=result,
                    artifacts=metadata,
                )
            return TaskResult(
                status=TaskStatus.FAILED,
                content=(
                    f"dsh exited with code {self._process.returncode}\n"
                    f"{errors}\n{output}"
                ),
                artifacts=metadata,
            )
        except asyncio.TimeoutError:
            if self._process is not None and self._process.returncode is None:
                self._process.kill()
            return TaskResult(status=TaskStatus.FAILED, content="dsh timed out", artifacts=metadata)
        except Exception as exc:  # noqa: BLE001 - surface any launch failure to the caller
            return TaskResult(status=TaskStatus.FAILED, content=f"dsh error: {exc}", artifacts=metadata)
        finally:
            self._process = None

    async def cancel(self, task_id: str) -> bool:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
        return True


_TEAM_PROFILE_SOURCE = Path(__file__).resolve().parent / "assets" / "dsh" / "team" / "cordis.yml"


class DshSwarmAdapter(DshAdapter):
    """Opaque DeepSeek Harness Team adapter (one OpenOPC execution unit).

    Runs the dsh ``team`` profile (an agent that decomposes work across its own
    subagents and must close with a single JSON envelope), so OpenOPC treats
    the whole run as one opaque team: the broker owns the work item, while the
    dsh team owns every internal teammate and subtask. Team results project
    through the same envelope contract as JiuwenSwarm-team.
    """

    agent_type = "dshswarm"
    default_command = "dsh"
    _TEAM_PROFILE = "team"

    def execution_unit_kind(self) -> str:
        return "opaque_external_team"

    def supports_company_execution(self) -> bool:
        return True

    def agent_isolation_home_slug(self) -> str:
        # Same runtime as the single-agent dsh: share the isolated DSH_HOME so
        # credentials, settings, and the team profile resolve consistently.
        return "dsh"

    def _ensure_team_profile(self) -> None:
        """Install the team profile into the isolated DSH_HOME if missing."""
        target = get_opc_home() / "agent_homes" / self.agent_isolation_home_slug() / "profiles" / self._TEAM_PROFILE / "cordis.yml"
        try:
            if target.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_TEAM_PROFILE_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        self._ensure_team_profile()
        prompt = self.build_task_prompt(task)
        workspace = str(Path(workspace_path or os.getcwd()).expanduser().resolve())
        cmd = [sys.executable, "-m", _WRAPPER_MODULE, "--cwd", workspace, "--dsh-cmd", self.configured_command()]
        cmd.extend(str(arg) for arg in (self.config.extra_args or []))
        cmd.extend(["--", prompt])
        metadata: dict[str, Any] = {
            "transport": "acp",
            "profile": self._TEAM_PROFILE,
            "prompt_transport": "wrapper_argv",
            "stdin_policy": "pipe_open",
        }
        return cmd, metadata

    # ------------------------------------------------------------------
    # Team envelope contract (mirrors JiuwenSwarm-team)
    # ------------------------------------------------------------------

    _TEAM_ENVELOPE_REQUIRED = frozenset({
        "work_item_id",
        "attempt_id",
        "status",
        "summary",
        "deliverables",
        "verification",
        "risks",
        "open_questions",
        "handoff",
    })

    @staticmethod
    def _normalize_team_sequence(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if isinstance(value, (dict, str)):
            return [value] if value not in ("", {}) else []
        return [value]

    @classmethod
    def _team_result_envelope(cls, output: str) -> dict[str, Any]:
        envelope: dict[str, Any] = {}
        for candidate in cls._iter_json_object_candidates(output):
            if isinstance(candidate, dict) and cls._TEAM_ENVELOPE_REQUIRED.issubset(candidate):
                envelope = dict(candidate)
        if envelope:
            for key in ("deliverables", "risks", "open_questions"):
                envelope[key] = cls._normalize_team_sequence(envelope.get(key))
        return envelope

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
                    "kind": str(item.get("kind") or "deliverable").strip() or "deliverable",
                    "label": str(item.get("name") or item.get("label") or "deliverable").strip() or "deliverable",
                    "value": location,
                })
            for nested_key in ("files", "artifacts", "outputs", "deliverables"):
                nested = item.get(nested_key)
                if nested not in (None, "", [], {}):
                    artifacts.extend(
                        DshSwarmAdapter._artifact_index_from_deliverables(
                            nested if isinstance(nested, list) else [nested]
                        )
                    )
        return artifacts[:24]

    @staticmethod
    def _verification_evidence(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        verdict = str(value.get("verdict") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if verdict not in {"pass", "passed", "success", "fail", "failed", "unknown", "pending", "blocked"}:
            verdict = "unknown"
        evidence = str(value.get("evidence") or value.get("command") or value.get("summary") or "").strip()
        return {"verdict": verdict, "evidence": evidence} if evidence else {"verdict": verdict}

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
                "dshswarm completed without the required OpenOPC output envelope "
                "(attempt_id, deliverables, handoff, open_questions, risks, status, "
                "summary, verification, work_item_id)"
            )
        expected_work_item_id = linked_work_item_id_for_task(task)
        actual_work_item_id = str(envelope.get("work_item_id") or "").strip()
        if expected_work_item_id and actual_work_item_id != expected_work_item_id:
            return (
                "dshswarm output envelope work_item_id mismatch: "
                f"expected {expected_work_item_id!r}, got {actual_work_item_id!r}"
            )
        expected_attempt_id = str(
            (task.metadata or {}).get("attempt_id")
            or (task.metadata or {}).get("claimed_work_item_attempt_seq")
            or ""
        ).strip()
        actual_attempt_id = str(envelope.get("attempt_id") or "").strip()
        if not actual_attempt_id:
            return "dshswarm output envelope attempt_id must not be empty"
        if expected_attempt_id and actual_attempt_id != expected_attempt_id:
            return (
                "dshswarm output envelope attempt_id mismatch: "
                f"expected {expected_attempt_id!r}, got {actual_attempt_id!r}"
            )
        status = str(envelope.get("status") or "").strip().lower()
        if status not in {"complete", "completed", "done", "success", "partial", "blocked", "failed"}:
            return f"dshswarm output envelope has unsupported status {status!r}"
        if status in {"blocked", "failed"}:
            return f"dshswarm reported terminal status {status!r}: {str(envelope.get('summary') or '').strip()}"
        if not str(envelope.get("summary") or "").strip():
            return "dshswarm output envelope summary must not be empty"
        if not isinstance(envelope.get("verification"), (dict, list, str, type(None))):
            return "dshswarm output envelope verification must be an object, list, string, or null"
        if not isinstance(envelope.get("handoff"), (dict, list, str, type(None))):
            return "dshswarm output envelope handoff must be an object, list, string, or null"
        return None
