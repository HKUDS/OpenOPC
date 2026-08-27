"""Read-model builders for the CLI board."""

from __future__ import annotations

import json
import time as _time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from opc.layer2_organization.phase import (
    Phase,
    coerce_phase,
    kanban_column,
    should_hide_work_item_from_company_kanban,
)
from opc.layer2_organization.work_item_context_view import WorkItemContextView
from opc.layer2_organization.work_item_links import task_by_linked_work_item_id
from opc.presentation.kanban import (
    COMPANY_KANBAN_COLUMNS,
    build_base_task_payload,
    datetime_to_timestamp,
)

from ..state.models import (
    BoardAlert,
    BoardMetrics,
    BoardSnapshot,
    BoardTaskView,
    LinkedExecutionView,
    OrgEmployeeView,
    OrgRoleView,
    OrgSnapshotView,
    PendingCheckpointView,
    PipelineSnapshot,
    PipelineWorkItemView,
    SessionMessageView,
    SessionSummaryView,
    TaskDetailView,
)

if TYPE_CHECKING:
    from .engine_facade import EngineFacade


class BoardRepository:
    """Builds CLI-specific board and detail snapshots from the main OPC store."""

    def __init__(self, facade: "EngineFacade", project_id: str | None = None) -> None:
        self.facade = facade
        self.project_id = project_id

    async def load_snapshot(self) -> BoardSnapshot:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return BoardSnapshot(project_id=self._project_id)

        tasks = await engine.store.get_tasks(project_id=self._project_id)
        checkpoints = await engine.store.get_pending_checkpoints(project_id=self._project_id)
        checkpoint_by_session = self._checkpoint_by_session(checkpoints)
        await self._enrich_checkpoint_payloads(checkpoint_by_session)

        company_snapshot = await self._maybe_build_company_snapshot(
            engine,
            tasks=tasks,
            checkpoint_by_session=checkpoint_by_session,
            checkpoint_count=len(checkpoints),
        )
        if company_snapshot is not None:
            return company_snapshot

        visible_tasks, hidden_by_origin, hidden_count = self._split_visible_tasks(tasks)

        task_views: list[BoardTaskView] = []
        for display_num, task in enumerate(visible_tasks, start=1):
            task_views.append(
                self._task_to_view(
                    task,
                    checkpoint=checkpoint_by_session.get(getattr(task, "session_id", None)),
                    linked_tasks=hidden_by_origin.get(getattr(task, "id", ""), []),
                    display_num=display_num,
                )
            )

        session_summaries = [self._session_summary_view(task) for task in task_views]
        alerts = self._alerts_from_tasks(task_views)
        return BoardSnapshot(
            project_id=self._project_id,
            tasks=task_views,
            hidden_task_count=hidden_count,
            pending_checkpoint_count=len(checkpoints),
            session_summaries=session_summaries,
            alerts=alerts,
            metrics=self._build_metrics(task_views, hidden_count=hidden_count, pending_checkpoint_count=len(checkpoints)),
        )

    async def _maybe_build_company_snapshot(
        self,
        engine: Any,
        *,
        tasks: list[Any],
        checkpoint_by_session: dict[str | None, PendingCheckpointView],
        checkpoint_count: int,
    ) -> BoardSnapshot | None:
        """Return a company-mode snapshot if an active delegation run exists.

        Cards are sourced from `DelegationWorkItem`. Runtime `Task` objects are
        kept only as audit references (`runtime_task_id` / `session_id`).
        """
        store = engine.store
        list_runs = getattr(store, "list_open_delegation_runs", None)
        list_items = getattr(store, "list_delegation_work_items", None)
        if list_runs is None or list_items is None:
            return None
        try:
            open_runs = await list_runs(project_id=self._project_id)
        except TypeError:
            open_runs = await list_runs()
        if not open_runs:
            return None
        active_run = open_runs[0]
        run_id = str(getattr(active_run, "run_id", "") or "").strip()
        if not run_id:
            return None
        work_items = await list_items(run_id)
        if not work_items:
            return None

        hydrate_links = getattr(store, "hydrate_task_work_item_links", None)
        if callable(hydrate_links):
            await hydrate_links(tasks)
        task_by_work_item_id = task_by_linked_work_item_id(tasks)

        visible_items: list[Any] = []
        hidden_count = 0
        for item in work_items:
            metadata = dict(getattr(item, "metadata", {}) or {})
            if not str(getattr(item, "parent_work_item_id", "") or "").strip():
                # Skip the synthetic root work item — the kanban shows leaf delegations.
                hidden_count += 1
                continue
            if bool(metadata.get("attention_work_item", False)):
                hidden_count += 1
                continue
            if should_hide_work_item_from_company_kanban(metadata):
                hidden_count += 1
                continue
            visible_items.append(item)

        task_views: list[BoardTaskView] = []
        for display_num, item in enumerate(visible_items, start=1):
            linked_task = task_by_work_item_id.get(str(getattr(item, "work_item_id", "") or "").strip())
            checkpoint = None
            linked_session_id = getattr(linked_task, "session_id", None) if linked_task is not None else None
            if linked_session_id:
                checkpoint = checkpoint_by_session.get(linked_session_id)
            task_views.append(
                self._work_item_to_view(
                    item,
                    linked_task=linked_task,
                    checkpoint=checkpoint,
                    display_num=display_num,
                )
            )

        session_summaries = [self._session_summary_view(task) for task in task_views]
        alerts = self._alerts_from_tasks(task_views)
        column_order = [column.column_id for column in COMPANY_KANBAN_COLUMNS]
        return BoardSnapshot(
            project_id=self._project_id,
            tasks=task_views,
            hidden_task_count=hidden_count,
            pending_checkpoint_count=checkpoint_count,
            session_summaries=session_summaries,
            alerts=alerts,
            metrics=self._build_metrics(
                task_views,
                hidden_count=hidden_count,
                pending_checkpoint_count=checkpoint_count,
            ),
            mode="company",
            column_order=column_order,
        )

    async def load_task_detail(self, task_id: str) -> TaskDetailView | None:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return None

        tasks = await engine.store.get_tasks(project_id=self._project_id)
        checkpoints = await engine.store.get_pending_checkpoints(project_id=self._project_id)
        checkpoint_by_session = self._checkpoint_by_session(checkpoints)
        await self._enrich_checkpoint_payloads(checkpoint_by_session)

        # In company mode the kanban card_id is a DelegationWorkItem.work_item_id,
        # not a Task.id. Try the work-item path first; fall back to the runtime
        # Task path so standard mode (and detail links by Task.id) still works.
        work_item_detail = await self._load_work_item_detail(
            engine,
            work_item_id=task_id,
            tasks=tasks,
            checkpoint_by_session=checkpoint_by_session,
        )
        if work_item_detail is not None:
            return work_item_detail

        visible_tasks, _, _ = self._split_visible_tasks(tasks)
        target = next((task for task in tasks if getattr(task, "id", "") == task_id), None)
        if target is None:
            return None

        pending = checkpoint_by_session.get(getattr(target, "session_id", None))
        linked: list[Any] = []
        seen_ids: set[str] = set()
        for task in tasks:
            origin_task_id = self._origin_task_id(task)
            if origin_task_id == task_id and getattr(task, "id", "") != task_id:
                if task.id not in seen_ids:
                    linked.append(task)
                    seen_ids.add(task.id)
                continue
            if getattr(task, "parent_session_id", None) and getattr(task, "parent_session_id", None) == getattr(target, "session_id", None):
                if task.id not in seen_ids and task.id != task_id:
                    linked.append(task)
                    seen_ids.add(task.id)

        transcript: list[SessionMessageView] = []
        if getattr(target, "session_id", None):
            raw_transcript = await engine.store.get_session_transcript(target.session_id)
            transcript = [msg for msg in (self._transcript_item_to_view(item) for item in raw_transcript) if msg is not None]

        display_num = next(
            (index for index, task in enumerate(visible_tasks, start=1) if getattr(task, "id", "") == task_id),
            0,
        )
        task_view = self._task_to_view(target, checkpoint=pending, linked_tasks=linked, display_num=display_num)
        linked_views = [self._linked_execution_view(task) for task in linked]
        result = getattr(target, "result", None) or {}
        result_content = result.get("content") if isinstance(result, dict) else None
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        metadata = getattr(target, "metadata", {}) if isinstance(getattr(target, "metadata", {}), dict) else {}
        context_preview = (
            metadata.get("handoff_context")
            or metadata.get("context_preview")
            or metadata.get("secretary_context")
            or None
        )
        return TaskDetailView(
            task=task_view,
            transcript=transcript,
            linked_executions=linked_views,
            progress_entries=list(metadata.get("progress_log", []) or []),
            pending_checkpoint=pending,
            result_content=result_content,
            artifacts=artifacts if isinstance(artifacts, list) else [artifacts],
            context_preview=str(context_preview).strip() if context_preview else None,
        )

    async def _load_work_item_detail(
        self,
        engine: Any,
        *,
        work_item_id: str,
        tasks: list[Any],
        checkpoint_by_session: dict[str | None, PendingCheckpointView],
    ) -> TaskDetailView | None:
        """Build a detail view keyed by DelegationWorkItem.work_item_id.

        Returns None when the id is not a known work item (caller falls back
        to the Task-id path).
        """
        store = engine.store
        list_runs = getattr(store, "list_open_delegation_runs", None)
        list_items = getattr(store, "list_delegation_work_items", None)
        if list_runs is None or list_items is None or not work_item_id:
            return None
        try:
            open_runs = await list_runs(project_id=self._project_id)
        except TypeError:
            open_runs = await list_runs()
        if not open_runs:
            return None
        target_item: Any | None = None
        for run in open_runs:
            run_id = str(getattr(run, "run_id", "") or "").strip()
            if not run_id:
                continue
            for item in await list_items(run_id):
                if str(getattr(item, "work_item_id", "") or "").strip() == work_item_id:
                    target_item = item
                    break
            if target_item is not None:
                break
        if target_item is None:
            return None

        task_by_work_item_id = task_by_linked_work_item_id(tasks)
        linked_task = task_by_work_item_id.get(work_item_id)

        linked_session_id = str(getattr(linked_task, "session_id", "") or "").strip() if linked_task is not None else ""
        checkpoint = checkpoint_by_session.get(linked_session_id) if linked_session_id else None

        transcript: list[SessionMessageView] = []
        if linked_session_id:
            raw_transcript = await engine.store.get_session_transcript(linked_session_id)
            transcript = [
                msg
                for msg in (self._transcript_item_to_view(item) for item in raw_transcript)
                if msg is not None
            ]

        task_view = self._work_item_to_view(
            target_item,
            linked_task=linked_task,
            checkpoint=checkpoint,
            display_num=0,
        )
        result = getattr(linked_task, "result", None) or {} if linked_task is not None else {}
        result_content = result.get("content") if isinstance(result, dict) else None
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        # Prefer work-item handoff context; fall back to runtime Task metadata.
        linked_metadata = (
            getattr(linked_task, "metadata", None) or {}
            if linked_task is not None
            else {}
        )
        view = WorkItemContextView(target_item, linked_task)
        context_preview = (
            view.get("handoff_context")
            or view.get("context_preview")
            or (linked_metadata.get("secretary_context") if isinstance(linked_metadata, dict) else None)
            or None
        )
        progress_log = view.get_list("progress_log")
        return TaskDetailView(
            task=task_view,
            transcript=transcript,
            linked_executions=[self._linked_execution_view(linked_task)] if linked_task is not None else [],
            progress_entries=progress_log,
            pending_checkpoint=checkpoint,
            result_content=result_content,
            artifacts=artifacts if isinstance(artifacts, list) else [artifacts],
            context_preview=str(context_preview).strip() if context_preview else None,
        )

    async def load_pipeline_state(
        self,
        parent_task_id: str,
        runtime_lookup: dict[str, Any] | None = None,
    ) -> PipelineSnapshot | None:
        """Build the terminal Office-style work-item pipeline for a run."""
        engine = await self.facade.ensure_ready()
        store = getattr(engine, "store", None)
        if not store:
            return None
        list_runs = getattr(store, "list_open_delegation_runs", None)
        list_items = getattr(store, "list_delegation_work_items", None)
        if list_runs is None or list_items is None:
            return None
        try:
            runs = list(await list_runs(project_id=self._project_id))
        except TypeError:
            runs = list(await list_runs())
        if not runs:
            return None

        tasks = await store.get_tasks(project_id=self._project_id) if hasattr(store, "get_tasks") else []
        hydrate_links = getattr(store, "hydrate_task_work_item_links", None)
        if callable(hydrate_links):
            await hydrate_links(tasks)
        linked_tasks = task_by_linked_work_item_id(tasks)

        selected_run = None
        selected_items: list[Any] = []
        target = str(parent_task_id or "").strip()
        for run in runs:
            run_id = str(getattr(run, "run_id", "") or "").strip()
            items = list(await list_items(run_id)) if run_id else []
            run_keys = {
                run_id,
                str(getattr(run, "parent_task_id", "") or "").strip(),
                str(getattr(run, "task_id", "") or "").strip(),
                str(getattr(run, "parent_session_id", "") or "").strip(),
            }
            item_keys = {str(getattr(item, "work_item_id", "") or "").strip() for item in items}
            if not target or target in run_keys or target in item_keys:
                selected_run = run
                selected_items = items
                break
        if selected_run is None:
            selected_run = runs[0]
            run_id = str(getattr(selected_run, "run_id", "") or "").strip()
            selected_items = list(await list_items(run_id)) if run_id else []
        if not selected_items:
            return None

        work_items: list[PipelineWorkItemView] = []
        start_ts = 0.0
        end_ts = 0.0
        for item in selected_items:
            work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
            linked_task = linked_tasks.get(work_item_id)
            linked_task_id = str(getattr(linked_task, "id", "") or "").strip() if linked_task is not None else ""
            metadata = dict(getattr(item, "metadata", {}) or {})
            linked_metadata = dict(getattr(linked_task, "metadata", {}) or {}) if linked_task is not None else {}
            phase = coerce_phase(getattr(item, "phase", Phase.READY))
            created_at = datetime_to_timestamp(getattr(item, "created_at", None))
            updated_at = datetime_to_timestamp(getattr(item, "updated_at", None) or getattr(item, "created_at", None))
            if created_at and (not start_ts or created_at < start_ts):
                start_ts = created_at
            if updated_at and updated_at > end_ts:
                end_ts = updated_at
            runtime = None
            if runtime_lookup:
                runtime = runtime_lookup.get(linked_task_id) or runtime_lookup.get(work_item_id)
            work_items.append(PipelineWorkItemView(
                projection_id=work_item_id,
                title=str(getattr(item, "title", "") or work_item_id),
                role_id=str(getattr(item, "role_id", "") or ""),
                status=phase.value,
                assigned_to=str(getattr(linked_task, "assigned_to", "") or getattr(item, "role_id", "") or ""),
                task_id=linked_task_id or None,
                runtime_task_id=linked_task_id or None,
                execution_turn_id=linked_task_id or None,
                session_id=str(getattr(linked_task, "session_id", "") or "") or None,
                elapsed_sec=max(0.0, (updated_at or _time.time()) - created_at) if created_at else 0.0,
                current_tool=getattr(runtime, "current_tool", None) if runtime is not None else None,
                tool_elapsed_ms=int(getattr(runtime, "tool_elapsed_ms", 0) or 0) if runtime is not None else 0,
                last_tool_summary=str(getattr(runtime, "last_tool_summary", "") or "") if runtime is not None else "",
                context_remaining_pct=int(getattr(runtime, "context_remaining_pct", 0) or 0) if runtime is not None else 0,
                turn_cost_usd=float(getattr(runtime, "turn_cost_usd", 0.0) or 0.0) if runtime is not None else 0.0,
                has_gate=bool(metadata.get("gate_type") or linked_metadata.get("checkpoint_hint") or (getattr(runtime, "pending_permission_count", 0) if runtime is not None else 0)),
                gate_type=str(metadata.get("gate_type") or linked_metadata.get("checkpoint_type") or "") or None,
                dependencies=[
                    str(dep).strip()
                    for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                    if str(dep).strip()
                ],
                parallel_group=str(metadata.get("parallel_group", "") or "") or None,
            ))

        done_count = sum(1 for item in work_items if item.status in {"done", "reviewed", "delivered"})
        parent_title = (
            str(getattr(selected_run, "title", "") or "")
            or str(getattr(selected_run, "summary", "") or "")
            or str(getattr(selected_run, "run_id", "") or "")
        )
        return PipelineSnapshot(
            parent_task_id=str(getattr(selected_run, "parent_task_id", "") or getattr(selected_run, "task_id", "") or target),
            parent_title=parent_title,
            profile=str(getattr(selected_run, "profile", "") or getattr(selected_run, "company_profile", "") or ""),
            work_items=work_items,
            done_count=done_count,
            total_count=len(work_items),
            elapsed_sec=max(0.0, (end_ts or _time.time()) - start_ts) if start_ts else 0.0,
        )

    async def load_org_snapshot(self) -> OrgSnapshotView | None:
        """Build a read-only view of the current org structure."""
        engine = await self.facade.ensure_ready()
        if not engine.org_engine:
            return None

        org = engine.org_engine

        # Roles as tree
        all_agents = org.list_agents()
        all_employees = org.list_employees()
        emp_count_by_role: dict[str, int] = {}
        for emp in all_employees:
            emp_count_by_role[emp.role_id] = emp_count_by_role.get(emp.role_id, 0) + 1

        try:
            tree_raw = org.get_org_tree()
        except Exception:
            tree_raw = []

        def _build_role_tree(nodes: list[dict]) -> list[OrgRoleView]:
            result: list[OrgRoleView] = []
            for node in nodes:
                agent = node.get("agent")
                if agent is None:
                    continue
                role_id = getattr(agent, "role_id", "")
                result.append(OrgRoleView(
                    role_id=role_id,
                    name=getattr(agent, "name", role_id),
                    responsibility=getattr(agent, "responsibility", ""),
                    reports_to=getattr(agent, "reports_to", "owner"),
                    employee_count=emp_count_by_role.get(role_id, 0),
                    children=_build_role_tree(node.get("reports", [])),
                ))
            return result

        role_tree = _build_role_tree(tree_raw)

        # Employees
        employee_views = [
            OrgEmployeeView(
                employee_id=emp.employee_id,
                name=emp.name,
                role_id=emp.role_id,
                category=emp.category,
                domains=list(emp.domains),
                seniority=emp.seniority,
            )
            for emp in all_employees
        ]

        profile = org.get_company_profile()

        return OrgSnapshotView(
            role_tree=role_tree,
            employees=employee_views,
            company_profile=profile,
            role_count=len(all_agents),
            employee_count=len(all_employees),
        )

    @property
    def _project_id(self) -> str:
        return self.project_id or self.facade.project_id or "default"

    @staticmethod
    def _origin_task_id(task: Any) -> str | None:
        metadata = getattr(task, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
        return origin_task_id or None

    def _split_visible_tasks(self, tasks: list[Any]) -> tuple[list[Any], dict[str, list[Any]], int]:
        visible_tasks: list[Any] = []
        hidden_by_origin: dict[str, list[Any]] = defaultdict(list)
        hidden_count = 0
        for task in tasks:
            origin_task_id = self._origin_task_id(task)
            if origin_task_id and origin_task_id != getattr(task, "id", ""):
                hidden_by_origin[origin_task_id].append(task)
                hidden_count += 1
                continue
            visible_tasks.append(task)
        return visible_tasks, hidden_by_origin, hidden_count

    @staticmethod
    def _checkpoint_prompt(checkpoint: Any) -> tuple[str, str]:
        payload = getattr(checkpoint, "payload", {}) or {}
        interaction = dict(payload.get("interaction", {}) or {})
        prompt = (
            interaction.get("prompt")
            or payload.get("prompt")
            or payload.get("message")
            or payload.get("summary")
            or payload.get("original_message")
            or ""
        )
        prompt_text = str(prompt)
        if not prompt_text:
            prompt_text = json.dumps(payload, ensure_ascii=False, indent=2)[:600]

        summary = (
            payload.get("feedback_scope")
            or payload.get("work_item_projection_title")
            or payload.get("summary")
            or payload.get("title")
            or getattr(checkpoint, "checkpoint_type", "pending")
        )
        return str(summary).strip(), prompt_text

    def _checkpoint_view(self, checkpoint: Any) -> PendingCheckpointView:
        summary, prompt = self._checkpoint_prompt(checkpoint)
        return PendingCheckpointView(
            checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or ""),
            checkpoint_type=str(getattr(checkpoint, "checkpoint_type", "") or ""),
            status=str(getattr(checkpoint, "status", "") or ""),
            session_id=getattr(checkpoint, "session_id", None),
            task_id=getattr(checkpoint, "task_id", None),
            summary=summary,
            prompt=prompt,
            payload=dict(getattr(checkpoint, "payload", {}) or {}),
        )

    def _checkpoint_by_session(self, checkpoints: list[Any]) -> dict[str | None, PendingCheckpointView]:
        result: dict[str | None, PendingCheckpointView] = {}
        for checkpoint in checkpoints:
            session_id = getattr(checkpoint, "session_id", None)
            if session_id not in result:
                result[session_id] = self._checkpoint_view(checkpoint)
        return result

    async def _enrich_checkpoint_payloads(
        self,
        checkpoint_map: dict[str | None, PendingCheckpointView],
    ) -> None:
        """Enrich reorg checkpoint payloads with full proposal data from store."""
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return
        for view in checkpoint_map.values():
            if view.checkpoint_type != "company_reorg_pending":
                continue
            proposal_id = str(view.payload.get("proposal_id", "") or "").strip()
            if not proposal_id:
                continue
            try:
                proposal = await engine.store.get_reorg_proposal(proposal_id)
            except Exception:
                continue
            if proposal is None:
                continue
            view.payload["title"] = proposal.title
            view.payload["scope"] = proposal.scope.value if hasattr(proposal.scope, "value") else str(proposal.scope)
            view.payload["risk_level"] = proposal.risk_level.value if hasattr(proposal.risk_level, "value") else str(proposal.risk_level)
            view.payload["summary"] = proposal.summary
            view.payload["rationale"] = proposal.rationale
            view.payload["impact_summary"] = dict(proposal.impact_summary) if proposal.impact_summary else {}
            changeset = proposal.changeset
            if changeset:
                view.payload["role_changes"] = [
                    {"action": rc.action, "role_id": rc.role_id, "replacement_role_id": getattr(rc, "replacement_role_id", ""), "reason": getattr(rc, "reason", "")}
                    for rc in (changeset.role_changes if hasattr(changeset, "role_changes") else [])
                ]

    def _work_item_to_view(
        self,
        item: Any,
        *,
        linked_task: Any | None,
        checkpoint: PendingCheckpointView | None,
        display_num: int = 0,
    ) -> BoardTaskView:
        """Build a BoardTaskView from a DelegationWorkItem (company-mode card).

        The card identity is the work_item_id; runtime Task / session are
        kept only as audit references via runtime_task_id / session_id.
        """
        normalized_metadata = dict(getattr(item, "metadata", {}) or {})
        phase = coerce_phase(getattr(item, "phase", Phase.READY))
        column_id = kanban_column(phase).replace("_", "-")
        canonical_status = phase.value
        work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
        role_id = str(getattr(item, "role_id", "") or "").strip()
        title = str(getattr(item, "title", "") or "").strip()
        summary = str(getattr(item, "summary", "") or "").strip()
        kind = str(getattr(item, "kind", "") or "").strip()
        dependencies = [
            str(dep).strip()
            for dep in list(normalized_metadata.get("dependency_work_item_ids", []) or [])
            if str(dep).strip()
        ]
        created_at = datetime_to_timestamp(getattr(item, "created_at", None))
        updated_at = datetime_to_timestamp(getattr(item, "updated_at", None) or getattr(item, "created_at", None))

        # Audit/back-references (NOT used to drive lifecycle on the card).
        linked_task_id = str(getattr(linked_task, "id", "") or "").strip() if linked_task is not None else ""
        linked_session_id = str(getattr(linked_task, "session_id", "") or "").strip() if linked_task is not None else ""
        result = getattr(linked_task, "result", None) or {} if linked_task is not None else {}
        result_content = result.get("content") if isinstance(result, dict) else None
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        # Merge work-item metadata first, then linked-task metadata so the card
        # surfaces work-item-truth (status/dependencies) plus runtime telemetry.
        merged_metadata: dict[str, Any] = dict(normalized_metadata)
        if linked_task is not None:
            linked_meta = getattr(linked_task, "metadata", None) or {}
            if isinstance(linked_meta, dict):
                for key, value in linked_meta.items():
                    merged_metadata.setdefault(key, value)

        return BoardTaskView(
            task_id=work_item_id,
            title=title,
            description=summary,
            status=canonical_status,
            column_id=column_id,
            priority=None,
            assignee_ids=[role_id] if role_id else [],
            assigned_to=role_id,
            tags=[kind] if kind else [],
            session_id=linked_session_id or None,
            created_at=created_at,
            updated_at=updated_at,
            metadata=merged_metadata,
            pending_checkpoint=checkpoint,
            linked_task_count=1 if linked_task is not None else 0,
            result_content=result_content,
            artifacts=artifacts if isinstance(artifacts, list) else [artifacts],
            origin_task_id=None,
            display_id=f"OPC-{display_num}" if display_num else "",
            dependencies=dependencies,
            work_item_id=work_item_id,
            runtime_task_id=linked_task_id or None,
            execution_turn_id=linked_task_id or None,
        )

    def _task_to_view(
        self,
        task: Any,
        *,
        checkpoint: PendingCheckpointView | None,
        linked_tasks: list[Any],
        display_num: int = 0,
    ) -> BoardTaskView:
        payload = build_base_task_payload(task, display_num)
        result = getattr(task, "result", None) or {}
        result_content = result.get("content") if isinstance(result, dict) else None
        artifacts = result.get("artifacts", []) if isinstance(result, dict) else []
        metadata = getattr(task, "metadata", {}) if isinstance(getattr(task, "metadata", {}), dict) else {}
        return BoardTaskView(
            task_id=payload["task_id"],
            title=payload["title"],
            description=payload["description"],
            status=payload["status"],
            column_id=payload["column_id"],
            priority=payload["priority"],
            assignee_ids=payload["assignee_ids"],
            assigned_to=str(getattr(task, "assigned_to", "") or ""),
            tags=payload["tags"],
            session_id=payload["session_id"],
            created_at=float(payload["created_at"]),
            updated_at=float(payload["updated_at"]),
            metadata=metadata,
            pending_checkpoint=checkpoint,
            linked_task_count=len(linked_tasks),
            result_content=result_content,
            artifacts=artifacts if isinstance(artifacts, list) else [artifacts],
            origin_task_id=self._origin_task_id(task),
            display_id=str(payload.get("display_id", "") or ""),
            dependencies=list(payload.get("dependencies", []) or []),
            runtime_task_id=payload["task_id"],
            execution_turn_id=payload["task_id"],
        )

    @staticmethod
    def _session_summary_view(task: BoardTaskView) -> SessionSummaryView:
        return SessionSummaryView(
            task_id=task.task_id,
            title=task.title,
            status=task.status,
            column_id=task.column_id,
            session_id=task.session_id,
            updated_at=float(task.updated_at),
            created_at=float(task.created_at),
            assigned_to=task.assigned_to,
            priority=task.priority,
            pending_checkpoint=task.pending_checkpoint is not None,
            linked_task_count=task.linked_task_count,
            tags=list(task.tags),
            runtime_task_id=task.runtime_task_id,
            execution_turn_id=task.execution_turn_id or task.runtime_task_id,
        )

    @staticmethod
    def _alerts_from_tasks(tasks: list[BoardTaskView]) -> list[BoardAlert]:
        alerts: list[BoardAlert] = []
        for task in tasks:
            item_label = "Work item" if task.work_item_id else "Task"
            if task.pending_checkpoint:
                alerts.append(
                    BoardAlert(
                        alert_id=f"checkpoint:{task.task_id}",
                        level="warn",
                        title="Checkpoint pending",
                        message=f"{task.title} is waiting for human feedback.",
                        task_id=task.task_id,
                        created_at=float(task.updated_at or task.created_at),
                    )
                )
            if task.status in {"blocked", "awaiting_peer", "awaiting_review"}:
                alerts.append(
                    BoardAlert(
                        alert_id=f"blocked:{task.task_id}",
                        level="warn",
                        title=f"{item_label} paused",
                        message=f"{task.title} is paused in `{task.status}`.",
                        task_id=task.task_id,
                        created_at=float(task.updated_at or task.created_at),
                    )
                )
            if task.status in {"failed", "cancelled"}:
                alerts.append(
                    BoardAlert(
                        alert_id=f"terminal:{task.task_id}",
                        level="error",
                        title=f"{item_label} ended abnormally",
                        message=f"{task.title} finished with status `{task.status}`.",
                        task_id=task.task_id,
                        created_at=float(task.updated_at or task.created_at),
                    )
                )
        level_weight = {"error": 0, "warn": 1, "info": 2}
        alerts.sort(key=lambda alert: (level_weight.get(alert.level, 99), -float(alert.created_at)))
        return alerts[:12]

    @staticmethod
    def _build_metrics(
        tasks: list[BoardTaskView],
        *,
        hidden_count: int,
        pending_checkpoint_count: int,
    ) -> BoardMetrics:
        todo_count = sum(1 for task in tasks if task.column_id == "todo")
        in_progress_count = sum(1 for task in tasks if task.column_id == "in-progress")
        in_review_count = sum(1 for task in tasks if task.column_id == "in-review")
        done_count = sum(1 for task in tasks if task.column_id == "done")
        blocked_count = sum(1 for task in tasks if task.status in {"blocked", "awaiting_peer", "awaiting_review"})
        failed_count = sum(1 for task in tasks if task.status in {"failed", "cancelled"})
        active_session_count = sum(1 for task in tasks if task.session_id)
        running_count = sum(
            1
            for task in tasks
            if task.status in {"running", "idle", "blocked", "awaiting_peer", "awaiting_review"}
        )
        return BoardMetrics(
            total_tasks=len(tasks) + hidden_count,
            visible_tasks=len(tasks),
            filtered_tasks=len(tasks),
            hidden_task_count=hidden_count,
            todo_count=todo_count,
            in_progress_count=in_progress_count,
            in_review_count=in_review_count,
            done_count=done_count,
            running_count=running_count,
            blocked_count=blocked_count,
            failed_count=failed_count,
            pending_checkpoint_count=pending_checkpoint_count,
            active_session_count=active_session_count,
            alert_count=len(BoardRepository._alerts_from_tasks(tasks)),
        )

    @staticmethod
    def _linked_execution_view(task: Any) -> LinkedExecutionView:
        payload = build_base_task_payload(task, 0)
        return LinkedExecutionView(
            task_id=payload["task_id"],
            title=payload["title"],
            status=payload["status"],
            assigned_to=str(getattr(task, "assigned_to", "") or ""),
            session_id=payload["session_id"],
            created_at=float(payload["created_at"]),
            updated_at=float(payload["updated_at"]),
            runtime_task_id=payload["task_id"],
            execution_turn_id=payload["task_id"],
            metadata=dict(getattr(task, "metadata", {}) or {}),
        )

    @staticmethod
    def _render_parts(parts: list[Any]) -> str:
        lines: list[str] = []
        for part in parts:
            part_type = getattr(part, "part_type", "")
            payload = getattr(part, "payload", {}) if isinstance(getattr(part, "payload", {}), dict) else {}
            if part_type == "text":
                text = payload.get("text", "")
                if text:
                    lines.append(str(text))
            elif part_type in {"subtask_result", "task_result"}:
                title = payload.get("task_title", "Task")
                summary = payload.get("summary", "")
                lines.append(f"{title}: {summary}".strip(": "))
        return "\n".join(lines).strip()

    def _transcript_item_to_view(self, item: dict[str, Any]) -> SessionMessageView | None:
        message = item.get("message")
        if not message or getattr(message, "summary_flag", False):
            return None

        content = self._render_parts(item.get("parts", []))
        if not content:
            return None

        role = str(getattr(message, "role", "") or "").strip().lower()
        agent_id = str(getattr(message, "agent_id", "") or "").strip()
        sender_name = {
            "user": "You",
            "assistant": "OPC",
            "system": "System",
            "subagent": agent_id.replace("_", " ").replace("-", " ").title() if agent_id else "Subagent",
        }.get(role, agent_id or role.title() or "OPC")
        created_at = getattr(message, "created_at", None)
        timestamp = created_at.timestamp() if hasattr(created_at, "timestamp") else 0.0
        return SessionMessageView(
            message_id=str(getattr(message, "message_id", "") or ""),
            role=role or "assistant",
            sender_name=sender_name,
            content=content,
            created_at=timestamp,
            metadata={"agent_id": agent_id} if agent_id else {},
        )
