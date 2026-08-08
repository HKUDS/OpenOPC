"""Shared Office service context.

This module intentionally owns runtime wiring that was previously duplicated or
buried inside the WebSocket handler: project validation, project-engine
delegation, active mode defaults, and access to UI persistence stores.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from opc.core.config import get_project_workplace

LoadOrgConfigHook = Callable[[Optional[str]], bool]
SetActiveOrgHook = Callable[[str], Awaitable[None]]
GetActiveOrgHook = Callable[[], Awaitable[str]]
PersistRuntimeConfigHook = Callable[[], None]
RebindEngineConfigHook = Callable[[Any], None]
AsyncNoArgHook = Callable[[], Awaitable[Any]]
CancelSessionTasksHook = Callable[[str], None]
CancelTaskTreeHook = Callable[..., Awaitable[list[str]]]
RuntimeControlHook = Callable[..., Awaitable[Any]]


@dataclass
class ModeState:
    exec_mode: str = "task"
    company_profile: str = "corporate"
    task_preferred_agent: str = "native"


class OfficeServiceContext:
    """Dependency holder shared by Office UI, CLI, and CLI board services."""

    def __init__(
        self,
        *,
        engine: Any,
        agent_store: Any,
        chat_store: Any,
        event_adapter: Any,
        mode_state: ModeState | None = None,
    ) -> None:
        self.root_engine = engine
        self.active_engine = engine
        self.agent_store = agent_store
        self.chat_store = chat_store
        self.event_adapter = event_adapter
        self.mode_state = mode_state or ModeState()
        self.active_project_id = self.normalize_project_id(getattr(engine, "project_id", None))
        self.project_switch_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.task_bg_map: dict[str, set[asyncio.Task[Any]]] = {}
        self.task_bg_context: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self.session_to_task: dict[str, str] = {}
        self.active_runtime_children: dict[str, str] = {}
        self.stop_requested_task_ids: set[str] = set()
        self.task_locks: dict[str, asyncio.Lock] = {}
        self.task_lock_holders: dict[str, asyncio.Task[Any]] = {}
        self.load_active_org_config: LoadOrgConfigHook | None = None
        self.set_active_saved_org_name: SetActiveOrgHook | None = None
        self.get_active_saved_org_name: GetActiveOrgHook | None = None
        self.on_engine_activated: Callable[[Any, str], None] | None = None
        self.persist_runtime_config: PersistRuntimeConfigHook | None = None
        self.rebind_engine_config: RebindEngineConfigHook | None = None
        self.sync_role_map: AsyncNoArgHook | None = None
        self.ensure_custom_role_agents: AsyncNoArgHook | None = None
        self.broadcast_snapshot: AsyncNoArgHook | None = None
        self.cancel_session_tasks: CancelSessionTasksHook | None = None
        self.cancel_task_tree: CancelTaskTreeHook | None = None
        self.runtime_stop_hook: RuntimeControlHook | None = None
        self.runtime_continue_hook: RuntimeControlHook | None = None
        # Workplace path cache: project_id -> resolved absolute path string.
        # Populated lazily by set_project_workplace_cache() so that
        # project_workplace() stays synchronous everywhere it is called.
        self._workplace_cache: dict[str, str] = {}

    @property
    def engine(self) -> Any:
        return self.active_engine

    @property
    def opc_home(self) -> Path:
        return Path(getattr(self.root_engine, "opc_home", Path.cwd() / ".opc"))

    @staticmethod
    def normalize_project_id(project_id: Any) -> str:
        return str(project_id or "default").strip() or "default"

    @staticmethod
    def is_safe_project_id(project_id: str) -> bool:
        return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project_id or ""))

    @staticmethod
    def store_is_ready(store: Any) -> bool:
        if store is None:
            return False
        ready = getattr(store, "is_ready", True)
        return bool(ready)

    def active_engine_project_id(self) -> str:
        return self.normalize_project_id(getattr(self.active_engine, "project_id", None) or self.active_project_id)

    def rebind_config(self, config: Any) -> None:
        if self.rebind_engine_config is not None:
            self.rebind_engine_config(config)
            return
        self.engine.config = config
        org_engine = getattr(self.engine, "org_engine", None)
        if org_engine is not None:
            org_engine.config = config
        talent_market = getattr(self.engine, "talent_market", None)
        if talent_market is not None:
            talent_market.config = config
        if hasattr(self.engine, "_runtime_config_signature"):
            self.engine._runtime_config_signature = None

    def is_custom_org_editable(self) -> bool:
        mode = str(getattr(self.mode_state, "exec_mode", "") or "").strip().lower()
        profile = str(getattr(self.mode_state, "company_profile", "") or "").strip().lower()
        cfg_org = getattr(getattr(self.engine, "config", None), "org", None)
        cfg_profile = str(getattr(cfg_org, "company_profile", "") or "").strip().lower()
        org_id = str(getattr(cfg_org, "organization_id", "") or "").strip().lower()
        return (
            mode in {"org", "custom"}
            and profile == "custom"
            and cfg_profile == "custom"
            and org_id != "corporate"
        )

    def project_dir(self, project_id: str) -> Path:
        return self.opc_home / "projects" / self.normalize_project_id(project_id)

    def project_workplace(self, project_id: str) -> Path:
        hook = getattr(self, "project_workplace_hook", None)
        if callable(hook):
            return Path(hook(self.normalize_project_id(project_id)))
        pid = self.normalize_project_id(project_id)
        # Use cached custom path when available (populated by set_project_workplace_cache)
        custom = self._workplace_cache.get(pid)
        return get_project_workplace(pid, custom_path=custom)

    def set_project_workplace_cache(self, project_id: str, workplace_path: str) -> None:
        """Store a resolved workplace path in the in-memory cache.

        Called after a successful ``save_project_workplace`` DB write so that
        subsequent synchronous calls to ``project_workplace()`` see the new
        value immediately without an extra DB round-trip.
        """
        pid = self.normalize_project_id(project_id)
        if workplace_path:
            self._workplace_cache[pid] = workplace_path
        else:
            self._workplace_cache.pop(pid, None)

    def clear_project_workplace_cache(self, project_id: str | None = None) -> None:
        """Evict one or all entries from the workplace path cache."""
        if project_id:
            self._workplace_cache.pop(self.normalize_project_id(project_id), None)
        else:
            self._workplace_cache.clear()

    def list_project_entries(self) -> list[dict[str, str]]:
        projects_dir = self.opc_home / "projects"
        projects: list[dict[str, str]] = []
        if projects_dir.is_dir():
            for entry in sorted(projects_dir.iterdir()):
                if entry.is_dir():
                    projects.append({"id": entry.name, "name": entry.name})
        if not any(project["id"] == "default" for project in projects):
            projects.insert(0, {"id": "default", "name": "default"})
        return projects

    def project_exists(self, project_id: str) -> bool:
        normalized = self.normalize_project_id(project_id)
        if normalized == "default":
            return True
        return self.project_dir(normalized).is_dir()

    async def engine_for_project(self, project_id: str) -> Any:
        normalized = self.normalize_project_id(project_id)
        root = self.root_engine
        current_root_project = self.normalize_project_id(getattr(root, "project_id", None))
        if normalized == current_root_project:
            engine = root
        else:
            delegate_getter = getattr(root, "_get_project_delegate", None)
            if not callable(delegate_getter):
                raise RuntimeError("Project switching requires OPCEngine project delegates.")
            maybe_engine = delegate_getter(normalized)
            engine = await maybe_engine if inspect.isawaitable(maybe_engine) else maybe_engine
        wire = getattr(self, "wire_engine_callbacks", None)
        if callable(wire):
            try:
                wire(engine)
            except Exception:
                logger.opt(exception=True).debug("Failed to wire service project engine callbacks")
        return engine

    async def activate_project(self, project_id: str) -> Any:
        engine = await self.engine_for_project(project_id)
        self.active_engine = engine
        self.active_project_id = self.normalize_project_id(getattr(engine, "project_id", None) or project_id)
        if self.on_engine_activated is not None:
            self.on_engine_activated(engine, self.active_project_id)
        else:
            ensure_attachment_store = getattr(engine, "_ensure_attachment_store", None)
            if callable(ensure_attachment_store):
                ensure_attachment_store()
        return engine

    def get_task_lock(self, task_id: str) -> asyncio.Lock:
        prev_holder = self.task_lock_holders.get(task_id)
        if prev_holder is not None and prev_holder.done():
            self.task_locks.pop(task_id, None)
            self.task_lock_holders.pop(task_id, None)
        lock = self.task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self.task_locks[task_id] = lock
        return lock
