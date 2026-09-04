"""Runtime and global execution-mode service."""

from __future__ import annotations

from typing import Any

from opc.plugins.office_ui.snapshot_builder import build_collab_sync, build_snapshot
from opc.layer3_agent.external_team_activity import reduce_external_team_events

from .context import OfficeServiceContext
from .models import ServiceError, ServiceEvent, ServiceResult
from .session import SessionService


class RuntimeService:
    def __init__(self, context: OfficeServiceContext, session_service: SessionService) -> None:
        self.context = context
        self.session_service = session_service

    async def mode_show(self) -> ServiceResult:
        active_org = ""
        if self.context.mode_state.exec_mode == "org" and self.context.get_active_saved_org_name is not None:
            active_org = await self.context.get_active_saved_org_name()
        return ServiceResult({
            "mode": self.context.mode_state.exec_mode,
            "profile": self.context.mode_state.company_profile,
            "org_id": active_org,
            "preferred_agent": self.context.mode_state.task_preferred_agent,
        })

    async def status(self, *, project_id: str, limit: int = 50) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        payload: dict[str, Any] = {
            "project_id": project_id,
            "mode": self.context.mode_state.exec_mode,
            "profile": self.context.mode_state.company_profile,
            "preferred_agent": self.context.mode_state.task_preferred_agent,
            "active_tasks": [],
            "runtime_sessions": [],
            "external_sessions": [],
            "checkpoints": [],
        }
        if not self.context.store_is_ready(store):
            payload["available"] = False
            payload["reason"] = "store_not_ready"
            return ServiceResult(payload)
        from opc.core.models import TaskStatus

        terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        tasks = await store.get_tasks(project_id=project_id) if hasattr(store, "get_tasks") else []
        payload["active_tasks"] = [
            {
                "task_id": getattr(task, "id", ""),
                "title": getattr(task, "title", ""),
                "status": getattr(getattr(task, "status", None), "value", getattr(task, "status", "")),
                "session_id": getattr(task, "session_id", ""),
                "assigned_to": getattr(task, "assigned_to", ""),
            }
            for task in tasks
            if getattr(task, "status", None) not in terminal
        ][:limit]
        if hasattr(store, "list_runtime_sessions"):
            payload["runtime_sessions"] = await store.list_runtime_sessions(project_id=project_id, limit=limit)
        if hasattr(store, "list_external_sessions"):
            payload["external_sessions"] = await store.list_external_sessions(project_id=project_id, limit=limit)
        if hasattr(store, "get_pending_checkpoints"):
            payload["checkpoints"] = await store.get_pending_checkpoints(project_id=project_id)
            payload["checkpoints"] = payload["checkpoints"][:limit]
        return ServiceResult(payload)

    async def mode_set(
        self,
        *,
        mode: str,
        profile: str = "corporate",
        preferred_agent: str | None = None,
        org_id: str | None = None,
        sync_config: bool = True,
    ) -> ServiceResult:
        new_mode = self.session_service.normalize_exec_mode(mode)
        normalized_org_id = self.session_service.normalize_org_id(org_id)
        if new_mode == "org":
            profile = "custom"
            if sync_config and normalized_org_id and self.context.load_active_org_config:
                if not self.context.load_active_org_config(normalized_org_id):
                    raise ServiceError("org_not_found", "org_not_found", {"org_id": normalized_org_id})
                if self.context.set_active_saved_org_name:
                    await self.context.set_active_saved_org_name(normalized_org_id)
        else:
            normalized_org_id = ""
            profile = self.session_service.normalize_company_profile(profile)
            if new_mode == "company" and profile == "custom":
                profile = "corporate"
        agent = self.session_service.normalize_preferred_agent(
            preferred_agent if preferred_agent is not None else self.context.mode_state.task_preferred_agent,
            default=self.context.mode_state.task_preferred_agent,
        )
        self.context.mode_state.exec_mode = new_mode
        self.context.mode_state.company_profile = profile
        self.context.mode_state.task_preferred_agent = agent
        if self.context.agent_store:
            await self.context.agent_store.set_server_state("exec_mode", new_mode)
            await self.context.agent_store.set_server_state("company_profile", profile)
            await self.context.agent_store.set_server_state("task_preferred_agent", agent)
        if getattr(self.context.engine, "org_engine", None) and self.context.agent_store:
            await self.context.agent_store.load_preset("custom" if new_mode == "org" else profile, self.context.engine.org_engine)
        snapshot = await build_snapshot(
            self.context.engine,
            self.context.agent_store,
            self.context.chat_store,
            self.context.event_adapter,
        )
        snapshot["exec_mode"] = new_mode
        snapshot["company_profile"] = profile
        snapshot["task_preferred_agent"] = agent
        collab = await build_collab_sync(
            self.context.engine,
            self.context.agent_store,
            self.context.chat_store,
            self.context.event_adapter,
            exec_mode=new_mode,
        )
        payload = {"mode": new_mode, "profile": profile, "org_id": normalized_org_id, "preferred_agent": agent}
        return ServiceResult(payload, [ServiceEvent("snapshot", snapshot), ServiceEvent("collab_sync_push", collab)])

    async def run_task(self, *, project_id: str, task_id: str) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        task = await engine.store.get_task(task_id) if getattr(engine, "store", None) else None
        if not task:
            raise ServiceError("task_not_found", "task_not_found", {"task_id": task_id})
        prompt = f"{getattr(task, 'title', '')}\n{getattr(task, 'description', '')}".strip()
        return await self.session_service.send(
            project_id=project_id,
            task_id=task_id,
            content=prompt,
        )

    async def checkpoints(self, *, project_id: str, limit: int = 50) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        checkpoints = await store.get_pending_checkpoints(project_id=project_id) if store and hasattr(store, "get_pending_checkpoints") else []
        return ServiceResult({"project_id": project_id, "checkpoints": checkpoints[-limit:]})

    async def logs(self, *, project_id: str, task_id: str, limit: int = 100) -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        task = await store.get_task(task_id) if store else None
        if (
            not task
            or str(getattr(task, "project_id", "default") or "default").strip()
            != str(project_id or "default").strip()
        ):
            raise ServiceError("task_not_found", "task_not_found", {"task_id": task_id})
        metadata = dict(getattr(task, "metadata", {}) or {})
        transcript = await store.get_session_transcript(task.session_id) if getattr(task, "session_id", None) else []
        runtime_sessions = []
        runtime_events: list[dict[str, Any]] = []
        if hasattr(store, "list_runtime_sessions"):
            runtime_sessions = await store.list_runtime_sessions(project_id=project_id, task_id=task_id, limit=limit)
        if runtime_sessions and hasattr(store, "list_runtime_events"):
            for session in runtime_sessions:
                runtime_id = str(session.get("runtime_session_id", "") or "")
                if runtime_id:
                    runtime_events.extend(await store.list_runtime_events(runtime_id, limit=limit))
        enriched_events = [self._runtime_event_payload(event) for event in runtime_events[-limit:]]
        return ServiceResult({
            "project_id": project_id,
            "task_id": task_id,
            "target": {
                "task_id": task_id,
                "session_id": str(getattr(task, "session_id", "") or ""),
                "title": str(getattr(task, "title", "") or ""),
                "status": str(getattr(getattr(task, "status", None), "value", getattr(task, "status", "")) or ""),
                "role_id": str(metadata.get("role_id") or getattr(task, "assigned_to", "") or ""),
                "agent_id": str(metadata.get("agent_id") or metadata.get("preferred_agent") or ""),
                "work_item_id": str(
                    metadata.get("work_item_id")
                    or metadata.get("linked_work_item_id")
                    or ""
                ),
            },
            "transcript": transcript[-limit:],
            "runtime_sessions": runtime_sessions,
            "runtime_events": enriched_events,
        })

    async def external_team_activity(
        self,
        *,
        project_id: str,
        task_id: str,
        external_invocation_id: str = "",
        limit: int = 100,
        before_created_at: str = "",
        before_event_id: str = "",
    ) -> ServiceResult:
        """Return read-only Jiuwen Team telemetry for one runtime turn."""

        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        task = await store.get_task(task_id) if store else None
        if (
            not task
            or str(getattr(task, "project_id", "default") or "default").strip()
            != str(project_id or "default").strip()
        ):
            raise ServiceError("task_not_found", "task_not_found", {"task_id": task_id})
        if not hasattr(store, "list_external_sessions"):
            return ServiceResult({
                "available": False,
                "reason": "telemetry_unavailable",
                "project_id": project_id,
                "task_id": task_id,
                "external_invocation_id": str(external_invocation_id or "").strip(),
            })

        sessions = await store.list_external_sessions(
            project_id=project_id,
            task_id=task_id,
            limit=100,
        )
        requested_invocation = str(external_invocation_id or "").strip()
        team_sessions: list[tuple[Any, dict[str, Any], str]] = []
        for candidate in sessions:
            metadata = dict(getattr(candidate, "metadata", {}) or {})
            is_team = (
                str(getattr(candidate, "agent_type", "") or "").strip() == "jiuwenswarm"
                and str(metadata.get("execution_unit_kind") or "").strip()
                == "opaque_external_team"
            )
            if not is_team:
                continue
            runtime_session_id = str(
                metadata.get("runtime_session_id")
                or getattr(candidate, "session_id", "")
                or ""
            ).strip()
            if runtime_session_id:
                team_sessions.append((candidate, metadata, runtime_session_id))

        invocation_rows: list[dict[str, Any]] = []
        invocation_owner: dict[str, tuple[Any, dict[str, Any], str]] = {}
        seen_runtime_sessions: set[str] = set()
        for candidate, metadata, runtime_session_id in team_sessions:
            if runtime_session_id in seen_runtime_sessions:
                continue
            seen_runtime_sessions.add(runtime_session_id)
            rows: list[dict[str, Any]] = []
            if hasattr(store, "list_external_team_invocations"):
                rows = await store.list_external_team_invocations(
                    runtime_session_id,
                    task_id,
                )
            if not rows:
                current_invocation = str(
                    metadata.get("external_invocation_id")
                    or dict(metadata.get("external_team_summary", {}) or {}).get(
                        "external_invocation_id"
                    )
                    or ""
                ).strip()
                if current_invocation:
                    rows = [{
                        "external_invocation_id": current_invocation,
                        "started_at": "",
                        "last_event_at": "",
                        "event_count": 0,
                        "member_count": int(
                            dict(metadata.get("external_team_summary", {}) or {})
                            .get("counts", {})
                            .get("members", 0)
                            or 0
                        ),
                        "task_count": int(
                            dict(metadata.get("external_team_summary", {}) or {})
                            .get("counts", {})
                            .get("tasks", 0)
                            or 0
                        ),
                        "message_count": 0,
                        "output_count": 0,
                    }]
            for row in rows:
                invocation_id = str(row.get("external_invocation_id") or "").strip()
                if not invocation_id or invocation_id in invocation_owner:
                    continue
                normalized = {
                    "external_invocation_id": invocation_id,
                    "started_at": str(row.get("started_at") or ""),
                    "last_event_at": str(row.get("last_event_at") or ""),
                    "event_count": int(row.get("event_count") or 0),
                    "member_count": int(row.get("member_count") or 0),
                    "task_count": int(row.get("task_count") or 0),
                    "message_count": int(row.get("message_count") or 0),
                    "output_count": int(row.get("output_count") or 0),
                }
                invocation_rows.append(normalized)
                invocation_owner[invocation_id] = (
                    candidate,
                    metadata,
                    runtime_session_id,
                )

        if not invocation_rows:
            return ServiceResult({
                "available": False,
                "reason": "no_team_telemetry",
                "project_id": project_id,
                "task_id": task_id,
                "external_invocation_id": requested_invocation,
            })

        latest_invocation = max(
            invocation_rows,
            key=lambda row: (
                str(row.get("last_event_at") or ""),
                str(row.get("external_invocation_id") or ""),
            ),
        )["external_invocation_id"]
        preferred_invocation = max(
            invocation_rows,
            key=lambda row: (
                int(row.get("member_count") or 0) > 0,
                int(row.get("task_count") or 0) > 0,
                int(row.get("member_count") or 0),
                int(row.get("task_count") or 0),
                int(row.get("message_count") or 0),
                str(row.get("last_event_at") or ""),
            ),
        )["external_invocation_id"]
        requested_invocation = requested_invocation or preferred_invocation
        owner = invocation_owner.get(requested_invocation)
        if owner is None:
            return ServiceResult({
                "available": False,
                "reason": "no_team_telemetry",
                "project_id": project_id,
                "task_id": task_id,
                "external_invocation_id": requested_invocation,
                "invocations": invocation_rows,
            })
        selected, metadata, runtime_session_id = owner
        for row in invocation_rows:
            row["is_preferred"] = (
                row["external_invocation_id"] == preferred_invocation
            )
            row["is_latest"] = row["external_invocation_id"] == latest_invocation

        page = {"events": [], "has_more": False, "next_cursor": None}
        if runtime_session_id and hasattr(store, "list_external_team_events"):
            page = await store.list_external_team_events(
                runtime_session_id,
                requested_invocation,
                limit=limit,
                before_created_at=before_created_at,
                before_event_id=before_event_id,
            )
        normalized_events = [
            dict(row.get("payload", {}) or {})
            for row in list(page.get("events", []) or [])
            if isinstance(row, dict) and isinstance(row.get("payload"), dict)
        ]
        projection_events: list[dict[str, Any]] = []
        if hasattr(store, "list_external_team_projection_events"):
            projection_rows = await store.list_external_team_projection_events(
                runtime_session_id,
                requested_invocation,
            )
            projection_events = [
                dict(row.get("payload", {}) or {})
                for row in projection_rows
                if isinstance(row, dict) and isinstance(row.get("payload"), dict)
            ]
            # Always include sparse, high-signal lifecycle events in the first
            # view even when output-heavy runs push them outside the latest
            # pagination window.  Exact invocation scope is preserved.
            highlights = {
                "runtime_ready", "team_completed", "team_error",
                "member_spawned", "member_restarted", "member_shutdown",
                "task_created", "task_claimed", "task_completed",
                "task_cancelled", "task_unblocked", "message_p2p",
                "message_broadcast",
            }
            by_event_id = {
                str(event.get("event_id") or ""): event
                for event in normalized_events
                if str(event.get("event_id") or "")
            }
            for event in projection_events:
                event_id = str(event.get("event_id") or "")
                if event_id and str(event.get("kind") or "") in highlights:
                    by_event_id[event_id] = event
            normalized_events = sorted(
                by_event_id.values(),
                key=lambda event: (
                    int(event.get("sequence", 0) or 0),
                    str(event.get("occurred_at") or ""),
                    str(event.get("event_id") or ""),
                ),
            )
        persisted_summary = metadata.get("external_team_summary")
        current_invocation = str(metadata.get("external_invocation_id") or "").strip()
        if projection_events:
            summary = reduce_external_team_events(projection_events)
        elif (
            isinstance(persisted_summary, dict)
            and current_invocation == requested_invocation
        ):
            summary = dict(persisted_summary)
        else:
            summary = reduce_external_team_events(normalized_events)
        return ServiceResult({
            "available": True,
            "project_id": project_id,
            "task_id": task_id,
            "execution_turn_id": task_id,
            "provider": "jiuwenswarm",
            "execution_unit_kind": "opaque_external_team",
            "external_invocation_id": requested_invocation,
            "provider_session_id": str(
                metadata.get("provider_session_id")
                or metadata.get("resume_session_id")
                or ""
            ).strip(),
            "invocations": invocation_rows,
            "summary": summary,
            "events": normalized_events,
            "has_more": bool(page.get("has_more", False)),
            "next_cursor": page.get("next_cursor"),
        })

    @staticmethod
    def _runtime_event_payload(event: Any) -> dict[str, Any]:
        if isinstance(event, dict):
            payload = dict(event)
        elif hasattr(event, "model_dump"):
            payload = dict(event.model_dump())
        else:
            payload = dict(getattr(event, "__dict__", {}) or {})
        event_type = str(payload.get("event_type") or payload.get("type") or "")
        raw_payload = payload.get("payload")
        if isinstance(raw_payload, dict):
            tool_name = str(raw_payload.get("tool_name") or raw_payload.get("name") or "")
            summary = str(raw_payload.get("summary") or raw_payload.get("result_summary") or raw_payload.get("text") or "")
        else:
            tool_name = ""
            summary = ""
        display_parts = [part for part in (event_type, tool_name, summary) if part]
        payload["display_text"] = " | ".join(display_parts)
        payload["event_type"] = event_type
        return payload
