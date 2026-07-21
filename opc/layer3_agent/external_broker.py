"""External agent broker with approval-aware execution and session persistence."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Coroutine

from loguru import logger

from opc.core.company_tools import (
    company_collaboration_enabled_for_task,
    resolve_task_collaboration_tools,
)
from opc.core.config import DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS, get_opc_home
from opc.core.events import EventBus
from opc.core.models import ApprovalAction, ExternalSession, Task, TaskResult, TaskStatus, VerificationEvidence
from opc.core.worker_envelope import classify_worker_message
from opc.database.store import OPCStore
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.collaboration_service import CollaborationContext, CollaborationService
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.work_item_runtime import is_work_item_runtime_metadata
from opc.layer2_organization.work_item_identity import projection_id_for_task, turn_type_for_task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task, set_linked_work_item_id
from opc.layer3_agent.adapters.base import ExternalAgentAdapter
from opc.layer3_agent.external_session_identity import (
    external_session_allows_resume,
    external_session_matches_provider_token,
    is_provider_session_token,
    provider_token_from_external_session,
    select_best_external_resume_session,
)
from opc.layer3_agent.preflight import (
    assert_external_agent_write_contract,
    ExternalAgentPreflightError,
)
from opc.layer3_agent.skill_installer import (
    install_collab_surface,
    opc_collab_executable,
    prepend_to_path,
)
from opc.layer5_memory.markdown_memory import MarkdownMemoryStore
from opc.layer4_tools.collaboration_rpc import (
    OPC_COLLAB_RPC_HOST,
    OPC_COLLAB_RPC_PATH,
    OPC_COLLAB_RPC_PORT,
    OPC_COLLAB_RPC_TRANSPORT,
    start_collaboration_rpc_server,
)


def _collaboration_role_cfg(org_engine: Any | None, role_id: str) -> Any | None:
    if org_engine is None or not role_id:
        return None
    try:
        return org_engine.get_agent(role_id)
    except Exception:
        return None


class ExternalAgentBroker:
    """Coordinates approval, execution mode, and session persistence for external agents."""

    _STREAM_READ_SIZE = 8192
    _MAX_PATH_HINT_TOKEN_LENGTH = 512
    # Result statuses that prove the provider attempt terminally failed, so
    # its session token must not stay pinned. Parks (awaiting_human /
    # awaiting_peer / awaiting_manager_review) and cancels keep the token:
    # those runs resume the same provider thread once the gate clears.
    _SESSION_INVALIDATING_RESULT_STATUSES = frozenset({TaskStatus.FAILED})
    _STREAM_SESSION_UPDATE_MIN_SECONDS = 2.0
    _STREAM_PROGRESS_MIN_SECONDS = 2.0
    _STREAM_TRANSCRIPT_HEAD_LINES = 40
    _STREAM_TRANSCRIPT_SUMMARY_EVERY = 25
    _STREAM_TRANSCRIPT_LINE_LIMIT = 2000

    _PATH_HINT_RE = re.compile(
        r"([A-Za-z]:\\[^\s\"']+|(?:\.\.?[\\/])?[A-Za-z0-9._-]+(?:[\\/][A-Za-z0-9._-]+)+)",
    )
    _SIGNIFICANT_STREAM_RE = re.compile(
        r"\b(error|failed|failure|warning|denied|approval|permission|completed|created|modified|updated|test|verified|verdict)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        store: OPCStore,
        approval_engine: ApprovalEngine,
        task_preparer: Callable[[Task], Coroutine[Any, Any, Task]] | None = None,
        communication: CommunicationManager | None = None,
        org_engine: Any | None = None,
    ) -> None:
        self.store = store
        self.approval_engine = approval_engine
        self.task_preparer = task_preparer
        self.communication = communication
        self.org_engine = org_engine

    @staticmethod
    def _normalize_external_agent_choice(value: Any) -> str:
        return re.sub(r"[\s\-]+", "_", str(value or "").strip()).strip("_").lower()

    async def _best_resume_external_session(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        role_session_id: str,
    ) -> ExternalSession | None:
        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return None
        project_id = str(task.project_id or "default").strip() or "default"
        kwargs: dict[str, Any] = {
            "project_id": project_id,
            "limit": 100,
        }
        if role_session_id:
            kwargs["opc_session_id"] = role_session_id
        else:
            kwargs["task_id"] = task.id
        try:
            sessions = await list_sessions(**kwargs)
        except TypeError:
            return None
        except Exception:
            logger.opt(exception=True).debug(
                "External resume restore: candidate listing failed"
            )
            return None
        selected, _token = select_best_external_resume_session(
            sessions,
            agent_type=adapter.agent_type,
            project_id=project_id,
        )
        return selected

    async def _stored_provider_token_allows_resume(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        role_session_id: str,
        token: str,
        strict: bool = False,
    ) -> bool | None:
        """Return the latest durable resumability verdict for one provider token.

        ``True`` means the newest row for the token finalized resumable,
        ``False`` means the token is dead and must be cleared, ``None`` means
        there is no durable verdict either way (caller keeps the token).
        ``strict`` treats an unfinalized newest row as ``False`` — required
        for provider_stream tokens whose run may have crashed mid-stream.
        """

        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return None
        project_id = str(task.project_id or "default").strip() or "default"
        kwargs: dict[str, Any] = {"project_id": project_id, "limit": 100}
        if role_session_id:
            kwargs["opc_session_id"] = role_session_id
        else:
            kwargs["task_id"] = task.id
        try:
            sessions = await list_sessions(**kwargs)
        except Exception:
            logger.opt(exception=True).debug(
                "External resume restore: provider-token status lookup failed"
            )
            return None
        matching = [
            session
            for session in sessions
            if str(getattr(session, "agent_type", "") or "").strip()
            == adapter.agent_type
            and external_session_matches_provider_token(session, token)
        ]
        if not matching:
            return None
        selected, selected_token = select_best_external_resume_session(
            matching,
            agent_type=adapter.agent_type,
            project_id=project_id,
        )
        if (
            selected is None
            or selected_token != token
            or not external_session_allows_resume(selected)
        ):
            # The newest row for this token is terminally non-resumable.
            return False
        status = str(getattr(selected, "status", "") or "").strip().lower()
        if status in {"done", "suspended"}:
            return True
        # The newest row is alive but not finalized (running / awaiting_human /
        # awaiting_peer). An approval or peer park is not evidence the thread
        # is dead, so a canonical token keeps its pin; a provider_stream token
        # must not resume an attempt that never finalized.
        return False if strict else None

    @classmethod
    def _task_explicitly_selected_external_agent(cls, task: Task, agent_type: str) -> bool:
        selected_agent = cls._normalize_external_agent_choice(agent_type)
        if not selected_agent:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        if cls._normalize_external_agent_choice(metadata.get("router_preferred_agent")) == selected_agent:
            return True
        if (
            bool(metadata.get("execution_agent_locked"))
            and cls._normalize_external_agent_choice(metadata.get("selected_execution_agent")) == selected_agent
        ):
            return True
        source = str(metadata.get("selected_execution_agent_source", "") or "").strip().lower()
        user_selection_sources = {
            "explicit_user_agent",
            "explicit_user_agent_selection",
            "recruitment_user_override",
            "user_selected_agent",
        }
        return (
            cls._normalize_external_agent_choice(getattr(task, "assigned_external_agent", "")) == selected_agent
            and source in user_selection_sources
        )

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _resolve_timeout_settings(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
    ) -> tuple[int, int, int]:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        hard_timeout_seconds = (
            self._coerce_positive_int(task_metadata.get("external_hard_timeout_seconds"))
            or self._coerce_positive_int(adapter.config.interactive_timeout_seconds)
            or 900
        )
        idle_timeout_seconds = self._coerce_positive_int(task_metadata.get("external_idle_timeout_seconds"))
        if idle_timeout_seconds is None:
            idle_timeout_seconds = (
                self._coerce_positive_int(adapter.config.idle_timeout_seconds)
                or hard_timeout_seconds
            )
        idle_timeout_seconds = min(idle_timeout_seconds, hard_timeout_seconds)
        startup_timeout_seconds = self._coerce_positive_int(task_metadata.get("external_startup_timeout_seconds"))
        if startup_timeout_seconds is None:
            startup_timeout_seconds = self._coerce_positive_int(
                getattr(adapter.config, "startup_timeout_seconds", 0)
            )
        if startup_timeout_seconds is None:
            startup_timeout_seconds = min(
                idle_timeout_seconds,
                DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS,
            )
        startup_timeout_seconds = min(startup_timeout_seconds, idle_timeout_seconds)
        return hard_timeout_seconds, idle_timeout_seconds, startup_timeout_seconds

    @staticmethod
    def _memory_env(task: Task) -> dict[str, str]:
        project_id = str(task.project_id or "default").strip() or "default"
        opc_home = Path(get_opc_home())
        store = MarkdownMemoryStore(opc_home)
        global_path = store.ensure_memory_file(None, heading="# Global Memory")
        project_path = store.ensure_memory_file(project_id, heading=f"# Project Memory ({project_id})")
        return {
            "OPC_MEMORY_ROOT": str(store.global_memory_dir),
            "OPC_GLOBAL_MEMORY_PATH": str(global_path),
            "OPC_PROJECT_MEMORY_PATH": str(project_path),
        }

    @staticmethod
    async def _terminate_process(proc: asyncio.subprocess.Process) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pid": proc.pid,
            "returncode_before": proc.returncode,
            "method": "",
            "ok": False,
        }
        if proc.returncode is not None:
            result["method"] = "already_exited"
            result["returncode_after"] = proc.returncode
            result["ok"] = True
            return result
        if os.name == "nt":
            result["method"] = "taskkill_tree"
            try:
                killer = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
                stdout = killer.stdout or b""
                stderr = killer.stderr or b""
                result["taskkill_returncode"] = killer.returncode
                if stdout:
                    result["taskkill_stdout"] = stdout.decode("utf-8", errors="replace")[:1000]
                if stderr:
                    result["taskkill_stderr"] = stderr.decode("utf-8", errors="replace")[:1000]
                if killer.returncode != 0:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        proc.terminate()
            except (OSError, subprocess.SubprocessError) as exc:
                result["taskkill_error"] = str(exc)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    proc.terminate()
            waiter = asyncio.create_task(proc.wait())
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    proc.kill()
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            result["returncode_after"] = proc.returncode
            result["ok"] = proc.returncode is not None
            return result
        if os.name == "posix":
            result["method"] = "process_group"
            with contextlib.suppress(ProcessLookupError, PermissionError):
                pgid = os.getpgid(proc.pid)
                if pgid == proc.pid:
                    os.killpg(pgid, signal.SIGTERM)
        else:
            result["method"] = "process"
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.terminate()
        waiter = asyncio.create_task(proc.wait())
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
        except asyncio.TimeoutError:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    pgid = os.getpgid(proc.pid)
                    if pgid == proc.pid:
                        os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(waiter), timeout=5)
        except asyncio.CancelledError:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    pgid = os.getpgid(proc.pid)
                    if pgid == proc.pid:
                        os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.kill()
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        result["returncode_after"] = proc.returncode
        result["ok"] = proc.returncode is not None
        return result

    async def run(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        prepared_task: Task | None = None,
    ) -> TaskResult:
        await self._restore_session_resume_from_store(adapter, task, on_progress=on_progress)
        agent_task = prepared_task or await self._prepare_task_for_agent(task)
        await self._clear_broker_pending_inbox(task)
        mode = adapter.config.run_mode
        if mode == "interactive" and adapter.supports_interactive():
            cmd, metadata = adapter.build_interactive_invocation(agent_task, workspace_path=workspace_path)
        else:
            cmd, metadata = adapter.build_invocation(agent_task, workspace_path=workspace_path)

        metadata = {
            **metadata,
            "workspace": workspace_path,
            "explicit_user_selected_agent": self._task_explicitly_selected_external_agent(
                task,
                adapter.agent_type,
            ),
            "external_session_continuation": bool(
                str(getattr(adapter.config, "session_mode", "") or "").strip().lower() == "resume"
            ),
        }
        try:
            checks = assert_external_agent_write_contract(
                workspace_path=workspace_path,
                opc_home=get_opc_home(),
                task=task,
                project_db_path=str(getattr(self.store, "db_path", "") or "") or None,
            )
            metadata["workspace_permission_contract"] = [check.as_dict() for check in checks]
        except ExternalAgentPreflightError as exc:
            metadata["workspace_permission_contract"] = [check.as_dict() for check in exc.checks]
            return TaskResult(
                status=TaskStatus.FAILED,
                content=str(exc),
                artifacts=metadata,
            )
        allowed, decision = await self.approval_engine.authorize_external_action(
            task=task,
            agent_name=adapter.agent_type,
            metadata=metadata,
            on_progress=on_progress,
        )
        metadata["approval"] = {
            "action": decision.action.value,
            "risk_level": decision.risk_level.value,
            "confidence": decision.confidence,
            "policy_source": decision.policy_source,
            "rationale": decision.rationale,
        }
        if not allowed:
            blocked_status = TaskStatus.AWAITING_HUMAN if decision.action == ApprovalAction.REQUIRE_INPUT else TaskStatus.FAILED
            result = TaskResult(
                status=blocked_status,
                content=f"External action blocked by autonomy policy: {decision.rationale}",
                artifacts={**metadata, "requires_user_input": decision.action == ApprovalAction.REQUIRE_INPUT},
            )
            await self._persist_session(adapter, task, workspace_path, result.artifacts or {}, result)
            return result

        if mode == "interactive" and adapter.supports_interactive():
            result = await self._run_interactive(
                adapter,
                task,
                agent_task,
                workspace_path,
                cmd,
                metadata,
                on_progress,
            )
        elif mode == "interactive":
            metadata["interactive_fallback"] = True
            result = await self._run_monitored_process(
                adapter=adapter,
                task=task,
                launch_task=agent_task,
                workspace_path=workspace_path,
                cmd=cmd,
                metadata=metadata,
                on_progress=on_progress,
                allow_prompt_handling=True,
            )
        else:
            result = await self._run_monitored_process(
                adapter=adapter,
                task=task,
                launch_task=agent_task,
                workspace_path=workspace_path,
                cmd=cmd,
                metadata=metadata,
                on_progress=on_progress,
                allow_prompt_handling=True,
            )

        artifacts = {**metadata, **(result.artifacts or {})}
        result.artifacts = artifacts
        await self._persist_session(adapter, task, workspace_path, artifacts, result)
        return result

    async def _restore_session_resume_from_store(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        *,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Before the first ``start_process`` of this invocation, look up any
        previously persisted ``external_sessions`` row for this task/agent and
        seed ``adapter.config.session_mode="resume"`` plus ``session_id`` so the
        adapter's ``_build_session_args`` injects the right CLI resume flag
        (``--resume <id>`` / ``--continue`` for claude-code/opencode, Codex
        thread resume, etc.).

        This is the cross-invocation resume path for CLI sessions.
        """
        supports_resume = bool(
            adapter.supports_session_resume()
            if hasattr(adapter, "supports_session_resume")
            else str(getattr(getattr(adapter, "config", None), "resume_session_flag", "") or "").strip()
        )
        if not supports_resume:
            return
        config = getattr(adapter, "config", None)
        if config is None:
            return
        if str(getattr(config, "session_mode", "") or "").strip().lower() == "new":
            return
        configured_session_token = (
            str(getattr(config, "session_id", "") or "").strip()
            if str(getattr(config, "session_mode", "") or "").strip().lower()
            == "resume"
            else ""
        )

        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "get_external_session"):
            return
        project_id = str(task.project_id or "default") or "default"
        # Role-instance model (Phase A): prefer looking up prior session by
        # the *role_session_id*, which is stable across turns for the same
        # role (e.g. CMO's delegate turn and her review-of-designer turn
        # share the same codex session). Fall back to task.id for
        # non-company-mode tasks that have no role_session_id.
        role_session_id = str(
            (task.metadata or {}).get("delegation_role_session_id", "") or ""
        ).strip()

        # Fix 5 PR6: canonical source is ``role_runtime_session.adapter_session_state[agent_type]``.
        # Check it first; the ExternalSession table is a compatibility
        # fallback for legacy rows written before PR6 landed.
        session_token = configured_session_token
        if role_session_id and hasattr(store, "get_role_session_adapter_state"):
            try:
                entry = await store.get_role_session_adapter_state(
                    role_session_id, adapter.agent_type
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"PR6 role adapter-state read failed "
                    f"sid={role_session_id} agent={adapter.agent_type}",
                )
                entry = None
            if isinstance(entry, dict):
                role_session_token = str(
                    entry.get("resume_session_id")
                    or entry.get("provider_session_id")
                    or ""
                ).strip()
                if not is_provider_session_token(
                    role_session_token,
                    agent_type=adapter.agent_type,
                    project_id=project_id,
                ):
                    role_session_token = ""
                session_token = session_token or role_session_token
                token_allows_resume = (
                    await self._stored_provider_token_allows_resume(
                        adapter=adapter,
                        task=task,
                        role_session_id=role_session_id,
                        token=session_token,
                        strict=str(entry.get("source", "") or "").strip()
                        == "provider_stream",
                    )
                    if session_token
                    else None
                )
                if session_token and token_allows_resume is False:
                    session_token = ""
                    clear_role_state = getattr(
                        store,
                        "update_role_session_adapter_state",
                        None,
                    )
                    if callable(clear_role_state):
                        try:
                            await clear_role_state(
                                role_session_id,
                                adapter.agent_type,
                                None,
                            )
                        except Exception:
                            logger.opt(exception=True).debug(
                                "External resume restore: stale provider token clear failed"
                            )
                    if hasattr(config, "session_mode"):
                        config.session_mode = "new"
                    if hasattr(config, "session_id"):
                        config.session_id = ""
                    task.metadata = dict(task.metadata or {})
                    task.metadata.pop("external_resume_session_id", None)
                    task.metadata.pop("external_resume_session_scope_id", None)
                    task.metadata.pop("external_resume_agent_type", None)
                    task.metadata["external_resume_fallback"] = (
                        "provider_terminal_failure"
                    )
                    if on_progress:
                        await on_progress(
                            f"[External resume] {adapter.agent_type} skipped prior failed session"
                        )
                    # Do not immediately rediscover the same unfinalized or
                    # failed stream row through the compatibility fallback.
                    return

        prior = None
        if not session_token:
            prior = await self._best_resume_external_session(
                adapter=adapter,
                task=task,
                role_session_id=role_session_id,
            )
            if role_session_id:
                if prior is None:
                    try:
                        prior = await store.get_external_session(
                            adapter.agent_type,
                            project_id,
                            opc_session_id=role_session_id,
                        )
                    except Exception:
                        logger.opt(exception=True).debug(
                            f"External resume restore: get_external_session by role failed for {adapter.agent_type}/{role_session_id}",
                        )
                        prior = None
            if prior is None:
                try:
                    prior = await store.get_external_session(
                        adapter.agent_type,
                        project_id,
                        task_id=task.id,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        f"External resume restore: get_external_session failed for {adapter.agent_type}/{task.id}",
                    )
                    return
            if prior is None:
                return
            if not external_session_allows_resume(prior):
                if on_progress:
                    await on_progress(
                        f"[External resume] {adapter.agent_type} skipped prior "
                        f"{str(getattr(prior, 'status', '') or 'unknown')} session"
                    )
                return

            session_token = provider_token_from_external_session(
                prior,
                agent_type=adapter.agent_type,
                project_id=project_id,
            )
        can_resume_without_session_id = bool(
            adapter.can_resume_without_session_id()
            if hasattr(adapter, "can_resume_without_session_id")
            else False
        )
        if not session_token and not can_resume_without_session_id:
            return

        if hasattr(config, "session_mode"):
            config.session_mode = "resume"
        if hasattr(config, "session_id"):
            config.session_id = session_token
        if session_token:
            task.metadata = dict(task.metadata)
            task.metadata["external_resume_session_id"] = session_token
            task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
            task.metadata["external_resume_agent_type"] = adapter.agent_type

        if on_progress:
            label = session_token or "(continue, no id)"
            await on_progress(
                f"[External resume] {adapter.agent_type} restored prior session → {label}"
            )

    async def _persist_discovered_provider_session(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        runtime_session_id: str,
        metadata: dict[str, Any],
        provider_session_id: str,
        status: str,
        extra: dict[str, Any],
    ) -> bool:
        """Persist a provider thread as soon as it appears on the stream.

        Waiting for process exit loses the token when Stop cancels the broker.
        The external-session row is written first, then the canonical per-role
        adapter state, so a concurrent suspend checkpoint can capture either
        durable source.
        """

        project_id = str(task.project_id or "default").strip() or "default"
        token = str(provider_session_id or "").strip()
        if not is_provider_session_token(
            token,
            agent_type=adapter.agent_type,
            project_id=project_id,
        ):
            return False
        metadata["resume_session_id"] = token
        metadata["provider_session_id"] = token
        task.metadata = dict(task.metadata or {})
        task.metadata["external_resume_session_id"] = token
        task.metadata["external_resume_agent_type"] = adapter.agent_type
        task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
        discovered_at = datetime.now().isoformat()
        await self._save_runtime_session(
            adapter=adapter,
            task=task,
            workspace_path=workspace_path,
            session_id=runtime_session_id,
            status=status,
            metadata=metadata,
            extra={
                **dict(extra or {}),
                "resume_session_id": token,
                "provider_session_id": token,
                "provider_session_discovered_at": discovered_at,
            },
        )
        role_session_id = str(
            task.metadata.get("delegation_role_session_id", "") or ""
        ).strip()
        update_role_state = getattr(
            self.store,
            "update_role_session_adapter_state",
            None,
        )
        if role_session_id and callable(update_role_state):
            try:
                await update_role_state(
                    role_session_id,
                    adapter.agent_type,
                    {
                        "resume_session_id": token,
                        "provider_session_id": token,
                        "agent_type": adapter.agent_type,
                        "updated_at": discovered_at,
                        "last_task_id": str(task.id or ""),
                        "last_project_id": project_id,
                        "workspace_path": workspace_path,
                        "source": "provider_stream",
                        "status": "working",
                    },
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "Provider stream token role-state write failed"
                )
        return True

    async def _run_interactive(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        launch_task: Task,
        workspace_path: str,
        cmd: list[str],
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> TaskResult:
        logger.info(f"External broker interactive run: {adapter.agent_type} -> {task.title}")
        return await self._run_monitored_process(
            adapter=adapter,
            task=task,
            launch_task=launch_task,
            workspace_path=workspace_path,
            cmd=cmd,
            metadata=metadata,
            on_progress=on_progress,
            allow_prompt_handling=True,
        )

    async def _run_monitored_process(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        launch_task: Task,
        workspace_path: str,
        cmd: list[str],
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        allow_prompt_handling: bool = False,
    ) -> TaskResult:
        # Company-mode external agents get the opc-collab CLI surface.
        # Task mode runs do not load the communication surface at all.
        comms_env: dict[str, str] = self._memory_env(task)
        collaboration_enabled = company_collaboration_enabled_for_task(task)
        workspace_root = ""
        output_root = ""
        comms_root = ""
        collab_rpc_server = None
        if collaboration_enabled:
            comms_role = (
                str(task.assigned_to or "").strip()
                or str(task.metadata.get("work_item_role_id", "") or "").strip()
            )
            runtime_state = {
                "manager_board_summary": dict(task.context_snapshot.get("manager_board_summary", {}) or {}),
            }
            role_cfg = _collaboration_role_cfg(getattr(self, "org_engine", None), comms_role)
            collab_profile, allowed_collab_tools = resolve_task_collaboration_tools(
                task,
                role=comms_role,
                seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                runtime_state=runtime_state,
                role_cfg=role_cfg,
            )
            if comms_role:
                comms_env["OPC_COMMS_FROM"] = comms_role
                comms_env["OPC_COMMS_PROJECT"] = str(task.project_id or "default")
                comms_env["OPC_COMMS_SESSION"] = str(
                    task.parent_session_id or task.session_id or "default"
                )
            comms_env["OPC_COLLAB_PROFILE"] = collab_profile
            comms_env["OPC_ALLOWED_COLLAB_TOOLS"] = json.dumps(sorted(allowed_collab_tools))
            comms_env["OPC_MAILBOX_MODE"] = "runtime_owned"
            workspace_root = str(task.metadata.get("workspace_root", "") or task.metadata.get("comms_workspace_root", "") or "").strip()
            output_root = str(task.metadata.get("output_root", "") or task.metadata.get("target_output_dir", "") or "").strip()
            comms_root = str(task.metadata.get("comms_root", "") or "").strip()
        if workspace_root:
            comms_env["OPC_WORKSPACE_ROOT"] = workspace_root
        if output_root:
            comms_env["OPC_OUTPUT_ROOT"] = output_root
        # OPC_COMMS_ROOT / OPC_PROJECT_DB_PATH / OPC_TASK_ID are independent
        # of whether the project has locked an output_root yet. Previously
        # these three were nested under `if output_root:`, which meant that
        # intake/dispatch turns (where the output_root is not yet chosen)
        # spawned collaboration runs without a db path or task id; every
        # collaboration tool then failed with "requires an active assigned
        # task" because the dispatch runtime could not look up the task.
        if comms_root:
            comms_env["OPC_COMMS_ROOT"] = comms_root
        if getattr(self.store, "db_path", ""):
            comms_env["OPC_PROJECT_DB_PATH"] = str(getattr(self.store, "db_path"))
        if str(task.id or "").strip():
            comms_env["OPC_TASK_ID"] = str(task.id).strip()
        if collaboration_enabled and str(task.id or "").strip():
            comms_env["OPC_RUNTIME_TASK_ID"] = str(task.id).strip()
        current_work_item_id = linked_work_item_id_for_task(task) if collaboration_enabled else ""
        if current_work_item_id:
            # This is the canonical collaboration identity. Runtime Task IDs
            # stay available for diagnostics/session continuity, but
            # collaboration tools such as manager_board_read consume WorkItem
            # IDs only.
            comms_env["OPC_WORK_ITEM_ID"] = current_work_item_id
        # Install the ``opc-collab`` skill + CLI shim into the agent's
        # isolated home, then wire the agent to it. Adapters that opt
        # into the skill path (return an ``agent_isolation_home_slug``)
        # get the CLI on PATH and the SKILL.md under their native
        # ``skills/`` directory; the user's personal agent config
        # (``~/.codex``, ``~/.claude``, etc.) stays untouched.
        if collaboration_enabled:
            slug = adapter.agent_isolation_home_slug()
            if not slug:
                raise RuntimeError(
                    f"External adapter `{adapter.agent_type}` does not provide an opc-collab CLI isolation home."
                )
            home, bin_dir = install_collab_surface(slug)
            adapter.post_install_agent_home(str(home))
            repo_root = str(Path(__file__).resolve().parents[2])
            existing_pythonpath = comms_env.get("PYTHONPATH") or os.environ.get("PYTHONPATH", "")
            pythonpath_parts = [part for part in existing_pythonpath.split(os.pathsep) if part]
            if repo_root not in pythonpath_parts:
                comms_env["PYTHONPATH"] = os.pathsep.join([repo_root, *pythonpath_parts])
            for env_key, env_value in adapter.agent_home_env_vars(str(home)).items():
                if env_key:
                    comms_env[str(env_key)] = str(env_value)
            existing_path = comms_env.get("PATH") or os.environ.get("PATH", "")
            comms_env["PATH"] = prepend_to_path(existing_path, bin_dir)
            comms_env["OPC_COLLAB_CLI"] = str(opc_collab_executable(bin_dir))
            async def _dispatch_collaboration_rpc(
                tool_name: str,
                args: dict[str, Any],
            ) -> tuple[dict[str, Any], bool]:
                from opc.layer4_tools.collaboration_dispatch import (
                    CollaborationRuntimeBinding,
                    dispatch_collaboration_tool_bound,
                )

                runtime_task_id = str(comms_env.get("OPC_TASK_ID", "") or comms_env.get("OPC_RUNTIME_TASK_ID", "") or "").strip()
                fresh_task = await self.store.get_task(runtime_task_id) if runtime_task_id else None
                active_task = fresh_task or task
                active_work_item_id = linked_work_item_id_for_task(active_task) or str(comms_env.get("OPC_WORK_ITEM_ID", "") or "").strip()
                if active_work_item_id and active_task is not None:
                    set_linked_work_item_id(active_task, active_work_item_id)
                role_id = str(comms_env.get("OPC_COMMS_FROM", "") or "").strip()
                context = (
                    CollaborationContext.from_task(active_task, role_id=role_id)
                    if active_task is not None
                    else CollaborationContext.from_environment(
                        role_id=role_id,
                        project_id=str(comms_env.get("OPC_COMMS_PROJECT", "") or task.project_id or "default"),
                        session_id=str(comms_env.get("OPC_COMMS_SESSION", "") or task.parent_session_id or task.session_id or "default"),
                        workspace_root=str(comms_env.get("OPC_WORKSPACE_ROOT", "") or workspace_path),
                        task_id=runtime_task_id,
                    )
                )
                if active_work_item_id and "linked_work_item_id" not in context.metadata:
                    context.metadata["linked_work_item_id"] = active_work_item_id
                manager = self.communication or CommunicationManager(
                    self.store,
                    EventBus(),
                    org_engine=getattr(self, "org_engine", None),
                )
                service = CollaborationService(manager)
                binding = CollaborationRuntimeBinding(
                    service=service,
                    context=context,
                    store=self.store,
                    manager=manager,
                    env=comms_env,
                    allowed_tools=set(allowed_collab_tools),
                    owns_store=False,
                )
                return await dispatch_collaboration_tool_bound(tool_name, args, binding)

            try:
                collab_rpc_server = await start_collaboration_rpc_server(_dispatch_collaboration_rpc)
            except Exception as exc:
                message = f"Company collaboration RPC setup failed: {exc}"
                logger.warning(message)
                metadata["collaboration_rpc"] = {
                    "enabled": False,
                    "error": str(exc),
                }
                if on_progress:
                    await on_progress(f"[External status] {message}")
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content=message,
                    artifacts=metadata,
                )
            if collab_rpc_server is not None:
                rpc_env = collab_rpc_server.client_env
                comms_env.update(rpc_env)
                rpc_transport = rpc_env.get(OPC_COLLAB_RPC_TRANSPORT, "fifo")
                metadata["collaboration_rpc"] = {
                    "transport": rpc_transport,
                    "enabled": True,
                }
                if rpc_transport == "tcp":
                    metadata["collaboration_rpc"]["host"] = rpc_env.get(OPC_COLLAB_RPC_HOST, "")
                    metadata["collaboration_rpc"]["port"] = rpc_env.get(OPC_COLLAB_RPC_PORT, "")
                else:
                    metadata["collaboration_rpc"]["request_path"] = rpc_env.get(OPC_COLLAB_RPC_PATH, "")
        try:
            proc = await adapter.start_process(
                cmd,
                workspace_path,
                extra_env=comms_env or None,
                task=launch_task,
                launch_metadata=metadata,
            )
        except Exception:
            if collab_rpc_server is not None:
                await collab_rpc_server.close()
            raise
        adapter._process = proc  # noqa: SLF001 - broker and adapter intentionally coordinate runtime state
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        approval_prompts: list[dict[str, Any]] = []
        prompt_handling_enabled = bool(
            allow_prompt_handling
            and adapter.supports_approval_prompt_handling(cmd, metadata)
        )
        metadata["approval_prompt_bridge"] = prompt_handling_enabled
        started_at = datetime.now()
        session_id = self._resolve_runtime_session_id(adapter, task, metadata)
        hard_timeout_seconds, idle_timeout_seconds, startup_timeout_seconds = self._resolve_timeout_settings(adapter, task)
        heartbeat_seconds = max(1, int(adapter.config.status_heartbeat_seconds))
        loop = asyncio.get_running_loop()
        state: dict[str, Any] = {
            "status": "starting",
            "last_activity_monotonic": loop.time(),
            "last_activity_at": started_at,
            "activity_count": 0,
            "last_output": "",
            "last_session_update_monotonic": 0.0,
            "last_progress_monotonic": 0.0,
            "timed_out": False,
            "timeout_kind": "",
            "timeout_reason": "",
            "fatal_reason": "",
            "process_cleanup": {},
            "provider_session_id": "",
        }
        provider_session_lock = asyncio.Lock()
        stream_line_counts: dict[str, int] = {}
        trace_path = self._external_trace_path(adapter, task, started_at)

        await self._save_runtime_session(
            adapter=adapter,
            task=task,
            workspace_path=workspace_path,
            session_id=session_id,
            status="running",
            metadata=metadata,
            extra={
                "pid": proc.pid,
                "started_at": started_at.isoformat(),
                "last_activity_at": started_at.isoformat(),
                "activity_count": 0,
                "startup_timeout_seconds": startup_timeout_seconds,
                "idle_timeout_seconds": idle_timeout_seconds,
                "hard_timeout_seconds": hard_timeout_seconds,
                "status_heartbeat_seconds": heartbeat_seconds,
            },
        )
        if on_progress:
            await on_progress(
                f"[External status] {adapter.agent_type} started pid={proc.pid} "
                f"(startup timeout {startup_timeout_seconds}s, idle timeout {idle_timeout_seconds}s, hard timeout {hard_timeout_seconds}s)"
            )

        async def _consume(stream: asyncio.StreamReader | None, sink: list[str], stream_name: str) -> None:
            if stream is None:
                return
            async for line in self._iter_stream_lines(stream):
                text = line.decode("utf-8", errors="replace")
                sink.append(text)
                self._write_external_trace_line(
                    trace_path,
                    adapter=adapter,
                    task=task,
                    stream_name=stream_name,
                    text=text,
                )
                if text.strip():
                    try:
                        discovered_provider_session_id = str(
                            adapter.extract_resume_session_id(text) or ""
                        ).strip()
                    except Exception:
                        discovered_provider_session_id = ""
                    if discovered_provider_session_id:
                        async with provider_session_lock:
                            if not state["provider_session_id"]:
                                persisted = await self._persist_discovered_provider_session(
                                    adapter=adapter,
                                    task=task,
                                    workspace_path=workspace_path,
                                    runtime_session_id=session_id,
                                    metadata=metadata,
                                    provider_session_id=discovered_provider_session_id,
                                    status="working",
                                    extra={
                                        "pid": proc.pid,
                                        "started_at": started_at.isoformat(),
                                        "last_activity_at": datetime.now().isoformat(),
                                        "activity_count": state["activity_count"] + 1,
                                        "last_output": text.strip(),
                                        "stream": stream_name,
                                    },
                                )
                                if persisted:
                                    state["provider_session_id"] = (
                                        discovered_provider_session_id
                                    )
                try:
                    fatal_reason = adapter.detect_runtime_failure(text, stream_name, metadata)
                except TypeError:
                    fatal_reason = adapter.detect_runtime_failure(text, stream_name)
                if fatal_reason:
                    state["status"] = "failed"
                    state["fatal_reason"] = fatal_reason
                    state["last_activity_at"] = datetime.now()
                    state["last_output"] = text.strip()
                    await self._save_runtime_session(
                        adapter=adapter,
                        task=task,
                        workspace_path=workspace_path,
                        session_id=session_id,
                        status="failed",
                        metadata=metadata,
                        extra={
                            "pid": proc.pid,
                            "started_at": started_at.isoformat(),
                            "last_activity_at": state["last_activity_at"].isoformat(),
                            "activity_count": state["activity_count"],
                            "last_output": state["last_output"],
                            "stream": stream_name,
                            "failure_reason": fatal_reason,
                        },
                    )
                    if on_progress:
                        await on_progress(f"[External status] {fatal_reason}")
                    state["process_cleanup"] = await self._terminate_process(proc)
                    return
                if text.strip():
                    state["status"] = "working"
                    state["last_activity_monotonic"] = loop.time()
                    state["last_activity_at"] = datetime.now()
                    state["activity_count"] += 1
                    state["last_output"] = text.strip()
                    now_monotonic = loop.time()
                    if (
                        state["activity_count"] <= 3
                        or now_monotonic - float(state["last_session_update_monotonic"]) >= self._STREAM_SESSION_UPDATE_MIN_SECONDS
                    ):
                        state["last_session_update_monotonic"] = now_monotonic
                        await self._save_runtime_session(
                            adapter=adapter,
                            task=task,
                            workspace_path=workspace_path,
                            session_id=session_id,
                            status="working",
                            metadata=metadata,
                            extra={
                                "pid": proc.pid,
                                "started_at": started_at.isoformat(),
                                "last_activity_at": state["last_activity_at"].isoformat(),
                                "activity_count": state["activity_count"],
                                "last_output": state["last_output"],
                                "stream": stream_name,
                            },
                        )
                    stream_line_counts[stream_name] = stream_line_counts.get(stream_name, 0) + 1
                    transcript_entry = self._stream_transcript_entry(
                        text,
                        stream_name=stream_name,
                        line_count=stream_line_counts[stream_name],
                    )
                    if transcript_entry:
                        transcript_content, transcript_meta = transcript_entry
                        await self._save_runtime_transcript_entry(
                            adapter=adapter,
                            task=task,
                            metadata=metadata,
                            role="assistant",
                            entry_type="stream",
                            content=transcript_content,
                            extra=transcript_meta,
                        )
                if on_progress and text.strip():
                    progress_update = adapter.format_progress_update(text, stream_name)
                    now_monotonic = loop.time()
                    if (
                        progress_update
                        and self._should_emit_stream_progress(
                            progress_update,
                            now_monotonic=now_monotonic,
                            last_progress_monotonic=float(state["last_progress_monotonic"]),
                        )
                    ):
                        state["last_progress_monotonic"] = now_monotonic
                        await on_progress(progress_update)
                if prompt_handling_enabled:
                    prompt = await self._maybe_handle_prompt(
                        adapter=adapter,
                        task=task,
                        workspace_path=workspace_path,
                        text=text,
                        stream_name=stream_name,
                        proc=proc,
                        on_progress=on_progress,
                    )
                    if prompt:
                        approval_prompts.append(prompt)
                        if prompt.get("response") and not prompt.get("response_sent"):
                            failure_reason = str(
                                prompt.get("failure_reason")
                                or "approval_response_not_delivered"
                            )
                            state["status"] = "failed"
                            state["fatal_reason"] = failure_reason
                            state["last_activity_at"] = datetime.now()
                            state["last_output"] = text.strip()
                            await self._save_runtime_session(
                                adapter=adapter,
                                task=task,
                                workspace_path=workspace_path,
                                session_id=session_id,
                                status="failed",
                                metadata=metadata,
                                extra={
                                    "pid": proc.pid,
                                    "started_at": started_at.isoformat(),
                                    "last_activity_at": state["last_activity_at"].isoformat(),
                                    "activity_count": state["activity_count"],
                                    "last_output": state["last_output"],
                                    "stream": stream_name,
                                    "failure_reason": failure_reason,
                                    "approval_prompt": prompt,
                                },
                            )
                            if on_progress:
                                await on_progress(f"[External approval] {failure_reason}")
                            state["process_cleanup"] = await self._terminate_process(proc)
                            return

        async def _heartbeat() -> None:
            while proc.returncode is None:
                await asyncio.sleep(heartbeat_seconds)
                if proc.returncode is not None:
                    return
                if state["timed_out"] or state["fatal_reason"]:
                    return
                idle_for = int(loop.time() - state["last_activity_monotonic"])
                runtime_status = "working" if state["activity_count"] else "running"
                await self._save_runtime_session(
                    adapter=adapter,
                    task=task,
                    workspace_path=workspace_path,
                    session_id=session_id,
                    status=runtime_status,
                    metadata=metadata,
                    extra={
                        "pid": proc.pid,
                        "started_at": started_at.isoformat(),
                        "last_activity_at": state["last_activity_at"].isoformat(),
                        "activity_count": state["activity_count"],
                        "last_output": state["last_output"],
                        "idle_for_seconds": idle_for,
                    },
                )
                if on_progress:
                    await on_progress(
                        f"[External status] {adapter.agent_type} {runtime_status}; "
                        f"last activity {idle_for}s ago"
                    )

        async def _watch_idle() -> None:
            while proc.returncode is None:
                await asyncio.sleep(1)
                idle_for = loop.time() - state["last_activity_monotonic"]
                startup_phase = state["activity_count"] == 0
                timeout_limit = startup_timeout_seconds if startup_phase else idle_timeout_seconds
                if idle_for <= timeout_limit:
                    continue
                state["timed_out"] = True
                timeout_kind = "startup" if startup_phase else "idle"
                state["timeout_kind"] = timeout_kind
                if startup_phase:
                    state["timeout_reason"] = (
                        f"{adapter.agent_type} startup timed out after {timeout_limit}s "
                        f"with no observable output/activity"
                    )
                else:
                    state["timeout_reason"] = (
                        f"{adapter.agent_type} idle timed out after {timeout_limit}s "
                        f"with no further observable output/activity"
                    )
                await self._save_runtime_session(
                    adapter=adapter,
                    task=task,
                    workspace_path=workspace_path,
                    session_id=session_id,
                    status=f"{timeout_kind}_timeout",
                    metadata=metadata,
                    extra={
                        "pid": proc.pid,
                        "started_at": started_at.isoformat(),
                        "last_activity_at": state["last_activity_at"].isoformat(),
                        "activity_count": state["activity_count"],
                        "last_output": state["last_output"],
                        "idle_for_seconds": int(idle_for),
                        "timeout_kind": timeout_kind,
                        "timeout_limit_seconds": int(timeout_limit),
                        "failure_reason": state["timeout_reason"],
                    },
                )
                if on_progress:
                    await on_progress(f"[External status] {state['timeout_reason']}")
                state["process_cleanup"] = await self._terminate_process(proc)
                return

        async def _poll_inbox() -> None:
            if not (
                adapter.supports_live_inbox_delivery()
                or adapter.supports_resume_inbox_delivery()
            ):
                return
            seen_ids = {
                str(item.get("msg_id", "")).strip()
                for item in list(task.context_snapshot.get("broker_pending_inbox", []) or [])
                if isinstance(item, dict) and str(item.get("msg_id", "")).strip()
            }
            while proc.returncode is None:
                await asyncio.sleep(1)
                if proc.returncode is not None:
                    return
                fresh = await self._queue_external_inbox_updates(
                    adapter=adapter,
                    task=task,
                    workspace_path=workspace_path,
                    session_id=session_id,
                    metadata=metadata,
                    seen_ids=seen_ids,
                )
                if fresh and on_progress:
                    await on_progress(
                        f"[External inbox] queued {len(fresh)} new message(s) for `{adapter.agent_type}`; "
                        "they will be injected on the next safe resume boundary."
                    )

        stdout_task = asyncio.create_task(_consume(proc.stdout, stdout_chunks, "stdout"))
        stderr_task = asyncio.create_task(_consume(proc.stderr, stderr_chunks, "stderr"))
        heartbeat_task = asyncio.create_task(_heartbeat())
        idle_task = asyncio.create_task(_watch_idle())
        inbox_task = asyncio.create_task(_poll_inbox())
        cancellation_status_persisted = False

        try:
            try:
                return_code = await asyncio.wait_for(proc.wait(), timeout=hard_timeout_seconds)
            except asyncio.TimeoutError:
                state["timed_out"] = True
                state["timeout_kind"] = "hard"
                state["timeout_reason"] = f"{adapter.agent_type} execution timed out after {hard_timeout_seconds}s"
                await self._save_runtime_session(
                    adapter=adapter,
                    task=task,
                    workspace_path=workspace_path,
                    session_id=session_id,
                    status="hard_timeout",
                    metadata=metadata,
                    extra={
                        "pid": proc.pid,
                        "started_at": started_at.isoformat(),
                        "last_activity_at": state["last_activity_at"].isoformat(),
                        "activity_count": state["activity_count"],
                        "last_output": state["last_output"],
                        "timeout_kind": "hard",
                        "timeout_limit_seconds": hard_timeout_seconds,
                        "failure_reason": state["timeout_reason"],
                    },
                )
                if on_progress:
                    await on_progress(f"[External status] {state['timeout_reason']}")
                state["process_cleanup"] = await self._terminate_process(proc)
                return_code = proc.returncode if proc.returncode is not None else -9
        except asyncio.CancelledError:
            try:
                logger.warning(
                    "External broker cancelled while {agent} subprocess still running: "
                    "task_id={task_id} title={title!r} pid={pid} activity_count={activity} "
                    "status={status} last_output={last_output!r}",
                    agent=adapter.agent_type,
                    task_id=task.id,
                    title=task.title,
                    pid=proc.pid,
                    activity=state["activity_count"],
                    status=state["status"],
                    last_output=str(state.get("last_output") or "")[:300],
                )
                if os.environ.get("OPC_EXTERNAL_CANCEL_STACK", "").strip():
                    import traceback as _tb
                    logger.debug(
                        "External broker cancellation stack:\n{stack}",
                        stack="".join(_tb.format_stack()),
                    )
            except Exception as _diag_exc:  # noqa: BLE001
                logger.warning(f"External broker cancel-diagnostic logging failed: {_diag_exc}")
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            company_suspend = (
                proc.returncode is None
                and (
                    is_work_item_runtime_metadata(task_metadata)
                    or bool(task_metadata.get("company_runtime_suspended_at"))
                )
            )
            if proc.returncode is None:
                runtime_status = "suspended" if company_suspend else "cancelled"
                failure_reason = f"{adapter.agent_type} monitor {runtime_status} while process was still running"
                state["process_cleanup"] = await self._terminate_process(proc)
            elif proc.returncode == 0:
                runtime_status = "done"
                failure_reason = f"{adapter.agent_type} monitor cancelled after process exited cleanly"
            else:
                runtime_status = "failed"
                failure_reason = f"{adapter.agent_type} monitor cancelled after process exited with code {proc.returncode}"
            reason = str(task_metadata.get("last_stop_reason") or "runtime_cancelled").strip()
            await self._save_runtime_session(
                adapter=adapter,
                task=task,
                workspace_path=workspace_path,
                session_id=session_id,
                status=runtime_status,
                metadata=metadata,
                extra={
                    "pid": proc.pid,
                    "started_at": started_at.isoformat(),
                    "last_activity_at": state["last_activity_at"].isoformat(),
                    "activity_count": state["activity_count"],
                    "last_output": state["last_output"],
                    "suspend_reason": reason if company_suspend else "",
                    "failure_reason": failure_reason,
                    "return_code": proc.returncode,
                },
            )
            cancellation_status_persisted = True
            raise
        finally:
            async def _cancel_and_await(task_obj: asyncio.Task[Any]) -> None:
                task_obj.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task_obj

            async def _drain_reader_task(task_obj: asyncio.Task[Any]) -> None:
                if task_obj.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await task_obj
                    return
                try:
                    await asyncio.wait_for(asyncio.shield(task_obj), timeout=2)
                except asyncio.TimeoutError:
                    await _cancel_and_await(task_obj)
                except asyncio.CancelledError:
                    if task_obj.cancelled():
                        return
                    raise

            async def _finish_process_cleanup() -> None:
                if proc.returncode is None:
                    state["process_cleanup"] = await self._terminate_process(proc)
                for task_obj in (heartbeat_task, idle_task, inbox_task):
                    await _cancel_and_await(task_obj)
                for task_obj in (stdout_task, stderr_task):
                    await _drain_reader_task(task_obj)
                try:
                    await adapter.cleanup_process(proc)
                finally:
                    if collab_rpc_server is not None:
                        await collab_rpc_server.close()
                    adapter._process = None  # noqa: SLF001

            cleanup_task = asyncio.create_task(_finish_process_cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # Cancellation can arrive after proc.wait() completed but
                # while stdout/adapter cleanup is still draining.  Finish the
                # cleanup in its own task and make the provider row terminal
                # before propagating cancellation; otherwise a stale
                # checkpoint can retain a false `working` capability.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await cleanup_task
                if not cancellation_status_persisted:
                    cleanup_terminal_status = (
                        "done" if proc.returncode == 0 else "failed"
                    )
                    terminal_save = asyncio.create_task(self._save_runtime_session(
                        adapter=adapter,
                        task=task,
                        workspace_path=workspace_path,
                        session_id=session_id,
                        status=cleanup_terminal_status,
                        metadata=metadata,
                        extra={
                            "pid": proc.pid,
                            "started_at": started_at.isoformat(),
                            "last_activity_at": state["last_activity_at"].isoformat(),
                            "activity_count": state["activity_count"],
                            "last_output": state["last_output"],
                            "return_code": proc.returncode,
                            "failure_reason": (
                                ""
                                if cleanup_terminal_status == "done"
                                else f"{adapter.agent_type} exited with code {proc.returncode}"
                            ),
                        },
                    ))
                    try:
                        await asyncio.shield(terminal_save)
                    except asyncio.CancelledError:
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await terminal_save
                raise
        output = "".join(stdout_chunks)
        errors = "".join(stderr_chunks)
        normalized_output = adapter.normalize_result_output(output)
        raw_log_path = self._write_external_raw_log(
            task=task,
            workspace_path=workspace_path,
            agent_type=adapter.agent_type,
            stdout=output,
            stderr=errors,
        )
        resume_session_id = str(
            adapter.extract_resume_session_id(output)
            or metadata.get("resume_session_id")
            or (
                adapter.config.session_id
                if str(metadata.get("session_mode", "") or "").strip().lower() == "resume"
                else ""
            )
            or ""
        ).strip()
        artifacts = {
            **metadata,
            "approval_prompts": approval_prompts,
            "stderr": errors,
            "pid": proc.pid,
            "started_at": started_at.isoformat(),
            "last_activity_at": state["last_activity_at"].isoformat(),
            "activity_count": state["activity_count"],
            "last_output": state["last_output"],
            "timeout_kind": state["timeout_kind"],
            "timeout_reason": state["timeout_reason"],
            "startup_timeout_seconds": startup_timeout_seconds,
            "idle_timeout_seconds": idle_timeout_seconds,
            "hard_timeout_seconds": hard_timeout_seconds,
            "session_id": session_id,
            "process_cleanup": state.get("process_cleanup") or {},
        }
        if raw_log_path:
            artifacts["raw_output_log_path"] = raw_log_path
        if resume_session_id:
            artifacts["resume_session_id"] = resume_session_id
            artifacts["provider_session_id"] = resume_session_id
        artifacts = self._enrich_structured_result_artifacts(
            adapter=adapter,
            task=task,
            metadata=metadata,
            normalized_output=normalized_output,
            raw_output=output,
            base_artifacts=artifacts,
        )
        terminal_status = (
            "done"
            if not state["timed_out"]
            and not state["fatal_reason"]
            and return_code == 0
            else "failed"
        )
        terminal_save = asyncio.create_task(self._save_runtime_session(
            adapter=adapter,
            task=task,
            workspace_path=workspace_path,
            session_id=session_id,
            status=terminal_status,
            metadata=metadata,
            extra={
                **artifacts,
                "return_code": return_code,
                "failure_reason": (
                    ""
                    if terminal_status == "done"
                    else str(
                        state["timeout_reason"]
                        or state["fatal_reason"]
                        or f"{adapter.agent_type} exited with code {return_code}"
                    )
                ),
            },
        ))
        try:
            await asyncio.shield(terminal_save)
        except asyncio.CancelledError:
            # Once the subprocess has exited, its terminal status must win
            # over the early provider-stream `working` capability even if the
            # parent coroutine is cancelled in this narrow handoff window.
            with contextlib.suppress(Exception):
                await terminal_save
            raise
        if state["timed_out"]:
            return TaskResult(
                status=TaskStatus.FAILED,
                content=state["timeout_reason"],
                artifacts=artifacts,
            )
        if state["fatal_reason"]:
            return TaskResult(
                status=TaskStatus.FAILED,
                content=state["fatal_reason"],
                artifacts=artifacts,
            )
        if return_code == 0:
            return TaskResult(
                status=TaskStatus.DONE,
                content=normalized_output,
                artifacts=artifacts,
            )
        return TaskResult(
            status=TaskStatus.FAILED,
            content=f"{adapter.agent_type} exited with code {return_code}\n{errors}\n{output}",
            artifacts=artifacts,
        )

    async def _iter_stream_lines(self, stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        buffer = bytearray()
        while True:
            chunk = await stream.read(self._STREAM_READ_SIZE)
            if not chunk:
                if buffer:
                    yield bytes(buffer)
                return
            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                line = bytes(buffer[: newline_index + 1])
                del buffer[: newline_index + 1]
                yield line

    @staticmethod
    def _external_trace_enabled() -> bool:
        value = str(os.environ.get("OPC_EXTERNAL_AGENT_TRACE") or "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @classmethod
    def _should_emit_stream_progress(
        cls,
        progress_update: str,
        *,
        now_monotonic: float,
        last_progress_monotonic: float,
    ) -> bool:
        if not progress_update:
            return False
        # Tool cards are discrete UI events. Do not let the generic stream
        # throttle swallow fast tool calls that arrive immediately after init.
        if ":tool]" in progress_update:
            return True
        return (
            now_monotonic - last_progress_monotonic >= cls._STREAM_PROGRESS_MIN_SECONDS
            or bool(cls._SIGNIFICANT_STREAM_RE.search(progress_update))
        )

    @classmethod
    def _external_trace_path(
        cls,
        adapter: ExternalAgentAdapter,
        task: Task,
        started_at: datetime,
    ) -> Path | None:
        if not cls._external_trace_enabled():
            return None
        root = get_opc_home() / "logs" / "external_agents" / adapter.agent_type
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Unable to create external-agent trace directory {}: {}", root, exc)
            return None
        safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(task.id or task.title or "task")).strip("-")
        safe_task = safe_task[:80] or "task"
        stamp = started_at.strftime("%Y%m%dT%H%M%S")
        return root / f"{stamp}-{safe_task}.jsonl"

    @staticmethod
    def _write_external_trace_line(
        trace_path: Path | None,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        stream_name: str,
        text: str,
    ) -> None:
        if trace_path is None:
            return
        payload = {
            "timestamp": datetime.now().isoformat(),
            "agent": adapter.agent_type,
            "task_id": task.id,
            "stream": stream_name,
            "text": text.rstrip("\n"),
        }
        try:
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.debug("Unable to write external-agent trace {}: {}", trace_path, exc)

    @classmethod
    def _stream_transcript_entry(
        cls,
        text: str,
        *,
        stream_name: str,
        line_count: int,
    ) -> tuple[str, dict[str, Any]] | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        significant = bool(cls._SIGNIFICANT_STREAM_RE.search(normalized))
        if (
            line_count <= cls._STREAM_TRANSCRIPT_HEAD_LINES
            or significant
        ):
            return (
                normalized[: cls._STREAM_TRANSCRIPT_LINE_LIMIT],
                {
                    "stream": stream_name,
                    "line_count": line_count,
                    "transcript_compacted": len(normalized) > cls._STREAM_TRANSCRIPT_LINE_LIMIT,
                    "transcript_significant": significant,
                },
            )
        if line_count % cls._STREAM_TRANSCRIPT_SUMMARY_EVERY == 0:
            preview = normalized[:500]
            return (
                f"[{stream_name}] {line_count} stream lines received; latest: {preview}",
                {
                    "stream": stream_name,
                    "line_count": line_count,
                    "transcript_compacted": True,
                    "transcript_summary": True,
                },
            )
        return None

    @staticmethod
    def _safe_log_token(value: str) -> str:
        token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
        return token.strip("-")[:80] or "external"

    def _write_external_raw_log(
        self,
        *,
        task: Task,
        workspace_path: str,
        agent_type: str,
        stdout: str,
        stderr: str,
    ) -> str:
        if not stdout and not stderr:
            return ""
        try:
            log_dir = Path(workspace_path).expanduser().resolve() / ".opc" / "external_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            filename = (
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
                f"{self._safe_log_token(agent_type)}-"
                f"{self._safe_log_token(task.id)}.log"
            )
            path = log_dir / filename
            path.write_text(
                "\n".join(
                    part
                    for part in (
                        "## STDOUT",
                        stdout,
                        "## STDERR",
                        stderr,
                    )
                    if part is not None
                ),
                encoding="utf-8",
            )
            return str(path)
        except Exception:
            logger.opt(exception=True).debug("ExternalAgentBroker: failed to write raw external log")
            return ""

    def _enrich_structured_result_artifacts(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
        normalized_output: str,
        raw_output: str = "",
        base_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        structured = adapter.extract_structured_result_fields(normalized_output)
        enriched = dict(base_artifacts)

        runtime_plan = structured.get("work_item_runtime_plan") or structured.get("runtime_plan")
        if not isinstance(runtime_plan, dict) or not runtime_plan:
            runtime_plan = dict(task.metadata.get("work_item_runtime_plan", {}) or {})
        if not runtime_plan:
            runtime_plan = self._fallback_work_item_runtime_plan(task)
        if runtime_plan:
            enriched["work_item_runtime_plan"] = runtime_plan
            enriched.setdefault("runtime_plan", runtime_plan)

        artifact_index = self._normalize_artifact_index(
            structured.get("work_item_artifact_index") or structured.get("artifact_index")
        )
        if not artifact_index:
            artifact_index = self._fallback_artifact_index(task, metadata, normalized_output)
        if artifact_index:
            enriched["work_item_artifact_index"] = artifact_index
            enriched.setdefault("artifact_index", artifact_index)

        verification_evidence = self._normalize_verification_evidence(
            structured.get("verification_evidence")
            or self._infer_verification_evidence_from_command_events(raw_output)
            or self._infer_verification_evidence(normalized_output)
        )
        if verification_evidence.get("status") == "provided":
            enriched["verification_evidence"] = verification_evidence

        collaboration_failure = self._infer_collaboration_infrastructure_failure(raw_output)
        if collaboration_failure:
            enriched["collaboration_infrastructure_failure"] = collaboration_failure

        # Review verdicts must come from a structured JSON verdict only.
        # Plain-prose keyword inference is intentionally avoided here so an
        # unparseable reviewer turn flows to the runtime's parse-retry /
        # human-escalation path instead of being applied mechanically.
        review_verdict = adapter.infer_review_verdict(normalized_output)
        if review_verdict:
            enriched["structured_review_verdict"] = review_verdict
            enriched.setdefault("review_verdict", review_verdict)

        return enriched

    @staticmethod
    def _command_text_from_event_item(item: dict[str, Any]) -> str:
        command = item.get("command")
        if isinstance(command, list):
            return " ".join(str(part) for part in command if str(part).strip())
        return str(command or "").strip()

    @staticmethod
    def _collaboration_infrastructure_marker(value: Any) -> bool:
        text = str(value or "").strip().lower()
        if not text:
            return False
        markers = (
            '"error_type": "infrastructure"',
            '"error_type":"infrastructure"',
            "'error_type': 'infrastructure'",
            "disk i/o error",
            "database is locked",
            "readonly database",
            "unable to open database file",
            "collaboration broker rpc",
            "broker rpc failed",
            "sqlite3.operationalerror",
        )
        return any(marker in text for marker in markers)

    def _infer_collaboration_infrastructure_failure(self, output: str) -> dict[str, Any]:
        raw = str(output or "").strip()
        if not raw:
            return {}
        for line in raw.splitlines():
            try:
                envelope = json.loads(line)
            except Exception:
                continue
            if not isinstance(envelope, dict):
                continue
            event = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else envelope
            if not isinstance(event, dict):
                continue
            if str(event.get("type", "") or "").strip() != "item.completed":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            if not isinstance(item, dict) or str(item.get("type", "") or "").strip() != "command_execution":
                continue
            command_text = self._command_text_from_event_item(item)
            if "opc-collab" not in command_text:
                continue
            output_text = str(item.get("aggregated_output", "") or "").strip()
            if not self._collaboration_infrastructure_marker(output_text):
                continue
            tool_name = ""
            match = re.search(r"(?:^|\s)opc-collab(?:\s+--[^\s]+(?:\s+\S+)*)?\s+([A-Za-z_][A-Za-z0-9_-]*)", command_text)
            if match:
                tool_name = match.group(1)
            return {
                "error_type": "infrastructure",
                "retryable": True,
                "tool_name": tool_name,
                "command": command_text,
                "observed_output": output_text[:4000],
            }
        return {}

    @staticmethod
    def _normalize_verification_evidence(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        evidence = VerificationEvidence(
            status=str(value.get("status", "") or "missing").strip() or "missing",
            verdict=str(value.get("verdict", "") or "").strip().lower(),
            summary=str(value.get("summary", "") or "").strip(),
            checks=[
                dict(item)
                for item in list(value.get("checks", []) or [])
                if isinstance(item, dict)
            ][:24],
            raw_output=str(value.get("raw_output", "") or "").strip(),
        )
        if evidence.status != "provided" and evidence.checks and evidence.verdict:
            evidence.status = "provided"
        if evidence.status != "provided":
            return {}
        return evidence.__dict__

    @staticmethod
    def _normalize_verification_line(line: str) -> str:
        return re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", str(line or "")).strip()

    def _infer_verification_evidence(self, output: str) -> dict[str, Any]:
        raw = str(output or "").strip()
        if not raw:
            return {}
        checks: list[dict[str, Any]] = []
        current: dict[str, str] = {}
        verdict = ""
        summary_lines: list[str] = []
        for raw_line in raw.splitlines():
            line = self._normalize_verification_line(raw_line)
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith("verdict:"):
                verdict_value = line.split(":", 1)[1].strip().lower()
                if verdict_value.startswith("pass"):
                    verdict = "pass"
                elif verdict_value.startswith("fail"):
                    verdict = "fail"
                elif verdict_value.startswith("partial"):
                    verdict = "partial"
                continue
            if lowered.startswith("check:"):
                if current:
                    checks.append(dict(current))
                    current = {}
                current["check"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("command:"):
                current["command"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("observed output:"):
                current["observed_output"] = line.split(":", 1)[1].strip()
                continue
            if lowered.startswith("result:"):
                current["result"] = line.split(":", 1)[1].strip().upper()
                continue
            if line.startswith("VERIFIED:") or line.startswith("ISSUES:"):
                summary_lines.append(line)
                continue
            if current and "observed_output" in current and "result" not in current:
                current["observed_output"] = f"{current.get('observed_output', '')}\n{line}".strip()
            else:
                summary_lines.append(line)
        if current:
            checks.append(dict(current))
        if not checks or not verdict:
            return {}
        return VerificationEvidence(
            status="provided",
            verdict=verdict,
            summary="\n".join(summary_lines).strip(),
            checks=checks[:24],
            raw_output=raw,
        ).__dict__

    def _infer_verification_evidence_from_command_events(self, output: str) -> dict[str, Any]:
        raw = str(output or "").strip()
        if not raw:
            return {}
        checks: list[dict[str, Any]] = []
        failure_seen = False
        for line in raw.splitlines():
            try:
                envelope = json.loads(line)
            except Exception:
                continue
            if not isinstance(envelope, dict):
                continue
            event = envelope.get("msg") if isinstance(envelope.get("msg"), dict) else envelope
            if not isinstance(event, dict):
                continue
            if str(event.get("type", "") or "").strip() != "item.completed":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else None
            if not isinstance(item, dict) or str(item.get("type", "") or "").strip() != "command_execution":
                continue
            command = item.get("command")
            if isinstance(command, list):
                command_text = " ".join(str(part) for part in command if str(part).strip())
            else:
                command_text = str(command or "").strip()
            output_text = str(item.get("aggregated_output", "") or "").strip()
            exit_code = item.get("exit_code")
            status = str(item.get("status", "") or "").strip().lower()
            passed = exit_code in (None, 0) and status not in {"failed", "error", "cancelled"}
            if not passed:
                failure_seen = True
            observed_lines: list[str] = []
            if output_text:
                observed_lines.append(output_text)
            if status:
                observed_lines.append(f"status={status}")
            if exit_code is not None:
                observed_lines.append(f"exit_code={exit_code}")
            checks.append(
                {
                    "check": "external command execution",
                    "command": command_text or "(unknown command)",
                    "observed_output": "\n".join(observed_lines).strip(),
                    "result": "PASS" if passed else "FAIL",
                }
            )
        if not checks:
            return {}
        verdict = "fail" if failure_seen else "pass"
        summary = "Derived verification evidence from external command execution events."
        return VerificationEvidence(
            status="provided",
            verdict=verdict,
            summary=summary,
            checks=checks[:24],
            raw_output=raw,
        ).__dict__

    def _fallback_work_item_runtime_plan(self, task: Task) -> dict[str, Any]:
        work_item_assignment = dict(task.metadata.get("work_item_assignment", {}) or {})
        acceptance = list(task.metadata.get("acceptance_criteria", []) or [])
        projection_id = projection_id_for_task(task)
        turn_type = turn_type_for_task(task, fallback="")
        return {
            "projection_id": projection_id,
            "turn_type": turn_type,
            "summary": (
                str(work_item_assignment.get("your_responsibility", "") or "").strip()
                or str(task.description or task.title or "").strip()
            ),
            "deliverables": [
                str(item).strip()
                for item in work_item_assignment.get("deliverables", [])
                if str(item).strip()
            ][:6],
            "acceptance_criteria": [
                str(item).strip()
                for item in (work_item_assignment.get("acceptance_criteria", acceptance) or [])
                if str(item).strip()
            ][:6],
        }

    def _fallback_artifact_index(
        self,
        task: Task,
        metadata: dict[str, Any],
        normalized_output: str,
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for key in ("target_output_dir", "workspace"):
            value = str(metadata.get(key, "") or task.metadata.get(key, "") or "").strip()
            if value:
                items.append({"kind": "workspace", "label": key, "value": value})
        for candidate in self._iter_path_hint_tokens(normalized_output):
            items.append({"kind": "artifact_ref", "label": "artifact", "value": candidate})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            fingerprint = (item["kind"], item["label"], item["value"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]

    def _iter_path_hint_tokens(self, text: str) -> list[str]:
        candidates: list[str] = []
        for raw_token in re.split(r"\s+", str(text or "")):
            candidate = raw_token.strip().strip("`'\"()[]{}<>").rstrip(".,:;)")
            if not candidate or "://" in candidate:
                continue
            if len(candidate) > self._MAX_PATH_HINT_TOKEN_LENGTH:
                continue
            if not self._PATH_HINT_RE.fullmatch(candidate):
                continue
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _normalize_artifact_index(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                rendered = {
                    "kind": str(item.get("kind", "") or "artifact").strip() or "artifact",
                    "label": str(item.get("label", "") or item.get("name", "") or "artifact").strip() or "artifact",
                    "value": str(item.get("value", "") or item.get("location", "") or item.get("path", "") or "").strip(),
                }
                if rendered["value"]:
                    normalized.append(rendered)
            elif isinstance(item, str) and item.strip():
                normalized.append({"kind": "artifact", "label": "artifact", "value": item.strip()})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in normalized:
            fingerprint = (item["kind"], item["label"], item["value"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]

    async def _maybe_handle_prompt(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        text: str,
        stream_name: str,
        proc: asyncio.subprocess.Process,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> dict[str, Any] | None:
        request = adapter.parse_approval_request(text, stream_name)
        if not request:
            return None

        if on_progress:
            target = request.action_name
            await on_progress(
                f"[External approval] {adapter.agent_type} requested {request.approval_scope}:{target}"
            )

        if request.approval_scope == "tool":
            arguments = dict(request.arguments or {})
            if (
                request.action_name == "shell_exec"
                and not str(arguments.get("working_directory", "")).strip()
                and workspace_path
            ):
                arguments["working_directory"] = workspace_path
                request.arguments = arguments
            metadata = {
                **request.metadata,
                "source_agent": adapter.agent_type,
                "prompt_text": request.prompt_text,
                "run_mode": "interactive",
                "workspace": workspace_path,
            }
            allowed, decision = await self.approval_engine.authorize_tool_call(
                task=task,
                tool_name=request.action_name,
                arguments=arguments,
                metadata=metadata,
                on_progress=on_progress,
            )
        else:
            metadata = {
                **request.metadata,
                "arguments": dict(request.arguments or {}),
                "agent": adapter.agent_type,
                "prompt_text": request.prompt_text,
                "run_mode": "interactive",
                "workspace": workspace_path,
            }
            allowed, decision = await self.approval_engine.authorize_external_action(
                task=task,
                agent_name=request.action_name,
                metadata=metadata,
                on_progress=on_progress,
            )

        approved = allowed and decision.action == ApprovalAction.AUTO_APPROVE
        response = adapter.format_approval_response(request, approved, decision)
        response_sent = False
        if response:
            response_sent = await adapter.send_process_input(proc, response)
        failure_reason = ""
        if response and not response_sent:
            failure_reason = (
                "approval_response_not_delivered: OpenOPC received and decided "
                f"{adapter.agent_type}'s approval request for {request.approval_scope}:"
                f"{request.action_name}, but could not write the response back to the child process."
            )
        return {
            "approval_scope": request.approval_scope,
            "action_name": request.action_name,
            "arguments": request.arguments,
            "prompt_text": request.prompt_text or text.strip(),
            "response": response.strip() if response else "",
            "response_sent": response_sent,
            "failure_reason": failure_reason,
            "approved": approved,
            "decision_action": decision.action.value,
            "risk_level": decision.risk_level.value,
            "policy_source": decision.policy_source,
            "human_reply": (decision.metadata or {}).get("human_reply") if decision.metadata else None,
        }

    async def _prepare_task_for_agent(self, task: Task) -> Task:
        if self.task_preparer:
            return await self.task_preparer(task)
        return task

    async def _clear_broker_pending_inbox(self, task: Task) -> None:
        queued = list(task.context_snapshot.get("broker_pending_inbox", []) or [])
        if not queued:
            return
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot.pop("broker_pending_inbox", None)
        task.context_snapshot.pop("latest_broker_inbox", None)
        await self.store.save_task(task)

    @staticmethod
    def _task_comms_layout(task: Task) -> Any | None:
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
        )
        if not workspace_root:
            return None
        try:
            from opc.layer2_organization import comms as _comms

            return _comms.resolve_layout(
                workspace_root,
                str(task.project_id or "default").strip() or "default",
                str(task.parent_session_id or task.session_id or "default").strip() or "default",
            )
        except Exception:
            return None

    @classmethod
    def _collect_external_unread_messages(
        cls,
        task: Task,
        *,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        role_id = str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        if not role_id:
            return []
        layout = cls._task_comms_layout(task)
        if layout is None:
            return []
        try:
            from opc.layer2_organization import comms as _comms

            headers = _comms.list_unread(layout, role_id, limit=limit)
        except Exception:
            return []
        queued: list[dict[str, Any]] = []
        for header in headers:
            _, body = _comms.read_message(header.path)
            queued.append(classify_worker_message(
                {
                    "msg_id": str(header.message_id or "").strip(),
                    "message_id": str(header.message_id or "").strip(),
                    "from_agent": str(header.from_role or "").strip(),
                    "to_agent": str(header.to_role or "").strip(),
                    "from": str(header.from_role or "").strip(),
                    "subject": str(header.subject or "").strip(),
                    "body": str(body or "").strip(),
                    "reply_needed": bool(header.blocking),
                    "urgency": str(header.priority or "").strip() or "normal",
                    "transport_kind": str(header.raw_frontmatter.get("transport_kind", "") or "").strip(),
                    "semantic_type": str(
                        header.raw_frontmatter.get("semantic_type")
                        or header.raw_frontmatter.get("kind")
                        or ""
                    ).strip(),
                    "metadata": dict(header.raw_frontmatter or {}),
                    "worker_id": str(
                        task.metadata.get("runtime_v2", {}).get("runtime_session_id")
                        or task.context_snapshot.get("runtime_resume", {}).get("runtime_session_id")
                        or task.session_id
                        or task.id
                        or ""
                    ).strip(),
                    "origin_task_id": str(task.id or "").strip(),
                    "origin_session_id": str(task.session_id or "").strip(),
                }
            ))
        return queued

    async def _queue_external_inbox_updates(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        session_id: str,
        metadata: dict[str, Any],
        seen_ids: set[str],
    ) -> list[dict[str, Any]]:
        context = CollaborationContext.from_task(task)
        fresh = await CollaborationService().prepare_inbox_for_resume(
            context,
            seen_ids=seen_ids,
            limit=6,
        )
        if not fresh:
            return []
        if hasattr(self.store, "save_task"):
            await self.store.save_task(task)
        await self._save_runtime_session(
            adapter=adapter,
            task=task,
            workspace_path=workspace_path,
            session_id=session_id,
            status="working",
            metadata=metadata,
            extra={
                "queued_inbox_count": len(task.context_snapshot["broker_pending_inbox"]),
                "latest_inbox_message_id": str(fresh[-1].get("msg_id", "")).strip(),
            },
        )
        return fresh

    def _resolve_runtime_session_id(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
    ) -> str:
        configured = str(metadata.get("session_id") or adapter.config.session_id or "").strip()
        if configured:
            return configured
        return f"{adapter.agent_type}:{task.project_id}:{task.id}"

    def _resolve_observability_runtime_session_id(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
    ) -> str:
        role_session_id = str(
            (task.metadata or {}).get("delegation_role_session_id")
            or (task.metadata or {}).get("assigned_role_runtime_id")
            or ""
        ).strip()
        if role_session_id:
            return role_session_id
        return self._resolve_runtime_session_id(adapter, task, metadata)

    async def _save_runtime_transcript_entry(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
        role: str,
        entry_type: str,
        content: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not content or not hasattr(self.store, "save_runtime_transcript_entry"):
            return
        runtime_session_id = self._resolve_observability_runtime_session_id(adapter, task, metadata)
        try:
            await self.store.save_runtime_transcript_entry(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                role=role,
                entry_type=entry_type,
                content=str(content),
                metadata={
                    "agent_type": adapter.agent_type,
                    "external_broker": True,
                    **dict(extra or {}),
                },
            )
        except Exception:
            logger.opt(exception=True).debug(
                "ExternalAgentBroker: failed to persist runtime transcript entry"
            )

    async def _save_runtime_tool_exchange(
        self,
        *,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        runtime_session_id = self._resolve_observability_runtime_session_id(adapter, task, metadata)
        tool_call_id = str(result.get("tool_call_id") or result.get("call_id") or f"external::{tool_name}").strip()
        try:
            if hasattr(self.store, "save_runtime_tool_call"):
                await self.store.save_runtime_tool_call(
                    runtime_session_id=runtime_session_id,
                    task_id=task.id,
                    session_id=task.session_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    metadata={"agent_type": adapter.agent_type, "external_broker": True},
                )
            if hasattr(self.store, "save_runtime_tool_result"):
                await self.store.save_runtime_tool_result(
                    runtime_session_id=runtime_session_id,
                    task_id=task.id,
                    session_id=task.session_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    payload=dict(result or {}),
                    metadata={"agent_type": adapter.agent_type, "external_broker": True},
                )
        except Exception:
            logger.opt(exception=True).debug(
                "ExternalAgentBroker: failed to persist runtime tool exchange"
            )

    def _resolve_persisted_session_id(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        metadata: dict[str, Any],
        *,
        artifacts: dict[str, Any] | None = None,
    ) -> str:
        artifact_data = dict(artifacts or {})
        configured = str(
            artifact_data.get("provider_session_id")
            or artifact_data.get("resume_session_id")
            or metadata.get("provider_session_id")
            or metadata.get("resume_session_id")
            or metadata.get("session_id")
            or adapter.config.session_id
            or ""
        ).strip()
        if configured:
            return configured
        return self._resolve_runtime_session_id(adapter, task, metadata)

    async def _save_runtime_session(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        session_id: str,
        status: str,
        metadata: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        if not bool(getattr(self.store, "is_ready", True)):
            logger.debug(
                "Skipping external runtime-session save because store is closed: task_id=%s status=%s",
                task.id,
                status,
            )
            return
        persisted_session_id = self._resolve_persisted_session_id(
            adapter,
            task,
            metadata,
            artifacts=extra,
        )
        # Role-instance model: opc_session_id is the stable role_session_id
        # when available, so later turns for the same role can look the
        # session up regardless of which task.id they run under.
        role_session_id = str(
            (task.metadata or {}).get("delegation_role_session_id", "") or ""
        ).strip()
        session = ExternalSession(
            agent_type=adapter.agent_type,
            project_id=task.project_id,
            session_id=persisted_session_id,
            opc_session_id=role_session_id or task.session_id,
            task_id=task.id,
            workspace_path=workspace_path,
            run_mode=adapter.config.run_mode,
            status=status,
            metadata={
                "command": metadata.get("command", ""),
                "model": metadata.get("model", "(cli default)"),
                "session_mode": metadata.get("session_mode", "auto"),
                "agent_type": adapter.agent_type,
                "runtime_session_id": session_id,
                "delegation_role_session_id": role_session_id,
                "resume_session_id": str(
                    extra.get("resume_session_id")
                    or metadata.get("resume_session_id")
                    or ""
                ).strip(),
                "provider_session_id": str(
                    extra.get("provider_session_id")
                    or metadata.get("provider_session_id")
                    or ""
                ).strip(),
                **extra,
            },
            updated_at=datetime.now(),
        )
        try:
            await self.store.save_external_session(session)
        except AssertionError:
            logger.debug(
                "Skipping external runtime-session save after store closed: task_id=%s status=%s",
                task.id,
                status,
            )

    async def _persist_session(
        self,
        adapter: ExternalAgentAdapter,
        task: Task,
        workspace_path: str,
        metadata: dict[str, Any],
        result: TaskResult,
    ) -> None:
        session_id = self._resolve_persisted_session_id(
            adapter,
            task,
            metadata,
            artifacts=result.artifacts or {},
        )

        # Role-instance model: opc_session_id keyed by role_session_id.
        role_session_id = str(
            (task.metadata or {}).get("delegation_role_session_id", "") or ""
        ).strip()
        resume_session_id = str(
            (result.artifacts or {}).get("resume_session_id")
            or metadata.get("resume_session_id")
            or ""
        ).strip()
        provider_session_id = str(
            (result.artifacts or {}).get("provider_session_id") or ""
        ).strip()
        session = ExternalSession(
            agent_type=adapter.agent_type,
            project_id=task.project_id,
            session_id=session_id,
            opc_session_id=role_session_id or task.session_id,
            task_id=task.id,
            workspace_path=workspace_path,
            run_mode=adapter.config.run_mode,
            status=result.status.value,
            metadata={
                "command": metadata.get("command", ""),
                "model": metadata.get("model", "(cli default)"),
                "session_mode": metadata.get("session_mode", "auto"),
                "agent_type": adapter.agent_type,
                "delegation_role_session_id": role_session_id,
                "resume_session_id": resume_session_id,
                "provider_session_id": provider_session_id,
                "runtime_session_id": self._resolve_runtime_session_id(adapter, task, metadata),
                "failure_reason": result.content if result.status != TaskStatus.DONE else "",
                "last_activity_at": str((result.artifacts or {}).get("last_activity_at", "")),
                "activity_count": int((result.artifacts or {}).get("activity_count", 0) or 0),
                "last_output": str((result.artifacts or {}).get("last_output", "") or ""),
                "timeout_kind": str((result.artifacts or {}).get("timeout_kind", "") or ""),
                "timeout_reason": str((result.artifacts or {}).get("timeout_reason", "") or ""),
                "startup_timeout_seconds": self._coerce_positive_int((result.artifacts or {}).get("startup_timeout_seconds")),
                "idle_timeout_seconds": self._coerce_positive_int((result.artifacts or {}).get("idle_timeout_seconds")),
                "hard_timeout_seconds": self._coerce_positive_int((result.artifacts or {}).get("hard_timeout_seconds")),
                "pid": (result.artifacts or {}).get("pid"),
            },
            updated_at=datetime.now(),
        )
        if not bool(getattr(self.store, "is_ready", True)):
            logger.debug(
                "Skipping external session persist because store is closed: task_id=%s status=%s",
                task.id,
                result.status.value,
            )
            return
        try:
            await self.store.save_external_session(session)
        except AssertionError:
            logger.debug(
                "Skipping external session persist after store closed: task_id=%s status=%s",
                task.id,
                result.status.value,
            )
            return

        # Fix 5 PR6: canonical per-role adapter token. Write alongside the
        # ExternalSession row so consecutive tasks for this role resume
        # the same external session (codex thread / claude-code session /
        # opencode session) regardless of task boundary. Applies only
        # when DONE with a usable token — failed runs shouldn't pin a
        # stale token that the next attempt would try to resume.
        if (
            role_session_id
            and result.status == TaskStatus.DONE
            and hasattr(self.store, "update_role_session_adapter_state")
        ):
            can_continue = bool(
                adapter.can_resume_without_session_id()
                if hasattr(adapter, "can_resume_without_session_id")
                else False
            )
            if resume_session_id or provider_session_id or can_continue:
                token_record = {
                    "resume_session_id": resume_session_id,
                    "provider_session_id": provider_session_id,
                    "agent_type": adapter.agent_type,
                    "updated_at": datetime.now().isoformat(),
                    "last_task_id": str(task.id or ""),
                    "last_project_id": str(task.project_id or ""),
                    "workspace_path": workspace_path,
                }
                try:
                    await self.store.update_role_session_adapter_state(
                        role_session_id,
                        adapter.agent_type,
                        token_record,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        f"PR6 role adapter-state write failed "
                        f"sid={role_session_id} agent={adapter.agent_type}",
                    )
        elif (
            role_session_id
            and result.status in self._SESSION_INVALIDATING_RESULT_STATUSES
            and hasattr(self.store, "get_role_session_adapter_state")
            and hasattr(self.store, "update_role_session_adapter_state")
        ):
            # A stream token is durable early so Stop can retain it, and a
            # park (awaiting_human / awaiting_peer) keeps it so the run can
            # resume the same thread once the gate clears. A terminal failure
            # must clear both a token discovered by this task and an older
            # role token that this failed attempt resumed.
            try:
                current = await self.store.get_role_session_adapter_state(
                    role_session_id,
                    adapter.agent_type,
                )
                current_token = ""
                if isinstance(current, dict):
                    current_token = str(
                        current.get("resume_session_id")
                        or current.get("provider_session_id")
                        or ""
                    ).strip()
                failed_token = str(
                    resume_session_id
                    or provider_session_id
                    or getattr(adapter.config, "session_id", "")
                    or ""
                ).strip()
                if (
                    isinstance(current, dict)
                    and (
                        str(current.get("last_task_id", "") or "").strip()
                        == str(task.id or "").strip()
                        or bool(failed_token and current_token == failed_token)
                    )
                ):
                    await self.store.update_role_session_adapter_state(
                        role_session_id,
                        adapter.agent_type,
                        None,
                    )
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to clear provider-stream role state after terminal failure"
                )
        if result.status in self._SESSION_INVALIDATING_RESULT_STATUSES:
            failed_token = str(
                resume_session_id
                or provider_session_id
                or getattr(adapter.config, "session_id", "")
                or ""
            ).strip()
            task_token = str(
                (task.metadata or {}).get("external_resume_session_id", "") or ""
            ).strip()
            if failed_token and task_token == failed_token:
                task.metadata = dict(task.metadata or {})
                task.metadata.pop("external_resume_session_id", None)
                task.metadata.pop("external_resume_session_scope_id", None)
                task.metadata.pop("external_resume_agent_type", None)
                task.metadata["external_resume_fallback"] = "provider_terminal_failure"
