"""Comms state service."""

from __future__ import annotations

from typing import Any

from opc.layer2_organization import comms as file_comms

from .context import OfficeServiceContext
from .models import ServiceError, ServiceResult


class CommsService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    async def state(self, *, project_id: str, task_id: str = "", session_id: str = "") -> ServiceResult:
        engine = await self.context.engine_for_project(project_id)
        store = getattr(engine, "store", None)
        request_project_id = self.context.normalize_project_id(project_id)
        if not self.context.store_is_ready(store):
            return ServiceResult({"available": False, "reason": "store_not_ready", "project_id": request_project_id})

        task = None
        task_id = str(task_id or "").strip()
        if task_id:
            try:
                task = await store.get_task(task_id)
            except Exception:
                task = None

        resolved_project_id = self.context.normalize_project_id(
            (getattr(task, "project_id", None) if task is not None else None) or request_project_id
        )
        session_id_hint = str(session_id or "").strip()
        if task is not None and not session_id_hint:
            session_id_hint = (
                str(getattr(task, "parent_session_id", "") or "").strip()
                or str(getattr(task, "session_id", "") or "").strip()
            )

        if task is None or not self._task_has_comms_workspace(task):
            try:
                tasks = await store.get_tasks(project_id=resolved_project_id)
            except Exception:
                tasks = []

            def _ts(candidate: Any) -> float:
                created_at = getattr(candidate, "created_at", None)
                if created_at is None:
                    return 0.0
                timestamp = getattr(created_at, "timestamp", None)
                if callable(timestamp):
                    try:
                        return float(timestamp())
                    except Exception:
                        return 0.0
                try:
                    return float(created_at)
                except (TypeError, ValueError):
                    return 0.0

            ranked = sorted(
                tasks,
                key=lambda candidate: (
                    0
                    if session_id_hint
                    and (
                        getattr(candidate, "parent_session_id", "") == session_id_hint
                        or getattr(candidate, "session_id", "") == session_id_hint
                    )
                    else 1,
                    -_ts(candidate),
                ),
            )
            for candidate in ranked:
                metadata = dict(getattr(candidate, "metadata", {}) or {})
                if (
                    str(metadata.get("comms_workspace_root") or "").strip()
                    or str(metadata.get("target_output_dir") or "").strip()
                ):
                    task = candidate
                    break

        if task is None:
            return ServiceResult({
                "available": False,
                "reason": "no_task_with_workspace",
                "project_id": resolved_project_id,
            })

        metadata = dict(getattr(task, "metadata", {}) or {})
        workspace_root = (
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
            or str(metadata.get("setup_workspace_prepared") or "").strip()
        )
        if not workspace_root:
            return ServiceResult({
                "available": False,
                "reason": "no_workspace_root",
                "project_id": resolved_project_id,
            })

        resolved_session_id = (
            str(getattr(task, "parent_session_id", "") or "").strip()
            or str(getattr(task, "session_id", "") or "").strip()
            or session_id_hint
            or "default"
        )
        try:
            layout = file_comms.resolve_layout(workspace_root, resolved_project_id, resolved_session_id)
        except Exception as exc:
            return ServiceResult({
                "available": False,
                "reason": f"layout_error: {exc}",
                "project_id": resolved_project_id,
            })

        base_payload = {
            "project_id": resolved_project_id,
            "task_id": str(getattr(task, "id", "") or ""),
            "session_id": resolved_session_id,
            "workspace_root": workspace_root,
            "output_root": str(metadata.get("output_root") or metadata.get("target_output_dir") or "").strip(),
            "comms_root": str(layout.root),
        }
        if not file_comms.layout_available(layout):
            return ServiceResult({
                "available": True,
                "empty": True,
                **base_payload,
                "projection_status": "empty",
                "recent_failures": list((getattr(task, "context_snapshot", {}) or {}).get("comms_failures", []) or [])[-5:],
                "roles": [],
                "meetings": [],
            })

        projection_status = "unknown"
        try:
            communication = getattr(engine, "communication", None)
            if communication and hasattr(communication, "rebuild_comms_projection"):
                await communication.rebuild_comms_projection(task=task, layout=layout)
                projection_status = "synced"
        except Exception as exc:
            projection_status = f"projection_error: {exc}"

        roles_payload: list[dict[str, Any]] = []
        try:
            role_ids = file_comms.list_roles(layout)
        except Exception:
            role_ids = []
        for role_id in role_ids:
            try:
                unread_headers = file_comms.list_unread(layout, role_id, limit=8)
            except Exception:
                unread_headers = []
            try:
                seen_count = file_comms.count_role_messages(
                    layout,
                    role_id,
                    bucket=file_comms.SEEN_DIRNAME,
                )
            except Exception:
                seen_count = 0
            try:
                outbox_count = file_comms.count_role_messages(
                    layout,
                    role_id,
                    bucket=file_comms.OUTBOX_DIRNAME,
                )
            except Exception:
                outbox_count = 0
            recent_seen: list[dict[str, Any]] = []
            recent_outbox: list[dict[str, Any]] = []
            try:
                recent_seen = [
                    self._header_payload(header, "seen")
                    for header in file_comms.list_role_messages(
                        layout,
                        role_id,
                        include_new=False,
                        include_seen=True,
                        include_outbox=False,
                        limit=12,
                    )
                ]
            except Exception:
                pass
            try:
                recent_outbox = [
                    self._header_payload(header, "sent")
                    for header in file_comms.list_role_messages(
                        layout,
                        role_id,
                        include_new=False,
                        include_seen=False,
                        include_outbox=True,
                        limit=12,
                    )
                ]
            except Exception:
                pass
            roles_payload.append({
                "role_id": role_id,
                "unread_count": len(unread_headers),
                "has_blocking": any(bool(getattr(header, "blocking", False)) for header in unread_headers),
                "seen_count": seen_count,
                "outbox_count": outbox_count,
                "recent_unread": [self._header_payload(header, "new") for header in unread_headers],
                "recent_seen": recent_seen,
                "recent_outbox": recent_outbox,
            })

        meetings_payload: list[dict[str, Any]] = []
        try:
            for state in file_comms.list_active_meetings(layout):
                meetings_payload.append({
                    "meeting_id": state.meeting_id,
                    "topic": state.topic,
                    "status": state.status,
                    "organizer": state.organizer,
                    "participants": list(state.participants),
                    "entry_count": state.entry_count,
                    "opened_at": state.opened_at,
                    "transcript_path": str(state.transcript_path),
                })
            for meeting_id in file_comms.list_meeting_ids(layout)[-10:]:
                state = file_comms.read_meeting_state(layout, meeting_id)
                if state is None or state.status != "closed":
                    continue
                meetings_payload.append({
                        "meeting_id": state.meeting_id,
                        "topic": state.topic,
                        "status": state.status,
                        "organizer": state.organizer,
                        "participants": list(state.participants),
                        "entry_count": state.entry_count,
                        "opened_at": state.opened_at,
                        "closed_at": state.closed_at,
                        "decision": state.decision,
                        "transcript_path": str(state.transcript_path),
                    })
        except Exception:
            pass

        recent_failures: list[dict[str, Any]] = []
        try:
            session_tasks = await store.get_tasks(project_id=resolved_project_id)
        except Exception:
            session_tasks = []
        for candidate in session_tasks:
            candidate_root = (
                str(getattr(candidate, "parent_session_id", "") or "").strip()
                or str(getattr(candidate, "session_id", "") or "").strip()
            )
            if candidate_root != resolved_session_id:
                continue
            for failure in list((getattr(candidate, "context_snapshot", {}) or {}).get("comms_failures", []) or [])[-3:]:
                if isinstance(failure, dict):
                    recent_failures.append(dict(failure))

        return ServiceResult({
            "available": True,
            **base_payload,
            "projection_status": projection_status,
            "recent_failures": recent_failures[-8:],
            "roles": roles_payload,
            "meetings": meetings_payload,
        })

    async def read(self, *, project_id: str, task_id: str = "", path: str) -> ServiceResult:
        if not str(path or "").strip():
            raise ServiceError("path_required", "path_required")
        request_project_id = self.context.normalize_project_id(project_id)
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            raise ServiceError("task_required", "task_required")

        engine = await self.context.engine_for_project(request_project_id)
        store = getattr(engine, "store", None)
        if not self.context.store_is_ready(store):
            raise ServiceError("store_not_ready", "store_not_ready")
        try:
            task = await store.get_task(clean_task_id)
        except Exception as exc:
            raise ServiceError("task_lookup_failed", "task_lookup_failed") from exc
        if task is None:
            raise ServiceError("task_not_found", "task_not_found")

        task_project_id = self.context.normalize_project_id(
            getattr(task, "project_id", None)
        )
        if task_project_id != request_project_id:
            raise ServiceError(
                "task_project_mismatch",
                "task_project_mismatch",
                {"task_id": clean_task_id},
            )
        metadata = dict(getattr(task, "metadata", {}) or {})
        workspace_root = (
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
        )
        session_id = (
            str(getattr(task, "parent_session_id", "") or "").strip()
            or str(getattr(task, "session_id", "") or "").strip()
        )
        if not workspace_root:
            raise ServiceError("no_workspace_root", "no_workspace_root")
        if not session_id:
            raise ServiceError("no_session_id", "no_session_id")
        try:
            layout = file_comms.resolve_layout(
                workspace_root,
                task_project_id,
                session_id,
            )
        except Exception as exc:
            raise ServiceError("layout_error", "layout_error") from exc
        try:
            candidate = file_comms.runtime_read_layout_path(layout, path)
        except Exception as exc:
            raise ServiceError(
                "path_outside_comms",
                "path_outside_comms",
                {"path": path},
            ) from exc
        if ".opc-comms" not in candidate.parts:
            raise ServiceError("path_outside_comms", "path_outside_comms", {"path": path})
        if not file_comms.runtime_file_exists(candidate):
            raise ServiceError("not_a_file", "not_a_file", {"path": path})
        try:
            header, body = file_comms.read_message(candidate)
        except Exception as exc:
            raise ServiceError("read_error", f"read_error: {exc}", {"path": path}) from exc
        return ServiceResult({
            "project_id": task_project_id,
            "task_id": clean_task_id,
            "path": str(candidate),
            "header": getattr(header, "raw_frontmatter", {}) if header else {},
            "message": self._header_payload(header),
            "body": body,
        })

    @staticmethod
    def _header_payload(header: Any, bucket: str = "") -> dict[str, Any]:
        if header is None:
            return {}
        payload = {
            "path": str(getattr(header, "path", "")),
            "message_id": getattr(header, "message_id", ""),
            "from": getattr(header, "from_role", ""),
            "to": getattr(header, "to_role", ""),
            "from_role": getattr(header, "from_role", ""),
            "to_role": getattr(header, "to_role", ""),
            "subject": getattr(header, "subject", ""),
            "sent_at": getattr(header, "sent_at", ""),
            "blocking": bool(getattr(header, "blocking", False)),
            "priority": getattr(header, "priority", "normal"),
            "tags": list(getattr(header, "tags", []) or []),
        }
        if bucket:
            payload["bucket"] = bucket
        return payload

    @staticmethod
    def _task_has_comms_workspace(task: Any | None) -> bool:
        if task is None:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        return bool(
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
            or str(metadata.get("setup_workspace_prepared") or "").strip()
            or str(metadata.get("comms_root") or "").strip()
        )
