"""Task detail pane with structured checkpoint panels."""

from __future__ import annotations

import math
from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Static

from ..state.models import PendingCheckpointView, TaskDetailView
from ..state.store import BoardStateStore
from .render_utils import adaptive_summary, badge, format_clock, humanize_age, priority_style, status_style, truncate_text

_RISK_STYLES = {
    "low": "bold #22c55e",
    "medium": "bold #f59e0b",
    "high": "bold #ef4444",
    "critical": "bold white on #ef4444",
}

_SCOPE_LABELS = {
    "task_adjustment": "Task Adjustment",
    "org_mutation": "Org Mutation",
}

_CHANGE_ACTION_STYLES = {
    "add": ("+ ", "bold #22c55e"),
    "remove": ("- ", "bold #ef4444"),
    "replace": ("~ ", "bold #f59e0b"),
    "update": ("~ ", "bold #f59e0b"),
}


def _progress_bar(ratio: float | None, width: int = 10) -> str:
    """Render a block progress bar: ████████░░"""
    try:
        val = float(ratio if ratio is not None else 0)
        if math.isnan(val) or math.isinf(val):
            val = 0.0
        clamped = max(0.0, min(1.0, val))
    except (TypeError, ValueError):
        clamped = 0.0
    filled = int(clamped * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


class DetailPaneWidget(Static):
    """Render the selected task's metadata and linked executions."""

    def __init__(self, state: BoardStateStore) -> None:
        super().__init__(id="detail-pane")
        self.state = state
        self.detail: TaskDetailView | None = None

    def set_detail(self, detail: TaskDetailView | None) -> None:
        self.detail = detail
        self.refresh()

    def render(self) -> RenderableType:
        focused = self.state.pane_focus == "context" and self.state.context_tab == "detail"
        company_mode = self.state.snapshot.mode == "company"
        detail_title = "Work Item Detail" if company_mode else "Task Detail"
        if self.detail is None:
            return Panel(
                Text(
                    "Select a work item to inspect details." if company_mode else "Select a task to inspect details.",
                    style="dim",
                ),
                title=f"{detail_title} [Focused]" if focused else detail_title,
                border_style="cyan" if focused else "white",
            )

        task = self.detail.task
        runtime = self.state.runtime_for(task.task_id)
        blocks: list[RenderableType] = [self._render_overview(task, runtime)]

        if self.detail.pending_checkpoint:
            blocks.append(self._render_checkpoint())
        if self.detail.linked_executions:
            blocks.append(self._render_linked())
        if self.detail.result_content:
            blocks.append(self._render_section("Latest Result", self.detail.result_content, 560))
        if self.detail.context_preview:
            blocks.append(self._render_section("Context Preview", self.detail.context_preview, 560))

        return Panel(
            Group(*blocks),
            title=f"{detail_title} [Focused]" if focused else detail_title,
            border_style="cyan" if focused else "white",
        )

    def _render_overview(self, task: Any, runtime: Any) -> Text:
        adaptive = adaptive_summary(task.metadata)
        header = Text()
        header.append(f"{task.display_id or task.task_id}\n", style="bold #38bdf8")
        header.append(f"{task.title}\n", style="bold white")
        header += badge(task.status.upper(), status_style(task.status))
        if task.priority:
            header.append(" ")
            header += badge(task.priority.upper(), priority_style(task.priority))
        if task.pending_checkpoint:
            header.append(" ")
            header += badge("REVIEW", status_style("warn"))
        if runtime and runtime.status not in {"idle", ""}:
            header.append(" ")
            header += badge(runtime.status.upper(), status_style(runtime.status))
        header.append("\n")
        header.append(f"column {task.column_id}", style="dim")
        header.append(f"  updated {humanize_age(task.updated_at)}", style="dim")
        header.append(f"  created {format_clock(task.created_at)}", style="dim")
        if task.assigned_to:
            header.append(f"\nowner {task.assigned_to}", style="dim")
        if task.tags:
            header.append(f"\ntags {truncate_text(', '.join(task.tags), 70)}", style="dim")
        if task.dependencies:
            header.append(f"\ndeps {truncate_text(', '.join(task.dependencies), 70)}", style="dim")
        if adaptive["state"]:
            header.append(f"\nadaptive {adaptive['state']}", style="dim")
        if adaptive["gate_owner"]:
            header.append(f"\ngate owner {adaptive['gate_owner']}", style="dim")
        if adaptive["missing_signals"]:
            header.append(
                f"\nmissing signals {truncate_text(', '.join(adaptive['missing_signals']), 70)}",
                style="dim",
            )
        if adaptive["confidence_label"]:
            header.append(f"\nconfidence {adaptive['confidence_label']}", style="dim")
        if adaptive["blocked_reason"]:
            header.append(f"\nwaiting {truncate_text(adaptive['blocked_reason'], 120)}", style="dim")
        if runtime and runtime.current_tool:
            header.append(f"\nactive tool {runtime.current_tool}", style="dim")
        if task.description:
            header.append(f"\n\n{truncate_text(task.description, 420)}", style="white")
        return header

    # ----- Checkpoint dispatcher -----

    def _render_checkpoint(self) -> RenderableType:
        checkpoint = self.detail.pending_checkpoint
        assert checkpoint is not None
        cp_type = checkpoint.checkpoint_type.strip().lower()
        if cp_type == "company_staffing_selection":
            return self._render_staffing_checkpoint(checkpoint)
        if cp_type == "company_recruitment_confirmation":
            return self._render_recruitment_checkpoint(checkpoint)
        if cp_type == "company_reorg_pending":
            return self._render_reorg_checkpoint(checkpoint)
        return self._render_generic_checkpoint(checkpoint)

    # ----- Recruitment -----

    def _render_staffing_checkpoint(self, checkpoint: PendingCheckpointView) -> RenderableType:
        payload = checkpoint.payload
        roles = payload.get("staffing_roles", []) or []
        pool = payload.get("staffing_pool", {}) or {}
        employees = {
            str(item.get("employee_id", "") or ""): item
            for item in list(pool.get("employees", []) or [])
            if str(item.get("employee_id", "") or "")
        }
        profile = payload.get("company_profile", "")

        header = Text()
        header.append("MANUAL STAFFING", style="bold #22c55e")
        header.append("  ")
        header += badge("PENDING", status_style("warn"))
        if profile:
            header.append(f"  {profile}", style="dim italic")

        parts: list[RenderableType] = [header]
        for index, role in enumerate(roles, start=1):
            role_id = str(role.get("role_id", "") or "?")
            role_label = str(role.get("role_label", "") or role_id)
            selection = role.get("default_selection", {}) or {}
            text = Text(f"\n{index}. ", style="bold white")
            text.append(role_id, style="bold #38bdf8")
            if role_label and role_label != role_id:
                text.append(f"  {role_label}", style="dim")
            if selection.get("kind") == "employee":
                employee_id = str(selection.get("employee_id") or selection.get("id") or "")
                employee = employees.get(employee_id, {})
                name = employee.get("employee_name") or employee_id
                text.append(f"\n   default: {name}", style="bold white")
                text.append(f" ({employee_id})", style="dim")
            else:
                text.append("\n   default: fallback role-only", style="dim")
            parts.append(text)
        parts.append(Text("\n[a] Approve defaults  [r] Auto Recruit  [d] Deny", style="dim italic"))
        return Panel(Group(*parts), title="Checkpoint: Manual Staffing", border_style="#22c55e")

    def _render_recruitment_checkpoint(self, checkpoint: PendingCheckpointView) -> RenderableType:
        payload = checkpoint.payload
        plan = payload.get("recruitment_plan", {})
        proposals = plan.get("proposals", [])
        summary_text = plan.get("summary", "") or checkpoint.summary
        profile = plan.get("company_profile", "")

        header = Text()
        header.append("RECRUITMENT", style="bold #fbbf24")
        header.append("  ")
        header += badge("PENDING", status_style("warn"))
        if profile:
            header.append(f"  {profile}", style="dim italic")

        parts: list[RenderableType] = [header]

        if summary_text:
            parts.append(Text(f"\n{truncate_text(summary_text, 280)}", style="white"))

        for i, proposal in enumerate(proposals):
            parts.append(self._render_proposal(i + 1, proposal))

        parts.append(Text("\n[a] Approve  [d] Deny  [e] Feedback", style="dim italic"))

        return Panel(Group(*parts), title="Checkpoint: Recruitment", border_style="#fbbf24")

    def _render_proposal(self, index: int, proposal: dict[str, Any]) -> Text:
        role_id = proposal.get("role_id") or "?"
        status = proposal.get("status") or ""
        status_label = {"proposed_hire": "New Hire", "existing_staff": "Existing"}.get(status, status or "Fallback")
        role_labels = proposal.get("role_labels") or []

        text = Text(f"\n{index}. ", style="bold white")
        text.append(role_id, style="bold #38bdf8")
        text.append("  ")
        text += badge(status_label, "bold black on #64748b")

        if role_labels:
            text.append(f"\n   roles: {', '.join(str(label) for label in role_labels)}", style="dim")

        candidate = proposal.get("candidate")
        if candidate and isinstance(candidate, dict):
            name = candidate.get("proposed_employee_name") or candidate.get("template_name") or "unnamed"
            category = candidate.get("category") or ""
            domains = candidate.get("domains") or []
            rationale = candidate.get("rationale") or ""
            text.append(f"\n   {name}", style="bold white")
            if category:
                text.append(f"  [{category}]", style="dim")
            if domains:
                text.append(f"\n   domains: {', '.join(str(d) for d in domains[:6])}", style="#a78bfa")
            if rationale:
                text.append(f"\n   {truncate_text(str(rationale), 120)}", style="dim italic")

        existing = proposal.get("existing_employee")
        if existing and isinstance(existing, dict):
            emp_name = existing.get("employee_name") or "?"
            emp_id = existing.get("employee_id") or ""
            score_raw = existing.get("experience_score")
            score = float(score_raw) if score_raw is not None else 0.0
            domains = existing.get("domains") or []
            rationale = existing.get("rationale") or ""
            text.append(f"\n   {emp_name}", style="bold white")
            if emp_id:
                text.append(f"  ({emp_id})", style="dim")
            text.append(f"\n   score: {_progress_bar(score)} {int(score * 100)}%", style="#22c55e")
            if domains:
                text.append(f"\n   domains: {', '.join(str(d) for d in domains[:6])}", style="#a78bfa")
            if rationale:
                text.append(f"\n   {truncate_text(str(rationale), 120)}", style="dim italic")

        top_rationale = proposal.get("rationale") or ""
        if top_rationale and not candidate and not existing:
            text.append(f"\n   {truncate_text(str(top_rationale), 120)}", style="dim italic")

        return text

    # ----- Reorg -----

    def _render_reorg_checkpoint(self, checkpoint: PendingCheckpointView) -> RenderableType:
        payload = checkpoint.payload
        title = payload.get("title", "") or "Company Reorg"
        scope = payload.get("scope", "org_mutation")
        risk = payload.get("risk_level", "medium")
        summary = payload.get("summary", "") or checkpoint.summary
        rationale = payload.get("rationale", "")
        role_changes = payload.get("role_changes", [])
        impact = payload.get("impact_summary", {})

        header = Text()
        header.append("REORG", style="bold #fbbf24")
        header.append("  ")
        scope_label = _SCOPE_LABELS.get(scope, scope)
        header += badge(scope_label, "bold black on #64748b")
        header.append("  Risk: ", style="dim")
        risk_style = _RISK_STYLES.get(risk, "bold #f59e0b")
        header.append(f"\u25a0 {risk.upper()}", style=risk_style)

        parts: list[RenderableType] = [header]

        if summary:
            parts.append(Text(f"\n{truncate_text(summary, 280)}", style="white"))
        if rationale:
            parts.append(Text(f"\n{truncate_text(rationale, 200)}", style="dim italic"))

        if role_changes:
            changes_text = Text("\nRole Changes:", style="bold #cbd5e1")
            for rc in role_changes[:8]:
                action = rc.get("action", "?")
                prefix, style = _CHANGE_ACTION_STYLES.get(action, ("? ", "bold white"))
                changes_text.append(f"\n  {prefix}", style=style)
                changes_text.append(rc.get("role_id", "?"), style="bold white")
                replacement = rc.get("replacement_role_id", "")
                if replacement:
                    changes_text.append(f" \u2192 {replacement}", style="dim")
                reason = rc.get("reason", "")
                if reason:
                    changes_text.append(f"  {truncate_text(reason, 50)}", style="dim italic")
            parts.append(changes_text)

        if impact:
            impact_text = Text("\nImpact: ", style="dim")
            impact_parts = []
            if impact.get("affected_roles") is not None:
                impact_parts.append(f"{impact['affected_roles']} roles")
            if impact.get("affected_tasks") is not None:
                impact_parts.append(f"{impact['affected_tasks']} tasks")
            if impact.get("migration_count") is not None:
                impact_parts.append(f"{impact['migration_count']} migrations")
            if impact_parts:
                impact_text.append(", ".join(impact_parts), style="white")
            parts.append(impact_text)

        parts.append(Text("\n[a] Approve  [d] Deny  [e] Feedback", style="dim italic"))

        return Panel(Group(*parts), title=f"Checkpoint: {title}", border_style="#fbbf24")

    # ----- Generic fallback -----

    def _render_generic_checkpoint(self, checkpoint: PendingCheckpointView) -> RenderableType:
        section = Text()
        section.append(checkpoint.checkpoint_type.upper(), style="bold #fbbf24")
        section.append("  ")
        section += badge("PENDING", status_style("warn"))
        if checkpoint.summary:
            section.append(f"\n{truncate_text(checkpoint.summary, 120)}", style="white")
        section.append(f"\n{truncate_text(checkpoint.prompt, 420)}", style="dim")
        if checkpoint.checkpoint_type in {"tool_permission", "action_permission"}:
            section.append(
                "\n[a/e] Disabled here (use Office UI for exact review)  [d] Deny",
                style="dim italic",
            )
        else:
            section.append("\n[a] Approve  [d] Deny  [e] Feedback", style="dim italic")
        return Panel(section, title="Checkpoint", border_style="#fbbf24")

    # ----- Shared renderers -----

    def _render_linked(self) -> Text:
        title = "Linked Execution Turns" if self.state.snapshot.mode == "company" else "Linked Executions"
        linked_text = Text(f"\n{title}\n", style="bold #cbd5e1")
        for linked in self.detail.linked_executions[:8]:
            linked_text.append(f"\u2022 {truncate_text(linked.title, 40)}", style="white")
            linked_text.append(f"  {linked.status}", style=status_style(linked.status))
            linked_text.append(f"  {humanize_age(linked.updated_at)}", style="dim")
            if linked.assigned_to:
                linked_text.append(f"  {truncate_text(linked.assigned_to, 16)}", style="dim")
            linked_text.append("\n")
        return linked_text

    @staticmethod
    def _render_section(title: str, body: str, limit: int) -> Text:
        section = Text(f"\n{title}\n", style="bold #cbd5e1")
        section.append(truncate_text(body, limit), style="white")
        return section
