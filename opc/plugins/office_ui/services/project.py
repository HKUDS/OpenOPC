"""Project lifecycle service shared by Office UI and CLI."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger
from opc.layer5_memory.markdown_memory import MarkdownMemoryStore
from opc.plugins.office_ui.snapshot_builder import build_collab_sync, build_project_index_sync, build_snapshot
from opc.project_id import project_id_policy

from .context import OfficeServiceContext
from .models import ServiceEvent, ServiceError, ServiceResult


class ProjectService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    @staticmethod
    def _quote_sql_identifier(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    @classmethod
    def _rewrite_project_id_in_sqlite(cls, db_path: Path, old_project_id: str, new_project_id: str) -> dict[str, int]:
        if not db_path.exists():
            return {}
        counts: dict[str, int] = {}
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            for (table_name,) in rows:
                table = str(table_name or "")
                if not table or table.startswith("sqlite_"):
                    continue
                quoted = cls._quote_sql_identifier(table)
                columns = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
                if not any(str(col[1]) == "project_id" for col in columns):
                    continue
                cursor = conn.execute(
                    f"UPDATE {quoted} SET project_id = ? WHERE project_id = ?",
                    (new_project_id, old_project_id),
                )
                counts[table] = int(cursor.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
        return counts

    async def _close_project_engine_store(self, project_id: str) -> None:
        root = self.context.root_engine
        candidates: list[Any] = []
        active = self.context.engine
        if self.context.normalize_project_id(getattr(active, "project_id", None)) == project_id:
            candidates.append(active)
        if self.context.normalize_project_id(getattr(root, "project_id", None)) == project_id:
            candidates.append(root)
        delegates = getattr(root, "_project_engine_delegates", None)
        if isinstance(delegates, dict):
            delegate = delegates.pop(project_id, None)
            if delegate is not None:
                candidates.append(delegate)
        seen: set[int] = set()
        for engine in candidates:
            marker = id(engine)
            if marker in seen:
                continue
            seen.add(marker)
            store = getattr(engine, "store", None)
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    maybe = close()
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    logger.opt(exception=True).debug(f"Failed to close project store for {project_id}")

    def _project_list_payload(self, active_project_id: str) -> dict[str, Any]:
        return {
            "projects": self.context.list_project_entries(),
            "active_project_id": self.context.normalize_project_id(active_project_id),
            "project_id_policy": project_id_policy(),
        }

    async def list(self, *, active_project_id: str | None = None) -> ServiceResult:
        active = active_project_id or self.context.active_engine_project_id()
        return ServiceResult(self._project_list_payload(active))

    async def create(self, project_id: str, *, active_project_id: str | None = None) -> ServiceResult:
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if not self.context.is_safe_project_id(project_id):
            raise ServiceError("invalid_project_id", "Invalid project_id (use alphanumeric, hyphens, underscores)")

        projects_dir = self.context.project_dir(project_id)
        memory_store = MarkdownMemoryStore(Path(self.context.root_engine.opc_home))
        memory_path = memory_store.memory_path(project_id)
        workplace = self.context.project_workplace(project_id)
        if projects_dir.exists() or memory_path.exists() or workplace.exists():
            raise ServiceError("project_exists", f"Project '{project_id}' already exists")

        projects_dir.mkdir(parents=True, exist_ok=False)
        workplace.mkdir(parents=True, exist_ok=False)
        memory_store.ensure_memory_file(project_id, f"# Project Memory ({project_id})")
        active = active_project_id or self.context.active_engine_project_id()
        return ServiceResult({
            **self._project_list_payload(active),
            "action": "create_project",
            "project_id": project_id,
        })

    async def rename(self, old_project_id: str, new_project_id: str) -> ServiceResult:
        old_id = str(old_project_id or "").strip()
        new_id = str(new_project_id or "").strip()
        if not old_id or not new_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if old_id == "default":
            raise ServiceError("default_project", "Cannot rename the default project")
        if new_id == "default":
            raise ServiceError("invalid_project_id", "Cannot rename a project to 'default'")
        if not self.context.is_safe_project_id(old_id) or not self.context.is_safe_project_id(new_id):
            raise ServiceError("invalid_project_id", "Invalid project_id (use alphanumeric, hyphens, underscores)")
        if old_id == new_id:
            return ServiceResult({
                **self._project_list_payload(self.context.active_engine_project_id()),
                "action": "rename_project",
                "old_project_id": old_id,
                "project_id": new_id,
                "new_project_id": new_id,
                "renamed": False,
            })

        old_dir = self.context.project_dir(old_id)
        new_dir = self.context.project_dir(new_id)
        memory_store = MarkdownMemoryStore(Path(self.context.root_engine.opc_home))
        old_memory = memory_store.memory_path(old_id)
        new_memory = memory_store.memory_path(new_id)
        old_workplace = self.context.project_workplace(old_id)
        new_workplace = self.context.project_workplace(new_id)
        old_exists = old_dir.is_dir() or old_memory.exists() or old_workplace.exists()
        if not old_exists:
            raise ServiceError("project_not_found", f"Project '{old_id}' does not exist", {"project_id": old_id})
        if new_dir.exists() or new_memory.exists() or new_workplace.exists():
            raise ServiceError("project_exists", f"Project '{new_id}' already exists", {"project_id": new_id})
        chat_data_exists = getattr(self.context.chat_store, "project_data_exists", None)
        if callable(chat_data_exists) and await chat_data_exists(new_id):
            raise ServiceError("project_exists", f"Project '{new_id}' already has UI data", {"project_id": new_id})

        was_active = self.context.active_engine_project_id() == old_id
        if was_active:
            for task in list(self.context.background_tasks):
                task.cancel()
            self.context.background_tasks.clear()
            self.context.task_bg_map.clear()
            self.context.task_bg_context.clear()
        await self._close_project_engine_store(old_id)

        if old_dir.is_dir():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            old_dir.rename(new_dir)
        else:
            new_dir.mkdir(parents=True, exist_ok=True)
        if old_memory.exists():
            new_memory.parent.mkdir(parents=True, exist_ok=True)
            old_memory.rename(new_memory)
        if old_workplace.exists():
            new_workplace.parent.mkdir(parents=True, exist_ok=True)
            old_workplace.rename(new_workplace)

        db_counts = self._rewrite_project_id_in_sqlite(new_dir / "tasks.db", old_id, new_id)
        chat_counts: dict[str, int] = {}
        rename_chat = getattr(self.context.chat_store, "rename_project_data", None)
        if callable(rename_chat):
            try:
                chat_counts = dict(await rename_chat(old_id, new_id) or {})
            except ValueError as exc:
                raise ServiceError("project_exists", str(exc), {"project_id": new_id}) from exc

        active_id = self.context.active_engine_project_id()
        events = [ServiceEvent("project_renamed", {"old_project_id": old_id, "project_id": new_id, "new_project_id": new_id})]
        payload: dict[str, Any] = {
            **self._project_list_payload(active_id),
            "action": "rename_project",
            "old_project_id": old_id,
            "project_id": new_id,
            "new_project_id": new_id,
            "renamed": True,
            "updated_task_tables": db_counts,
            "updated_ui_rows": chat_counts,
        }
        if was_active:
            engine = await self.context.activate_project(new_id)
            await self.context.chat_store.ensure_activity_channel(project_id=new_id)
            await self.context.chat_store.ensure_secretary_channel(project_id=new_id)
            payload["active_project_id"] = new_id
            payload["engine_project_id"] = getattr(engine, "project_id", new_id)
            events.append(ServiceEvent("project_switched", {"project_id": new_id}))
            snapshot = await build_snapshot(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            events.append(ServiceEvent("snapshot", snapshot))
            collab = await build_collab_sync(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
                exec_mode=self.context.mode_state.exec_mode,
            )
            events.append(ServiceEvent("collab_sync_push", collab))
        return ServiceResult(payload, events)

    async def delete(self, project_id: str) -> ServiceResult:
        project_id = str(project_id or "").strip()
        if not project_id or project_id == "default":
            raise ServiceError("default_project", "Cannot delete the default project")
        if not self.context.is_safe_project_id(project_id):
            raise ServiceError("invalid_project_id", "Invalid project_id")

        was_active = self.context.active_engine_project_id() == project_id
        if was_active:
            for task in list(self.context.background_tasks):
                task.cancel()
            self.context.background_tasks.clear()
            self.context.task_bg_map.clear()
            self.context.task_bg_context.clear()

        deleted_channels = 0
        delete_chat = getattr(self.context.chat_store, "delete_project_data", None)
        if callable(delete_chat):
            deleted_channels = int(await delete_chat(project_id) or 0)
            logger.info(f"Deleted {deleted_channels} channels for project '{project_id}'")

        # Close every engine bound to this project AND evict its delegate from
        # the root engine's cache; a stale delegate would otherwise be reused
        # (with a closed store) if a project with the same id is re-created.
        await self._close_project_engine_store(project_id)

        projects_dir = self.context.project_dir(project_id)
        if projects_dir.is_dir():
            shutil.rmtree(str(projects_dir), ignore_errors=True)

        workplace = self.context.project_workplace(project_id)
        if workplace.is_dir():
            shutil.rmtree(str(workplace), ignore_errors=True)

        memory = getattr(self.context.engine, "memory", None)
        if memory:
            delete_fn = getattr(memory, "delete_project", None)
            if callable(delete_fn):
                try:
                    maybe = delete_fn(project_id)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                except Exception:
                    logger.opt(exception=True).debug(f"memory.delete_project failed for {project_id}")

        events = [ServiceEvent("project_deleted", {"project_id": project_id})]
        payload: dict[str, Any] = {"project_id": project_id, "deleted_channels": deleted_channels}
        if was_active:
            self.context.project_dir("default").mkdir(parents=True, exist_ok=True)
            await self.context.activate_project("default")
            await self.context.chat_store.ensure_activity_channel(project_id="default")
            await self.context.chat_store.ensure_secretary_channel(project_id="default")
            payload["active_project_id"] = "default"
            events.append(ServiceEvent("project_switched", {"project_id": "default"}))
            snapshot = await build_snapshot(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            events.append(ServiceEvent("snapshot", snapshot))
            collab = await build_collab_sync(
                self.context.engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
                exec_mode=self.context.mode_state.exec_mode,
            )
            events.append(ServiceEvent("collab_sync_push", collab))
        return ServiceResult(payload, events)

    async def switch(self, project_id: str, *, switch_seq: str = "", include_snapshot: bool = True) -> ServiceResult:
        new_id = str(project_id or "").strip()
        if not new_id:
            raise ServiceError("missing_project_id", "Missing project_id")
        if not self.context.is_safe_project_id(new_id):
            raise ServiceError("invalid_project_id", "Invalid project_id")
        async with self.context.project_switch_lock:
            if new_id == "default":
                self.context.project_dir(new_id).mkdir(parents=True, exist_ok=True)
                self.context.project_workplace(new_id).mkdir(parents=True, exist_ok=True)
            elif not self.context.project_dir(new_id).is_dir():
                raise ServiceError("project_not_found", f"Project '{new_id}' does not exist", {"project_id": new_id, "switch_seq": switch_seq})
            engine = await self.context.activate_project(new_id)
            await self.context.chat_store.ensure_activity_channel(project_id=new_id)
            await self.context.chat_store.ensure_secretary_channel(project_id=new_id)

        events = [ServiceEvent("project_switched", {"project_id": new_id, "switch_seq": switch_seq})]
        if include_snapshot:
            index_payload = await self.project_index(new_id, switch_seq=switch_seq, include_snapshot=True)
            for key in ("project_index", "snapshot"):
                if key in index_payload.payload:
                    event_type = "project_index_push" if key == "project_index" else "snapshot"
                    events.append(ServiceEvent(event_type, index_payload.payload[key]))
        return ServiceResult({"project_id": new_id, "switch_seq": switch_seq, "engine_project_id": getattr(engine, "project_id", new_id)}, events)

    async def project_index(
        self,
        project_id: str,
        *,
        switch_seq: str = "",
        view_generation: Any = None,
        include_snapshot: bool = False,
    ) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        index_payload = await build_project_index_sync(
            engine,
            self.context.agent_store,
            self.context.chat_store,
            self.context.event_adapter,
            exec_mode=self.context.mode_state.exec_mode,
        )
        index_payload["project_id"] = self.context.normalize_project_id(project_id)
        index_payload["switch_seq"] = switch_seq
        if view_generation is not None:
            index_payload["view_generation"] = view_generation
        payload: dict[str, Any] = {"project_index": index_payload}
        if include_snapshot:
            snapshot = await build_snapshot(
                engine,
                self.context.agent_store,
                self.context.chat_store,
                self.context.event_adapter,
            )
            snapshot["project_id"] = self.context.normalize_project_id(project_id)
            snapshot["exec_mode"] = self.context.mode_state.exec_mode
            snapshot["company_profile"] = self.context.mode_state.company_profile
            snapshot["task_preferred_agent"] = self.context.mode_state.task_preferred_agent
            snapshot["switch_seq"] = switch_seq
            if view_generation is not None:
                snapshot["view_generation"] = view_generation
            payload["snapshot"] = snapshot
        return ServiceResult(payload)
