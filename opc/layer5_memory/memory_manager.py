"""Memory hierarchy manager for global/project/session/employee-project scopes."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from opc.core.models import (
    AgentMemorySnapshotRecord,
    Phase,
    SessionLinkRecord,
    SessionMemorySnapshotRecord,
    SessionMessageRecord,
    SessionPartRecord,
    SessionRecord,
    TaskStatus,
)
from opc.layer5_memory.employee_evolution import EmployeeEvolutionManager
from opc.layer5_memory.markdown_memory import MarkdownMemoryStore
from opc.layer2_organization.work_item_identity import projection_id_for_task, work_item_identity_payload_for_task
from opc.layer4_tools.output_budget import clip_text


class MemoryManager:
    """Manages four memory scopes.

    - Global memory: durable cross-project memory in `memory/global.md`
    - Project memory: durable project memory in `memory/projects/<project_id>.md`
    - Session memory: persisted transcript + session memory snapshots
    - Employee-project memory: process/final employee memory in the store
    """

    def __init__(
        self,
        opc_home: Path,
        project_id: str | None = None,
        store: Any | None = None,
    ) -> None:
        self.opc_home = opc_home
        self.project_id = project_id
        self.store = store
        self.markdown_store = MarkdownMemoryStore(opc_home)
        self.employee_evolution = EmployeeEvolutionManager(opc_home)
        self.history_compactor: Any | None = None

        self.global_memory_dir = opc_home / "memory"
        self.global_memory_dir.mkdir(parents=True, exist_ok=True)

        self.project_memory_dir: Path | None = None
        if project_id:
            self.project_memory_dir = self.global_memory_dir / "projects"
            self.project_memory_dir.mkdir(parents=True, exist_ok=True)

    def set_project(self, project_id: str | None) -> None:
        self.project_id = project_id
        self.project_memory_dir = None
        if project_id:
            self.project_memory_dir = self.global_memory_dir / "projects"
            self.project_memory_dir.mkdir(parents=True, exist_ok=True)

    def set_history_compactor(self, compactor: Any | None) -> None:
        self.history_compactor = compactor

    async def maybe_compact_session_history(
        self,
        session_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Threshold-gated session-transcript compaction.

        Chat-style callers invoke this before building prompt context so a
        long transcript is folded into a summary snapshot instead of growing
        without bound. Best-effort: failures never block prompt building.
        """
        compactor = self.history_compactor
        maybe_compact = getattr(compactor, "maybe_compact_session", None) if compactor else None
        if not callable(maybe_compact) or not session_id:
            return False
        try:
            return bool(
                await maybe_compact(
                    project_id=self._resolve_project_id(project_id),
                    session_id=session_id,
                    force=False,
                )
            )
        except Exception as exc:
            logger.debug(f"Session history compaction skipped: {exc}")
            return False

    def _resolve_project_id(self, project_id: str | None = None) -> str:
        return str(project_id or self.project_id or "default")

    def _project_root(self, project_id: str | None) -> Path:
        return self.opc_home / "projects" / self._resolve_project_id(project_id)

    def _project_memory_path_for_id(self, project_id: str | None) -> Path:
        return self.markdown_store.memory_path(self._resolve_project_id(project_id))

    def _project_history_path_for_id(self, project_id: str | None) -> Path:
        return self._project_root(project_id) / "HISTORY.md"

    # --- Durable Markdown memory ---

    def _memory_path(self, project: bool = False) -> Path:
        project_id = self._resolve_project_id() if project and self.project_id else None
        return self.markdown_store.memory_path(project_id)

    def load_memory(self, project: bool = False) -> str:
        project_id = self._resolve_project_id() if project and self.project_id else None
        return self.markdown_store.load_visible_text(project_id)

    def save_memory(self, content: str, project: bool = False) -> None:
        project_id = self._resolve_project_id() if project and self.project_id else None
        self.markdown_store.save_visible_text(content, project_id)
        logger.debug(f"Memory saved: {self.markdown_store.memory_path(project_id)}")

    def append_memory(self, entry: str, project: bool = False) -> None:
        project_id = self._resolve_project_id() if project and self.project_id else None
        self.markdown_store.append_visible_entry(entry, project_id)

    def delete_project(self, project_id: str) -> None:
        self.markdown_store.delete_project(project_id)

    # --- Legacy HISTORY compatibility ---

    def _history_path(self, project: bool = False) -> Path:
        if project and self.project_memory_dir:
            return self.project_memory_dir / "HISTORY.md"
        return self.global_memory_dir / "HISTORY.md"

    def load_history(self, project: bool = False) -> str:
        _ = project
        return ""

    def append_history(self, task_summary: dict[str, Any], project: bool = False) -> None:
        _ = (task_summary, project)

    def append_autonomy_event(self, event: dict[str, Any], project: bool = False) -> None:
        _ = (event, project)

    def record_task_completion(self, task: Any, result_content: str, project: bool = False) -> None:
        _ = (task, result_content, project)

    async def record_task_completion_async(
        self,
        task: Any,
        result_content: str,
        project: bool = False,
        *,
        record_evolution: bool = True,
        record_reflections: bool = True,
    ) -> None:
        self.record_task_completion(task=task, result_content=result_content, project=project)
        is_company_mode = getattr(task, "metadata", {}).get("execution_mode") == "company_mode"
        legacy_company_evolution_enabled = bool(getattr(task, "metadata", {}).get("enable_legacy_employee_evolution", False))
        if (
            record_evolution
            and is_company_mode
            and legacy_company_evolution_enabled
            and getattr(task, "metadata", {}).get("employee_assignment")
        ):
            self.employee_evolution.record_work_item_completion(task=task, result_content=result_content)
        if not self.store:
            return
        project_id = getattr(task, "project_id", None) or (self.project_id or "default")
        role_id = getattr(task, "assigned_to", "") or getattr(task, "metadata", {}).get("work_item_role_id", "")
        projection_id = projection_id_for_task(task)
        decisions = list(getattr(task, "metadata", {}).get("decisions", []))
        risks = list(getattr(task, "metadata", {}).get("risks", []))
        artifacts = list(getattr(task, "metadata", {}).get("artifacts", []))
        open_questions = list(getattr(task, "metadata", {}).get("open_questions", []))
        if role_id:
            from opc.core.models import RoleMemoryRecord

            await self.store.record_role_memory(
                RoleMemoryRecord(
                    project_id=project_id,
                    role_id=role_id,
                    summary=f"{getattr(task, 'title', 'Task')}: {(result_content or '').strip()}",
                    details={
                        "task_id": getattr(task, "id", None),
                        "projection_id": projection_id,
                    },
                )
            )
        if decisions or risks or open_questions:
            from opc.core.models import WorkItemDecisionRecord

            await self.store.record_work_item_decision(
                WorkItemDecisionRecord(
                    project_id=project_id,
                    task_id=getattr(task, "id", None),
                    role_id=role_id,
                    projection_id=projection_id,
                    category="work_item_completion",
                    summary=(getattr(task, "metadata", {}).get("work_item_summary_for_downstream") or result_content or getattr(task, "title", "Task")),
                    details={
                        "decisions": decisions,
                        "risks": risks,
                        "open_questions": open_questions,
                    },
                )
            )
        if artifacts:
            from opc.core.models import ArtifactRecord

            for item in artifacts:
                text = str(item)
                location = text.split(": ", 1)[1] if ": " in text else text
                artifact_type = text.split(":", 1)[0] if ":" in text else "generic"
                await self.store.record_artifact(
                    ArtifactRecord(
                        project_id=project_id,
                        task_id=getattr(task, "id", None),
                        projection_id=projection_id,
                        role_id=role_id,
                        name=text,
                        artifact_type=artifact_type,
                        location=location,
                        details={"source": "task_metadata"},
                    )
                )
        if record_reflections and is_company_mode and legacy_company_evolution_enabled:
            await self._record_project_reflections_if_ready(task)

    async def record_company_feedback_outcomes(
        self,
        *,
        delivery_task: Any,
        work_item_tasks: list[Any],
        feedback: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> None:
        employee_outcomes = {
            str(item.get("employee_id", "")).strip(): dict(item)
            for item in list(evaluation.get("employees", []))
            if str(item.get("employee_id", "")).strip()
        }
        overall_outcome = str(evaluation.get("overall_outcome", "") or "partial_success").strip() or "partial_success"
        feedback_summary = str(evaluation.get("summary", "") or feedback.get("raw_feedback", "")).strip()

        for task in work_item_tasks:
            result_content = str(getattr(task, "result", {}).get("content", "") or "").strip()
            await self.record_task_completion_async(
                task=task,
                result_content=result_content,
                project=bool(getattr(task, "project_id", None) and getattr(task, "project_id", None) != "default"),
                record_evolution=False,
                record_reflections=False,
            )
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id:
                continue
            employee_feedback = employee_outcomes.get(employee_id, {})
            self.employee_evolution.record_work_item_completion(
                task=task,
                result_content=result_content,
                outcome=str(employee_feedback.get("outcome", overall_outcome) or overall_outcome),
                feedback_summary=feedback_summary,
                strengths=list(employee_feedback.get("strengths", [])),
                weaknesses=list(employee_feedback.get("weaknesses", [])),
                rationale=str(employee_feedback.get("reason", "")).strip(),
            )

        partial = overall_outcome != "success"
        await self._record_project_reflections_and_finalize(
            delivery_task,
            work_item_tasks,
            partial=partial,
            feedback=feedback,
            evaluation=evaluation,
        )


    async def _record_project_reflections_if_ready(self, task: Any) -> None:
        execution_task_ids = [
            str(item).strip()
            for item in list(getattr(task, "metadata", {}).get("execution_task_ids", []))
            if str(item).strip()
        ]
        if not execution_task_ids or not self.store:
            return

        project_id = getattr(task, "project_id", None) or (self.project_id or "default")
        tasks = await self.store.get_tasks(project_id=project_id)
        task_by_id = {item.id: item for item in tasks}
        task_by_id[getattr(task, "id", "")] = task
        work_item_tasks = [task_by_id[item_id] for item_id in execution_task_ids if item_id in task_by_id]
        if len(work_item_tasks) != len(execution_task_ids):
            return
        terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        if any(item.status not in terminal for item in work_item_tasks):
            return
        has_failures = any(item.status != TaskStatus.DONE for item in work_item_tasks)

        await self._record_project_reflections_and_finalize(
            task,
            work_item_tasks,
            partial=has_failures,
        )

    async def _record_project_reflections_and_finalize(
        self,
        delivery_task: Any,
        work_item_tasks: list[Any],
        *,
        partial: bool = False,
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> None:
        reflection_results = self.employee_evolution.record_project_reflections(
            delivery_task,
            work_item_tasks,
            partial=partial,
            feedback=feedback,
            evaluation=evaluation,
        )
        await self._finalize_employee_project_memories(
            delivery_task=delivery_task,
            work_item_tasks=work_item_tasks,
            reflection_results=reflection_results,
            feedback=feedback,
            evaluation=evaluation,
        )

    async def _finalize_employee_project_memories(
        self,
        *,
        delivery_task: Any,
        work_item_tasks: list[Any],
        reflection_results: list[dict[str, Any]],
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> None:
        if not self.store:
            return
        project_id = getattr(delivery_task, "project_id", None) or self._resolve_project_id()
        reflections_by_employee = {
            str(item.get("employee_id", "")).strip(): dict(item.get("reflection", {}) or {})
            for item in reflection_results
            if str(item.get("employee_id", "")).strip()
        }
        groups: dict[str, dict[str, Any]] = {}
        for task in work_item_tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            session_id = str(getattr(task, "session_id", "") or "").strip()
            if not employee_id:
                continue
            key = employee_id
            entry = groups.setdefault(
                key,
                {
                    "employee_id": employee_id,
                    "role_id": str(assignment.get("role_id") or getattr(task, "assigned_to", "") or "").strip(),
                    "session_ids": [],
                    "tasks": [],
                },
            )
            if not entry["role_id"]:
                entry["role_id"] = str(assignment.get("role_id") or getattr(task, "assigned_to", "") or "").strip()
            if session_id and session_id not in entry["session_ids"]:
                entry["session_ids"].append(session_id)
            entry["tasks"].append(task)

        for group in groups.values():
            employee_id = group["employee_id"]
            role_id = group["role_id"]
            reflection = reflections_by_employee.get(employee_id, {})
            existing_final = await self.store.get_agent_memory_snapshot(
                project_id=project_id,
                employee_id=employee_id,
                memory_kind="final",
                memory_scope="project",
            )
            if existing_final:
                for session_id in group["session_ids"]:
                    await self.store.delete_agent_memory_snapshots(
                        project_id=project_id,
                        session_id=session_id,
                        employee_id=employee_id,
                        memory_kind="process",
                        memory_scope="session",
                    )
                continue
            process_memory, process_snapshot = await self._build_project_process_memory(
                project_id=project_id,
                employee_id=employee_id,
                session_ids=list(group["session_ids"]),
            )
            if self.history_compactor:
                final_result = await self.history_compactor.finalize_agent_memory(
                    project_id=project_id,
                    session_id="",
                    employee_id=employee_id,
                    role_id=role_id,
                    process_memory=process_memory,
                    reflection_payload={
                        **reflection,
                        "feedback": feedback or {},
                        "evaluation": evaluation or {},
                    },
                )
            else:
                final_result = self._fallback_final_agent_memory(
                    process_memory=process_memory,
                    reflection_payload=reflection,
                )
            memory_text = str(final_result.get("memory_text", "")).strip()
            if not memory_text:
                continue
            summary_text = str(final_result.get("summary_text", "")).strip() or memory_text
            metadata = dict(final_result.get("metadata", {}) or {})
            metadata.update({
                "reflection": reflection,
                "task_ids": [str(getattr(task, "id", "")) for task in group["tasks"]],
                "session_ids": list(group["session_ids"]),
            })
            await self.store.save_agent_memory_snapshot(
                AgentMemorySnapshotRecord(
                    project_id=project_id,
                    session_id="",
                    employee_id=employee_id,
                    role_id=role_id,
                    memory_scope="project",
                    memory_kind="final",
                    summary_message_id=process_snapshot.summary_message_id if process_snapshot else "",
                    source_boundary_message_id=process_snapshot.source_boundary_message_id if process_snapshot else "",
                    summary_text=summary_text,
                    memory_text=memory_text,
                    metadata=metadata,
                )
            )
            for session_id in group["session_ids"]:
                await self.store.delete_agent_memory_snapshots(
                    project_id=project_id,
                    session_id=session_id,
                    employee_id=employee_id,
                    memory_kind="process",
                    memory_scope="session",
                )

    async def _build_project_process_memory(
        self,
        *,
        project_id: str,
        employee_id: str,
        session_ids: list[str],
    ) -> tuple[str, AgentMemorySnapshotRecord | None]:
        sections: list[str] = []
        latest_snapshot: AgentMemorySnapshotRecord | None = None
        for session_id in session_ids:
            snapshot = await self.store.get_agent_memory_snapshot(
                project_id=project_id,
                session_id=session_id,
                employee_id=employee_id,
                memory_kind="process",
                memory_scope="session",
            )
            if snapshot and snapshot.memory_text.strip():
                if latest_snapshot is None:
                    latest_snapshot = snapshot
                sections.append(f"## Session {session_id}\n{snapshot.memory_text.strip()}")
                continue
            history_tail = await self.build_employee_history_tail_messages(
                project_id=project_id,
                session_id=session_id,
                employee_id=employee_id,
            )
            draft = self._history_messages_to_process_memory(history_tail)
            if draft:
                sections.append(f"## Session {session_id}\n{draft}")
        return "\n\n".join(section for section in sections if section).strip(), latest_snapshot

    def _history_messages_to_process_memory(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        lines = ["## Process Memory Draft"]
        for item in messages:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            prefix = f"[{role}] " if role else ""
            lines.append(f"- {prefix}{content}")
        return "\n".join(lines).strip()

    def _fallback_final_agent_memory(
        self,
        *,
        process_memory: str,
        reflection_payload: dict[str, Any],
    ) -> dict[str, Any]:
        what_worked = [str(item).strip() for item in list(reflection_payload.get("what_worked", [])) if str(item).strip()]
        watchouts = [str(item).strip() for item in list(reflection_payload.get("mistakes_to_avoid", [])) if str(item).strip()]
        preferred_tools = [str(item).strip() for item in list(reflection_payload.get("tool_preferences", [])) if str(item).strip()]
        reviewer_preferences = [str(item).strip() for item in list(reflection_payload.get("reviewer_preferences", [])) if str(item).strip()]
        checklist = [str(item).strip() for item in list(reflection_payload.get("reusable_checklist", [])) if str(item).strip()]
        parts = [
            "## Effective Patterns",
            *([f"- {item}" for item in what_worked] or ["- (none)"]),
            "",
            "## Watchouts",
            *([f"- {item}" for item in watchouts] or ["- (none)"]),
            "",
            "## Preferred Tools",
            *([f"- {item}" for item in preferred_tools] or ["- (none)"]),
            "",
            "## Reviewer Preferences",
            *([f"- {item}" for item in reviewer_preferences] or ["- (none)"]),
            "",
            "## Reusable Checklist",
            *([f"- {item}" for item in checklist] or ["- (none)"]),
        ]
        summary = str(reflection_payload.get("project_summary", "")).strip() or process_memory.strip()
        return {
            "summary_text": summary,
            "memory_text": "\n".join(parts).strip(),
            "metadata": {
                "effective_patterns": what_worked,
                "watchouts": watchouts,
                "preferred_tools": preferred_tools,
                "reviewer_preferences": reviewer_preferences,
                "reusable_checklist": checklist,
            },
        }

    # --- Session memory and context building ---

    def _build_message_metadata(
        self,
        session: SessionRecord | None,
        *,
        metadata: dict[str, Any] | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        if session:
            merged["project_id"] = session.project_id
            merged["session_id"] = session.session_id
            if session.parent_session_id:
                merged["parent_session_id"] = session.parent_session_id
            session_metadata = dict(session.metadata or {})
            for key in (
                "employee_id",
                "role_id",
                "work_item_projection_id",
                "work_item_turn_type",
                "work_item_projection_id",
                "origin_session_id",
                "interface",
            ):
                value = session_metadata.get(key)
                if value not in (None, "", [], {}):
                    merged[key] = value
        if task_id:
            merged["task_id"] = task_id
        if agent_id and not merged.get("role_id"):
            merged["role_id"] = agent_id
        if metadata:
            merged.update(dict(metadata))
        if agent_id and not merged.get("role_id"):
            merged["role_id"] = agent_id
        return merged

    async def ensure_session(
        self,
        session_id: str,
        project_id: str | None = None,
        *,
        title: str = "",
        mode: str = "primary",
        parent_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionRecord | None:
        if not self.store:
            return None
        shared_role_session = bool((metadata or {}).get("shared_role_session", False))
        existing = await self.store.get_session(session_id)
        if existing:
            changed = False
            if title and not existing.title:
                existing.title = title
                changed = True
            if shared_role_session and existing.parent_session_id is not None:
                existing.parent_session_id = None
                changed = True
            if parent_session_id and not shared_role_session and existing.parent_session_id != parent_session_id:
                existing.parent_session_id = parent_session_id
                changed = True
            if metadata:
                existing.metadata = {**existing.metadata, **metadata}
                changed = True
            if changed:
                existing.updated_at = datetime.now()
                await self.store.save_session(existing)
            return existing
        record = SessionRecord(
            session_id=session_id,
            project_id=project_id or self.project_id or "default",
            parent_session_id=None if shared_role_session else parent_session_id,
            title=title,
            mode=mode,
            metadata=dict(metadata or {}),
        )
        await self.store.save_session(record)
        if parent_session_id and not shared_role_session:
            await self.store.save_session_link(
                SessionLinkRecord(
                    project_id=record.project_id,
                    session_id=parent_session_id,
                    linked_session_id=session_id,
                    link_type="child_session",
                    metadata={"mode": mode},
                )
            )
        return record

    async def append_session_message(
        self,
        session_id: str,
        role: str,
        *,
        text: str = "",
        part_type: str = "text",
        project_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        parent_message_id: str | None = None,
        summary_flag: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessageRecord | None:
        if not self.store:
            return None
        session = await self.ensure_session(
            session_id,
            project_id=project_id,
            mode="child" if role == "subagent" else "primary",
        )
        message_metadata = self._build_message_metadata(
            session,
            metadata=metadata,
            agent_id=agent_id,
            task_id=task_id,
        )
        message = SessionMessageRecord(
            session_id=session_id,
            role=role,
            task_id=task_id,
            agent_id=agent_id,
            parent_message_id=parent_message_id,
            summary_flag=summary_flag,
            metadata=message_metadata,
        )
        await self.store.save_session_message(message)
        if text or part_type != "text":
            payload = {"text": text} if part_type == "text" else {"text": text, **dict(metadata or {})}
            if part_type == "tool_output":
                payload = {
                    "tool_name": str((metadata or {}).get("tool_name", "tool")),
                    "output": text,
                    **dict(metadata or {}),
                }
            await self.store.save_session_part(
                SessionPartRecord(
                    message_id=message.message_id,
                    session_id=session_id,
                    part_type=part_type,
                    payload=payload,
                )
            )
        return message

    async def append_session_part(
        self,
        session_id: str,
        message_id: str,
        part_type: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.store:
            return
        await self.store.save_session_part(
            SessionPartRecord(
                message_id=message_id,
                session_id=session_id,
                part_type=part_type,
                payload=payload,
            )
        )

    async def record_user_turn(
        self,
        session_id: str,
        content: str,
        project_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessageRecord | None:
        return await self.append_session_message(
            session_id=session_id,
            role="user",
            text=content,
            project_id=project_id,
            metadata=metadata,
        )

    async def record_assistant_turn(
        self,
        session_id: str,
        content: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionMessageRecord | None:
        return await self.append_session_message(
            session_id=session_id,
            role="assistant",
            text=content,
            project_id=project_id,
            agent_id=agent_id,
            task_id=task_id,
            metadata=metadata,
        )

    async def record_child_session_result(
        self,
        parent_session_id: str,
        child_session_id: str,
        *,
        task: Any,
        result_content: str,
        artifacts: dict[str, Any] | None = None,
        result_delivery_id: str = "",
        source_result_message_id: str = "",
        canonical_turn_id: str = "",
    ) -> None:
        if not self.store:
            return
        summary = str(result_content or "").strip()
        assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
        msg = await self.append_session_message(
            session_id=parent_session_id,
            role="assistant",
            text="",
            project_id=getattr(task, "project_id", None) or self.project_id or "default",
            agent_id=getattr(task, "assigned_to", "") or None,
            task_id=getattr(task, "id", None),
            metadata={
                "kind": "child_result",
                "child_session_id": child_session_id,
                "source_task_id": str(getattr(task, "id", "") or ""),
                "task_title": getattr(task, "title", ""),
                "employee_id": str(assignment.get("employee_id", "")).strip(),
                "role_id": str(assignment.get("role_id") or getattr(task, "assigned_to", "") or "").strip(),
                **({"result_delivery_id": str(result_delivery_id).strip()} if str(result_delivery_id).strip() else {}),
                **({"source_result_message_id": str(source_result_message_id).strip()} if str(source_result_message_id).strip() else {}),
                **({"canonical_turn_id": str(canonical_turn_id).strip()} if str(canonical_turn_id).strip() else {}),
                **work_item_identity_payload_for_task(task),
            },
        )
        if not msg:
            return
        await self.append_session_part(
            parent_session_id,
            msg.message_id,
            "subtask_result",
            {
                "child_session_id": child_session_id,
                "task_id": getattr(task, "id", None),
                "task_title": getattr(task, "title", ""),
                "agent_id": getattr(task, "assigned_to", ""),
                "summary": summary,
                "artifacts": self._compact_artifacts(artifacts or {}),
                **({"result_delivery_id": str(result_delivery_id).strip()} if str(result_delivery_id).strip() else {}),
                **({"source_result_message_id": str(source_result_message_id).strip()} if str(source_result_message_id).strip() else {}),
            },
        )

    async def update_session_title(self, session_id: str, title: str) -> None:
        """Update the title of an existing session (unconditional overwrite)."""
        if not self.store:
            return
        session = await self.store.get_session(session_id)
        if not session:
            return
        session.title = title
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    async def update_session_summary(self, session_id: str, summary: str) -> None:
        if not self.store:
            return
        session = await self.store.get_session(session_id)
        if not session:
            return
        session.summary = summary
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    async def record_runtime_heartbeat_summary(
        self,
        *,
        session_id: str,
        role_id: str,
        worker_kind: str,
        summary: str,
        project_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.store:
            return {}
        text = str(summary or "").strip()
        if not text:
            return {}
        session = await self.store.get_session(session_id)
        if not session:
            return {}
        heartbeat_entries = list((session.metadata or {}).get("runtime_heartbeat_summaries", []) or [])
        heartbeat_entries.append(
            {
                "role_id": role_id,
                "worker_kind": worker_kind,
                "summary": text,
                "metadata": dict(metadata or {}),
                "recorded_at": datetime.now().isoformat(),
            }
        )
        heartbeat_entries = heartbeat_entries[-24:]
        await self._update_session_metadata(
            session_id,
            {
                "runtime_heartbeat_summaries": heartbeat_entries,
                "runtime_heartbeat_updated_at": datetime.now().isoformat(),
            },
        )
        rollup = await self.build_runtime_heartbeat_context(session_id)
        if rollup:
            await self.update_session_summary(session_id, rollup.replace("## Runtime Heartbeats\n", "").strip())
        return {
            "updated": True,
            "entry_count": len(heartbeat_entries),
            "summary_preview": text[:240],
        }

    async def build_global_memory_context(self) -> str:
        global_mem = self.load_memory(project=False).strip()
        if not global_mem:
            return ""
        return f"## Global Memory\n{global_mem}"

    def load_project_memory_markdown(self, project_id: str | None = None) -> str:
        pid = self._resolve_project_id(project_id) if (project_id or self.project_id) else None
        return self.markdown_store.load_visible_text(pid)

    async def build_project_memory_context(
        self,
        project_id: str | None = None,
        *,
        include_project_knowledge: bool = False,
    ) -> str:
        pid = self._resolve_project_id(project_id) if (project_id or self.project_id) else ""
        if not pid or pid == "default":
            return ""
        parts: list[str] = []
        project_mem = self.load_project_memory_markdown(pid).strip()
        if project_mem:
            parts.append(f"## Project Memory ({pid})\n{project_mem}")
        if include_project_knowledge:
            explicit_knowledge = self.employee_evolution.preferences.render_project_knowledge_context(pid)
            if explicit_knowledge:
                parts.append(f"## Project Knowledge ({pid})\n{explicit_knowledge}")
        return "\n\n".join(part for part in parts if part)

    async def build_project_dossier(
        self,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        pid = self._resolve_project_id(project_id) if (project_id or self.project_id) else "default"
        project_memory = self.load_project_memory_markdown(pid).strip() if pid and pid != "default" else ""
        if not self.store:
            return {
                "project_id": pid,
                "latest_deliverable_summary": "",
                "architecture_decisions": [],
                "completed_work_items": [],
                "open_issues": [],
                "verification_summary": "",
                "artifact_index": [],
                "work_item_summaries_for_downstream": [],
                "last_failure_summary": "",
                "project_memory_excerpt": clip_text(
                    project_memory,
                    limit=2000,
                    marker="project memory excerpt truncated",
                ).text,
            }

        latest_run = None
        if run_id and hasattr(self.store, "get_delegation_run"):
            latest_run = await self.store.get_delegation_run(run_id)
        elif hasattr(self.store, "get_latest_delegation_run"):
            latest_run = await self.store.get_latest_delegation_run(pid)

        effective_session_id = session_id or (getattr(latest_run, "session_id", "") or None)
        session_memory = await self.build_session_memory_context(effective_session_id) if effective_session_id else ""
        decisions = await self.store.get_work_item_decisions(pid, limit=limit) if hasattr(self.store, "get_work_item_decisions") else []
        artifacts = await self.store.get_artifacts(pid, limit=limit) if hasattr(self.store, "get_artifacts") else []
        handoffs = await self.store.get_handoff_records(pid, limit=limit) if hasattr(self.store, "get_handoff_records") else []
        work_items = (
            await self.store.list_delegation_work_items(latest_run.run_id)
            if latest_run is not None and hasattr(self.store, "list_delegation_work_items")
            else []
        )

        completed_work_items = [
            {
                "work_item_id": item.work_item_id,
                "title": item.title,
                "role_id": item.role_id,
                "seat_id": item.seat_id,
                "deliverable_summary": item.deliverable_summary or item.summary,
            }
            for item in work_items
            if item.phase == Phase.APPROVED
        ][:limit]

        open_issues: list[str] = []
        for item in work_items:
            if item.phase in {Phase.FAILED, Phase.NEEDS_ATTENTION, Phase.WAITING_DEPENDENCIES, Phase.WAITING_FOR_PEER, Phase.WAITING_FOR_CHILDREN, Phase.PAUSED}:
                issue = str(item.blocked_reason or item.summary or item.title or "").strip()
                if issue:
                    open_issues.append(issue)
        for decision in decisions:
            for question in list((decision.details or {}).get("open_questions", []) or []):
                text = str(question or "").strip()
                if text:
                    open_issues.append(text)
        deduped_open_issues = list(dict.fromkeys(open_issues))[:limit]

        verification_parts = [
            str(getattr(latest_run, "latest_deliverable_summary", "") or "").strip(),
            session_memory.replace("## Session Memory", "").strip(),
        ]
        verification_summary = next((part for part in verification_parts if part), "")

        last_failure_summary = ""
        for item in reversed(work_items):
            if item.phase == Phase.FAILED:
                last_failure_summary = str(item.blocked_reason or item.summary or item.title or "").strip()
                if last_failure_summary:
                    break

        return {
            "project_id": pid,
            "run_id": getattr(latest_run, "run_id", "") if latest_run is not None else (run_id or ""),
            "latest_deliverable_summary": str(getattr(latest_run, "latest_deliverable_summary", "") or "").strip(),
            "architecture_decisions": [
                {
                    "decision_id": item.decision_id,
                    "role_id": item.role_id,
                    "projection_id": item.projection_id,
                    "summary": item.summary,
                    "created_at": item.created_at.isoformat(),
                }
                for item in decisions[:limit]
            ],
            "completed_work_items": completed_work_items,
            "open_issues": deduped_open_issues,
            "verification_summary": verification_summary[:1500],
            "artifact_index": [
                {
                    "artifact_id": item.artifact_id,
                    "name": item.name,
                    "artifact_type": item.artifact_type,
                    "location": item.location,
                    "status": item.status,
                }
                for item in artifacts[:limit]
            ],
            "work_item_summaries_for_downstream": [
                {
                    "handoff_id": item.handoff_id,
                    "from_role": item.from_role,
                    "to_role": item.to_role,
                    "summary": item.summary,
                    "status": item.status,
                }
                for item in handoffs[:limit]
            ],
            "last_failure_summary": last_failure_summary,
            "project_memory_excerpt": clip_text(
                project_memory,
                limit=2000,
                marker="project memory excerpt truncated",
            ).text,
            "session_memory_excerpt": clip_text(
                session_memory,
                limit=1200,
                marker="session memory excerpt truncated",
            ).text,
        }

    async def build_memory_context(
        self,
        project_id: str | None = None,
        session_id: str | None = None,
        *,
        include_project_knowledge: bool = False,
    ) -> str:
        parts: list[str] = []
        global_ctx = await self.build_global_memory_context()
        if global_ctx:
            parts.append(global_ctx)
        project_ctx = await self.build_project_memory_context(
            project_id=project_id,
            include_project_knowledge=include_project_knowledge,
        )
        if project_ctx:
            parts.append(project_ctx)
        if session_id:
            session_ctx = await self.build_session_memory_context(session_id)
            if session_ctx:
                parts.append(session_ctx)
        return "\n\n".join(part for part in parts if part)

    async def build_focused_memory_context(
        self,
        *,
        query: str,
        project_id: str | None = None,
        session_id: str | None = None,
        include_project_knowledge: bool = False,
        max_chars: int = 2_400,
    ) -> str:
        pid = self._resolve_project_id(project_id) if (project_id or self.project_id) else ""
        normalized_query = " ".join(str(query or "").split()).strip()
        parts: list[str] = []

        global_sections = self._select_relevant_markdown_sections(
            self.load_memory(project=False),
            query=normalized_query,
            max_sections=2,
            max_chars=max_chars // 2,
        )
        if global_sections:
            parts.append(self._render_selected_sections("Focused Global Memory", global_sections))

        project_markdown = self.load_project_memory_markdown(pid) if pid and pid != "default" else ""
        project_sections = self._select_relevant_markdown_sections(
            project_markdown,
            query=normalized_query,
            max_sections=3,
            max_chars=max_chars,
        )
        if project_sections:
            parts.append(self._render_selected_sections(f"Focused Project Memory ({pid})", project_sections))

        if include_project_knowledge and pid and pid != "default":
            explicit_knowledge = self.employee_evolution.preferences.render_project_knowledge_context(pid)
            if explicit_knowledge:
                trimmed = explicit_knowledge.strip()
                if len(trimmed) > max_chars // 2:
                    trimmed = trimmed[: max_chars // 2].rstrip() + "\n[project knowledge truncated]"
                parts.append(f"## Project Knowledge ({pid})\n{trimmed}")

        if session_id:
            session_ctx = await self.build_session_memory_context(session_id)
            if session_ctx:
                parts.append(session_ctx)

        return "\n\n".join(part for part in parts if part)

    async def build_project_knowledge_context(self, project_id: str | None = None) -> str:
        parts: list[str] = []
        global_ctx = await self.build_global_memory_context()
        if global_ctx:
            parts.append(global_ctx)
        project_ctx = await self.build_project_memory_context(
            project_id=project_id,
            include_project_knowledge=True,
        )
        if project_ctx:
            parts.append(project_ctx)
        return "\n\n".join(parts)

    async def extract_durable_memories(
        self,
        *,
        session_id: str,
        project_id: str | None,
        query: str,
        assistant_response: str,
        llm: Any | None = None,
        min_messages: int = 4,
        max_input_chars: int = 12_000,
    ) -> dict[str, Any]:
        _ = (session_id, project_id, query, assistant_response, llm, min_messages, max_input_chars)
        return {}

    def _slice_transcript_from_boundary(
        self,
        transcript: list[dict[str, Any]],
        boundary_message_id: str,
    ) -> list[dict[str, Any]]:
        if not boundary_message_id:
            return transcript
        for idx, item in enumerate(transcript):
            if item["message"].message_id == boundary_message_id:
                return transcript[idx + 1:]
        return transcript

    def _slice_transcript_from_message_id(
        self,
        transcript: list[dict[str, Any]],
        message_id: str,
    ) -> list[dict[str, Any]]:
        if not message_id:
            return transcript
        for idx, item in enumerate(transcript):
            if item["message"].message_id == message_id:
                return transcript[idx + 1:]
        return transcript

    async def build_session_memory_context(self, session_id: str) -> str:
        if not self.store:
            return ""
        snapshot = await self.store.get_latest_session_memory_snapshot(session_id)
        if snapshot and snapshot.memory_text.strip():
            return f"## Session Memory\n{snapshot.memory_text.strip()}"
        session = await self.store.get_session(session_id)
        if session and session.summary.strip():
            return f"## Session Memory\n{session.summary.strip()}"
        return ""

    async def build_runtime_heartbeat_context(self, session_id: str) -> str:
        if not self.store:
            return ""
        session = await self.store.get_session(session_id)
        if not session:
            return ""
        entries = list((session.metadata or {}).get("runtime_heartbeat_summaries", []) or [])
        if not entries:
            return ""
        latest_by_role: dict[str, dict[str, Any]] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            role_id = str(item.get("role_id", "") or "").strip()
            if not role_id:
                continue
            latest_by_role[role_id] = item
        lines = ["## Runtime Heartbeats"]
        for role_id in sorted(latest_by_role):
            item = latest_by_role[role_id]
            worker_kind = str(item.get("worker_kind", "") or "").strip()
            label = f"{role_id} ({worker_kind})" if worker_kind else role_id
            lines.append(f"- {label}: {str(item.get('summary', '') or '').strip()}")
        return "\n".join(lines)

    async def update_runtime_session_memory(
        self,
        *,
        session_id: str,
        project_id: str | None,
        llm: Any | None,
        messages: list[dict[str, Any]],
        update_interval_messages: int = 4,
        max_input_chars: int = 6_000,
    ) -> dict[str, Any]:
        if not self.store or llm is None:
            return {}
        session = await self.store.get_session(session_id)
        if not session:
            return {}
        visible = await self._get_visible_session_transcript(session_id)
        if not visible:
            return {}
        current_message_count = len([item for item in visible if not getattr(item["message"], "summary_flag", False)])
        previous_count = int((session.metadata or {}).get("runtime_session_memory_message_count", 0) or 0)
        if current_message_count - previous_count < max(1, int(update_interval_messages or 1)):
            return {}
        rendered = [
            self._render_session_message(item["message"], item["parts"])
            for item in visible
            if not getattr(item["message"], "summary_flag", False)
        ]
        transcript_text = "\n\n".join(item for item in rendered if item).strip()
        if not transcript_text:
            return {}
        if len(transcript_text) > max_input_chars:
            transcript_text = transcript_text[-max_input_chars:]
        raw = await llm.simple_chat(
            prompt=json.dumps(
                {
                    "project_id": self._resolve_project_id(project_id),
                    "session_id": session_id,
                    "conversation_excerpt": transcript_text,
                },
                ensure_ascii=False,
            ),
            system=(
                "You maintain a rolling session memory for a coding agent.\n"
                "Return strict JSON with keys `summary_text` and `memory_text`.\n"
                "`memory_text` should be concise markdown with sections `## Primary Goal`, "
                "`## Current State`, `## Active Constraints`, and `## Open Risks` when applicable.\n"
                "Keep it durable for the next few turns; do not include verbose logs."
            ),
            task_type="quick_tasks",
        )
        parsed = self._parse_json_object(raw)
        memory_text = str(parsed.get("memory_text", "") or parsed.get("summary_text", "")).strip()
        if not memory_text:
            return {}
        latest_message_id = str(visible[-1]["message"].message_id)
        await self.store.save_session_memory_snapshot(
            SessionMemorySnapshotRecord(
                project_id=self._resolve_project_id(project_id),
                session_id=session_id,
                summary_message_id=latest_message_id,
                source_boundary_message_id=latest_message_id,
                summary_text=str(parsed.get("summary_text", "") or memory_text).strip(),
                memory_text=memory_text,
                metadata={"source": "runtime_background_session_memory"},
            )
        )
        await self.update_session_summary(session_id, memory_text)
        await self._update_session_metadata(
            session_id,
            {
                "runtime_session_memory_message_count": current_message_count,
                "runtime_session_memory_updated_at": datetime.now().isoformat(),
            },
        )
        return {
            "updated": True,
            "message_count": current_message_count,
            "summary_preview": memory_text[:240],
        }

    async def record_verification_feedback(
        self,
        *,
        task: Any,
        verdict: str,
        content: str,
    ) -> dict[str, Any]:
        _ = (task, verdict, content)
        return {}

    async def _update_session_metadata(self, session_id: str, updates: dict[str, Any]) -> None:
        if not self.store:
            return
        session = await self.store.get_session(session_id)
        if not session:
            return
        session.metadata = {**dict(session.metadata or {}), **dict(updates or {})}
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    @staticmethod
    def _memory_keywords(text: str) -> set[str]:
        stopwords = {
            "the", "and", "for", "with", "that", "this", "from", "into", "your",
            "about", "after", "before", "while", "when", "where", "have", "has",
            "using", "use", "used", "task", "work", "project", "session", "agent",
            "should", "would", "could", "then", "than", "them", "they", "their",
            "need", "needs", "also", "only", "over", "under", "more",
        }
        tokens = set(re.findall(r"[a-zA-Z0-9_]{3,}", str(text or "").lower()))
        return {token for token in tokens if token not in stopwords}

    def _split_markdown_sections(self, markdown: str) -> list[dict[str, str]]:
        text = str(markdown or "").strip()
        if not text:
            return []
        sections: list[dict[str, str]] = []
        heading = ""
        body_lines: list[str] = []
        for line in text.splitlines():
            match = re.match(r"^\s{0,3}(#{2,4})\s+(.*\S)\s*$", line)
            if match:
                body = "\n".join(body_lines).strip()
                if heading or body:
                    sections.append({"heading": heading, "body": body})
                heading = match.group(2).strip()
                body_lines = []
                continue
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if heading or body:
            sections.append({"heading": heading, "body": body})
        if not sections:
            return [{"heading": "", "body": text}]
        return sections

    def _select_relevant_markdown_sections(
        self,
        markdown: str,
        *,
        query: str,
        max_sections: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        sections = self._split_markdown_sections(markdown)
        if not sections:
            return []
        query_tokens = self._memory_keywords(query)
        scored: list[tuple[float, int, dict[str, str]]] = []
        for index, section in enumerate(sections):
            heading = section.get("heading", "")
            body = section.get("body", "")
            combined = f"{heading}\n{body}".strip()
            if not combined:
                continue
            heading_tokens = self._memory_keywords(heading)
            body_tokens = self._memory_keywords(body)
            overlap_heading = len(query_tokens & heading_tokens)
            overlap_body = len(query_tokens & body_tokens)
            score = overlap_heading * 3 + overlap_body
            lowered = combined.lower()
            if any(token in lowered for token in ("checklist", "warning", "watchout", "gotcha", "risk")):
                score += 0.5
            if not query_tokens and index == 0:
                score += 0.5
            if score <= 0:
                continue
            scored.append((score, -index, section))
        if not scored:
            return []
        scored.sort(reverse=True)
        selected: list[dict[str, str]] = []
        used_chars = 0
        for _, _, section in scored:
            heading = str(section.get("heading", "")).strip()
            body = str(section.get("body", "")).strip()
            snippet = body
            projected = used_chars + len(heading) + len(snippet)
            if projected > max_chars:
                remaining = max_chars - used_chars - len(heading) - 32
                if remaining <= 80:
                    continue
                snippet = snippet[:remaining].rstrip() + "\n[memory section truncated]"
            selected.append({"heading": heading, "body": snippet})
            used_chars += len(heading) + len(snippet)
            if len(selected) >= max_sections or used_chars >= max_chars:
                break
        return selected

    def _render_selected_sections(self, title: str, sections: list[dict[str, str]]) -> str:
        lines = [f"## {title}"]
        for section in sections:
            heading = str(section.get("heading", "")).strip()
            body = str(section.get("body", "")).strip()
            if heading:
                lines.append(f"### {heading}")
            if body:
                lines.append(body)
        return "\n".join(lines).strip()

    def _parse_json_object(self, raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            parts = text.split("\n", 1)
            text = parts[1] if len(parts) == 2 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                    return data if isinstance(data, dict) else {}
                except Exception:
                    return {}
        return {}

    def _normalize_memory_entries(self, values: Any) -> list[str]:
        entries: list[str] = []
        if not isinstance(values, list):
            return entries
        for item in values:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    entries.append(text)
                continue
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "") or "").strip()
            content = str(item.get("content", "") or "").strip()
            if not content:
                continue
            entry = f"### {title}\n{content}" if title else content
            entries.append(entry.strip())
        return entries

    def _append_unique_memory_entry(self, entry: str, *, project_id: str | None) -> bool:
        normalized_entry = " ".join(str(entry or "").split()).strip().lower()
        if not normalized_entry:
            return False
        existing = self.markdown_store.load_visible_text(project_id)
        if normalized_entry in " ".join(existing.split()).strip().lower():
            return False
        self.markdown_store.append_visible_entry(entry, project_id)
        return True

    async def _get_visible_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
        if not self.store:
            return []
        transcript = await self.store.get_session_transcript(session_id)
        if not transcript:
            return []
        compaction = await self.store.get_latest_session_compaction(session_id)
        boundary_message_id = compaction.source_boundary_message_id if compaction else ""
        return self._slice_transcript_from_boundary(transcript, boundary_message_id)

    def _render_session_parts(self, parts: list[SessionPartRecord]) -> str:
        rendered_parts: list[str] = []
        for part in parts:
            payload = dict(part.payload)
            if part.part_type == "text":
                text = str(payload.get("text", "")).strip()
                if text:
                    rendered_parts.append(text)
                continue
            if part.part_type == "subtask_result":
                title = payload.get("task_title") or payload.get("child_session_id") or "child task"
                summary = str(payload.get("summary", "")).strip()
                artifacts = payload.get("artifacts") or {}
                lines = [f"Child session result: {title}"]
                if summary:
                    lines.append(summary)
                artifact_lines = self._format_artifact_lines(artifacts)
                if artifact_lines:
                    lines.append("Artifacts:")
                    lines.extend(f"- {line}" for line in artifact_lines)
                rendered_parts.append("\n".join(lines))
                continue
            if part.part_type == "task_result":
                title = payload.get("task_title") or payload.get("task_id") or "task"
                outcome = str(payload.get("summary", "")).strip()
                rendered_parts.append(f"Task result: {title}\n{outcome}".strip())
                continue
            if part.part_type == "tool_output":
                name = payload.get("tool_name", "tool")
                output = str(payload.get("output", "")).strip()
                rendered_parts.append(f"Tool output [{name}]\n{output}".strip())
                continue
            if part.part_type == "tool_result":
                name = payload.get("tool_name", "tool")
                output = payload.get("result", {})
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False, default=str)
                rendered_parts.append(f"Tool result [{name}]\n{str(output).strip()}".strip())
                continue
            if part.part_type == "tool_call":
                name = payload.get("tool_name", "tool")
                arguments = payload.get("arguments", {})
                rendered_parts.append(
                    f"Tool call [{name}]\n{json.dumps(arguments, ensure_ascii=False, default=str)}".strip()
                )
                continue
            text = str(payload.get("text", "")).strip()
            if text:
                rendered_parts.append(text)
        return "\n\n".join(rendered_parts).strip()

    def _filter_prompt_history_items(
        self,
        visible_items: list[dict[str, Any]],
        *,
        include_latest_user_turn: bool,
    ) -> list[dict[str, Any]]:
        filtered = [
            item
            for item in list(visible_items)
            if not self._is_child_session_seed_item(item)
        ]
        if include_latest_user_turn:
            return filtered
        for idx in range(len(filtered) - 1, -1, -1):
            item = filtered[idx]
            message = item.get("message")
            if getattr(message, "summary_flag", False):
                continue
            role = str(getattr(message, "role", "") or "").strip().lower()
            if role == "user":
                return [*filtered[:idx], *filtered[idx + 1 :]]
            break
        return filtered

    def _is_child_session_seed_item(self, item: dict[str, Any]) -> bool:
        message = item.get("message")
        if message is None or getattr(message, "summary_flag", False):
            return False
        if str(getattr(message, "role", "") or "").strip().lower() != "user":
            return False
        metadata = dict(getattr(message, "metadata", {}) or {})
        return str(metadata.get("kind", "") or "").strip().lower() == "child_session_seed"

    async def build_session_history_messages(
        self,
        session_id: str,
        *,
        include_latest_user_turn: bool = True,
    ) -> list[dict[str, Any]]:
        visible_items = await self._get_visible_session_transcript(session_id)
        visible_items = self._filter_prompt_history_items(
            visible_items,
            include_latest_user_turn=include_latest_user_turn,
        )
        messages: list[dict[str, Any]] = []
        for item in visible_items:
            message = item["message"]
            content = self._render_session_parts(item["parts"])
            if not content:
                continue
            role = "user" if message.role == "user" else "assistant"
            messages.append({"role": role, "content": content})
        return messages

    async def build_session_history_tail_messages(
        self,
        session_id: str,
        *,
        include_latest_user_turn: bool = True,
    ) -> list[dict[str, Any]]:
        return await self.build_session_history_messages(
            session_id,
            include_latest_user_turn=include_latest_user_turn,
        )

    async def build_session_prompt_context(
        self,
        session_id: str,
        *,
        include_latest_user_turn: bool = True,
    ) -> str:
        visible_items = await self._get_visible_session_transcript(session_id)
        visible_items = self._filter_prompt_history_items(
            visible_items,
            include_latest_user_turn=include_latest_user_turn,
        )
        session_memory = await self.build_session_memory_context(session_id)
        blocks: list[str] = []
        for item in visible_items:
            rendered = self._render_session_message(item["message"], item["parts"])
            if rendered:
                blocks.append(rendered)
        parts: list[str] = []
        if session_memory:
            parts.append(session_memory)
        combined = "\n\n".join(blocks).strip()
        if combined:
            parts.append(f"## Current Session History\n{combined}")
        return "\n\n".join(parts)

    async def _get_agent_transcript(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
    ) -> list[dict[str, Any]]:
        if not self.store or not session_id or not employee_id:
            return []
        transcript = await self.store.get_session_transcript(session_id)
        if not transcript:
            return []
        session = await self.store.get_session(session_id)
        session_employee_id = str((session.metadata or {}).get("employee_id", "")).strip() if session else ""
        if session_employee_id and session_employee_id == employee_id:
            return transcript
        visible_items: list[dict[str, Any]] = []
        for item in transcript:
            metadata = dict(item["message"].metadata or {})
            if str(metadata.get("employee_id", "")).strip() == employee_id:
                visible_items.append(item)
        return visible_items

    async def _get_visible_agent_transcript(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
    ) -> list[dict[str, Any]]:
        if not self.store:
            return []
        transcript = await self._get_agent_transcript(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
        )
        if not transcript:
            return []
        compaction = await self.store.get_latest_agent_compaction(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
        )
        boundary_message_id = compaction.source_boundary_message_id if compaction else ""
        return self._slice_transcript_from_boundary(transcript, boundary_message_id)

    async def build_employee_memory_context(
        self,
        *,
        project_id: str | None,
        session_id: str | None,
        employee_id: str,
        role_id: str = "",
    ) -> str:
        if not self.store or not employee_id:
            return ""
        pid = self._resolve_project_id(project_id)
        snapshot = await self.store.get_agent_memory_snapshot(
            project_id=pid,
            employee_id=employee_id,
            memory_kind="final",
            memory_scope="project",
        )
        if snapshot is None:
            legacy_final = await self.store.get_agent_memory_snapshot(
                project_id=pid,
                employee_id=employee_id,
                memory_kind="final",
                memory_scope="session",
            )
            if legacy_final is not None:
                snapshot = await self._migrate_legacy_project_final_snapshot(legacy_final)
        if snapshot is None and session_id:
            snapshot = await self.store.get_agent_memory_snapshot(
                project_id=pid,
                session_id=session_id,
                employee_id=employee_id,
                memory_kind="final",
                memory_scope="session",
            )
        if snapshot is None and session_id:
            snapshot = await self.store.get_agent_memory_snapshot(
                project_id=pid,
                session_id=session_id,
                employee_id=employee_id,
                memory_kind="process",
                memory_scope="session",
            )
        if not snapshot or not snapshot.memory_text.strip():
            return ""
        if snapshot.memory_kind == "final" and snapshot.memory_scope == "project":
            title = "## Employee Project Memory"
        elif snapshot.memory_kind == "final":
            title = "## Employee Final Memory"
        else:
            title = "## Employee Process Memory"
        resolved_role = role_id or snapshot.role_id
        header_lines = [title, f"- Employee ID: {employee_id}"]
        if resolved_role:
            header_lines.append(f"- Role: {resolved_role}")
        header = "\n".join(header_lines)
        return f"{header}\n\n{snapshot.memory_text.strip()}"

    async def _migrate_legacy_project_final_snapshot(
        self,
        snapshot: AgentMemorySnapshotRecord,
    ) -> AgentMemorySnapshotRecord:
        if not self.store or snapshot.memory_scope == "project":
            return snapshot
        migrated = AgentMemorySnapshotRecord(
            project_id=snapshot.project_id,
            session_id="",
            employee_id=snapshot.employee_id,
            role_id=snapshot.role_id,
            memory_scope="project",
            memory_kind=snapshot.memory_kind,
            summary_message_id=snapshot.summary_message_id,
            source_boundary_message_id=snapshot.source_boundary_message_id,
            summary_text=snapshot.summary_text,
            memory_text=snapshot.memory_text,
            metadata={
                **dict(snapshot.metadata or {}),
                "migrated_from_session_id": snapshot.session_id,
            },
        )
        await self.store.save_agent_memory_snapshot(migrated)
        return migrated

    async def build_employee_history_tail_messages(
        self,
        *,
        project_id: str | None,
        session_id: str | None,
        employee_id: str,
    ) -> list[dict[str, Any]]:
        if not session_id or not employee_id:
            return []
        pid = self._resolve_project_id(project_id)
        visible_items = await self._get_visible_agent_transcript(
            project_id=pid,
            session_id=session_id,
            employee_id=employee_id,
        )
        messages: list[dict[str, Any]] = []
        for item in visible_items:
            message = item["message"]
            content = self._render_session_parts(item["parts"])
            if not content:
                continue
            role = "user" if message.role == "user" else "assistant"
            messages.append({"role": role, "content": content})
        return messages

    async def build_agent_memory_context(self, task: Any, role_id: str) -> str:
        project_id = getattr(task, "project_id", None) or (self.project_id or "default")
        session_id = getattr(task, "session_id", None)
        assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
        employee_id = str(
            assignment.get("employee_id")
            or getattr(task, "metadata", {}).get("employee_id", "")
            or ""
        ).strip()
        return await self.build_employee_memory_context(
            project_id=project_id,
            session_id=session_id,
            employee_id=employee_id,
            role_id=role_id or str(assignment.get("role_id", "")).strip(),
        )

    def build_external_prompt_context(self, task: Any, role_id: str, memory_ctx: str, comm_ctx: dict[str, Any] | None = None) -> str:
        parts = [memory_ctx] if memory_ctx else []
        if comm_ctx:
            inbox = list(comm_ctx.get("inbox", []))
            annotations = list(comm_ctx.get("annotations", []))
            if inbox:
                parts.append(
                    "## Inbox\n" +
                    "\n".join(
                        f"- From {item.get('from_agent', '')}: {item.get('subject', '')} :: {item.get('body', '')}"
                        for item in inbox
                    )
                )
            if annotations:
                parts.append(
                    "## Task Annotations\n" +
                    "\n".join(
                        f"- {item.get('from', '')}: {item.get('body', '')}"
                        for item in annotations
                    )
                )
        if getattr(task, "metadata", {}).get("handoff_context"):
            parts.append(f"## Handoff Context\n{getattr(task, 'metadata', {}).get('handoff_context', '')}")
        return "\n\n".join(part for part in parts if part)

    def _render_session_message(self, message: SessionMessageRecord, parts: list[SessionPartRecord]) -> str:
        rendered = self._render_session_parts(parts)
        if not rendered:
            return ""
        role = "User" if message.role == "user" else ("Summary" if message.summary_flag else "Assistant")
        return f"{role}:\n{rendered}"

    def _compact_artifacts(self, artifacts: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for key, value in artifacts.items():
            if isinstance(value, str):
                compact[key] = value
            elif isinstance(value, (int, float, bool)) or value is None:
                compact[key] = value
            elif isinstance(value, list):
                compact[key] = [self._normalize_artifact_value(item) for item in value]
            elif isinstance(value, dict):
                compact[key] = {k: self._normalize_artifact_value(v) for k, v in value.items()}
            else:
                compact[key] = str(value)
        return compact

    def _normalize_artifact_value(self, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, list):
            return [self._normalize_artifact_value(item) for item in value]
        if isinstance(value, dict):
            return {k: self._normalize_artifact_value(v) for k, v in value.items()}
        return str(value)

    def _format_artifact_lines(self, artifacts: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for key, value in artifacts.items():
            if isinstance(value, list):
                lines.append(f"{key}: {', '.join(str(item) for item in value)}")
            elif isinstance(value, dict):
                inner = ", ".join(f"{k}={v}" for k, v in value.items())
                lines.append(f"{key}: {inner}")
            else:
                lines.append(f"{key}: {value}")
        return lines

    def get_compression_prompt(self, messages: list[dict[str, Any]], existing_memory: str) -> str:
        """Create a prompt to compress conversation history into memory."""
        msg_text = "\n".join(
            f"[{m.get('role', '?')}]: {m.get('content', '')}"
            for m in messages
        )
        return (
            "You are a memory compressor. Summarize the following conversation into "
            "key facts, decisions, and learnings. Preserve important technical details, "
            "user preferences, and outcomes. Be concise but comprehensive. "
            "If the source conversation is large, aggressively deduplicate and abstract instead of copying verbatim.\n\n"
            f"## Existing Memory\n{existing_memory}\n\n"
            f"## New Conversation\n{msg_text}\n\n"
            "Write the updated memory as a structured markdown document. "
            "Merge new information with existing memory. Remove duplicates."
        )
