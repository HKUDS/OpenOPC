"""Session lifecycle service shared by Office UI and CLI."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from opc.core.models import Task, TaskStatus
from opc.layer2_organization.company_runtime_identity import (
    ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES,
    COMPANY_RUNTIME_CHECKPOINT_TYPES,
    is_company_runtime_task,
    load_company_runtime_identity_index,
)
from opc.plugins.office_ui.execution_identity import (
    ExecutionIdentity,
    canonicalize_execution_identity,
    execution_identity_from_task,
    normalize_company_profile,
    normalize_exec_mode,
    normalize_org_id,
    normalize_preferred_agent,
)
from opc.plugins.office_ui.snapshot_builder import build_collab_sync

from .context import OfficeServiceContext
from .models import ServiceEvent, ServiceError, ServiceResult


class SessionService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    @staticmethod
    def normalize_exec_mode(value: Any) -> str:
        return normalize_exec_mode(value)

    def normalize_company_profile(self, value: Any) -> str:
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            default = "custom"
        else:
            default = self.context.mode_state.company_profile if self.context.mode_state.company_profile == "corporate" else "corporate"
        return normalize_company_profile(value, default=default)

    @staticmethod
    def normalize_preferred_agent(value: Any, default: str = "native") -> str:
        return normalize_preferred_agent(value, default=default)

    @staticmethod
    def normalize_org_id(value: Any) -> str:
        return normalize_org_id(value)

    def resolve_task_identity(
        self,
        task: Any | None,
        *,
        default_exec_mode: Any = None,
        default_company_profile: Any = None,
        default_preferred_agent: Any = None,
        default_org_id: Any = "",
    ) -> ExecutionIdentity:
        return execution_identity_from_task(
            task,
            default_exec_mode=default_exec_mode if default_exec_mode is not None else self.context.mode_state.exec_mode,
            default_company_profile=(
                default_company_profile
                if default_company_profile is not None
                else self.context.mode_state.company_profile
            ),
            default_preferred_agent=(
                default_preferred_agent
                if default_preferred_agent is not None
                else self.context.mode_state.task_preferred_agent
            ),
            default_org_id=default_org_id,
        )

    def resolve_task_session_config(self, task: Any | None) -> tuple[str, str]:
        identity = self.resolve_task_identity(task)
        return identity.exec_mode, identity.company_profile

    def resolve_task_org_id(self, task: Any | None) -> str:
        identity = self.resolve_task_identity(task)
        return identity.org_id if identity.is_custom_org else ""

    def resolve_task_preferred_agent(self, task: Any | None) -> str:
        return self.resolve_task_identity(task).preferred_agent

    def resolve_task_selected_execution_agent(self, task: Any | None) -> str:
        metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        selected = metadata.get("selected_execution_agent")
        if selected not in (None, "", [], {}):
            return self.normalize_preferred_agent(selected, default="native")
        mode = str(metadata.get("mode", "") or "").strip().lower()
        exec_mode = str(metadata.get("exec_mode", "") or "").strip().lower()
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        task_mode_contract = str(metadata.get("task_mode_contract", "") or "").strip()
        is_task_mode = (
            mode == "task"
            or exec_mode in {"task", "project", "single"}
            or execution_mode in {"task", "task_mode", "project"}
            or task_mode_contract == "single_full_capability_main_agent"
        )
        if is_task_mode:
            preferred = metadata.get("preferred_agent")
            if preferred not in (None, "", [], {}):
                return self.normalize_preferred_agent(preferred, default="native")
        assigned = getattr(task, "assigned_external_agent", None) if task is not None else None
        if assigned not in (None, "", [], {}):
            return self.normalize_preferred_agent(assigned, default="native")
        return "native"

    @staticmethod
    def _task_status_value(task: Any) -> str:
        status = getattr(task, "status", "")
        return status.value if hasattr(status, "value") else str(status or "")

    @staticmethod
    def _is_terminal_status(task: Any) -> bool:
        return SessionService._task_status_value(task) in {"done", "failed", "cancelled"}

    async def _coerce_hook_result(
        self,
        result: Any,
        *,
        default_payload: dict[str, Any],
        default_event_type: str = "session_updated",
    ) -> ServiceResult:
        if isinstance(result, ServiceResult):
            return result
        if isinstance(result, dict):
            payload = {**default_payload, **result}
        else:
            payload = dict(default_payload)
        return ServiceResult(payload, [ServiceEvent(default_event_type, payload)])

    async def _resolve_task_target(
        self,
        *,
        project_id: str,
        target: str = "",
        task_id: str = "",
        session_id: str = "",
    ) -> tuple[Any, str]:
        pid = self.context.normalize_project_id(project_id)
        raw_target = str(target or task_id or session_id or "").strip()
        if not raw_target:
            raise ServiceError("missing_target", "Missing task_id or session_id")
        engine = await self.context.engine_for_project(pid)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": pid})

        task = await store.get_task(raw_target) if hasattr(store, "get_task") else None
        if task is not None:
            task_project = self.context.normalize_project_id(getattr(task, "project_id", None))
            if task_project != pid:
                raise ServiceError("target_wrong_project", "Target belongs to a different project", {"project_id": task_project})

        # Company control is resolved from durable session/checkpoint scope
        # before any generic Task/session fallback.  This prevents a shared
        # final-decider row from winning merely because it was loaded first.
        identity_index = await load_company_runtime_identity_index(store, pid)
        if task is not None:
            identity = identity_index.resolve(task_id=raw_target)
        else:
            identity = (
                identity_index.resolve(runtime_session_id=raw_target)
                or identity_index.resolve(task_session_id=raw_target)
            )
        if identity is not None:
            # A concrete Task id is the caller's UI/chat channel, never the
            # company execution identity.  Keep that channel stable while the
            # shared identity supplies the durable runtime session/checkpoint.
            if task is not None:
                return task, identity.runtime_session_id
            resolved_task = (
                identity_index.task(identity.ui_anchor_task_id)
                or identity_index.task(identity.config_source_task_id)
            )
            if resolved_task is not None:
                return resolved_task, identity.runtime_session_id

        if task is not None:
            return task, str(getattr(task, "session_id", "") or getattr(task, "parent_session_id", "") or "")

        session = await store.get_session(raw_target) if hasattr(store, "get_session") else None
        if session is None:
            raise ServiceError("target_not_found", "Task or session not found", {"target": raw_target})
        session_project = self.context.normalize_project_id(getattr(session, "project_id", None))
        if session_project != pid:
            raise ServiceError("target_wrong_project", "Target belongs to a different project", {"project_id": session_project})

        tasks = list(identity_index.tasks)
        session_tasks = [
            candidate for candidate in tasks
            if str(getattr(candidate, "session_id", "") or "") == raw_target
        ]
        if not session_tasks:
            raise ServiceError("session_not_task_backed", "Session is not linked to a task-backed runtime", {"session_id": raw_target})
        task_mode_anchor = min(
            session_tasks,
            key=lambda item: (
                bool(str(getattr(item, "parent_session_id", "") or "")),
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "id", "") or ""),
            ),
        )
        return task_mode_anchor, raw_target

    async def _resolve_company_runtime_target(
        self,
        engine: Any,
        task: Any,
        *,
        runtime_session_id: str = "",
        checkpoint_id: str = "",
    ) -> dict[str, Any]:
        store = getattr(engine, "store", None)
        project_id = self.context.normalize_project_id(getattr(task, "project_id", None) or getattr(engine, "project_id", None))
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": project_id})
        index = await load_company_runtime_identity_index(store, project_id)
        identity = index.resolve(
            task_id=str(getattr(task, "id", "") or ""),
            runtime_session_id=runtime_session_id,
            checkpoint_id=checkpoint_id,
        )
        if identity is None:
            raise ServiceError(
                "company_runtime_identity_mismatch",
                "Company runtime identity does not match the requested task/session/checkpoint",
                {
                    "task_id": str(getattr(task, "id", "") or ""),
                    "runtime_session_id": str(runtime_session_id or ""),
                    "checkpoint_id": str(checkpoint_id or ""),
                },
            )
        config_task = index.task(identity.config_source_task_id) or task
        ui_anchor_task_id = identity.ui_anchor_task_id
        return {
            "identity": identity,
            "runtime_session_id": identity.runtime_session_id,
            "ui_channel_task_id": str(getattr(task, "id", "") or ""),
            "ui_anchor_task_id": ui_anchor_task_id,
            "config_source_task_id": identity.config_source_task_id,
            "config_task": config_task,
            "origin_task_id": ui_anchor_task_id,
            "affected_task_ids": list(identity.runtime_task_ids),
            "checkpoint": identity.checkpoint,
        }

    def _normalize_requested_config(
        self,
        *,
        exec_mode: Any,
        company_profile: Any = None,
        preferred_agent: Any = None,
        org_id: Any = None,
        current_company_profile: str = "corporate",
        current_preferred_agent: str = "native",
    ) -> tuple[str, str, str, str]:
        default_profile = current_company_profile if current_company_profile != "custom" else "corporate"
        identity = canonicalize_execution_identity(
            exec_mode=exec_mode,
            company_profile=company_profile if company_profile not in (None, "", [], {}) else default_profile,
            preferred_agent=preferred_agent,
            org_id=org_id,
            default_company_profile=default_profile,
            default_preferred_agent=current_preferred_agent,
            explicit_exec_mode=exec_mode is not None,
        )
        return identity.exec_mode, identity.company_profile, identity.preferred_agent, identity.org_id

    async def _ensure_org_loaded(self, org_id: str) -> None:
        if not org_id:
            return
        loader = self.context.load_active_org_config
        if callable(loader) and not loader(org_id):
            raise ServiceError("org_not_found", "org_not_found", {"org_id": org_id})
        setter = self.context.set_active_saved_org_name
        if callable(setter):
            await setter(org_id)

    async def create(
        self,
        *,
        project_id: str,
        title: str = "New Chat",
        exec_mode: Any = None,
        company_profile: Any = None,
        preferred_agent: Any = None,
        org_id: Any = None,
        task_id: str | None = None,
        description: str = "",
        interface: str = "office_ui",
        emit_board_event: bool = True,
        assignee_ids: list[str] | None = None,
        board_id: str | None = None,
    ) -> ServiceResult:
        pid = self.context.normalize_project_id(project_id)
        engine = await self.context.engine_for_project(pid)
        current_agent = self.context.mode_state.task_preferred_agent
        requested_exec = exec_mode if exec_mode is not None else self.context.mode_state.exec_mode
        requested_profile = company_profile if company_profile is not None else self.context.mode_state.company_profile
        normalized_exec, normalized_profile, normalized_agent, normalized_org = self._normalize_requested_config(
            exec_mode=requested_exec,
            company_profile=requested_profile,
            preferred_agent=preferred_agent if preferred_agent is not None else current_agent,
            org_id=org_id,
            current_company_profile=self.context.mode_state.company_profile,
            current_preferred_agent=current_agent,
        )
        if normalized_exec == "org":
            if not normalized_org and self.context.get_active_saved_org_name is not None:
                normalized_org = await self.context.get_active_saved_org_name()
            if not normalized_org:
                raise ServiceError("org_id_required", "org_id_required", {"project_id": pid})
            await self._ensure_org_loaded(normalized_org)
        else:
            normalized_org = ""

        tid = str(task_id or uuid.uuid4())
        session_id = str(uuid.uuid4())
        channel_id = f"session:{tid}"
        self.context.session_to_task[session_id] = tid
        normalized_title = str(title or "New Chat").strip() or "New Chat"
        metadata = self._session_metadata(
            task_id=tid,
            exec_mode=normalized_exec,
            company_profile=normalized_profile,
            preferred_agent=normalized_agent,
            org_id=normalized_org,
            interface=interface,
        )

        if getattr(engine, "memory", None):
            await engine.memory.ensure_session(
                session_id=session_id,
                project_id=pid,
                title=normalized_title,
                mode="primary",
                metadata=metadata,
            )

        ch = await self.context.chat_store.create_session_channel(tid, normalized_title, project_id=pid)
        if getattr(engine, "store", None):
            await engine.store.save_task(Task(
                id=tid,
                title=normalized_title,
                description=str(description or ""),
                project_id=pid,
                session_id=session_id,
                metadata={key: value for key, value in metadata.items() if key != "interface"},
                org_id=normalized_org or None,
            ))

        events: list[ServiceEvent] = []
        self.context.event_adapter._task_display_counter += 1
        self.context.event_adapter._task_display_map[tid] = self.context.event_adapter._task_display_counter
        display_id = f"OPC-{self.context.event_adapter.task_display_counter}"
        if emit_board_event and normalized_exec == "task":
            events.append(ServiceEvent("board_task_created", {
                "project_id": pid,
                "task_id": tid,
                "display_id": display_id,
                "board_id": board_id or pid,
                "title": normalized_title,
                "assignee_ids": assignee_ids or [],
            }))
        created_at = ch.get("created_at", time.time()) if isinstance(ch, dict) else time.time()
        session_payload = {
            "project_id": pid,
            "task_id": tid,
            "channel_id": channel_id,
            "session_id": session_id,
            "origin_task_id": tid,
            "exec_mode": normalized_exec,
            "company_profile": normalized_profile,
            "org_id": normalized_org,
            "preferred_agent": normalized_agent,
            "selected_execution_agent": normalized_agent,
            "title": normalized_title,
            "status": "pending",
            "created_at": created_at,
        }
        events.append(ServiceEvent("session_created", session_payload))
        if normalized_exec in {"company", "org", "custom"}:
            try:
                collab = await build_collab_sync(
                    engine,
                    self.context.agent_store,
                    self.context.chat_store,
                    self.context.event_adapter,
                    exec_mode=normalized_exec,
                )
                events.append(ServiceEvent("collab_sync_push", collab))
            except Exception:
                logger.opt(exception=True).warning("create_session collab_sync build failed")
        return ServiceResult(session_payload, events)

    def _session_metadata(
        self,
        *,
        task_id: str,
        exec_mode: str,
        company_profile: str,
        preferred_agent: str,
        org_id: str = "",
        interface: str = "office_ui",
    ) -> dict[str, Any]:
        identity = canonicalize_execution_identity(
            exec_mode=exec_mode,
            company_profile=company_profile,
            preferred_agent=preferred_agent,
            org_id=org_id,
            default_preferred_agent=self.context.mode_state.task_preferred_agent,
            explicit_exec_mode=True,
        )
        metadata: dict[str, Any] = {
            "exec_mode": identity.exec_mode,
            "company_profile": identity.company_profile,
            "preferred_agent": identity.preferred_agent,
            "interface": interface,
        }
        if identity.is_custom_org and identity.org_id:
            metadata.update({"org_id": identity.org_id, "organization_id": identity.org_id})
        if identity.is_task:
            metadata.update({
                "mode": "task",
                "execution_mode": "task_mode",
                "origin_task_id": task_id,
                "task_mode_contract": "single_full_capability_main_agent",
                "selected_execution_agent": identity.preferred_agent,
                "force_native_execution": identity.preferred_agent == "native",
            })
        else:
            metadata.update({
                "mode": identity.exec_mode,
                "execution_mode": "company_mode",
                "origin_task_id": "",
                "task_mode_contract": "",
                "selected_execution_agent": "",
                "force_native_execution": False,
                "preferred_external_agent": "",
                "agent_selection": {},
            })
        return metadata

    async def list(self, *, project_id: str, limit: int = 50) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        if not getattr(engine, "store", None):
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": project_id})
        sessions = await engine.store.list_sessions(project_id=self.context.normalize_project_id(project_id), parent_session_id=None, limit=limit)
        return ServiceResult({"project_id": self.context.normalize_project_id(project_id), "sessions": [self._record_to_dict(item) for item in sessions]})

    async def detail(self, *, project_id: str, task_id: str = "", session_id: str = "", limit: int = 200) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": project_id})
        task = await store.get_task(task_id) if task_id else None
        if not task and session_id and hasattr(store, "get_session"):
            session = await store.get_session(session_id)
        elif task and getattr(task, "session_id", None):
            session_id = str(task.session_id)
            session = await store.get_session(session_id) if hasattr(store, "get_session") else None
        else:
            session = None
        if not task and not session:
            raise ServiceError("session_not_found", "session_not_found", {"task_id": task_id, "session_id": session_id})
        transcript = await store.get_session_transcript(session_id) if session_id and hasattr(store, "get_session_transcript") else []
        return ServiceResult({
            "project_id": self.context.normalize_project_id(project_id),
            "task": self._task_to_dict(task) if task else None,
            "session": self._record_to_dict(session) if session else None,
            "messages": transcript[-max(1, min(limit, 500)):],
        })

    async def update_config(
        self,
        *,
        project_id: str,
        task_id: str,
        exec_mode: Any = None,
        company_profile: Any = None,
        preferred_agent: Any = None,
        org_id: Any = None,
    ) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready")
        task = await store.get_task(str(task_id or ""))
        if not task:
            raise ServiceError("task_not_found", "task_not_found", {"project_id": project_id, "task_id": task_id})
        current_exec, current_profile = self.resolve_task_session_config(task)
        current_agent = self.resolve_task_preferred_agent(task)
        lock_reason = await self.session_config_lock_reason(task, self.context.normalize_project_id(project_id))
        if lock_reason:
            raise ServiceError("session_config_locked", "session_config_locked", {
                "reason": lock_reason,
                "project_id": project_id,
                "task_id": task_id,
                "exec_mode": current_exec,
                "company_profile": current_profile,
                "org_id": self.resolve_task_org_id(task),
                "preferred_agent": current_agent,
                "selected_execution_agent": self.resolve_task_selected_execution_agent(task),
            })
        normalized_exec, normalized_profile, normalized_agent, normalized_org = self._normalize_requested_config(
            exec_mode=exec_mode if exec_mode is not None else current_exec,
            company_profile=company_profile,
            preferred_agent=preferred_agent if preferred_agent is not None else current_agent,
            org_id=org_id if org_id is not None else self.resolve_task_org_id(task),
            current_company_profile=current_profile,
            current_preferred_agent=current_agent,
        )
        if normalized_exec == "org" and normalized_org:
            await self._ensure_org_loaded(normalized_org)
        elif normalized_exec == "org":
            if self.context.get_active_saved_org_name is not None:
                normalized_org = await self.context.get_active_saved_org_name()
            if not normalized_org:
                raise ServiceError("org_id_required", "org_id_required", {"project_id": project_id, "task_id": task_id})
            await self._ensure_org_loaded(normalized_org)
        await self.persist_session_config(
            task,
            exec_mode=normalized_exec,
            company_profile=normalized_profile,
            preferred_agent=normalized_agent,
            org_id=normalized_org,
            engine=engine,
        )
        payload = {
            "project_id": self.context.normalize_project_id(project_id),
            "task_id": task_id,
            "exec_mode": normalized_exec,
            "company_profile": normalized_profile,
            "org_id": normalized_org,
            "preferred_agent": normalized_agent,
            "selected_execution_agent": self.resolve_task_selected_execution_agent(task),
        }
        return ServiceResult(payload, [ServiceEvent("session_updated", payload)])

    async def session_config_lock_reason(self, task: Any, project_id: str) -> str:
        task_id = str(getattr(task, "id", "") or "").strip()
        if task_id and self.context.chat_store:
            channel_id = f"session:{task_id}"
            try:
                count_fn = getattr(self.context.chat_store, "get_channel_visible_message_count", None)
                if callable(count_fn):
                    message_count = await count_fn(channel_id, project_id=project_id)
                else:
                    count_fn = getattr(self.context.chat_store, "get_channel_message_count", None)
                    message_count = await count_fn(channel_id, project_id=project_id) if callable(count_fn) else 0
                if int(message_count or 0) > 0:
                    return "message_history"
            except Exception:
                logger.opt(exception=True).debug("Failed to inspect session message count for config lock")
        status = getattr(getattr(task, "status", None), "value", getattr(task, "status", None))
        status_value = str(status or "").strip().lower()
        if status_value and status_value != "pending":
            return f"status:{status_value}"
        if getattr(task, "execution_lock", False) or getattr(task, "execution_locked_at", None):
            return "execution_lock"
        if str(getattr(task, "parent_session_id", "") or "").strip():
            return "child_session"
        if getattr(task, "result", None) is not None:
            return "result"
        if str(getattr(task, "linked_work_item_id", "") or "").strip():
            return "linked_work_item"
        metadata = dict(getattr(task, "metadata", {}) or {})
        for key in {
            "runtime_session_id",
            "external_session_id",
            "delegation_run_id",
            "company_run_id",
            "company_runtime_run_id",
            "company_runtime_started_at",
            "company_runtime_suspended_at",
            "work_item_id",
            "work_item_projection_id",
            "work_item_role_id",
            "work_item_turn_type",
            "active_work_item_id",
            "current_work_item_id",
            "runtime_context",
        }:
            value = metadata.get(key)
            if value not in (None, "", [], {}):
                return f"metadata:{key}"
        return ""

    async def persist_session_config(
        self,
        task: Any,
        *,
        exec_mode: str,
        company_profile: str,
        preferred_agent: str,
        org_id: str = "",
        engine: Any | None = None,
    ) -> None:
        runtime_engine = engine or self.context.engine
        metadata = dict(getattr(task, "metadata", {}) or {})
        identity = canonicalize_execution_identity(
            exec_mode=exec_mode,
            company_profile=company_profile,
            preferred_agent=preferred_agent,
            org_id=org_id,
            default_preferred_agent=self.context.mode_state.task_preferred_agent,
            explicit_exec_mode=True,
        )
        if identity.is_custom_org and not identity.org_id:
            raise ServiceError("org_id_required", "org_id_required", {
                "task_id": str(getattr(task, "id", "") or ""),
            })
        metadata["exec_mode"] = identity.exec_mode
        metadata["company_profile"] = identity.company_profile
        metadata["preferred_agent"] = identity.preferred_agent
        normalized_org_id = identity.org_id
        if identity.is_custom_org and normalized_org_id:
            metadata["org_id"] = normalized_org_id
            metadata["organization_id"] = normalized_org_id
        else:
            metadata.pop("org_id", None)
            metadata.pop("organization_id", None)
        if identity.is_task:
            task_id = str(getattr(task, "id", "") or "").strip()
            metadata.setdefault("mode", "task")
            metadata["execution_mode"] = "task_mode"
            metadata.setdefault("origin_task_id", task_id)
            metadata.setdefault("task_mode_contract", "single_full_capability_main_agent")
            metadata["selected_execution_agent"] = identity.preferred_agent
            metadata["force_native_execution"] = identity.preferred_agent == "native"
            metadata["preferred_external_agent"] = None if identity.preferred_agent == "native" else identity.preferred_agent
            metadata["agent_selection"] = {
                **dict(metadata.get("agent_selection", {}) or {}),
                "selected": identity.preferred_agent,
                "strategy": "native" if identity.preferred_agent == "native" else "external",
                "decision_reason": "task_mode_session_preference",
                "selection_source": "session_config",
            }
        else:
            for key in ("mode", "execution_mode", "task_mode_contract", "force_native_execution", "preferred_external_agent", "agent_selection"):
                metadata.pop(key, None)
        task.metadata = metadata
        task.org_id = normalized_org_id or None
        if getattr(runtime_engine, "store", None):
            await runtime_engine.store.save_task(task)
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if getattr(runtime_engine, "memory", None) and session_id:
            session_metadata = self._session_metadata(
                task_id=str(getattr(task, "id", "") or ""),
                exec_mode=identity.exec_mode,
                company_profile=identity.company_profile,
                preferred_agent=identity.preferred_agent,
                org_id=normalized_org_id,
            )
            if not identity.is_custom_org:
                session_metadata["org_id"] = ""
                session_metadata["organization_id"] = ""
            await runtime_engine.memory.ensure_session(
                session_id=session_id,
                project_id=getattr(task, "project_id", None) or runtime_engine.project_id or "default",
                title=getattr(task, "title", "") or "",
                mode="primary" if not getattr(task, "parent_session_id", None) else "child",
                parent_session_id=getattr(task, "parent_session_id", None),
                metadata=session_metadata,
            )

    async def send(
        self,
        *,
        project_id: str,
        task_id: str,
        content: str,
        mode: str = "task",
        company_profile: str | None = None,
        preferred_agent: str | None = None,
        org_id: str | None = None,
        domains: list[str] | None = None,
    ) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": project_id})
        task = await store.get_task(task_id)
        if not task:
            raise ServiceError("task_not_found", "task_not_found", {"project_id": project_id, "task_id": task_id})
        if not getattr(task, "session_id", None):
            task.session_id = str(uuid.uuid4())
            if getattr(engine, "memory", None):
                await engine.memory.ensure_session(task.session_id, project_id=project_id, title=task.title, mode="primary", metadata={"source": "service"})
            await store.save_task(task)

        # A Task selected by the UI/CLI is only the chat channel for company
        # mode.  Resolve the durable runtime scope before choosing execution
        # configuration, session, origin, or checkpoint.  In particular, a
        # role/work-item Task must never become the parent execution identity.
        company_target: dict[str, Any] | None = None
        task_is_company_runtime = is_company_runtime_task(task)
        try:
            company_target = await self._resolve_company_runtime_target(engine, task)
        except ServiceError as exc:
            if exc.code != "company_runtime_identity_mismatch" or task_is_company_runtime:
                raise

        if task.status == TaskStatus.CANCELLED:
            company_identity = (
                company_target.get("identity")
                if company_target is not None
                else None
            )
            checkpoint = (
                company_target.get("checkpoint")
                if company_target is not None
                else None
            )
            active_cancelled_anchor = bool(
                company_identity is not None
                and str(getattr(company_identity, "ui_anchor_task_id", "") or "").strip()
                == str(getattr(task, "id", "") or "").strip()
                and checkpoint is not None
                and str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
                in COMPANY_RUNTIME_CHECKPOINT_TYPES
                and str(getattr(checkpoint, "status", "") or "").strip().lower()
                in ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES
            )
            if not active_cancelled_anchor:
                raise ServiceError(
                    "session_ended",
                    "session_ended",
                    {"project_id": project_id, "task_id": task.id},
                )

        config_task = (
            company_target.get("config_task")
            if company_target is not None
            else task
        ) or task
        identity = self.resolve_task_identity(
            config_task,
            default_exec_mode=mode,
            default_company_profile=company_profile if company_profile is not None else "corporate",
            default_preferred_agent=preferred_agent if preferred_agent is not None else "native",
            default_org_id=org_id or "",
        )
        if identity.is_custom_org and not identity.org_id:
            raise ServiceError("org_id_required", "org_id_required", {"project_id": project_id, "task_id": task.id})

        # Existing company scopes read configuration from the resolver's
        # config source.  Only persist when that source is the selected Task;
        # this preserves normal session configuration while avoiding writes to
        # an internal work item merely because it was used as the UI channel.
        if (
            company_target is None
            or str(getattr(config_task, "id", "") or "").strip()
            == str(getattr(task, "id", "") or "").strip()
        ):
            await self.persist_session_config(
                task,
                exec_mode=identity.exec_mode,
                company_profile=identity.company_profile,
                preferred_agent=identity.preferred_agent,
                org_id=identity.org_id,
                engine=engine,
            )

        execution_session_id = str(getattr(task, "session_id", "") or "").strip()
        origin_task_id = str(getattr(task, "id", "") or "").strip() or None
        message_metadata: dict[str, Any] | None = None
        if company_target is not None:
            execution_session_id = str(
                company_target.get("runtime_session_id", "") or ""
            ).strip()
            origin_task_id = str(
                company_target.get("ui_anchor_task_id", "") or ""
            ).strip() or None
            if not execution_session_id:
                raise ServiceError(
                    "company_runtime_identity_mismatch",
                    "Company runtime has no canonical runtime session",
                    {"project_id": project_id, "task_id": task.id},
                )
            checkpoint = company_target.get("checkpoint")
            if checkpoint is not None:
                checkpoint_id = str(
                    getattr(checkpoint, "checkpoint_id", "") or ""
                ).strip()
                checkpoint_status = str(
                    getattr(checkpoint, "status", "") or ""
                ).strip().lower()
                if checkpoint_status != "pending":
                    raise ServiceError(
                        "company_runtime_checkpoint_not_pending",
                        "Company runtime checkpoint is not pending",
                        {
                            "project_id": project_id,
                            "task_id": task.id,
                            "checkpoint_id": checkpoint_id,
                            "checkpoint_status": checkpoint_status,
                        },
                    )
                message_metadata = {
                    "response_to_checkpoint_id": checkpoint_id,
                    "response_to_checkpoint_type": str(
                        getattr(checkpoint, "checkpoint_type", "") or ""
                    ).strip(),
                }

        response = await engine.process_message(
            str(content or "").strip(),
            project_id=project_id,
            session_id=execution_session_id,
            mode=identity.exec_mode,
            org_id=identity.org_id or None,
            company_profile=identity.company_profile if identity.is_company_runtime else None,
            preferred_agent=identity.preferred_agent if identity.is_task else None,
            domains=list(domains or []),
            origin_task_id=origin_task_id,
            message_metadata=message_metadata,
        )
        return ServiceResult({
            "project_id": project_id,
            "task_id": task.id,
            "session_id": execution_session_id,
            "response": response,
        })

    async def rename(self, *, project_id: str, task_id: str = "", session_id: str = "", title: str) -> ServiceResult:
        pid = self.context.normalize_project_id(project_id)
        target = str(task_id or session_id or "").strip()
        new_title = str(title or "").strip()
        if not target:
            raise ServiceError("missing_session", "Missing task_id or session_id")
        if not new_title:
            raise ServiceError("missing_title", "Missing title")
        engine = await self.context.engine_for_project(pid)
        store = getattr(engine, "store", None)
        if not store:
            raise ServiceError("store_not_ready", "store_not_ready", {"project_id": pid})

        task = await store.get_task(target) if hasattr(store, "get_task") else None
        task_project_id = str(getattr(task, "project_id", "") or "").strip() if task is not None else ""
        if (
            task is not None
            and task_project_id not in {"", "default"}
            and self.context.normalize_project_id(task_project_id) != pid
        ):
            task = None
        session = None
        resolved_session_id = ""
        if task is not None:
            resolved_session_id = str(getattr(task, "session_id", "") or "")
            if resolved_session_id and hasattr(store, "get_session"):
                session = await store.get_session(resolved_session_id)
        elif hasattr(store, "get_session"):
            session = await store.get_session(target)
            session_project_id = str(getattr(session, "project_id", "") or "").strip() if session is not None else ""
            if (
                session is not None
                and session_project_id not in {"", "default"}
                and self.context.normalize_project_id(session_project_id) != pid
            ):
                session = None
            if session is not None:
                resolved_session_id = str(getattr(session, "session_id", "") or target)
                if hasattr(store, "get_tasks"):
                    tasks = await store.get_tasks(project_id=pid)
                    task = next((item for item in tasks if str(getattr(item, "session_id", "") or "") == resolved_session_id), None)

        if task is None and session is None:
            raise ServiceError("session_not_found", "session_not_found", {"target": target})

        resolved_task_id = str(getattr(task, "id", "") or "") if task is not None else ""
        if task is not None:
            task.title = new_title
            await store.save_task(task)
            resolved_session_id = str(getattr(task, "session_id", "") or resolved_session_id)
        if resolved_session_id:
            if getattr(engine, "memory", None):
                update_title = getattr(engine.memory, "update_session_title", None)
                if callable(update_title):
                    await update_title(resolved_session_id, new_title)
            elif session is not None and hasattr(store, "save_session"):
                session.title = new_title
                await store.save_session(session)
        if resolved_task_id:
            await self.context.chat_store.update_channel_name(f"session:{resolved_task_id}", new_title, project_id=pid)
        payload = {
            "project_id": pid,
            "task_id": resolved_task_id,
            "session_id": resolved_session_id,
            "title": new_title,
        }
        return ServiceResult(payload, [ServiceEvent("session_title_updated", payload)])

    async def delete(self, *, project_id: str, task_id: str) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        task = await engine.store.get_task(task_id) if getattr(engine, "store", None) else None
        if not task:
            # Older UI/session rows can outlive their runtime task rows after
            # project migrations or startup reconciliation.  Delete should stay
            # idempotent for those orphaned sidebar chats instead of surfacing a
            # task_not_found error to the frontend.
            tid = str(task_id or "").strip()
            if not tid:
                raise ServiceError("task_not_found", "task_not_found", {"task_id": task_id})
            await self.context.chat_store.delete_channel(f"session:{tid}", project_id=project_id)
            delete_progress = getattr(self.context.chat_store, "delete_progress", None)
            if callable(delete_progress):
                await delete_progress(tid, project_id=project_id)
            await self.context.chat_store.delete_activity_messages_for_task(project_id, tid)
            payload = {"project_id": project_id, "task_id": tid, "task_ids": [tid], "orphaned": True}
            return ServiceResult(payload, [ServiceEvent("session_deleted", {"project_id": project_id, "task_id": tid})])
        session_ids_to_runtime_clean: set[str] = {str(getattr(task, "session_id", "") or "").strip()}
        comms_dirs_to_remove: set[Path] = set()
        tasks_to_scan = [task]
        parent_sid = str(getattr(task, "session_id", "") or task_id)
        try:
            siblings = await engine.store.get_tasks(project_id=project_id)
            for sibling in siblings:
                if getattr(sibling, "id", "") != task_id and getattr(sibling, "parent_session_id", None) == parent_sid:
                    tasks_to_scan.append(sibling)
                    if getattr(sibling, "session_id", None):
                        session_ids_to_runtime_clean.add(str(sibling.session_id))
        except Exception:
            pass
        for candidate in tasks_to_scan:
            comms_dir = self._resolve_task_comms_dir(candidate, engine=engine)
            if comms_dir is not None:
                comms_dirs_to_remove.add(comms_dir)

        if self.context.cancel_task_tree is not None:
            all_task_ids = await self.context.cancel_task_tree(task_id, hard=True, store=engine.store)
        else:
            all_task_ids = [str(getattr(candidate, "id", "") or "") for candidate in tasks_to_scan if str(getattr(candidate, "id", "") or "")]
            hard_delete = getattr(engine.store, "hard_delete_task", None)
            if callable(hard_delete):
                for candidate in reversed(tasks_to_scan):
                    await hard_delete(getattr(candidate, "id", ""), getattr(candidate, "session_id", None))
        cleanup = getattr(engine.store, "delete_company_runtime_artifacts_for_session", None)
        if callable(cleanup):
            for session_id in session_ids_to_runtime_clean:
                if session_id:
                    try:
                        await cleanup(session_id)
                    except Exception:
                        pass
        for tid in all_task_ids:
            try:
                await self.context.chat_store.delete_channel(f"session:{tid}", project_id=project_id)
            except Exception:
                pass
            try:
                await self.context.chat_store.delete_activity_messages_for_task(project_id, tid)
            except Exception:
                pass
        for path in comms_dirs_to_remove:
            try:
                if path.is_dir():
                    shutil.rmtree(str(path), ignore_errors=True)
            except Exception:
                pass
        events = [
            ServiceEvent("session_deleted", {"project_id": project_id, "task_id": tid})
            for tid in all_task_ids
        ]
        return ServiceResult({"project_id": project_id, "task_id": task_id, "task_ids": all_task_ids}, events)

    async def stop(self, *, project_id: str, task_id: str = "", session_id: str = "", target: str = "") -> ServiceResult:
        from opc.layer2_organization.work_item_transition import apply_task_status_transition

        task, resolved_session_id = await self._resolve_task_target(
            project_id=project_id,
            target=target,
            task_id=task_id,
            session_id=session_id,
        )
        resolved_task_id = str(getattr(task, "id", "") or "")
        default_payload = {
            "project_id": self.context.normalize_project_id(project_id),
            "task_id": resolved_task_id,
            "session_id": resolved_session_id,
        }
        if self.context.runtime_stop_hook is not None:
            return await self._coerce_hook_result(
                await self.context.runtime_stop_hook(project_id=project_id, task_id=resolved_task_id, session_id=resolved_session_id),
                default_payload=default_payload,
            )
        engine = await self.context.engine_for_project(project_id)
        try:
            target_info = await self._resolve_company_runtime_target(engine, task)
        except ServiceError as exc:
            if exc.code != "company_runtime_identity_mismatch":
                raise
            target_info = None
        if target_info is None and is_company_runtime_task(task):
            raise ServiceError(
                "company_runtime_identity_mismatch",
                "Company runtime identity could not be resolved; refusing task-mode cancellation",
                {"task_id": resolved_task_id, "session_id": resolved_session_id},
            )
        if target_info is not None:
            stop_intent_id = str(uuid.uuid4())
            affected_task_ids = list(target_info.get("affected_task_ids", []) or [resolved_task_id])
            suspended: dict[str, Any] | None = None
            suspend = getattr(engine, "suspend_company_runtime", None)
            if callable(suspend):
                try:
                    suspended = await suspend(
                        origin_task_id=str(target_info.get("origin_task_id", "") or resolved_task_id),
                        session_id=(str(target_info.get("runtime_session_id", "") or resolved_session_id).strip() or None),
                        reason="user_stop",
                        checkpoint_type="company_runtime_suspended",
                        stop_intent_id=stop_intent_id,
                    )
                except Exception:
                    logger.opt(exception=True).warning("suspend_company_runtime failed during service stop")
            if suspended is None:
                raise ServiceError(
                    "company_runtime_suspend_failed",
                    "Company runtime could not be suspended",
                    {"runtime_session_id": target_info.get("runtime_session_id", "")},
                )
            for candidate in list(suspended.get("task_ids", []) or []):
                candidate_id = str(candidate or "").strip()
                if candidate_id and candidate_id not in affected_task_ids:
                    affected_task_ids.append(candidate_id)
            self.context.stop_requested_task_ids.update(affected_task_ids)
            if self.context.cancel_session_tasks is not None:
                for tid in affected_task_ids:
                    self.context.cancel_session_tasks(tid)
            if self.context.chat_store is not None:
                try:
                    await self.context.chat_store.insert_message(
                        channel_id=f"session:{str(target_info.get('origin_task_id', '') or resolved_task_id)}",
                        sender="system",
                        sender_name="System",
                        content="Company runtime suspended by user",
                        project_id=self.context.normalize_project_id(project_id),
                        metadata={
                            "type": "system",
                            "stop_reason": "user_stop",
                            "checkpoint_type": "company_runtime_suspended",
                            "checkpoint_id": str((suspended or {}).get("checkpoint_id", "") or ""),
                            "stop_intent_id": stop_intent_id,
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("failed to insert company runtime stop system message")
            payload = {
                **default_payload,
                "status": "suspended",
                "runtime_control_state": "suspended",
                "can_resume": True,
                "stop_intent_id": stop_intent_id,
                "checkpoint_id": str((suspended or {}).get("checkpoint_id", "") or ""),
                "task_ids": affected_task_ids,
                "resume_parent_session_id": str(target_info.get("runtime_session_id", "") or resolved_session_id),
            }
            return ServiceResult(payload, [ServiceEvent("session_runtime_control", payload), ServiceEvent("session_updated", payload)])

        await apply_task_status_transition(engine.store, task, target_status_or_phase=TaskStatus.CANCELLED, reason="service_session_stop")
        if self.context.cancel_session_tasks is not None:
            self.context.cancel_session_tasks(resolved_task_id)
        payload = {**default_payload, "status": "cancelled", "runtime_control_state": "stopped", "can_resume": False}
        return ServiceResult(payload, [ServiceEvent("board_task_status_changed", payload), ServiceEvent("session_updated", payload)])

    async def complete(self, *, project_id: str, task_id: str) -> ServiceResult:
        from opc.layer2_organization.work_item_transition import apply_task_status_transition

        engine = await self.context.engine_for_project(project_id)
        task = await engine.store.get_task(task_id) if getattr(engine, "store", None) else None
        if not task:
            raise ServiceError("task_not_found", "task_not_found", {"task_id": task_id})
        if self.context.cancel_session_tasks is not None:
            self.context.cancel_session_tasks(task_id)
        await apply_task_status_transition(engine.store, task, target_status_or_phase=TaskStatus.DONE, reason="service_session_complete")
        payload = {"project_id": project_id, "task_id": task_id, "status": "done"}
        return ServiceResult(payload, [ServiceEvent("board_task_status_changed", payload), ServiceEvent("session_updated", payload)])

    async def continue_run(
        self,
        *,
        project_id: str,
        task_id: str = "",
        session_id: str = "",
        runtime_session_id: str = "",
        checkpoint_id: str = "",
        target: str = "",
        content: str = "",
    ) -> ServiceResult:
        task, resolved_session_id = await self._resolve_task_target(
            project_id=project_id,
            target=target,
            task_id=task_id,
            session_id=session_id,
        )
        resolved_task_id = str(getattr(task, "id", "") or "")
        default_payload = {
            "project_id": self.context.normalize_project_id(project_id),
            "task_id": resolved_task_id,
            "session_id": resolved_session_id,
            "runtime_control_state": "resuming",
        }
        if self.context.runtime_continue_hook is not None:
            return await self._coerce_hook_result(
                await self.context.runtime_continue_hook(
                    project_id=project_id,
                    task_id=resolved_task_id,
                    session_id=resolved_session_id,
                    content=content,
                ),
                default_payload=default_payload,
            )
        engine = await self.context.engine_for_project(project_id)
        try:
            company_target = await self._resolve_company_runtime_target(
                engine,
                task,
                runtime_session_id=runtime_session_id,
                checkpoint_id=checkpoint_id,
            )
        except ServiceError as exc:
            if exc.code != "company_runtime_identity_mismatch" or runtime_session_id or checkpoint_id:
                raise
            if is_company_runtime_task(task):
                raise
            company_target = None
        target_info = company_target or {
            "affected_task_ids": [resolved_task_id],
            "ui_anchor_task_id": resolved_task_id,
            "runtime_session_id": resolved_session_id,
            "config_task": task,
            "checkpoint": None,
        }
        affected_task_ids = list(target_info.get("affected_task_ids", []) or [resolved_task_id])
        message = str(content or "").strip() or "Resume the existing runtime."
        config_task = target_info.get("config_task") or task
        config_exec_mode, config_company_profile = self.resolve_task_session_config(config_task)
        engine_mode = "company" if config_exec_mode == "company" else (
            "org" if config_exec_mode in {"org", "custom"} else "task"
        )
        if engine_mode in {"company", "org"}:
            checkpoint = target_info.get("checkpoint")
            if checkpoint is None:
                raise ServiceError("company_runtime_checkpoint_not_found", "No active company runtime checkpoint")
            if str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending":
                raise ServiceError(
                    "company_runtime_checkpoint_not_pending",
                    "Company runtime checkpoint is not pending",
                    {"checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or "")},
                )
        else:
            checkpoint = None
        org_id = self.resolve_task_org_id(config_task) if engine_mode == "org" else ""
        if engine_mode == "org" and not org_id:
            raise ServiceError(
                "org_id_required",
                "org_id_required",
                {"project_id": project_id, "task_id": resolved_task_id},
            )
        message_metadata: dict[str, Any] = {"ui_force_resume": True}
        if checkpoint is not None:
            message_metadata.update({
                "response_to_checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
                "response_to_checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
            })
        response = await engine.process_message(
            message,
            project_id=self.context.normalize_project_id(project_id),
            session_id=str(target_info.get("runtime_session_id", "") or resolved_session_id),
            mode=engine_mode,
            org_id=org_id or None,
            company_profile=config_company_profile if engine_mode == "company" else None,
            preferred_agent=self.resolve_task_preferred_agent(config_task) if engine_mode == "task" else None,
            origin_task_id=(str(target_info.get("ui_anchor_task_id", "") or "").strip() or None),
            message_metadata=message_metadata,
        )
        runtime_control_state = "idle"
        pending_runtime_checkpoint_id = ""
        if engine_mode in {"company", "org"}:
            refreshed_index = await load_company_runtime_identity_index(
                engine.store,
                self.context.normalize_project_id(project_id),
            )
            refreshed_identity = refreshed_index.resolve(
                runtime_session_id=str(
                    target_info.get("runtime_session_id", "") or resolved_session_id
                ),
            )
            if refreshed_identity is not None and refreshed_identity.pending_checkpoint_id:
                pending_runtime_checkpoint_id = refreshed_identity.pending_checkpoint_id
                runtime_control_state = (
                    "suspended"
                    if refreshed_identity.pending_checkpoint_status == "pending"
                    else "resuming"
                )
        payload = {
            **default_payload,
            "status": runtime_control_state,
            "runtime_control_state": runtime_control_state,
            "can_resume": runtime_control_state == "suspended",
            "response": response,
            "task_ids": affected_task_ids,
            "resume_parent_session_id": str(target_info.get("runtime_session_id", "") or resolved_session_id),
            "pending_runtime_checkpoint_id": pending_runtime_checkpoint_id,
        }
        return ServiceResult(payload, [ServiceEvent("session_runtime_control", payload), ServiceEvent("session_updated", payload)])

    async def resume(
        self,
        *,
        project_id: str,
        task_id: str = "",
        session_id: str = "",
        runtime_session_id: str = "",
        checkpoint_id: str = "",
        target: str = "",
        content: str = "",
    ) -> ServiceResult:
        return await self.continue_run(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            runtime_session_id=runtime_session_id,
            checkpoint_id=checkpoint_id,
            target=target,
            content=content,
        )

    async def resolve_starting_session(self, *, project_id: str) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            return await self.create(project_id=project_id, title="CLI Session", interface="cli")
        sessions = await store.list_sessions(project_id=self.context.normalize_project_id(project_id), parent_session_id=None, limit=1)
        if sessions:
            session = sessions[0]
            return ServiceResult({"session_id": session.session_id, "restored": True, "title": session.title, "project_id": project_id})
        created = await self.create(project_id=project_id, title="CLI Session", interface="cli")
        created.payload["restored"] = False
        return created

    @staticmethod
    def _record_to_dict(record: Any) -> dict[str, Any]:
        if record is None:
            return {}
        data = dict(getattr(record, "__dict__", {}) or {})
        for key, value in list(data.items()):
            if hasattr(value, "isoformat"):
                data[key] = value.isoformat()
        return data

    @staticmethod
    def _task_to_dict(task: Any) -> dict[str, Any]:
        if task is None:
            return {}
        data = dict(getattr(task, "__dict__", {}) or {})
        status = data.get("status")
        if hasattr(status, "value"):
            data["status"] = status.value
        for key, value in list(data.items()):
            if hasattr(value, "isoformat"):
                data[key] = value.isoformat()
        return data

    def _resolve_task_comms_dir(self, task: Any, *, engine: Any) -> Path | None:
        metadata = dict(getattr(task, "metadata", {}) or {})
        workspace_root = (
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
            or str(metadata.get("setup_workspace_prepared") or "").strip()
        )
        comms_root = str(metadata.get("comms_root") or "").strip()
        session_id = (
            str(getattr(task, "parent_session_id", "") or "").strip()
            or str(getattr(task, "session_id", "") or "").strip()
        )
        pid = str(getattr(task, "project_id", "") or getattr(engine, "project_id", "") or "default")
        if not session_id:
            return None
        try:
            from opc.layer2_organization import comms as file_comms

            if workspace_root:
                return file_comms.resolve_layout(workspace_root, pid, session_id).root
            if comms_root:
                return file_comms.resolve_layout(str(Path(comms_root).parent), pid, session_id).root
        except Exception:
            return None
        return None
